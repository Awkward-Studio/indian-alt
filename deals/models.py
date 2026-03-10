import uuid
from django.db import models
from banks.models import Bank
from contacts.models import Contact
# Use string reference to avoid import collision with HTTP requests library
# Using string reference 'api_requests.Request' in ForeignKey to avoid circular imports


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
        'api_requests.Request',  # String reference to avoid import collision
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deals',
        db_column='request_id'
    )
    # Responsible users for this deal
    responsibility = models.ManyToManyField(
        'accounts.Profile',
        related_name='deals',
        blank=True,
        help_text='Profiles responsible for this deal'
    )
    reasons_for_passing = models.TextField(blank=True, null=True)
    city = models.TextField(blank=True, null=True)
    state = models.TextField(blank=True, null=True)
    country = models.TextField(blank=True, null=True)
    # Originally: ArrayField(models.UUIDField(), ...) for Postgres.
    # Used for additional contacts beyond the primary_contact.
    # Now stored as JSON list for SQLite/Postgres compatibility.
    other_contacts = models.JSONField(
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
    # Originally: ArrayField(models.TextField(), ...) for Postgres.
    # Now stored as JSON list for SQLite/Postgres compatibility.
    themes = models.JSONField(
        default=list,
        blank=True,
        help_text='Array of theme tags'
    )
    is_indexed = models.BooleanField(
        default=False,
        help_text='Whether this deal data has been vectorized and stored in the vector database'
    )
    extracted_text = models.TextField(blank=True, null=True, help_text='Combined text from linked source (Email/Files) for RAG context')
    
    # Forensic Analysis Storage
    thinking = models.TextField(blank=True, null=True, help_text='Internal reasoning process of the AI')
    ambiguities = models.JSONField(default=list, blank=True, help_text='List of ambiguous points identified during analysis')
    analysis_json = models.JSONField(default=dict, blank=True, help_text='Full raw JSON output from the AI analysis')

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


class DocumentType(models.TextChoices):
    PITCH_DECK = 'Pitch Deck', 'Pitch Deck'
    FINANCIALS = 'Financials', 'Financials'
    LEGAL = 'Legal', 'Legal'
    TERM_SHEET = 'Term Sheet', 'Term Sheet'
    KYC = 'KYC', 'KYC'
    MEMO = 'Memo', 'Memo'
    OTHER = 'Other', 'Other'


class DealDocument(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    deal = models.ForeignKey(
        Deal,
        on_delete=models.CASCADE,
        related_name='documents'
    )
    title = models.TextField()
    document_type = models.CharField(
        max_length=50,
        choices=DocumentType.choices,
        default=DocumentType.OTHER
    )
    onedrive_id = models.TextField(blank=True, null=True)
    file_url = models.URLField(blank=True, null=True)
    extracted_text = models.TextField(blank=True, null=True)
    is_indexed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        'accounts.Profile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    class Meta:
        db_table = 'deal_document'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.document_type}: {self.title}"
