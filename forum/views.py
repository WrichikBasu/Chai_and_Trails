from datetime import timedelta
from functools import cached_property
from typing import Any, Final, TypedDict, cast

from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.contrib.postgres.search import SearchQuery, SearchRank
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import InvalidPage, Paginator
from django.db.models import Count, Exists, F, Max, OuterRef, Prefetch, Q, QuerySet, Subquery, Value
from django.db.models.functions import Coalesce
from django.http import (
    Http404, HttpRequest, HttpResponse, HttpResponsePermanentRedirect, HttpResponseRedirect, JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_http_methods
from django.views.generic import CreateView, DetailView, ListView, View

from .forms import MAX_PHOTOS, NewThreadForm, ProfileForm, RegistrationForm, ReplyForm, draft_key
from .models import SEARCH_CONFIG, Attachment, Category, Forum, Post, Thread, User
from .photos import MAX_UPLOAD_BYTES, prepare_photo
from .posting import add_reply, discard_photos, save_photo, start_thread, waiting_photos
from .rendering import photo_markdown, render_body

THREADS_PER_PAGE: Final[int] = 20
POSTS_PER_PAGE: Final[int] = 20
LATEST_TRIP_LOGS: Final[int] = 4
# Forums with a main-nav item of their own, by slug (see partials/header.html).
NAV_SECTIONS: Final[frozenset[str]] = frozenset({'trip-logs', 'route-notes'})
# The index's "Recently posting" strip: how far back it looks, and how many faces fit.
RECENT_POSTER_WINDOW: Final[timedelta] = timedelta(days=7)
RECENT_FACES: Final[int] = 8
# Search: how much a title match counts next to a match inside a post, and how many
# members a search can turn up beside the threads.
TITLE_WEIGHT: Final[float] = 2.0
SEARCH_MEMBERS: Final[int] = 6
MEMBERS_PER_PAGE: Final[int] = 24
# What the Members page can be sorted by, and the ?sort= value that asks for it.
MEMBER_ORDERINGS: Final[dict[str, list[str]]] = {
    'posts': ['-post_count', 'username'],
    'newest': ['-date_joined', 'username'],
}
DEFAULT_MEMBER_SORT: Final[str] = 'posts'
PROFILE_POSTS: Final[int] = 10
PROFILE_THREADS: Final[int] = 5
PROFILE_PHOTOS: Final[int] = 6
# Photos a member has uploaded that no post has claimed yet. Enough for a long trip report
# in progress, few enough that the upload endpoint can't be used as free file hosting.
MAX_WAITING_PHOTOS: Final[int] = 30


class Crumb(TypedDict):
    title: str
    url: str


def with_last_post(forums: QuerySet[Forum]) -> QuerySet[Forum]:
    """Load each forum's latest post, its thread and author in the same query."""
    return forums.select_related('last_post__thread', 'last_post__author')


def forum_chain(forum: Forum) -> list[Forum]:
    """A forum and every forum above it, outermost first. Walked once per page: the
    breadcrumbs and the main-nav highlight are both worked out from it."""
    return [*forum.ancestors(), forum]


def forum_crumbs(chain: list[Forum], *, include_forum: bool) -> list[Crumb]:
    """The category, then each parent forum (and the forum itself for pages inside it)."""
    category = chain[0].category
    forums = chain if include_forum else chain[:-1]
    return [
        {'title': category.title, 'url': f"{reverse('index')}#cat-{category.slug}"},
        *({'title': f.title, 'url': reverse('forum', args=[f.slug])} for f in forums),
    ]


def nav_section(chain: list[Forum]) -> str:
    """Which main-nav item to light up for a page inside this forum.

    Two sections have their own nav item, so a subforum or thread under them
    highlights that item rather than Forums: a thread in Trip logs › On foot
    belongs to Trip logs as far as the nav is concerned.
    """
    for forum in chain:
        if forum.slug in NAV_SECTIONS:
            return forum.slug
    return 'forums'


class ForumNumbers(TypedDict):
    threads: int
    posts: int
    members: int
    newest: User | None


def forum_numbers(categories: list[Category], members: QuerySet[User]) -> ForumNumbers:
    """The totals for the index sidebar.

    Threads and posts are added up from the top-level forums already loaded for
    the page, so they cost nothing and agree with the counts shown on the rows.
    Only the top level is counted: posting adds to a forum and to every forum
    above it, so a parent's count already holds its subforums'.
    """
    top_level = [forum for category in categories for forum in category.forums.all()]
    return {
        'threads': sum(forum.thread_count for forum in top_level),
        'posts': sum(forum.post_count for forum in top_level),
        'members': members.count(),
        'newest': members.order_by('-date_joined').first(),
    }


def index(request: HttpRequest) -> HttpResponse:
    # A list, not a queryset: the totals below walk it, and the template then reuses
    # what was loaded here instead of asking for the forums a second time.
    categories = list(Category.objects.prefetch_related(
        Prefetch('forums', queryset=with_last_post(Forum.objects.all())),
        'forums__children',
    ))
    trip_logs = (
        Thread.objects.filter(Q(forum__slug='trip-logs') | Q(forum__parent__slug='trip-logs'))
        .select_related('author').order_by('-created_at')[:LATEST_TRIP_LOGS]
    )
    # Who has been posting lately. Annotating with each member's newest post groups by
    # member, so someone who wrote ten replies this week appears once, not ten times.
    members = User.objects.filter(is_active=True)
    posted_lately = members.filter(
        posts__created_at__gte=timezone.now() - RECENT_POSTER_WINDOW,
    ).annotate(latest_post=Max('posts__created_at'))
    return render(request, 'index.html', {
        'categories': categories,
        'trip_logs': trip_logs,
        'recent_posters': posted_lately.order_by('-latest_post')[:RECENT_FACES],
        'recent_days': RECENT_POSTER_WINDOW.days,
        'numbers': forum_numbers(categories, members),
    })


class ElidedPagesMixin:
    """Adds page_range: "1 2 3 … 12", with the current page and its neighbours spelled out."""

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = super().get_context_data(**kwargs)  # type: ignore[misc]
        page = context['page_obj']
        context['page_range'] = page.paginator.get_elided_page_range(page.number, on_each_side=2, on_ends=1)
        return context


class PhotoEditorMixin:
    """What the visual editor needs: the member's photo tray, and after a failed submit,
    the post they were writing, rendered back to HTML for the editor to reopen."""

    request: HttpRequest

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = super().get_context_data(**kwargs)  # type: ignore[misc]
        if not self.request.user.is_authenticated:
            return context
        photos = waiting_photos(cast(User, self.request.user))
        context['waiting_photos'] = photos
        context.update(max_photos=MAX_PHOTOS, max_upload_mb=MAX_UPLOAD_BYTES // (1024 * 1024))
        form = context.get('form')
        if form is not None and form.is_bound:
            context['editor_html'] = render_body(form.data.get('body', ''), {photo.pk: photo for photo in photos})
        return context


class ForumView(ElidedPagesMixin, ListView):
    """A forum's subforums and a page of its threads, pinned first, then by latest activity."""

    template_name = 'forum.html'
    context_object_name = 'threads'
    paginate_by = THREADS_PER_PAGE

    @cached_property
    def forum(self) -> Forum:
        return get_object_or_404(
            with_last_post(Forum.objects.prefetch_related(
                Prefetch('children', queryset=with_last_post(Forum.objects.all())),
            )),
            slug=self.kwargs['slug'],
        )

    @cached_property
    def chain(self) -> list[Forum]:
        return forum_chain(self.forum)

    def get_queryset(self) -> QuerySet[Thread]:
        return self.forum.threads.select_related('author', 'last_post__author')

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.update(
            forum=self.forum,
            breadcrumbs=forum_crumbs(self.chain, include_forum=False),
            nav_active=nav_section(self.chain),
        )
        return context


class ThreadView(PhotoEditorMixin, ElidedPagesMixin, ListView):
    """A page of a thread's posts, with the reply form. GET reads; POST replies."""

    template_name = 'thread.html'
    context_object_name = 'posts'
    paginate_by = POSTS_PER_PAGE  # ?page=last jumps to the newest posts

    @cached_property
    def thread(self) -> Thread:
        return get_object_or_404(Thread.objects.select_related('forum', 'author'), pk=self.kwargs['pk'])

    def get_queryset(self) -> QuerySet[Post]:
        return self.thread.posts.select_related('author')  # photos are already in each post's body_html

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        # A missing or outdated slug (no slug given, a typo, a renamed thread) gets a permanent
        # redirect to the one proper address. The query string comes along; browsers keep #post-….
        if kwargs.get('slug') != self.thread.slug:
            query = request.GET.urlencode()
            return HttpResponsePermanentRedirect(self.thread.get_absolute_url() + (f'?{query}' if query else ''))
        Thread.objects.filter(pk=self.thread.pk).update(view_count=F('view_count') + 1)
        return super().get(request, *args, **kwargs)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if self.thread.is_closed:
            raise PermissionDenied('This thread is closed to new replies.')

        form = ReplyForm(request.POST, request.FILES)  # the text from the body; photos from the upload parts
        if form.is_valid():
            data = form.cleaned_data
            post = add_reply(self.thread, cast(User, request.user), data['body'], data['photos'], data['draft'])
            return redirect(self.thread.latest_post_url(post))
        # Show the page again with the errors, and what they typed still in the box.
        self.object_list = self.get_queryset()
        return self.render_to_response(self.get_context_data(form=form))

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.setdefault('form', ReplyForm())
        chain = forum_chain(self.thread.forum)
        context.update(
            thread=self.thread,
            breadcrumbs=forum_crumbs(chain, include_forum=True),
            nav_active=nav_section(chain),
        )
        return context


class NewThreadView(LoginRequiredMixin, PhotoEditorMixin, CreateView):
    """Start a thread: its title and section, and the opening post."""

    form_class = NewThreadForm
    template_name = 'new-thread.html'

    def get_initial(self) -> dict[str, Any]:
        # "Start a thread" on a forum page links here with ?forum=<slug>, so that forum is preselected.
        # Only a GET shows the blank form; on POST the submitted choice wins, so skip the lookup.
        slug = self.request.GET.get('forum')
        if self.request.method != 'GET' or not slug:
            return {}
        forum = Forum.objects.filter(slug=slug).first()
        return {'forum': forum} if forum else {}

    def form_valid(self, form: NewThreadForm) -> HttpResponseRedirect:
        # Instead of form.save(): start_thread also writes the opening post and updates the counts.
        data = form.cleaned_data
        self.object = start_thread(
            data['forum'], cast(User, self.request.user), data['title'], data['body'], data['photos'], data['draft'],
        )
        return redirect(self.object)


def paginate(request: HttpRequest, objects: QuerySet[Any], per_page: int) -> dict[str, Any]:
    """One page of `objects`, and what the pager partial needs to draw itself.

    This is what ListView does for the pages built on it. ?page=last asks for
    the newest page without knowing how many there are, and anything else that
    isn't a real page number is a 404 rather than a silent jump to page 1.
    """
    paginator = Paginator(objects, per_page)
    asked = request.GET.get('page') or 1
    try:
        page = paginator.page(paginator.num_pages if asked == 'last' else asked)
    except InvalidPage as error:
        raise Http404(f'Invalid page ({asked}): {error}')
    return {
        'paginator': paginator,
        'page_obj': page,
        'is_paginated': page.has_other_pages(),
        'page_range': paginator.get_elided_page_range(page.number, on_each_side=2, on_ends=1),
    }


def search(request: HttpRequest) -> HttpResponse:
    """What the masthead box looks for: threads, by title and by what was posted in them.

    PostgreSQL does the matching against the stored search_vector columns, so a
    search for "camping" finds "camped" and a search for "the" finds nothing.
    "websearch" is the query language people already know from search engines:
    quoted phrases, OR, and -word to leave something out. Whatever they type is
    a query rather than an error, which plain tsquery input would not be.

    A thread is a hit if its title matches or any of its posts do, and it is
    ranked on both, with the title counting for more.
    """
    asked = request.GET.get('q', '').strip()
    if not asked:
        return render(request, 'search.html', {'search_query': asked})

    query = SearchQuery(asked, search_type='websearch', config=SEARCH_CONFIG)
    matching_posts = Post.objects.filter(thread=OuterRef('pk'), search_vector=query)
    best_post = matching_posts.annotate(
        rank=SearchRank(F('search_vector'), query),
    ).order_by('-rank').values('rank')[:1]
    threads = (
        Thread.objects
        .filter(Q(search_vector=query) | Exists(matching_posts))
        .annotate(
            score=(
                SearchRank(F('search_vector'), query) * TITLE_WEIGHT
                + Coalesce(Subquery(best_post), Value(0.0))
            ),
        )
        .select_related('author', 'forum', 'last_post__author')
        .order_by('-score', '-last_posted_at', '-id')  # newest first among equally good matches
    )
    # Names are looked up as they are written, not stemmed: nobody searches for half a name.
    members = User.objects.filter(is_active=True).filter(
        Q(display_name__icontains=asked) | Q(username__icontains=asked) | Q(location__icontains=asked),
    ).order_by('-post_count', 'username')[:SEARCH_MEMBERS]

    page = paginate(request, threads, THREADS_PER_PAGE)
    return render(request, 'search.html', {
        'search_query': asked,
        'pager_query': urlencode({'q': asked}),  # page 2 of the same search, not of nothing
        'threads': page['page_obj'].object_list,
        'members': members,
        **page,
    })


def whats_new(request: HttpRequest) -> HttpResponse:
    """Threads from every section, the ones posted in most recently first.

    Thread.Meta puts pinned threads at the top, which is right inside a section
    but not here: this page is about when something was last said, so it orders
    by that alone, with the id breaking ties between posts at the same instant.
    """
    threads = (
        Thread.objects.select_related('author', 'forum', 'last_post__author')
        .order_by('-last_posted_at', '-id')
    )
    page = paginate(request, threads, THREADS_PER_PAGE)
    return render(request, 'whats-new.html', {'threads': page['page_obj'].object_list, **page})


class MembersView(ElidedPagesMixin, ListView):
    """Everyone who has signed up: the busiest posters first, or the newest arrivals.

    Both orderings break ties on the username, so paging never shows the same
    member twice or skips one when several share a post count or a join date.
    """

    template_name = 'members.html'
    context_object_name = 'members'
    paginate_by = MEMBERS_PER_PAGE

    @cached_property
    def sort(self) -> str:
        chosen = self.request.GET.get('sort', '')
        return chosen if chosen in MEMBER_ORDERINGS else DEFAULT_MEMBER_SORT

    def get_queryset(self) -> QuerySet[User]:
        return User.objects.filter(is_active=True).order_by(*MEMBER_ORDERINGS[self.sort])

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        # The pager links keep the ordering; without it, page 2 would start sorting afresh.
        context.update(sort=self.sort, query='' if self.sort == DEFAULT_MEMBER_SORT else f'sort={self.sort}')
        return context


class MemberView(DetailView):
    """A member's profile: who they are, and what they have posted lately.

    Looking at your own also gives you the profile photo form, which posts back here.
    """

    template_name = 'profile.html'
    context_object_name = 'member'
    slug_field = 'username'
    slug_url_kwarg = 'username'
    # Closed accounts are hidden rather than shown as empty profiles.
    queryset = User.objects.filter(is_active=True)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        """Change the name or photo, or take the photo away. Only the member's own profile."""
        self.object = self.get_object()
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if request.user != self.object:
            raise PermissionDenied('You can only change your own profile.')

        if 'remove' in request.POST:  # its own small form, so nothing else is being sent
            self.object.set_avatar(None)
            return redirect(self.object)
        form = ProfileForm(request.POST, request.FILES, instance=self.object)
        if form.is_valid():
            form.save()
            # Redirect rather than render, so reloading the profile doesn't send the photo again.
            return redirect(self.object)
        return self.render_to_response(self.get_context_data(profile_form=form))

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        member = self.object
        is_owner = self.request.user == member
        context['is_owner'] = is_owner
        if is_owner:
            context.setdefault('profile_form', ProfileForm(instance=member))
        # How many posts come before each one in its thread, so the link can name the right page.
        earlier = (
            Post.objects.filter(thread=OuterRef('thread'), created_at__lt=OuterRef('created_at'))
            .values('thread').annotate(total=Count('pk')).values('total')
        )
        posts = list(
            member.posts.select_related('thread', 'thread__forum')
            .annotate(earlier=Subquery(earlier))
            .order_by('-created_at')[:PROFILE_POSTS]
        )
        for post in posts:
            page = (post.earlier or 0) // POSTS_PER_PAGE + 1
            post.url = f'{post.thread.get_absolute_url()}{f"?page={page}" if page > 1 else ""}#post-{post.pk}'

        context.update(
            breadcrumbs=[{'title': 'Members', 'url': reverse('members')}],
            posts=posts,
            threads=member.threads.select_related('forum').order_by('-created_at')[:PROFILE_THREADS],
            thread_count=member.threads.count(),
            photos=(
                Attachment.objects.filter(uploader=member, post__isnull=False)
                .select_related('post__thread').order_by('-created_at')[:PROFILE_PHOTOS]
            ),
        )
        return context


class PhotoUploadView(LoginRequiredMixin, View):
    """The editor's upload: one photo per request, cleaned and stored before its post exists.

    Answers in JSON. On success the reply carries the Markdown line that places
    the photo, which the editor puts in at the cursor.
    """

    raise_exception = True  # a visitor who isn't logged in gets 403, not a login page, since the page's script is asking
    http_method_names = ['post']

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        member = cast(User, request.user)
        upload = request.FILES.get('photo')
        if upload is None:
            return JsonResponse({'error': 'Choose a photo to upload.'}, status=400)
        if Attachment.objects.filter(uploader=member, post__isnull=True).count() >= MAX_WAITING_PHOTOS:
            return JsonResponse({'error': 'You have too many photos waiting to be posted. Post what you have first.'},
                                status=400)
        try:
            photo = prepare_photo(upload)
        except ValidationError as error:
            return JsonResponse({'error': error.messages[0]}, status=400)
        attachment = save_photo(member, photo, draft=draft_key(request.POST.get('draft', '')))
        return JsonResponse({
            'id': attachment.pk,
            'markdown': photo_markdown(attachment.pk),
            'preview_url': attachment.preview_url,   # the tray's thumbnail
            'display_url': attachment.display_url,   # what the editor and the post show
            'width': attachment.width,
            'height': attachment.height,
            'remove_url': reverse('photo_remove', args=[attachment.pk]),
        }, status=201)


class PhotoRemoveView(LoginRequiredMixin, View):
    """Remove a photo from the tray: files and all. Only the member's own, and only while no post has it."""

    raise_exception = True
    http_method_names = ['post']

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        photo = get_object_or_404(Attachment, pk=kwargs['pk'], uploader=request.user, post__isnull=True)
        discard_photos([photo])
        return HttpResponse(status=204)


def favicon(request: HttpRequest) -> HttpResponse:
    """Browsers ask the site root for /favicon.ico whatever the pages link, so send them on.

    A temporary redirect on purpose: static files are served under a hashed name in
    production, so the address of the icon changes whenever the icon does, and a 301
    would sit in browser caches pointing at the old one.
    """
    return redirect(static('img/favicon-32.png'))


@require_http_methods(['GET', 'POST'])
def register(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect('index')

    if request.method == 'POST':
        # request.POST holds the form fields from the request body.
        form = RegistrationForm(request.POST)
        if form.is_valid():
            member = form.save()  # creates the forum_user row, with the password hashed
            login(request, member)
            return redirect('index')
    else:
        form = RegistrationForm()
    return render(request, 'register.html', {'form': form})
