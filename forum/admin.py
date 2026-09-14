from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User

PROFILE_FIELDS: tuple[str, ...] = ('display_name', 'avatar_tone', 'rank', 'location', 'rides')


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    # Django's own fieldsets list first_name and last_name, which this model replaces.
    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        ('Forum profile', {'fields': (*PROFILE_FIELDS, 'email')}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        ('Important dates', {'fields': ('last_login', 'date_joined')}),
    )
    add_fieldsets = (*BaseUserAdmin.add_fieldsets, ('Forum profile', {'fields': (*PROFILE_FIELDS, 'email')}))
    list_display = ('username', 'display_name', 'email', 'rank', 'location', 'date_joined', 'is_staff')
    list_filter = (*BaseUserAdmin.list_filter, 'rank')
    search_fields = ('username', 'display_name', 'email')
