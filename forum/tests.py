import json
import re
import unicodedata
from datetime import timedelta
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.contrib.staticfiles import finders
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.models import Model
from django.http import HttpResponse
from django.templatetags.static import static
from django.test import Client, SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import dateformat, timezone

from .management.commands.import_forums import DATA_FILE, JsonData, JsonForum, parse_when
from .models import Category, Forum, Post, Thread, Tone, User
from .posting import add_reply, start_thread
from .rendering import render_body
from .templatetags.forum_extras import compact_count, forum_time
from .views import POSTS_PER_PAGE


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
        self.assertContains(response, '<a href="profile.html">Tenzin Norbu</a> &middot; 22 minutes ago')
        self.assertContains(response, '61,908')

    def test_index_needs_four_queries(self) -> None:
        with self.assertNumQueries(4):  # categories, their forums with latest posts, subforums, trip logs
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
        self.assertContains(self.client.get(reverse('index')), '<span>Meera Iyer</span>', html=True)

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
        self.assertContains(index, '<span>Tenzin Norbu</span>', html=True)
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
        for path in ['css/site.css', 'js/site.js', 'js/theme.js']:
            with self.subTest(path=path):
                self.assertIsNotNone(finders.find(path))

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

    def test_raw_html_is_shown_as_text(self) -> None:
        html = render_body('<script>alert(1)</script> <b onclick="x()">hi</b>')
        self.assertNotIn('<script', html)
        self.assertNotIn('<b', html)
        self.assertIn('&lt;script&gt;', html)

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
        self.assertEqual(self.client.get(reverse('thread', args=[999_999])).status_code, 404)


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
