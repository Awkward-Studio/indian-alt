import os
import django

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from deals.models import DealDocument
from microsoft.models import Email

def align_indexing():
    docs = DealDocument.objects.filter(is_indexed=False)
    count = 0
    print(f"Checking {docs.count()} documents for indexing alignment...")
    
    for doc in docs:
        # Check if the parent deal is marked as indexed
        if doc.deal.is_indexed:
            doc.is_indexed = True
            doc.save(update_fields=['is_indexed'])
            count += 1
            print(f" [✓] Marked as Indexed: {doc.title}")
            continue
            
        # Check if any associated email for this deal is indexed
        indexed_email = Email.objects.filter(deal=doc.deal, is_indexed=True).exists()
        if indexed_email:
            doc.is_indexed = True
            doc.save(update_fields=['is_indexed'])
            count += 1
            print(f" [✓] Marked as Indexed (via Email): {doc.title}")

    print(f"\n--- ALIGNMENT COMPLETE ---")
    print(f"Successfully aligned {count} documents to active neural indexing.")

if __name__ == "__main__":
    align_indexing()
