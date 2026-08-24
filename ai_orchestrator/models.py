import uuid
from django.db import models
from django.contrib.auth.models import User
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.utils.translation import gettext_lazy as _
from pgvector.django import HnswIndex, VectorField

class AIPersonality(models.Model):
    """
    Stores different personalities for the LLM (e.g., "Private Equity MD", "Deal Analyst").
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True, help_text="Unique name for this personality")
    description = models.TextField(blank=True, help_text="Brief description of what this personality is for")
    
    model_provider = models.CharField(
        max_length=50, 
        default='vllm',
        choices=[
            ('vllm', 'vLLM (OpenAI-Compatible)'),
            ('anthropic', 'Anthropic Claude API'),
        ]
    )
    text_model_name = models.CharField(max_length=200, default='default', help_text="Model for text-only tasks")
    system_instructions = models.TextField(help_text="The core 'system' prompt that defines behavior")
    is_default = models.BooleanField(default=False, help_text="Whether this is the default personality")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "AI Personality"
        verbose_name_plural = "AI Personalities"
        ordering = ['-is_default', 'name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.is_default:
            # Ensure only one default personality exists
            AIPersonality.objects.filter(is_default=True).update(is_default=False)
        super().save(*args, **kwargs)


class AISkill(models.Model):
    """
    Stores specific 'skills' or task-based prompts (e.g., "Deal Extraction", "Summary Generation").
    """
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        RETIRED = "retired", "Retired"

    class Format(models.TextChoices):
        NATIVE_PROMPT_V1 = "native_prompt_v1", "Native prompt template v1"
        CLAUDE_PROMPT_V1 = "claude_prompt_v1", "Claude prompt-only subset v1"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True, help_text="Unique name for this skill (e.g., deal_extraction)")
    description = models.TextField(blank=True)
    system_template = models.TextField(blank=True, help_text="Optional system-level instructions for this skill")
    prompt_template = models.TextField(help_text="The task-specific prompt template")
    input_schema = models.JSONField(default=dict, blank=True, help_text="Expected JSON structure for input (optional)")
    output_schema = models.JSONField(default=dict, blank=True, help_text="Expected JSON structure for output (optional)")
    owner = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_ai_skills",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.APPROVED,
        db_index=True,
    )
    approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_ai_skills",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    skill_format = models.CharField(
        max_length=30,
        choices=Format.choices,
        default=Format.NATIVE_PROMPT_V1,
    )
    is_industry_overview_eligible = models.BooleanField(
        default=False,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "AI Skill"
        verbose_name_plural = "AI Skills"
        ordering = ['name']

    def __str__(self):
        return self.name


class DealIndustrySkillAssignment(models.Model):
    class RunStatus(models.TextChoices):
        IDLE = "IDLE", "Idle"
        QUEUED = "QUEUED", "Queued"
        PROCESSING = "PROCESSING", "Processing"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    deal = models.ForeignKey(
        "deals.Deal",
        on_delete=models.CASCADE,
        related_name="industry_skill_assignments",
    )
    skill = models.ForeignKey(
        AISkill,
        on_delete=models.PROTECT,
        related_name="deal_assignments",
    )
    enabled = models.BooleanField(default=True)
    auto_run = models.BooleanField(default=False)
    inputs = models.JSONField(default=dict, blank=True)
    source_document_ids = models.JSONField(default=list, blank=True)
    last_context_hash = models.CharField(max_length=64, blank=True, default="")
    last_run_status = models.CharField(
        max_length=20,
        choices=RunStatus.choices,
        default=RunStatus.IDLE,
    )
    last_run_trigger = models.CharField(max_length=20, blank=True, default="")
    last_audit_log = models.ForeignKey(
        "AIAuditLog",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="industry_skill_assignments",
    )
    configured_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="configured_industry_skills",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["skill__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["deal", "skill"],
                name="unique_deal_industry_skill_assignment",
            ),
        ]
        indexes = [
            models.Index(fields=["deal", "enabled", "auto_run"]),
        ]

    def __str__(self):
        return f"{self.deal_id}: {self.skill.name}"


class AnalysisProtocol(models.Model):
    """
    Stores institutional rules for deal analysis. 
    This decouples the "Style" and "Logic" from the code.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    
    # Forensic Directives (The "Institutional Style")
    directives = models.JSONField(
        default=list, 
        help_text="List of rules: ['Find all forensic risks', 'Convert units to INR']"
    )
    
    # Output Control
    output_schema = models.JSONField(
        default=dict, 
        help_text="Mandatory JSON structure for the AI response"
    )
    
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Analysis Protocol"
        verbose_name_plural = "Analysis Protocols"
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.is_active:
            AnalysisProtocol.objects.filter(is_active=True).update(is_active=False)
        super().save(*args, **kwargs)


class AIFlowDefinition(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class AIFlowVersion(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    flow = models.ForeignKey(
        AIFlowDefinition,
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version = models.IntegerField(default=1)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    config = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-version", "-updated_at"]
        unique_together = [("flow", "version")]
        indexes = [
            models.Index(fields=["flow", "status"]),
        ]

    def __str__(self):
        return f"{self.flow.key} v{self.version} ({self.status})"


class AIPromptDefinition(models.Model):
    """Stable identity and editable contract for one production prompt."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=150, unique=True)
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=100, blank=True, default="")
    description = models.TextField(blank=True, default="")
    variables = models.JSONField(default=list, blank=True)
    is_guardrail = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category", "name"]

    def __str__(self):
        return self.key


class AIPromptRevision(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    definition = models.ForeignKey(
        AIPromptDefinition, on_delete=models.CASCADE, related_name="revisions"
    )
    revision = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    system_template = models.TextField(blank=True, default="")
    user_template = models.TextField(blank=True, default="")
    input_schema = models.JSONField(default=dict, blank=True)
    output_schema = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_ai_prompt_revisions",
    )
    published_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="published_ai_prompt_revisions",
    )
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(fields=["definition", "revision"], name="unique_ai_prompt_revision"),
        ]

    def __str__(self):
        return f"{self.definition.key} r{self.revision} ({self.status})"


class AISkillRevision(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    skill = models.ForeignKey(AISkill, on_delete=models.CASCADE, related_name="revisions")
    revision = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    system_template = models.TextField(blank=True, default="")
    prompt_template = models.TextField(blank=True, default="")
    input_schema = models.JSONField(default=dict, blank=True)
    output_schema = models.JSONField(default=dict, blank=True)
    skill_format = models.CharField(max_length=30, choices=AISkill.Format.choices, default=AISkill.Format.NATIVE_PROMPT_V1)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_ai_skill_revisions")
    published_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="published_ai_skill_revisions")
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(fields=["skill", "revision"], name="unique_ai_skill_revision"),
        ]

    def __str__(self):
        return f"{self.skill.name} r{self.revision} ({self.status})"


class AIPipelineDefinition(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=150, unique=True)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.key


class AIPipelineStage(models.Model):
    class Kind(models.TextChoices):
        PROMPT = "prompt", "Prompt"
        SKILL = "skill", "Skill"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pipeline = models.ForeignKey(AIPipelineDefinition, on_delete=models.CASCADE, related_name="stages")
    key = models.CharField(max_length=150)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    position = models.PositiveIntegerField(default=0)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    prompt_definition = models.ForeignKey(AIPromptDefinition, on_delete=models.PROTECT, null=True, blank=True, related_name="stages")
    skill = models.ForeignKey(AISkill, on_delete=models.PROTECT, null=True, blank=True, related_name="pipeline_stages")
    required_variables = models.JSONField(default=list, blank=True)
    is_required = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["pipeline__name", "position", "name"]
        constraints = [
            models.UniqueConstraint(fields=["pipeline", "key"], name="unique_ai_pipeline_stage_key"),
        ]

    def __str__(self):
        return f"{self.pipeline.key}.{self.key}"


class AIAuditLog(models.Model):
    """
    Logs every interaction with the LLM for transparency and debugging.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Generic relationship or specific FK? Let's use specific FK to Email if possible, or generic.
    # For now, let's keep it simple with text fields for the source.
    source_type = models.CharField(max_length=50, default="email")
    source_id = models.CharField(max_length=255, blank=True, null=True)
    context_label = models.CharField(max_length=500, blank=True, null=True, help_text='Descriptive label (Folder Name, Email Subject, etc.)')
    
    personality = models.ForeignKey(AIPersonality, on_delete=models.SET_NULL, null=True)
    skill = models.ForeignKey(AISkill, on_delete=models.SET_NULL, null=True)
    requested_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_ai_runs",
    )
    skill_version = models.PositiveIntegerField(null=True, blank=True)
    pipeline = models.ForeignKey(AIPipelineDefinition, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_logs")
    pipeline_stage = models.ForeignKey(AIPipelineStage, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_logs")
    prompt_revision = models.ForeignKey(AIPromptRevision, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_logs")
    skill_revision = models.ForeignKey(AISkillRevision, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_logs")
    
    model_provider = models.CharField(max_length=50, default='vllm')
    model_used = models.CharField(max_length=100)
    
    # Payload details
    system_prompt = models.TextField()
    user_prompt = models.TextField()
    raw_response = models.TextField()
    raw_thinking = models.TextField(blank=True, null=True, help_text='Real-time thinking trace')
    parsed_json = models.JSONField(null=True, blank=True)
    
    # Performance
    request_duration_ms = models.IntegerField(null=True, blank=True)
    tokens_used = models.IntegerField(null=True, blank=True)
    
    # Context Preservation
    source_metadata = models.JSONField(null=True, blank=True, help_text='Extra context like file trees or drive IDs')
    celery_task_id = models.CharField(max_length=255, null=True, blank=True, help_text='ID of the associated Celery task')
    
    error_message = models.TextField(blank=True, null=True)
    worker_logs = models.JSONField(default=list, blank=True, help_text='Execution logs from celery workers')
    is_success = models.BooleanField(default=True)
    
    status = models.CharField(
        max_length=20, 
        default='PENDING',
        choices=[
            ('PENDING', 'Pending'),
            ('PROCESSING', 'Processing'),
            ('COMPLETED', 'Completed'),
            ('FAILED', 'Failed'),
        ],
        null=True, blank=True
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "AI Audit Log"
        verbose_name_plural = "AI Audit Logs"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['source_type', 'source_id']),
            models.Index(fields=['-created_at']),
        ]

    def __str__(self):
        return f"{self.source_type} {self.source_id} - {self.created_at}"


class VMControlOperation(models.Model):
    class Action(models.TextChoices):
        START = "start", "Start"
        DEALLOCATE = "deallocate", "Deallocate"

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    action = models.CharField(max_length=20, choices=Action.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SUBMITTED)
    target_label = models.CharField(max_length=200)
    target_vm_name = models.CharField(max_length=200)
    target_resource_id = models.CharField(max_length=600)
    provider_request_id = models.CharField(max_length=200, blank=True, default="")
    requested_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="vm_control_operations")
    requested_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-requested_at"]
        indexes = [models.Index(fields=["status", "requested_at"], name="ai_orchestr_status_8ee0c2_idx")]

    def __str__(self):
        return f"{self.action} {self.target_vm_name} ({self.status})"

class AIConversation(models.Model):
    """
    Stores a persistent chat session between a user and the AI.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ai_conversations')
    title = models.CharField(max_length=255, default="New Conversation")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"{self.title} ({self.user.username})"

class AIMessage(models.Model):
    """
    Stores individual messages within a conversation.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(AIConversation, on_delete=models.CASCADE, related_name='messages')
    role = models.CharField(max_length=20, choices=[('user', 'User'), ('assistant', 'Assistant')])
    content = models.TextField()
    thinking = models.TextField(blank=True, null=True)
    
    # Metadata for the UI
    data_points = models.JSONField(default=list, blank=True)
    applied_filters = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.role} - {self.created_at}"

class DocumentChunk(models.Model):
    """
    Stores individual chunks of documents with their vector embeddings for RAG.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    deal = models.ForeignKey(
        'deals.Deal',
        on_delete=models.CASCADE,
        related_name='chunks',
        help_text='The deal this chunk belongs to',
        null=True,
        blank=True,
    )
    audit_log = models.ForeignKey(
        'ai_orchestrator.AIAuditLog',
        on_delete=models.CASCADE,
        related_name='chunks',
        null=True,
        blank=True,
        help_text='Initial analysis run this chunk belongs to before a deal exists',
    )
    
    # Provenance
    source_type = models.CharField(
        max_length=50, 
        choices=[
            ('email', 'Email Body'),
            ('attachment', 'Email Attachment'),
            ('onedrive', 'OneDrive File'),
            ('deal_summary', 'Deal Summary'),
            ('document', 'Deal Document Artifact'),
            ('analysis_document', 'Folder Analysis Document Artifact'),
            ('meeting_note', 'Meeting Note'),
            ('ai_thinking', 'AI Reasoning Logic'),
            ('ai_ambiguities', 'AI Identified Ambiguities'),
            ('extracted_source', 'Raw Extracted Text'),
        ]
    )
    source_id = models.CharField(max_length=255, help_text="Original ID of the source (Email ID, File ID, etc.)")
    
    # Content & Vector
    content = models.TextField()
    search_text = models.TextField(blank=True, default="")
    search_vector = SearchVectorField(null=True, blank=True)
    # Qwen/Qwen3-Embedding-0.6B returns 1024 dimensions.
    embedding = VectorField(dimensions=1024, null=True, blank=True)
    embedding_model = models.CharField(max_length=200, blank=True, default="")
    embedding_dimensions = models.IntegerField(null=True, blank=True)
    indexed_at = models.DateTimeField(null=True, blank=True)
    
    # Extra context (e.g., filename, page number, chunk index)
    metadata = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Document Chunk"
        verbose_name_plural = "Document Chunks"
        indexes = [
            models.Index(fields=['deal', 'source_type']),
            models.Index(fields=['audit_log', 'source_type']),
            GinIndex(fields=['search_vector'], name='docchunk_search_vector_gin'),
            HnswIndex(
                name='docchunk_embedding_hnsw',
                fields=['embedding'],
                m=16,
                ef_construction=64,
                opclasses=['vector_cosine_ops'],
            ),
        ]

    def __str__(self):
        return f"Chunk for Deal {self.deal_id} ({self.source_type})"


class DealRetrievalProfile(models.Model):
    """
    Semantic retrieval profile for shortlist generation before chunk search.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    deal = models.OneToOneField(
        'deals.Deal',
        on_delete=models.CASCADE,
        related_name='retrieval_profile',
    )
    profile_text = models.TextField()
    embedding = VectorField(dimensions=1024, null=True, blank=True)
    embedding_model = models.CharField(max_length=200, blank=True, default="")
    embedding_dimensions = models.IntegerField(null=True, blank=True)
    source_version = models.CharField(max_length=100, blank=True, default="v1")
    metadata = models.JSONField(default=dict, blank=True)
    indexed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['deal']),
            models.Index(fields=['embedding_model']),
            HnswIndex(
                name='dealprofile_embedding_hnsw',
                fields=['embedding'],
                m=16,
                ef_construction=64,
                opclasses=['vector_cosine_ops'],
            ),
        ]

    def __str__(self):
        return f"Retrieval Profile for {self.deal_id}"


class AISystemSetting(models.Model):
    """
    Stores system-wide key-value configuration overrides for AI services.
    """
    key = models.CharField(max_length=100, unique=True, help_text="Setting key (e.g. CLAUDE_TEXT_MODEL)")
    value = models.TextField(help_text="Setting value")
    description = models.TextField(blank=True, null=True, help_text="Optional description of what this setting is for")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "AI System Setting"
        verbose_name_plural = "AI System Settings"
        ordering = ['key']

    def __str__(self):
        return f"{self.key}: {self.value}"
