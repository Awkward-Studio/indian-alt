from rest_framework.permissions import BasePermission


class IsActiveProfile(BasePermission):
    message = "An active application profile is required."

    def has_permission(self, request, view):
        profile = getattr(request.user, "profile", None)
        return bool(request.user and request.user.is_authenticated and profile and not profile.is_disabled)
