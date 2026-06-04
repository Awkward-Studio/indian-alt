import uuid
from django.db import models
from banks.models import Bank
from contacts.models import Contact
# Use string reference to avoid import collision with HTTP requests library
# Using string reference 'api_requests.Request' in ForeignKey to avoid circular imports


class DealPriority(models.TextChoices):
    HIGH = 'High', 'High'
    MEDIUM = 'Medium', 'Medium'
    LOW = 'Low', 'Low'


class DealStatus(models.TextChoices):
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
    INVESTED = 'Invested', 'Invested'
    PORTFOLIO = 'Portfolio', 'Portfolio'


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
    INVESTED = 'Invested', 'Invested'
    PORTFOLIO = 'Portfolio', 'Portfolio'
    # Keep legacy choices for backwards compatibility during migration
    ORIGINATION = 'Origination', 'Origination'
    SCREENING = 'Screening', 'Screening'
    MGMT_MEETING = 'Management Meeting', 'Management Meeting'
    DUE_DILIGENCE = 'Due Diligence', 'Due Diligence'
    IC_APPROVAL = 'IC Approval', 'IC Approval'
    TERM_SHEET = 'Term Sheet', 'Term Sheet'
    EXECUTION = 'Execution', 'Execution'


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
        default=DealPriority.MEDIUM,
        blank=True,
        null=True,
        db_column='priority'
    )
    deal_status = models.CharField(
        max_length=50,
        choices=DealStatus.choices,
        default=DealStatus.STAGE_1,
        blank=True,
        null=True,
        db_column='deal_status'
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
    bank_name = models.TextField(blank=True, null=True, help_text="Raw bank name extracted from analysis")
    primary_contact_name = models.TextField(blank=True, null=True, help_text="Raw contact name extracted from analysis")
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
    additional_contacts = models.ManyToManyField(
        Contact,
        related_name='additional_deals',
        blank=True,
        help_text='Additional contacts linked to this deal beyond the primary contact'
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
    analysis_prompt = models.TextField(
        blank=True,
        null=True,
        help_text='Deal-specific analysis directive appended to the AI personality for full rewrites and analysis runs.'
    )

    class Meta:
        db_table = 'deal'
        ordering = ['-created_at', 'title']
        verbose_name = 'Deal'
        verbose_name_plural = 'Deals'
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['priority']),
            models.Index(fields=['deal_status']),
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


class DealRelationshipContext(models.Model):
    class RelationshipType(models.TextChoices):
        COMPETITOR = 'competitor', 'Competitor'
        SISTER_COMPANY = 'sister_company', 'Sister Company'
        PARENT_COMPANY = 'parent_company', 'Parent Company'
        SUBSIDIARY = 'subsidiary', 'Subsidiary'
        COMPARABLE = 'comparable', 'Comparable'
        CUSTOMER = 'customer', 'Customer'
        VENDOR = 'vendor', 'Vendor'
        OTHER = 'other', 'Other'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    deal = models.ForeignKey(
        Deal,
        on_delete=models.CASCADE,
        related_name='relationship_contexts',
    )
    related_deal = models.ForeignKey(
        Deal,
        on_delete=models.CASCADE,
        related_name='related_to_contexts',
        null=True,
        blank=True,
    )
    relationship_type = models.CharField(
        max_length=40,
        choices=RelationshipType.choices,
        default=RelationshipType.COMPARABLE,
    )
    notes = models.TextField(blank=True, null=True)
    selected_deal_ids = models.JSONField(default=list, blank=True)
    selected_document_ids = models.JSONField(default=list, blank=True)
    selected_chunk_ids = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(
        'accounts.Profile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'deal_relationship_context'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['deal', 'relationship_type']),
            models.Index(fields=['related_deal']),
        ]

    def __str__(self):
        target = self.related_deal.title if self.related_deal else "Selected deals"
        return f"{self.deal.title} -> {target} ({self.relationship_type})"


class DealGeneratedDocument(models.Model):
    class DocumentKind(models.TextChoices):
        DIRECTIVE = 'directive', 'Directive Document'
        IC_NOTE = 'ic_note', 'IC Note'
        FINANCIAL_MODEL = 'financial_model', 'Financial Model'
        DILIGENCE_MEMO = 'diligence_memo', 'Diligence Memo'
        RISK_REGISTER = 'risk_register', 'Risk Register'
        OTHER = 'other', 'Other'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    deal = models.ForeignKey(
        Deal,
        on_delete=models.CASCADE,
        related_name='generated_documents',
    )
    title = models.CharField(max_length=255)
    kind = models.CharField(max_length=40, choices=DocumentKind.choices, default=DocumentKind.DIRECTIVE)
    directive = models.TextField()
    content = models.TextField(blank=True, null=True)
    selected_deal_ids = models.JSONField(default=list, blank=True)
    selected_document_ids = models.JSONField(default=list, blank=True)
    selected_chunk_ids = models.JSONField(default=list, blank=True)
    audit_log_id = models.CharField(max_length=255, blank=True, null=True)
    created_by = models.ForeignKey(
        'accounts.Profile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'deal_generated_document'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['deal', 'kind']),
        ]

    def __str__(self):
        return f"{self.title} ({self.deal.title})"


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
    DOCPROC_REMOTE = 'docproc_remote', 'Docproc Remote'
    VLLM_VISION = 'vllm_vision', 'vLLM Vision'
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


class FolderAnalysisDocument(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    audit_log = models.ForeignKey(
        'ai_orchestrator.AIAuditLog',
        on_delete=models.CASCADE,
        related_name='analysis_documents',
    )
    source_file_id = models.CharField(max_length=255, db_index=True)
    source_drive_id = models.CharField(max_length=255, blank=True, default="")
    file_name = models.TextField()
    file_path = models.TextField(blank=True, default="")
    document_type = models.CharField(
        max_length=50,
        choices=DocumentType.choices,
        default=DocumentType.OTHER,
    )
    raw_extracted_text = models.TextField(blank=True, default="")
    normalized_text = models.TextField(blank=True, default="")
    evidence_json = models.JSONField(default=dict, blank=True)
    source_map_json = models.JSONField(default=dict, blank=True)
    table_json = models.JSONField(default=list, blank=True)
    key_metrics_json = models.JSONField(default=list, blank=True)
    reasoning = models.TextField(blank=True, null=True)
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
    quality_flags = models.JSONField(default=list, blank=True)
    render_metadata = models.JSONField(default=dict, blank=True)
    is_indexed = models.BooleanField(default=False)
    chunk_count = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, null=True)
    last_transcribed_at = models.DateTimeField(blank=True, null=True)
    last_chunked_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', '-created_at']
        unique_together = [('audit_log', 'source_file_id')]
        indexes = [
            models.Index(fields=['audit_log', 'source_file_id']),
            models.Index(fields=['audit_log', 'transcription_status']),
            models.Index(fields=['audit_log', 'is_indexed']),
        ]

    def __str__(self):
        return f"{self.file_name} [{self.audit_log_id}]"


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
    normalized_text = models.TextField(blank=True, null=True)
    evidence_json = models.JSONField(default=dict, blank=True)
    source_map_json = models.JSONField(default=dict, blank=True)
    table_json = models.JSONField(default=list, blank=True)
    key_metrics_json = models.JSONField(default=list, blank=True)
    reasoning = models.TextField(blank=True, null=True)
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


class VentureIntelligenceCompanyProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    cin = models.CharField(max_length=21, unique=True, null=True, blank=True, db_index=True)
    name = models.TextField()
    registered_name = models.TextField(null=True, blank=True)
    website = models.TextField(null=True, blank=True)
    industry = models.TextField(null=True, blank=True)
    sector = models.TextField(null=True, blank=True)
    email = models.TextField(null=True, blank=True)
    year_founded = models.CharField(max_length=10, null=True, blank=True)
    city = models.TextField(null=True, blank=True)
    total_funding = models.TextField(null=True, blank=True)

    # New Location & Contact fields
    state = models.TextField(null=True, blank=True)
    region = models.TextField(null=True, blank=True)
    country = models.TextField(null=True, blank=True)
    pincode = models.CharField(max_length=20, null=True, blank=True)
    telephone = models.TextField(null=True, blank=True)
    phone = models.TextField(null=True, blank=True)
    linkedin = models.TextField(null=True, blank=True)

    # New Profile & Status fields
    tags = models.TextField(null=True, blank=True)
    listing_status = models.TextField(null=True, blank=True)
    additional_info = models.TextField(null=True, blank=True)
    short_name = models.TextField(null=True, blank=True)
    previous_name = models.TextField(null=True, blank=True)
    full_name = models.TextField(null=True, blank=True)
    business_description = models.TextField(null=True, blank=True)
    transacted_status = models.CharField(max_length=100, null=True, blank=True)
    incorp_year = models.IntegerField(null=True, blank=True)
    company_status = models.CharField(max_length=100, null=True, blank=True)
    address = models.TextField(null=True, blank=True)
    address_line2 = models.TextField(null=True, blank=True)
    contact_name = models.TextField(null=True, blank=True)
    contact_designation = models.TextField(null=True, blank=True)
    auditor_name = models.TextField(null=True, blank=True)

    # New Shareholding & Tech fields
    shp_year = models.IntegerField(null=True, blank=True)
    shp_promoter = models.FloatField(null=True, blank=True)
    shp_non_promoter = models.FloatField(null=True, blank=True)
    is_xbrl = models.BooleanField(null=True, blank=True)

    raw_profile_json = models.JSONField(default=dict, blank=True, help_text="Full raw JSON response from VI")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'vi_company_profile'
        verbose_name = 'VI Company Profile'
        verbose_name_plural = 'VI Company Profiles'

    def __str__(self):
        return f"{self.name} ({self.cin or 'No CIN'})"


class VentureIntelligenceExecutive(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company_profile = models.ForeignKey(
        VentureIntelligenceCompanyProfile,
        on_delete=models.CASCADE,
        related_name='executives'
    )
    name = models.TextField()
    designation = models.TextField(null=True, blank=True)
    belongs_to_firm_name = models.TextField(null=True, blank=True)
    role_type = models.CharField(
        max_length=20,
        choices=[('management', 'Management'), ('board', 'Board')],
        default='management'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'vi_executive'
        verbose_name = 'VI Executive'
        verbose_name_plural = 'VI Executives'

    def __str__(self):
        return f"{self.name} - {self.designation} ({self.role_type})"


class VentureIntelligencePEInvestment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company_profile = models.ForeignKey(
        VentureIntelligenceCompanyProfile,
        on_delete=models.CASCADE,
        related_name='pe_investments'
    )
    round = models.TextField(null=True, blank=True)
    deal_date = models.TextField(null=True, blank=True)
    amount = models.TextField(null=True, blank=True)
    amount_inr = models.TextField(null=True, blank=True)
    investors = models.JSONField(default=list, blank=True)
    exit_status = models.TextField(null=True, blank=True)
    company_valuation_post_money = models.TextField(null=True, blank=True)
    revenue_multiple_post_money = models.TextField(null=True, blank=True)
    is_vc = models.TextField(null=True, blank=True)
    is_amount_hide = models.BooleanField(null=True, blank=True)
    is_debt_deal = models.BooleanField(null=True, blank=True)
    is_agg_hide = models.BooleanField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'vi_pe_investment'
        ordering = ['-deal_date']


class VentureIntelligenceAngelInvestment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company_profile = models.ForeignKey(
        VentureIntelligenceCompanyProfile,
        on_delete=models.CASCADE,
        related_name='angel_investments'
    )
    date = models.TextField(null=True, blank=True)
    investors = models.JSONField(default=list, blank=True)
    is_exited = models.BooleanField(null=True, blank=True)
    is_agg_hide = models.BooleanField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'vi_angel_investment'
        ordering = ['-date']


class VentureIntelligenceIncubationInvestment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company_profile = models.ForeignKey(
        VentureIntelligenceCompanyProfile,
        on_delete=models.CASCADE,
        related_name='incubation_investments'
    )
    date = models.TextField(null=True, blank=True)
    status = models.TextField(null=True, blank=True)
    incubator = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'vi_incubation_investment'
        ordering = ['-date']


class VentureIntelligencePEExit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company_profile = models.ForeignKey(
        VentureIntelligenceCompanyProfile,
        on_delete=models.CASCADE,
        related_name='pe_exits'
    )
    deal_type = models.TextField(null=True, blank=True)
    date = models.TextField(null=True, blank=True)
    exit_investors = models.JSONField(default=list, blank=True)
    amount = models.TextField(null=True, blank=True)
    exit_status = models.TextField(null=True, blank=True)
    valuation = models.TextField(null=True, blank=True)
    revenue_multiple = models.TextField(null=True, blank=True)
    is_vc = models.BooleanField(null=True, blank=True)
    is_hide_amount = models.BooleanField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'vi_pe_exit'
        ordering = ['-date']


class VentureIntelligencePEIPO(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company_profile = models.ForeignKey(
        VentureIntelligenceCompanyProfile,
        on_delete=models.CASCADE,
        related_name='pe_ipos'
    )
    date = models.TextField(null=True, blank=True)
    ipo_investors = models.JSONField(default=list, blank=True)
    ipo_size = models.TextField(null=True, blank=True)
    is_investor_sale = models.BooleanField(null=True, blank=True)
    ipo_valuation = models.TextField(null=True, blank=True)
    is_amount_hide = models.BooleanField(null=True, blank=True)
    is_vc = models.BooleanField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'vi_pe_ipo'
        ordering = ['-date']


class VentureIntelligenceMergerAcquisition(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company_profile = models.ForeignKey(
        VentureIntelligenceCompanyProfile,
        on_delete=models.CASCADE,
        related_name='mergers_acquisitions'
    )
    company = models.TextField(null=True, blank=True)
    date = models.TextField(null=True, blank=True)
    amount = models.TextField(null=True, blank=True)
    acquirer = models.TextField(null=True, blank=True)
    company_valuation = models.TextField(null=True, blank=True)
    company_valuation_post = models.TextField(null=True, blank=True)
    revenue_multiple = models.TextField(null=True, blank=True)
    revenue_multiple_post = models.TextField(null=True, blank=True)
    is_hide_amount = models.BooleanField(null=True, blank=True)
    is_asset_sale = models.BooleanField(null=True, blank=True)
    is_minority_deal = models.BooleanField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'vi_merger_acquisition'
        ordering = ['-date']


class VentureIntelligenceEpfoData(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company_profile = models.ForeignKey(
        VentureIntelligenceCompanyProfile,
        on_delete=models.CASCADE,
        related_name='epfo_data'
    )
    qrtr = models.TextField(null=True, blank=True)
    employees = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'vi_epfo_data'
        ordering = ['-qrtr']


class VentureIntelligenceSimilarCompany(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company_profile = models.ForeignKey(
        VentureIntelligenceCompanyProfile,
        on_delete=models.CASCADE,
        related_name='similar_companies'
    )
    name = models.TextField()
    cin = models.CharField(max_length=21, null=True, blank=True, db_index=True)
    sector = models.TextField(null=True, blank=True)
    total_funding = models.TextField(null=True, blank=True)
    latest_investment = models.JSONField(default=dict, blank=True)
    city = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'vi_similar_company'
        ordering = ['name']


class VentureIntelligenceStatementType(models.TextChoices):
    PROFIT_LOSS = 'profit_loss', 'Profit & Loss'
    BALANCE_SHEET = 'balance_sheet', 'Balance Sheet'
    CASH_FLOW = 'cash_flow', 'Cash Flow'


class VentureIntelligenceFinancialStatement(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company_profile = models.ForeignKey(
        VentureIntelligenceCompanyProfile,
        on_delete=models.CASCADE,
        related_name='financial_statements'
    )
    statement_type = models.CharField(
        max_length=20,
        choices=VentureIntelligenceStatementType.choices
    )
    fy = models.CharField(max_length=20, db_index=True)  # e.g., "FY23", "2023"
    fin_type = models.CharField(max_length=50, default="Standalone")  # Standalone or Consolidated
    data = models.JSONField(default=dict, help_text="Structured row key-value data")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'vi_financial_statement'
        unique_together = ('company_profile', 'statement_type', 'fy', 'fin_type')
        ordering = ['-fy', 'statement_type']

    def __str__(self):
        return f"{self.company_profile.name} - {self.statement_type} - {self.fy} ({self.fin_type})"


class VentureIntelligenceRelationType(models.TextChoices):
    TARGET = 'target', 'Target Company'
    COMPETITOR = 'competitor', 'Competitor Company'


class VentureIntelligenceCompanyRelation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    deal = models.ForeignKey(
        Deal,
        on_delete=models.CASCADE,
        related_name='vi_relations'
    )
    company_profile = models.ForeignKey(
        VentureIntelligenceCompanyProfile,
        on_delete=models.CASCADE,
        related_name='deal_relations'
    )
    relation_type = models.CharField(
        max_length=20,
        choices=VentureIntelligenceRelationType.choices,
        default=VentureIntelligenceRelationType.TARGET
    )
    notes = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'vi_company_relation'
        unique_together = ('deal', 'company_profile')
        verbose_name = 'VI Company Relation'
        verbose_name_plural = 'VI Company Relations'

    def __str__(self):
        return f"{self.deal.title} -> {self.company_profile.name} ({self.relation_type})"

