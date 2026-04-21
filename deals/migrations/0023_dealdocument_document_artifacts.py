from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("deals", "0022_rename_deal_deal_st_c54847_idx_deal_deal_st_503d5b_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="dealdocument",
            name="evidence_json",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="dealdocument",
            name="key_metrics_json",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="dealdocument",
            name="normalized_text",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dealdocument",
            name="reasoning",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dealdocument",
            name="source_map_json",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="dealdocument",
            name="table_json",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
