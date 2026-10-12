"""Start a bounded hosted report pilot through the ordinary durable queue."""
import json
from django.core.management.base import BaseCommand,CommandError
from django.conf import settings
from deals.models import Deal
from deals.services.folder_analysis import FolderAnalysisService
from ai_orchestrator.models import AIAuditLog


class Command(BaseCommand):
    help='Queue fresh reports for up to three explicit deals, retaining source review items and AI History.'

    def add_arguments(self,parser):
        parser.add_argument('--deal',action='append',required=True)
        parser.add_argument('--preview',action='store_true')

    def handle(self,*args,**options):
        ids=list(dict.fromkeys(options['deal']))
        if len(ids)>3:raise CommandError('The pilot is limited to three deals.')
        if settings.AI_INFERENCE_TARGET!='h100' or settings.CHAT_MODEL_CONTEXT_TOKENS!=262144:
            raise CommandError('This pilot requires the configured H100 256K profile.')
        if settings.AI_SLOT_TRANSPORT_ENABLED:raise CommandError('vLLM must not use llama.cpp slot transport.')
        deals=list(Deal.objects.filter(pk__in=ids))
        if len(deals)!=len(ids):raise CommandError('One or more selected deals do not exist.')
        for deal in deals:
            readiness=FolderAnalysisService.deal_analysis_readiness(deal)
            if not readiness['ready'] and not readiness['can_build_with_gaps']:
                raise CommandError('%s has no usable source artifacts.' % deal.title)
        for deal in deals:
            if options['preview']:
                self.stdout.write(json.dumps({'deal_id':str(deal.id),'title':deal.title,'status':'preview','context':settings.CHAT_MODEL_CONTEXT_TOKENS,'concurrency':settings.AI_INFERENCE_MAX_CONCURRENT_REQUESTS}))
                continue
            result=FolderAnalysisService.trigger_vdr_analysis(deal,allow_gaps=True,force_regenerate=True,use_review_feedback=True)
            if result.get('error'):raise CommandError(result['error'])
            if result.get('audit_log_id'):
                audit=AIAuditLog.objects.get(pk=result['audit_log_id'])
                audit.source_metadata={**(audit.source_metadata or {}),'financial_reliability_pilot':True,'pilot_context':262144,'source_review_policy':'record_issues_and_pipeline_failures_separately'}
                audit.save(update_fields=['source_metadata'])
            self.stdout.write(json.dumps({'deal_id':str(deal.id),'title':deal.title,**{key:result.get(key) for key in ['status','audit_log_id','queue_position','task_id']}}))
