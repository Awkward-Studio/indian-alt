from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from microsoft.models import MicrosoftToken


class Command(BaseCommand):
    help = "Store or update a delegated Microsoft token in the database"

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Account email for the delegated token")
        parser.add_argument("--access-token", required=True, help="Current access token")
        parser.add_argument("--refresh-token", required=False, help="Refresh token")
        parser.add_argument(
            "--expires-in",
            type=int,
            default=3600,
            help="Access token lifetime in seconds from now (default 3600)",
        )

    def handle(self, *args, **options):
        email = options["email"].strip()
        access_token = options["access_token"].strip()
        refresh_token = (options.get("refresh_token") or "").strip() or None
        expires_in = options["expires_in"]

        if not email:
            raise CommandError("--email is required")
        if not access_token:
            raise CommandError("--access-token is required")
        if expires_in <= 0:
            raise CommandError("--expires-in must be greater than 0")

        token, created = MicrosoftToken.objects.update_or_create(
            account_email=email,
            defaults={
                "token_type": "delegated",
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": timezone.now() + timedelta(seconds=expires_in),
            },
        )

        action = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{action} delegated token for {token.account_email}"))
        self.stdout.write(f"Expires at: {token.expires_at.isoformat()}")
