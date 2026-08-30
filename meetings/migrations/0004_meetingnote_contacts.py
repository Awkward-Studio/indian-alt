from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('contacts', '0005_contact_contact_type'), ('meetings', '0003_meetingsignalflag')]
    operations = [migrations.AddField(model_name='meetingnote', name='contacts', field=models.ManyToManyField(blank=True, help_text='Contacts who attended or were discussed in this meeting note', related_name='meeting_notes', to='contacts.contact'))]
