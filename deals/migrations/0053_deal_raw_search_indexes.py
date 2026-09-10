from django.contrib.postgres.indexes import GinIndex
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('deals', '0052_deal_ledger_search_indexes'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='deal',
            index=GinIndex(fields=['bank_name'], name='deal_bank_name_trgm', opclasses=['gin_trgm_ops']),
        ),
        migrations.AddIndex(
            model_name='deal',
            index=GinIndex(fields=['primary_contact_name'], name='deal_contact_name_trgm', opclasses=['gin_trgm_ops']),
        ),
    ]
