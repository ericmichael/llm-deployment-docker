from django.db import models
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.utils.translation import gettext_lazy as _


class CustomUserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("The Email field must be set")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(email, password, **extra_fields)


class CustomUser(AbstractUser):
    username = None  # Remove the username field
    email = models.EmailField(_("email address"), unique=True)  # Make email unique

    USERNAME_FIELD = "email"  # Use email as the unique identifier
    REQUIRED_FIELDS = []  # Remove email from REQUIRED_FIELDS

    objects = CustomUserManager()  # Use the custom manager

    litellm_key = models.CharField(max_length=200, blank=True, default="")
    litellm_key_id = models.CharField(max_length=200, blank=True, default="")

    def __str__(self):
        return self.email

    def has_active_enrollment(self):
        """Check if user has an enrollment in any active course."""
        return self.enrollments.filter(course__is_active=True).exists()


class Course(models.Model):
    """A course that students can be enrolled in."""

    name = models.CharField(max_length=200, help_text="e.g., AI Engineering Fall 2026")
    code = models.CharField(
        max_length=50, unique=True, help_text="e.g., CSCI-4380-01"
    )
    semester = models.CharField(max_length=50, help_text="e.g., Fall 2026")
    is_active = models.BooleanField(
        default=True, help_text="Inactive courses deny API access to enrolled students"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.code} - {self.name}"


class Enrollment(models.Model):
    """Links a user to a course with a role."""

    class Role(models.TextChoices):
        STUDENT = "student", _("Student")
        TA = "ta", _("Teaching Assistant")

    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="enrollments"
    )
    user = models.ForeignKey(
        CustomUser, on_delete=models.CASCADE, related_name="enrollments"
    )
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.STUDENT)
    enrolled_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["user__email"]
        constraints = [
            # Students can only be in one course
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(role="student"),
                name="unique_student_enrollment",
            ),
            # A user can only be enrolled once per course (regardless of role)
            models.UniqueConstraint(
                fields=["course", "user"],
                name="unique_course_user",
            ),
        ]

    def __str__(self):
        return f"{self.user.email} - {self.course.code} ({self.role})"
