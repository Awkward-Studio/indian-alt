from django.db import migrations, models


CANONICAL_DEAL_STAGES = [
    (f"{number}: {title}", f"{number}: {title}")
    for number, title in enumerate(
        [
            "Deal Sourced", "Initial Banker Call", "NDA Execution",
            "Initial Materials Review", "Financial Model Call",
            "Additional Data Request", "Industry Research", "Reference Calls",
            "IA Model Build", "Field Visit", "Business Proposal", "Term Sheet",
            "Full Due Diligence", "IC Note I", "IC Feedback", "IC Note II",
            "Definitive Documentation", "Closure",
        ],
        start=1,
    )
] + [("Passed", "Passed"), ("Invested", "Invested"), ("Portfolio", "Portfolio")]


class Migration(migrations.Migration):
    dependencies = [("deals", "0044_dealfieldprovenance")]

    operations = [
        migrations.AlterField(
            model_name="deal",
            name="current_phase",
            field=models.CharField(
                choices=CANONICAL_DEAL_STAGES,
                default="1: Deal Sourced",
                max_length=50,
            ),
        ),
        migrations.AlterField(
            model_name="dealphaselog",
            name="from_phase",
            field=models.CharField(choices=CANONICAL_DEAL_STAGES, max_length=50, null=True),
        ),
        migrations.AlterField(
            model_name="dealphaselog",
            name="to_phase",
            field=models.CharField(choices=CANONICAL_DEAL_STAGES, max_length=50),
        ),
    ]
