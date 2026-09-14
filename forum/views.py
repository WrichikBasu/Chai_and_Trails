from typing import TypedDict

from django.db.models import Prefetch, QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from .models import Category, Forum


class Crumb(TypedDict):
    title: str
    url: str


def with_last_post(forums: QuerySet[Forum]) -> QuerySet[Forum]:
    """Load each forum's latest post, its thread and author in the same query."""
    return forums.select_related('last_post__thread', 'last_post__author')


def index(request: HttpRequest) -> HttpResponse:
    categories = Category.objects.prefetch_related(
        Prefetch('forums', queryset=with_last_post(Forum.objects.all())),
        'forums__children',
    )
    return render(request, 'index.html', {'categories': categories})


def forum(request: HttpRequest, slug: str) -> HttpResponse:
    current = get_object_or_404(
        with_last_post(Forum.objects.prefetch_related(
            Prefetch('children', queryset=with_last_post(Forum.objects.all())),
        )),
        slug=slug,
    )

    # The first crumb ("Forums") and the current page are fixed in the template.
    parents = current.ancestors()
    category = (parents[0] if parents else current).category
    breadcrumbs: list[Crumb] = [
        {'title': category.title, 'url': f"{reverse('index')}#cat-{category.slug}"},
        *({'title': parent.title, 'url': reverse('forum', args=[parent.slug])} for parent in parents),
    ]
    return render(request, 'forum.html', {'forum': current, 'breadcrumbs': breadcrumbs})
