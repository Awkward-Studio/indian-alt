from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from .models import Meeting, MeetingContact, MeetingNote, MeetingProfile
from contacts.serializers import ContactListSerializer
from accounts.serializers import ProfileListSerializer
from deals.serializers import DealListSerializer


def get_contact_queryset():
    """Lazy import to avoid circular dependencies."""
    from contacts.models import Contact
    return Contact.objects.all()


def get_profile_queryset():
    """Lazy import to avoid circular dependencies."""
    from accounts.models import Profile
    return Profile.objects.all()


def get_deal_queryset():
    """Lazy import to avoid circular dependencies."""
    from deals.models import Deal
    return Deal.objects.all()


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
        queryset=get_contact_queryset(),
        write_only=True,
        required=False,
        source='contacts'
    )
    profile_ids = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=get_profile_queryset(),
        write_only=True,
        required=False,
        source='profiles'
    )
    
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


class MeetingNoteSerializer(serializers.ModelSerializer):
    deals = DealListSerializer(many=True, read_only=True)
    deal_ids = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=get_deal_queryset(),
        write_only=True,
        required=False,
        source='deals',
    )
    contacts = ContactListSerializer(many=True, read_only=True)
    contact_ids = serializers.PrimaryKeyRelatedField(many=True, queryset=get_contact_queryset(), write_only=True, required=False, source='contacts')

    class Meta:
        model = MeetingNote
        fields = '__all__'
        read_only_fields = (
            'id',
            'created_at',
            'updated_at',
            'is_indexed',
            'chunk_count',
            'embedding_error',
        )
        extra_kwargs = {
            'body': {'required': False, 'allow_blank': True},
            'summary': {'required': False, 'allow_blank': True},
        }

    @staticmethod
    def _sync_contact_interactions(note):
        from contacts.models import ContactInteraction
        contact_ids = set(note.contacts.values_list('id', flat=True))
        ContactInteraction.objects.filter(meeting_note=note).exclude(contact_id__in=contact_ids).delete()
        occurred_at = note.meeting_at or note.created_at or timezone.now()
        deal = note.deals.first()
        for contact in note.contacts.all():
            ContactInteraction.objects.update_or_create(
                contact=contact,
                meeting_note=note,
                defaults={'kind': ContactInteraction.Kind.MEETING, 'occurred_at': occurred_at, 'notes': note.title, 'deal': deal},
            )

    def validate(self, attrs):
        body = attrs.get('body', getattr(self.instance, 'body', '') if self.instance else '')
        summary = attrs.get('summary', getattr(self.instance, 'summary', '') if self.instance else '')
        if not str(body or '').strip() and not str(summary or '').strip():
            raise ValidationError({'body': 'Provide either a transcript or a summary.'})
        return attrs

    def _vectorize(self, note):
        from ai_orchestrator.services.embedding_processor import EmbeddingService

        if not EmbeddingService().vectorize_meeting_note(note):
            note.refresh_from_db(fields=['embedding_error'])
            raise ValidationError({
                'embedding_error': note.embedding_error or 'Meeting note was saved, but embeddings were not created.'
            })

    def create(self, validated_data):
        try:
            with transaction.atomic():
                deals = validated_data.pop('deals', [])
                contacts = validated_data.pop('contacts', [])
                note = MeetingNote.objects.create(**validated_data)
                note.deals.set(deals)
                note.contacts.set(contacts)
                self._sync_contact_interactions(note)
                self._vectorize(note)
                note.refresh_from_db(fields=['is_indexed', 'chunk_count', 'embedding_error'])
                return note
        except ValidationError:
            raise
        except Exception as e:
            raise ValidationError({'error': f'Failed to create meeting note: {str(e)}'})

    def update(self, instance, validated_data):
        try:
            with transaction.atomic():
                deals = validated_data.pop('deals', None)
                contacts = validated_data.pop('contacts', None)

                for attr, value in validated_data.items():
                    setattr(instance, attr, value)
                instance.save()

                if deals is not None:
                    instance.deals.set(deals)
                if contacts is not None:
                    instance.contacts.set(contacts)
                self._sync_contact_interactions(instance)

                self._vectorize(instance)
                instance.refresh_from_db(fields=['is_indexed', 'chunk_count', 'embedding_error'])
                return instance
        except ValidationError:
            raise
        except Exception as e:
            raise ValidationError({'error': f'Failed to update meeting note: {str(e)}'})
