from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ai_orchestrator", "0037_alter_aiauditlog_celery_task_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="aiauditlog",
            name="input_tokens",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="aiauditlog",
            name="output_tokens",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="aiauditlog",
            name="token_count_is_estimate",
            field=models.BooleanField(blank=True, null=True),
        ),
    ]
