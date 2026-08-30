from __future__ import annotations

from django.db.models import Count, DateField, F, Max, Q, QuerySet
from django.db.models.functions import Cast, Coalesce, Greatest

from banks.models import Bank
from contacts.models import Contact
from deals.models import Deal, DealStatus


CONVERTED_STATUSES = (DealStatus.INVESTED, DealStatus.PORTFOLIO)
INACTIVE_STATUSES = (DealStatus.PASSED, *CONVERTED_STATUSES)
IC_STATUSES = (
    DealStatus.STAGE_14,
    DealStatus.STAGE_15,
    DealStatus.STAGE_16,
)


def _effective_deal_date(prefix: str):
    return Coalesce(
        F(f"{prefix}received_at"),
        Cast(F(f"{prefix}created_at"), output_field=DateField()),
        output_field=DateField(),
    )


def banker_analytics_queryset() -> QuerySet[Contact]:
    """Return sourcing metrics where only the primary banker receives credit."""
    return (
        Contact.objects.select_related("bank")
        .annotate(
            total_deals_introduced=Count("primary_deals", distinct=True),
            active_mandates=Count(
                "primary_deals",
                filter=~Q(primary_deals__deal_status__in=INACTIVE_STATUSES),
                distinct=True,
            ),
            sourced_mandates=Count(
                "primary_deals",
                filter=(
                    ~Q(primary_deals__deal_status__in=INACTIVE_STATUSES)
                    & ~Q(primary_deals__deal_status__in=IC_STATUSES)
                ),
                distinct=True,
            ),
            ic_mandates=Count(
                "primary_deals",
                filter=Q(primary_deals__deal_status__in=IC_STATUSES),
                distinct=True,
            ),
            converted_deals=Count(
                "primary_deals",
                filter=Q(primary_deals__deal_status__in=CONVERTED_STATUSES),
                distinct=True,
            ),
            passed_deals=Count(
                "primary_deals",
                filter=Q(primary_deals__deal_status=DealStatus.PASSED),
                distinct=True,
            ),
            last_deal_date=Max(_effective_deal_date("primary_deals__")),
            meeting_count=Count("meetings", distinct=True) + Count("interactions", filter=Q(interactions__kind="MEETING", interactions__meeting_note__isnull=False), distinct=True),
            last_interaction_at=Greatest(Max("interactions__occurred_at"), Max("meetings__created_at")),
        )
        .order_by("-total_deals_introduced", "name", "id")
    )


def bank_analytics_queryset() -> QuerySet[Bank]:
    """Return deal sourcing metrics grouped by the bank linked on each deal."""
    return (
        Bank.objects.annotate(
            banker_count=Count("contacts", distinct=True),
            total_deals_introduced=Count("deals", distinct=True),
            active_mandates=Count(
                "deals",
                filter=~Q(deals__deal_status__in=INACTIVE_STATUSES),
                distinct=True,
            ),
            sourced_mandates=Count(
                "deals",
                filter=(
                    ~Q(deals__deal_status__in=INACTIVE_STATUSES)
                    & ~Q(deals__deal_status__in=IC_STATUSES)
                ),
                distinct=True,
            ),
            ic_mandates=Count(
                "deals",
                filter=Q(deals__deal_status__in=IC_STATUSES),
                distinct=True,
            ),
            converted_deals=Count(
                "deals",
                filter=Q(deals__deal_status__in=CONVERTED_STATUSES),
                distinct=True,
            ),
            passed_deals=Count(
                "deals",
                filter=Q(deals__deal_status=DealStatus.PASSED),
                distinct=True,
            ),
            last_deal_date=Max(_effective_deal_date("deals__")),
        )
        .order_by("-total_deals_introduced", "name", "id")
    )


def deal_activity_queryset(*, contact: Contact | None = None, bank: Bank | None = None):
    if (contact is None) == (bank is None):
        raise ValueError("Provide exactly one of contact or bank.")

    queryset = Deal.objects.select_related("bank", "primary_contact")
    if contact is not None:
        queryset = queryset.filter(primary_contact=contact)
    else:
        queryset = queryset.filter(bank=bank)

    return queryset.annotate(
        activity_date=Coalesce(
            F("received_at"),
            Cast(F("created_at"), output_field=DateField()),
            output_field=DateField(),
        )
    ).order_by("-activity_date", "-created_at", "id")


def conversion_rate(*, converted_deals: int, total_deals: int) -> float:
    if not total_deals:
        return 0.0
    return round((converted_deals / total_deals) * 100, 1)
