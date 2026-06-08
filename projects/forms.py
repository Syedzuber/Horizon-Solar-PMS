import re
from django import forms
from django.contrib.auth.models import User
from .models import UserProfile


class UserCreateForm(forms.Form):
    first_name = forms.CharField(
        max_length=50,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    last_name = forms.CharField(
        max_length=50,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    username = forms.CharField(
        max_length=50,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={'class': 'form-control'}),
    )
    password = forms.CharField(
        min_length=8,
        widget=forms.PasswordInput(attrs={'class': 'form-control'}),
    )
    role = forms.ChoiceField(
        choices=UserProfile.ROLE_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    phone_number = forms.CharField(
        max_length=10,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    is_active = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )

    def clean_first_name(self):
        value = self.cleaned_data['first_name'].strip()
        if not re.fullmatch(r'[A-Za-z]+', value):
            raise forms.ValidationError('First name must contain letters only.')
        return value

    def clean_last_name(self):
        value = self.cleaned_data['last_name'].strip()
        if not re.fullmatch(r'[A-Za-z]+', value):
            raise forms.ValidationError('Last name must contain letters only.')
        return value

    def clean_username(self):
        value = self.cleaned_data['username'].strip().lower()
        if not re.fullmatch(r'[a-z0-9._]+', value):
            raise forms.ValidationError(
                'Username may only contain lowercase letters, digits, dots, and underscores.'
            )
        if User.objects.filter(username=value).exists():
            raise forms.ValidationError('This username is already taken.')
        return value

    def clean_phone_number(self):
        value = self.cleaned_data['phone_number'].strip()
        if not re.fullmatch(r'[0-9]{10}', value):
            raise forms.ValidationError('Phone number must be exactly 10 digits.')
        if value[0] not in '6789':
            raise forms.ValidationError('Phone number must start with 6, 7, 8, or 9.')
        return value

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get('role')
        if role == 'Admin':
            already_exists = UserProfile.objects.filter(role='Admin').exists()
            if already_exists:
                raise forms.ValidationError('Only one Admin account is permitted.')
        return cleaned


class UserEditForm(forms.Form):
    first_name = forms.CharField(
        max_length=50,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    last_name = forms.CharField(
        max_length=50,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    role = forms.ChoiceField(
        choices=UserProfile.ROLE_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    phone_number = forms.CharField(
        max_length=10,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    is_active = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )

    email = forms.EmailField(
        widget=forms.EmailInput(attrs={'class': 'form-control'}),
    )

    def __init__(self, *args, instance_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._instance_user = instance_user

    def clean_first_name(self):
        value = self.cleaned_data['first_name'].strip()
        if not re.fullmatch(r'[A-Za-z]+', value):
            raise forms.ValidationError('First name must contain letters only.')
        return value

    def clean_last_name(self):
        value = self.cleaned_data['last_name'].strip()
        if not re.fullmatch(r'[A-Za-z]+', value):
            raise forms.ValidationError('Last name must contain letters only.')
        return value

    def clean_phone_number(self):
        value = self.cleaned_data['phone_number'].strip()
        if not re.fullmatch(r'[0-9]{10}', value):
            raise forms.ValidationError('Phone number must be exactly 10 digits.')
        if value[0] not in '6789':
            raise forms.ValidationError('Phone number must start with 6, 7, 8, or 9.')
        return value

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get('role')
        if role == 'Admin' and self._instance_user:
            already_exists = UserProfile.objects.filter(role='Admin').exclude(
                user=self._instance_user
            ).exists()
            if already_exists:
                raise forms.ValidationError('Only one Admin account is permitted.')
        return cleaned
