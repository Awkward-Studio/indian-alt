from django.db import migrations, models


def backfill_document_errors(apps, schema_editor):
    DealDocument = apps.get_model("deals", "DealDocument")
    AIAuditLog = apps.get_model("ai_orchestrator", "AIAuditLog")

    audits = AIAuditLog.objects.filter(source_type="vdr_indexing").order_by("-created_at")
    for audit in audits.iterator():
        metadata = audit.source_metadata if isinstance(audit.source_metadata, dict) else {}
        for item in metadata.get("document_queue") or []:
            error = str(item.get("error") or "").strip()
            if not error:
                continue
            queryset = DealDocument.objects.filter(deal_id=audit.source_id)
            if item.get("document_id"):
                queryset = queryset.filter(pk=item["document_id"])
            elif item.get("source_file_id"):
                queryset = queryset.filter(onedrive_id=item["source_file_id"])
            else:
                queryset = queryset.filter(title=item.get("name") or "")
            queryset.filter(error_message__isnull=True).update(error_message=error[:4000])


class Migration(migrations.Migration):
    dependencies = [
        ("deals", "0055_consolidate_deal_status"),
        ("ai_orchestrator", "0038_aiauditlog_token_breakdown"),
    ]

    operations = [
        migrations.AddField(
            model_name="dealdocument",
            name="error_message",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_document_errors, migrations.RunPython.noop),
    ]
