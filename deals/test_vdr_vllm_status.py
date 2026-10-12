from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate
from deals.models import Deal
from deals.views import DealViewSet


class VllmStatusTests(TestCase):
    @override_settings(AI_SLOT_TRANSPORT_ENABLED=False)
    @patch('deals.views.requests.get')
    @patch('deals.views.cache.get', return_value=None)
    def test_vdr_status_does_not_poll_llama_cpp_slots(self, cache, get):
        user=User.objects.create_user(username='status-admin',is_staff=True,is_superuser=True)
        deal=Deal.objects.create(title='Status regression')
        request=APIRequestFactory().get('/api/deals/%s/vdr-status/' % deal.id)
        force_authenticate(request,user=user)
        response=DealViewSet.as_view({'get':'vdr_status'})(request,pk=str(deal.id))
        self.assertEqual(response.status_code,200)
        get.assert_not_called()
