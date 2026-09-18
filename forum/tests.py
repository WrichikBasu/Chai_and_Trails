import json
import re
import shutil
import tempfile
import unicodedata
from datetime import timedelta
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.contrib.staticfiles import finders
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.models import Model
from django.http import HttpResponse
from django.templatetags.static import static
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import dateformat, timezone
from PIL import Image

from .management.commands.import_forums import DATA_FILE, JsonData, JsonForum, parse_when
from .forms import MAX_PHOTOS
from .models import SLUG_MAX_LENGTH, Attachment, Category, Forum, Post, Thread, Tone, User, transliterated_slug
from .photos import AVATAR_SIZE, MAX_UPLOAD_BYTES, prepare_avatar, prepare_photo
from .posting import add_reply, save_photo, start_thread, waiting_photos
from .rendering import photo_ids, render_body
from .templatetags.forum_extras import compact_count, forum_time
from .views import MAX_WAITING_PHOTOS, MEMBERS_PER_PAGE, POSTS_PER_PAGE, RECENT_FACES, THREADS_PER_PAGE


class UserModelTests(TestCase):
    def test_custom_user_model_is_active(self) -> None:
        self.assertIs(get_user_model(), User)

    def test_new_member_gets_profile_defaults(self) -> None:
        member = User.objects.create_user('meera', password='chai-and-trails')
        self.assertIn(member.avatar_tone, Tone.values)
        self.assertEqual(member.rank, 'Member')
        self.assertEqual(member.location, '')
        self.assertEqual(member.display_name, 'meera')  # blank falls back to the username

    def test_display_name_is_the_members_full_name(self) -> None:
        member = User.objects.create_user('meera-iyer', display_name='Meera Iyer')
        self.assertEqual(member.display_name, 'Meera Iyer')
        self.assertEqual(member.get_full_name(), 'Meera Iyer')

    def test_password_must_not_resemble_display_name(self) -> None:
        member = User(username='t', display_name='Tenzin Norbu')
        with self.assertRaises(ValidationError):
            validate_password('tenzinnorbu', member)

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
        # A weekday is always in the past week; on a Tuesday it means last Tuesday, up to 8 days back.
        self.assertTrue(timedelta(0) < now - tuesday < timedelta(days=8))


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
        tenzin = User.objects.get(username='tenzin-norbu')
        self.assertContains(response, f'<a href="{tenzin.get_absolute_url()}">Tenzin Norbu</a> &middot; 22 minutes ago')
        self.assertContains(response, '61,908')

    def test_index_needs_seven_queries(self) -> None:
        # categories, their forums with latest posts, subforums, how many members, the newest
        # one, who posted lately, trip logs. The thread and post totals add up forum counts
        # that are already loaded, so they cost nothing.
        with self.assertNumQueries(7):
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


class ForumAdminTests(TestCase):
    staff: User

    @classmethod
    def setUpTestData(cls) -> None:
        import_forums()
        cls.staff = User.objects.create_superuser('boss', 'boss@example.com', 'chai-and-trails')

    def setUp(self) -> None:
        self.client.force_login(self.staff)

    def save_forum(self, url: str, **fields: object) -> HttpResponse:
        data: dict[str, object] = {
            'title': 'Spiti Valley', 'slug': 'spiti-valley', 'description': 'Kaza, Tabo and the loop.',
            'category': '', 'parent': '', 'position': 9, **fields,
        }
        return self.client.post(url, data)

    def test_subforum_added_in_admin_appears_on_the_site(self) -> None:
        route_notes = Forum.objects.get(slug='route-notes')
        response = self.save_forum(reverse('admin:forum_forum_add'), parent=route_notes.pk)
        self.assertRedirects(response, reverse('admin:forum_forum_changelist'))

        self.assertContains(self.client.get(reverse('index')), reverse('forum', args=['spiti-valley']))
        page = self.client.get(reverse('forum', args=['spiti-valley']))
        self.assertContains(page, '<h1>Spiti Valley</h1>', html=True)
        self.assertContains(page, f'<a href="{reverse("forum", args=["route-notes"])}">Route notes</a>', html=True)

        parent_page = self.client.get(reverse('forum', args=['route-notes']))
        self.assertContains(parent_page, 'Spiti Valley')
        self.assertContains(parent_page, 'No posts yet')

    def test_forum_in_both_category_and_parent_is_refused(self) -> None:
        response = self.save_forum(
            reverse('admin:forum_forum_add'),
            category=Category.objects.get(slug='before-you-go').pk,
            parent=Forum.objects.get(slug='route-notes').pk,
        )
        self.assertContains(response, 'A forum needs either a category (top level) or a parent forum')
        self.assertFalse(Forum.objects.filter(slug='spiti-valley').exists())

    def test_forum_cannot_move_inside_its_own_subforum(self) -> None:
        route_notes = Forum.objects.get(slug='route-notes')
        response = self.save_forum(
            reverse('admin:forum_forum_change', args=[route_notes.pk]),
            title=route_notes.title, slug=route_notes.slug, description=route_notes.description,
            parent=Forum.objects.get(slug='himalaya-and-ladakh').pk,
        )
        self.assertContains(response, 'A forum cannot sit inside itself or one of its own subforums.')
        route_notes.refresh_from_db()
        self.assertIsNone(route_notes.parent)

    def test_threads_and_posts_are_not_added_in_admin(self) -> None:
        for url in [reverse('admin:forum_thread_add'), reverse('admin:forum_post_add')]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_admin_pages_load(self) -> None:
        thread = Thread.objects.first()
        assert thread is not None and thread.last_post is not None
        urls: list[str] = [
            *(reverse(f'admin:forum_{name}_changelist') for name in ['user', 'category', 'forum', 'thread', 'post']),
            reverse('admin:forum_forum_change', args=[Forum.objects.get(slug='route-notes').pk]),
            reverse('admin:forum_thread_change', args=[thread.pk]),
            reverse('admin:forum_post_change', args=[thread.last_post.pk]),
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class RegistrationTests(TestCase):
    def register(self, **fields: str) -> HttpResponse:
        data: dict[str, str] = {
            'username': 'meera', 'display_name': 'Meera Iyer', 'email': 'meera@example.com',
            'location': 'Bengaluru', 'password1': 'monsoon-konkan-26', 'password2': 'monsoon-konkan-26',
            'terms': 'on', **fields,
        }
        return self.client.post(reverse('register'), data)

    def test_form_renders_with_a_real_csrf_token(self) -> None:
        response = self.client.get(reverse('register'))
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertNotContains(response, 'EGQ9rWR2Nmrqblq9XlH1PBdXw3rHHyGdDlr6MC7iOoprgH1orcJUuETI5T8mta8A')
        for name in ['username', 'display_name', 'email', 'location', 'password1', 'password2', 'terms']:
            self.assertContains(response, f'name="{name}"')

    def test_registering_creates_the_member_and_logs_them_in(self) -> None:
        response = self.register()
        self.assertRedirects(response, reverse('index'))

        member = User.objects.get(username='meera')
        self.assertEqual((member.display_name, member.email, member.location), ('Meera Iyer', 'meera@example.com', 'Bengaluru'))
        self.assertTrue(member.password.startswith('argon2$argon2id$'))
        self.assertTrue(member.check_password('monsoon-konkan-26'))
        self.assertEqual(int(self.client.session['_auth_user_id']), member.pk)
        # The bar greets them by name, and the name links to their profile.
        self.assertContains(
            self.client.get(reverse('index')),
            f'<a href="{member.get_absolute_url()}">Meera Iyer</a>', html=True)

    def test_registering_works_with_csrf_checks_on(self) -> None:
        browser = Client(enforce_csrf_checks=True)
        browser.get(reverse('register'))  # sets the csrftoken cookie, as a real visit would
        response = browser.post(reverse('register'), {
            'csrfmiddlewaretoken': browser.cookies['csrftoken'].value,
            'username': 'farida', 'email': 'farida@example.com',
            'password1': 'konkan-coast-26', 'password2': 'konkan-coast-26', 'terms': 'on',
        })
        self.assertRedirects(response, reverse('index'), fetch_redirect_response=False)
        self.assertEqual(User.objects.get(username='farida').display_name, 'farida')

    def test_mismatched_passwords_are_refused(self) -> None:
        response = self.register(password2='monsoon-konkan-27')
        self.assertContains(response, 'The two password fields didn’t match.')
        self.assertFalse(User.objects.filter(username='meera').exists())

    def test_weak_password_is_refused(self) -> None:
        response = self.register(username='wanderer', password1='meeraiyer', password2='meeraiyer')
        self.assertContains(response, 'The password is too similar to the display name.')

    def test_rules_must_be_accepted(self) -> None:
        response = self.register(terms='')
        self.assertContains(response, 'Please confirm you have read the forum rules.')

    def test_email_and_username_must_be_unused(self) -> None:
        User.objects.create_user('meera', 'MEERA@example.com', 'x')
        response = self.register(username='Meera')
        self.assertContains(response, 'A user with that username already exists.')
        self.assertContains(response, 'An account with this email already exists.')

    def test_username_cannot_contain_spaces(self) -> None:
        response = self.register(username='meera iyer')
        self.assertContains(response, 'Enter a valid username.')

    def test_members_are_sent_away_from_registration(self) -> None:
        self.client.force_login(User.objects.create_user('tenzin'))
        self.assertRedirects(self.client.get(reverse('register')), reverse('index'))


class LoginTests(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        User.objects.create_user('tenzin', 'tenzin@example.com', 'high-passes-26', display_name='Tenzin Norbu')

    def test_log_in_log_out_and_back_in(self) -> None:
        response = self.client.post(reverse('login'), {'username': 'tenzin', 'password': 'high-passes-26'})
        self.assertRedirects(response, reverse('index'))
        index = self.client.get(reverse('index'))
        tenzin = User.objects.get(username='tenzin')
        self.assertContains(index, f'<a href="{tenzin.get_absolute_url()}">Tenzin Norbu</a>', html=True)
        self.assertContains(index, f'action="{reverse("logout")}"')

        self.assertRedirects(self.client.post(reverse('logout')), reverse('index'))
        self.assertContains(self.client.get(reverse('index')), f'href="{reverse("login")}"')

        response = self.client.post(reverse('login'), {'username': 'tenzin', 'password': 'high-passes-26'})
        self.assertRedirects(response, reverse('index'))

    def test_wrong_password_shows_an_error(self) -> None:
        response = self.client.post(reverse('login'), {'username': 'tenzin', 'password': 'wrong'})
        self.assertContains(response, 'Please enter a correct username and password.')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_login_returns_to_the_page_that_asked(self) -> None:
        forum_url = '/forum/route-notes/'
        response = self.client.post(reverse('login'), {'username': 'tenzin', 'password': 'high-passes-26', 'next': forum_url})
        self.assertRedirects(response, forum_url, fetch_redirect_response=False)

    def test_logout_needs_post(self) -> None:
        self.client.force_login(User.objects.get(username='tenzin'))
        self.assertEqual(self.client.get(reverse('logout')).status_code, 405)


class StaticAssetTests(TestCase):
    def test_shared_assets_are_found(self) -> None:
        for path in ['css/site.css', 'js/site.js', 'js/theme.js',
                     'img/favicon.svg', 'img/favicon-32.png', 'img/apple-touch-icon.png']:
            with self.subTest(path=path):
                self.assertIsNotNone(finders.find(path))

    def test_every_page_carries_the_tab_icon(self) -> None:
        response = self.client.get(reverse('index'))
        self.assertContains(response, f'<link rel="icon" href="{static("img/favicon.svg")}" type="image/svg+xml">')
        self.assertContains(response, f'href="{static("img/apple-touch-icon.png")}"')

    def test_the_root_favicon_request_is_sent_to_the_icon(self) -> None:
        # Browsers ask for /favicon.ico by themselves; without this it is a 404 in the log.
        response = self.client.get('/favicon.ico')
        self.assertRedirects(response, static('img/favicon-32.png'), fetch_redirect_response=False)
        self.assertEqual(response.status_code, 302)  # not 301: the address changes when the icon does

    def test_the_icon_is_a_real_image_of_the_right_size(self) -> None:
        # The PNGs are rasterised from the SVGs; a wrong size here means they were rebuilt badly.
        for path, size in [('img/favicon-32.png', (32, 32)), ('img/apple-touch-icon.png', (180, 180))]:
            with self.subTest(path=path), Image.open(finders.find(path)) as icon:
                self.assertEqual(icon.size, size)
        svg = Path(finders.find('img/favicon.svg')).read_text(encoding='utf-8')
        self.assertIn('viewBox="0 0 32 32"', svg)
        self.assertIn('#f2b01e', svg)  # the same milestone yellow as the masthead mark

    def test_quote_button_script_has_no_raw_line_break_in_a_string(self) -> None:
        # Regression: the quote stub once held literal line breaks, a syntax error that stopped the whole script.
        script = Path(finders.find('js/site.js')).read_text(encoding='utf-8')
        self.assertIn("replyBox.value += (replyBox.value ? '\\n\\n' : '') + lines.join('\\n') + '\\n\\n';", script)
        self.assertNotIn('[QUOTE=', script)  # quotes are Markdown now, not BBCode

    def test_every_page_links_the_shared_assets_instead_of_inline_code(self) -> None:
        import_forums()
        member = Client()
        member.force_login(User.objects.create_user('reader'))  # new-thread needs a member
        stylesheet = f'<link rel="stylesheet" href="{static("css/site.css")}">'
        script = f'<script src="{static("js/site.js")}"></script>'
        thread = Thread.objects.first()
        assert thread is not None
        urls: list[str] = [
            reverse(name) for name in ['index', 'new_thread', 'members', 'register', 'login']
        ] + [reverse('forum', args=['route-notes']), thread.get_absolute_url()]
        for url in urls:
            with self.subTest(url=url):
                response = (member if url == reverse('new_thread') else self.client).get(url)
                self.assertContains(response, stylesheet, html=True)
                self.assertContains(response, script, html=True)
                self.assertNotContains(response, '<style>')
                self.assertNotContains(response, '<script>')
                self.assertTemplateUsed(response, 'base.html')
                self.assertTemplateUsed(response, 'partials/footer.html')

    def test_theme_script_runs_before_any_stylesheet(self) -> None:
        # Loaded after the CSS, a dark-mode visitor would see the light theme flash first.
        theme = f'<script src="{static("js/theme.js")}"></script>'
        for url in [reverse('index'), reverse('login')]:
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                self.assertIn(theme, html)
                self.assertLess(html.index(theme), html.index('rel="stylesheet"'))
                self.assertLess(html.index(theme), html.index('</head>'))


class InjectionTests(TestCase):
    """The browser's live checks can be switched off, so the server must hold on its own."""

    ATTACKS: list[str] = [
        '<script>alert(1)</script>',
        '<img src=x onerror=alert(1)>',
        "x'); DROP TABLE forum_user;--",
        "' OR '1'='1",
        'admin"--',
    ]

    def register(self, username: str, display_name: str = '') -> HttpResponse:
        return self.client.post(reverse('register'), {
            'username': username, 'display_name': display_name, 'email': 'probe@example.com',
            'password1': 'konkan-coast-26', 'password2': 'konkan-coast-26', 'terms': 'on',
        })

    def test_injection_usernames_are_refused_by_the_server(self) -> None:
        for attack in self.ATTACKS:
            with self.subTest(username=attack):
                response = self.register(attack)
                self.assertContains(response, 'Enter a valid username.')
                self.assertFalse(User.objects.filter(username=attack).exists())
        self.assertEqual(User.objects.count(), 0)  # the table is still there, and still queryable

    def test_sql_injection_in_login_does_not_log_in(self) -> None:
        User.objects.create_user('tenzin', 'tenzin@example.com', 'high-passes-26')
        for attack in ["' OR '1'='1", "tenzin' --", "tenzin'; --"]:
            with self.subTest(username=attack):
                response = self.client.post(reverse('login'), {'username': attack, 'password': "' OR '1'='1"})
                self.assertContains(response, 'Please enter a correct username and password.')
                self.assertNotIn('_auth_user_id', self.client.session)

    def test_script_in_display_name_is_shown_as_text(self) -> None:
        # Display names may contain any character, so escaping on output is what protects pages.
        self.assertRedirects(self.register('safe-name', '<script>alert("hi")</script>'), reverse('index'))
        html = self.client.get(reverse('index')).content.decode()
        self.assertIn('&lt;script&gt;alert(&quot;hi&quot;)&lt;/script&gt;', html)
        self.assertNotIn('<script>alert("hi")</script>', html)


class LiveValidationTests(TestCase):
    def test_register_page_loads_the_live_checks(self) -> None:
        response = self.client.get(reverse('register'))
        self.assertContains(response, 'data-live-validate')
        self.assertContains(response, f'<script src="{static("js/register.js")}"></script>', html=True)

    def test_other_pages_do_not_load_them(self) -> None:
        self.assertNotContains(self.client.get(reverse('login')), 'js/register.js')

    def test_browser_username_rule_matches_the_servers(self) -> None:
        # register.js allows /[\p{L}\p{N}_.@+-]/u per character; Django allows [\w.@+-]. Same set.
        script = Path(finders.find('js/register.js')).read_text(encoding='utf-8')
        self.assertIn(r'var USERNAME_CHAR = /^[\p{L}\p{N}_.@+-]$/u;', script)
        django_rule = re.compile(r'^[\w.@+-]+\Z')
        for code in range(0x110000):
            if 0xD800 <= code <= 0xDFFF:
                continue
            char = chr(code)
            browser_allows = unicodedata.category(char)[0] in 'LN' or char in '_.@+-'
            if browser_allows != bool(django_rule.match(char)):
                self.fail(f'U+{code:04X} {char!r}: browser={browser_allows}, django={not browser_allows}')


class RenderingTests(SimpleTestCase):
    def test_markdown_formatting(self) -> None:
        html = render_body('**bold**, _italic_, ~~gone~~ and `code`\n\n- one\n- two\n\n> quoted')
        for fragment in ['<strong>bold</strong>', '<em>italic</em>', '<s>gone</s>', '<code>code</code>',
                         '<li>one</li>', '<blockquote>']:
            self.assertIn(fragment, html)

    def test_single_line_breaks_are_kept(self) -> None:
        self.assertIn('<br>', render_body('Line one\nline two'))

    def test_headings_start_below_the_page_headings(self) -> None:
        self.assertEqual(render_body('# Spiti').strip(), '<h3>Spiti</h3>')

    def test_html_is_rendered_but_cleaned(self) -> None:
        # Posts may contain HTML; HtmlInPostsTests covers what survives in detail.
        html = render_body('<script>alert(1)</script> <b onclick="x()">hi</b>')
        self.assertNotIn('<script', html)
        self.assertNotIn('alert(1)', html)
        self.assertNotIn('onclick', html)
        self.assertIn('<b>hi</b>', html)

    def test_only_safe_links_become_links(self) -> None:
        html = render_body('[site](https://example.com) [bad](javascript:alert(1)) [data](data:text/html,x)')
        self.assertIn('<a href="https://example.com" rel="nofollow ugc noopener noreferrer">site</a>', html)
        self.assertNotIn('javascript:alert(1)"', html)
        self.assertNotIn('href="data:', html)

    def test_images_are_not_embedded(self) -> None:
        self.assertNotIn('<img', render_body('![map](https://example.com/x.png)'))


class PostingTests(TestCase):
    member: User
    himalaya: Forum

    @classmethod
    def setUpTestData(cls) -> None:
        import_forums()
        cls.member = User.objects.create_user('meera', display_name='Meera Iyer')
        cls.himalaya = Forum.objects.get(slug='himalaya-and-ladakh')

    def counts(self, slug: str) -> tuple[int, int]:
        forum = Forum.objects.get(slug=slug)
        return forum.thread_count, forum.post_count

    def test_starting_a_thread_counts_it_here_and_in_every_parent(self) -> None:
        before = {slug: self.counts(slug) for slug in ['himalaya-and-ladakh', 'route-notes', 'off-topic']}
        thread = start_thread(self.himalaya, self.member, 'Spiti in June', '**Four** riding days.')

        self.assertEqual(thread.posts.get().body_html, '<p><strong>Four</strong> riding days.</p>\n')
        for slug in ['himalaya-and-ladakh', 'route-notes']:
            threads, posts = before[slug]
            self.assertEqual(self.counts(slug), (threads + 1, posts + 1))
            self.assertEqual(Forum.objects.get(slug=slug).last_post, thread.last_post)
        self.assertEqual(self.counts('off-topic'), before['off-topic'])
        self.member.refresh_from_db()
        self.assertEqual(self.member.post_count, 1)
        self.assertEqual(thread.reply_count, 0)

    def test_a_reply_counts_as_a_post_but_not_a_thread(self) -> None:
        thread = start_thread(self.himalaya, self.member, 'Spiti in June', 'Four riding days.')
        threads, posts = self.counts('route-notes')
        reply = add_reply(thread, self.member, 'Batal by noon.')

        thread.refresh_from_db()
        self.assertEqual((thread.reply_count, thread.last_post), (1, reply))
        self.assertEqual(self.counts('route-notes'), (threads, posts + 1))
        self.assertEqual(Forum.objects.get(slug='route-notes').last_post, reply)

    def test_an_older_reply_never_replaces_a_newer_latest_post(self) -> None:
        # Simulates two replies milliseconds apart whose transactions finish in the wrong order.
        thread = start_thread(self.himalaya, self.member, 'Spiti in June', 'Four riding days.')
        newer = thread.last_post
        assert newer is not None
        Thread.objects.filter(pk=thread.pk).update(last_posted_at=timezone.now() + timedelta(minutes=1))
        Post.objects.filter(pk=newer.pk).update(created_at=timezone.now() + timedelta(minutes=1))

        add_reply(thread, self.member, 'Late arrival.')
        thread.refresh_from_db()
        self.assertEqual(thread.last_post, newer)
        self.assertEqual(thread.reply_count, 1)  # still counted
        self.assertEqual(Forum.objects.get(slug='himalaya-and-ladakh').last_post, newer)


class NewThreadViewTests(TestCase):
    member: User

    @classmethod
    def setUpTestData(cls) -> None:
        import_forums()
        cls.member = User.objects.create_user('meera', display_name='Meera Iyer')

    def test_visitors_are_asked_to_log_in(self) -> None:
        url = reverse('new_thread')
        self.assertRedirects(self.client.get(url), f'{reverse("login")}?next={url}')

    def test_forum_page_link_preselects_the_forum(self) -> None:
        self.client.force_login(self.member)
        response = self.client.get(f'{reverse("new_thread")}?forum=himalaya-and-ladakh')
        himalaya = Forum.objects.get(slug='himalaya-and-ladakh')
        self.assertContains(
            response, f'<option value="{himalaya.pk}" selected>Before you go: Route notes › Himalaya and Ladakh</option>',
            html=True,
        )

    def test_posting_a_thread(self) -> None:
        self.client.force_login(self.member)
        himalaya = Forum.objects.get(slug='himalaya-and-ladakh')
        response = self.client.post(reverse('new_thread'), {
            'forum': himalaya.pk, 'title': 'Spiti in June', 'body': 'Four riding days, _one_ spare.',
        })
        thread = Thread.objects.get(title='Spiti in June')
        self.assertRedirects(response, thread.get_absolute_url())
        self.assertEqual((thread.forum, thread.author), (himalaya, self.member))
        self.assertContains(self.client.get(thread.get_absolute_url()), 'Four riding days, <em>one</em> spare.')

    def test_submitting_skips_the_forum_menu_and_initial_lookup(self) -> None:
        self.client.force_login(self.member)
        himalaya = Forum.objects.get(slug='himalaya-and-ladakh')
        with CaptureQueriesContext(connection) as queries:
            self.client.post(reverse('new_thread'), {'forum': himalaya.pk, 'title': 'Spiti', 'body': 'Four days.'})
        sql = [q['sql'] for q in queries.captured_queries]
        self.assertFalse(any('forum_category' in s for s in sql), 'the menu was built for a valid submit')
        self.assertFalse(any('"slug" = \'\'' in s for s in sql), 'looked up an empty ?forum= slug')

    def test_showing_the_form_builds_the_menu_once(self) -> None:
        self.client.force_login(self.member)
        for url in [reverse('new_thread'), f'{reverse("new_thread")}?forum=himalaya-and-ladakh']:
            with self.subTest(url=url), CaptureQueriesContext(connection) as queries:
                response = self.client.get(url)
            self.assertEqual(sum('forum_category' in q['sql'] for q in queries.captured_queries), 1)
            self.assertContains(response, '<option value="" selected>Choose a section</option>' if '?' not in url
                                else '>Before you go: Route notes › Himalaya and Ladakh</option>')

    def test_missing_fields_are_reported(self) -> None:
        self.client.force_login(self.member)
        response = self.client.post(reverse('new_thread'), {'forum': '', 'title': '', 'body': ''})
        self.assertContains(response, 'This field is required.', count=3)
        self.assertFalse(Thread.objects.filter(author=self.member).exists())


class ThreadViewTests(TestCase):
    member: User
    thread: Thread

    @classmethod
    def setUpTestData(cls) -> None:
        import_forums()
        cls.member = User.objects.create_user('meera', display_name='Meera Iyer')
        cls.thread = start_thread(Forum.objects.get(slug='himalaya-and-ladakh'), cls.member, 'Spiti in June', 'Four riding days.')

    def reply(self, body: str = 'Batal by *noon*.') -> HttpResponse:
        return self.client.post(self.thread.get_absolute_url(), {'body': body})

    def test_reply_shows_up_as_the_forums_latest_post_on_the_index(self) -> None:
        # Step 8's "done when": the reply raises the reply count and leads the index.
        self.client.force_login(self.member)
        response = self.reply()
        reply = Post.objects.latest('created_at')
        self.assertRedirects(response, self.thread.latest_post_url(reply), fetch_redirect_response=False)

        self.thread.refresh_from_db()
        self.assertEqual(self.thread.reply_count, 1)
        index = self.client.get(reverse('index'))
        latest = f'{self.thread.get_absolute_url()}?page=last#post-{reply.pk}'
        self.assertContains(index, f'<a class="last__title" href="{latest}">Spiti in June</a>', html=True)

    def test_thread_page_shows_the_rendered_posts(self) -> None:
        self.client.force_login(self.member)
        self.reply()
        response = self.client.get(self.thread.get_absolute_url())
        self.assertContains(response, '<h1>Spiti in June</h1>', html=True)
        self.assertContains(response, 'Batal by <em>noon</em>.')
        self.assertContains(response, '<a class="post__no" href="#post-', count=2)

    def test_visitors_see_a_login_link_and_cannot_post(self) -> None:
        self.assertContains(self.client.get(self.thread.get_absolute_url()), 'to reply.')
        self.assertRedirects(self.reply(), f'{reverse("login")}?next={self.thread.get_absolute_url()}',
                             fetch_redirect_response=False)
        self.assertEqual(self.thread.posts.count(), 1)

    def test_empty_reply_is_refused_and_text_kept(self) -> None:
        self.client.force_login(self.member)
        response = self.reply('   ')
        self.assertContains(response, 'This field is required.')
        self.assertEqual(self.thread.posts.count(), 1)

    def test_closed_thread_takes_no_replies(self) -> None:
        Thread.objects.filter(pk=self.thread.pk).update(is_closed=True)
        self.client.force_login(self.member)
        self.assertContains(self.client.get(self.thread.get_absolute_url()), "This thread is closed")
        self.assertEqual(self.reply().status_code, 403)
        self.assertEqual(self.thread.posts.count(), 1)

    def test_posts_are_paginated_and_last_page_is_reachable(self) -> None:
        for n in range(POSTS_PER_PAGE):  # 1 opening post + 20 replies = 2 pages
            add_reply(self.thread, self.member, f'Reply {n}')
        last = self.client.get(f'{self.thread.get_absolute_url()}?page=last')
        self.assertContains(last, 'Reply 19')
        self.assertNotContains(last, 'Four riding days.')
        self.assertContains(last, f'<a class="post__no" href="#post-{Post.objects.latest("created_at").pk}">#21</a>', html=True)
        self.assertContains(self.client.get(self.thread.get_absolute_url()), '<a href="?page=2">2</a>', html=True)

    def test_views_are_counted(self) -> None:
        self.client.get(self.thread.get_absolute_url())
        self.client.get(self.thread.get_absolute_url())
        self.thread.refresh_from_db()
        self.assertEqual(self.thread.view_count, 2)

    def test_unknown_thread_is_404(self) -> None:
        self.assertEqual(self.client.get(reverse('thread_by_id', args=[999_999])).status_code, 404)


class ForumThreadListTests(TestCase):
    def test_forum_lists_its_real_threads_pinned_first(self) -> None:
        import_forums()
        member = User.objects.create_user('meera')
        himalaya = Forum.objects.get(slug='himalaya-and-ladakh')
        start_thread(himalaya, member, 'Newest chatter', 'Hi.')
        rules = start_thread(himalaya, member, 'Read before posting', 'Rules.')
        Thread.objects.filter(pk=rules.pk).update(is_pinned=True, last_posted_at=timezone.now() - timedelta(days=30))

        html = self.client.get(reverse('forum', args=['himalaya-and-ladakh'])).content.decode()
        self.assertLess(html.index('Read before posting'), html.index('Newest chatter'))
        self.assertIn('<span class="chip chip--pinned">Pinned</span>', html)
        self.assertIn(f'href="{reverse("new_thread")}?forum=himalaya-and-ladakh"', html)

    def test_empty_forum_invites_the_first_thread(self) -> None:
        import_forums()
        Forum.objects.create(parent=Forum.objects.get(slug='on-foot'), slug='sikkim', title='Sikkim', description='.')
        self.assertContains(self.client.get(reverse('forum', args=['sikkim'])), 'No threads here yet.')

    def test_compact_counts(self) -> None:
        self.assertEqual([compact_count(n) for n in [940, 1_400, 94_000, 1_900_000]], ['940', '1.4K', '94K', '1.9M'])


class PostAdminEditTests(TestCase):
    def test_editing_a_post_in_admin_re_renders_it(self) -> None:
        import_forums()
        staff = User.objects.create_superuser('boss', 'boss@example.com', 'x')
        post = Post.objects.first()
        assert post is not None
        self.client.force_login(staff)
        self.client.post(reverse('admin:forum_post_change', args=[post.pk]), {'body_source': 'Now **bold**.'})
        post.refresh_from_db()
        self.assertEqual(post.body_html, '<p>Now <strong>bold</strong>.</p>\n')
        self.assertIsNotNone(post.edited_at)


class ThreadSlugTests(TestCase):
    member: User
    thread: Thread

    @classmethod
    def setUpTestData(cls) -> None:
        import_forums()
        cls.member = User.objects.create_user('meera')
        cls.thread = start_thread(Forum.objects.get(slug='himalaya-and-ladakh'), cls.member, 'Spiti in June', 'Four days.')

    def test_titles_in_any_script_become_ascii_slugs(self) -> None:
        cases: dict[str, str] = {
            'Manali to Kaza, first week of June': 'manali-to-kaza-first-week-of-june',
            'मनाली से काज़ा, जून में': 'mnali-se-kaja-jun-mem',
            'দার্জিলিং থেকে সান্দাকফু': 'darjilim-theke-sandakphu',
            'Manali से Kaza 2026': 'manali-se-kaza-2026',
            '🏍️ Spiti on a 350': 'spiti-on-a-350',
            '🏍️🏔️!!': 'thread',
        }
        for title, slug in cases.items():
            with self.subTest(title=title):
                self.assertEqual(transliterated_slug(title), slug)

    def test_long_titles_are_cut_at_a_word(self) -> None:
        slug = transliterated_slug('Sach Pass in a stock hatchback: talk me out of it, please, before the monsoon')
        self.assertEqual(slug, 'sach-pass-in-a-stock-hatchback-talk-me-out-of-it-please')
        self.assertLessEqual(len(transliterated_slug('x' * 200)), SLUG_MAX_LENGTH)

    def test_thread_url_carries_the_slug(self) -> None:
        self.assertEqual(self.thread.get_absolute_url(), f'/thread/{self.thread.pk}/spiti-in-june/')
        self.assertEqual(self.client.get(self.thread.get_absolute_url()).status_code, 200)

    def test_missing_or_wrong_slug_redirects_permanently(self) -> None:
        canonical = self.thread.get_absolute_url()
        for url in [f'/thread/{self.thread.pk}/', f'/thread/{self.thread.pk}/old-title/']:
            with self.subTest(url=url):
                self.assertRedirects(self.client.get(url), canonical, status_code=301)
        self.assertRedirects(self.client.get(f'/thread/{self.thread.pk}/x/?page=last'), f'{canonical}?page=last',
                             status_code=301, fetch_redirect_response=False)

    def test_redirects_do_not_count_as_views(self) -> None:
        self.client.get(f'/thread/{self.thread.pk}/')
        self.thread.refresh_from_db()
        self.assertEqual(self.thread.view_count, 0)

    def test_renamed_thread_keeps_old_links_working(self) -> None:
        old_url = self.thread.get_absolute_url()
        Thread.objects.filter(pk=self.thread.pk).update(title='Spiti in July')
        self.assertRedirects(self.client.get(old_url), f'/thread/{self.thread.pk}/spiti-in-july/', status_code=301)

    def test_reply_to_an_outdated_url_is_still_saved(self) -> None:
        # A POST is never redirected: that would lose the reply's text.
        self.client.force_login(self.member)
        response = self.client.post(f'/thread/{self.thread.pk}/old-title/', {'body': 'Batal by noon.'})
        self.assertEqual(self.thread.posts.count(), 2)
        self.assertTrue(response['Location'].startswith(self.thread.get_absolute_url()))


def photo_bytes(
    image_format: str = 'JPEG', size: tuple[int, int] = (3000, 2000), *, gps: bool = True, rotated: bool = False,
) -> bytes:
    """A stand-in phone photo: optionally GPS-tagged (near Kaza), with XMP and a comment, and a rotation flag."""
    exif = Image.Exif()
    if gps:
        exif.get_ifd(0x8825).update({1: 'N', 2: (32.0, 13.0, 30.0), 3: 'E', 4: (78.0, 4.0, 12.0)})
    if rotated:
        exif[0x0112] = 6  # "rotate 90° clockwise to display"
    buffer = BytesIO()
    extra: dict[str, object] = {}
    if image_format == 'JPEG':
        extra = {'xmp': b'<x:xmpmeta xmlns:x="adobe:ns:meta/">GPSLatitude 32,13.5N</x:xmpmeta>', 'comment': b'home'}
    Image.new('RGB', size, (180, 120, 60)).save(buffer, image_format, exif=exif, **extra)
    return buffer.getvalue()


def camera_photo_bytes(size: tuple[int, int] = (3000, 2000)) -> bytes:
    """What many cameras write to a .JPG: the photo plus an embedded preview (Pillow calls this MPO).

    Real camera files carry EXIF as well, but Pillow's MPO writer and reader disagree when
    it writes both, so this fixture leaves EXIF out. Metadata removal is covered by the
    plain-JPEG tests; it runs on the same code either way.
    """
    main = Image.new('RGB', size, (180, 120, 60))
    buffer = BytesIO()
    main.save(buffer, 'MPO', save_all=True, append_images=[main.resize((size[0] // 2, size[1] // 2))])
    return buffer.getvalue()

def upload(name: str, content: bytes, content_type: str = 'image/jpeg') -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type)


def has_location(data: bytes) -> bool:
    with Image.open(BytesIO(data)) as image:
        gps_in_exif = bool(image.getexif().get_ifd(0x8825))
    return gps_in_exif or b'GPSLatitude' in data or b'Exif\x00\x00' in data


class PhotoPreparationTests(SimpleTestCase):
    def test_location_and_camera_details_are_removed(self) -> None:
        original = photo_bytes()
        self.assertTrue(has_location(original))
        photo = prepare_photo(upload('IMG_home.jpg', original))
        for stored in [photo.original, *photo.thumbnails.values()]:
            with self.subTest(name=stored.name):
                data = stored.read()
                self.assertFalse(has_location(data))
                self.assertNotIn(b'home', data)

    def test_photo_is_straightened_and_renamed(self) -> None:
        photo = prepare_photo(upload('IMG_home_street.jpg', photo_bytes(rotated=True)))
        self.assertEqual((photo.width, photo.height), (2000, 3000))
        self.assertRegex(photo.original.name, r'^[0-9a-f]{32}\.jpg$')

    def test_smaller_copies_only_when_the_photo_is_wider(self) -> None:
        big = prepare_photo(upload('big.jpg', photo_bytes(size=(3000, 2000))))
        self.assertEqual(sorted(big.thumbnails), [800, 1600])
        with Image.open(big.thumbnails[800]) as thumb:
            self.assertEqual(thumb.size, (800, 533))
        mid = prepare_photo(upload('mid.jpg', photo_bytes(size=(1200, 900))))
        self.assertEqual(sorted(mid.thumbnails), [800])
        small = prepare_photo(upload('small.png', photo_bytes('PNG', (500, 400)), 'image/png'))
        self.assertEqual(small.thumbnails, {})

    def test_png_and_webp_keep_their_format(self) -> None:
        for image_format, extension in [('PNG', 'png'), ('WEBP', 'webp')]:
            with self.subTest(image_format=image_format):
                photo = prepare_photo(upload(f'map.{extension}', photo_bytes(image_format, (900, 600))))
                self.assertTrue(photo.original.name.endswith(f'.{extension}'))
                self.assertFalse(has_location(photo.original.read()))

    def test_only_real_photos_are_accepted(self) -> None:
        gif = BytesIO()
        Image.new('RGB', (10, 10)).save(gif, 'GIF')
        cases: dict[str, bytes] = {
            'notes.jpg': b'just some text pretending to be a photo',
            'anim.gif': gif.getvalue(),
            'vector.svg': b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
            'broken.jpg': photo_bytes()[:2000],
        }
        for name, content in cases.items():
            with self.subTest(name=name), self.assertRaises(ValidationError) as caught:
                prepare_photo(upload(name, content))
            self.assertIn(name, caught.exception.messages[0])

    def test_upload_limits(self) -> None:
        self.assertEqual((MAX_UPLOAD_BYTES, MAX_PHOTOS), (10 * 1024 * 1024, 10))
        just_fits = prepare_photo(upload('fits.jpg', photo_bytes(size=(600, 400)) + b'\0' * (MAX_UPLOAD_BYTES - 50_000)))
        self.assertEqual(just_fits.width, 600)

    def test_camera_jpg_files_are_accepted(self) -> None:
        # Cameras write .JPG files that hold a preview image too, which Pillow calls MPO.
        raw = camera_photo_bytes()
        self.assertEqual(Image.open(BytesIO(raw)).format, 'MPO')
        photo = prepare_photo(upload('DSCF1234.JPG', raw))
        self.assertEqual((photo.width, photo.height), (3000, 2000))
        self.assertTrue(photo.original.name.endswith('.jpg'))
        stored = photo.original.read()
        with Image.open(BytesIO(stored)) as saved:
            self.assertEqual(saved.format, 'JPEG')          # stored as a plain JPEG
            self.assertEqual(getattr(saved, 'n_frames', 1), 1)  # the extra frame is left behind
        self.assertFalse(has_location(stored))

    def test_oversized_files_and_images_are_refused(self) -> None:
        with self.assertRaisesMessage(ValidationError, f'larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB'):
            prepare_photo(upload('huge.jpg', b'x' * (MAX_UPLOAD_BYTES + 1)))
        bomb = BytesIO()
        Image.new('1', (8000, 8000)).save(bomb, 'PNG')  # 64 megapixels, but only a few KB on disk
        with self.assertRaisesMessage(ValidationError, 'too large (8000 × 8000 pixels)'):
            prepare_photo(upload('bomb.png', bomb.getvalue(), 'image/png'))


class PhotoUploadTests(TestCase):
    media_root: str
    member: User
    himalaya: Forum

    @classmethod
    def setUpClass(cls) -> None:
        cls.media_root = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.media_root, ignore_errors=True)
        cls.enterClassContext(override_settings(MEDIA_ROOT=cls.media_root))  # never write into the project's media/
        super().setUpClass()

    @classmethod
    def setUpTestData(cls) -> None:
        import_forums()
        cls.member = User.objects.create_user('meera', display_name='Meera Iyer')
        cls.himalaya = Forum.objects.get(slug='himalaya-and-ladakh')

    def setUp(self) -> None:
        self.client.force_login(self.member)

    def post_thread(self, *photos: SimpleUploadedFile) -> HttpResponse:
        return self.client.post(reverse('new_thread'), {
            'forum': self.himalaya.pk, 'title': 'Spiti in June', 'body': 'Photos from Kaza.', 'photos': list(photos),
        })

    def test_phone_photo_appears_in_the_post_without_gps(self) -> None:
        # Step 9's "done when": a phone photo shows in a post, and the stored file has no GPS tags.
        response = self.post_thread(upload('IMG_2041.jpg', photo_bytes(rotated=True)))
        thread = Thread.objects.get(title='Spiti in June')
        self.assertRedirects(response, thread.get_absolute_url())

        attachment = Attachment.objects.get()
        self.assertEqual((attachment.post, attachment.uploader), (thread.last_post, self.member))
        self.assertEqual((attachment.width, attachment.height), (2000, 3000))
        for stored in [attachment.file, attachment.thumbnail_800, attachment.thumbnail_1600]:
            with self.subTest(name=stored.name):
                self.assertTrue(Path(stored.path).is_file())
                self.assertFalse(has_location(Path(stored.path).read_bytes()))

        page = self.client.get(thread.get_absolute_url()).content.decode()
        self.assertIn(f'src="{attachment.thumbnail_1600.url}"', page)
        self.assertIn(f'{attachment.thumbnail_800.url} 800w', page)
        self.assertIn(f'<figure class="photo"><a href="{attachment.file.url}"', page)
        # Sent through the plain file input, so placed after the text.
        self.assertLess(page.index('Photos from Kaza.'), page.index('<figure class="photo">'))

    def test_reply_with_several_photos(self) -> None:
        thread = start_thread(self.himalaya, self.member, 'Spiti in June', 'Four days.')
        self.client.post(thread.get_absolute_url(), {
            'body': 'Two from Chandratal.',
            'photos': [upload('a.jpg', photo_bytes(size=(1000, 750))), upload('b.png', photo_bytes('PNG', (600, 400)), 'image/png')],
        })
        reply = Post.objects.latest('created_at')
        self.assertEqual([a.file.name.rsplit('.', 1)[1] for a in reply.attachments.all()], ['jpg', 'png'])
        self.assertEqual(reply.body_source.count('](attachment:'), 2)
        self.assertContains(self.client.get(thread.get_absolute_url()), '<figure class="photo">', count=2)

    def test_one_bad_file_saves_nothing(self) -> None:
        response = self.post_thread(upload('good.jpg', photo_bytes()), upload('notes.jpg', b'not a photo'))
        self.assertContains(response, 'notes.jpg couldn’t be read as a photo.')
        self.assertFalse(Thread.objects.filter(title='Spiti in June').exists())
        self.assertFalse(Attachment.objects.exists())

    def test_too_many_photos_are_refused(self) -> None:
        small = photo_bytes(size=(200, 150), gps=False)
        response = self.post_thread(*(upload(f'{n}.jpg', small) for n in range(MAX_PHOTOS + 1)))
        self.assertContains(response, f'Attach up to {MAX_PHOTOS} photos to one post ({MAX_PHOTOS + 1} chosen).')
        self.assertFalse(Attachment.objects.exists())

    def test_forms_send_files(self) -> None:
        thread = start_thread(self.himalaya, self.member, 'Spiti in June', 'Four days.')
        for url in [reverse('new_thread'), thread.get_absolute_url()]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertContains(response, 'enctype="multipart/form-data"')
                self.assertContains(response, 'accept="image/jpeg,image/png,image/webp,.jpg,.JPG')

    def test_showing_photos_needs_no_queries_per_post(self) -> None:
        thread = start_thread(self.himalaya, self.member, 'Spiti in June', 'Four days.',
                              [prepare_photo(upload('a.jpg', photo_bytes(size=(900, 600))))])
        for n in range(3):
            add_reply(thread, self.member, f'Reply {n}', [prepare_photo(upload(f'{n}.jpg', photo_bytes(size=(900, 600))))])
        with CaptureQueriesContext(connection) as queries:
            self.client.get(thread.get_absolute_url())
        # Posts carry their photos in body_html. The one query is the member's photo tray,
        # the same however many posts on the page have photos.
        attachment_queries = [q['sql'] for q in queries.captured_queries if 'forum_attachment' in q['sql']]
        self.assertEqual(len(attachment_queries), 1)
        self.assertIn('"post_id" IS NULL', attachment_queries[0])

    def test_admin_lists_photos(self) -> None:
        start_thread(self.himalaya, self.member, 'Spiti in June', 'Four days.',
                     [prepare_photo(upload('a.jpg', photo_bytes(size=(900, 600))))])
        self.client.force_login(User.objects.create_superuser('boss', 'boss@example.com', 'x'))
        self.assertEqual(self.client.get(reverse('admin:forum_attachment_changelist')).status_code, 200)
        self.assertEqual(self.client.get(reverse('admin:forum_attachment_add')).status_code, 403)


def fake_photo(url: str = '/media/attachments/p') -> SimpleNamespace:
    """Stands in for an Attachment when testing the renderer alone."""
    return SimpleNamespace(display_url=f'{url}-1600.jpg', file=SimpleNamespace(url=f'{url}.jpg'),
                           srcset=f'{url}-800.jpg 800w, {url}-1600.jpg 1600w, {url}.jpg 3000w', width=3000, height=2000)


class PhotoRenderingTests(SimpleTestCase):
    def test_photo_on_its_own_line_becomes_a_full_width_figure(self) -> None:
        html = render_body('Day one.\n\n![Rohtang at dawn](attachment:7)\n\nDay two.', {7: fake_photo()})
        self.assertInHTML(
            '<figure class="photo"><a href="/media/attachments/p.jpg" rel="nofollow ugc noopener noreferrer">'
            '<img src="/media/attachments/p-1600.jpg" srcset="/media/attachments/p-800.jpg 800w, '
            '/media/attachments/p-1600.jpg 1600w, /media/attachments/p.jpg 3000w" sizes="(max-width: 48rem) 100vw, 60rem" '
            'width="3000" height="2000" alt="Rohtang at dawn" loading="lazy" decoding="async" data-photo="7"></a></figure>', html)
        self.assertLess(html.index('Day one.'), html.index('<figure'))
        self.assertLess(html.index('</figure>'), html.index('Day two.'))
        self.assertNotIn('<p><figure', html)

    def test_photo_mid_sentence_stays_in_its_paragraph(self) -> None:
        html = render_body('Look at ![this](attachment:7) view.', {7: fake_photo()})
        self.assertTrue(html.startswith('<p>Look at <a class="photo"'))

    def test_only_this_posts_photos_are_shown(self) -> None:
        self.assertEqual(render_body('![someone else’s](attachment:8)', {7: fake_photo()}).strip(), '')

    def test_outside_pictures_are_linked_not_loaded(self) -> None:
        html = render_body('![map](https://example.com/x.png)')
        self.assertNotIn('<img', html)
        self.assertIn('<p><a href="https://example.com/x.png" rel="nofollow ugc noopener noreferrer">map</a></p>', html)

    def test_images_load_only_from_this_sites_storage(self) -> None:
        html = render_body('![x](attachment:1)', {1: fake_photo('https://evil.example/x')})
        self.assertNotIn('evil.example/x-1600', html.split('<img')[1])  # src and srcset removed from the <img>

    def test_photo_ids_are_found_in_order_once(self) -> None:
        self.assertEqual(photo_ids('![a](attachment:3)\n\n![b](attachment:1)\n\n![again](attachment:3) ![x](https://e.com/y)'), [3, 1])


class InlinePhotoTests(TestCase):
    media_root: str
    member: User
    other: User
    thread: Thread

    @classmethod
    def setUpClass(cls) -> None:
        cls.media_root = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.media_root, ignore_errors=True)
        cls.enterClassContext(override_settings(MEDIA_ROOT=cls.media_root))
        super().setUpClass()

    @classmethod
    def setUpTestData(cls) -> None:
        import_forums()
        cls.member = User.objects.create_user('meera', display_name='Meera Iyer')
        cls.other = User.objects.create_user('tenzin')
        cls.thread = start_thread(Forum.objects.get(slug='himalaya-and-ladakh'), cls.member, 'Spiti in June', 'Four days.')

    def setUp(self) -> None:
        self.client.force_login(self.member)

    def upload_photo(self, size: tuple[int, int] = (3000, 2000)) -> dict[str, Any]:
        response = self.client.post(reverse('photo_upload'), {'photo': upload('IMG.jpg', photo_bytes(size=size))})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def test_upload_returns_the_line_that_places_the_photo(self) -> None:
        body = self.upload_photo()
        attachment = Attachment.objects.get(pk=body['id'])
        self.assertEqual(body['markdown'], f'![photo](attachment:{attachment.pk})')
        self.assertEqual((attachment.uploader, attachment.post), (self.member, None))  # waiting for its post
        self.assertFalse(has_location(Path(attachment.file.path).read_bytes()))

    def test_upload_refusals(self) -> None:
        self.assertEqual(self.client.get(reverse('photo_upload')).status_code, 405)
        response = self.client.post(reverse('photo_upload'), {'photo': upload('notes.jpg', b'not a photo')})
        self.assertEqual((response.status_code, response.json()), (400, {'error': 'notes.jpg couldn’t be read as a photo.'}))
        self.assertEqual(self.client.post(reverse('photo_upload')).status_code, 400)
        self.client.logout()
        self.assertEqual(self.client.post(reverse('photo_upload'), {'photo': upload('a.jpg', photo_bytes())}).status_code, 403)
        self.assertFalse(Attachment.objects.exists())

    def test_waiting_photos_are_capped(self) -> None:
        photo = prepare_photo(upload('a.jpg', photo_bytes(size=(200, 150), gps=False)))
        Attachment.objects.bulk_create([
            Attachment(uploader=self.member, file=photo.original.name, width=200, height=150, size=1)
            for _ in range(MAX_WAITING_PHOTOS)
        ])
        response = self.client.post(reverse('photo_upload'), {'photo': upload('b.jpg', photo_bytes(size=(200, 150)))})
        self.assertEqual(response.status_code, 400)
        self.assertIn('too many photos waiting', response.json()['error'])

    def test_photos_appear_where_the_text_places_them(self) -> None:
        first, second = self.upload_photo(), self.upload_photo(size=(1200, 800))
        body = f'Day one.\n\n![Rohtang](attachment:{first["id"]})\n\nDay two.\n\n![Kaza](attachment:{second["id"]})'
        self.client.post(self.thread.get_absolute_url(), {'body': body})

        reply = Post.objects.latest('created_at')
        self.assertEqual(set(reply.attachments.values_list('pk', flat=True)), {first['id'], second['id']})
        html = reply.body_html
        positions = [html.index(s) for s in ['Day one.', 'alt="Rohtang"', 'Day two.', 'alt="Kaza"']]
        self.assertEqual(positions, sorted(positions))
        rohtang = Attachment.objects.get(pk=first['id'])
        self.assertIn(f'src="{rohtang.thumbnail_1600.url}"', html)  # shown large, not as a thumbnail
        self.assertIn(f'<a href="{rohtang.file.url}"', html)          # opens the full-resolution photo

    def test_other_members_photos_cannot_be_borrowed(self) -> None:
        theirs = Attachment.objects.create(
            uploader=self.other, file=prepare_photo(upload('t.jpg', photo_bytes(size=(300, 200)))).original)
        self.client.post(self.thread.get_absolute_url(), {'body': f'Mine now: ![x](attachment:{theirs.pk})'})
        theirs.refresh_from_db()
        self.assertIsNone(theirs.post)
        self.assertNotIn('<img', Post.objects.latest('created_at').body_html)

    def test_a_posted_photo_cannot_be_reused_elsewhere(self) -> None:
        placed = self.upload_photo()
        self.client.post(self.thread.get_absolute_url(), {'body': f'![a](attachment:{placed["id"]})'})
        self.client.post(self.thread.get_absolute_url(), {'body': f'Again: ![a](attachment:{placed["id"]})'})
        self.assertNotIn('<img', Post.objects.latest('created_at').body_html)

    def test_photo_limit_counts_placed_photos(self) -> None:
        body = '\n\n'.join(f'![p](attachment:{n})' for n in range(1, MAX_PHOTOS + 2))
        response = self.client.post(self.thread.get_absolute_url(), {'body': body})
        self.assertContains(response, f'A post can hold up to {MAX_PHOTOS} photos ({MAX_PHOTOS + 1} added).')

    def test_editor_pages_load_the_visual_editor_and_keep_a_fallback(self) -> None:
        for url in [self.thread.get_absolute_url(), reverse('new_thread')]:
            with self.subTest(url=url):
                page = self.client.get(url)
                self.assertContains(page, '<div class="editor" data-visual-editor>')  # editor.js mounts here
                self.assertContains(page, 'data-photo-tray')                          # shown by editor.js
                self.assertContains(page, f'data-upload-url="{reverse("photo_upload")}"')
                self.assertContains(page, '<div data-photo-fallback>')                # hidden by editor.js
                self.assertContains(page, f'<script src="{static("js/editor.js")}"></script>', html=True)
        self.assertNotContains(self.client.get(reverse('index')), 'js/editor.js')
        self.assertIsNotNone(finders.find('js/editor.js'))
        self.assertIsNone(finders.find('js/photos.js'))

    def test_admin_edit_keeps_the_photos(self) -> None:
        placed = self.upload_photo()
        self.client.post(self.thread.get_absolute_url(), {'body': f'![a](attachment:{placed["id"]})'})
        reply = Post.objects.latest('created_at')
        self.client.force_login(User.objects.create_superuser('boss', 'boss@example.com', 'x'))
        self.client.post(reverse('admin:forum_post_change', args=[reply.pk]),
                         {'body_source': f'Edited.\n\n![a](attachment:{placed["id"]})'})
        reply.refresh_from_db()
        self.assertIn('Edited.', reply.body_html)
        self.assertIn('<figure class="photo">', reply.body_html)


class RerenderPostsTests(TestCase):
    def test_rerender_rebuilds_html_from_markdown(self) -> None:
        import_forums()
        post = Post.objects.first()
        assert post is not None
        Post.objects.filter(pk=post.pk).update(body_source='Now **bold**.', body_html='stale')
        out = StringIO()
        call_command('rerender_posts', stdout=out)
        post.refresh_from_db()
        self.assertEqual(post.body_html, '<p>Now <strong>bold</strong>.</p>\n')
        self.assertIn('1 changed', out.getvalue())


class PhotoWidthRenderingTests(SimpleTestCase):
    def test_resized_photo_keeps_its_width(self) -> None:
        # The exact line the editor writes after a photo is dragged to 60%.
        html = render_body('Intro\n\n![Rohtang](attachment:41){width="60%"}\n\nOutro', {41: fake_photo()})
        self.assertIn('<figure class="photo" style="width:60%">', html)

    def test_widths_are_kept_in_range(self) -> None:
        for given, shown in [('5%', 'style="width:20%"'), ('100%', '<figure class="photo"><a'), ('abc', '<figure class="photo"><a')]:
            with self.subTest(width=given):
                self.assertIn(shown, render_body(f'![x](attachment:1){{width="{given}"}}', {1: fake_photo()}))

    def test_no_other_attributes_can_be_added(self) -> None:
        html = render_body('![x](attachment:1){width="50%" onerror="alert(1)"} [l](https://e.com){onclick="x()"}', {1: fake_photo()})
        self.assertNotRegex(html, r'<[^>]*\b(onerror|onclick)=')  # no tag gets them as attributes
        self.assertIn('</a>{onclick="x()"}', html)                  # after a link they stay visible text

    def test_photos_carry_their_id_for_the_editor(self) -> None:
        self.assertIn('data-photo="41"', render_body('![x](attachment:41)', {41: fake_photo()}))


class PhotoTrayTests(TestCase):
    media_root: str
    member: User
    thread: Thread

    @classmethod
    def setUpClass(cls) -> None:
        cls.media_root = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.media_root, ignore_errors=True)
        cls.enterClassContext(override_settings(MEDIA_ROOT=cls.media_root))
        super().setUpClass()

    @classmethod
    def setUpTestData(cls) -> None:
        import_forums()
        cls.member = User.objects.create_user('meera')
        cls.thread = start_thread(Forum.objects.get(slug='himalaya-and-ladakh'), cls.member, 'Spiti in June', 'Four days.')

    def setUp(self) -> None:
        self.client.force_login(self.member)

    def upload_photo(self) -> dict[str, Any]:
        return self.client.post(reverse('photo_upload'), {'photo': upload('IMG.jpg', photo_bytes(size=(2000, 1500)))}).json()

    def test_upload_reply_has_what_the_tray_needs(self) -> None:
        body = self.upload_photo()
        photo = Attachment.objects.get(pk=body['id'])
        self.assertEqual(body['preview_url'], photo.thumbnail_800.url)
        self.assertEqual(body['display_url'], photo.thumbnail_1600.url)
        self.assertEqual((body['width'], body['height']), (2000, 1500))
        self.assertEqual(body['remove_url'], reverse('photo_remove', args=[photo.pk]))

    def test_tray_lists_the_members_waiting_photos(self) -> None:
        first, second = self.upload_photo(), self.upload_photo()
        page = self.client.get(self.thread.get_absolute_url()).content.decode()
        self.assertLess(page.index(f'data-photo="{first["id"]}"'), page.index(f'data-photo="{second["id"]}"'))
        self.assertIn(f'src="{first["preview_url"]}"', page)
        self.assertIn('aria-label="Insert photo 1 at the cursor"', page)
        self.client.force_login(User.objects.create_user('tenzin'))  # someone else's tray is empty
        self.assertNotContains(self.client.get(self.thread.get_absolute_url()), f'data-photo="{first["id"]}"')

    def test_removing_a_waiting_photo_deletes_its_files(self) -> None:
        photo = Attachment.objects.get(pk=self.upload_photo()['id'])
        paths = [Path(stored.path) for stored in (photo.file, photo.thumbnail_800, photo.thumbnail_1600)]
        self.assertTrue(all(path.exists() for path in paths))
        with self.captureOnCommitCallbacks(execute=True):  # files are deleted once the change commits
            response = self.client.post(reverse('photo_remove', args=[photo.pk]))
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Attachment.objects.filter(pk=photo.pk).exists())
        self.assertFalse(any(path.exists() for path in paths))

    def test_only_your_own_waiting_photos_can_be_removed(self) -> None:
        mine = self.upload_photo()
        self.client.post(self.thread.get_absolute_url(), {'body': f'![a](attachment:{mine["id"]})'})
        self.assertEqual(self.client.post(reverse('photo_remove', args=[mine['id']])).status_code, 404)  # posted now
        theirs = Attachment.objects.create(
            uploader=User.objects.create_user('tenzin'), file=prepare_photo(upload('t.jpg', photo_bytes(size=(300, 200)))).original)
        self.assertEqual(self.client.post(reverse('photo_remove', args=[theirs.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('photo_remove', args=[theirs.pk])).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.post(reverse('photo_remove', args=[theirs.pk])).status_code, 403)
        self.assertEqual(Attachment.objects.count(), 2)

    def test_failed_submit_reopens_the_editor_with_text_and_photos(self) -> None:
        placed = self.upload_photo()
        body = f'Day one.\n\n![Rohtang](attachment:{placed["id"]}){{width="60%"}}\n\n' + 'x' * 20_001  # too long
        page = self.client.post(self.thread.get_absolute_url(), {'body': body}).content.decode()
        saved = page.split('<template data-editor-html>')[1].split('</template>')[0]
        self.assertIn('<p>Day one.</p>', saved)
        self.assertIn(f'data-photo="{placed["id"]}"', saved)
        self.assertIn('style="width:60%"', saved)
        self.assertIsNone(Attachment.objects.get(pk=placed['id']).post)  # still waiting: nothing was posted


class CameraPhotoUploadTests(TestCase):
    media_root: str
    member: User

    @classmethod
    def setUpClass(cls) -> None:
        cls.media_root = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.media_root, ignore_errors=True)
        cls.enterClassContext(override_settings(MEDIA_ROOT=cls.media_root))
        super().setUpClass()

    @classmethod
    def setUpTestData(cls) -> None:
        import_forums()
        cls.member = User.objects.create_user('meera')

    def test_camera_jpg_uploads_through_the_editor(self) -> None:
        self.client.force_login(self.member)
        # Browsers often report no type at all for camera files.
        response = self.client.post(reverse('photo_upload'), {
            'photo': upload('DSCF1234.JPG', camera_photo_bytes(), content_type=''),
        })
        self.assertEqual(response.status_code, 201, response.content)
        photo = Attachment.objects.get(pk=response.json()['id'])
        self.assertEqual((photo.width, photo.height), (3000, 2000))
        self.assertFalse(has_location(Path(photo.file.path).read_bytes()))

    def test_the_file_picker_offers_uppercase_extensions(self) -> None:
        # The picker's extension filter is case-sensitive on Linux, so .JPG must be listed too.
        self.client.force_login(self.member)
        page = self.client.get(reverse('new_thread')).content.decode()
        for accept in re.findall(r'accept="([^"]+)"', page):
            with self.subTest(accept=accept):
                self.assertIn('.JPG', accept)
                self.assertIn('.jpg', accept)


class LeftoverPhotoTests(TestCase):
    """Posting clears up after its own draft: photos from that editor that went unused."""

    media_root: str
    member: User
    other: User
    thread: Thread
    draft = 'a' * 32       # what the hidden field in one tab's form holds
    other_draft = 'b' * 32  # another tab, another draft

    @classmethod
    def setUpClass(cls) -> None:
        cls.media_root = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.media_root, ignore_errors=True)
        cls.enterClassContext(override_settings(MEDIA_ROOT=cls.media_root))
        super().setUpClass()

    @classmethod
    def setUpTestData(cls) -> None:
        import_forums()
        cls.member = User.objects.create_user('meera')
        cls.other = User.objects.create_user('tenzin')
        cls.thread = start_thread(Forum.objects.get(slug='himalaya-and-ladakh'), cls.member, 'Spiti in June', 'Four days.')

    def waiting_photo(self, member: User | None = None, draft: str = draft) -> Attachment:
        # Big enough to get its smaller copies too, so the test can check those files as well.
        photo = prepare_photo(upload('IMG.jpg', photo_bytes(size=(2000, 1500))))
        return save_photo(member or self.member, photo, draft=draft)

    def reply(self, body: str, draft: str = draft) -> HttpResponse:
        self.client.force_login(self.member)
        with self.captureOnCommitCallbacks(execute=True):  # files go once the change commits
            return self.client.post(self.thread.get_absolute_url(), {'body': body, 'draft': draft})

    def test_replying_keeps_the_placed_photo_and_deletes_this_drafts_leftovers(self) -> None:
        placed, leftover = self.waiting_photo(), self.waiting_photo()
        leftover_files = [Path(stored.path) for stored in (leftover.file, leftover.thumbnail_800)]

        self.reply(f'Here it is.\n\n![a](attachment:{placed.pk})')

        placed.refresh_from_db()
        self.assertEqual(placed.post, Post.objects.latest('created_at'))
        self.assertFalse(Attachment.objects.filter(pk=leftover.pk).exists())
        self.assertFalse(any(path.exists() for path in leftover_files))

    def test_a_posted_photo_gives_up_its_draft_key(self) -> None:
        # The key only groups photos that are still waiting; a post owning the photo ends that.
        placed = self.waiting_photo()
        self.assertEqual(placed.draft_key, self.draft)
        self.reply(f'Here it is.\n\n![a](attachment:{placed.pk})')
        placed.refresh_from_db()
        self.assertEqual(placed.draft_key, '')

    def test_only_waiting_photos_carry_a_draft_key(self) -> None:
        kept, elsewhere = self.waiting_photo(), self.waiting_photo(draft=self.other_draft)
        self.reply(f'Here it is.\n\n![a](attachment:{kept.pk})')
        self.assertFalse(Attachment.objects.filter(post__isnull=False).exclude(draft_key='').exists())
        elsewhere.refresh_from_db()
        self.assertEqual(elsewhere.draft_key, self.other_draft)  # still waiting, still keyed

    def test_another_tabs_draft_is_left_alone(self) -> None:
        mine, elsewhere = self.waiting_photo(), self.waiting_photo(draft=self.other_draft)
        self.reply('Just words.')
        self.assertFalse(Attachment.objects.filter(pk=mine.pk).exists())
        self.assertTrue(Attachment.objects.filter(pk=elsewhere.pk).exists())
        self.assertTrue(Path(elsewhere.file.path).exists())
        self.assertEqual(waiting_photos(self.member), [elsewhere])  # still in the tray

    def test_a_post_without_a_draft_key_deletes_nothing(self) -> None:
        # No JavaScript, so no draft: photos sent with the form are attached, and nothing is tidied.
        waiting = self.waiting_photo()
        self.reply('Just words.', draft='')
        self.assertTrue(Attachment.objects.filter(pk=waiting.pk).exists())

    def test_starting_a_thread_clears_its_own_draft(self) -> None:
        leftover, elsewhere = self.waiting_photo(), self.waiting_photo(draft=self.other_draft)
        self.client.force_login(self.member)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('new_thread'), {
                'forum': Forum.objects.get(slug='himalaya-and-ladakh').pk,
                'title': 'Kaza in July', 'body': 'No photos.', 'draft': self.draft,
            })
        self.assertFalse(Attachment.objects.filter(pk=leftover.pk).exists())
        self.assertTrue(Attachment.objects.filter(pk=elsewhere.pk).exists())

    def test_other_members_photos_are_left_alone(self) -> None:
        theirs = self.waiting_photo(self.other)  # same draft key, different member
        self.reply('Nothing of mine.')
        self.assertTrue(Attachment.objects.filter(pk=theirs.pk).exists())

    def test_a_failed_reply_keeps_the_tray(self) -> None:
        waiting = self.waiting_photo()
        response = self.reply('')  # the body is required
        self.assertContains(response, 'This field is required.')
        self.assertEqual(waiting_photos(self.member), [waiting])

    def test_posted_photos_are_never_touched(self) -> None:
        placed = self.waiting_photo()
        self.reply(f'![a](attachment:{placed.pk})')
        self.reply('A later reply.')
        placed.refresh_from_db()
        self.assertTrue(Path(placed.file.path).exists())

    def test_uploads_remember_their_draft(self) -> None:
        self.client.force_login(self.member)
        for sent, stored in [(self.draft, self.draft), ('not-a-key', ''), ('', '')]:
            with self.subTest(sent=sent):
                response = self.client.post(reverse('photo_upload'), {
                    'photo': upload('IMG.jpg', photo_bytes(size=(600, 400))), 'draft': sent,
                })
                self.assertEqual(Attachment.objects.get(pk=response.json()['id']).draft_key, stored)

    def test_the_form_carries_a_draft_key(self) -> None:
        self.client.force_login(self.member)
        page = self.client.get(self.thread.get_absolute_url()).content.decode()
        keys = re.findall(r'name="draft" value="([^"]+)"', page)
        self.assertEqual(len(keys), 1)
        self.assertRegex(keys[0], r'^[0-9a-f]{32}$')

    def test_a_failed_submit_keeps_the_same_draft_key(self) -> None:
        # Otherwise the photos uploaded before the failure would belong to a draft that no longer exists.
        self.client.force_login(self.member)
        page = self.client.post(self.thread.get_absolute_url(), {'body': '', 'draft': self.draft}).content.decode()
        self.assertIn(f'name="draft" value="{self.draft}"', page)
class HtmlInPostsTests(SimpleTestCase):
    """Posts may contain HTML; nh3's allowlist is what makes that safe."""

    def test_formatting_html_is_kept(self) -> None:
        html = render_body('<b>bold</b>, <u>underlined</u>, <mark>marked</mark> and <sup>up</sup>')
        for tag in ['<b>bold</b>', '<u>underlined</u>', '<mark>marked</mark>', '<sup>up</sup>']:
            self.assertIn(tag, html)

    def test_alignment_survives(self) -> None:
        # Exactly what the editor sends for a centred paragraph.
        self.assertIn('<p style="text-align:center">Kaza at last</p>',
                      render_body('<p style="text-align: center;">Kaza at last</p>'))

    def test_the_editors_table_is_kept_and_tidied(self) -> None:
        editor_output = (
            '<table class="post-table" style="min-width: 50px;"><colgroup><col style="min-width: 25px;"></colgroup>'
            '<tbody><tr><th colspan="1" rowspan="1"><p>Stage</p></th><td colspan="2"><p>4 h</p></td></tr></tbody></table>'
        )
        html = render_body(editor_output)
        self.assertIn('<th colspan="1" rowspan="1"><p>Stage</p></th>', html)
        self.assertIn('<td colspan="2"><p>4 h</p></td>', html)
        self.assertNotIn('colgroup', html)        # column widths are the editor's business
        self.assertNotIn('min-width', html)
        self.assertNotIn('post-table', html)      # the stylesheet styles post tables already

    def test_markdown_tables_still_work(self) -> None:
        html = render_body('| Stage | Hours |\n|---|---|\n| Gramphu | 4 |')
        self.assertIn('<th>Stage</th>', html)
        self.assertIn('<td>Gramphu</td>', html)

    def test_scripts_and_styles_go_with_their_contents(self) -> None:
        html = render_body('before<script>alert(1)</script><style>body{display:none}</style>after')
        self.assertNotIn('alert(1)', html)
        self.assertNotIn('display:none', html)
        self.assertIn('beforeafter', html)

    def test_dangerous_attributes_and_tags_are_removed(self) -> None:
        html = render_body(
            '<a href="javascript:alert(1)">bad</a>'
            '<a href="https://example.com" onclick="steal()">ok</a>'
            '<iframe src="https://example.com"></iframe>'
            '<form action="/x"><input name="card"><button>Send</button></form>'
            '<div onmouseover="x()">hover</div>',
        )
        self.assertNotRegex(html, r'<[^>]*\bon\w+=')
        self.assertNotIn('javascript:', html)
        self.assertNotIn('<iframe', html)
        self.assertNotIn('<input', html)
        self.assertIn('<a href="https://example.com" rel="nofollow ugc noopener noreferrer">ok</a>', html)
        self.assertIn('<div>hover</div>', html)

    def test_only_width_and_alignment_survive_in_a_style(self) -> None:
        html = render_body('<div style="position:fixed;top:0;opacity:0;width:50%;text-align:right">boxed</div>')
        self.assertIn('<div style="width:50%;text-align:right">boxed</div>', html)

    def test_outside_images_still_cannot_load(self) -> None:
        html = render_body('<img src="https://evil.example/track.png" alt="tracker">')
        self.assertNotIn('evil.example', html)

    def test_headings_above_h3_are_dropped_but_their_words_stay(self) -> None:
        html = render_body('<h1>Shouting</h1><h3>Fine</h3>')
        self.assertNotIn('<h1', html)
        self.assertIn('Shouting', html)
        self.assertIn('<h3>Fine</h3>', html)


class MemberProfileTests(TestCase):
    media_root: str
    member: User
    thread: Thread

    @classmethod
    def setUpClass(cls) -> None:
        cls.media_root = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.media_root, ignore_errors=True)
        cls.enterClassContext(override_settings(MEDIA_ROOT=cls.media_root))
        super().setUpClass()

    @classmethod
    def setUpTestData(cls) -> None:
        import_forums()
        cls.member = User.objects.create_user(
            'meera', display_name='Meera Iyer', location='Bengaluru', rides='Himalayan 450', rank='Trail regular')
        himalaya = Forum.objects.get(slug='himalaya-and-ladakh')
        cls.thread = start_thread(himalaya, cls.member, 'Spiti in June', 'Four riding days.')
        add_reply(cls.thread, cls.member, 'Batal by noon.')

    def profile(self) -> HttpResponse:
        return self.client.get(self.member.get_absolute_url())

    def test_the_profile_shows_who_they_are(self) -> None:
        response = self.profile()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<h1>Meera Iyer</h1>', html=True)
        for detail in ['Trail regular', 'Bengaluru', 'Himalayan 450']:
            self.assertContains(response, detail)
        self.assertContains(response, '<div><dt>Posts</dt><dd>2</dd></div>', html=True)
        self.assertContains(response, '<div><dt>Threads</dt><dd>1</dd></div>', html=True)
        self.assertNotContains(response, self.member.email or 'no-email-set@example.com')

    def test_recent_posts_link_to_the_post_itself(self) -> None:
        response = self.profile()
        latest = Post.objects.latest('created_at')
        self.assertContains(response, f'href="{self.thread.get_absolute_url()}#post-{latest.pk}"')
        html = response.content.decode()
        opening = self.thread.posts.first()
        assert opening is not None
        self.assertLess(html.index(f'#post-{latest.pk}'), html.index(f'#post-{opening.pk}'))  # newest first
        self.assertContains(response, 'Batal by noon.')  # an excerpt of each post

    def test_a_post_on_a_later_page_links_to_that_page(self) -> None:
        for n in range(POSTS_PER_PAGE):
            add_reply(self.thread, self.member, f'Reply {n}')
        latest = Post.objects.latest('created_at')
        self.assertContains(self.profile(), f'href="{self.thread.get_absolute_url()}?page=2#post-{latest.pk}"')

    def test_threads_started_are_listed(self) -> None:
        response = self.profile()
        self.assertContains(response, f'<a class="mini__title" href="{self.thread.get_absolute_url()}">Spiti in June</a>', html=True)
        self.assertContains(response, '1 reply')

    def test_photos_they_posted_are_shown(self) -> None:
        photo = prepare_photo(upload('IMG.jpg', photo_bytes(size=(1200, 900))))
        waiting = save_photo(self.member, prepare_photo(upload('IMG.jpg', photo_bytes(size=(1200, 900)))))
        reply = add_reply(self.thread, self.member, 'A photo.', [photo])
        response = self.profile()
        posted = reply.attachments.get()
        self.assertContains(response, f'src="{posted.preview_url}"')
        self.assertContains(response, f'href="{self.thread.get_absolute_url()}#post-{reply.pk}"')
        self.assertNotContains(response, waiting.preview_url)  # not in a post yet, so not on show

    def test_a_member_with_nothing_yet(self) -> None:
        User.objects.create_user('newcomer', display_name='Newcomer')
        response = self.client.get(reverse('member', args=['newcomer']))
        self.assertContains(response, "Newcomer hasn't posted yet.")
        self.assertContains(response, 'No threads started yet.')

    def test_unknown_and_closed_accounts_are_not_found(self) -> None:
        self.assertEqual(self.client.get(reverse('member', args=['nobody'])).status_code, 404)
        User.objects.create_user('retired', is_active=False)
        self.assertEqual(self.client.get(reverse('member', args=['retired'])).status_code, 404)

    def test_usernames_with_symbols_work(self) -> None:
        odd = User.objects.create_user('meera+trips@2026', display_name='Meera Again')
        self.assertContains(self.client.get(odd.get_absolute_url()), '<h1>Meera Again</h1>', html=True)

    def test_posts_and_threads_link_to_profiles(self) -> None:
        url = self.member.get_absolute_url()
        self.assertContains(self.client.get(self.thread.get_absolute_url()), f'href="{url}"')       # a post's author
        self.assertContains(self.client.get(reverse('forum', args=['himalaya-and-ladakh'])), f'href="{url}"')
        self.assertContains(self.client.get(reverse('index')), f'href="{url}"')                      # latest post on a forum row

    def test_the_page_is_a_handful_of_queries(self) -> None:
        with self.assertNumQueries(5):  # the member, their posts, their thread count, their threads, their photos
            self.profile()


class AvatarPreparationTests(SimpleTestCase):
    def test_the_middle_is_cut_out_as_a_square(self) -> None:
        avatar = prepare_avatar(upload('me.jpg', photo_bytes(size=(3000, 2000))))
        with Image.open(avatar) as square:
            self.assertEqual(square.size, (AVATAR_SIZE, AVATAR_SIZE))

    def test_a_small_picture_is_cut_but_never_enlarged(self) -> None:
        avatar = prepare_avatar(upload('tiny.png', photo_bytes('PNG', (120, 90)), 'image/png'))
        with Image.open(avatar) as square:
            self.assertEqual(square.size, (90, 90))
        self.assertTrue(avatar.name.endswith('.png'))

    def test_location_and_camera_details_are_removed(self) -> None:
        avatar = prepare_avatar(upload('IMG_home.jpg', photo_bytes()))
        data = avatar.read()
        self.assertFalse(has_location(data))
        self.assertNotIn(b'home', data)

    def test_only_real_photos_are_accepted(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            prepare_avatar(upload('notes.jpg', b'just some text pretending to be a photo'))
        self.assertIn('notes.jpg', caught.exception.messages[0])


class ProfileEditTests(TestCase):
    """Members changing their own name and photo, from their profile page."""

    media_root: str
    member: User
    other: User
    thread: Thread

    @classmethod
    def setUpClass(cls) -> None:
        cls.media_root = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.media_root, ignore_errors=True)
        cls.enterClassContext(override_settings(MEDIA_ROOT=cls.media_root))
        super().setUpClass()

    @classmethod
    def setUpTestData(cls) -> None:
        import_forums()
        cls.member = User.objects.create_user('meera', password='chai-and-trails', display_name='Meera Iyer')
        cls.other = User.objects.create_user('arjun', password='chai-and-trails', display_name='Arjun Sethi')
        cls.thread = start_thread(
            Forum.objects.get(slug='himalaya-and-ladakh'), cls.member, 'Spiti in June', 'Four riding days.')

    def setUp(self) -> None:
        self.client.force_login(self.member)

    def edit(self, **fields: Any) -> HttpResponse:
        """Send the profile form the way the page does: every field, changed or not."""
        data: dict[str, Any] = {'display_name': self.member.display_name, **fields}
        with self.captureOnCommitCallbacks(execute=True):  # the old file goes once the change commits
            return self.client.post(self.member.get_absolute_url(), data)

    def change_photo(self, name: str = 'me.jpg', content: bytes | None = None) -> HttpResponse:
        return self.edit(avatar=upload(name, photo_bytes(size=(1200, 900)) if content is None else content))

    def stored(self) -> User:
        self.member.refresh_from_db()
        return self.member

    def test_the_photo_is_stored_square_and_cleaned(self) -> None:
        response = self.change_photo()
        self.assertRedirects(response, self.member.get_absolute_url())
        avatar = self.stored().avatar
        self.assertTrue(avatar)
        self.assertRegex(avatar.name, r'^avatars/[0-9a-f]{32}\.jpg$')
        with avatar.open('rb') as saved:
            data = saved.read()
        with Image.open(BytesIO(data)) as square:
            self.assertEqual(square.size, (AVATAR_SIZE, AVATAR_SIZE))
        self.assertFalse(has_location(data))

    def test_pages_show_the_photo_in_place_of_the_letter(self) -> None:
        self.change_photo()
        url = self.stored().avatar.url
        for page in [self.member.get_absolute_url(), self.thread.get_absolute_url(), reverse('index')]:
            with self.subTest(page=page):
                response = self.client.get(page)
                self.assertContains(response, f'src="{url}"')
        # Someone without a photo still gets their letter on a tinted square.
        self.assertContains(self.client.get(self.other.get_absolute_url()), f'tone-{self.other.avatar_tone}')

    def test_a_new_photo_replaces_the_old_file(self) -> None:
        self.change_photo()
        first = self.stored().avatar.name
        self.change_photo('again.jpg')
        second = self.stored().avatar.name
        self.assertNotEqual(first, second)
        self.assertFalse((Path(self.media_root) / first).exists())
        self.assertTrue((Path(self.media_root) / second).exists())

    def test_removing_the_photo_deletes_the_file(self) -> None:
        self.change_photo()
        name = self.stored().avatar.name
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.member.get_absolute_url(), {'remove': '1'})
        self.assertRedirects(response, self.member.get_absolute_url())
        self.assertFalse(self.stored().avatar)
        self.assertFalse((Path(self.media_root) / name).exists())

    def test_a_file_that_is_not_a_photo_is_refused(self) -> None:
        response = self.change_photo('notes.jpg', b'just some text pretending to be a photo')
        self.assertEqual(response.status_code, 200)  # the profile again, with the error on the form
        self.assertContains(response, 'couldn’t be read as a photo')
        self.assertFalse(self.stored().avatar)

    def test_the_form_is_only_on_your_own_profile(self) -> None:
        self.assertContains(self.client.get(self.member.get_absolute_url()), 'Edit your profile')
        self.assertNotContains(self.client.get(self.other.get_absolute_url()), 'Edit your profile')
        self.client.logout()
        self.assertNotContains(self.client.get(self.member.get_absolute_url()), 'Edit your profile')

    def test_the_name_can_be_changed(self) -> None:
        response = self.edit(display_name='Meera I.')
        self.assertRedirects(response, self.member.get_absolute_url())
        self.assertEqual(self.stored().display_name, 'Meera I.')
        # The new name is what posts and the profile show from now on.
        self.assertContains(self.client.get(self.thread.get_absolute_url()), 'Meera I.')
        self.assertContains(self.client.get(self.member.get_absolute_url()), '<h1>Meera I.</h1>', html=True)

    def test_a_blank_name_goes_back_to_the_username(self) -> None:
        self.edit(display_name='')
        self.assertEqual(self.stored().display_name, 'meera')

    def test_the_name_and_the_photo_save_together(self) -> None:
        self.edit(display_name='Meera on tour', avatar=upload('me.jpg', photo_bytes(size=(900, 900))))
        member = self.stored()
        self.assertEqual(member.display_name, 'Meera on tour')
        self.assertTrue(member.avatar)

    def test_a_rejected_photo_leaves_the_name_alone(self) -> None:
        response = self.edit(display_name='Meera I.', avatar=upload('notes.jpg', b'not a photo at all'))
        self.assertContains(response, 'couldn’t be read as a photo')
        member = self.stored()
        self.assertEqual(member.display_name, 'Meera Iyer')  # nothing was saved
        self.assertFalse(member.avatar)

    def test_your_name_in_the_bar_links_to_your_profile(self) -> None:
        response = self.client.get(reverse('index'))
        self.assertContains(response, f'<a href="{self.member.get_absolute_url()}">Meera Iyer</a>', html=True)

    def test_nobody_else_can_change_your_photo(self) -> None:
        self.client.force_login(self.other)
        response = self.client.post(self.member.get_absolute_url(), {'avatar': upload('me.jpg', photo_bytes())})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.stored().avatar)

    def test_a_visitor_is_sent_to_sign_in(self) -> None:
        self.client.logout()
        response = self.client.post(self.member.get_absolute_url(), {'remove': '1'})
        self.assertRedirects(response, f"{reverse('login')}?next={self.member.get_absolute_url()}")

    def test_a_moderator_can_take_a_photo_away(self) -> None:
        self.change_photo()
        name = self.stored().avatar.name
        staff = User.objects.create_superuser('mod', password='chai-and-trails')
        self.client.force_login(staff)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('admin:forum_user_changelist'), {
                'action': 'remove_profile_photo', '_selected_action': [str(self.member.pk)],
            })
        self.assertFalse(self.stored().avatar)
        self.assertFalse((Path(self.media_root) / name).exists())


class MembersPageTests(TestCase):
    """The Members page: real people, in a chosen order, each linking to their profile."""

    @classmethod
    def setUpTestData(cls) -> None:
        # Joined oldest first, and the earliest joiner has posted the most.
        cls.people = [
            User.objects.create_user(
                name, display_name=display, location=place, rank=rank,
                date_joined=timezone.now() - timedelta(days=days), post_count=posts,
            )
            for name, display, place, rank, days, posts in [
                ('arjun', 'Arjun Sethi', 'Chandigarh', 'Moderator', 900, 6215),
                ('meera', 'Meera Iyer', 'Bengaluru', 'Trail regular', 400, 1842),
                ('dev', 'Dev Mahato', '', 'Member', 5, 47),
            ]
        ]

    def members(self, query: str = '') -> HttpResponse:
        return self.client.get(f"{reverse('members')}{query}")

    def test_every_card_links_to_that_member(self) -> None:
        response = self.members()
        for member in self.people:
            with self.subTest(member=member.username):
                self.assertContains(
                    response,
                    f'<a class="member__name" href="{member.get_absolute_url()}">{member.display_name}</a>',
                    html=True,
                )

    def test_a_card_says_who_they_are(self) -> None:
        response = self.members()
        self.assertContains(response, 'Moderator')
        self.assertContains(response, 'Chandigarh')
        self.assertContains(response, '6,215 posts')
        self.assertContains(response, '3 people have signed up.')

    def test_the_busiest_posters_come_first(self) -> None:
        names = [member.display_name for member in self.members().context['members']]
        self.assertEqual(names, ['Arjun Sethi', 'Meera Iyer', 'Dev Mahato'])

    def test_the_newest_arrivals_can_come_first(self) -> None:
        names = [member.display_name for member in self.members('?sort=newest').context['members']]
        self.assertEqual(names, ['Dev Mahato', 'Meera Iyer', 'Arjun Sethi'])
        self.assertContains(self.members('?sort=newest'), 'The newest arrivals first.')

    def test_an_unknown_sort_falls_back_to_the_usual_one(self) -> None:
        response = self.members('?sort=; DROP TABLE forum_user')
        self.assertEqual(response.context['members'][0].display_name, 'Arjun Sethi')

    def test_closed_accounts_are_not_listed(self) -> None:
        User.objects.create_user('retired', display_name='Gone Away', is_active=False)
        self.assertNotContains(self.members(), 'Gone Away')

    def test_paging_keeps_the_order(self) -> None:
        User.objects.bulk_create([
            User(username=f'rider{number}', display_name=f'Rider {number}', post_count=number)
            for number in range(MEMBERS_PER_PAGE)
        ])
        response = self.members('?sort=newest')
        self.assertContains(response, 'href="?page=2&amp;sort=newest"')
        second = self.members('?page=2&sort=newest')
        self.assertEqual(second.context['page_obj'].number, 2)
        self.assertEqual(second.context['sort'], 'newest')
        # Every member appears exactly once across the two pages.
        listed = [m.pk for m in response.context['members']] + [m.pk for m in second.context['members']]
        self.assertEqual(len(listed), len(set(listed)))
        self.assertEqual(len(listed), User.objects.filter(is_active=True).count())

    def test_a_member_with_a_photo_shows_it_here(self) -> None:
        # No file is written: the page only needs the field to have a name to link to.
        User.objects.filter(username='meera').update(avatar='avatars/kaza.jpg')
        self.assertContains(self.members(), 'avatars/kaza.jpg')


class RecentPostersTests(TestCase):
    """The index's "Recently posting" strip: real faces, newest poster first."""

    forum: Forum

    @classmethod
    def setUpTestData(cls) -> None:
        # A forum of its own rather than the imported ones, which arrive with their own posters.
        cls.forum = Forum.objects.create(
            category=Category.objects.create(slug='on-the-road', title='On the road'),
            slug='himalaya', title='Himalaya', description='Passes and cold deserts.',
        )

    def posted(self, username: str, *, days_ago: float = 0, replies: int = 0) -> User:
        member = User.objects.create_user(username, display_name=username.title())
        thread = start_thread(self.forum, member, f'{username} in June', 'Four riding days.')
        for _ in range(replies):
            add_reply(thread, member, 'One more thing.')
        when = timezone.now() - timedelta(days=days_ago)
        Post.objects.filter(author=member).update(created_at=when)
        return member

    def strip(self) -> list[User]:
        return list(self.client.get(reverse('index')).context['recent_posters'])

    def test_the_faces_are_the_people_who_posted_lately(self) -> None:
        meera = self.posted('meera', days_ago=2)
        response = self.client.get(reverse('index'))
        self.assertContains(response, f'<a href="{meera.get_absolute_url()}" title="Meera">')
        self.assertContains(response, 'Recently posting')

    def test_the_newest_poster_comes_first(self) -> None:
        self.posted('arjun', days_ago=5)
        self.posted('meera', days_ago=1)
        self.assertEqual([member.display_name for member in self.strip()], ['Meera', 'Arjun'])

    def test_somebody_who_posted_ten_times_appears_once(self) -> None:
        self.posted('meera', replies=9)
        self.assertEqual([member.display_name for member in self.strip()], ['Meera'])

    def test_older_posts_and_closed_accounts_are_left_out(self) -> None:
        self.posted('lastyear', days_ago=400)
        User.objects.filter(username=self.posted('retired', days_ago=1).username).update(is_active=False)
        self.assertEqual(self.strip(), [])
        self.assertContains(self.client.get(reverse('index')), 'Nobody has posted in the last 7 days.')

    def test_the_strip_holds_at_most_eight_faces(self) -> None:
        for number in range(RECENT_FACES + 2):
            self.posted(f'rider{number}', days_ago=1)
        self.assertEqual(len(self.strip()), RECENT_FACES)


class ForumNumbersTests(TestCase):
    """The index sidebar's totals, which come from the counts posting keeps up to date."""

    @classmethod
    def setUpTestData(cls) -> None:
        import_forums()

    def numbers(self) -> dict[str, Any]:
        return self.client.get(reverse('index')).context['numbers']

    def test_threads_and_posts_add_up_the_whole_board(self) -> None:
        # Subforums count inside their parents, so the total is the top level's.
        top_level = Forum.objects.filter(category__isnull=False)
        self.assertEqual(self.numbers()['threads'], sum(forum.thread_count for forum in top_level))
        self.assertEqual(self.numbers()['posts'], sum(forum.post_count for forum in top_level))

    def test_a_new_post_moves_the_totals(self) -> None:
        before = self.numbers()
        member = User.objects.create_user('meera', display_name='Meera Iyer')
        thread = start_thread(Forum.objects.get(slug='himalaya-and-ladakh'), member, 'Spiti in June', 'Four days.')
        add_reply(thread, member, 'Batal by noon.')
        after = self.numbers()
        self.assertEqual(after['threads'], before['threads'] + 1)
        self.assertEqual(after['posts'], before['posts'] + 2)

    def test_a_subforums_posts_are_counted_once(self) -> None:
        member = User.objects.create_user('arjun', display_name='Arjun Sethi')
        before = self.numbers()['posts']
        subforum = Forum.objects.filter(parent__isnull=False).first()
        assert subforum is not None
        start_thread(subforum, member, 'Hampta in July', 'Two nights out.')
        self.assertEqual(self.numbers()['posts'], before + 1)

    def test_members_are_counted_and_the_newest_is_named(self) -> None:
        User.objects.create_user('early', display_name='Early Bird',
                                 date_joined=timezone.now() - timedelta(days=30))
        newest = User.objects.create_user('latest', display_name='Just Arrived')
        User.objects.create_user('retired', display_name='Gone Away', is_active=False)
        numbers = self.numbers()
        self.assertEqual(numbers['members'], User.objects.filter(is_active=True).count())
        self.assertEqual(numbers['newest'], newest)
        response = self.client.get(reverse('index'))
        self.assertContains(response, f'<a href="{newest.get_absolute_url()}">Just Arrived</a>', html=True)

    def test_a_board_with_nothing_posted_shows_zeroes(self) -> None:
        Forum.objects.update(thread_count=0, post_count=0)
        numbers = self.numbers()
        self.assertEqual((numbers['threads'], numbers['posts']), (0, 0))


class EmptySiteTests(TestCase):
    """The index before anyone has signed up: no counts, and nothing made up."""

    def test_the_numbers_are_all_nothing(self) -> None:
        response = self.client.get(reverse('index'))
        numbers = response.context['numbers']
        self.assertEqual((numbers['threads'], numbers['posts'], numbers['members']), (0, 0, 0))
        self.assertIsNone(numbers['newest'])
        self.assertContains(response, 'Nobody has posted in the last 7 days.')


class WhatsNewTests(TestCase):
    """The What's new page: every section's threads, by when they were last posted in."""

    member: User
    himalaya: Forum
    gear: Forum

    @classmethod
    def setUpTestData(cls) -> None:
        # Two sections of its own: the imported ones arrive with threads already in them.
        category = Category.objects.create(slug='on-the-road', title='On the road')
        cls.himalaya = Forum.objects.create(
            category=category, slug='himalaya-and-ladakh', title='Himalaya and Ladakh', description='Passes.')
        cls.gear = Forum.objects.create(
            category=category, slug='gear-and-packing', title='Gear and packing', description='What to take.')
        cls.member = User.objects.create_user('meera', display_name='Meera Iyer')

    def thread(self, forum: Forum, title: str, *, minutes_ago: int = 0) -> Thread:
        thread = start_thread(forum, self.member, title, 'Four riding days.')
        when = timezone.now() - timedelta(minutes=minutes_ago)
        Thread.objects.filter(pk=thread.pk).update(last_posted_at=when, created_at=when)
        thread.refresh_from_db()
        return thread

    def listed(self) -> list[Thread]:
        return list(self.client.get(reverse('whats_new')).context['threads'])

    def test_threads_from_every_section_in_activity_order(self) -> None:
        old = self.thread(self.gear, 'Panniers that survive a rack', minutes_ago=90)
        recent = self.thread(self.himalaya, 'Spiti in June', minutes_ago=5)
        self.assertEqual(self.listed(), [recent, old])
        response = self.client.get(reverse('whats_new'))
        self.assertContains(response, 'Spiti in June')
        self.assertContains(response, f'href="{reverse("forum", args=[self.gear.slug])}"')  # which section

    def test_a_reply_brings_its_thread_to_the_top(self) -> None:
        first = self.thread(self.himalaya, 'Spiti in June', minutes_ago=60)
        self.thread(self.gear, 'Panniers that survive a rack', minutes_ago=10)
        add_reply(first, self.member, 'Batal by noon.')
        self.assertEqual(self.listed()[0], first)

    def test_pinned_threads_do_not_jump_the_queue(self) -> None:
        # Pinning belongs to a section's own list, not to a page about what just happened.
        recent = self.thread(self.himalaya, 'Spiti in June', minutes_ago=5)
        pinned = self.thread(self.gear, 'Read this first', minutes_ago=300)
        Thread.objects.filter(pk=pinned.pk).update(is_pinned=True)
        self.assertEqual(self.listed()[0], recent)

    def test_the_page_is_paged(self) -> None:
        for number in range(THREADS_PER_PAGE + 1):
            self.thread(self.himalaya, f'Trip {number}', minutes_ago=number)
        response = self.client.get(reverse('whats_new'))
        self.assertEqual(len(response.context['threads']), THREADS_PER_PAGE)
        self.assertContains(response, f'{THREADS_PER_PAGE + 1} threads')
        self.assertEqual(len(self.client.get(f"{reverse('whats_new')}?page=2").context['threads']), 1)

    def test_the_last_page_can_be_asked_for_by_name(self) -> None:
        for number in range(THREADS_PER_PAGE + 1):
            self.thread(self.himalaya, f'Trip {number}', minutes_ago=number)
        page = self.client.get(f"{reverse('whats_new')}?page=last").context['page_obj']
        self.assertEqual((page.number, page.paginator.num_pages), (2, 2))

    def test_a_page_that_is_not_there_is_not_found(self) -> None:
        self.thread(self.himalaya, 'Spiti in June')
        for asked in ['7', 'nonsense', '0', '-1']:
            with self.subTest(page=asked):
                self.assertEqual(self.client.get(f"{reverse('whats_new')}?page={asked}").status_code, 404)

    def test_an_empty_board_says_so(self) -> None:
        self.assertContains(self.client.get(reverse('whats_new')), 'Nothing has been posted yet.')

    def test_every_page_links_to_it(self) -> None:
        url = reverse('whats_new')
        for page in [reverse('index'), reverse('members'), reverse('forum', args=[self.himalaya.slug])]:
            with self.subTest(page=page):
                self.assertContains(self.client.get(page), f'href="{url}"')


class NavHighlightTests(TestCase):
    """Which main-nav item is lit up on each page."""

    @classmethod
    def setUpTestData(cls) -> None:
        import_forums()
        cls.member = User.objects.create_user('meera', display_name='Meera Iyer')

    def lit(self, url: str) -> list[str]:
        """The nav items marked as the current page."""
        body = self.client.get(url).content.decode()
        return re.findall(r'<a href="[^"]*" aria-current="page">([^<]+)</a>', body)

    def test_a_section_with_its_own_nav_item_lights_that_item(self) -> None:
        self.assertEqual(self.lit(reverse('forum', args=['trip-logs'])), ['Trip logs'])
        self.assertEqual(self.lit(reverse('forum', args=['route-notes'])), ['Route notes'])

    def test_a_subforum_lights_the_section_above_it(self) -> None:
        subforum = Forum.objects.filter(parent__slug='trip-logs').first()
        assert subforum is not None
        self.assertEqual(self.lit(reverse('forum', args=[subforum.slug])), ['Trip logs'])

    def test_a_thread_lights_the_section_it_is_in(self) -> None:
        trip_logs = Forum.objects.get(slug='trip-logs')
        thread = start_thread(trip_logs, self.member, 'Spiti in June', 'Four riding days.')
        self.assertEqual(self.lit(thread.get_absolute_url()), ['Trip logs'])

    def test_anywhere_else_lights_forums(self) -> None:
        other = Forum.objects.exclude(slug__in=['trip-logs', 'route-notes']).filter(parent__isnull=True).first()
        assert other is not None
        self.assertEqual(self.lit(reverse('forum', args=[other.slug])), ['Forums'])
        self.assertEqual(self.lit(reverse('index')), ['Forums'])

    def test_the_other_pages_light_their_own_item(self) -> None:
        self.assertEqual(self.lit(reverse('whats_new')), ["What's new"])
        self.assertEqual(self.lit(reverse('members')), ['Members'])
        self.assertEqual(self.lit(self.member.get_absolute_url()), ['Members'])


class SearchTests(TestCase):
    """The masthead search: threads by title and by what was posted in them, plus members."""

    member: User
    other: User
    himalaya: Forum

    @classmethod
    def setUpTestData(cls) -> None:
        category = Category.objects.create(slug='on-the-road', title='On the road')
        cls.himalaya = Forum.objects.create(
            category=category, slug='himalaya-and-ladakh', title='Himalaya and Ladakh', description='Passes.')
        cls.member = User.objects.create_user('meera', display_name='Meera Iyer', location='Bengaluru')
        cls.other = User.objects.create_user('tenzin', display_name='Tenzin Norbu', location='Leh')

    def thread(self, title: str, body: str = 'Nothing much to report.', author: User | None = None) -> Thread:
        return start_thread(self.himalaya, author or self.member, title, body)

    def found(self, asked: str) -> list[Thread]:
        return list(self.client.get(reverse('search'), {'q': asked}).context['threads'])

    def test_a_title_is_searched(self) -> None:
        spiti = self.thread('Spiti in June')
        self.thread('Konkan in the rain')
        self.assertEqual(self.found('spiti'), [spiti])

    def test_what_was_posted_is_searched_too(self) -> None:
        thread = self.thread('Four days out', 'The road to Batal is washed out past Chhatru.')
        self.assertEqual(self.found('chhatru'), [thread])

    def test_words_are_matched_by_their_stem(self) -> None:
        thread = self.thread('Camping above the treeline')
        self.assertEqual(self.found('camped'), [thread])
        self.assertEqual(self.found('camp'), [thread])

    def test_a_quoted_phrase_must_appear_in_that_order(self) -> None:
        together = self.thread('Route notes', 'Ask about the road condition before you set off.')
        self.thread('Other notes', 'The condition of the bike matters more than the road.')
        self.assertEqual(self.found('"road condition"'), [together])

    def test_a_word_can_be_left_out(self) -> None:
        wanted = self.thread('Spiti in June')
        self.thread('Spiti in June by bus')
        self.assertEqual(self.found('spiti -bus'), [wanted])

    def test_a_title_match_outranks_a_match_inside_a_post(self) -> None:
        in_body = self.thread('Four days out', 'We stopped in Kaza for the night.')
        in_title = self.thread('Kaza and back')
        self.assertEqual(self.found('kaza'), [in_title, in_body])

    def test_a_thread_is_listed_once_however_many_posts_match(self) -> None:
        thread = self.thread('Four days out', 'Kaza was cold.')
        for _ in range(3):
            add_reply(thread, self.other, 'Kaza again, still cold.')
        self.assertEqual(self.found('kaza'), [thread])

    def test_members_are_found_by_name_username_or_town(self) -> None:
        for asked in ['Meera', 'meera', 'Bengaluru']:
            with self.subTest(asked=asked):
                found = self.client.get(reverse('search'), {'q': asked}).context['members']
                self.assertEqual(list(found), [self.member])

    def test_an_empty_search_just_asks_for_something(self) -> None:
        response = self.client.get(reverse('search'))
        self.assertContains(response, 'Look through thread titles')
        self.assertNotIn('threads', response.context)
        self.assertContains(self.client.get(reverse('search'), {'q': '   '}), 'Look through thread titles')

    def test_a_search_with_no_hits_says_so(self) -> None:
        self.thread('Spiti in June')
        response = self.client.get(reverse('search'), {'q': 'submarine'})
        self.assertContains(response, 'Nothing matches')

    def test_odd_input_is_a_search_and_not_an_error(self) -> None:
        self.thread('Spiti in June')
        for asked in ['!@#$%', '"unbalanced', "'; DROP TABLE forum_thread; --", 'and or not', ':*', '<script>']:
            with self.subTest(asked=asked):
                self.assertEqual(self.client.get(reverse('search'), {'q': asked}).status_code, 200)
        self.assertTrue(Thread.objects.filter(title='Spiti in June').exists())  # still there

    def test_the_results_are_paged_and_keep_the_search(self) -> None:
        for number in range(THREADS_PER_PAGE + 1):
            self.thread(f'Spiti trip {number}')
        response = self.client.get(reverse('search'), {'q': 'spiti'})
        self.assertEqual(len(response.context['threads']), THREADS_PER_PAGE)
        self.assertContains(response, 'href="?page=2&amp;q=spiti"')
        second = self.client.get(reverse('search'), {'q': 'spiti', 'page': '2'})
        self.assertEqual(len(second.context['threads']), 1)

    def test_the_masthead_box_searches_and_remembers_what_was_asked(self) -> None:
        response = self.client.get(reverse('search'), {'q': 'spiti'})
        self.assertContains(response, f'action="{reverse("search")}"')
        self.assertContains(response, 'value="spiti"')
        # Every other page has the same box, empty.
        self.assertContains(self.client.get(reverse('index')), f'action="{reverse("search")}"')
