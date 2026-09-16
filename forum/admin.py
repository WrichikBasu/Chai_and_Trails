from typing import Any

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.http import HttpRequest
from django.utils import timezone
from django.utils.html import format_html
from django.utils.text import Truncator

from .models import Attachment, Category, Forum, Post, Thread, User
from .posting import render_post

PROFILE_FIELDS: tuple[str, ...] = ('display_name', 'avatar_tone', 'rank', 'location', 'rides')


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    # Django's own fieldsets list first_name and last_name, which this model replaces.
    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        ('Forum profile', {'fields': (*PROFILE_FIELDS, 'email', 'post_count')}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        ('Important dates', {'fields': ('last_login', 'date_joined')}),
    )
    add_fieldsets = (*BaseUserAdmin.add_fieldsets, ('Forum profile', {'fields': (*PROFILE_FIELDS, 'email')}))
    list_display = ('username', 'display_name', 'email', 'rank', 'location', 'date_joined', 'is_staff')
    list_filter = (*BaseUserAdmin.list_filter, 'rank')
    search_fields = ('username', 'display_name', 'email')
    readonly_fields = ('post_count',)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('title', 'slug', 'position')
    list_editable = ('position',)
    prepopulated_fields = {'slug': ('title',)}


@admin.register(Forum)
class ForumAdmin(admin.ModelAdmin):
    list_display = ('title', 'placement', 'position', 'thread_count', 'post_count')
    list_editable = ('position',)
    list_filter = ('category', ('parent', admin.RelatedOnlyFieldListFilter))
    list_select_related = ('category', 'parent')
    search_fields = ('title', 'slug', 'description')
    prepopulated_fields = {'slug': ('title',)}
    fieldsets = (
        (None, {'fields': ('title', 'slug', 'description')}),
        ('Placement', {
            'fields': ('category', 'parent', 'position'),
            'description': 'Pick a category for a top-level forum, or a parent forum for a subforum. Not both.',
        }),
        ('Activity', {'fields': ('thread_count', 'post_count', 'last_post')}),
    )
    # Kept up to date by posting, so hand edits would only make them wrong.
    readonly_fields = ('thread_count', 'post_count', 'last_post')

    @admin.display(description='Sits in')
    def placement(self, forum: Forum) -> str:
        return str(forum.category) if forum.category else f'↳ {forum.parent}'


@admin.register(Thread)
class ThreadAdmin(admin.ModelAdmin):
    list_display = ('title', 'forum', 'author', 'is_pinned', 'is_closed', 'reply_count', 'last_posted_at')
    list_editable = ('is_pinned', 'is_closed')
    list_filter = ('is_pinned', 'is_closed', 'forum')
    list_select_related = ('forum', 'author')
    search_fields = ('title', 'author__username', 'author__display_name')
    date_hierarchy = 'last_posted_at'
    fields = (
        'title', 'forum', 'author', 'is_pinned', 'is_closed',
        'reply_count', 'view_count', 'created_at', 'last_posted_at', 'last_post',
    )
    # Moving a thread or changing its author would leave forum counts and latest
    # posts pointing the wrong way, so those stay fixed here.
    readonly_fields = ('forum', 'author', 'reply_count', 'view_count', 'created_at', 'last_posted_at', 'last_post')

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False  # threads start on the site, which also updates the forum's counts


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ('excerpt', 'thread', 'author', 'created_at')
    list_select_related = ('thread', 'author')
    search_fields = ('body_source', 'thread__title', 'author__username', 'author__display_name')
    date_hierarchy = 'created_at'
    fields = ('thread', 'author', 'created_at', 'edited_at', 'body_source', 'body_html')
    # Moderators edit the Markdown; the HTML is always rebuilt from it on save.
    readonly_fields = ('thread', 'author', 'created_at', 'edited_at', 'body_html')

    @admin.display(description='Post')
    def excerpt(self, post: Post) -> str:
        return Truncator(post.body_source).chars(80)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False  # replies are written on the site, which also updates the counts

    def save_model(self, request: HttpRequest, obj: Post, form: Any, change: bool) -> None:
        obj.body_html = render_post(obj)
        obj.edited_at = timezone.now()
        super().save_model(request, obj, form, change)


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    """View and delete photos. Uploading happens on the site, where each photo is checked and cleaned."""

    list_display = ('thumbnail', 'post', 'uploader', 'dimensions', 'size_kb', 'created_at')
    list_select_related = ('post__thread', 'uploader')
    search_fields = ('uploader__username', 'uploader__display_name', 'post__thread__title')
    date_hierarchy = 'created_at'
    fields = ('thumbnail', 'file', 'post', 'uploader', 'dimensions', 'size_kb', 'created_at')
    readonly_fields = fields

    @admin.display(description='Photo')
    def thumbnail(self, attachment: Attachment) -> str:
        return format_html('<img src="{}" alt="" style="height:4rem;border-radius:4px">', attachment.preview_url)

    @admin.display(description='Size')
    def dimensions(self, attachment: Attachment) -> str:
        return f'{attachment.width} × {attachment.height}'

    @admin.display(description='KB', ordering='size')
    def size_kb(self, attachment: Attachment) -> int:
        return round(attachment.size / 1024)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Attachment | None = None) -> bool:
        return False
