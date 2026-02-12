"""
Management command to create or update the default superuser.

This command ensures that a superuser with email 'admin@example.com' and password 'changeme'
exists after every deployment. It updates the password if the user already exists.

Usage:
    python manage.py create_default_superuser
"""
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.db import transaction

User = get_user_model()


class Command(BaseCommand):
    """Create or update default superuser for deployments."""
    
    help = 'Create or update default superuser (admin@example.com / changeme)'
    
    DEFAULT_EMAIL = 'admin@example.com'
    DEFAULT_PASSWORD = 'changeme'
    
    def handle(self, *args, **options):
        """Execute the command."""
        try:
            with transaction.atomic():
                # Use username for lookup (Django's default User model uses username as primary identifier)
                username = self.DEFAULT_EMAIL
                user, created = User.objects.get_or_create(
                    username=username,
                    defaults={
                        'email': self.DEFAULT_EMAIL,
                        'is_staff': True,
                        'is_superuser': True,
                    }
                )
                
                if created:
                    user.set_password(self.DEFAULT_PASSWORD)
                    user.save()
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'✓ Created superuser: {self.DEFAULT_EMAIL} (username: {username})'
                        )
                    )
                else:
                    # Update existing user to ensure it's a superuser with correct password
                    user.email = self.DEFAULT_EMAIL  # Ensure email is set
                    user.is_staff = True
                    user.is_superuser = True
                    user.set_password(self.DEFAULT_PASSWORD)
                    user.save()
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'✓ Updated superuser: {self.DEFAULT_EMAIL} (username: {username})'
                        )
                    )
        
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'✗ Failed to create/update superuser: {str(e)}')
            )
            raise
