from django.db import migrations


RELATIONSHIP_FIELDS = (
    "bank",
    "bank_name",
    "legacy_investment_bank",
    "primary_contact",
    "primary_contact_name",
)


def remove_broad_relationship_provenance(apps, schema_editor):
    DealFieldProvenance = apps.get_model("deals", "DealFieldProvenance")
    DealFieldProvenance.objects.filter(
        source_type="ONEDRIVE",
        field_name__in=RELATIONSHIP_FIELDS,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("deals", "0046_add_onedrive_provenance_source")]

    operations = [
        migrations.RunPython(
            remove_broad_relationship_provenance,
            migrations.RunPython.noop,
        ),
    ]
