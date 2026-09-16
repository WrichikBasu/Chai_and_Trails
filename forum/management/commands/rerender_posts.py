"""Rebuild every post's HTML from its Markdown.

Photo addresses are written into each post's HTML when it's saved. Run this
after anything that changes how posts render: moving photos to other storage
(such as S3 or a CDN, which changes their URLs), or a change to forum.rendering.
"""

from typing import Any

from django.core.management.base import BaseCommand
from django.db.models import Prefetch

from forum.models import Attachment, Post
from forum.posting import render_post


class Command(BaseCommand):
    help = "Rebuild every post's HTML from its Markdown, with current photo addresses."

    def handle(self, *args: Any, **options: Any) -> None:
        changed = 0
        posts = Post.objects.prefetch_related(Prefetch('attachments', queryset=Attachment.objects.all()))
        for post in posts.iterator(chunk_size=500):
            html = render_post(post)
            if html != post.body_html:
                Post.objects.filter(pk=post.pk).update(body_html=html)
                changed += 1
        self.stdout.write(self.style.SUCCESS(f'Re-rendered posts: {changed} changed.'))
