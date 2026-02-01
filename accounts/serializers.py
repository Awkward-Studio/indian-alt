from rest_framework import serializers
from .models import Profile


class ProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Profile
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')


class ProfileListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Profile
        fields = (
            'id', 'name', 'email', 'image_url', 'is_admin',
            'initials', 'is_disabled', 'created_at'
        )
        read_only_fields = ('id', 'created_at', 'updated_at')
