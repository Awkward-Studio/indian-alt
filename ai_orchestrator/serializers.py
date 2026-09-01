from rest_framework import serializers
from .models import AIConversation, AIMessage, AIPersonality, AISkill, AnalysisProtocol, AIAuditLog, AIFlowDefinition, AIFlowVersion

class AIAuditLogSerializer(serializers.ModelSerializer):
    personality_name = serializers.SerializerMethodField()
    skill_name = serializers.SerializerMethodField()
    requested_by_name = serializers.SerializerMethodField()
    
    class Meta:
        model = AIAuditLog
        fields = [
            'id', 'source_type', 'source_id', 'context_label', 'personality', 'personality_name', 
            'skill', 'skill_name', 'model_provider', 'model_used', 
            'requested_by', 'requested_by_name', 'skill_version',
            'pipeline', 'pipeline_stage', 'prompt_revision', 'skill_revision',
            'request_duration_ms', 'tokens_used', 'is_success', 'status',
            'celery_task_id', 'created_at', 'completed_at', 'error_message',
            'raw_response', 'raw_thinking', 'user_prompt', 'system_prompt', 'parsed_json',
            'source_metadata'
        ]

    def get_personality_name(self, obj):
        return obj.personality.name if obj.personality else "Direct Inference"

    def get_skill_name(self, obj):
        return obj.skill.name if obj.skill else "General Analysis"

    def get_requested_by_name(self, obj):
        if not obj.requested_by:
            return None
        return obj.requested_by.get_full_name() or obj.requested_by.username

class AIPersonalitySerializer(serializers.ModelSerializer):
    class Meta:
        model = AIPersonality
        fields = '__all__'

class AISkillSerializer(serializers.ModelSerializer):
    owner_name = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()

    class Meta:
        model = AISkill
        fields = '__all__'
        read_only_fields = (
            'id', 'owner', 'approved_by', 'approved_at', 'version',
            'created_at', 'updated_at',
        )

    def get_owner_name(self, obj):
        if not obj.owner:
            return None
        return obj.owner.get_full_name() or obj.owner.username

    def get_approved_by_name(self, obj):
        if not obj.approved_by:
            return None
        return obj.approved_by.get_full_name() or obj.approved_by.username

    def validate_input_schema(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("input_schema must be an object.")
        return value

    def validate_output_schema(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("output_schema must be an object.")
        return value

class AnalysisProtocolSerializer(serializers.ModelSerializer):
    class Meta:
        model = AnalysisProtocol
        fields = '__all__'


class AIFlowDefinitionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIFlowDefinition
        fields = '__all__'


class AIFlowVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIFlowVersion
        fields = '__all__'

class AIMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIMessage
        fields = ['id', 'role', 'content', 'thinking', 'data_points', 'applied_filters', 'created_at']

class AIConversationSerializer(serializers.ModelSerializer):
    messages = AIMessageSerializer(many=True, read_only=True)
    metadata = serializers.SerializerMethodField()

    def get_metadata(self, obj):
        metadata = dict(obj.metadata) if isinstance(obj.metadata, dict) else {}
        documents = metadata.get('chat_documents')
        if isinstance(documents, list):
            from .services.chat_documents import ChatDocumentEvidenceService
            metadata['chat_documents'] = [
                ChatDocumentEvidenceService.public_metadata(document)
                for document in documents
                if isinstance(document, dict)
            ]
        return metadata
    
    class Meta:
        model = AIConversation
        fields = ['id', 'title', 'metadata', 'created_at', 'updated_at', 'messages']
        read_only_fields = ['id', 'created_at', 'updated_at']
