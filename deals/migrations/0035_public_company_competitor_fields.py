# Generated manually for public-company competitor enrichment.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('deals', '0034_deal_competitor_candidates'),
    ]

    operations = [
        migrations.AddField(
            model_name='ventureintelligencecompanyprofile',
            name='company_type',
            field=models.CharField(db_index=True, default='private', max_length=40),
        ),
        migrations.AddField(
            model_name='ventureintelligencecompanyprofile',
            name='data_source',
            field=models.CharField(db_index=True, default='venture_intelligence', max_length=40),
        ),
        migrations.AddField(
            model_name='ventureintelligencecompanyprofile',
            name='exchange',
            field=models.CharField(blank=True, max_length=40, null=True),
        ),
        migrations.AddField(
            model_name='ventureintelligencecompanyprofile',
            name='market_cap',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='ventureintelligencecompanyprofile',
            name='public_market_snapshot',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='ventureintelligencecompanyprofile',
            name='screener_url',
            field=models.URLField(blank=True, max_length=500, null=True),
        ),
        migrations.AddField(
            model_name='ventureintelligencecompanyprofile',
            name='ticker',
            field=models.CharField(blank=True, db_index=True, max_length=40, null=True),
        ),
        migrations.AlterField(
            model_name='ventureintelligencefinancialstatement',
            name='statement_type',
            field=models.CharField(choices=[('profit_loss', 'Profit & Loss'), ('balance_sheet', 'Balance Sheet'), ('cash_flow', 'Cash Flow'), ('screener_annual', 'Screener Annual'), ('screener_quarterly', 'Screener Quarterly')], max_length=40),
        ),
    ]
