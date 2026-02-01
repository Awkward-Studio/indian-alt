from rest_framework import serializers
from .models import Version


class VersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Version
        fields = '__all__'
        read_only_fields = ('id', 'created_at')
