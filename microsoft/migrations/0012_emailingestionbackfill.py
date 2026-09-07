import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('microsoft', '0011_email_decision_prompts')]
    operations = [
        migrations.CreateModel(
            name='EmailIngestionBackfill',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('key', models.CharField(max_length=120, unique=True)),
                ('filters', models.JSONField(default=dict)),
                ('cursor', models.UUIDField(blank=True, null=True)),
                ('stats', models.JSONField(default=dict)),
                ('completed', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
