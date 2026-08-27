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
        fields = ["code", "name", "semester", "monthly_budget"]  # is_active is toggled separately
        widgets = {
            "code": forms.TextInput(attrs={"class": TAILWIND_INPUT, "placeholder": "CSCI-4380-01"}),
            "name": forms.TextInput(attrs={"class": TAILWIND_INPUT, "placeholder": "AI Engineering Fall 2026"}),
            "semester": forms.TextInput(attrs={"class": TAILWIND_INPUT, "placeholder": "Fall 2026"}),
            "monthly_budget": forms.NumberInput(
                attrs={"class": TAILWIND_INPUT, "placeholder": "default", "step": "0.01", "min": "0"}
            ),
        }
        labels = {"monthly_budget": "Monthly budget (USD)"}


class BudgetForm(forms.Form):
    """Set or clear a monthly budget override (empty = inherit)."""

    monthly_budget = forms.DecimalField(
        required=False, min_value=0, max_digits=8, decimal_places=2,
        widget=forms.NumberInput(attrs={"class": TAILWIND_INPUT, "step": "0.01", "min": "0", "placeholder": "inherit"}),
    )


class CSVImportForm(forms.Form):
    """Form for uploading CSV files to import students or TAs."""

    csv_file = forms.FileField(
        label="CSV File",
        help_text="CSV with column: email",
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


class AddStudentForm(forms.Form):
    """Form for quickly adding a single student to a course."""

    email = forms.EmailField(
        label="Email",
        help_text="Student's email address",
        widget=forms.EmailInput(attrs={"class": TAILWIND_INPUT, "placeholder": "student@university.edu"}),
    )


class CourseAccessForm(forms.Form):
    """Course-wide cap and model allowlist (drives the LiteLLM team)."""

    total_budget = forms.DecimalField(
        required=False, min_value=0, max_digits=10, decimal_places=2,
        widget=forms.NumberInput(attrs={"class": TAILWIND_INPUT + " pl-7", "step": "0.01", "min": "0", "placeholder": "no cap"}),
    )
    allowed_models = forms.MultipleChoiceField(
        required=False, choices=(), widget=forms.CheckboxSelectMultiple
    )

    def __init__(self, *args, model_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["allowed_models"].choices = [(m, m) for m in model_choices]
