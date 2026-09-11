from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("deals", "0053_deal_raw_search_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="dealdocument",
            name="extraction_manifest",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="folderanalysisdocument",
            name="extraction_manifest",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
