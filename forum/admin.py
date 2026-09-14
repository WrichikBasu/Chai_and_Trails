from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User

PROFILE_FIELDS: tuple[str, ...] = ('avatar_tone', 'rank', 'location', 'rides')


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    fieldsets = (*BaseUserAdmin.fieldsets, ('Forum profile', {'fields': PROFILE_FIELDS}))
    add_fieldsets = (*BaseUserAdmin.add_fieldsets, ('Forum profile', {'fields': PROFILE_FIELDS}))
    list_display = ('username', 'email', 'rank', 'location', 'date_joined', 'is_staff')
    list_filter = (*BaseUserAdmin.list_filter, 'rank')
