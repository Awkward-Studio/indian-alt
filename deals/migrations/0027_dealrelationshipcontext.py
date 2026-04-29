import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_alter_profile_email_alter_profile_user'),
        ('deals', '0026_deal_bank_name_deal_primary_contact_name'),
    ]

    operations = [
        migrations.CreateModel(
            name='DealRelationshipContext',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('relationship_type', models.CharField(choices=[('competitor', 'Competitor'), ('sister_company', 'Sister Company'), ('parent_company', 'Parent Company'), ('subsidiary', 'Subsidiary'), ('comparable', 'Comparable'), ('customer', 'Customer'), ('vendor', 'Vendor'), ('other', 'Other')], default='comparable', max_length=40)),
                ('notes', models.TextField(blank=True, null=True)),
                ('selected_deal_ids', models.JSONField(blank=True, default=list)),
                ('selected_document_ids', models.JSONField(blank=True, default=list)),
                ('selected_chunk_ids', models.JSONField(blank=True, default=list)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='accounts.profile')),
                ('deal', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='relationship_contexts', to='deals.deal')),
                ('related_deal', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='related_to_contexts', to='deals.deal')),
            ],
            options={
                'db_table': 'deal_relationship_context',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='dealrelationshipcontext',
            index=models.Index(fields=['deal', 'relationship_type'], name='deal_relati_deal_id_b024ed_idx'),
        ),
        migrations.AddIndex(
            model_name='dealrelationshipcontext',
            index=models.Index(fields=['related_deal'], name='deal_relati_related_853581_idx'),
        ),
    ]
