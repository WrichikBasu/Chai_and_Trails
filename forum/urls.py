from django.contrib.auth import views as auth_views
from django.urls import URLPattern, path

from . import views
from .forms import LoginForm

urlpatterns: list[URLPattern] = [
    path('', views.index, name='index'),
    path('forum/<slug:slug>/', views.ForumView.as_view(), name='forum'),
    path('thread/<int:pk>/<slug:slug>/', views.ThreadView.as_view(), name='thread'),
    # Links without the slug still work: the view redirects them to the full address.
    path('thread/<int:pk>/', views.ThreadView.as_view(), name='thread_by_id'),
    path('new-thread/', views.NewThreadView.as_view(), name='new_thread'),
    path('photos/upload/', views.PhotoUploadView.as_view(), name='photo_upload'),
    path('photos/<int:pk>/remove/', views.PhotoRemoveView.as_view(), name='photo_remove'),
    path('members/', views.MembersView.as_view(), name='members'),
    # Usernames allow @ . + - _ , which <slug:…> would refuse, so match the whole segment.
    path('member/<str:username>/', views.MemberView.as_view(), name='member'),
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
