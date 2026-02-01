from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from .models import Meeting, MeetingContact, MeetingProfile
from contacts.serializers import ContactListSerializer
from accounts.serializers import ProfileListSerializer


class MeetingContactSerializer(serializers.ModelSerializer):
    contact_name = serializers.CharField(source='contact.name', read_only=True)
    contact_email = serializers.EmailField(source='contact.email', read_only=True)
    
    class Meta:
        model = MeetingContact
        fields = '__all__'
        read_only_fields = ('id',)


class MeetingProfileSerializer(serializers.ModelSerializer):
    profile_name = serializers.CharField(source='profile.name', read_only=True)
    profile_email = serializers.EmailField(source='profile.email', read_only=True)
    
    class Meta:
        model = MeetingProfile
        fields = '__all__'
        read_only_fields = ('id',)


class MeetingSerializer(serializers.ModelSerializer):
    # Read-only nested serializers for displaying related objects
    contacts = ContactListSerializer(many=True, read_only=True, source='meeting_contacts')
    profiles = serializers.SerializerMethodField()
    # Write-only fields for accepting IDs during create/update
    contact_ids = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=None,  # Set in __init__ to avoid circular imports
        write_only=True,
        required=False,
        source='contacts'
    )
    profile_ids = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=None,  # Set in __init__ to avoid circular imports
        write_only=True,
        required=False,
        source='profiles'
    )
    
    def __init__(self, *args, **kwargs):
        # Lazy import to avoid circular dependency issues
        super().__init__(*args, **kwargs)
        from contacts.models import Contact
        from accounts.models import Profile
        self.fields['contact_ids'].queryset = Contact.objects.all()
        self.fields['profile_ids'].queryset = Profile.objects.all()
    
    class Meta:
        model = Meeting
        fields = '__all__'
        read_only_fields = ('id', 'created_at')
    
    def get_profiles(self, obj):
        try:
            return ProfileListSerializer(obj.meeting_profiles.all(), many=True).data
        except Exception:
            return []
    
    def create(self, validated_data):
        # Handle M2M relationships separately since they require the meeting to exist first
        try:
            contacts = validated_data.pop('contacts', [])
            profiles = validated_data.pop('profiles', [])
            meeting = Meeting.objects.create(**validated_data)
            meeting.contacts.set(contacts)
            meeting.profiles.set(profiles)
            return meeting
        except Exception as e:
            raise ValidationError({'error': f'Failed to create meeting: {str(e)}'})
    
    def update(self, instance, validated_data):
        # Only update M2M relationships if they're provided (None means don't change)
        try:
            contacts = validated_data.pop('contacts', None)
            profiles = validated_data.pop('profiles', None)
            
            for attr, value in validated_data.items():
                setattr(instance, attr, value)
            instance.save()
            
            # Only update relationships if explicitly provided
            if contacts is not None:
                instance.contacts.set(contacts)
            if profiles is not None:
                instance.profiles.set(profiles)
            
            return instance
        except Exception as e:
            raise ValidationError({'error': f'Failed to update meeting: {str(e)}'})
