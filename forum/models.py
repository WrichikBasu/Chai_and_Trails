import random
from typing import Any

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


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
    avatar_tone = models.PositiveSmallIntegerField(choices=Tone, default=random_tone)
    rank = models.CharField(max_length=40, default='Member')
    location = models.CharField('based in', max_length=100, blank=True)
    rides = models.CharField(max_length=100, blank=True, help_text='Bike, car or other ride shown on posts.')

    class Meta(AbstractUser.Meta):
        constraints = [
            models.CheckConstraint(
                condition=models.Q(avatar_tone__in=Tone.values),
                name='user_avatar_tone_in_palette',
            ),
        ]


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
            ),
        ]

    def __str__(self) -> str:
        return self.title


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
        ordering = ['-is_pinned', '-last_posted_at']
        indexes = [models.Index(fields=['forum', '-is_pinned', '-last_posted_at'])]

    def __str__(self) -> str:
        return self.title


class Post(models.Model):
    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name='posts')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='posts')
    body_source = models.TextField()  # what the member typed; edited and re-rendered from here
    body_html = models.TextField()  # rendered and sanitised on save; what pages show
    created_at = models.DateTimeField(default=timezone.now)
    edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']
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
