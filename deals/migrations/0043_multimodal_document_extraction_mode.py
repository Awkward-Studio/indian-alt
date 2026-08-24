from django.db import migrations, models


def rename_vision_mode(apps, schema_editor):
    for model_name in ("DealDocument", "FolderAnalysisDocument"):
        model = apps.get_model("deals", model_name)
        model.objects.filter(extraction_mode="vllm_vision").update(
            extraction_mode="multimodal_model"
        )


def restore_vision_mode(apps, schema_editor):
    for model_name in ("DealDocument", "FolderAnalysisDocument"):
        model = apps.get_model("deals", model_name)
        model.objects.filter(extraction_mode="multimodal_model").update(
            extraction_mode="vllm_vision"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("deals", "0042_sectorresearchacquisition"),
    ]

    operations = [
        migrations.RunPython(rename_vision_mode, restore_vision_mode),
        migrations.AlterField(
            model_name="dealdocument",
            name="extraction_mode",
            field=models.CharField(
                blank=True,
                choices=[
                    ("docproc_remote", "Docproc Remote"),
                    ("multimodal_model", "Multimodal Model"),
                    ("fallback_text", "Fallback Text"),
                ],
                max_length=40,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="folderanalysisdocument",
            name="extraction_mode",
            field=models.CharField(
                blank=True,
                choices=[
                    ("docproc_remote", "Docproc Remote"),
                    ("multimodal_model", "Multimodal Model"),
                    ("fallback_text", "Fallback Text"),
                ],
                max_length=40,
                null=True,
            ),
        ),
    ]
