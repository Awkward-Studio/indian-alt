from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory

from deals.models import Deal, DealFieldProvenance
from deals.serializers import DealListSerializer, DealSerializer
from deals.services.deal_creation import DealCreationService


class DealFieldProvenanceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='analyst@example.com')
        self.request = APIRequestFactory().patch('/api/deals/example/')
        self.request.user = self.user

    def test_human_update_records_latest_field_source(self):
        deal = Deal.objects.create(title='Example', sector='Old sector')
        serializer = DealSerializer(
            deal,
            data={'sector': 'Fintech'},
            partial=True,
            context={'request': self.request},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        record = DealFieldProvenance.objects.get(deal=deal, field_name='sector')
        self.assertEqual(record.source_type, DealFieldProvenance.SourceType.HUMAN)
        self.assertEqual(record.previous_value, 'Old sector')
        self.assertEqual(record.value, 'Fintech')

    def test_ai_update_records_changed_fields(self):
        deal = Deal.objects.create(title='Example')
        DealCreationService.apply_analysis_to_deal(
            deal,
            {'deal_model_data': {'industry': 'Financial Services'}},
        )

        record = DealFieldProvenance.objects.get(deal=deal, field_name='industry')
        self.assertEqual(record.source_type, DealFieldProvenance.SourceType.AI)
        self.assertEqual(record.value, 'Financial Services')

    def test_list_serializer_returns_only_latest_record_for_each_field(self):
        deal = Deal.objects.create(title='Example')
        DealFieldProvenance.objects.create(
            deal=deal,
            field_name='priority',
            source_type=DealFieldProvenance.SourceType.SHEET,
            value='Low',
        )
        DealFieldProvenance.objects.create(
            deal=deal,
            field_name='priority',
            source_type=DealFieldProvenance.SourceType.HUMAN,
            previous_value='Low',
            value='High',
            changed_by=self.user,
        )

        data = DealListSerializer(deal).data
        self.assertEqual(data['field_provenance']['priority']['source_type'], 'HUMAN')

