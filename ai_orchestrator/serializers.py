from rest_framework import serializers
from .models import AIConversation, AIMessage, AIPersonality, AISkill, AnalysisProtocol

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
