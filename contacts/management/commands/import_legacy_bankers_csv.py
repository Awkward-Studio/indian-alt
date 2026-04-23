import csv
import argparse
from django.core.management.base import BaseCommand
from django.db import transaction
from banks.models import Bank
from contacts.models import Contact

class Command(BaseCommand):
    help = 'Import legacy banker/advisor data from a CSV file'

    def add_arguments(self, parser):
        parser.add_argument('--file', type=str, required=True, help='Path to the banker CSV file')

    def handle(self, *args, **options):
        file_path = options['file']

        try:
            with open(file_path, 'r', encoding='utf-8-sig') as csvfile:
                reader = csv.DictReader(csvfile)
                
                with transaction.atomic():
                    count = 0
                    for row in reader:
                        # 1. Resolve Bank
                        bank_name = (row.get('Covering Bank/Advisor') or row.get('Investment Bank') or '').strip()
                        bank = None
                        if bank_name and bank_name != '-':
                            bank, _ = Bank.objects.get_or_create(name=bank_name)

                        # 2. Resolve Contact Name
                        contact_name = (row.get('Contact Person') or row.get('Primary Person Covering') or '').strip()
                        if not contact_name or contact_name == '-':
                            # Try Secondary if primary is empty
                            contact_name = (row.get('Secondary Investor') or '').strip()
                        
                        if not contact_name or contact_name == '-':
                            self.stdout.write(self.style.WARNING(f"Skipping row with no contact name: {row}"))
                            continue

                        # 3. Sector Coverage
                        sectors_raw = (row.get('Sector Coverage') or '').strip()
                        sectors = []
                        if sectors_raw and sectors_raw != 'N/A' and sectors_raw != '-':
                            sectors = [s.strip() for s in sectors_raw.split(',') if s.strip()]

                        # 4. Create or Update Contact
                        email = (row.get('Email') or '').strip()
                        if email == '-':
                            email = None

                        contact_data = {
                            'name': contact_name,
                            'bank': bank,
                            'location': (row.get('Location') or '').strip(),
                            'designation': (row.get('Designation') or '').strip(),
                            'phone': (row.get('Phone') or '').strip(),
                            'email': email,
                            'sector_coverage': sectors,
                            'rank': (row.get('Ranking') or '').strip(),
                        }

                        # Clean up '-' values
                        for key, value in contact_data.items():
                            if value == '-':
                                contact_data[key] = ''

                        # Try to get by email if available, otherwise by name and bank
                        contact = None
                        if email:
                            contact = Contact.objects.filter(email=email).first()
                        
                        if not contact:
                            contact = Contact.objects.filter(name=contact_name, bank=bank).first()

                        if contact:
                            for key, value in contact_data.items():
                                setattr(contact, key, value)
                            contact.save()
                        else:
                            Contact.objects.create(**contact_data)
                        
                        count += 1

                    self.stdout.write(self.style.SUCCESS(f'Successfully imported {count} banker contacts'))

        except FileNotFoundError:
            self.stdout.write(self.style.ERROR(f'File not found: {file_path}'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'An error occurred: {str(e)}'))
