from rest_framework import serializers
from .models import Deal, DealDocument, DealPhaseLog, InitialAnalysisStatus
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
    initial_analysis_status = serializers.SerializerMethodField()
    initial_analysis_reason = serializers.SerializerMethodField()

    def _get_initial_analysis_map(self, obj):
        cache = self.context.setdefault('_initial_analysis_map', {})
        deal_id = str(obj.deal_id)
        if deal_id in cache:
            return cache[deal_id]

        analysis = obj.deal.analyses.filter(version=1).first()
        metadata = (analysis.analysis_json or {}).get('metadata', {}) if analysis else {}
        mapping = {
            'passed_by_file_id': {},
            'passed_by_name': {},
            'failed_by_file_id': {},
            'failed_by_name': {},
        }
        for file in metadata.get('analysis_input_files', []) or metadata.get('passed_files', []):
            file_id = file.get('file_id')
            file_name = file.get('file_name')
            if file_id:
                mapping['passed_by_file_id'][str(file_id)] = file
            if file_name:
                mapping['passed_by_name'][str(file_name).strip().lower()] = file
        for file in metadata.get('failed_files', []):
            file_id = file.get('file_id')
            file_name = file.get('file_name')
            if file_id:
                mapping['failed_by_file_id'][str(file_id)] = file
            if file_name:
                mapping['failed_by_name'][str(file_name).strip().lower()] = file
        cache[deal_id] = mapping
        return mapping

    def get_initial_analysis_status(self, obj):
        if obj.initial_analysis_status and obj.initial_analysis_status != InitialAnalysisStatus.NOT_SELECTED:
            return obj.initial_analysis_status

        mapping = self._get_initial_analysis_map(obj)
        if obj.onedrive_id and str(obj.onedrive_id) in mapping['passed_by_file_id']:
            return InitialAnalysisStatus.SELECTED_AND_ANALYZED
        if obj.onedrive_id and str(obj.onedrive_id) in mapping['failed_by_file_id']:
            return InitialAnalysisStatus.SELECTED_FAILED

        normalized_title = (obj.title or '').strip().lower()
        if normalized_title in mapping['passed_by_name']:
            return InitialAnalysisStatus.SELECTED_AND_ANALYZED
        if normalized_title in mapping['failed_by_name']:
            return InitialAnalysisStatus.SELECTED_FAILED
        return InitialAnalysisStatus.NOT_SELECTED

    def get_initial_analysis_reason(self, obj):
        if obj.initial_analysis_reason:
            return obj.initial_analysis_reason

        mapping = self._get_initial_analysis_map(obj)
        if obj.onedrive_id and str(obj.onedrive_id) in mapping['failed_by_file_id']:
            return mapping['failed_by_file_id'][str(obj.onedrive_id)].get('reason')
        normalized_title = (obj.title or '').strip().lower()
        if normalized_title in mapping['failed_by_name']:
            return mapping['failed_by_name'][normalized_title].get('reason')
        return None
    
    class Meta:
        model = DealDocument
        fields = (
            'id', 'deal', 'deal_title', 'title', 'document_type', 
            'onedrive_id', 'file_url', 'is_indexed', 'is_ai_analyzed',
            'initial_analysis_status', 'initial_analysis_reason',
            'extraction_mode', 'transcription_status', 'chunking_status',
            'last_transcribed_at', 'last_chunked_at',
            'created_at', 'uploaded_by', 'uploaded_by_name'
        )
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
    
    def create(self, validated_data):
        # Pop fields that are not on the Deal model anymore
        # Use a copy to ensure they remain available for perform_create hooks
        model_data = validated_data.copy()
        model_data.pop('source_email_id', None)
        model_data.pop('contact_discovery', None)
        model_data.pop('analysis_json', None)
        return super().create(model_data)

    class Meta:
        model = Deal
        fields = '__all__'
        read_only_fields = ('id', 'created_at')


class DealDetailSerializer(DealSerializer):
    documents = DealDocumentSerializer(many=True, read_only=True)
    phase_logs = DealPhaseLogSerializer(many=True, read_only=True)
    thinking = serializers.SerializerMethodField()
    ambiguities = serializers.SerializerMethodField()
    analysis_json = serializers.SerializerMethodField()
    analysis_history = serializers.SerializerMethodField()
    latest_phase_readiness_check = serializers.SerializerMethodField()

    def get_thinking(self, obj):
        return obj.thinking

    def get_ambiguities(self, obj):
        return obj.ambiguities if isinstance(obj.ambiguities, list) else []

    def get_analysis_json(self, obj):
        return obj.analysis_json if isinstance(obj.analysis_json, dict) else {}

    def get_analysis_history(self, obj):
        return obj.analysis_history if isinstance(obj.analysis_history, list) else []

    def get_latest_phase_readiness_check(self, obj):
        from .services.phase_readiness import (
            DealPhaseReadinessService,
            PHASE_READINESS_SOURCE_TYPE,
        )

        log = AIAuditLog.objects.filter(
            source_type=PHASE_READINESS_SOURCE_TYPE,
            source_id=str(obj.id),
        ).order_by("-created_at").first()
        return DealPhaseReadinessService.serialize_audit_log(log)
    
    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Truncate potentially massive text fields for initial render
        if data.get('extracted_text') and len(data['extracted_text']) > 20000:
            data['extracted_text'] = data['extracted_text'][:20000]
        if data.get('thinking') and len(data['thinking']) > 50000:
            data['thinking'] = data['thinking'][:50000] + "\n\n... [Thinking trace truncated] ..."
        return data
    
    class Meta(DealSerializer.Meta):
        fields = '__all__'


class DealListSerializer(serializers.ModelSerializer):
    bank_name = serializers.CharField(source='bank.name', read_only=True)
    primary_contact_name = serializers.CharField(
        source='primary_contact.name',
        read_only=True
    )
    
    def get_extracted_text(self, obj):
        if obj.extracted_text and len(obj.extracted_text) > 20000:
            return obj.extracted_text[:20000]
        return obj.extracted_text

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
