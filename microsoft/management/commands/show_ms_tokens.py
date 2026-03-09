from django.core.management.base import BaseCommand
from microsoft.models import MicrosoftToken
from django.utils import timezone

class Command(BaseCommand):
    help = 'Show Microsoft tokens (Access Token, Refresh Token) from the database'

    def handle(self, *args, **kwargs):
        tokens = MicrosoftToken.objects.all()
        if not tokens.exists():
            self.stdout.write(self.style.WARNING("No Microsoft tokens found in the database."))
            return

        for token in tokens:
            self.stdout.write(self.style.SUCCESS(f"--- Token for account: {token.account_email} ---"))
            
            # Print basic info
            self.stdout.write(f"Token Type: {token.token_type}")
            self.stdout.write(f"Created At: {token.created_at}")
            self.stdout.write(f"Updated At: {token.updated_at}")
            self.stdout.write(f"Expires At: {token.expires_at}")
            is_expired = token.expires_at < timezone.now() if token.expires_at else True
            self.stdout.write(f"Is Expired: {is_expired}")
            
            # Print tokens
            self.stdout.write("\nACCESS TOKEN:")
            self.stdout.write(token.access_token if token.access_token else "None")
            
            self.stdout.write("\nREFRESH TOKEN:")
            self.stdout.write(token.refresh_token if token.refresh_token else "None")
                
            self.stdout.write("\n" + "="*50 + "\n")
