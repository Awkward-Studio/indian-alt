from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from ai_orchestrator.models import AIAuditLog, AIPipelineDefinition, AIPipelineStage, AIPromptDefinition
from ai_orchestrator.views import _pipeline_inventory
from ai_orchestrator.services.pipeline_registry import (
    PipelineRegistryService,
    RegistryValidationError,
)


class PromptRenderingTests(SimpleTestCase):
    def test_render_requires_declared_and_supplied_variables(self):
        rendered = PipelineRegistryService.render(
            "Deal: {{ deal_title }}\nQuestion: {{ content }}",
            {"deal_title": "Acme", "content": "Summarize"},
            ["deal_title", "content"],
        )
        self.assertEqual(rendered, "Deal: Acme\nQuestion: Summarize")

    def test_render_rejects_missing_or_unknown_variables(self):
        with self.assertRaises(RegistryValidationError):
            PipelineRegistryService.validate_template("{{ deal_title }}", ["content"])
        with self.assertRaises(RegistryValidationError):
            PipelineRegistryService.render("{{ content }}", {}, ["content"])

    def test_business_edit_keeps_active_output_contract(self):
        active = (
            "Assess the deal using {{ content }}.\n"
            "Return exactly one JSON object.\n"
            "```json\n"
            '{"summary": "string"}\n'
            "```"
        )

        composed = PipelineRegistryService.compose_business_edit(
            active, "Prioritise risks and opportunities using {{ content }}."
        )

        self.assertIn("Prioritise risks", composed)
        self.assertIn("Return exactly one JSON object.", composed)
        self.assertIn("```json", composed)
        self.assertIn('{"summary": "string"}', composed)


class PromptRevisionLifecycleTests(TestCase):
    def setUp(self):
        self.definition = AIPromptDefinition.objects.create(
            key="test_prompt",
            name="Test prompt",
            variables=["content"],
        )

    def test_publish_archives_previous_revision_and_resolves_stage(self):
        first = PipelineRegistryService.create_prompt_draft(
            self.definition, user_template="First {{ content }}"
        )
        PipelineRegistryService.publish_prompt(first)
        second = PipelineRegistryService.create_prompt_draft(
            self.definition, user_template="Second {{ content }}"
        )
        PipelineRegistryService.publish_prompt(second)

        pipeline = AIPipelineDefinition.objects.create(key="test_pipeline", name="Test")
        AIPipelineStage.objects.create(
            pipeline=pipeline,
            key="answer",
            name="Answer",
            kind=AIPipelineStage.Kind.PROMPT,
            prompt_definition=self.definition,
            required_variables=["content"],
        )
        resolved = PipelineRegistryService.resolve_stage("test_pipeline", "answer")

        first.refresh_from_db()
        self.assertEqual(first.status, "archived")
        self.assertEqual(resolved.prompt_revision.pk, second.pk)

    def test_seed_backfills_core_stages_with_published_revisions(self):
        call_command("seed_ai_prompts", verbosity=0)

        resolved = PipelineRegistryService.resolve_stage(
            "onedrive_analysis", "document_analysis"
        )

        self.assertEqual(resolved.skill_revision.skill.name, "document_analysis")
        self.assertEqual(resolved.skill_revision.status, "published")

    def test_seed_registers_research_and_ocr_stages(self):
        call_command("seed_ai_prompts", verbosity=0)

        competitor = PipelineRegistryService.resolve_stage(
            "competitor_research", "extract"
        )
        ocr = PipelineRegistryService.resolve_stage("document_ocr", "transcribe")

        self.assertEqual(competitor.prompt_revision.definition.key, "competitor_research_extract")
        self.assertEqual(ocr.prompt_revision.definition.key, "ocr_transcription")
        public_search = PipelineRegistryService.resolve_stage(
            "competitor_research", "public_search"
        )
        private_search = PipelineRegistryService.resolve_stage(
            "competitor_research", "private_search"
        )
        screener = PipelineRegistryService.resolve_stage(
            "competitor_research", "screener_resolution"
        )
        self.assertEqual(public_search.stage.kind, AIPipelineStage.Kind.OPERATION)
        self.assertEqual(private_search.stage.depends_on, ["query_planner"])
        self.assertEqual(screener.stage.depends_on, ["grounding"])

    def test_inventory_exposes_registered_topology_and_live_stage_state(self):
        call_command("seed_ai_prompts", verbosity=0)
        extraction = AIPipelineStage.objects.get(pipeline__key="deal_ingestion", key="extraction")
        self.assertEqual(extraction.depends_on, ["normalization"])
        log = AIAuditLog.objects.create(
            source_type="pipeline_test",
            pipeline=extraction.pipeline,
            pipeline_stage=extraction,
            model_used="test-model",
            system_prompt="",
            user_prompt="",
            raw_response="",
            status="PROCESSING",
        )

        pipeline = next(row for row in _pipeline_inventory() if row["key"] == "deal_ingestion")
        stage = next(row for row in pipeline["stages"] if row["key"] == "extraction")

        self.assertEqual(stage["depends_on"], ["normalization"])
        self.assertEqual(stage["runtime"]["active_runs"], 1)
        self.assertEqual(stage["runtime"]["last_run"]["id"], str(log.id))
        self.assertEqual(pipeline["active_runs"], 1)
