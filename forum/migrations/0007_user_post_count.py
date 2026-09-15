"""Add User.post_count, filled in from the posts members have already written."""

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Count, OuterRef, Subquery
from django.db.models.functions import Coalesce


def count_existing_posts(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    User = apps.get_model('forum', 'User')
    Post = apps.get_model('forum', 'Post')
    per_author = Post.objects.filter(author=OuterRef('pk')).values('author').annotate(total=Count('pk')).values('total')
    User.objects.update(post_count=Coalesce(Subquery(per_author), 0))


class Migration(migrations.Migration):

    dependencies = [
        ('forum', '0006_forum_placement_message'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='post_count',
            field=models.PositiveIntegerField(default=0, editable=False),
        ),
        migrations.RunPython(count_existing_posts, migrations.RunPython.noop),
    ]
