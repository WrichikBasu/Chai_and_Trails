from django.urls import URLPattern, path
from django.views.generic import TemplateView

from . import views

urlpatterns: list[URLPattern] = [
    path('', views.index, name='index'),
    path('forum/<slug:slug>/', views.forum, name='forum'),
    path('thread/', TemplateView.as_view(template_name='thread.html'), name='thread'),
    path('new-thread/', TemplateView.as_view(template_name='new-thread.html'), name='new_thread'),
    path('members/', TemplateView.as_view(template_name='members.html'), name='members'),
    path('register/', TemplateView.as_view(template_name='register.html'), name='register'),
]
