import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("accounts", "0002_alter_profile_email_alter_profile_user"),
        ("deals", "0035_public_company_competitor_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="Task",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("title", models.TextField()),
                ("description", models.TextField(blank=True, default="")),
                ("status", models.CharField(choices=[("todo", "To Do"), ("in_progress", "In Progress"), ("blocked", "Blocked"), ("done", "Done")], default="todo", max_length=20)),
                ("priority", models.CharField(choices=[("low", "Low"), ("medium", "Medium"), ("high", "High")], default="medium", max_length=20)),
                ("due_date", models.DateField(blank=True, null=True)),
                ("origin", models.CharField(choices=[("manual", "Manual"), ("analysis", "Analysis")], default="manual", max_length=20)),
                ("fingerprint", models.CharField(blank=True, db_index=True, default="", max_length=64)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("assignee", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="assigned_tasks", to="accounts.profile")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_tasks", to="accounts.profile")),
                ("deal", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="tasks", to="deals.deal")),
            ],
            options={"db_table": "task", "ordering": ["due_date", "-created_at"]},
        ),
        migrations.CreateModel(
            name="TaskSuggestion",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("analysis_version", models.IntegerField(blank=True, null=True)),
                ("report_hash", models.CharField(max_length=64)),
                ("fingerprint", models.CharField(max_length=64)),
                ("category", models.TextField(blank=True, default="")),
                ("title", models.TextField()),
                ("source_section", models.TextField(blank=True, default="")),
                ("source_table_kind", models.CharField(blank=True, default="", max_length=40)),
                ("source_owner", models.TextField(blank=True, default="")),
                ("source_assignee", models.TextField(blank=True, default="")),
                ("source_status", models.TextField(blank=True, default="")),
                ("source_priority", models.TextField(blank=True, default="")),
                ("source_references", models.JSONField(blank=True, default=list)),
                ("state", models.CharField(choices=[("pending", "Pending"), ("accepted", "Accepted"), ("dismissed", "Dismissed"), ("superseded", "Superseded")], default="pending", max_length=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("analysis", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="task_suggestions", to="deals.dealanalysis")),
                ("deal", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="task_suggestions", to="deals.deal")),
                ("task", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="source_suggestions", to="work_items.task")),
            ],
            options={"db_table": "task_suggestion", "ordering": ["deal__title", "source_section", "created_at"]},
        ),
        migrations.AddIndex(model_name="task", index=models.Index(fields=["deal", "status"], name="task_deal_id_status_idx")),
        migrations.AddIndex(model_name="task", index=models.Index(fields=["assignee", "status"], name="task_assignee_status_idx")),
        migrations.AddIndex(model_name="task", index=models.Index(fields=["status", "due_date"], name="task_status_due_idx")),
        migrations.AddConstraint(model_name="tasksuggestion", constraint=models.UniqueConstraint(fields=("deal", "report_hash", "fingerprint"), name="unique_task_suggestion_revision")),
        migrations.AddIndex(model_name="tasksuggestion", index=models.Index(fields=["deal", "state"], name="task_sugg_deal_state_idx")),
        migrations.AddIndex(model_name="tasksuggestion", index=models.Index(fields=["analysis", "state"], name="task_sugg_analysis_state_idx")),
        migrations.AddIndex(model_name="tasksuggestion", index=models.Index(fields=["report_hash", "state"], name="task_sugg_report_state_idx")),
    ]
