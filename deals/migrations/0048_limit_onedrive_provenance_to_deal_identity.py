from django.db import migrations


def limit_onedrive_provenance(apps, schema_editor):
    DealFieldProvenance = apps.get_model("deals", "DealFieldProvenance")
    DealFieldProvenance.objects.filter(source_type="ONEDRIVE").exclude(
        field_name__in=("title", "source_onedrive_id")
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("deals", "0047_remove_broad_onedrive_relationship_provenance"),
    ]

    operations = [
        migrations.RunPython(limit_onedrive_provenance, migrations.RunPython.noop),
    ]
