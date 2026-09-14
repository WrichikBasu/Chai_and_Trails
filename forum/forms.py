from typing import Any

from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.core.exceptions import ValidationError

from .models import User


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
