import uuid
from django.db import models
from django.utils.translation import gettext_lazy as _

class AIPersonality(models.Model):
    """
    Stores different personalities for the LLM (e.g., "Private Equity MD", "Deal Analyst").
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True, help_text="Unique name for this personality")
    description = models.TextField(blank=True, help_text="Brief description of what this personality is for")
    
    model_provider = models.CharField(
        max_length=50, 
        default='ollama',
        choices=[
            ('ollama', 'Ollama (Local/Azure)'),
            ('openai', 'OpenAI (GPT-4o)'),
            ('gemini', 'Google Gemini'),
            ('anthropic', 'Anthropic Claude'),
        ]
    )
    text_model_name = models.CharField(max_length=100, default='llama3.1:latest', help_text="Model for text-only tasks")
    vision_model_name = models.CharField(max_length=100, default='llava:latest', help_text="Model for tasks with images/charts")
    
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
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True, help_text="Unique name for this skill (e.g., deal_extraction)")
    description = models.TextField(blank=True)
    prompt_template = models.TextField(help_text="The task-specific prompt template")
    input_schema = models.JSONField(default=dict, blank=True, help_text="Expected JSON structure for input (optional)")
    output_schema = models.JSONField(default=dict, blank=True, help_text="Expected JSON structure for output (optional)")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "AI Skill"
        verbose_name_plural = "AI Skills"
        ordering = ['name']

    def __str__(self):
        return self.name


class AIAuditLog(models.Model):
    """
    Logs every interaction with the LLM for transparency and debugging.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Generic relationship or specific FK? Let's use specific FK to Email if possible, or generic.
    # For now, let's keep it simple with text fields for the source.
    source_type = models.CharField(max_length=50, default="email")
    source_id = models.CharField(max_length=255, blank=True, null=True)
    
    personality = models.ForeignKey(AIPersonality, on_delete=models.SET_NULL, null=True)
    skill = models.ForeignKey(AISkill, on_delete=models.SET_NULL, null=True)
    
    model_provider = models.CharField(max_length=50, default='ollama')
    model_used = models.CharField(max_length=100)
    
    # Payload details
    system_prompt = models.TextField()
    user_prompt = models.TextField()
    raw_response = models.TextField()
    parsed_json = models.JSONField(null=True, blank=True)
    
    # Performance
    request_duration_ms = models.IntegerField(null=True, blank=True)
    tokens_used = models.IntegerField(null=True, blank=True)
    
    error_message = models.TextField(blank=True, null=True)
    is_success = models.BooleanField(default=True)
    
    created_at = models.DateTimeField(auto_now_add=True)

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
