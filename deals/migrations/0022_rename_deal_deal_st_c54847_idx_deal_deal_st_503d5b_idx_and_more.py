from django.db import migrations, models


FLOW_PHASE_CHOICES = [
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
    ("Origination", "Origination"),
    ("Screening", "Screening"),
    ("Management Meeting", "Management Meeting"),
    ("Due Diligence", "Due Diligence"),
    ("IC Approval", "IC Approval"),
    ("Term Sheet", "Term Sheet"),
    ("Execution", "Execution"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("deals", "0021_deal_additional_contacts"),
    ]

    operations = [
        migrations.RenameIndex(
            model_name="deal",
            new_name="deal_deal_st_503d5b_idx",
            old_name="deal_deal_st_c54847_idx",
        ),
        migrations.AlterField(
            model_name="dealphaselog",
            name="from_phase",
            field=models.CharField(choices=FLOW_PHASE_CHOICES, max_length=50, null=True),
        ),
        migrations.AlterField(
            model_name="dealphaselog",
            name="to_phase",
            field=models.CharField(choices=FLOW_PHASE_CHOICES, max_length=50),
        ),
    ]
