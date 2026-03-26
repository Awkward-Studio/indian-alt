from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("deals", "0014_remove_deal_ambiguities_remove_deal_analysis_history_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="dealdocument",
            name="initial_analysis_reason",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dealdocument",
            name="initial_analysis_status",
            field=models.CharField(
                choices=[
                    ("not_selected", "Not Selected"),
                    ("selected_and_analyzed", "Selected And Analyzed"),
                    ("selected_failed", "Selected Failed"),
                ],
                default="not_selected",
                help_text="Whether the document was selected for the initial folder analysis flow.",
                max_length=40,
            ),
        ),
    ]
