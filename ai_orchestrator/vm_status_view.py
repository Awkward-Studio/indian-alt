from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .services.vm_service import VMControlService


class VMStatusView(APIView):
    """Read-only availability for chat users; power commands remain admin-only."""
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "head", "options"]

    def get(self, request):
        snapshot = VMControlService().snapshot()
        return Response({
            "power_state": snapshot.power_state,
            "service_state": snapshot.service_state,
            "startup_phase": snapshot.startup_phase,
            "services": snapshot.services,
            "error": "AI server status is unavailable. Refresh to try again." if snapshot.error else "",
        })
