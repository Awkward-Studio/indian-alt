from django.contrib.postgres.indexes import GinIndex
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('contacts', '0007_contact_name_trigram_index'),
        ('deals', '0051_financial_statement_provenance'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='deal',
            index=GinIndex(fields=['title'], name='deal_title_trgm', opclasses=['gin_trgm_ops']),
        ),
        migrations.AddIndex(
            model_name='deal',
            index=GinIndex(fields=['industry'], name='deal_industry_trgm', opclasses=['gin_trgm_ops']),
        ),
        migrations.AddIndex(
            model_name='deal',
            index=GinIndex(fields=['sector'], name='deal_sector_trgm', opclasses=['gin_trgm_ops']),
        ),
        migrations.AddIndex(
            model_name='deal',
            index=GinIndex(fields=['city'], name='deal_city_trgm', opclasses=['gin_trgm_ops']),
        ),
        migrations.AddIndex(
            model_name='deal',
            index=GinIndex(fields=['state'], name='deal_state_trgm', opclasses=['gin_trgm_ops']),
        ),
        migrations.AddIndex(
            model_name='deal',
            index=GinIndex(fields=['country'], name='deal_country_trgm', opclasses=['gin_trgm_ops']),
        ),
        migrations.AddIndex(
            model_name='deal',
            index=GinIndex(fields=['legacy_investment_bank'], name='deal_legacy_bank_trgm', opclasses=['gin_trgm_ops']),
        ),
    ]
