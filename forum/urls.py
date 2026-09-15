from django.contrib.auth import views as auth_views
from django.urls import URLPattern, path
from django.views.generic import TemplateView

from . import views
from .forms import LoginForm

urlpatterns: list[URLPattern] = [
    path('', views.index, name='index'),
    path('forum/<slug:slug>/', views.ForumView.as_view(), name='forum'),
    path('thread/<int:pk>/', views.ThreadView.as_view(), name='thread'),
    path('new-thread/', views.NewThreadView.as_view(), name='new_thread'),
    path('members/', TemplateView.as_view(template_name='members.html'), name='members'),
    path('register/', views.register, name='register'),
    path(
        'login/',
        auth_views.LoginView.as_view(
            template_name='login.html', authentication_form=LoginForm, redirect_authenticated_user=True,
        ),
        name='login',
    ),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
]
