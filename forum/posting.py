"""Writing threads and replies, and keeping every stored count in step with them.

The views call these instead of saving models themselves, so the rules for
counts live in one place. Each function is one transaction: the post and all
of its count changes are saved together or not at all.

Counts are changed with F() expressions ("post_count = post_count + 1"), so
PostgreSQL does the arithmetic on the current value. Two replies saved at the
same moment both land, where reading, adding and saving in Python would lose one.
"""

from collections.abc import Sequence

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import Attachment, Forum, Post, Thread, User
from .photos import PreparedPhoto
from .rendering import photo_ids, photo_markdown, render_body


@transaction.atomic
def start_thread(forum: Forum, author: User, title: str, source: str, photos: Sequence[PreparedPhoto] = (),
                 draft: str = '') -> Thread:
    now = timezone.now()
    thread = Thread.objects.create(forum=forum, author=author, title=title, created_at=now, last_posted_at=now)
    post = Post.objects.create(thread=thread, author=author, created_at=now)
    _write_body(post, author, source, photos, draft)
    thread.last_post = post
    thread.save(update_fields=['last_post'])
    _count_post(forum, author, post, new_thread=True)
    return thread


@transaction.atomic
def add_reply(thread: Thread, author: User, source: str, photos: Sequence[PreparedPhoto] = (),
              draft: str = '') -> Post:
    post = Post.objects.create(thread=thread, author=author)
    _write_body(post, author, source, photos, draft)
    Thread.objects.filter(pk=thread.pk).update(reply_count=F('reply_count') + 1)
    # Only if nothing newer got there first: with two replies milliseconds apart, the one
    # whose transaction happens to finish last must not replace the newer latest post.
    Thread.objects.filter(pk=thread.pk, last_posted_at__lte=post.created_at).update(
        last_post=post, last_posted_at=post.created_at,
    )
    _count_post(thread.forum, author, post, new_thread=False)
    return post


def save_photo(uploader: User, photo: PreparedPhoto, post: Post | None = None, draft: str = '') -> Attachment:
    """Write a cleaned photo's files to storage and record it.

    With no post, the photo waits for one: the editor uploads photos as they are
    added, before the post is written, and the post claims them when it's saved.
    `draft` says which draft it came from, so posting that draft can clear it up.
    """
    return Attachment.objects.create(
        post=post,
        uploader=uploader,
        draft_key=draft,
        file=photo.original,
        thumbnail_800=photo.thumbnails.get(800),
        thumbnail_1600=photo.thumbnails.get(1600),
    )


def waiting_photos(member: User, draft: str | None = None) -> list[Attachment]:
    """Photos the member has uploaded that no post has claimed yet, oldest first.

    With a draft key, only that draft's: the tray shows everything waiting, but
    posting only tidies up the draft being posted.
    """
    photos = Attachment.objects.filter(uploader=member, post__isnull=True)
    if draft is not None:
        photos = photos.filter(draft_key=draft)
    return list(photos.order_by('created_at', 'id'))


def discard_photos(photos: Sequence[Attachment]) -> None:
    """Delete these photos: their records now, their files once the change is committed.

    Files are deleted after the commit because a transaction that rolls back would
    otherwise leave records pointing at files that are already gone.
    """
    if not photos:
        return
    files = [stored for photo in photos
             for stored in (photo.file, photo.thumbnail_800, photo.thumbnail_1600) if stored]
    Attachment.objects.filter(pk__in=[photo.pk for photo in photos]).delete()
    transaction.on_commit(lambda: [stored.delete(save=False) for stored in files])


def render_post(post: Post) -> str:
    """The post's HTML from its Markdown, showing the photos that belong to it."""
    return render_body(post.body_source, {photo.pk: photo for photo in post.attachments.all()})


def _write_body(post: Post, author: User, source: str, uploads: Sequence[PreparedPhoto], draft: str = '') -> None:
    """Give the post its photos and text, then render it.

    Photos the text places (uploaded from the editor) are claimed, but only the
    author's own that no post has claimed yet; a reference to anyone else's
    photo shows nothing. Photos sent with the form instead (no JavaScript) are
    saved and placed at the end of the text.

    Posting also clears up after this draft: photos uploaded from the same editor
    but left out of the post are deleted, files and all. Photos from the member's
    other drafts (another tab, another thread) are untouched.
    """
    Attachment.objects.filter(pk__in=photo_ids(source), uploader=author, post__isnull=True).update(post=post)
    added = [save_photo(author, photo, post) for photo in uploads]
    if added:
        source = '\n\n'.join([source.rstrip(), *(photo_markdown(photo.pk) for photo in added)])
    post.body_source = source
    post.body_html = render_post(post)
    post.save(update_fields=['body_source', 'body_html'])
    if draft:
        discard_photos(waiting_photos(author, draft))  # this draft's leftovers were not wanted


def _count_post(forum: Forum, author: User, post: Post, *, new_thread: bool) -> None:
    """Count the post in its forum and every forum above it, and on its author."""
    forums = Forum.objects.filter(pk__in=[forum.pk, *(parent.pk for parent in forum.ancestors())])
    forums.update(post_count=F('post_count') + 1, thread_count=F('thread_count') + int(new_thread))
    forums.filter(Q(last_post__isnull=True) | Q(last_post__created_at__lte=post.created_at)).update(last_post=post)
    User.objects.filter(pk=author.pk).update(post_count=F('post_count') + 1)
