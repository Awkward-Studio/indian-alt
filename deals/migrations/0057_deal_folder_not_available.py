from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("deals", "0056_dealdocument_error_message"),
    ]

    operations = [
        migrations.AddField(
            model_name="deal",
            name="folder_not_available",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text="Whether the deal has been confirmed not to have a OneDrive folder",
            ),
        ),
        migrations.AddField(
            model_name="deal",
            name="folder_file_count",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="deal",
            name="folder_last_scanned_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="deal",
            name="folder_readable_file_count",
            field=models.PositiveIntegerField(blank=True, db_index=True, null=True),
        ),
    ]
