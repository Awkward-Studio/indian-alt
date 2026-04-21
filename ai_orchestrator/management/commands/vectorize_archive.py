from django.core.management.base import BaseCommand
from deals.models import Deal
from microsoft.models import Email
from ai_orchestrator.services.embedding_processor import EmbeddingService

class Command(BaseCommand):
    help = 'Vectorizes existing deals and emails for the RAG system'

    def add_arguments(self, parser):
        parser.add_argument(
            '--deal_id',
            type=str,
            help='Specific Deal ID to vectorize',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Re-vectorize even if already indexed',
        )

    def handle(self, *args, **options):
        deal_id = options.get('deal_id')
        force = options.get('force')
        
        embed_service = EmbeddingService()
        
        # 1. Process Deals
        deals = Deal.objects.all()
        if deal_id:
            deals = deals.filter(id=deal_id)
        if not force:
            deals = deals.filter(is_indexed=False)
            
        self.stdout.write(f"Found {deals.count()} deals to vectorize...")
        for deal in deals:
            self.stdout.write(f"Vectorizing Deal: {deal.title} ({deal.id})...")
            success = embed_service.vectorize_deal(deal)
            if success:
                self.stdout.write(self.style.SUCCESS(f"Successfully vectorized deal {deal.id}"))
            else:
                profile_success = embed_service.refresh_deal_profile(deal)
                if profile_success:
                    self.stdout.write(self.style.SUCCESS(f"Refreshed retrieval profile for deal {deal.id}"))
                else:
                    self.stdout.write(self.style.WARNING(f"Skipped deal {deal.id} (no summary/profile text)"))

        # 2. Process Emails
        emails = Email.objects.filter(deal__isnull=False).exclude(extracted_text__isnull=True).exclude(extracted_text='')
        if deal_id:
            emails = emails.filter(deal_id=deal_id)
        if not force:
            emails = emails.filter(is_indexed=False)

        self.stdout.write(f"Found {emails.count()} emails to vectorize...")
        for email in emails:
            self.stdout.write(f"Vectorizing Email: {email.subject} ({email.id})...")
            success = embed_service.vectorize_email(email)
            if success:
                self.stdout.write(self.style.SUCCESS(f"Successfully vectorized email {email.id}"))
            else:
                self.stdout.write(self.style.ERROR(f"Failed to vectorize email {email.id}"))

        self.stdout.write(self.style.SUCCESS("Vectorization process complete!"))
