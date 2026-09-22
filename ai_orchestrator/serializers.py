import uuid

from django.db.models import Q
from rest_framework import serializers
from .models import AIConversation, AIMessage, AIPersonality, AISkill, AnalysisProtocol, AIAuditLog, AIFlowDefinition, AIFlowVersion


def _uuid_strings(values):
    valid = set()
    for value in values:
        try:
            valid.add(str(uuid.UUID(str(value))))
        except (TypeError, ValueError, AttributeError):
            continue
    return valid


def _ordered_uuid_strings(values):
    valid = []
    for value in values:
        try:
            normalized = str(uuid.UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            continue
        if normalized not in valid:
            valid.append(normalized)
    return valid


def audit_deal_names(logs):
    """Resolve deal ownership for a page of heterogeneous audit records."""
    from deals.models import Deal, DealDocument

    logs = list(logs)
    source_ids = _uuid_strings(log.source_id for log in logs if log.source_id)
    parent_ids = _uuid_strings(
        (log.source_metadata or {}).get('vdr_parent_audit_id')
        or (log.source_metadata or {}).get('parent_audit_log_id')
        for log in logs
    ) | source_ids
    parents = {
        str(parent.id): parent
        for parent in AIAuditLog.objects.filter(id__in=parent_ids).only(
            'id', 'source_id', 'source_metadata',
        )
    }

    document_ids = set(source_ids)
    for parent in parents.values():
        document_ids.update(_uuid_strings([parent.source_id]))
    document_deals = {
        str(document.id): str(document.deal_id)
        for document in DealDocument.objects.filter(id__in=document_ids).only('id', 'deal_id')
    }

    candidates_by_log = {}
    all_deal_ids = set()
    for log in logs:
        metadata = log.source_metadata or {}
        match = metadata.get('match') if isinstance(metadata.get('match'), dict) else {}
        source_id = str(log.source_id or '')
        parent_id = str(
            metadata.get('vdr_parent_audit_id')
            or metadata.get('parent_audit_log_id')
            or source_id
        )
        parent = parents.get(parent_id)
        parent_metadata = parent.source_metadata or {} if parent else {}
        parent_match = (
            parent_metadata.get('match')
            if isinstance(parent_metadata.get('match'), dict)
            else {}
        )
        parent_source_id = str(parent.source_id or '') if parent else ''
        candidates = _ordered_uuid_strings([
            metadata.get('deal_id'),
            match.get('deal_id'),
            document_deals.get(source_id),
            parent_metadata.get('deal_id'),
            parent_match.get('deal_id'),
            document_deals.get(parent_source_id),
            parent_source_id,
            source_id,
        ])
        candidates_by_log[str(log.id)] = candidates
        all_deal_ids.update(candidates)

    deals = {
        str(deal.id): deal.title or 'Untitled deal'
        for deal in Deal.objects.filter(id__in=all_deal_ids).only('id', 'title')
    }
    return {
        audit_id: next((deals[deal_id] for deal_id in candidates if deal_id in deals), None)
        for audit_id, candidates in candidates_by_log.items()
    }


def related_audits(obj):
    query = (
        Q(source_metadata__vdr_parent_audit_id=str(obj.pk))
        | Q(source_metadata__parent_audit_log_id=str(obj.pk))
    )
    if obj.celery_task_id:
        query |= Q(celery_task_id=obj.celery_task_id)
    return AIAuditLog.objects.filter(query).exclude(pk=obj.pk).order_by('created_at')


def token_usage(log, children=()):
    own_method = (
        'estimated' if log.token_count_is_estimate is True
        else 'provider' if log.token_count_is_estimate is False
        else 'legacy'
    )
    child_rows = [child for child in children if child.tokens_used is not None]
    methods = {
        'estimated' if child.token_count_is_estimate is True
        else 'provider' if child.token_count_is_estimate is False
        else 'legacy'
        for child in child_rows
    }
    aggregate_method = next(iter(methods)) if len(methods) == 1 else 'mixed' if methods else None

    def summed(field):
        values = [getattr(child, field) for child in child_rows if getattr(child, field) is not None]
        return sum(values) if values else None

    return {
        'input_tokens': log.input_tokens,
        'output_tokens': log.output_tokens,
        'total_tokens': log.tokens_used,
        'method': own_method,
        'aggregate': {
            'input_tokens': summed('input_tokens'),
            'output_tokens': summed('output_tokens'),
            'total_tokens': summed('tokens_used'),
            'method': aggregate_method,
            'child_count': len(child_rows),
        } if child_rows else None,
    }


class AIAuditChildLogSerializer(serializers.ModelSerializer):
    token_usage = serializers.SerializerMethodField()
    segment_index = serializers.SerializerMethodField()
    segment_count = serializers.SerializerMethodField()

    class Meta:
        model = AIAuditLog
        fields = [
            'id', 'source_type', 'source_id', 'context_label', 'model_provider',
            'model_used', 'status', 'is_success', 'created_at', 'completed_at',
            'request_duration_ms', 'tokens_used', 'input_tokens', 'output_tokens',
            'token_count_is_estimate', 'token_usage', 'error_message', 'worker_logs',
            'parsed_json', 'source_metadata', 'segment_index', 'segment_count',
        ]

    def get_token_usage(self, obj):
        return token_usage(obj)

    def get_segment_index(self, obj):
        return (obj.source_metadata or {}).get('segment_index')

    def get_segment_count(self, obj):
        return (obj.source_metadata or {}).get('segment_count')

class AIAuditLogSerializer(serializers.ModelSerializer):
    personality_name = serializers.SerializerMethodField()
    skill_name = serializers.SerializerMethodField()
    requested_by_name = serializers.SerializerMethodField()
    child_audits = serializers.SerializerMethodField()
    token_usage = serializers.SerializerMethodField()
    deal_name = serializers.SerializerMethodField()
    
    class Meta:
        model = AIAuditLog
        fields = [
            'id', 'source_type', 'source_id', 'context_label', 'personality', 'personality_name', 
            'skill', 'skill_name', 'model_provider', 'model_used', 
            'requested_by', 'requested_by_name', 'skill_version',
            'pipeline', 'pipeline_stage', 'prompt_revision', 'skill_revision',
            'request_duration_ms', 'tokens_used', 'input_tokens', 'output_tokens',
            'token_count_is_estimate', 'token_usage', 'is_success', 'status',
            'celery_task_id', 'created_at', 'completed_at', 'error_message', 'worker_logs',
            'raw_response', 'raw_thinking', 'user_prompt', 'system_prompt', 'parsed_json',
            'source_metadata', 'child_audits', 'deal_name'
        ]

    def get_child_audits(self, obj):
        if not self.context.get('include_child_audits'):
            return []
        return AIAuditChildLogSerializer(related_audits(obj), many=True).data

    def get_token_usage(self, obj):
        children = related_audits(obj) if self.context.get('include_child_audits') else ()
        return token_usage(obj, children)

    def get_deal_name(self, obj):
        cache = self.context.get('_audit_deal_names')
        if cache is None:
            instances = self.parent.instance if getattr(self.parent, 'many', False) else [obj]
            cache = audit_deal_names(instances)
            self.context['_audit_deal_names'] = cache
        return cache.get(str(obj.id))

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not self.context.get('include_child_audits'):
            data.pop('child_audits', None)
        return data

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
