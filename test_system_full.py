import os
import django
import json
import uuid
from datetime import datetime

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from microsoft.models import Email, EmailAccount
from ai_orchestrator.services.ai_processor import AIProcessorService
from deals.models import Deal
from accounts.models import Profile
from django.test import RequestFactory
from deals.views import DealViewSet
from deals.serializers import DealSerializer

def test_system():
    print("--- STARTING 5-MANDATE FORENSIC STRESS TEST (V3) ---")
    
    profile = Profile.objects.first()
    if not profile:
        profile = Profile.objects.create(name='Test Analyst', email='analyst@test.com', is_admin=True)
    
    email_account, _ = EmailAccount.objects.get_or_create(email='test-system@india-alt.com')

    samples = [
        {
            "id": "CASE-01",
            "subject": "Project Zeta: UPI-based Cross-border Payments Disruptor",
            "from": "neil.shah@jmfl.com",
            "body": """Hi Team, Sharing a teaser for "PayGlobe". They are doing $15M ARR in the SG-India corridor. 
            Seeking a Series B of $25 Million (~INR 210 Crores) at a $120M post-money cap. 
            Regards, Neil Shah, Director - Fintech, JM Financial, Mumbai. www.jmfl.com"""
        },
        {
            "id": "CASE-02",
            "subject": "Investment Opportunity: Sustainable Ethic-Wear Brand - 'Maya'",
            "from": "ananya.r@greenadvise.in",
            "body": """Dear Omi, Maya is a female-led sustainable fashion brand. EBITDA positive. 
            Seeking INR 15 Crores for retail expansion. Ananya Rao, Founder, GreenAdvise Partners, Bangalore. www.greenadvise.in"""
        },
        {
            "id": "CASE-03",
            "subject": "Teaser: Edge-AI for Industrial Automation (Confidential)",
            "from": "tech.deals@investindia.org",
            "body": """Proprietary mandate for "AutoBrain AI". Pune-based. Patents pending. 
            Looking for structured debt/equity mix of $10M. Note: Product is in beta, no commercial revenue yet.
            Rajiv Kumar, InvestIndia Advisors. www.investindia.org"""
        },
        {
            "id": "CASE-04",
            "subject": "Project Pulse: Secondary Opportunity in Multi-Speciality Hospital Chain",
            "from": "s.singh@edelweiss.in",
            "body": """Secondary sale of 15% stake in "HealthFirst Hospitals". Tier-2 focus. 
            INR 450 Crores valuation. 1200 beds across 5 cities.
            Siddharth Singh, Edelweiss Wealth Management. www.edelweiss.in"""
        },
        {
            "id": "CASE-05",
            "subject": "Quick Look: B2B HR-Tech SaaS",
            "from": "karan@startup-pipe.com",
            "body": """Interested in HR-Tech? "WorkFlow Pro" is raising $2M. 40% MoM growth.
            Karan, StartupPipe. www.startup-pipe.com"""
        }
    ]

    processor = AIProcessorService()

    for sample in samples:
        print(f"\n[{sample['id']}] Processing: {sample['subject']}...")
        
        email = Email.objects.create(
            email_account=email_account,
            subject=sample['subject'],
            from_email=sample['from'],
            body_text=sample['body'],
            is_processed=False,
            graph_id=str(uuid.uuid4()),
            analysis_result={}
        )
        
        result = processor.process_content(
            content=sample['body'],
            metadata={'subject': sample['subject']},
            source_id=str(email.id),
            skill_name="deal_extraction"
        )
        
        deal_data = result.get('deal_model_data', {})
        contact_discovery = result.get('contact_discovery', {})
        
        # Prepare data for deal creation
        data = {
            **deal_data,
            "source_email_id": str(email.id),
            "contact_discovery": contact_discovery,
            "analysis_json": result # Full result for ambiguity mapping
        }
        
        factory = RequestFactory()
        request = factory.post('/api/deals/', data=json.dumps(data), content_type='application/json')
        request.user = type('User', (), {'profile': profile, 'is_authenticated': True})()
        
        viewset = DealViewSet()
        viewset.request = request
        viewset.action = 'create'
        
        serializer = DealSerializer(data=data, context={'request': request})
        if serializer.is_valid():
            viewset.perform_create(serializer)
            deal = Deal.objects.latest('created_at')
            print(f"  [+] Deal: {deal.title}")
            print(f"  [+] Bank: {deal.bank.name if deal.bank else 'MISSING'}")
            print(f"  [+] Banker: {deal.primary_contact.name if deal.primary_contact else 'MISSING'}")
            print(f"  [+] Ambiguities Identified: {len(deal.ambiguities)}")
            for idx, amb in enumerate(deal.ambiguities):
                print(f"      {idx+1}. {amb}")
        else:
            print(f"  [-] Error: {serializer.errors}")

    print("\n--- STRESS TEST COMPLETE ---")

if __name__ == "__main__":
    test_system()
