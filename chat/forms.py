# chat/forms.py
from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth import get_user_model

from .models import Course, Enrollment

TAILWIND_INPUT = (
    "block w-full rounded-md border-0 py-1.5 px-3 text-gray-900 shadow-sm "
    "ring-1 ring-inset ring-gray-300 placeholder:text-gray-400 "
    "focus:ring-2 focus:ring-inset focus:ring-indigo-600 sm:text-sm sm:leading-6"
)
TAILWIND_SELECT = (
    "block w-full rounded-md border-0 py-1.5 px-3 text-gray-900 shadow-sm "
    "ring-1 ring-inset ring-gray-300 "
    "focus:ring-2 focus:ring-inset focus:ring-indigo-600 sm:text-sm sm:leading-6"
)
TAILWIND_FILE = (
    "block w-full text-sm text-gray-500 "
    "file:mr-4 file:py-2 file:px-4 file:rounded-md file:border-0 "
    "file:text-sm file:font-semibold file:bg-indigo-50 file:text-indigo-700 "
    "hover:file:bg-indigo-100"
)


class CustomUserAuthenticationForm(AuthenticationForm):
    username = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(
            attrs={
                "class": TAILWIND_INPUT,
                "autocomplete": "email",
                "required": True,
            }
        ),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": TAILWIND_INPUT,
                "autocomplete": "current-password",
                "required": True,
            }
        )
    )

    class Meta:
        model = get_user_model()
        fields = ("email", "password")


class CourseForm(forms.ModelForm):
    """Form for creating/editing a course."""

    class Meta:
        model = Course
        fields = ["code", "name", "semester", "is_active"]
        widgets = {
            "code": forms.TextInput(attrs={"class": TAILWIND_INPUT, "placeholder": "CSCI-4380-01"}),
            "name": forms.TextInput(attrs={"class": TAILWIND_INPUT, "placeholder": "AI Engineering Fall 2026"}),
            "semester": forms.TextInput(attrs={"class": TAILWIND_INPUT, "placeholder": "Fall 2026"}),
        }


class CSVImportForm(forms.Form):
    """Form for uploading CSV files to import students or TAs."""

    csv_file = forms.FileField(
        label="CSV File",
        help_text="CSV with columns: email, student_id",
        widget=forms.ClearableFileInput(attrs={"class": TAILWIND_FILE, "accept": ".csv"}),
    )
    role = forms.ChoiceField(
        choices=Enrollment.Role.choices,
        initial=Enrollment.Role.STUDENT,
        label="Import as",
        widget=forms.Select(attrs={"class": TAILWIND_SELECT}),
    )


class AddTAForm(forms.Form):
    """Form for quickly adding a single TA to a course."""

    email = forms.EmailField(
        label="Email",
        help_text="TA's email address",
        widget=forms.EmailInput(attrs={"class": TAILWIND_INPUT, "placeholder": "ta@university.edu"}),
    )
    student_id = forms.CharField(
        label="Student ID",
        max_length=50,
        help_text="Used for password generation (ai_<student_id>)",
        widget=forms.TextInput(attrs={"class": TAILWIND_INPUT, "placeholder": "123456"}),
    )


class AddStudentForm(forms.Form):
    """Form for quickly adding a single student to a course."""

    email = forms.EmailField(
        label="Email",
        help_text="Student's email address",
        widget=forms.EmailInput(attrs={"class": TAILWIND_INPUT, "placeholder": "student@university.edu"}),
    )
    student_id = forms.CharField(
        label="Student ID",
        max_length=50,
        help_text="Used for password generation (ai_<student_id>)",
        widget=forms.TextInput(attrs={"class": TAILWIND_INPUT, "placeholder": "123456"}),
    )
