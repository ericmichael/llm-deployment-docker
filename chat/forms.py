# chat/forms.py
from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth import get_user_model

from .models import Enrollment


class CustomUserAuthenticationForm(AuthenticationForm):
    username = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(
            attrs={
                "class": "block w-full rounded-md border-0 py-1.5 px-3 text-gray-900 shadow-sm ring-1 ring-inset ring-gray-300 placeholder:text-gray-400 focus:ring-2 focus:ring-inset focus:ring-indigo-600 sm:text-sm sm:leading-6",
                "autocomplete": "email",
                "required": True,
            }
        ),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": "block w-full rounded-md border-0 py-1.5 px-3 text-gray-900 shadow-sm ring-1 ring-inset ring-gray-300 placeholder:text-gray-400 focus:ring-2 focus:ring-inset focus:ring-indigo-600 sm:text-sm sm:leading-6",
                "autocomplete": "current-password",
                "required": True,
            }
        )
    )

    class Meta:
        model = get_user_model()
        fields = ("email", "password")


class CSVImportForm(forms.Form):
    """Form for uploading CSV files to import students or TAs."""

    csv_file = forms.FileField(
        label="CSV File",
        help_text="CSV with columns: email, student_id",
    )
    role = forms.ChoiceField(
        choices=Enrollment.Role.choices,
        initial=Enrollment.Role.STUDENT,
        label="Import as",
    )


class AddTAForm(forms.Form):
    """Form for quickly adding a single TA to a course."""

    email = forms.EmailField(
        label="Email",
        help_text="TA's email address",
    )
    student_id = forms.CharField(
        label="Student ID",
        max_length=50,
        help_text="Used for password generation (ai_<student_id>)",
    )
