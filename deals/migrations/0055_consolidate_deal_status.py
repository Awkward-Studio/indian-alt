import re

from django.db import migrations, models


CANONICAL_STATUSES = [
    "New",
    "Interesting",
    "Semi Interesting",
    "To Pass",
    "Passed",
    "Portfolio",
]

ALIASES = {
    "new": "New",
    "to pass": "To Pass",
    "to be passed": "To Pass",
    "to be pass": "To Pass",
    "passed": "Passed",
    "invested": "Portfolio",
    "portfolio": "Portfolio",
    "interesting": "Interesting",
    "semi interesting": "Semi Interesting",
    "semi-interesting": "Semi Interesting",
}


def canonical_status(value):
    normalized = " ".join(str(value or "").split())
    alias = ALIASES.get(normalized.casefold())
    if alias:
        return alias
    match = re.fullmatch(r"(\d+):\s*.+", normalized)
    if match:
        return "New" if int(match.group(1)) == 1 else "Interesting"
    return None


def reconcile_statuses(apps, schema_editor):
    Deal = apps.get_model("deals", "Deal")
    invalid = []
    updates = []
    for deal in Deal.objects.all().only("id", "deal_status", "current_phase").iterator():
        source = deal.deal_status or deal.current_phase
        status = canonical_status(source)
        if status is None:
            invalid.append((str(deal.id), source))
            continue
        if deal.deal_status != status:
            deal.deal_status = status
            updates.append(deal)

    if invalid:
        sample = ", ".join(f"{deal_id}={value!r}" for deal_id, value in invalid[:10])
        raise RuntimeError(f"Cannot reconcile {len(invalid)} deal statuses: {sample}")
    if updates:
        Deal.objects.bulk_update(updates, ["deal_status"], batch_size=1000)


class Migration(migrations.Migration):
    dependencies = [("deals", "0054_document_extraction_manifests")]

    operations = [
        migrations.RunPython(reconcile_statuses, migrations.RunPython.noop),
        migrations.RemoveField(model_name="deal", name="current_phase"),
        migrations.RemoveField(model_name="deal", name="deal_flow_decisions"),
        migrations.RemoveField(model_name="deal", name="rejection_stage_id"),
        migrations.DeleteModel(name="DealPhaseLog"),
        migrations.AlterField(
            model_name="deal",
            name="deal_status",
            field=models.CharField(
                choices=[(status, status) for status in CANONICAL_STATUSES],
                db_column="deal_status",
                default="New",
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="deal",
            constraint=models.CheckConstraint(
                condition=models.Q(deal_status__in=CANONICAL_STATUSES),
                name="deal_status_is_canonical",
            ),
        ),
    ]
