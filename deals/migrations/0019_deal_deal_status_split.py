from django.db import migrations, models


def split_priority_into_status_and_priority(apps, schema_editor):
    Deal = apps.get_model("deals", "Deal")
    urgency_values = {"High", "Medium", "Low"}
    status_values = {"New", "To be Passed", "To Be Pass", "Passed", "Portfolio", "Invested"}

    for deal in Deal.objects.all().only("id", "priority", "deal_status"):
        value = deal.priority
        if value in urgency_values:
            deal.deal_status = deal.deal_status or "New"
            if not deal.priority:
                deal.priority = "Medium"
        elif value in status_values:
            deal.deal_status = value
            deal.priority = "Medium"
        else:
            deal.deal_status = deal.deal_status or "New"
            deal.priority = deal.priority or "Medium"
        deal.save(update_fields=["deal_status", "priority"])


class Migration(migrations.Migration):

    dependencies = [
        ("deals", "0018_deal_source_email_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="deal",
            name="deal_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("New", "New"),
                    ("To be Passed", "To be Passed"),
                    ("To Be Pass", "To Be Pass"),
                    ("Passed", "Passed"),
                    ("Portfolio", "Portfolio"),
                    ("Invested", "Invested"),
                ],
                db_column="deal_status",
                default="New",
                max_length=20,
                null=True,
            ),
        ),
        migrations.RunPython(split_priority_into_status_and_priority, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="deal",
            name="priority",
            field=models.CharField(
                blank=True,
                choices=[("High", "High"), ("Medium", "Medium"), ("Low", "Low")],
                db_column="priority",
                default="Medium",
                max_length=20,
                null=True,
            ),
        ),
        migrations.AddIndex(
            model_name="deal",
            index=models.Index(fields=["deal_status"], name="deal_deal_st_c54847_idx"),
        ),
    ]
