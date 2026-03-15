from rest_framework import serializers
from .models import Deal, DealDocument, DealPhaseLog
from accounts.models import Profile
from api_requests.serializers import RequestSerializer
from ai_orchestrator.models import AIAuditLog


class DealPhaseLogSerializer(serializers.ModelSerializer):
    changed_by_name = serializers.CharField(source='changed_by.name', read_only=True)
    
    class Meta:
        model = DealPhaseLog
        fields = '__all__'
        read_only_fields = ('id', 'changed_at')


class DealDocumentSerializer(serializers.ModelSerializer):
    deal_title = serializers.CharField(source='deal.title', read_only=True)
    uploaded_by_name = serializers.CharField(source='uploaded_by.name', read_only=True)
    
    class Meta:
        model = DealDocument
        fields = '__all__'
        read_only_fields = ('id', 'created_at')


class DealSerializer(serializers.ModelSerializer):
    bank_name = serializers.CharField(source='bank.name', read_only=True)
    primary_contact_name = serializers.CharField(
        source='primary_contact.name',
        read_only=True
    )
    request_data = RequestSerializer(source='request', read_only=True)
    
    # Write-only field for linking email during creation
    source_email_id = serializers.UUIDField(write_only=True, required=False)
    contact_discovery = serializers.JSONField(write_only=True, required=False)
    analysis_json = serializers.JSONField(write_only=True, required=False)
    
    # Allow passing a list of Profile IDs (UUIDs)
    responsibility = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Profile.objects.all(),
        required=False
    )
    
    class Meta:
        model = Deal
        fields = '__all__'
        read_only_fields = ('id', 'created_at')


class DealDetailSerializer(DealSerializer):
    documents = DealDocumentSerializer(many=True, read_only=True)
    phase_logs = DealPhaseLogSerializer(many=True, read_only=True)
    
    class Meta(DealSerializer.Meta):
        fields = '__all__'


class DealListSerializer(serializers.ModelSerializer):
    bank_name = serializers.CharField(source='bank.name', read_only=True)
    primary_contact_name = serializers.CharField(
        source='primary_contact.name',
        read_only=True
    )
    
    class Meta:
        model = Deal
        fields = (
            'id', 'title', 'bank', 'bank_name', 'priority', 'current_phase', 'created_at',
            'deal_summary', 'industry', 'sector', 'primary_contact',
            'primary_contact_name', 'fund', 'themes', 'responsibility',
            'funding_ask', 'funding_ask_for', 'extracted_text',
            'thinking', 'ambiguities', 'deal_flow_decisions',
            'rejection_stage_id', 'rejection_reason', 'analysis_history'
        )
        read_only_fields = ('id', 'created_at')
