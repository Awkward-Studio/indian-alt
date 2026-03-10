import os
import django

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from deals.models import Deal, DealDocument, DocumentType
from microsoft.models import Email

def sync_historical_docs():
    deals = Deal.objects.all()
    count = 0
    print(f"Checking {deals.count()} deals for missing document records...")
    
    for deal in deals:
        emails = Email.objects.filter(deal=deal)
        for email in emails:
            if email.attachments:
                for att in email.attachments:
                    name = att.get('name', 'Unknown Document')
                    # Avoid duplicates
                    if not DealDocument.objects.filter(deal=deal, title=name).exists():
                        name_low = name.lower()
                        dt = DocumentType.OTHER
                        if any(x in name_low for x in ['fin', 'mis', 'mod', 'p&l']): 
                            dt = DocumentType.FINANCIALS
                        elif any(x in name_low for x in ['leg', 'sha', 'ssa', 'agreement']): 
                            dt = DocumentType.LEGAL
                        elif any(x in name_low for x in ['deck', 'pitch', 'teaser']): 
                            dt = DocumentType.PITCH_DECK
                        
                        DealDocument.objects.create(
                            deal=deal,
                            title=name,
                            document_type=dt,
                            onedrive_id=att.get('id'),
                            created_at=email.created_at # Match email date
                        )
                        print(f" [+] Synced: {name} ({dt}) for Deal: {deal.title}")
                        count += 1
    
    print(f"\n--- SUCCESS ---")
    print(f"Retroactively synced {count} institutional artifacts into the Documents Hub.")

if __name__ == "__main__":
    sync_historical_docs()
