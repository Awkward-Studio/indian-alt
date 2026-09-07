import hashlib
import json

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from microsoft.models import Email, EmailIngestionBackfill
from microsoft.services.email_evidence import EmailEvidenceService
from microsoft.services.email_ingestion import EmailIngestionService


class Command(BaseCommand):
    help = 'Preview or enqueue a bounded, resumable email evidence backfill.'

    def add_arguments(self, parser):
        parser.add_argument('--mailbox')
        parser.add_argument('--after')
        parser.add_argument('--email-id', action='append', default=[])
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument('--resume-key')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        if options['apply'] == options['dry_run']:
            raise CommandError('Choose exactly one of --dry-run or --apply.')
        limit = options['limit']
        if limit < 1 or limit > 1000:
            raise CommandError('--limit must be between 1 and 1000.')
        after = parse_datetime(options['after']) if options.get('after') else None
        if options.get('after') and after is None:
            raise CommandError('--after must be an ISO-8601 date and time.')
        filters = {'mailbox': options.get('mailbox'), 'after': options.get('after'),
                   'email_ids': sorted(set(options.get('email_id') or [])), 'limit': limit}
        key = options.get('resume_key') or hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()[:24]
        cursor = None
        progress = None
        if options['apply']:
            progress, created = EmailIngestionBackfill.objects.get_or_create(key=key, defaults={'filters': filters})
            if not created and progress.filters != filters:
                raise CommandError('The resume key belongs to a different filter set.')
            if progress.completed:
                self.stdout.write(json.dumps({'resume_key': key, 'completed': True, 'selected': 0, 'enqueued': 0}, sort_keys=True))
                return
            cursor = progress.cursor
        queryset = Email.objects.select_related('email_account').order_by('id')
        if filters['mailbox']:
            queryset = queryset.filter(email_account__email__iexact=filters['mailbox'])
        if after:
            queryset = queryset.filter(date_received__gte=after)
        if filters['email_ids']:
            queryset = queryset.filter(id__in=filters['email_ids'])
        if cursor:
            queryset = queryset.filter(id__gt=cursor)
        emails = list(queryset[:limit])
        preview = []
        for email in emails:
            preview.append({'email_id': str(email.id), 'mailbox': email.email_account.email,
                            'already_linked': bool(email.deal_id), 'existing_run': email.ingestion_runs.exists()})
            if options['apply']:
                EmailIngestionService.enqueue(email)
        has_more = bool(emails and queryset.filter(id__gt=emails[-1].id).exists())
        if progress:
            stats = dict(progress.stats or {})
            stats['selected'] = int(stats.get('selected', 0)) + len(emails)
            stats['last_batch'] = len(emails)
            stats['linked_in_last_batch'] = sum(1 for item in preview if item['already_linked'])
            progress.cursor = emails[-1].id if emails else progress.cursor
            progress.completed = not has_more
            progress.stats = stats
            progress.save()
        self.stdout.write(json.dumps({
            'mode': 'apply' if options['apply'] else 'dry-run', 'resume_key': key,
            'selected': len(emails), 'enqueued': len(emails) if options['apply'] else 0,
            'has_more': has_more, 'next_cursor': str(emails[-1].id) if emails else None,
            'emails': preview,
        }, sort_keys=True))
