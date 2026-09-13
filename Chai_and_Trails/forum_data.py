"""Read the forum tree (categories, forums, subforums) from data/forums.json."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Final, NamedTuple, TypedDict

DATA_FILE: Final[Path] = Path(__file__).resolve().parent / 'data' / 'forums.json'


class LastPost(TypedDict):
    title: str
    author: str
    tone: int  # avatar colour, matches the .tone-1 ... .tone-6 CSS classes
    when: str  # already formatted for display, e.g. "22 minutes ago"


class Forum(TypedDict):
    slug: str
    title: str
    description: str
    threads: int
    posts: int
    fresh: bool  # has unread posts; lights up the milestone marker
    last_post: LastPost
    children: list[Forum]


class Category(TypedDict):
    slug: str
    title: str
    forums: list[Forum]


class ForumData(TypedDict):
    categories: list[Category]


class ForumLocation(NamedTuple):
    forum: Forum
    category: Category
    parents: list[Forum]  # outermost first; empty for a top-level forum


def load_categories() -> list[Category]:
    # Read on every call so edits to the JSON show up without restarting the server.
    with DATA_FILE.open(encoding='utf-8') as f:
        data: ForumData = json.load(f)
    return data['categories']


def iter_forums(categories: list[Category]) -> Iterator[ForumLocation]:
    """Yield every forum at any depth, with the category and parents it sits under."""
    def walk(forums: list[Forum], category: Category, parents: list[Forum]) -> Iterator[ForumLocation]:
        for forum in forums:
            yield ForumLocation(forum, category, parents)
            yield from walk(forum['children'], category, [*parents, forum])

    for category in categories:
        yield from walk(category['forums'], category, [])


def find_forum(slug: str) -> ForumLocation | None:
    return next((loc for loc in iter_forums(load_categories()) if loc.forum['slug'] == slug), None)
