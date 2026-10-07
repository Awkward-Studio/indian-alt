"""Publish reversible citation/readability fixes while preserving analytical instructions."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ai_orchestrator.services.bulk_prompt_contracts import IC_REPORT_SECTION_STAGE_KEYS
from ai_orchestrator.services.pipeline_registry import PipelineRegistryService
from ai_orchestrator.services.report_prompt_quality import upgrade_report_prompt


class Command(BaseCommand):
    help = "Preview IC section prompt contract fixes; publish new revisions with --apply."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        # Lock stages before reading so concurrent authoring cannot be overwritten.
        from ai_orchestrator.models import AIPipelineStage
        list(AIPipelineStage.objects.select_for_update().filter(
            pipeline__key="ic_report_generation", key__in=IC_REPORT_SECTION_STAGE_KEYS.values(),
        ))
        changed = 0
        for title, key in IC_REPORT_SECTION_STAGE_KEYS.items():
            resolved = PipelineRegistryService.resolve_stage("ic_report_generation", key)
            old = resolved.prompt_revision
            if not old:
                raise CommandError(f"No published prompt for {title}.")
            system, user = upgrade_report_prompt(old.system_template, old.user_template)
            PipelineRegistryService.validate_template(user, old.definition.variables)
            if (system, user) == (old.system_template, old.user_template):
                self.stdout.write(f"{title}: already current (r{old.revision})")
                continue
            changed += 1
            if options["apply"]:
                draft = PipelineRegistryService.create_prompt_draft(
                    old.definition, system_template=system, user_template=user,
                    input_schema=old.input_schema, output_schema=old.output_schema,
                )
                PipelineRegistryService.publish_prompt(draft)
                self.stdout.write(f"{title}: r{old.revision} -> r{draft.revision}; previous={old.id}")
            else:
                self.stdout.write(f"{title}: would replace r{old.revision}; previous={old.id}")
        self.stdout.write(f"{'Published' if options['apply'] else 'Previewed'} {changed} section prompt revisions.")
