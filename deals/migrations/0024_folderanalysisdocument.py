from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("ai_orchestrator", "0019_documentchunk_analysis_document_support"),
        ("deals", "0023_dealdocument_document_artifacts"),
    ]

    operations = [
        migrations.CreateModel(
            name="FolderAnalysisDocument",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("source_file_id", models.CharField(db_index=True, max_length=255)),
                ("source_drive_id", models.CharField(blank=True, default="", max_length=255)),
                ("file_name", models.TextField()),
                ("file_path", models.TextField(blank=True, default="")),
                ("document_type", models.CharField(choices=[("Pitch Deck", "Pitch Deck"), ("Financials", "Financials"), ("Legal", "Legal"), ("Term Sheet", "Term Sheet"), ("KYC", "KYC"), ("Memo", "Memo"), ("Other", "Other")], default="Other", max_length=50)),
                ("raw_extracted_text", models.TextField(blank=True, default="")),
                ("normalized_text", models.TextField(blank=True, default="")),
                ("evidence_json", models.JSONField(blank=True, default=dict)),
                ("source_map_json", models.JSONField(blank=True, default=dict)),
                ("table_json", models.JSONField(blank=True, default=list)),
                ("key_metrics_json", models.JSONField(blank=True, default=list)),
                ("reasoning", models.TextField(blank=True, null=True)),
                ("extraction_mode", models.CharField(blank=True, choices=[("docproc_remote", "Docproc Remote"), ("vllm_vision", "vLLM Vision"), ("fallback_text", "Fallback Text")], max_length=40, null=True)),
                ("transcription_status", models.CharField(choices=[("pending", "Pending"), ("partial", "Partial"), ("complete", "Complete"), ("failed", "Failed")], default="pending", max_length=20)),
                ("chunking_status", models.CharField(choices=[("not_chunked", "Not Chunked"), ("chunked", "Chunked"), ("failed", "Failed")], default="not_chunked", max_length=20)),
                ("quality_flags", models.JSONField(blank=True, default=list)),
                ("render_metadata", models.JSONField(blank=True, default=dict)),
                ("is_indexed", models.BooleanField(default=False)),
                ("chunk_count", models.PositiveIntegerField(default=0)),
                ("error_message", models.TextField(blank=True, null=True)),
                ("last_transcribed_at", models.DateTimeField(blank=True, null=True)),
                ("last_chunked_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("audit_log", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="analysis_documents", to="ai_orchestrator.aiauditlog")),
            ],
            options={
                "ordering": ["-updated_at", "-created_at"],
                "unique_together": {("audit_log", "source_file_id")},
            },
        ),
        migrations.AddIndex(
            model_name="folderanalysisdocument",
            index=models.Index(fields=["audit_log", "source_file_id"], name="deals_fold_audit_l_02e3e4_idx"),
        ),
        migrations.AddIndex(
            model_name="folderanalysisdocument",
            index=models.Index(fields=["audit_log", "transcription_status"], name="deals_fold_audit_l_7a0f89_idx"),
        ),
        migrations.AddIndex(
            model_name="folderanalysisdocument",
            index=models.Index(fields=["audit_log", "is_indexed"], name="deals_fold_audit_l_70d2de_idx"),
        ),
    ]
