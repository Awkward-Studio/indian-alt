from rest_framework import serializers
from .models import AIConversation, AIMessage, AIPersonality, AISkill, AnalysisProtocol, AIAuditLog

class AIAuditLogSerializer(serializers.ModelSerializer):
    personality_name = serializers.SerializerMethodField()
    skill_name = serializers.SerializerMethodField()
    
    class Meta:
        model = AIAuditLog
        fields = [
            'id', 'source_type', 'source_id', 'context_label', 'personality', 'personality_name', 
            'skill', 'skill_name', 'model_provider', 'model_used', 
            'request_duration_ms', 'tokens_used', 'is_success', 'status',
            'celery_task_id', 'created_at', 'error_message',
            'raw_response', 'raw_thinking', 'user_prompt', 'system_prompt', 'parsed_json'
        ]

    def get_personality_name(self, obj):
        return obj.personality.name if obj.personality else "Direct Inference"

    def get_skill_name(self, obj):
        return obj.skill.name if obj.skill else "General Analysis"

class AIPersonalitySerializer(serializers.ModelSerializer):
    class Meta:
        model = AIPersonality
        fields = '__all__'

class AISkillSerializer(serializers.ModelSerializer):
    class Meta:
        model = AISkill
        fields = '__all__'

class AnalysisProtocolSerializer(serializers.ModelSerializer):
    class Meta:
        model = AnalysisProtocol
        fields = '__all__'

class AIMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIMessage
        fields = ['id', 'role', 'content', 'thinking', 'data_points', 'applied_filters', 'created_at']

class AIConversationSerializer(serializers.ModelSerializer):
    messages = AIMessageSerializer(many=True, read_only=True)
    
    class Meta:
        model = AIConversation
        fields = ['id', 'title', 'created_at', 'updated_at', 'messages']
        read_only_fields = ['id', 'created_at', 'updated_at']
