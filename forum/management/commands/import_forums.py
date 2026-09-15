"""Load categories and forums from data/forums.json, with each forum's latest post as sample content.

Safe to run again: forums are matched by slug, sample members by username and
sample threads by title, and the sample post times are refreshed from the JSON.
"""

import json
import re
from argparse import ArgumentParser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, TypedDict

from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.text import slugify

from forum.models import Category, Forum, Post, Thread, User
from forum.rendering import render_body

DATA_FILE: Final[Path] = Path(__file__).resolve().parents[2] / 'data' / 'forums.json'

WEEKDAYS: Final[list[str]] = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
AGO: Final[re.Pattern[str]] = re.compile(r'(\d+) (minute|hour)s? ago')
DAY_AT: Final[re.Pattern[str]] = re.compile(r'(\w+) at (\d{1,2}:\d{2} [AP]M)')


class JsonLastPost(TypedDict):
    title: str
    author: str
    tone: int  # avatar colour, 1-6
    when: str  # formatted for display, e.g. "22 minutes ago" or "Tuesday at 9:12 AM"


class JsonForum(TypedDict):
    slug: str
    title: str
    description: str
    threads: int
    posts: int
    fresh: bool  # not imported; the marker now follows the latest post's age
    last_post: JsonLastPost
    children: list[JsonForum]


class JsonCategory(TypedDict):
    slug: str
    title: str
    forums: list[JsonForum]


class JsonData(TypedDict):
    categories: list[JsonCategory]


def parse_when(when: str, now: datetime) -> datetime:
    """Turn a display string such as "2 hours ago" or "Yesterday at 9:14 PM" back into a time."""
    if match := AGO.fullmatch(when):
        amount = int(match[1])
        return now - (timedelta(minutes=amount) if match[2] == 'minute' else timedelta(hours=amount))

    if match := DAY_AT.fullmatch(when):
        day = match[1]
        if day == 'Today':
            days_back = 0
        elif day == 'Yesterday':
            days_back = 1
        elif day in WEEKDAYS:
            days_back = (now.weekday() - WEEKDAYS.index(day)) % 7 or 7
        else:
            raise CommandError(f'Unrecognised day in {when!r}')
        clock = datetime.strptime(match[2], '%I:%M %p').time()
        moment = datetime.combine(now.date() - timedelta(days=days_back), clock, tzinfo=now.tzinfo)
        return min(moment, now)  # "Today at 6:41 AM" imported at 5 AM

    raise CommandError(f'Unrecognised time {when!r}')


class Command(BaseCommand):
    help = 'Load categories and forums from forum/data/forums.json, with a sample latest post for each forum.'

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument('path', nargs='?', type=Path, default=DATA_FILE, help='JSON file to import')

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        path: Path = options['path']
        with path.open(encoding='utf-8') as f:
            data: JsonData = json.load(f)

        now = timezone.localtime()
        for position, entry in enumerate(data['categories']):
            category, _ = Category.objects.update_or_create(
                slug=entry['slug'],
                defaults={'title': entry['title'], 'position': position},
            )
            for forum_position, forum_entry in enumerate(entry['forums']):
                self.import_forum(forum_entry, forum_position, category, None, now)

        # Recount every member's posts. New sample members start at 0, and for everyone
        # else the recount matches the count posting already keeps.
        per_author = Post.objects.filter(author=OuterRef('pk')).values('author').annotate(total=Count('pk')).values('total')
        User.objects.update(post_count=Coalesce(Subquery(per_author), 0))

        self.stdout.write(self.style.SUCCESS(
            f'Imported {len(data["categories"])} categories and {Forum.objects.count()} forums from {path.name}.'
        ))

    def import_forum(
        self, entry: JsonForum, position: int, category: Category | None, parent: Forum | None, now: datetime,
    ) -> None:
        forum, _ = Forum.objects.update_or_create(
            slug=entry['slug'],
            defaults={
                'category': category,
                'parent': parent,
                'title': entry['title'],
                'description': entry['description'],
                'position': position,
                # The mockup's totals, so the index reads as before. From here on
                # the counts move with each new thread and post.
                'thread_count': entry['threads'],
                'post_count': entry['posts'],
            },
        )
        # Subforums first: a parent's latest post is often a subforum's, and should
        # reuse that thread rather than create a copy in the parent.
        for child_position, child in enumerate(entry['children']):
            self.import_forum(child, child_position, None, forum, now)

        forum.last_post = self.sample_post(forum, entry['last_post'], now)
        forum.save(update_fields=['last_post'])

    def sample_post(self, forum: Forum, entry: JsonLastPost, now: datetime) -> Post:
        author = self.sample_member(entry)
        posted_at = parse_when(entry['when'], now)
        times = {'created_at': posted_at, 'last_posted_at': posted_at}

        thread, _ = Thread.objects.update_or_create(
            title=entry['title'], author=author,
            defaults=times,
            create_defaults={'forum': forum, **times},
        )
        body = f'Sample post for "{entry["title"]}", imported from forums.json.'
        post, _ = Post.objects.update_or_create(
            thread=thread, author=author,
            defaults={'created_at': posted_at},
            create_defaults={'created_at': posted_at, 'body_source': body, 'body_html': render_body(body)},
        )
        thread.last_post = post
        thread.save(update_fields=['last_post'])
        return post

    def sample_member(self, entry: JsonLastPost) -> User:
        profile = {'display_name': entry['author'], 'avatar_tone': entry['tone']}
        member, _ = User.objects.update_or_create(
            username=slugify(entry['author']),
            defaults=profile,
            create_defaults={**profile, 'password': make_password(None)},  # unusable: can't log in
        )
        return member
