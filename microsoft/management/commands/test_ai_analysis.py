import os
import django
import json
from datetime import datetime
from django.utils import timezone
from django.core.management.base import BaseCommand
from microsoft.models import Email, EmailAccount
from ai_orchestrator.services.ai_processor import AIProcessorService

class Command(BaseCommand):
    help = 'Tests the AI analysis with a sample email'

    def handle(self, *args, **options):
        # 1. Ensure a mock account exists
        account, _ = EmailAccount.objects.get_or_create(
            email="test@india-alt.com"
        )

        # 2. Create a sample deal email
        subject = "Investment Proposal: SolarTech Solutions"
        body = """
Dear India Alternatives Team,

We are pleased to present SolarTech Solutions, a leading manufacturer of high-efficiency solar panels based in Pune. 

SolarTech has achieved a revenue of ₹120 Cr in FY24 with an EBITDA of ₹18 Cr (15% margin). We are seeking a primary investment of ₹40 Cr to expand our manufacturing capacity from 500MW to 1.2GW and strengthen our distribution network across South India.

Our founder, Dr. Ramesh Kumar, has 20 years of experience in the renewable energy sector.

We look forward to your interest.

Best regards,
SolarTech Solutions Team
        """
        
        email = Email.objects.create(
            email_account=account,
            graph_id="test-graph-id-" + str(datetime.now().timestamp()),
            subject=subject,
            from_email="ramesh@solartech.com",
            body_text=body,
            date_received=timezone.now(),
            is_read=True
        )

        self.stdout.write(self.style.SUCCESS(f"Created test email: {email.id}"))

        # 3. Trigger Analysis logic
        self.stdout.write("Starting AI Analysis...")
        
        ai_service = AIProcessorService()
        metadata = {
            'from_email': email.from_email,
            'subject': email.subject,
            'date_received': email.date_received.isoformat()
        }
        
        result = ai_service.process_content(
            content=email.body_text,
            metadata=metadata,
            source_id=str(email.id),
            source_type="email"
        )

        self.stdout.write(self.style.SUCCESS("\n--- AI ANALYSIS RESULT ---"))
        self.stdout.write(json.dumps(result, indent=2))
        
        if "error" not in result:
            email.is_processed = True
            email.save()
            self.stdout.write(self.style.SUCCESS("\nAnalysis completed successfully!"))
        else:
            self.stdout.write(self.style.ERROR("\nAnalysis failed!"))
