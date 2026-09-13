from typing import TypedDict

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse

from .forum_data import find_forum, load_categories


class Crumb(TypedDict):
    title: str
    url: str


def index(request: HttpRequest) -> HttpResponse:
    return render(request, 'index.html', {'categories': load_categories()})


def forum(request: HttpRequest, slug: str) -> HttpResponse:
    location = find_forum(slug)
    if location is None:
        raise Http404(f'No forum with slug {slug!r}')

    # The first crumb ("Forums") and the current page are fixed in the template.
    category = location.category
    breadcrumbs: list[Crumb] = [
        {'title': category['title'], 'url': f"{reverse('index')}#cat-{category['slug']}"},
        *({'title': parent['title'], 'url': reverse('forum', args=[parent['slug']])} for parent in location.parents),
    ]
    return render(request, 'forum.html', {'forum': location.forum, 'breadcrumbs': breadcrumbs})
