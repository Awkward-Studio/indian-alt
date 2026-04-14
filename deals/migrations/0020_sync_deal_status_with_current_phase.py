from django.db import migrations, models


FLOW_STATUSES = [
    ("1: Deal Sourced", "1: Deal Sourced"),
    ("2: Initial Banker Call", "2: Initial Banker Call"),
    ("3: NDA Execution", "3: NDA Execution"),
    ("4: Initial Materials Review", "4: Initial Materials Review"),
    ("5: Financial Model Call", "5: Financial Model Call"),
    ("6: Additional Data Request", "6: Additional Data Request"),
    ("7: Industry Research", "7: Industry Research"),
    ("8: Reference Calls", "8: Reference Calls"),
    ("9: IA Model Build", "9: IA Model Build"),
    ("10: Field Visit", "10: Field Visit"),
    ("11: Business Proposal", "11: Business Proposal"),
    ("12: Term Sheet", "12: Term Sheet"),
    ("13: Full Due Diligence", "13: Full Due Diligence"),
    ("14: IC Note I", "14: IC Note I"),
    ("15: IC Feedback", "15: IC Feedback"),
    ("16: IC Note II", "16: IC Note II"),
    ("17: Definitive Documentation", "17: Definitive Documentation"),
    ("18: Closure", "18: Closure"),
    ("Passed", "Passed"),
    ("Invested", "Invested"),
    ("Portfolio", "Portfolio"),
]


def sync_deal_status_and_phase(apps, schema_editor):
    Deal = apps.get_model("deals", "Deal")

    for deal in Deal.objects.all().only("id", "deal_status", "current_phase"):
        synced_status = deal.current_phase or deal.deal_status or "1: Deal Sourced"
        deal.deal_status = synced_status
        deal.current_phase = synced_status
        deal.save(update_fields=["deal_status", "current_phase"])


class Migration(migrations.Migration):

    dependencies = [
        ("deals", "0019_deal_deal_status_split"),
    ]

    operations = [
        migrations.AlterField(
            model_name="deal",
            name="deal_status",
            field=models.CharField(
                blank=True,
                choices=FLOW_STATUSES,
                db_column="deal_status",
                default="1: Deal Sourced",
                max_length=50,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="deal",
            name="current_phase",
            field=models.CharField(
                choices=[
                    *FLOW_STATUSES,
                    ("Origination", "Origination"),
                    ("Screening", "Screening"),
                    ("Management Meeting", "Management Meeting"),
                    ("Due Diligence", "Due Diligence"),
                    ("IC Approval", "IC Approval"),
                    ("Term Sheet", "Term Sheet"),
                    ("Execution", "Execution"),
                ],
                default="1: Deal Sourced",
                max_length=50,
            ),
        ),
        migrations.RunPython(sync_deal_status_and_phase, migrations.RunPython.noop),
    ]
