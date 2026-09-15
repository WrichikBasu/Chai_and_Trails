import random
import unicodedata
from datetime import timedelta
from typing import Any, Final

from anyascii import anyascii
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

# How long a forum's milestone marker stays lit after its latest post. A stand-in
# until per-member unread tracking exists.
FRESH_WINDOW: Final[timedelta] = timedelta(hours=3)

SLUG_MAX_LENGTH: Final[int] = 60
SLUG_FALLBACK: Final[str] = 'thread'


def transliterated_slug(title: str) -> str:
    """An ASCII slug for any title, in any script: "मनाली से काज़ा" -> "mnali-se-kaja".

    anyascii writes each character in Latin letters. Symbols such as emoji are
    dropped first, so "🏍️ Spiti" gives "spiti" rather than "motorcycle-spiti".
    Long slugs are cut at a word boundary, and a title with nothing usable
    gives "thread".
    """
    words = ''.join(' ' if unicodedata.category(ch).startswith('S') else ch for ch in title)
    slug = slugify(anyascii(words))
    if len(slug) > SLUG_MAX_LENGTH:
        slug = slug[:SLUG_MAX_LENGTH + 1].rsplit('-', 1)[0] if '-' in slug[:SLUG_MAX_LENGTH] else slug[:SLUG_MAX_LENGTH]
    return slug.strip('-_') or SLUG_FALLBACK


class Tone(models.IntegerChoices):
    """Avatar colours, matching the .tone-1 ... .tone-6 CSS classes."""
    GREEN = 1
    CHAI = 2
    BLUE = 3
    PLUM = 4
    OCHRE = 5
    OLIVE = 6


def random_tone() -> int:
    return random.choice(Tone.values)


class User(AbstractUser):
    # One free-form name instead of Django's first/last split, which doesn't fit
    # every name. Usernames can't contain spaces, so this is what posts show.
    first_name = None
    last_name = None
    display_name = models.CharField(
        max_length=150, blank=True,
        help_text='Shown on posts and profiles. Left blank, the username is used.',
    )
    avatar_tone = models.PositiveSmallIntegerField(choices=Tone, default=random_tone)
    rank = models.CharField(max_length=40, default='Member')
    location = models.CharField('based in', max_length=100, blank=True)
    rides = models.CharField(max_length=100, blank=True, help_text='Bike, car or other ride shown on posts.')
    # Kept up to date by posting, like the forum counts, so posts can show it without counting.
    post_count = models.PositiveIntegerField(default=0, editable=False)

    class Meta(AbstractUser.Meta):
        constraints = [
            models.CheckConstraint(
                condition=models.Q(avatar_tone__in=Tone.values),
                name='user_avatar_tone_in_palette',
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self.display_name:
            self.display_name = self.username
        super().save(*args, **kwargs)

    # AbstractUser builds these from first_name and last_name, which no longer exist.
    def get_full_name(self) -> str:
        return self.display_name

    def get_short_name(self) -> str:
        return self.display_name


class Category(models.Model):
    slug = models.SlugField(unique=True)
    title = models.CharField(max_length=120)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['position', 'title']
        verbose_name_plural = 'categories'

    def __str__(self) -> str:
        return self.title


class Forum(models.Model):

    # A top-level forum sits in a category; a subforum sits in its parent and
    # inherits the category from there. So category.forums is the top level only.
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.PROTECT, related_name='forums')
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT, related_name='children')
    slug = models.SlugField(unique=True)
    title = models.CharField(max_length=120)
    description = models.TextField()
    position = models.PositiveSmallIntegerField(default=0)

    # Kept up to date on each post, so the index never counts posts per visit.
    thread_count = models.PositiveIntegerField(default=0)
    post_count = models.PositiveIntegerField(default=0)
    last_post = models.ForeignKey('Post', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')

    class Meta:
        ordering = ['position', 'title']
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(category__isnull=False, parent__isnull=True)
                    | models.Q(category__isnull=True, parent__isnull=False)
                ),
                name='forum_in_category_xor_parent',
                violation_error_message='A forum needs either a category (top level) or a parent forum (subforum), but not both.',
            ),
        ]

    def __str__(self) -> str:
        return self.title

    def clean(self) -> None:
        # A loop (A inside B inside A) would make ancestors() walk forever.
        parent = self.parent
        while parent is not None:
            if parent.pk == self.pk:
                raise ValidationError({'parent': 'A forum cannot sit inside itself or one of its own subforums.'})
            parent = parent.parent

    def ancestors(self) -> list[Forum]:
        """Parent forums, outermost first; empty for a top-level forum."""
        chain: list[Forum] = []
        parent = self.parent
        while parent is not None:
            chain.insert(0, parent)
            parent = parent.parent
        return chain

    @property
    def is_fresh(self) -> bool:
        return self.last_post is not None and timezone.now() - self.last_post.created_at < FRESH_WINDOW


class Thread(models.Model):
    forum = models.ForeignKey(Forum, on_delete=models.PROTECT, related_name='threads')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='threads')
    title = models.CharField(max_length=200)
    is_pinned = models.BooleanField('pinned', default=False)
    is_closed = models.BooleanField('closed', default=False)
    reply_count = models.PositiveIntegerField(default=0)
    view_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    last_post = models.ForeignKey('Post', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    # Copy of last_post.created_at, so "Recent activity" sorts on one indexed column.
    last_posted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-is_pinned', '-last_posted_at', '-id']  # id breaks ties between threads active at the same instant
        indexes = [models.Index(fields=['forum', '-is_pinned', '-last_posted_at'])]

    def __str__(self) -> str:
        return self.title

    @property
    def slug(self) -> str:
        # Worked out from the current title, never stored: the id finds the thread, and the
        # view redirects any outdated slug, so a renamed thread's old links keep working.
        return transliterated_slug(self.title)

    def get_absolute_url(self) -> str:
        return reverse('thread', args=[self.pk, self.slug])

    def latest_post_url(self, post: Post) -> str:
        """Where a just-written post appears: the thread's last page, scrolled to the post."""
        return f'{self.get_absolute_url()}?page=last#post-{post.pk}'


class Post(models.Model):

    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name='posts')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='posts')

    body_source = models.TextField()
    '''what the member typed; edited and re-rendered from here'''

    body_html = models.TextField()
    '''rendered and sanitised on save; what pages show'''

    created_at = models.DateTimeField(default=timezone.now)
    edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at', 'id']  # id breaks ties between posts made at the same instant
        indexes = [models.Index(fields=['thread', 'created_at'])]

    def __str__(self) -> str:
        return f'Post {self.pk} in {self.thread}'


class Attachment(models.Model):
    # Empty while the member is still writing, so photos can upload before the post exists.
    post = models.ForeignKey(Post, null=True, blank=True, on_delete=models.CASCADE, related_name='attachments')
    uploader = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='attachments')
    file = models.ImageField(upload_to='attachments/%Y/%m/', width_field='width', height_field='height')
    width = models.PositiveIntegerField(null=True, editable=False)
    height = models.PositiveIntegerField(null=True, editable=False)
    size = models.PositiveIntegerField(editable=False)  # bytes
    created_at = models.DateTimeField(default=timezone.now)

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.size = self.file.size
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.file.name
