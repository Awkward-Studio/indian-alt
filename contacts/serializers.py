from rest_framework import serializers
from .models import Contact
from deals.models import Deal
from deals.services.contact_linking import sync_contact_deal_links, sync_primary_contact_bank


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
    
    class Meta:
        model = Contact
        fields = (
            'id', 'name', 'email', 'designation', 'bank', 'bank_name',
            'location', 'phone', 'sector_coverage', 'rank', 'created_at',
            'ranking', 'primary_coverage_person', 'secondary_coverage_person',
            'total_deals_legacy', 'pipeline', 'follow_ups', 'last_meeting_date'
        )
        read_only_fields = ('id', 'created_at')
