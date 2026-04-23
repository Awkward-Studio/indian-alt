import csv
import uuid
from datetime import datetime
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from deals.models import Deal, DealStatus, DealPhase
from contacts.models import Contact
from accounts.models import Profile
from django.contrib.auth.models import User

class Command(BaseCommand):
    help = 'Import legacy deal flow data from a CSV file'

    def add_arguments(self, parser):
        parser.add_argument('--file', type=str, required=True, help='Path to the deals CSV file')
        parser.add_argument('--fund', type=str, default='FUND3', help='Fund name (e.g., FUND1, FUND2, FUND3)')

    def handle(self, *args, **options):
        file_path = options['file']
        fund_name = options['fund']

        try:
            with open(file_path, 'r', encoding='utf-8-sig') as csvfile:
                reader = csv.DictReader(csvfile)
                
                with transaction.atomic():
                    count = 0
                    for row in reader:
                        title = (row.get('Deal Name') or '').strip()
                        if not title:
                            continue

                        # 1. Parse Date
                        raw_date = (row.get('Date of Receipt') or '').strip()
                        parsed_date = timezone.now()
                        if raw_date and raw_date != '-':
                            try:
                                # Try DD-Mon-YYYY
                                parsed_date = timezone.make_aware(datetime.strptime(raw_date, '%d-%b-%Y'))
                            except ValueError:
                                try:
                                    # Fallback to other common formats if needed
                                    parsed_date = timezone.make_aware(datetime.strptime(raw_date, '%Y-%m-%d'))
                                except ValueError:
                                    self.stdout.write(self.style.WARNING(f"Could not parse date '{raw_date}' for deal '{title}', using current time."))

                        # 2. Parse Booleans
                        def to_bool(val):
                            if not val: return False
                            val = str(val).strip().upper()
                            return val in ['TRUE', 'YES', '1', 'T', 'Y']

                        is_female_led = to_bool(row.get('Is Female Led?'))
                        management_meeting = to_bool(row.get('Meeting Status'))
                        business_proposal_stage = to_bool(row.get('Business Proposal Stage'))
                        ic_stage = to_bool(row.get('IC Stage'))

                        # 3. Industry & Sector
                        industry_sector = (row.get('Industry Sector') or '').strip()
                        industry = industry_sector
                        sector = ''
                        if ' - ' in industry_sector:
                            industry, sector = industry_sector.split(' - ', 1)

                        # 4. Deal Status
                        status_str = (row.get('Deal Status') or '').strip()
                        deal_status = DealStatus.STAGE_1
                        current_phase = DealPhase.STAGE_1
                        
                        if status_str.lower() == 'passed':
                            deal_status = DealStatus.PASSED
                            current_phase = DealPhase.PASSED
                        elif status_str.lower() == 'invested':
                            deal_status = DealStatus.INVESTED
                            current_phase = DealPhase.INVESTED
                        elif status_str.lower() == 'portfolio':
                            deal_status = DealStatus.PORTFOLIO
                            current_phase = DealPhase.PORTFOLIO

                        # 5. Create Deal
                        deal = Deal.objects.create(
                            title=title,
                            deal_status=deal_status,
                            current_phase=current_phase,
                            funding_ask=(row.get('Ask (INR Million)') or '').strip(),
                            industry=industry.strip(),
                            sector=sector.strip(),
                            deal_summary=(row.get('Summary Details') or '').strip(),
                            company_details=(row.get('Company Info') or '').strip(),
                            reasons_for_passing=(row.get('Reasons for Passing') or '').strip(),
                            city=(row.get('City') or '').strip(),
                            is_female_led=is_female_led,
                            management_meeting=management_meeting,
                            business_proposal_stage=business_proposal_stage,
                            ic_stage=ic_stage,
                            fund=fund_name,
                            deal_details=f"Source: {row.get('Source')}\nFunding Type: {row.get('Funding Type')}\nNext Steps: {row.get('Next Steps')}"
                        )

                        # Explicitly set created_at
                        Deal.objects.filter(pk=deal.pk).update(created_at=parsed_date)

                        # 6. Link Contacts
                        contact_names_raw = (row.get('Contacts') or '').strip()
                        if contact_names_raw and contact_names_raw != '-':
                            # Contacts might be comma separated
                            names = [n.strip() for n in contact_names_raw.split(',') if n.strip()]
                            for name in names:
                                # Try to find contact by name
                                contact = Contact.objects.filter(name__icontains=name).first()
                                if contact:
                                    deal.additional_contacts.add(contact)
                                else:
                                    # If not found, append to comments to preserve data
                                    if deal.comments:
                                        deal.comments += f"\nLegacy Contact: {name}"
                                    else:
                                        deal.comments = f"Legacy Contact: {name}"
                                    deal.save()

                        # 7. Link Deal Team
                        team_name = (row.get('Deal Team') or '').strip()
                        if team_name and team_name != '-':
                            # Get or create Profile
                            profile = Profile.objects.filter(user__first_name__icontains=team_name).first()
                            if not profile:
                                # Create a placeholder user/profile if it doesn't exist
                                username = f"legacy_{team_name.lower().replace(' ', '_')}"
                                user, created = User.objects.get_or_create(
                                    username=username,
                                    defaults={'first_name': team_name}
                                )
                                if created:
                                    profile = Profile.objects.create(user=user)
                                else:
                                    profile = getattr(user, 'profile', None) or Profile.objects.create(user=user)
                            
                            if profile:
                                deal.responsibility.add(profile)

                        count += 1

                    self.stdout.write(self.style.SUCCESS(f'Successfully imported {count} deals for {fund_name}'))

        except FileNotFoundError:
            self.stdout.write(self.style.ERROR(f'File not found: {file_path}'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'An error occurred: {str(e)}'))
            import traceback
            traceback.print_exc()
