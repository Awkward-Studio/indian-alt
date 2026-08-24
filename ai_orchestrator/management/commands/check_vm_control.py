import json

from django.core.management.base import BaseCommand, CommandError

from ai_orchestrator.services.vm_service import VMControlService


class Command(BaseCommand):
    help = "Report Azure VM control readiness without printing credentials."

    def add_arguments(self, parser):
        parser.add_argument("--verify-target", action="store_true")

    def handle(self, *args, **options):
        service = VMControlService()
        snapshot = service.snapshot()
        if options["verify_target"]:
            if service.resource_group != "indian-alt-prod_group" or service.vm_name != "indian-alt-prod":
                raise CommandError("Configured Azure target does not match indian-alt-prod.")
            if not service.read_available or snapshot.power_state in {"unknown", "unavailable"}:
                raise CommandError("Azure target verification failed.")
        self.stdout.write(json.dumps({
            "enabled": service.enabled,
            "configured": service.configured,
            "available": service.available,
            "read_available": service.read_available,
            "target_label": service.target_label,
            "resource_group": service.resource_group,
            "vm_name": service.vm_name,
            "power_state": snapshot.power_state,
            "services": snapshot.services,
            "error": snapshot.error,
        }, sort_keys=True))
