from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from microsoft.models import Email
from microsoft.services.email_html_sanitizer import (
    EMAIL_HTML_SANITIZER_VERSION,
    EmailHtmlSanitizer,
)


class Command(BaseCommand):
    help = 'Identify or reprocess stored email HTML using the current allowlist policy.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Persist sanitized bodies. Default is dry-run.')
        parser.add_argument('--limit', type=int, default=500, help='Maximum rows to inspect (1-5000).')

    def handle(self, *args, **options):
        limit = options['limit']
        if not 1 <= limit <= 5000:
            raise CommandError('--limit must be between 1 and 5000')

        queryset = Email.objects.filter(
            Q(body_html_sanitizer_version__isnull=True)
            | ~Q(body_html_sanitizer_version=EMAIL_HTML_SANITIZER_VERSION),
            body_html__isnull=False,
        ).exclude(body_html='').order_by('created_at', 'id')[:limit]
        emails = list(queryset)

        changed = 0
        if options['apply']:
            for email in emails:
                sanitized = EmailHtmlSanitizer.sanitize(email.body_html)
                email.body_html = sanitized.html
                email.body_html_sanitizer_version = sanitized.policy_version
                email.body_html_sanitized_at = timezone.now()
                email.save(update_fields=[
                    'body_html',
                    'body_html_sanitizer_version',
                    'body_html_sanitized_at',
                    'updated_at',
                ])
                changed += 1

        mode = 'APPLY' if options['apply'] else 'DRY RUN'
        self.stdout.write(
            f'{mode}: identified {len(emails)} email(s); reprocessed {changed}; '
            f'policy version {EMAIL_HTML_SANITIZER_VERSION}.'
        )
