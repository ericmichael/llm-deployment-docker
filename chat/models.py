from django.db import models
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.conf import settings


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

    def __str__(self):
        return self.email


def _thread_model_choices():
    configured = getattr(settings, "LITELLM_MODEL_LIST", [])
    seen = []
    for value in configured:
        if value and value not in seen:
            seen.append(value)
    if not seen:
        seen.append(getattr(settings, "LITELLM_DEFAULT_MODEL", "gpt-5"))
    return [(value, value) for value in seen]


class Thread(models.Model):
    MODEL_CHOICES = _thread_model_choices()
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    name = models.CharField(max_length=200, default="New Thread")
    model = models.CharField(
        max_length=100, choices=MODEL_CHOICES, default=settings.LITELLM_DEFAULT_MODEL
    )
    temperature = models.FloatField(default=1)
    prompt = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)  # Add this field


class Message(models.Model):
    ROLE_CHOICES = [
        ("system", "System"),
        ("user", "User"),
        ("assistant", "Assistant"),
    ]

    thread = models.ForeignKey(Thread, on_delete=models.CASCADE)
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    content = models.TextField()
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default="user")
    timestamp = models.DateTimeField(auto_now_add=True)
