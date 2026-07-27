import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_alter_profile_email_alter_profile_user"),
        ("ai_orchestrator", "0030_aiauditlog_completed_at_aiauditlog_requested_by_and_more"),
        ("deals", "0036_deal_received_at"),
    ]

    operations = [
        migrations.CreateModel(
            name="DealContradiction",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("fingerprint", models.CharField(max_length=64)),
                ("subject", models.TextField()),
                ("metric", models.CharField(max_length=80)),
                ("period", models.CharField(blank=True, default="", max_length=80)),
                ("unit", models.CharField(blank=True, default="", max_length=80)),
                ("classification", models.CharField(db_index=True, max_length=40)),
                ("confidence", models.FloatField(default=0)),
                ("materiality", models.CharField(default="unknown", max_length=20)),
                ("rationale", models.TextField()),
                ("left_claim", models.JSONField(default=dict)),
                ("right_claim", models.JSONField(default=dict)),
                ("classifier_version", models.CharField(default="1", max_length=40)),
                ("model_used", models.CharField(blank=True, default="", max_length=200)),
                (
                    "review_status",
                    models.CharField(
                        choices=[
                            ("UNREVIEWED", "Unreviewed"),
                            ("CONFIRMED", "Confirmed"),
                            ("DISMISSED", "Dismissed"),
                        ],
                        db_index=True,
                        default="UNREVIEWED",
                        max_length=20,
                    ),
                ),
                ("analyst_comment", models.TextField(blank=True, default="")),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("detected_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "audit_log",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="detected_contradictions",
                        to="ai_orchestrator.aiauditlog",
                    ),
                ),
                (
                    "deal",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="contradictions",
                        to="deals.deal",
                    ),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="reviewed_contradictions",
                        to="accounts.profile",
                    ),
                ),
            ],
            options={
                "db_table": "deal_contradiction",
                "ordering": ["-detected_at", "-updated_at"],
                "indexes": [
                    models.Index(fields=["deal", "review_status"], name="deal_contra_deal_id_e1e51d_idx"),
                    models.Index(fields=["deal", "classification"], name="deal_contra_deal_id_a8b876_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("deal", "fingerprint"),
                        name="unique_deal_contradiction_fingerprint",
                    ),
                ],
            },
        ),
    ]
