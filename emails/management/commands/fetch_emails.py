"""
Management command to fetch emails from Microsoft Graph API.

Usage:
    python manage.py fetch_emails
    python manage.py fetch_emails --email dms-demo@india-alt.com
    python manage.py fetch_emails --since 2024-01-01
    python manage.py fetch_emails --limit 50
    python manage.py fetch_emails --dry-run
"""
import logging
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from datetime import datetime, timedelta
from emails.models import EmailAccount
from emails.services.email_reader import EmailReaderService

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Management command to fetch emails from Microsoft Graph API."""
    
    help = 'Fetch emails from Microsoft Graph API for active email accounts'
    
    def add_arguments(self, parser):
        """Add command line arguments."""
        parser.add_argument(
            '--email',
            type=str,
            help='Fetch emails for a specific email account only'
        )
        parser.add_argument(
            '--since',
            type=str,
            help='Only fetch emails received after this date (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)'
        )
        parser.add_argument(
            '--limit',
            type=int,
            help='Maximum number of emails to fetch per account'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without actually fetching emails'
        )
    
    def handle(self, *args, **options):
        """Execute the command."""
        email = options.get('email')
        since_str = options.get('since')
        limit = options.get('limit')
        dry_run = options.get('dry_run', False)
        
        if dry_run:
            self.stdout.write(
                self.style.WARNING('DRY RUN MODE - No emails will be fetched')
            )
        
        # Parse since date
        since = None
        if since_str:
            try:
                # Try parsing as date
                if len(since_str) == 10:  # YYYY-MM-DD
                    since = datetime.strptime(since_str, '%Y-%m-%d')
                else:  # YYYY-MM-DD HH:MM:SS
                    since = datetime.strptime(since_str, '%Y-%m-%d %H:%M:%S')
                since = timezone.make_aware(since)
            except ValueError:
                raise CommandError(
                    f'Invalid date format for --since: {since_str}. '
                    'Use YYYY-MM-DD or YYYY-MM-DD HH:MM:SS'
                )
        
        try:
            email_reader = EmailReaderService()
            
            if email:
                # Fetch for specific email account
                try:
                    email_account = EmailAccount.objects.get(email=email)
                except EmailAccount.DoesNotExist:
                    raise CommandError(f'Email account not found: {email}')
                
                if not email_account.is_active:
                    raise CommandError(f'Email account is not active: {email}')
                
                if dry_run:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'Would fetch emails for: {email_account.email}'
                        )
                    )
                    if since:
                        self.stdout.write(f'  Since: {since}')
                    if limit:
                        self.stdout.write(f'  Limit: {limit}')
                    return
                
                self.stdout.write(f'Fetching emails for: {email_account.email}')
                result = email_reader.fetch_emails_for_account(
                    email_account=email_account,
                    limit=limit,
                    since=since
                )
                
                if result['success']:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"✓ Successfully fetched {result['count']} emails "
                            f"({result['new_count']} new, {result['updated_count']} updated)"
                        )
                    )
                    if result['errors']:
                        self.stdout.write(
                            self.style.WARNING(
                                f"Warnings: {len(result['errors'])} errors occurred"
                            )
                        )
                else:
                    self.stdout.write(
                        self.style.ERROR(f"✗ Failed to fetch emails: {result['errors']}")
                    )
            
            else:
                # Fetch for all active accounts
                active_accounts = EmailAccount.objects.filter(is_active=True)
                count = active_accounts.count()
                
                if count == 0:
                    self.stdout.write(
                        self.style.WARNING('No active email accounts found')
                    )
                    return
                
                if dry_run:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'Would fetch emails for {count} active account(s):'
                        )
                    )
                    for account in active_accounts:
                        self.stdout.write(f'  - {account.email}')
                    if since:
                        self.stdout.write(f'  Since: {since}')
                    if limit:
                        self.stdout.write(f'  Limit per account: {limit}')
                    return
                
                self.stdout.write(f'Fetching emails for {count} active account(s)...')
                results = email_reader.fetch_all_active_accounts(
                    limit_per_account=limit,
                    since=since
                )
                
                self.stdout.write(
                    self.style.SUCCESS(
                        f"✓ Completed: {results['successful_accounts']} successful, "
                        f"{results['failed_accounts']} failed"
                    )
                )
                self.stdout.write(
                    f"  Total emails: {results['total_emails']}"
                )
                
                # Show per-account results
                for account_email, account_result in results['account_results'].items():
                    if account_result['success']:
                        self.stdout.write(
                            f"  ✓ {account_email}: {account_result['count']} emails "
                            f"({account_result['new_count']} new, "
                            f"{account_result['updated_count']} updated)"
                        )
                    else:
                        self.stdout.write(
                            self.style.ERROR(
                                f"  ✗ {account_email}: Failed - {account_result.get('errors', [])}"
                            )
                        )
        
        except Exception as e:
            logger.error(f"Error in fetch_emails command: {str(e)}", exc_info=True)
            raise CommandError(f'Failed to fetch emails: {str(e)}')
