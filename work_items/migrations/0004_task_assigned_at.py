from django.db import migrations, models


def backfill_assignment_dates(apps, schema_editor):
    Task = apps.get_model("work_items", "Task")
    Activity = apps.get_model("work_items", "TaskActivity")
    for task in Task.objects.exclude(assignee_id=None).iterator():
        assignment = Activity.objects.filter(task_id_snapshot=task.id, action="assigned").order_by("-created_at").first()
        Task.objects.filter(pk=task.pk).update(assigned_at=assignment.created_at if assignment else task.created_at)


class Migration(migrations.Migration):
    dependencies = [("work_items", "0003_task_workflow")]
    operations = [
        migrations.AddField(model_name="task", name="assigned_at", field=models.DateTimeField(blank=True, editable=False, null=True)),
        migrations.RunPython(backfill_assignment_dates, migrations.RunPython.noop),
    ]
