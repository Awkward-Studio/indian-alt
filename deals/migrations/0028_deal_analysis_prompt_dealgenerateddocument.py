import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_alter_profile_email_alter_profile_user'),
        ('deals', '0027_dealrelationshipcontext'),
    ]

    operations = [
        migrations.AddField(
            model_name='deal',
            name='analysis_prompt',
            field=models.TextField(blank=True, help_text='Deal-specific analysis directive appended to the AI personality for full rewrites and analysis runs.', null=True),
        ),
        migrations.CreateModel(
            name='DealGeneratedDocument',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('title', models.CharField(max_length=255)),
                ('kind', models.CharField(choices=[('directive', 'Directive Document'), ('ic_note', 'IC Note'), ('financial_model', 'Financial Model'), ('diligence_memo', 'Diligence Memo'), ('risk_register', 'Risk Register'), ('other', 'Other')], default='directive', max_length=40)),
                ('directive', models.TextField()),
                ('content', models.TextField(blank=True, null=True)),
                ('selected_deal_ids', models.JSONField(blank=True, default=list)),
                ('selected_document_ids', models.JSONField(blank=True, default=list)),
                ('selected_chunk_ids', models.JSONField(blank=True, default=list)),
                ('audit_log_id', models.CharField(blank=True, max_length=255, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='accounts.profile')),
                ('deal', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='generated_documents', to='deals.deal')),
            ],
            options={
                'db_table': 'deal_generated_document',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='dealgenerateddocument',
            index=models.Index(fields=['deal', 'kind'], name='deal_genera_deal_id_33da04_idx'),
        ),
    ]
