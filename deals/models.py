import uuid
from django.db import models
from django.contrib.postgres.fields import ArrayField
from banks.models import Bank
from contacts.models import Contact
from requests.models import Request


class DealPriority(models.TextChoices):
    NEW = 'New', 'New'
    TO_BE_PASSED = 'To be Passed', 'To be Passed'
    TO_BE_PASS = 'To Be Pass', 'To Be Pass'
    PASSED = 'Passed', 'Passed'
    PORTFOLIO = 'Portfolio', 'Portfolio'
    INVESTED = 'Invested', 'Invested'
    HIGH = 'High', 'High'
    MEDIUM = 'Medium', 'Medium'
    LOW = 'Low', 'Low'


class Deal(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.TextField(blank=True, null=True)
    bank = models.ForeignKey(
        Bank,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deals',
        db_column='bank_id'
    )
    priority = models.CharField(
        max_length=20,
        choices=DealPriority.choices,
        blank=True,
        null=True,
        db_column='priority'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    deal_summary = models.TextField(blank=True, null=True)
    funding_ask = models.TextField(blank=True, null=True)
    industry = models.TextField(blank=True, null=True)
    sector = models.TextField(blank=True, null=True)
    comments = models.TextField(blank=True, null=True)
    deal_details = models.TextField(blank=True, null=True)
    is_female_led = models.BooleanField(default=False)
    management_meeting = models.BooleanField(default=False)
    funding_ask_for = models.TextField(blank=True, null=True)
    company_details = models.TextField(blank=True, null=True)
    business_proposal_stage = models.BooleanField(default=False)
    ic_stage = models.BooleanField(default=False)
    request = models.ForeignKey(
        Request,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deals',
        db_column='request_id'
    )
    # Array of profile UUIDs - stored as PostgreSQL array, not FK relationship
    responsibility = ArrayField(
        models.UUIDField(),
        default=list,
        blank=True,
        help_text='Array of profile UUIDs responsible for this deal'
    )
    reasons_for_passing = models.TextField(blank=True, null=True)
    city = models.TextField(blank=True, null=True)
    state = models.TextField(blank=True, null=True)
    country = models.TextField(blank=True, null=True)
    # Array of contact UUIDs - stored as PostgreSQL array, not FK relationship
    # Used for additional contacts beyond the primary_contact
    other_contacts = ArrayField(
        models.UUIDField(),
        default=list,
        blank=True,
        null=True,
        help_text='Array of contact UUIDs (not FK-enforced)'
    )
    primary_contact = models.ForeignKey(
        Contact,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='primary_deals',
        db_column='primary_contact'
    )
    fund = models.TextField(default='FUND3')
    legacy_investment_bank = models.TextField(blank=True, null=True)
    priority_rationale = models.TextField(blank=True, null=True)
    themes = ArrayField(
        models.TextField(),
        default=list,
        blank=True,
        help_text='Array of theme tags'
    )

    class Meta:
        db_table = 'deal'
        ordering = ['-created_at', 'title']
        verbose_name = 'Deal'
        verbose_name_plural = 'Deals'
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['priority']),
            models.Index(fields=['bank']),
        ]

    def __str__(self):
        return self.title or f'Deal {self.id}'
