from datetime import datetime, timedelta

from django import template
from django.utils import dateformat, timezone

register = template.Library()


@register.filter
def compact_count(value: int) -> str:
    """Counts as the thread list shows them: 940, 1.4K, 94K, 1.9M."""
    for limit, suffix in [(1_000_000, 'M'), (1_000, 'K')]:
        if value >= limit:
            shown = f'{value / limit:.1f}' if value < limit * 10 else f'{value // limit}'
            return f'{shown.removesuffix(".0")}{suffix}'
    return str(value)


@register.filter
def forum_time(value: datetime) -> str:
    """Forum-style time: "22 minutes ago", "Yesterday at 9:14 PM", "Tuesday at 7:02 AM", "3 Mar 2026"."""
    now = timezone.localtime()
    then = timezone.localtime(value)
    age = now - then

    if age < timedelta(minutes=1):
        return 'Just now'
    if age < timedelta(hours=1):
        minutes = age // timedelta(minutes=1)
        return f'{minutes} minute{"" if minutes == 1 else "s"} ago'
    if age < timedelta(hours=4):
        hours = age // timedelta(hours=1)
        return f'{hours} hour{"" if hours == 1 else "s"} ago'

    clock = dateformat.format(then, 'g:i A')
    days = (now.date() - then.date()).days
    if days == 0:
        return f'Today at {clock}'
    if days == 1:
        return f'Yesterday at {clock}'
    if days < 7:
        return f'{dateformat.format(then, "l")} at {clock}'
    return dateformat.format(then, 'j M Y')
