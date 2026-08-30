from django.db import migrations, models


def classify_contacts(apps, schema_editor):
    Contact = apps.get_model('contacts', 'Contact')
    for contact in Contact.objects.all().iterator():
        designation = (contact.designation or '').lower()
        if contact.bank_id:
            contact_type = 'BANKER'
        elif any(word in designation for word in ('consultant', 'advisor', 'adviser')):
            contact_type = 'CONSULTANT'
        elif any(word in designation for word in ('investor', 'investment', 'venture', 'private equity', 'fund', 'partner')):
            contact_type = 'INVESTOR'
        else:
            contact_type = 'OTHER'
        Contact.objects.filter(pk=contact.pk).update(contact_type=contact_type)


class Migration(migrations.Migration):
    dependencies = [('contacts', '0004_workplaceverificationsuggestion')]

    operations = [
        migrations.AddField(
            model_name='contact',
            name='contact_type',
            field=models.CharField(
                choices=[('BANKER', 'Banker'), ('CONSULTANT', 'Consultant'), ('INVESTOR', 'Investor'), ('OTHER', 'Other')],
                db_index=True,
                default='OTHER',
                max_length=20,
            ),
        ),
        migrations.RunPython(classify_contacts, migrations.RunPython.noop),
    ]
