import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('deals', '0043_multimodal_document_extraction_mode'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='DealFieldProvenance',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('field_name', models.CharField(max_length=100)),
                ('source_type', models.CharField(choices=[('AI', 'AI'), ('SHEET', 'Spreadsheet'), ('HUMAN', 'Human')], max_length=10)),
                ('source_id', models.CharField(blank=True, default='', max_length=500)),
                ('previous_value', models.JSONField(blank=True, null=True)),
                ('value', models.JSONField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('changed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='deal_field_changes', to=settings.AUTH_USER_MODEL)),
                ('deal', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='field_provenance', to='deals.deal')),
            ],
            options={
                'ordering': ['-created_at', '-id'],
                'indexes': [
                    models.Index(fields=['deal', 'field_name', '-created_at'], name='deals_dealf_deal_id_229bea_idx'),
                    models.Index(fields=['source_type', '-created_at'], name='deals_dealf_source__ad70c1_idx'),
                ],
            },
        ),
    ]
