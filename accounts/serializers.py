from rest_framework import serializers
from django.contrib.auth.models import User
from django.db import transaction
from .models import Profile


class ProfileSerializer(serializers.ModelSerializer):
    """Full serializer for Profile with User synchronization."""
    
    password = serializers.CharField(write_only=True, required=False)
    
    class Meta:
        model = Profile
        fields = (
            'id', 'user', 'name', 'email', 'image_url', 'is_admin',
            'initials', 'is_disabled', 'password', 'created_at', 'updated_at'
        )
        read_only_fields = ('id', 'user', 'created_at', 'updated_at')

    def create(self, validated_data):
        password = validated_data.pop('password', None)
        email = validated_data.get('email')
        name = validated_data.get('name', '')
        is_admin = validated_data.get('is_admin', False)

        with transaction.atomic():
            # 1. Create or get the Django User
            # We use email as the username for consistency
            user, created = User.objects.get_or_create(
                username=email,
                defaults={
                    'email': email,
                    'is_staff': is_admin,
                    'first_name': name.split()[0] if name else '',
                    'last_name': ' '.join(name.split()[1:]) if name and len(name.split()) > 1 else '',
                }
            )
            
            if password:
                user.set_password(password)
                user.save()
            elif created:
                # Set a random password if none provided for a new user
                user.set_unusable_password()
                user.save()

            # 2. Create the Profile linked to the User
            validated_data['user'] = user
            profile = Profile.objects.create(**validated_data)
            return profile

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        email = validated_data.get('email', instance.email)
        name = validated_data.get('name', instance.name)
        is_admin = validated_data.get('is_admin', instance.is_admin)

        with transaction.atomic():
            # Update associated User if it exists
            if instance.user:
                user = instance.user
                user.email = email
                user.username = email
                user.is_staff = is_admin
                if name:
                    parts = name.split()
                    user.first_name = parts[0]
                    user.last_name = ' '.join(parts[1:]) if len(parts) > 1 else ''
                
                if password:
                    user.set_password(password)
                
                user.save()
            
            return super().update(instance, validated_data)


class ProfileListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Profile
        fields = (
            'id', 'name', 'email', 'image_url', 'is_admin',
            'initials', 'is_disabled', 'created_at'
        )
        read_only_fields = ('id', 'created_at', 'updated_at')
