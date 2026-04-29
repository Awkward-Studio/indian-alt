from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ai_orchestrator', '0023_aiskill_system_template'),
    ]

    operations = [
        migrations.AddField(
            model_name='aiconversation',
            name='metadata',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
