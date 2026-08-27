import logging

from django.conf import settings
from django.contrib.auth import get_user_model, login

logger = logging.getLogger(__name__)
User = get_user_model()


class EasyAuthMiddleware:
    """Read Azure App Service Easy Auth headers and auto-login Django users.

    When running behind Azure Easy Auth, authenticated requests arrive with
    X-MS-CLIENT-PRINCIPAL-NAME set to the user's email/UPN.  This middleware
    looks up (or creates) a matching CustomUser and logs them in via Django's
    session framework.

    Guarded by settings.EASYAUTH_ENABLED so the headers are only trusted
    when running on Azure App Service (where Azure strips/replaces them).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not getattr(settings, "EASYAUTH_ENABLED", False):
            return self.get_response(request)

        principal_name = request.META.get("HTTP_X_MS_CLIENT_PRINCIPAL_NAME")
        if not principal_name:
            return self.get_response(request)

        email = principal_name.strip().lower()

        # Short-circuit if already authenticated as this user
        if request.user.is_authenticated and request.user.email == email:
            return self.get_response(request)

        user, created = User.objects.get_or_create(
            email=email,
            defaults={"is_active": True},
        )
        if created:
            user.set_unusable_password()
            user.save()
            logger.info("Created new user via Easy Auth: %s", email)

        # login() does not check is_active; a deactivated account must stay out.
        if not user.is_active:
            return self.get_response(request)

        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        return self.get_response(request)
