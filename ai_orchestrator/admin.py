from django.contrib import admin
from .models import (
    AIPersonality, AISkill, AIConversation, AIMessage, 
    AIAuditLog, DocumentChunk, AnalysisProtocol, 
    AIFlowDefinition, AIFlowVersion
)

@admin.register(AIPersonality)
class AIPersonalityAdmin(admin.ModelAdmin):
    list_display = ('name', 'text_model_name', 'vision_model_name', 'is_default', 'created_at')
    list_filter = ('is_default', 'model_provider')
    search_fields = ('name', 'system_instructions')

@admin.register(AISkill)
class AISkillAdmin(admin.ModelAdmin):
    list_display = ('name', 'description', 'created_at')
    search_fields = ('name', 'description', 'prompt_template')

@admin.register(AnalysisProtocol)
class AnalysisProtocolAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'description')

@admin.register(AIFlowDefinition)
class AIFlowDefinitionAdmin(admin.ModelAdmin):
    list_display = ('key', 'name', 'created_at')
    search_fields = ('key', 'name')

@admin.register(AIFlowVersion)
class AIFlowVersionAdmin(admin.ModelAdmin):
    list_display = ('flow', 'version', 'status', 'created_at')
    list_filter = ('status', 'version')

@admin.register(AIConversation)
class AIConversationAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'title', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('title', 'user__username', 'user__email')

@admin.register(AIMessage)
class AIMessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'conversation', 'role', 'created_at')
    list_filter = ('role', 'created_at')

@admin.register(AIAuditLog)
class AIAuditLogAdmin(admin.ModelAdmin):
    list_display = ('context_label', 'source_type', 'status', 'is_success', 'model_used', 'created_at')
    list_filter = ('status', 'is_success', 'source_type', 'model_used')
    search_fields = ('context_label', 'source_id', 'error_message')
    readonly_fields = ('created_at',)

@admin.register(DocumentChunk)
class DocumentChunkAdmin(admin.ModelAdmin):
    list_display = ('get_deal_title', 'source_type', 'get_filename', 'created_at')
    list_filter = ('source_type', 'created_at', 'deal')
    search_fields = ('content', 'metadata')
    readonly_fields = ('created_at',)

    def get_deal_title(self, obj):
        return obj.deal.title if obj.deal else "N/A"
    get_deal_title.short_description = 'Deal'

    def get_filename(self, obj):
        return obj.metadata.get('filename', 'Unknown')
    get_filename.short_description = 'Filename'
