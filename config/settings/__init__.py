"""
Settings module - imports the appropriate settings based on environment.
"""
from decouple import config

ENVIRONMENT = config('DJANGO_ENVIRONMENT', default='local')

if ENVIRONMENT == 'production':
    from .production import *
else:
    from .local import *
