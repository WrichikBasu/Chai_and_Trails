from django.test import SimpleTestCase
from django.urls import reverse

from .forum_data import iter_forums, load_categories


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
