import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ai_orchestrator", "0037_agent_skill_package_fields"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AISkillLifecycleEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("action", models.CharField(max_length=30)),
                ("package_digest", models.CharField(blank=True, default="", max_length=64)),
                ("validation_report", models.JSONField(blank=True, default=dict)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ("revision", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="lifecycle_events", to="ai_orchestrator.aiskillrevision")),
                ("skill", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lifecycle_events", to="ai_orchestrator.aiskill")),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
