import os
import pandas as pd
from datetime import datetime
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.contrib.auth.models import User
from deals.models import Deal, DealStatus, DealPhase
from contacts.models import Contact
from banks.models import Bank
from accounts.models import Profile

class Command(BaseCommand):
    help = 'Import legacy Excel data for Bankers and Deals'

    def add_arguments(self, parser):
        parser.add_argument('--bankers', type=str, help='Path to the Banker Data Excel file')
        parser.add_argument('--deals', type=str, nargs='+', help='Paths to one or more Deal Excel files (Fund I, II, III)')
        parser.add_argument('--dry-run', action='store_true', help='Preview changes without saving')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        if dry_run:
            self.stdout.write(self.style.MIGRATE_HEADING("DRY RUN MODE - No changes will be saved"))

        # 1. Process Bankers first
        if options['bankers']:
            self.process_bankers(options['bankers'], dry_run)

        # 2. Process Deals
        if options['deals']:
            for deal_file in options['deals']:
                self.process_deals(deal_file, dry_run)

    def process_bankers(self, file_path, dry_run):
        self.stdout.write(f"\n>>> Processing Bankers from: {file_path}")
        try:
            df = pd.read_excel(file_path)
            # Normalize column names (strip spaces)
            df.columns = [c.strip() for f in [df.columns] for c in f]
            
            with transaction.atomic():
                count = 0
                for _, row in df.iterrows():
                    bank_name = str(row.get('Investment Bank', '')).strip()
                    if not bank_name or bank_name == 'nan' or bank_name == '-':
                        bank = None
                    else:
                        if dry_run:
                            bank = Bank.objects.filter(name=bank_name).first()
                            if not bank: self.stdout.write(f"  [Will Create] Bank: {bank_name}")
                        else:
                            bank, _ = Bank.objects.get_or_create(name=bank_name)

                    contact_name = str(row.get('Contact Person', '')).strip()
                    if not contact_name or contact_name == 'nan' or contact_name == '-':
                        continue

                    email = str(row.get('Email', '')).strip()
                    if email == 'nan' or email == '-': email = None

                    # Sector coverage as list
                    sectors_raw = str(row.get('Sector Coverage', ''))
                    sectors = []
                    if sectors_raw and sectors_raw != 'nan' and sectors_raw != '-':
                        sectors = [s.strip() for s in sectors_raw.split(',') if s.strip()]

                    contact_data = {
                        'name': contact_name,
                        'bank': bank,
                        'location': str(row.get('Location', '')).replace('nan', '').strip(),
                        'designation': str(row.get('Designation', '')).replace('nan', '').strip(),
                        'phone': str(row.get('Phone', '')).replace('nan', '').strip(),
                        'email': email,
                        'sector_coverage': sectors,
                        'ranking': str(row.get('Ranking', '')).replace('nan', '').strip(),
                        'primary_coverage_person': str(row.get('Person Covering - Primary', '')).replace('nan', '').strip(),
                        'secondary_coverage_person': str(row.get('Person Covering - Secondary', '')).replace('nan', '').strip(),
                        'total_deals_legacy': int(row.get('Total Deals', 0)) if pd.notnull(row.get('Total Deals')) and str(row.get('Total Deals')).isdigit() else 0,
                        'pipeline': str(row.get('Pipeline', '')).replace('nan', '').strip(),
                        'follow_ups': str(row.get('Follow Ups', '')).replace('nan', '').strip(),
                        'last_meeting_date': str(row.get('Last Meeting / Call Date', '')).replace('nan', '').strip(),
                    }

                    contact = None
                    if email:
                        contact = Contact.objects.filter(email=email).first()
                    if not contact:
                        contact = Contact.objects.filter(name=contact_name, bank=bank).first()

                    if dry_run:
                        action = "Update" if contact else "Create"
                        self.stdout.write(f"  [{action}] Contact: {contact_name} ({email or 'no email'})")
                    else:
                        if contact:
                            for key, value in contact_data.items():
                                setattr(contact, key, value)
                            contact.save()
                        else:
                            Contact.objects.create(**contact_data)
                    count += 1

                if dry_run:
                    self.stdout.write(self.style.SUCCESS(f"Dry run: Would process {count} bankers."))
                    transaction.set_rollback(True)
                else:
                    self.stdout.write(self.style.SUCCESS(f"Successfully imported {count} bankers."))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error processing bankers: {e}"))

    def process_deals(self, file_path, dry_run):
        self.stdout.write(f"\n>>> Processing Deals from: {file_path}")
        try:
            df = pd.read_excel(file_path)
            df.columns = [c.strip() for f in [df.columns] for c in f]
            
            with transaction.atomic():
                count = 0
                for index, row in df.iterrows():
                    row_num = index + 2 # Excel row number
                    title = str(row.get('Deal Name', '')).strip()
                    industry = str(row.get('Industry', '')).replace('nan', '').strip()
                    sector = str(row.get('Sector', '')).replace('nan', '').strip()

                    # Fallback title logic if Deal Name is missing
                    if not title or title == 'nan':
                        if sector:
                            title = f"Unknown Company {row_num} ({sector})"
                        elif industry:
                            title = f"Unknown Company {row_num} ({industry})"
                        else:
                            self.stdout.write(self.style.WARNING(f"  [SKIPPING] Row {row_num}: No Deal Name, Industry, or Sector found."))
                            continue

                    # 1. Parse Date
                    raw_date = row.get('Date of Receipt')
                    parsed_date = timezone.now()
                    if pd.notnull(raw_date):
                        if isinstance(raw_date, datetime):
                            parsed_date = timezone.make_aware(raw_date)
                        else:
                            try:
                                parsed_date = timezone.make_aware(pd.to_datetime(raw_date))
                            except:
                                pass

                    # 2. Status Mapping
                    status_str = str(row.get('Deal Status', '')).lower().strip()
                    deal_status = DealStatus.STAGE_1
                    current_phase = DealPhase.STAGE_1
                    
                    if 'passed' in status_str:
                        deal_status = DealStatus.PASSED
                        current_phase = DealPhase.PASSED
                    elif 'invested' in status_str:
                        deal_status = DealStatus.INVESTED
                        current_phase = DealPhase.INVESTED
                    elif 'portfolio' in status_str:
                        deal_status = DealStatus.PORTFOLIO
                        current_phase = DealPhase.PORTFOLIO

                    # 3. Combine Summary and Details
                    summary = str(row.get('Summary', '')).replace('nan', '').strip()
                    details = str(row.get('Details', '')).replace('nan', '').strip()
                    combined_summary = f"{summary}\n\n{details}".strip()

                    # 4. Fund
                    fund_name = str(row.get('Fund', 'UNSPECIFIED')).strip()

                    # Create or Update Deal logic
                    deal = None
                    is_update = False
                    # Match by title (case-insensitive)
                    deal = Deal.objects.filter(title__iexact=title).first()
                    if deal:
                        is_update = True

                    if dry_run:
                        status = f"Update (Matches: '{deal.title}')" if is_update else "Create"
                        self.stdout.write(f"  [{status}] Deal: {title} | Fund: {fund_name}")
                    else:
                        deal_fields = {
                            'deal_status': deal_status,
                            'current_phase': current_phase,
                            'funding_ask': str(row.get('Funding Ask (INR MILLION)', '')).replace('nan', '').strip(),
                            'industry': str(row.get('Industry', '')).replace('nan', '').strip(),
                            'sector': str(row.get('Sector', '')).replace('nan', '').strip(),
                            'deal_summary': combined_summary,
                            'company_details': str(row.get('Company Info', '')).replace('nan', '').strip(),
                            'reasons_for_passing': str(row.get('Reasons for Passing', '')).replace('nan', '').strip(),
                            'city': str(row.get('City', '')).replace('nan', '').strip(),
                            'is_female_led': bool(row.get('Is Female Led', False)),
                            'management_meeting': bool(row.get('Management Meeting', False)),
                            'business_proposal_stage': bool(row.get('Business Proposal Stage', False)),
                            'ic_stage': bool(row.get('IC Stage', False)),
                            'fund': fund_name,
                            'deal_details': f"Source: {row.get('Source')}\nNext Steps: {row.get('Next Steps')}"
                        }

                        if is_update:
                            # Update existing deal
                            for key, value in deal_fields.items():
                                setattr(deal, key, value)
                            deal.save()
                            self.stdout.write(f"  [MERGED] '{title}' matched existing deal: '{deal.title}' (ID: {deal.id})")
                        else:
                            # Create new
                            deal = Deal.objects.create(title=title, **deal_fields)
                            self.stdout.write(f"  [CREATED] {title}")

                        # Preserving historical date
                        Deal.objects.filter(pk=deal.pk).update(created_at=parsed_date)

                        # 5. Link Contacts (Look by name in the Contacts column)
                        contact_names_raw = str(row.get('Contacts', ''))
                        if contact_names_raw and contact_names_raw != 'nan' and contact_names_raw != '-':
                            names = [n.strip() for n in contact_names_raw.replace(';', ',').split(',') if n.strip()]
                            for name in names:
                                contact = Contact.objects.filter(name__icontains=name).first() or \
                                          Contact.objects.filter(email__icontains=name).first()
                                if contact:
                                    deal.additional_contacts.add(contact)
                                else:
                                    if deal.comments: deal.comments += f"\nLegacy Contact Reference: {name}"
                                    else: deal.comments = f"Legacy Contact Reference: {name}"
                                    deal.save()

                        # 6. Link Deal Team
                        team_raw = str(row.get('Deal Team', ''))
                        if team_raw and team_raw != 'nan' and team_raw != '-':
                            team_parts = [t.strip() for t in team_raw.split(',') if t.strip()]
                            for member in team_parts:
                                profile = Profile.objects.filter(user__first_name__iexact=member).first() or \
                                          Profile.objects.filter(user__username__icontains=member.lower()).first()
                                
                                if not profile:
                                    # Create placeholder
                                    username = f"legacy_{member.lower().replace(' ', '_')}"
                                    legacy_email = f"{username}@legacy.india-alt.com"
                                    user, created = User.objects.get_or_create(
                                        username=username,
                                        defaults={'first_name': member, 'email': legacy_email}
                                    )
                                    profile = getattr(user, 'profile', None)
                                    if not profile:
                                        profile = Profile.objects.create(
                                            user=user, 
                                            name=member, 
                                            email=legacy_email
                                        )
                                
                                if profile:
                                    deal.responsibility.add(profile)

                    count += 1

                if dry_run:
                    self.stdout.write(self.style.SUCCESS(f"Dry run complete for {file_path}. Would process {count} deals."))
                    transaction.set_rollback(True)
                else:
                    self.stdout.write(self.style.SUCCESS(f"Successfully imported {count} deals from {file_path}"))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error processing deals in {file_path}: {e}"))
            import traceback
            traceback.print_exc()
