from rest_framework import serializers
from .models import Deal
from accounts.models import Profile


class DealSerializer(serializers.ModelSerializer):
    bank_name = serializers.CharField(source='bank.name', read_only=True)
    primary_contact_name = serializers.CharField(
        source='primary_contact.name',
        read_only=True
    )
    request_status = serializers.CharField(source='request.status', read_only=True)
    
    # Write-only field for linking email during creation
    source_email_id = serializers.UUIDField(write_only=True, required=False)
    
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


class DealListSerializer(serializers.ModelSerializer):
    bank_name = serializers.CharField(source='bank.name', read_only=True)
    primary_contact_name = serializers.CharField(
        source='primary_contact.name',
        read_only=True
    )
    
    class Meta:
        model = Deal
        fields = (
            'id', 'title', 'bank', 'bank_name', 'priority', 'created_at',
            'deal_summary', 'industry', 'sector', 'primary_contact',
            'primary_contact_name', 'fund', 'themes', 'responsibility',
            'funding_ask', 'funding_ask_for', 'extracted_text'
        )
        read_only_fields = ('id', 'created_at')
