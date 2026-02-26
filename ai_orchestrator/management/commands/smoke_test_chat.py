import os
import json
from django.core.management.base import BaseCommand
from django.test import RequestFactory
from ai_orchestrator.views import DealChatView
from deals.models import Deal
from microsoft.models import Email, EmailAccount
from accounts.models import Profile
from django.contrib.auth.models import User

class Command(BaseCommand):
    help = 'Smoke test for the Deal Chat API'

    def handle(self, *args, **options):
        # 1. Setup mock data
        user, _ = User.objects.get_or_create(username="testadmin", is_staff=True)
        deal, _ = Deal.objects.get_or_create(
            title="Chat Test Corp",
            defaults={"deal_summary": "A test company for chat forensics."}
        )
        
        # 2. Mock request
        factory = RequestFactory()
        url = f'/api/ai/deal-chat/?deal_id={deal.id}'
        request = factory.post(url, data=json.dumps({"message": "Summarize this deal for me."}), content_type='application/json')
        request.user = user
        
        # 3. Call view
        self.stdout.write("Testing DealChatView.post()...")
        view = DealChatView.as_view()
        try:
            response = view(request)
            self.stdout.write(f"Response Status: {response.status_code}")
            self.stdout.write(f"Response Data: {json.dumps(response.data, indent=2)}")
            if response.status_code == 200:
                self.stdout.write(self.style.SUCCESS("Endpoint is LIVE and responding!"))
            else:
                self.stdout.write(self.style.ERROR("Endpoint responded with error."))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Endpoint CRASHED: {str(e)}"))
