from rest_framework import serializers
from .models import Deal, DealDocument, DealPhaseLog, InitialAnalysisStatus
from accounts.models import Profile
from contacts.models import Contact
from api_requests.serializers import RequestSerializer
from .services.contact_linking import sync_deal_contact_links
from .services.document_artifacts import DocumentArtifactService
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
    in_latest_supplemental_analysis = serializers.SerializerMethodField()
    latest_supplemental_version = serializers.SerializerMethodField()
    artifact_status = serializers.SerializerMethodField()
    artifact_complete = serializers.SerializerMethodField()

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

    def _get_latest_supplemental_map(self, obj):
        cache = self.context.setdefault('_latest_supplemental_map', {})
        deal_id = str(obj.deal_id)
        if deal_id in cache:
            return cache[deal_id]

        analysis = obj.deal.latest_supplemental_analysis_record
        analysis_json = analysis.analysis_json if analysis and isinstance(analysis.analysis_json, dict) else {}
        metadata = analysis_json.get('metadata') if isinstance(analysis_json.get('metadata'), dict) else {}
        mapping = {
            'version': analysis.version if analysis else None,
            'by_file_id': {},
            'by_name': set(),
        }
        for file in metadata.get('analysis_input_files', []):
            file_id = file.get('file_id')
            file_name = file.get('file_name')
            if file_id:
                mapping['by_file_id'][str(file_id)] = file
            if file_name:
                mapping['by_name'].add(str(file_name).strip().lower())
        for file_name in metadata.get('documents_analyzed', []):
            mapping['by_name'].add(str(file_name).strip().lower())
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

    def get_in_latest_supplemental_analysis(self, obj):
        mapping = self._get_latest_supplemental_map(obj)
        if obj.onedrive_id and str(obj.onedrive_id) in mapping['by_file_id']:
            return True
        normalized_title = (obj.title or '').strip().lower()
        return normalized_title in mapping['by_name']

    def get_latest_supplemental_version(self, obj):
        mapping = self._get_latest_supplemental_map(obj)
        return mapping['version']

    def get_artifact_status(self, obj):
        return DocumentArtifactService.artifact_status(obj)

    def get_artifact_complete(self, obj):
        return DocumentArtifactService.artifact_complete(obj)
    
    class Meta:
        model = DealDocument
        fields = (
            'id', 'deal', 'deal_title', 'title', 'document_type', 
            'onedrive_id', 'file_url', 'is_indexed', 'is_ai_analyzed',
            'initial_analysis_status', 'initial_analysis_reason',
            'in_latest_supplemental_analysis', 'latest_supplemental_version',
            'artifact_status', 'artifact_complete',
            'normalized_text', 'evidence_json', 'source_map_json', 'table_json',
            'key_metrics_json', 'reasoning',
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
    primary_contact_details = serializers.SerializerMethodField()
    additional_contacts = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Contact.objects.all(),
        required=False
    )
    other_contact_details = serializers.SerializerMethodField()
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

    def get_other_contact_details(self, obj):
        contacts = obj.additional_contacts.select_related('bank').all()
        return [
            {
                "id": str(contact.id),
                "name": contact.name,
                "email": contact.email,
                "bank": str(contact.bank_id) if contact.bank_id else None,
                "bank_name": contact.bank.name if contact.bank else None,
            }
            for contact in contacts
        ]

    def get_primary_contact_details(self, obj):
        if not obj.primary_contact:
            return None
        contact = obj.primary_contact
        return {
            "id": str(contact.id),
            "name": contact.name,
            "email": contact.email,
            "bank": str(contact.bank_id) if contact.bank_id else None,
            "bank_name": contact.bank.name if contact.bank else None,
        }
    
    def create(self, validated_data):
        # Pop fields that are not on the Deal model anymore
        # Use a copy to ensure they remain available for perform_create hooks
        model_data = validated_data.copy()
        additional_contacts = model_data.pop('additional_contacts', None)
        legacy_other_contacts = model_data.pop('other_contacts', None)
        deal_status = model_data.get('deal_status')
        current_phase = model_data.get('current_phase')
        synced_status = current_phase or deal_status or '1: Deal Sourced'
        model_data['deal_status'] = synced_status
        model_data['current_phase'] = synced_status
        model_data.pop('source_email_id', None)
        model_data.pop('contact_discovery', None)
        model_data.pop('analysis_json', None)
        deal = super().create(model_data)

        if additional_contacts is None and legacy_other_contacts:
            additional_contacts = Contact.objects.filter(id__in=legacy_other_contacts)

        sync_deal_contact_links(
            deal,
            primary_contact=model_data.get('primary_contact'),
            primary_contact_provided='primary_contact' in model_data,
            additional_contacts=additional_contacts,
            additional_contacts_provided=additional_contacts is not None or legacy_other_contacts is not None,
        )
        return deal

    def update(self, instance, validated_data):
        model_data = validated_data.copy()
        additional_contacts = model_data.pop('additional_contacts', None)
        legacy_other_contacts = model_data.pop('other_contacts', None)
        if 'deal_status' in model_data or 'current_phase' in model_data:
            synced_status = model_data.get('current_phase') or model_data.get('deal_status') or instance.current_phase or instance.deal_status or '1: Deal Sourced'
            model_data['deal_status'] = synced_status
            model_data['current_phase'] = synced_status
        deal = super().update(instance, model_data)

        if additional_contacts is None and legacy_other_contacts is not None:
            additional_contacts = Contact.objects.filter(id__in=legacy_other_contacts)

        sync_deal_contact_links(
            deal,
            primary_contact=model_data.get('primary_contact', instance.primary_contact),
            primary_contact_provided='primary_contact' in model_data,
            additional_contacts=additional_contacts,
            additional_contacts_provided=additional_contacts is not None or legacy_other_contacts is not None,
        )
        return deal

    class Meta:
        model = Deal
        fields = '__all__'
        read_only_fields = ('id',)


class DealDetailSerializer(DealSerializer):
    documents = DealDocumentSerializer(many=True, read_only=True)
    phase_logs = DealPhaseLogSerializer(many=True, read_only=True)
    file_tree = serializers.SerializerMethodField()

    def get_file_tree(self, obj):
        from .services.folder_analysis import FolderAnalysisService

        return FolderAnalysisService.get_persisted_file_tree_for_deal(obj)
    
    class Meta:
        model = Deal
        fields = (
            'id', 'title', 'bank', 'bank_name', 'primary_contact',
            'primary_contact_name', 'primary_contact_details', 'priority', 'deal_status', 'fund', 'themes', 'responsibility',
            'funding_ask', 'funding_ask_for', 'current_phase', 'industry',
            'sector', 'is_female_led', 'management_meeting', 'business_proposal_stage',
            'ic_stage', 'city', 'country', 'created_at', 'deal_summary',
            'deal_details', 'company_details', 'comments', 'reasons_for_passing',
            'legacy_investment_bank', 'other_contacts', 'other_contact_details', 'additional_contacts', 'priority_rationale', 'state', 'request_data', 'documents',
            'phase_logs', 'source_onedrive_id',
            'source_drive_id', 'source_email_id', 'processing_status', 'processing_error',
            'file_tree',
        )
        read_only_fields = ('id',)


class DealHeavyFieldsSerializer(serializers.ModelSerializer):
    thinking = serializers.SerializerMethodField()
    ambiguities = serializers.SerializerMethodField()
    analysis_json = serializers.SerializerMethodField()
    initial_analysis = serializers.SerializerMethodField()
    current_analysis = serializers.SerializerMethodField()
    analysis_history = serializers.SerializerMethodField()

    def get_thinking(self, obj):
        return obj.thinking

    def get_ambiguities(self, obj):
        return obj.ambiguities if isinstance(obj.ambiguities, list) else []

    def get_analysis_json(self, obj):
        return obj.analysis_json if isinstance(obj.analysis_json, dict) else {}

    def get_initial_analysis(self, obj):
        return obj.initial_analysis

    def get_current_analysis(self, obj):
        return obj.current_analysis

    def get_analysis_history(self, obj):
        return obj.analysis_history if isinstance(obj.analysis_history, list) else []

    class Meta:
        model = Deal
        fields = (
            'id', 'extracted_text', 'thinking', 'ambiguities', 
            'analysis_json', 'initial_analysis', 'current_analysis', 
            'analysis_history'
        )


class DealListSerializer(serializers.ModelSerializer):
    bank_name = serializers.CharField(source='bank.name', read_only=True)
    primary_contact_name = serializers.CharField(
        source='primary_contact.name',
        read_only=True
    )
    
    class Meta:
        model = Deal
        fields = (
            'id', 'title', 'bank', 'bank_name', 'priority', 'deal_status', 'current_phase', 'created_at',
            'deal_summary', 'industry', 'sector', 'city', 'primary_contact',
            'primary_contact_name', 'fund', 'themes', 'responsibility',
            'funding_ask', 'funding_ask_for', 'legacy_investment_bank',
            'is_female_led', 'management_meeting', 'business_proposal_stage', 'ic_stage',
            'rejection_stage_id', 'rejection_reason'
        )
        read_only_fields = ('id', 'created_at')
