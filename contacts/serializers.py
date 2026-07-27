from rest_framework import serializers
from .models import Contact, WorkplaceVerificationSuggestion
from deals.models import Deal
from deals.services.contact_linking import sync_contact_deal_links, sync_primary_contact_bank
from contacts.services.banker_analytics import conversion_rate


class ContactLinkedDealSerializer(serializers.Serializer):
    deal_id = serializers.UUIDField()
    is_primary = serializers.BooleanField(default=False)


class ContactSerializer(serializers.ModelSerializer):
    # Include bank name for convenience without requiring nested serialization
    bank_name = serializers.CharField(source='bank.name', read_only=True)
    linked_deals = serializers.SerializerMethodField()
    linked_deals_payload = ContactLinkedDealSerializer(many=True, write_only=True, required=False)

    def get_linked_deals(self, obj):
        deals = Deal.objects.filter(primary_contact=obj).select_related('bank')
        additional = Deal.objects.filter(additional_contacts=obj).select_related('bank')
        combined = list(deals) + [deal for deal in additional if deal.id not in {item.id for item in deals}]
        return [
            {
                "deal_id": str(deal.id),
                "title": deal.title,
                "deal_status": deal.deal_status,
                "current_phase": deal.current_phase,
                "bank": str(deal.bank_id) if deal.bank_id else None,
                "bank_name": deal.bank.name if deal.bank else None,
                "is_primary": deal.primary_contact_id == obj.id,
            }
            for deal in combined
        ]

    def create(self, validated_data):
        linked_deals_payload = validated_data.pop('linked_deals_payload', None)
        contact = super().create(validated_data)
        if linked_deals_payload is not None:
            sync_contact_deal_links(contact, linked_deals_payload)
        sync_primary_contact_bank(contact)
        return contact

    def update(self, instance, validated_data):
        linked_deals_payload = validated_data.pop('linked_deals_payload', None)
        contact = super().update(instance, validated_data)
        if linked_deals_payload is not None:
            sync_contact_deal_links(contact, linked_deals_payload)
        sync_primary_contact_bank(contact)
        return contact
    
    class Meta:
        model = Contact
        fields = '__all__'
        read_only_fields = ('id', 'created_at')


class ContactListSerializer(serializers.ModelSerializer):
    bank_name = serializers.CharField(source='bank.name', read_only=True)
    deal_count = serializers.IntegerField(read_only=True)
    
    class Meta:
        model = Contact
        fields = (
            'id', 'name', 'email', 'designation', 'bank', 'bank_name',
            'location', 'phone', 'sector_coverage', 'rank', 'created_at',
            'ranking', 'primary_coverage_person', 'secondary_coverage_person',
            'total_deals_legacy', 'pipeline', 'follow_ups', 'last_meeting_date',
            'deal_count',
        )
        read_only_fields = ('id', 'created_at')


class BankerDealActivitySerializer(serializers.Serializer):
    deal_id = serializers.UUIDField(source='id')
    title = serializers.CharField(allow_null=True)
    deal_status = serializers.CharField(allow_null=True)
    current_phase = serializers.CharField()
    activity_date = serializers.DateField()
    received_at = serializers.DateField(allow_null=True)
    bank_id = serializers.UUIDField(source='bank.id', allow_null=True)
    bank_name = serializers.CharField(source='bank.name', allow_null=True)
    primary_contact_id = serializers.UUIDField(
        source='primary_contact.id',
        allow_null=True,
    )
    primary_contact_name = serializers.CharField(
        source='primary_contact.name',
        allow_null=True,
    )


class BankerAnalyticsSerializer(serializers.Serializer):
    entity_type = serializers.SerializerMethodField()
    id = serializers.UUIDField()
    name = serializers.CharField(allow_null=True)
    designation = serializers.CharField(allow_null=True)
    location = serializers.CharField(allow_null=True)
    sector_coverage = serializers.JSONField()
    bank_id = serializers.UUIDField(source='bank.id', allow_null=True)
    bank_name = serializers.CharField(source='bank.name', allow_null=True)
    total_deals_introduced = serializers.IntegerField()
    active_mandates = serializers.IntegerField()
    sourced_mandates = serializers.IntegerField()
    ic_mandates = serializers.IntegerField()
    converted_deals = serializers.IntegerField()
    passed_deals = serializers.IntegerField()
    conversion_rate = serializers.SerializerMethodField()
    last_deal_date = serializers.DateField(allow_null=True)
    meeting_count = serializers.IntegerField()
    last_interaction_at = serializers.DateTimeField(allow_null=True)
    activity_history = BankerDealActivitySerializer(many=True, required=False)

    def get_entity_type(self, _obj):
        return 'banker'

    def get_conversion_rate(self, obj):
        return conversion_rate(
            converted_deals=obj.converted_deals,
            total_deals=obj.total_deals_introduced,
        )


class BankAnalyticsSerializer(serializers.Serializer):
    entity_type = serializers.SerializerMethodField()
    id = serializers.UUIDField()
    name = serializers.CharField(allow_null=True)
    website_domain = serializers.CharField(allow_null=True)
    banker_count = serializers.IntegerField()
    total_deals_introduced = serializers.IntegerField()
    active_mandates = serializers.IntegerField()
    sourced_mandates = serializers.IntegerField()
    ic_mandates = serializers.IntegerField()
    converted_deals = serializers.IntegerField()
    passed_deals = serializers.IntegerField()
    conversion_rate = serializers.SerializerMethodField()
    last_deal_date = serializers.DateField(allow_null=True)
    activity_history = BankerDealActivitySerializer(many=True, required=False)

    def get_entity_type(self, _obj):
        return 'bank'

    def get_conversion_rate(self, obj):
        return conversion_rate(
            converted_deals=obj.converted_deals,
            total_deals=obj.total_deals_introduced,
        )


class WorkplaceVerificationSuggestionSerializer(serializers.ModelSerializer):
    contact_name = serializers.CharField(source='contact.name', read_only=True)
    requested_by_name = serializers.SerializerMethodField()
    reviewed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = WorkplaceVerificationSuggestion
        fields = (
            'id', 'contact', 'contact_name', 'old_bank_name',
            'old_designation', 'proposed_bank_name', 'proposed_designation',
            'source_url', 'source_title', 'source_snippet', 'source_domain',
            'search_query', 'confidence', 'retrieved_at', 'status',
            'accepted_fields', 'reviewer_comment', 'requested_by',
            'requested_by_name', 'reviewed_by', 'reviewed_by_name',
            'reviewed_at', 'audit_log', 'created_at',
        )
        read_only_fields = fields

    @staticmethod
    def _user_name(user):
        if not user:
            return None
        return user.get_full_name() or user.username

    def get_requested_by_name(self, obj):
        return self._user_name(obj.requested_by)

    def get_reviewed_by_name(self, obj):
        return self._user_name(obj.reviewed_by)


class WorkplaceVerificationReviewSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=('ACCEPT', 'REJECT'))
    accepted_fields = serializers.ListField(
        child=serializers.ChoiceField(choices=('bank', 'designation')),
        required=False,
        default=list,
    )
    comment = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=2000,
        default='',
    )

    def validate(self, attrs):
        if attrs['decision'] == 'ACCEPT' and not attrs['accepted_fields']:
            raise serializers.ValidationError(
                {'accepted_fields': 'Select at least one field to accept.'}
            )
        return attrs
