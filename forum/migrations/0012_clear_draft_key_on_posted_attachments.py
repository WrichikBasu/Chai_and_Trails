"""Drop the draft key from photos that a post has already claimed.

The key says which unposted photos belong to one draft, so that posting deletes
only that draft's leftovers. Once a post owns the photo the key means nothing.
Posting now clears it (forum/posting.py), and this clears the ones written before
that, so the rule holds for every row: only a waiting photo carries a draft key.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('forum', '0011_post_search_vector_thread_search_vector_and_more'),
    ]

    operations = [
        migrations.RunSQL(
            sql="UPDATE forum_attachment SET draft_key = '' WHERE post_id IS NOT NULL AND draft_key <> '';",
            # Nothing to undo: the keys named drafts that were finished with long ago,
            # and no behaviour depends on getting them back.
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
