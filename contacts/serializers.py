from rest_framework import serializers
from .models import Contact


class ContactSerializer(serializers.ModelSerializer):
    # Include bank name for convenience without requiring nested serialization
    bank_name = serializers.CharField(source='bank.name', read_only=True)
    
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
            'location', 'phone', 'sector_coverage', 'rank', 'created_at'
        )
        read_only_fields = ('id', 'created_at')
