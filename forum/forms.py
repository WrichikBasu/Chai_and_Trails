from collections.abc import Iterator
from functools import cached_property
from typing import Any, Final

from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.core.exceptions import ValidationError

from .models import Category, Forum, Thread, User

POST_MAX_LENGTH: Final[int] = 20_000
MARKDOWN_HINT: Final[str] = (
    'Formatting uses Markdown: **bold**, _italic_, [link text](https://…), > quote, - list. '
    'HTML is shown as plain text.'
)


def style_controls(form: forms.BaseForm) -> None:
    """Give inputs the mockup's .control class; checkboxes keep their own look."""
    for field in form.fields.values():
        if not isinstance(field.widget, forms.CheckboxInput):
            field.widget.attrs.setdefault('class', 'control')


class RegistrationForm(UserCreationForm):
    """A ModelForm for User: the Meta fields map straight onto forum_user columns.

    Note: UserCreationForm adds password1 and password2, checks they match, runs the
    AUTH_PASSWORD_VALIDATORS and hashes the password on save.
    """

    terms = forms.BooleanField(error_messages={'required': 'Please confirm you have read the forum rules.'})

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'display_name', 'email', 'location')
        labels = {'email': 'Email', 'location': 'Where you set off from'}
        help_texts = {
            'username': 'What you sign in with. Letters, digits and @ . + - _ only. It cannot be changed later.',
            'display_name': 'Shown on every post. Spaces are fine, and you can change it later. '
                            'Leave it blank to use your username.',
        }
        widgets = {
            'display_name': forms.TextInput(attrs={'autocomplete': 'name'}),
            'email': forms.EmailInput(attrs={'autocomplete': 'email'}),
            'location': forms.TextInput(attrs={'placeholder': 'City or town'}),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields['email'].required = True  # optional on the model, but needed for password resets
        self.fields['password1'].help_text = (
            'At least 8 characters. Not all numbers, not a common password, and not close to your name or email.'
        )
        self.fields['password2'].label = 'Confirm password'
        self.fields['password2'].help_text = ''
        style_controls(self)

    def clean_email(self) -> str:
        email: str = self.cleaned_data['email']
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError('An account with this email already exists.')
        return email


class LoginForm(AuthenticationForm):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        style_controls(self)


def forum_choices() -> Iterator[tuple[int, str]]:
    """Every forum in index order, labelled with its path: "On the road: Trip logs › On foot"."""
    def walk(forums: list[Forum], path: str) -> Iterator[tuple[int, str]]:
        for forum in forums:
            label = f'{path}{forum.title}'
            yield forum.pk, label
            yield from walk(list(forum.children.all()), f'{label} › ')

    for category in Category.objects.prefetch_related('forums__children__children'):
        yield from walk(list(category.forums.all()), f'{category.title}: ')


def body_field() -> forms.CharField:
    return forms.CharField(
        max_length=POST_MAX_LENGTH,
        help_text=MARKDOWN_HINT,
        widget=forms.Textarea(attrs={'rows': 10}),
    )


class NewThreadForm(forms.ModelForm):
    """A ModelForm for Thread's own fields, plus the opening post's text."""

    body = body_field()

    class Meta:
        model = Thread
        fields = ('forum', 'title')
        labels = {'forum': 'Post it in'}
        help_texts = {'title': 'Say where and when. "Manali to Kaza, first week of June" beats "help needed urgent".'}
        widgets = {'title': forms.TextInput(attrs={'placeholder': 'Route, month, vehicle'})}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # A callable, so the menu's queries run only when the form is displayed. Validating a
        # submission checks the chosen forum through the field's queryset instead.
        self.fields['forum'].choices = lambda: self.forum_menu
        self.fields['body'].label = 'Post'
        self.fields['body'].widget.attrs['placeholder'] = 'Dates, vehicle, budget, what you have already booked.'
        style_controls(self)

    @cached_property
    def forum_menu(self) -> list[tuple[int | str, str]]:
        # Cached: the <select> widget reads its choices twice when drawn (once to check the first option).
        return [('', 'Choose a section'), *forum_choices()]


class ReplyForm(forms.Form):
    body = body_field()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields['body'].label = 'Your reply'
        self.fields['body'].widget.attrs.update({
            'id': 'reply-body',  # the Quote buttons add to this box
            'placeholder': 'Add what you know: road condition, fuel stops, where you stayed, what it cost.',
        })
        style_controls(self)
