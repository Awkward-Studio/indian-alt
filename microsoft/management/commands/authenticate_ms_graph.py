"""
Management command: one-time authentication for Microsoft Graph API.

Uses the Resource Owner Password Credentials (ROPC) flow so no browser
redirect or admin-configured URI is needed.

Usage:
    python manage.py authenticate_ms_graph --email dms-demo@india-alt.com
"""
import getpass
import logging
from django.core.management.base import BaseCommand, CommandError
from microsoft.services.graph_service import GraphAPIService

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Authenticate a Microsoft 365 account via username/password (ROPC)."""

    help = 'One-time authentication for Microsoft Graph using Direct Password Flow (ROPC)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--email',
            type=str,
            required=True,
            help='Email address to authenticate (e.g. dms-demo@india-alt.com)',
        )

    def handle(self, *args, **options):
        email = options['email']

        self.stdout.write(f"Starting silent authentication for {email}...")
        self.stdout.write("Note: This bypasses the browser and redirect URI entirely.")

        password = getpass.getpass(f"Enter password for {email}: ")

        if not password:
            raise CommandError("No password provided — aborting.")

        try:
            service = GraphAPIService()
            success, message = service.authenticate_with_password(email, password)
        except Exception as e:
            logger.error(f"Authentication failed for {email}: {e}", exc_info=True)
            raise CommandError(f"Authentication error: {e}")

        if success:
            self.stdout.write(self.style.SUCCESS(f"\n✓ Successfully authenticated {email}!"))
            self.stdout.write(
                "Tip: add MS_GRAPH_PASSWORD to your .env to skip this step in CI/CD."
            )
        else:
            self.stdout.write(self.style.ERROR(f"\n✗ Authentication failed: {message}"))
            if "MFA" in message.upper() or "multi-factor" in message.lower():
                self.stdout.write(
                    self.style.WARNING(
                        "\nThis account has MFA enabled — ROPC will not work.\n"
                        "Ask the client to add 'http://localhost' as a Redirect URI instead."
                    )
                )
            raise CommandError(f"Authentication failed: {message}")
