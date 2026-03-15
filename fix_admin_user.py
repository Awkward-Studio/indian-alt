import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.contrib.auth.models import User
from accounts.models import Profile

username = 'admin@example.com'
password = 'changeme'

u, _ = User.objects.update_or_create(
    username=username,
    defaults={'email': username, 'is_superuser': True, 'is_staff': True}
)
u.set_password(password)
u.save()

p = Profile.objects.filter(email=username).first()
if p:
    p.user = u
    p.save()
else:
    Profile.objects.create(
        user=u,
        email=username,
        name='Admin User',
        is_admin=True
    )

print(f"Successfully set up superuser and profile for {username}")
