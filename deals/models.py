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


class DealPhase(models.TextChoices):
    STAGE_1 = '1: Deal Sourced', '1: Deal Sourced'
    STAGE_2 = '2: Initial Banker Call', '2: Initial Banker Call'
    STAGE_3 = '3: NDA Execution', '3: NDA Execution'
    STAGE_4 = '4: Initial Materials Review', '4: Initial Materials Review'
    STAGE_5 = '5: Financial Model Call', '5: Financial Model Call'
    STAGE_6 = '6: Additional Data Request', '6: Additional Data Request'
    STAGE_7 = '7: Industry Research', '7: Industry Research'
    STAGE_8 = '8: Reference Calls', '8: Reference Calls'
    STAGE_9 = '9: IA Model Build', '9: IA Model Build'
    STAGE_10 = '10: Field Visit', '10: Field Visit'
    STAGE_11 = '11: Business Proposal', '11: Business Proposal'
    STAGE_12 = '12: Term Sheet', '12: Term Sheet'
    STAGE_13 = '13: Full Due Diligence', '13: Full Due Diligence'
    STAGE_14 = '14: IC Note I', '14: IC Note I'
    STAGE_15 = '15: IC Feedback', '15: IC Feedback'
    STAGE_16 = '16: IC Note II', '16: IC Note II'
    STAGE_17 = '17: Definitive Documentation', '17: Definitive Documentation'
    STAGE_18 = '18: Closure', '18: Closure'
    PASSED = 'Passed', 'Passed'
    # Keep legacy choices for backwards compatibility during migration
    ORIGINATION = 'Origination', 'Origination'
    SCREENING = 'Screening', 'Screening'
    MGMT_MEETING = 'Management Meeting', 'Management Meeting'
    DUE_DILIGENCE = 'Due Diligence', 'Due Diligence'
    IC_APPROVAL = 'IC Approval', 'IC Approval'
    TERM_SHEET = 'Term Sheet', 'Term Sheet'
    EXECUTION = 'Execution', 'Execution'
    PORTFOLIO = 'Portfolio', 'Portfolio'


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
    current_phase = models.CharField(
        max_length=50,
        choices=DealPhase.choices,
        default=DealPhase.STAGE_1
    )
    deal_flow_decisions = models.JSONField(
        default=dict,
        blank=True,
        help_text='Dictionary mapping stage IDs to decisions (e.g., {"1": "yes"})'
    )
    rejection_stage_id = models.IntegerField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, null=True)
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
    
    source_onedrive_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text='The OneDrive/SharePoint folder ID this deal was created from'
    )
    source_drive_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text='The OneDrive/SharePoint Drive ID this deal belongs to'
    )
    source_email_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text='The Microsoft Graph ID of the email this deal was created from'
    )

    # Background Processing Tracking
    processing_status = models.CharField(
        max_length=20,
        default='idle',
        choices=[
            ('idle', 'Idle'),
            ('processing', 'Processing Background Files'),
            ('completed', 'Completed'),
            ('failed', 'Failed'),
        ],
        help_text='Status of background file processing from OneDrive'
    )
    processing_error = models.TextField(blank=True, null=True)

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

    @property
    def latest_analysis(self):
        return self.analyses.order_by('-version', '-created_at').first()

    @property
    def initial_analysis_record(self):
        analysis = self.analyses.filter(analysis_kind=AnalysisKind.INITIAL).order_by('version', 'created_at').first()
        return analysis or self.analyses.order_by('version', 'created_at').first()

    @property
    def latest_supplemental_analysis_record(self):
        return self.analyses.filter(analysis_kind=AnalysisKind.SUPPLEMENTAL).order_by('-version', '-created_at').first()

    @property
    def thinking(self):
        analysis = self.latest_analysis
        return analysis.thinking if analysis else None

    @property
    def ambiguities(self):
        analysis = self.latest_analysis
        return analysis.ambiguities if analysis else []

    @property
    def analysis_json(self):
        analysis = self.latest_analysis
        return analysis.analysis_json if analysis else {}

    @staticmethod
    def _normalize_analysis_record(analysis):
        if not analysis:
            return None

        analysis_json = analysis.analysis_json if isinstance(analysis.analysis_json, dict) else {}
        metadata = analysis_json.get('metadata') if isinstance(analysis_json.get('metadata'), dict) else {}
        canonical_snapshot = analysis_json.get('canonical_snapshot')
        if not isinstance(canonical_snapshot, dict):
            canonical_snapshot = {
                'deal_model_data': analysis_json.get('deal_model_data') if isinstance(analysis_json.get('deal_model_data'), dict) else {},
                'analyst_report': analysis_json.get('analyst_report', ''),
                'metadata': {
                    'ambiguous_points': metadata.get('ambiguous_points', []),
                },
            }

        report = analysis_json.get('analyst_report')
        if not isinstance(report, str):
            report = ''

        return {
            'version': analysis.version,
            'kind': analysis.analysis_kind,
            'thinking': analysis.thinking,
            'ambiguities': analysis.ambiguities,
            'analysis_json': analysis_json,
            'report': report,
            'created_at': analysis.created_at.isoformat() if analysis.created_at else None,
            'documents_analyzed': metadata.get('documents_analyzed', []),
            'analysis_input_files': metadata.get('analysis_input_files', []),
            'failed_files': metadata.get('failed_files', []),
            'canonical_snapshot': canonical_snapshot,
        }

    @property
    def initial_analysis(self):
        return self._normalize_analysis_record(self.initial_analysis_record)

    @property
    def current_analysis(self):
        current = self._normalize_analysis_record(self.latest_analysis)
        if current:
            return current

        fallback_report = self.deal_summary or ""
        return {
            'version': None,
            'kind': AnalysisKind.INITIAL,
            'thinking': None,
            'ambiguities': [],
            'analysis_json': {},
            'report': fallback_report,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'documents_analyzed': [],
            'analysis_input_files': [],
            'failed_files': [],
            'canonical_snapshot': {
                'deal_model_data': {},
                'analyst_report': fallback_report,
                'metadata': {'ambiguous_points': []},
            },
        }

    @property
    def analysis_history(self):
        analyses = self.analyses.order_by('version', 'created_at')
        return [self._normalize_analysis_record(analysis) for analysis in analyses]


class AnalysisKind(models.TextChoices):
    INITIAL = 'initial', 'Initial'
    SUPPLEMENTAL = 'supplemental', 'Supplemental'


class DealAnalysis(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    deal = models.ForeignKey(
        Deal,
        on_delete=models.CASCADE,
        related_name='analyses'
    )
    version = models.IntegerField(default=1)
    analysis_kind = models.CharField(
        max_length=20,
        choices=AnalysisKind.choices,
        default=AnalysisKind.INITIAL,
    )
    thinking = models.TextField(blank=True, null=True, help_text='Internal reasoning process of the AI')
    ambiguities = models.JSONField(default=list, blank=True, help_text='List of ambiguous points identified during analysis')
    analysis_json = models.JSONField(default=dict, blank=True, help_text='Full raw JSON output from the AI analysis')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'deal_analysis'
        ordering = ['-version', '-created_at']

    def __str__(self):
        return f"Analysis v{self.version} for {self.deal.title}"


class DealPhaseLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    deal = models.ForeignKey(
        Deal,
        on_delete=models.CASCADE,
        related_name='phase_logs'
    )
    from_phase = models.CharField(max_length=50, choices=DealPhase.choices, null=True)
    to_phase = models.CharField(max_length=50, choices=DealPhase.choices)
    rationale = models.TextField(blank=True, null=True)
    changed_at = models.DateTimeField(auto_now_add=True)
    changed_by = models.ForeignKey(
        'accounts.Profile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    class Meta:
        db_table = 'deal_phase_log'
        ordering = ['-changed_at']

    def __str__(self):
        return f"{self.deal.title}: {self.from_phase} -> {self.to_phase}"


class DocumentType(models.TextChoices):
    PITCH_DECK = 'Pitch Deck', 'Pitch Deck'
    FINANCIALS = 'Financials', 'Financials'
    LEGAL = 'Legal', 'Legal'
    TERM_SHEET = 'Term Sheet', 'Term Sheet'
    KYC = 'KYC', 'KYC'
    MEMO = 'Memo', 'Memo'
    OTHER = 'Other', 'Other'


class InitialAnalysisStatus(models.TextChoices):
    NOT_SELECTED = 'not_selected', 'Not Selected'
    SELECTED_AND_ANALYZED = 'selected_and_analyzed', 'Selected And Analyzed'
    SELECTED_FAILED = 'selected_failed', 'Selected Failed'


class ExtractionMode(models.TextChoices):
    GLM_OCR = 'glm_ocr', 'GLM OCR'
    FALLBACK_TEXT = 'fallback_text', 'Fallback Text'


class TranscriptionStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    PARTIAL = 'partial', 'Partial'
    COMPLETE = 'complete', 'Complete'
    FAILED = 'failed', 'Failed'


class ChunkingStatus(models.TextChoices):
    NOT_CHUNKED = 'not_chunked', 'Not Chunked'
    CHUNKED = 'chunked', 'Chunked'
    FAILED = 'failed', 'Failed'


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
    is_ai_analyzed = models.BooleanField(
        default=False,
        help_text='Whether this document was included in the AI summary generation'
    )
    initial_analysis_status = models.CharField(
        max_length=40,
        choices=InitialAnalysisStatus.choices,
        default=InitialAnalysisStatus.NOT_SELECTED,
        help_text='Whether the document was selected for the initial folder analysis flow.',
    )
    initial_analysis_reason = models.TextField(blank=True, null=True)
    extraction_mode = models.CharField(
        max_length=40,
        choices=ExtractionMode.choices,
        blank=True,
        null=True,
    )
    transcription_status = models.CharField(
        max_length=20,
        choices=TranscriptionStatus.choices,
        default=TranscriptionStatus.PENDING,
    )
    chunking_status = models.CharField(
        max_length=20,
        choices=ChunkingStatus.choices,
        default=ChunkingStatus.NOT_CHUNKED,
    )
    last_transcribed_at = models.DateTimeField(blank=True, null=True)
    last_chunked_at = models.DateTimeField(blank=True, null=True)
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
