from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("ai_orchestrator", "0018_align_runtime_to_ollama_only"),
    ]

    operations = [
        migrations.AlterField(
            model_name="documentchunk",
            name="deal",
            field=models.ForeignKey(
                blank=True,
                help_text="The deal this chunk belongs to",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="chunks",
                to="deals.deal",
            ),
        ),
        migrations.AddField(
            model_name="documentchunk",
            name="audit_log",
            field=models.ForeignKey(
                blank=True,
                help_text="Initial analysis run this chunk belongs to before a deal exists",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="chunks",
                to="ai_orchestrator.aiauditlog",
            ),
        ),
        migrations.AlterField(
            model_name="documentchunk",
            name="source_type",
            field=models.CharField(
                choices=[
                    ("email", "Email Body"),
                    ("attachment", "Email Attachment"),
                    ("onedrive", "OneDrive File"),
                    ("deal_summary", "Deal Summary"),
                    ("document", "Deal Document Artifact"),
                    ("analysis_document", "Folder Analysis Document Artifact"),
                    ("ai_thinking", "AI Reasoning Logic"),
                    ("ai_ambiguities", "AI Identified Ambiguities"),
                    ("extracted_source", "Raw Extracted Text"),
                ],
                max_length=50,
            ),
        ),
        migrations.AddIndex(
            model_name="documentchunk",
            index=models.Index(fields=["audit_log", "source_type"], name="ai_orchestr_audit_l_53a486_idx"),
        ),
    ]
