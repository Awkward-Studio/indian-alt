from django.db import models

from deals.models import DealFieldProvenance


TRACKED_DEAL_FIELDS = {
    'title', 'received_at', 'bank', 'bank_name', 'legacy_investment_bank',
    'primary_contact', 'primary_contact_name', 'deal_status',
    'current_phase', 'priority', 'fund', 'responsibility', 'sector',
    'industry', 'city', 'funding_ask', 'funding_ask_for', 'is_female_led',
    'source_onedrive_id',
}


def serializable_field_value(value):
    if isinstance(value, models.Model):
        return str(value.pk)
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    if isinstance(value, (list, tuple, set)):
        return [serializable_field_value(item) for item in value]
    return value


def record_deal_field_changes(deal, changes, *, source_type, source_id='', changed_by=None):
    records = []
    for field_name, values in changes.items():
        if field_name not in TRACKED_DEAL_FIELDS:
            continue
        previous_value, value = values
        previous_value = serializable_field_value(previous_value)
        value = serializable_field_value(value)
        if previous_value == value:
            continue
        records.append(DealFieldProvenance(
            deal=deal,
            field_name=field_name,
            source_type=source_type,
            source_id=source_id,
            previous_value=previous_value,
            value=value,
            changed_by=changed_by if getattr(changed_by, 'is_authenticated', False) else None,
        ))
    if records:
        DealFieldProvenance.objects.bulk_create(records)
    return records
