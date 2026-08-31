from rest_framework.permissions import BasePermission


def is_active_profile(user):
    profile = getattr(user, "profile", None)
    return bool(user and user.is_authenticated and profile and not profile.is_disabled)


def is_admin_profile(user):
    profile = getattr(user, "profile", None)
    return bool(is_active_profile(user) and (user.is_staff or profile.is_admin))


class IsActiveProfile(BasePermission):
    message = "An active application profile is required."

    def has_permission(self, request, view):
        return is_active_profile(request.user)


class IsAdminProfile(BasePermission):
    message = "Administrator access is required."

    def has_permission(self, request, view):
        return is_admin_profile(request.user)
