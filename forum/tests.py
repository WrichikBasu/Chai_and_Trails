import json
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.db.models import Model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import dateformat, timezone

from .management.commands.import_forums import DATA_FILE, JsonData, JsonForum, parse_when
from .models import Category, Forum, Post, Thread, Tone, User
from .templatetags.forum_extras import forum_time


class UserModelTests(TestCase):
    def test_custom_user_model_is_active(self) -> None:
        self.assertIs(get_user_model(), User)

    def test_new_member_gets_profile_defaults(self) -> None:
        member = User.objects.create_user('meera', password='chai-and-trails')
        self.assertIn(member.avatar_tone, Tone.values)
        self.assertEqual(member.rank, 'Member')
        self.assertEqual(member.location, '')

    def test_tone_outside_palette_is_rejected(self) -> None:
        with self.assertRaises(IntegrityError):
            User.objects.create_user('tenzin', password='chai-and-trails', avatar_tone=7)

    def test_new_passwords_are_hashed_with_argon2(self) -> None:
        member = User.objects.create_user('farida', password='chai-and-trails')
        self.assertTrue(member.password.startswith('argon2$argon2id$'))

    def test_pbkdf2_password_is_rehashed_with_argon2_on_login(self) -> None:
        member = User.objects.create_user('arjun')
        member.password = make_password('chai-and-trails', hasher='pbkdf2_sha256')
        member.save()

        self.assertTrue(member.check_password('chai-and-trails'))
        member.refresh_from_db()
        self.assertTrue(member.password.startswith('argon2$argon2id$'))


class ForumModelTests(TestCase):
    category: Category
    route_notes: Forum
    member: User

    @classmethod
    def setUpTestData(cls) -> None:
        cls.category = Category.objects.create(slug='before-you-go', title='Before you go')
        cls.route_notes = Forum.objects.create(
            category=cls.category, slug='route-notes', title='Route notes', description='Roads.',
        )
        Forum.objects.create(
            parent=cls.route_notes, slug='himalaya-and-ladakh', title='Himalaya and Ladakh', description='Passes.',
        )
        cls.member = User.objects.create_user('meera', password='chai-and-trails')

    def test_category_forums_are_top_level_only(self) -> None:
        self.assertQuerySetEqual(self.category.forums.all(), [self.route_notes])
        self.assertEqual([f.slug for f in self.route_notes.children.all()], ['himalaya-and-ladakh'])

    def test_forum_needs_exactly_one_of_category_or_parent(self) -> None:
        for slug, placement in [
            ('in-both', {'category': self.category, 'parent': self.route_notes}),
            ('in-neither', {}),
        ]:
            with self.subTest(slug=slug), self.assertRaises(IntegrityError), transaction.atomic():
                Forum.objects.create(slug=slug, title=slug, description='', **placement)

    def test_threads_list_pinned_first_then_latest_activity(self) -> None:
        now = timezone.now()
        titles: list[tuple[str, bool, timedelta]] = [
            ('Old pinned rules', True, timedelta(days=90)),
            ('Quiet thread', False, timedelta(days=3)),
            ('Busy thread', False, timedelta(minutes=5)),
        ]
        for title, pinned, age in titles:
            Thread.objects.create(
                forum=self.route_notes, author=self.member, title=title,
                is_pinned=pinned, last_posted_at=now - age,
            )
        self.assertEqual(
            [t.title for t in self.route_notes.threads.all()],
            ['Old pinned rules', 'Busy thread', 'Quiet thread'],
        )


def json_forum_slugs(forums: list[JsonForum]) -> list[str]:
    return [slug for forum in forums for slug in [forum['slug'], *json_forum_slugs(forum['children'])]]


def import_forums() -> None:
    call_command('import_forums', stdout=StringIO())


class ImportForumsTests(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        import_forums()

    def test_every_json_forum_is_imported(self) -> None:
        with DATA_FILE.open(encoding='utf-8') as f:
            data: JsonData = json.load(f)
        slugs = [slug for category in data['categories'] for slug in json_forum_slugs(category['forums'])]
        self.assertCountEqual(Forum.objects.values_list('slug', flat=True), slugs)
        self.assertEqual(Category.objects.count(), len(data['categories']))

    def test_running_again_creates_no_duplicates(self) -> None:
        models: list[type[Model]] = [Category, Forum, Thread, Post, User]
        before = [model.objects.count() for model in models]
        import_forums()
        self.assertEqual([model.objects.count() for model in models], before)

    def test_parent_shares_its_subforums_latest_post(self) -> None:
        route_notes = Forum.objects.get(slug='route-notes')
        himalaya = Forum.objects.get(slug='himalaya-and-ladakh')
        self.assertEqual(route_notes.last_post, himalaya.last_post)
        self.assertEqual(himalaya.last_post.thread.forum, himalaya)

    def test_sample_members_cannot_log_in(self) -> None:
        tenzin = User.objects.get(username='tenzin-norbu')
        self.assertEqual(tenzin.display_name, 'Tenzin Norbu')
        self.assertFalse(tenzin.has_usable_password())

    def test_display_times_are_parsed(self) -> None:
        now = timezone.localtime()
        self.assertEqual(parse_when('22 minutes ago', now), now - timedelta(minutes=22))
        self.assertEqual(parse_when('1 hour ago', now), now - timedelta(hours=1))
        yesterday = parse_when('Yesterday at 9:14 PM', now)
        self.assertEqual((yesterday.date(), yesterday.hour, yesterday.minute), (now.date() - timedelta(days=1), 21, 14))
        tuesday = parse_when('Tuesday at 7:02 AM', now)
        self.assertEqual(tuesday.strftime('%A %H:%M'), 'Tuesday 07:02')
        self.assertTrue(timedelta(0) < now - tuesday <= timedelta(days=7))


class ForumTimeTests(SimpleTestCase):
    def test_recent_times_are_relative(self) -> None:
        now = timezone.now()
        self.assertEqual(forum_time(now - timedelta(seconds=10)), 'Just now')
        self.assertEqual(forum_time(now - timedelta(minutes=1, seconds=5)), '1 minute ago')
        self.assertEqual(forum_time(now - timedelta(minutes=22, seconds=5)), '22 minutes ago')
        self.assertEqual(forum_time(now - timedelta(hours=2, minutes=1)), '2 hours ago')

    def test_older_times_use_day_then_date(self) -> None:
        now = timezone.now()
        self.assertTrue(forum_time(now - timedelta(days=1)).startswith('Yesterday at '))
        self.assertEqual(forum_time(now - timedelta(days=3)).split(' at ')[0],
                         timezone.localtime(now - timedelta(days=3)).strftime('%A'))
        self.assertEqual(forum_time(now - timedelta(days=30)), dateformat.format(timezone.localtime(now - timedelta(days=30)), 'j M Y'))


class ForumPageTests(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        import_forums()

    def test_index_lists_every_category_and_forum(self) -> None:
        response = self.client.get(reverse('index'))
        self.assertEqual(response.status_code, 200)
        for category in Category.objects.all():
            self.assertContains(response, f'id="cat-{category.slug}"')
        for forum in Forum.objects.all():
            self.assertContains(response, reverse('forum', args=[forum.slug]))

    def test_index_shows_each_forums_latest_post(self) -> None:
        response = self.client.get(reverse('index'))
        self.assertContains(response, 'Manali to Kaza, first week of June')
        self.assertContains(response, '<a href="profile.html">Tenzin Norbu</a> &middot; 22 minutes ago')
        self.assertContains(response, '61,908')

    def test_index_needs_three_queries(self) -> None:
        with self.assertNumQueries(3):  # categories, their forums with latest posts, subforums
            self.client.get(reverse('index'))

    def test_every_forum_page_renders(self) -> None:
        for forum in Forum.objects.all():
            with self.subTest(slug=forum.slug):
                response = self.client.get(reverse('forum', args=[forum.slug]))
                self.assertContains(response, f'<h1>{forum.title}</h1>', html=True)

    def test_subforum_breadcrumb_links_to_parent(self) -> None:
        response = self.client.get(reverse('forum', args=['himalaya-and-ladakh']))
        self.assertContains(response, f'<a href="{reverse("index")}#cat-before-you-go">Before you go</a>', html=True)
        self.assertContains(response, f'<a href="{reverse("forum", args=["route-notes"])}">Route notes</a>', html=True)
        self.assertContains(response, '<li aria-current="page">Himalaya and Ladakh</li>', html=True)

    def test_forum_without_subforums_hides_section(self) -> None:
        response = self.client.get(reverse('forum', args=['off-topic']))
        self.assertNotContains(response, 'Sections inside')

    def test_unknown_forum_is_404(self) -> None:
        response = self.client.get(reverse('forum', args=['no-such-forum']))
        self.assertEqual(response.status_code, 404)
