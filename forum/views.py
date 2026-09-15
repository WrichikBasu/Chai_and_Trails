from functools import cached_property
from typing import Any, Final, TypedDict, cast

from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.db.models import F, Prefetch, Q, QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from django.views.generic import CreateView, ListView

from .forms import NewThreadForm, RegistrationForm, ReplyForm
from .models import Category, Forum, Post, Thread, User
from .posting import add_reply, start_thread

THREADS_PER_PAGE: Final[int] = 20
POSTS_PER_PAGE: Final[int] = 20
LATEST_TRIP_LOGS: Final[int] = 4


class Crumb(TypedDict):
    title: str
    url: str


def with_last_post(forums: QuerySet[Forum]) -> QuerySet[Forum]:
    """Load each forum's latest post, its thread and author in the same query."""
    return forums.select_related('last_post__thread', 'last_post__author')


def forum_crumbs(forum: Forum, *, include_forum: bool) -> list[Crumb]:
    """The category, then each parent forum (and the forum itself for pages inside it)."""
    parents = forum.ancestors()
    category = (parents[0] if parents else forum).category
    return [
        {'title': category.title, 'url': f"{reverse('index')}#cat-{category.slug}"},
        *({'title': f.title, 'url': reverse('forum', args=[f.slug])} for f in [*parents, *([forum] if include_forum else [])]),
    ]


def index(request: HttpRequest) -> HttpResponse:
    categories = Category.objects.prefetch_related(
        Prefetch('forums', queryset=with_last_post(Forum.objects.all())),
        'forums__children',
    )
    trip_logs = (
        Thread.objects.filter(Q(forum__slug='trip-logs') | Q(forum__parent__slug='trip-logs'))
        .select_related('author').order_by('-created_at')[:LATEST_TRIP_LOGS]
    )
    return render(request, 'index.html', {'categories': categories, 'trip_logs': trip_logs})


class ElidedPagesMixin:
    """Adds page_range: "1 2 3 … 12", with the current page and its neighbours spelled out."""

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = super().get_context_data(**kwargs)  # type: ignore[misc]
        page = context['page_obj']
        context['page_range'] = page.paginator.get_elided_page_range(page.number, on_each_side=2, on_ends=1)
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

    def get_queryset(self) -> QuerySet[Thread]:
        return self.forum.threads.select_related('author', 'last_post__author')

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.update(forum=self.forum, breadcrumbs=forum_crumbs(self.forum, include_forum=False))
        return context


class ThreadView(ElidedPagesMixin, ListView):
    """A page of a thread's posts, with the reply form. GET reads; POST replies."""

    template_name = 'thread.html'
    context_object_name = 'posts'
    paginate_by = POSTS_PER_PAGE  # ?page=last jumps to the newest posts

    @cached_property
    def thread(self) -> Thread:
        return get_object_or_404(Thread.objects.select_related('forum', 'author'), pk=self.kwargs['pk'])

    def get_queryset(self) -> QuerySet[Post]:
        return self.thread.posts.select_related('author')

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        Thread.objects.filter(pk=self.thread.pk).update(view_count=F('view_count') + 1)
        return super().get(request, *args, **kwargs)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if self.thread.is_closed:
            raise PermissionDenied('This thread is closed to new replies.')

        form = ReplyForm(request.POST)
        if form.is_valid():
            post = add_reply(self.thread, cast(User, request.user), form.cleaned_data['body'])
            return redirect(self.thread.latest_post_url(post))
        # Show the page again with the errors, and what they typed still in the box.
        self.object_list = self.get_queryset()
        return self.render_to_response(self.get_context_data(form=form))

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.setdefault('form', ReplyForm())
        context.update(thread=self.thread, breadcrumbs=forum_crumbs(self.thread.forum, include_forum=True))
        return context


class NewThreadView(LoginRequiredMixin, CreateView):
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
        self.object = start_thread(data['forum'], cast(User, self.request.user), data['title'], data['body'])
        return redirect(self.object)


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
