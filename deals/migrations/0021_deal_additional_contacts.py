from django.db import migrations, models


def backfill_additional_contacts(apps, schema_editor):
    Deal = apps.get_model("deals", "Deal")
    Contact = apps.get_model("contacts", "Contact")

    for deal in Deal.objects.all().only("id", "other_contacts", "primary_contact"):
        ids = [str(value).strip() for value in (deal.other_contacts or []) if str(value).strip()]
        if deal.primary_contact_id:
            ids = [value for value in ids if value != str(deal.primary_contact_id)]
        contacts = list(Contact.objects.filter(id__in=ids))
        if contacts:
            deal.additional_contacts.set(contacts)


class Migration(migrations.Migration):

    dependencies = [
        ("contacts", "0003_contact_follow_ups_contact_last_meeting_date_and_more"),
        ("deals", "0020_sync_deal_status_with_current_phase"),
    ]

    operations = [
        migrations.AddField(
            model_name="deal",
            name="additional_contacts",
            field=models.ManyToManyField(
                blank=True,
                help_text="Additional contacts linked to this deal beyond the primary contact",
                related_name="additional_deals",
                to="contacts.contact",
            ),
        ),
        migrations.RunPython(backfill_additional_contacts, migrations.RunPython.noop),
    ]
