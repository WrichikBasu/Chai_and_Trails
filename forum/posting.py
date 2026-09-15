"""Writing threads and replies, and keeping every stored count in step with them.

The views call these instead of saving models themselves, so the rules for
counts live in one place. Each function is one transaction: the post and all
of its count changes are saved together or not at all.

Counts are changed with F() expressions ("post_count = post_count + 1"), so
PostgreSQL does the arithmetic on the current value. Two replies saved at the
same moment both land, where reading, adding and saving in Python would lose one.
"""

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import Forum, Post, Thread, User
from .rendering import render_body


@transaction.atomic
def start_thread(forum: Forum, author: User, title: str, source: str) -> Thread:
    now = timezone.now()
    thread = Thread.objects.create(forum=forum, author=author, title=title, created_at=now, last_posted_at=now)
    post = Post.objects.create(
        thread=thread, author=author, body_source=source, body_html=render_body(source), created_at=now,
    )
    thread.last_post = post
    thread.save(update_fields=['last_post'])
    _count_post(forum, author, post, new_thread=True)
    return thread


@transaction.atomic
def add_reply(thread: Thread, author: User, source: str) -> Post:
    post = Post.objects.create(thread=thread, author=author, body_source=source, body_html=render_body(source))
    Thread.objects.filter(pk=thread.pk).update(reply_count=F('reply_count') + 1)
    # Only if nothing newer got there first: with two replies milliseconds apart, the one
    # whose transaction happens to finish last must not replace the newer latest post.
    Thread.objects.filter(pk=thread.pk, last_posted_at__lte=post.created_at).update(
        last_post=post, last_posted_at=post.created_at,
    )
    _count_post(thread.forum, author, post, new_thread=False)
    return post


def _count_post(forum: Forum, author: User, post: Post, *, new_thread: bool) -> None:
    """Count the post in its forum and every forum above it, and on its author."""
    forums = Forum.objects.filter(pk__in=[forum.pk, *(parent.pk for parent in forum.ancestors())])
    forums.update(post_count=F('post_count') + 1, thread_count=F('thread_count') + int(new_thread))
    forums.filter(Q(last_post__isnull=True) | Q(last_post__created_at__lte=post.created_at)).update(last_post=post)
    User.objects.filter(pk=author.pk).update(post_count=F('post_count') + 1)
