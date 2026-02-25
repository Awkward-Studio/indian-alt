import getpass
from django.core.management.base import BaseCommand
from microsoft.services.graph_service import GraphAPIService

class Command(BaseCommand):
    help = 'One-time authentication for Microsoft Graph using Direct Password Flow (ROPC)'

    def add_arguments(self, parser):
        parser.add_argument('--email', type=str, required=True, help='Email to authenticate')

    def handle(self, *args, **options):
        email = options['email']
        service = GraphAPIService()
        
        self.stdout.write(f"Starting silent authentication for {email}...")
        self.stdout.write("Note: This bypasses the browser and redirect URI entirely.")
        
        password = getpass.getpass(f"Enter password for {email}: ")
        
        if not password:
            self.stdout.write(self.style.ERROR("No password provided."))
            return

        success, message = service.authenticate_with_password(email, password)
        
        if success:
            self.stdout.write(self.style.SUCCESS(f"\nSuccessfully authenticated {email}!"))
        else:
            self.stdout.write(self.style.ERROR(f"\nAuthentication failed: {message}"))
            self.stdout.write("\nIf you have MFA (Two-Factor) enabled, this method will fail.")
            self.stdout.write("In that case, the client MUST add 'http://localhost' as a Redirect URI later.")
