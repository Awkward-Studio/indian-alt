from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("ai_orchestrator", "0014_aiauditlog_context_label"),
    ]

    operations = [
        migrations.CreateModel(
            name="AIFlowDefinition",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("key", models.CharField(max_length=100, unique=True)),
                ("name", models.CharField(max_length=150)),
                ("description", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="AIFlowVersion",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("version", models.IntegerField(default=1)),
                ("status", models.CharField(choices=[("draft", "Draft"), ("published", "Published"), ("archived", "Archived")], default="draft", max_length=20)),
                ("config", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("flow", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="versions", to="ai_orchestrator.aiflowdefinition")),
            ],
            options={
                "ordering": ["-version", "-updated_at"],
                "indexes": [models.Index(fields=["flow", "status"], name="ai_orchestr_flow_id_e2f9b2_idx")],
                "unique_together": {("flow", "version")},
            },
        ),
    ]
