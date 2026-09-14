"""Replace first_name and last_name with a single display_name, keeping existing names."""

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Value
from django.db.models.functions import Coalesce, Concat, NullIf, Trim


def combine_names(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    User = apps.get_model('forum', 'User')
    # "first last", trimmed; the username where both were empty.
    full_name = Trim(Concat('first_name', Value(' '), 'last_name'))
    User.objects.update(display_name=Coalesce(NullIf(full_name, Value('')), 'username'))


def split_names(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    User = apps.get_model('forum', 'User')
    for user in User.objects.all():
        user.first_name, _, user.last_name = user.display_name.partition(' ')
        user.save(update_fields=['first_name', 'last_name'])


class Migration(migrations.Migration):

    dependencies = [
        ('forum', '0002_category_post_forum_attachment_thread_post_thread_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='display_name',
            field=models.CharField(blank=True, help_text='Shown on posts and profiles. Left blank, the username is used.', max_length=150),
        ),
        migrations.RunPython(combine_names, split_names),
        migrations.RemoveField(
            model_name='user',
            name='first_name',
        ),
        migrations.RemoveField(
            model_name='user',
            name='last_name',
        ),
    ]
