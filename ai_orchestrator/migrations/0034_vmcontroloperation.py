import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ai_orchestrator", "0033_remove_aipersonality_vision_model_name"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="VMControlOperation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("action", models.CharField(choices=[("start", "Start"), ("deallocate", "Deallocate")], max_length=20)),
                ("status", models.CharField(choices=[("submitted", "Submitted"), ("succeeded", "Succeeded"), ("failed", "Failed")], default="submitted", max_length=20)),
                ("target_label", models.CharField(max_length=200)),
                ("target_vm_name", models.CharField(max_length=200)),
                ("target_resource_id", models.CharField(max_length=600)),
                ("provider_request_id", models.CharField(blank=True, default="", max_length=200)),
                ("requested_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("failure_reason", models.CharField(blank=True, default="", max_length=255)),
                ("requested_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="vm_control_operations", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-requested_at"]},
        ),
        migrations.AddIndex(
            model_name="vmcontroloperation",
            index=models.Index(fields=["status", "requested_at"], name="ai_orchestr_status_8ee0c2_idx"),
        ),
    ]
