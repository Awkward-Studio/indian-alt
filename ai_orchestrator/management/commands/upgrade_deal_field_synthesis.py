from django.core.management.base import BaseCommand
from django.db import transaction
from ai_orchestrator.models import AISkill
from ai_orchestrator.prompt_contracts import (
    DEAL_FIELD_SYNTHESIS_SYSTEM_TEMPLATE,
    DEAL_FIELD_SYNTHESIS_PROMPT_TEMPLATE,
    DEAL_FIELD_SYNTHESIS_JSON_SCHEMA,
)
from ai_orchestrator.services.pipeline_registry import PipelineRegistryService


class Command(BaseCommand):
    help = 'Publish the dedicated ledger-field synthesis contract without changing IC report prompts.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    @transaction.atomic
    def handle(self, *args, **options):
        skill = AISkill.objects.get(name='deal_field_synthesis')
        published = skill.revisions.filter(status='PUBLISHED').order_by('-revision').first()
        if published and (published.system_template, published.prompt_template, published.output_schema) == (
            DEAL_FIELD_SYNTHESIS_SYSTEM_TEMPLATE, DEAL_FIELD_SYNTHESIS_PROMPT_TEMPLATE, DEAL_FIELD_SYNTHESIS_JSON_SCHEMA
        ):
            self.stdout.write('Dedicated ledger-field prompt is already current.')
            return
        if not options['apply']:
            self.stdout.write('Would publish the dedicated ledger-field prompt. Use --apply to publish.')
            return
        revision = PipelineRegistryService.create_skill_draft(skill,
            system_template=DEAL_FIELD_SYNTHESIS_SYSTEM_TEMPLATE,
            prompt_template=DEAL_FIELD_SYNTHESIS_PROMPT_TEMPLATE,
            output_schema=DEAL_FIELD_SYNTHESIS_JSON_SCHEMA)
        PipelineRegistryService.publish_skill(revision)
        self.stdout.write(f'Published deal_field_synthesis r{revision.revision}.')
