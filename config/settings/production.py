"""
Production settings.
"""
from .base import *

DEBUG = False

def _csv_hosts(value: str):
    hosts = [s.strip() for s in (value or "").split(",") if s.strip()]
    # If someone sets ALLOWED_HOSTS="" in Railway by mistake, don't brick startup/healthchecks.
    return hosts or ["*"]

ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='*', cast=_csv_hosts)

# If you're using Railway's default domain, set this in Railway Variables
# e.g. CSRF_TRUSTED_ORIGINS=https://your-app.up.railway.app
CSRF_TRUSTED_ORIGINS = config(
    'CSRF_TRUSTED_ORIGINS',
    default='',
    cast=lambda v: [s.strip() for s in v.split(',') if s.strip()]
)

# Security settings for production
SECURE_SSL_REDIRECT = config('SECURE_SSL_REDIRECT', default=True, cast=bool)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'

# Railway / proxy headers
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Email settings (configure as needed)
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = config('EMAIL_HOST', default='smtp.gmail.com')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
