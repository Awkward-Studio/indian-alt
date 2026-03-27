from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("deals", "0015_dealdocument_initial_analysis_status_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="dealdocument",
            name="chunking_status",
            field=models.CharField(
                choices=[
                    ("not_chunked", "Not Chunked"),
                    ("chunked", "Chunked"),
                    ("failed", "Failed"),
                ],
                default="not_chunked",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="dealdocument",
            name="extraction_mode",
            field=models.CharField(
                blank=True,
                choices=[
                    ("glm_ocr", "GLM OCR"),
                    ("fallback_text", "Fallback Text"),
                ],
                max_length=40,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="dealdocument",
            name="last_chunked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dealdocument",
            name="last_transcribed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dealdocument",
            name="transcription_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("partial", "Partial"),
                    ("complete", "Complete"),
                    ("failed", "Failed"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]
