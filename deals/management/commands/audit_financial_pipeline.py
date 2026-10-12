"""Audit a frozen deal cohort without inference or destructive report updates."""
import json
from pathlib import Path
from copy import deepcopy
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from deals.models import Deal, DealAnalysis
from deals.services.financial_snapshot import snapshot_for_deal, render_main_table, validate_report
from deals.services.analysis_section_rewrite import AnalysisSectionRewriteService
from deals.services.financial_table_rewrite import merge_financial_table
from ai_orchestrator.services.report_sections import ICReportSectionService


class Command(BaseCommand):
    help = 'Preview source coverage and financial repair; --apply persists snapshots, --repair-report creates a new report version.'

    def add_arguments(self, parser):
        parser.add_argument('--deal', action='append', default=[])
        parser.add_argument('--cohort-file')
        parser.add_argument('--output')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--repair-report', action='store_true')

    def handle(self, *args, **options):
        ids = options['deal']
        if options['cohort_file']:
            records = json.loads(Path(options['cohort_file']).read_text())
            ids += [row['deal_id'] for row in records]
        if not ids: raise CommandError('Pass explicit deal IDs or a frozen cohort file.')
        if options['repair_report'] and not options['apply']: raise CommandError('--repair-report requires --apply.')
        results=[]
        for identity in dict.fromkeys(ids):
            with transaction.atomic():
                deal=Deal.objects.select_for_update().get(pk=identity) if options['apply'] else Deal.objects.get(pk=identity)
                snapshot=snapshot_for_deal(deal,persist=options['apply'])
                citations={};table,gaps=render_main_table(snapshot,citations)
                report=(deal.latest_analysis.analysis_json or {}).get('analyst_report','') if deal.latest_analysis else deal.deal_summary or ''
                result={'deal_id':str(deal.id),'title':deal.title,'coverage':snapshot['coverage'],'main_cell_gaps':gaps,'existing_report_validation':validate_report(report,snapshot),'repaired':False}
                if options['repair_report'] and table and deal.latest_analysis:
                    original,_,_=AnalysisSectionRewriteService.locate_section(report,'Key Financials')
                    generated=ICReportSectionService._normalize_section('Key Financials','## Key Financials\n\n'+table,citations=citations)
                    updated,_=merge_financial_table(original,generated)
                    full=AnalysisSectionRewriteService.replace_section(report,'Key Financials',updated)
                    if full!=report:
                        prior=deal.latest_analysis;payload=deepcopy(prior.analysis_json)
                        payload['analyst_report']=full
                        if isinstance(payload.get('canonical_snapshot'),dict):payload['canonical_snapshot']['analyst_report']=full
                        payload['financial_validation']=validate_report(full,snapshot)
                        payload['metadata']={**payload.get('metadata',{}),'financial_snapshot_id':snapshot['id'],'financial_repair_from_analysis':str(prior.id),'financial_repair_review_required':True}
                        analysis=DealAnalysis.objects.create(deal=deal,version=prior.version+1,analysis_kind=prior.analysis_kind,thinking=prior.thinking,ambiguities=prior.ambiguities,analysis_json=payload)
                        deal.deal_summary=full;deal.save(update_fields=['deal_summary'])
                        result.update(repaired=True,analysis_id=str(analysis.id),version=analysis.version)
                results.append(result)
                self.stdout.write(json.dumps({key:value for key,value in result.items() if key!='existing_report_validation'}))
        if options['output']:Path(options['output']).write_text(json.dumps(results,indent=2))
