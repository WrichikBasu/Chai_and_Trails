from collections.abc import Iterator
from functools import cached_property
from typing import Any, Final

from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.core.exceptions import ValidationError

from .models import Category, Forum, Thread, User
from .photos import MAX_UPLOAD_BYTES, PreparedPhoto, prepare_photo
from .rendering import photo_ids

POST_MAX_LENGTH: Final[int] = 20_000
MAX_PHOTOS: Final[int] = 10
PHOTOS_HINT: Final[str] = (
    f'Up to {MAX_PHOTOS} photos: JPEG, PNG or WebP, {MAX_UPLOAD_BYTES // (1024 * 1024)} MB each. '
    'They are placed at the end of your post. Location and camera details are removed before anything is stored.'
)
MARKDOWN_HINT: Final[str] = (
    'Formatting uses Markdown: **bold**, _italic_, [link text](https://…), > quote, - list. '
    'Add photos with the Photo button, or paste or drop them into the text; move a photo by moving its '
    '![…](attachment:…) line, and describe it inside the brackets. HTML is shown as plain text.'
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


class MultiplePhotoInput(forms.FileInput):
    allow_multiple_selected = True


class PhotosField(forms.FileField):
    """Up to MAX_PHOTOS photos from one <input type="file" multiple>, each checked and cleaned.

    cleaned_data holds PreparedPhoto objects: already re-encoded without metadata,
    ready for start_thread() or add_reply() to save.
    """

    widget = MultiplePhotoInput(attrs={
        # Also makes iPhones convert HEIC photos to JPEG as they are picked.
        'accept': 'image/jpeg,image/png,image/webp,.jpg,.JPG,.jpeg,.JPEG,.png,.PNG,.webp,.WEBP',
    })

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault('required', False)
        kwargs.setdefault('label', 'Photos')
        kwargs.setdefault('help_text', PHOTOS_HINT)
        super().__init__(**kwargs)

    def clean(self, data: Any, initial: Any = None) -> list[PreparedPhoto]:
        uploads = [upload for upload in (data if isinstance(data, (list, tuple)) else [data]) if upload]
        if len(uploads) > MAX_PHOTOS:
            raise ValidationError(f'Attach up to {MAX_PHOTOS} photos to one post ({len(uploads)} chosen).')
        photos: list[PreparedPhoto] = []
        errors: list[ValidationError] = []
        for upload in uploads:
            try:
                photos.append(prepare_photo(super().clean(upload, initial)))
            except ValidationError as error:
                errors.append(error)  # report every bad file at once, not just the first
        if errors:
            raise ValidationError(errors)
        return photos


def body_field() -> forms.CharField:
    return forms.CharField(
        max_length=POST_MAX_LENGTH,
        help_text=MARKDOWN_HINT,
        widget=forms.Textarea(attrs={'rows': 10}),
    )


class PhotoLimitMixin(forms.BaseForm):
    """No more than MAX_PHOTOS per post, counting photos placed in the text and any sent with the form."""

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean()
        total = len(photo_ids(cleaned.get('body') or '')) + len(cleaned.get('photos') or [])
        if total > MAX_PHOTOS:
            self.add_error('body', f'A post can hold up to {MAX_PHOTOS} photos ({total} added).')
        return cleaned


class NewThreadForm(PhotoLimitMixin, forms.ModelForm):
    """A ModelForm for Thread's own fields, plus the opening post's text."""

    body = body_field()
    photos = PhotosField()

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


class ReplyForm(PhotoLimitMixin, forms.Form):
    body = body_field()
    photos = PhotosField()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields['body'].label = 'Your reply'
        self.fields['body'].widget.attrs.update({
            'id': 'reply-body',  # the Quote buttons add to this box
            'placeholder': 'Add what you know: road condition, fuel stops, where you stayed, what it cost.',
        })
        style_controls(self)
