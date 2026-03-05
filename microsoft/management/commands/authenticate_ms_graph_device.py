"""
Management command: delegated authentication for Microsoft Graph API via Device Code flow.

Why:
- Works when ROPC/password flow is blocked (MFA, conditional access, tenant policy).
- Handles AADSTS65001 consent requirements because it is interactive.
- No Redirect URI setup needed.

Usage:
    python manage.py authenticate_ms_graph_device --email dms-demo@india-alt.com
"""

import logging
import time
from django.core.management.base import BaseCommand, CommandError
from microsoft.services.graph_service import GraphAPIService

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Authenticate Microsoft Graph delegated token using Device Code flow (interactive)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            type=str,
            required=True,
            help="Email address to authenticate (e.g. dms-demo@india-alt.com)",
        )

    def handle(self, *args, **options):
        email = options["email"]

        self.stdout.write(f"Starting device-code authentication for {email}...")
        self.stdout.write("This will ask you to open a Microsoft login page and enter a code.\n")

        try:
            service = GraphAPIService()
            ok, flow, message = service.start_device_code_flow()
            if not ok:
                raise CommandError(message)

            # Print URL + code BEFORE we start polling
            self.stdout.write(message)
            self.stdout.write("\nWaiting for you to finish sign-in in the browser...")

            # Poll (MSAL blocks inside; we add a small spinner-like heartbeat for UX)
            # Note: acquire_token_by_device_flow() internally polls, so we only print a heartbeat here.
            start = time.time()
            while True:
                # Try to finish; if it returns quickly, we're done; otherwise it may block.
                success, finish_msg = service.finish_device_code_flow(email, flow)
                if success:
                    break
                # If MSAL returns an error immediately, show it.
                raise CommandError(finish_msg)
        except Exception as e:
            logger.error(f"Device-code authentication failed for {email}: {e}", exc_info=True)
            raise CommandError(f"Authentication error: {e}")

        if success:
            self.stdout.write(self.style.SUCCESS(f"\n✓ Successfully authenticated {email} via device code!"))
        else:
            self.stdout.write(self.style.ERROR(f"\n✗ Authentication failed"))
            raise CommandError("Authentication failed")

