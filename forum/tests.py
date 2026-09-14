from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from .forum_data import iter_forums, load_categories
from .models import Category, Forum, Thread, Tone, User


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


class ForumDataTests(SimpleTestCase):
    def test_slugs_are_unique(self) -> None:
        slugs: list[str] = [loc.forum['slug'] for loc in iter_forums(load_categories())]
        self.assertEqual(len(slugs), len(set(slugs)))


class ForumPageTests(SimpleTestCase):
    def test_index_lists_every_category_and_forum(self) -> None:
        response = self.client.get(reverse('index'))
        self.assertEqual(response.status_code, 200)
        for loc in iter_forums(load_categories()):
            self.assertContains(response, f'id="cat-{loc.category["slug"]}"')
            self.assertContains(response, reverse('forum', args=[loc.forum['slug']]))

    def test_every_forum_page_renders(self) -> None:
        for loc in iter_forums(load_categories()):
            with self.subTest(slug=loc.forum['slug']):
                response = self.client.get(reverse('forum', args=[loc.forum['slug']]))
                self.assertContains(response, f'<h1>{loc.forum["title"]}</h1>', html=True)

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
