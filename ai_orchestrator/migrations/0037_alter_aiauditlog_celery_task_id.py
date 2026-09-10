from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('ai_orchestrator', '0036_aipipelinestage_depends_on'),
    ]

    operations = [
        migrations.AlterField(
            model_name='aiauditlog',
            name='celery_task_id',
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text='ID of the associated Celery task',
                max_length=255,
                null=True,
            ),
        ),
    ]
