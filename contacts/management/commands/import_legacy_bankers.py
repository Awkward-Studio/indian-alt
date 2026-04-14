import csv
import os
from django.core.management.base import BaseCommand
from banks.models import Bank
from contacts.models import Contact
from deals.models import Deal

# Mapping legacy status to the 18-step Deal Flow tracker enumValues
DEAL_FLOW_MAPPING = {
    'deal sourced': '1: Deal Sourced',
    'sourced': '1: Deal Sourced',
    'initial banker call': '2: Initial Banker Call',
    'banker call': '2: Initial Banker Call',
    'nda execution': '3: NDA Execution',
    'nda': '3: NDA Execution',
    'initial materials review': '4: Initial Materials Review',
    'review': '4: Initial Materials Review',
    'materials review': '4: Initial Materials Review',
    'passed': 'Passed',
}

class Command(BaseCommand):
    help = 'Imports legacy banker data from a CSV file and maps deal status to the 18-step tracker'

    def add_arguments(self, parser):
        parser.add_argument('csv_file', type=str, help='Path to the legacy banker CSV file')

    def handle(self, *args, **options):
        csv_file_path = options['csv_file']
        if not os.path.exists(csv_file_path):
            self.stdout.write(self.style.ERROR(f"File not found: {csv_file_path}"))
            return

        with open(csv_file_path, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            # Print columns for debugging if headers don't match expectations
            self.stdout.write(f"Headers found: {reader.fieldnames}")

            count = 0
            for row in reader:
                # 1. Handle Bank
                bank_name = row.get('Investment Bank', '').strip()
                bank = None
                if bank_name:
                    bank, _ = Bank.objects.get_or_create(name=bank_name)

                # 2. Handle Contact
                contact_name = row.get('Contact Person', '').strip()
                if not contact_name:
                    continue

                email = row.get('Email', '').strip()
                
                # Deduplicate contact
                contact = None
                if email:
                    contact = Contact.objects.filter(email__iexact=email).first()
                if not contact:
                    contact = Contact.objects.filter(name__iexact=contact_name, bank=bank).first()

                # Sector coverage mapping
                raw_sectors = row.get('Sector Coverage', '')
                sector_list = [s.strip() for s in raw_sectors.split(',') if s.strip()]

                contact_data = {
                    'name': contact_name,
                    'bank': bank,
                    'email': email,
                    'designation': row.get('Designation', '').strip(),
                    'phone': row.get('Phone', '').strip(),
                    'location': row.get('Location', '').strip(),
                    'ranking': row.get('Ranking', '').strip(),
                    'primary_coverage_person': row.get('Person Covering - Primary', '').strip(),
                    'secondary_coverage_person': row.get('Person Covering - Secondary', '').strip(),
                    'pipeline': row.get('Pipeline', '').strip(),
                    'follow_ups': row.get('Follow Ups', '').strip(),
                    'last_meeting_date': row.get('Last Meeting / Call Date', '').strip(),
                    'sector_coverage': sector_list,
                }

                try:
                    total_deals = int(row.get('Total Deals', 0) or 0)
                    contact_data['total_deals_legacy'] = total_deals
                except ValueError:
                    pass

                if not contact:
                    contact = Contact.objects.create(**contact_data)
                    self.stdout.write(f"Created contact: {contact.name}")
                else:
                    # Update existing contact with legacy data
                    for key, value in contact_data.items():
                        if value and not getattr(contact, key):
                            setattr(contact, key, value)
                    contact.save()
                    self.stdout.write(f"Updated contact: {contact.name}")

                # 3. Handle Deal Flow / Status Mapping
                legacy_pipeline = row.get('Pipeline', '').strip().lower()
                if legacy_pipeline:
                    mapped_phase = DEAL_FLOW_MAPPING.get(legacy_pipeline)
                    
                    # Create a placeholder deal if we have a valid pipeline status
                    if mapped_phase:
                        # Only create if a deal with this contact and mapped phase doesn't exist to avoid duplicates
                        deal_title = f"Legacy Deal: {contact.name} - {legacy_pipeline}"
                        deal, created = Deal.objects.get_or_create(
                            title=deal_title,
                            primary_contact=contact,
                            defaults={
                                'current_phase': mapped_phase,
                                'bank': bank,
                                'deal_summary': f"Imported from legacy banker database. Original Status: {legacy_pipeline}",
                            }
                        )
                        if created:
                            self.stdout.write(self.style.SUCCESS(f"  -> Created deal: {deal_title} at phase {mapped_phase}"))

                count += 1

        self.stdout.write(self.style.SUCCESS(f"Successfully processed {count} records."))
