from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from ai_orchestrator.models import AIAuditLog, AIConversation, AIMessage


class Command(BaseCommand):
    help = "Mark stale PROCESSING chat audit logs as failed so the UI stops showing them as running."

    def add_arguments(self, parser):
        parser.add_argument(
            "--minutes",
            type=int,
            default=10,
            help="Fail PROCESSING universal/deal chat audits older than this many minutes.",
        )
        parser.add_argument(
            "--audit-id",
            action="append",
            dest="audit_ids",
            help="Specific audit id to fail. Can be passed multiple times.",
        )
        parser.add_argument(
            "--create-assistant-message",
            action="store_true",
            help="Also create a short assistant error message in the conversation if no assistant reply exists after the user prompt.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply changes. Default is dry-run.",
        )

    def handle(self, *args, **options):
        dry_run = not bool(options["apply"])
        audit_ids = [value for value in options.get("audit_ids") or [] if value]
        cutoff = timezone.now() - timedelta(minutes=max(int(options["minutes"]), 0))

        queryset = AIAuditLog.objects.filter(status="PROCESSING", source_type__in=["universal_chat", "deal_chat"])
        if audit_ids:
            queryset = queryset.filter(id__in=audit_ids)
        else:
            queryset = queryset.filter(created_at__lt=cutoff)

        stale_audits = list(queryset.order_by("created_at"))
        self.stdout.write(f"[{'DRY-RUN' if dry_run else 'APPLY'}] stale_processing_audits={len(stale_audits)}")

        for audit in stale_audits:
            message = "Chat task stopped before completion. Please retry the query after confirming the worker is running."
            self.stdout.write(f"[STALE] {audit.id} task={audit.celery_task_id} prompt={audit.user_prompt[:120]!r}")

            if dry_run:
                continue

            audit.status = "FAILED"
            audit.is_success = False
            audit.error_message = message
            audit.save(update_fields=["status", "is_success", "error_message"])

            if options["create_assistant_message"] and audit.source_id:
                self._create_failure_message_if_needed(audit, message)

    def _create_failure_message_if_needed(self, audit: AIAuditLog, message: str) -> None:
        try:
            conversation = AIConversation.objects.get(id=audit.source_id)
        except AIConversation.DoesNotExist:
            return

        later_assistant_exists = AIMessage.objects.filter(
            conversation=conversation,
            role="assistant",
            created_at__gte=audit.created_at,
        ).exists()
        if later_assistant_exists:
            return

        AIMessage.objects.create(
            conversation=conversation,
            role="assistant",
            content=f"Error: {message}",
        )
