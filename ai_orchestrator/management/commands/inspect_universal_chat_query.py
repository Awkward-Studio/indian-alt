import json

from django.core.management.base import BaseCommand, CommandError

from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.universal_chat import UniversalChatService


class Command(BaseCommand):
    help = "Run a dynamic universal-chat retrieval simulation and print the planner, selected deals, and selected chunks."

    def add_arguments(self, parser):
        parser.add_argument(
            "query",
            nargs="?",
            type=str,
            help="User query to run through the planner + retrieval stack.",
        )
        parser.add_argument(
            "--conversation-id",
            type=str,
            default="admin-preview",
            help="Conversation id to pass into planner simulation.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Print the full simulation payload as JSON instead of a formatted report.",
        )
        parser.add_argument(
            "--show-context",
            action="store_true",
            help="Print the context preview block as well.",
        )
        parser.add_argument(
            "--run-analysis",
            action="store_true",
            help="Run the final answer-generation step after retrieval.",
        )
        parser.add_argument(
            "--show-analysis-prompt",
            action="store_true",
            help="Print the rendered analysis prompt when --run-analysis is enabled.",
        )

    def handle(self, *args, **options):
        query = (options.get("query") or "").strip()
        if not query:
            raise CommandError("A query is required. Example: python manage.py inspect_universal_chat_query \"Tell me about Acme\"")

        conversation_id = str(options.get("conversation_id") or "admin-preview")
        as_json = bool(options.get("as_json"))
        show_context = bool(options.get("show_context"))
        run_analysis = bool(options.get("run_analysis"))
        show_analysis_prompt = bool(options.get("show_analysis_prompt"))

        service = UniversalChatService(AIProcessorService())
        simulation = service.simulate_query(
            query,
            conversation_id=conversation_id,
            run_analysis=run_analysis,
            include_analysis_prompt=show_analysis_prompt,
        )

        if as_json:
            self.stdout.write(json.dumps(simulation, default=str, indent=2))
            return

        self.stdout.write(self.style.SUCCESS("Query"))
        self.stdout.write(f"  {query}")
        self.stdout.write("")

        self.stdout.write(self.style.SUCCESS("Planner"))
        self.stdout.write(json.dumps(simulation.get("query_plan") or {}, default=str, indent=2))
        self.stdout.write("")

        query_plan = simulation.get("query_plan") or {}
        named_entities = query_plan.get("named_entities") or []
        self.stdout.write(self.style.SUCCESS(f"Named Entities ({len(named_entities)})"))
        if not named_entities:
            self.stdout.write("  No named entities were provided by the planner.")
        else:
            for index, entity in enumerate(named_entities, start=1):
                entity_type = entity.get("type") or "unknown"
                text = entity.get("text") or ""
                confidence = entity.get("confidence")
                self.stdout.write(f"{index}. type={entity_type} | text={text} | confidence={confidence}")
        self.stdout.write("")

        candidate_deals = simulation.get("candidate_deals") or []
        self.stdout.write(self.style.SUCCESS(f"Deals ({len(candidate_deals)})"))
        if not candidate_deals:
            self.stdout.write("  No candidate deals were selected.")
        else:
            for index, deal in enumerate(candidate_deals, start=1):
                deal_id = deal.get("deal_id") or "N/A"
                title = deal.get("title") or "Untitled"
                industry = deal.get("industry") or "N/A"
                sector = deal.get("sector") or "N/A"
                phase = deal.get("current_phase") or "N/A"
                score = deal.get("retrieval_score")
                components = deal.get("retrieval_components") or {}
                self.stdout.write(
                    f"{index}. {title} | deal_id={deal_id} | industry={industry} | sector={sector} | phase={phase} | retrieval_score={score}"
                )
                if components:
                    self.stdout.write(f"   components={json.dumps(components, default=str, ensure_ascii=True)}")
        self.stdout.write("")

        top_chunks = simulation.get("top_chunks") or []
        self.stdout.write(self.style.SUCCESS(f"Chunks ({len(top_chunks)})"))
        if not top_chunks:
            self.stdout.write("  No chunks were selected.")
        else:
            for index, chunk in enumerate(top_chunks, start=1):
                deal = chunk.get("deal") or "Unknown Deal"
                source_title = chunk.get("source_title") or chunk.get("source_type") or "Unknown Source"
                source_type = chunk.get("source_type") or "unknown"
                score = chunk.get("score")
                metadata = chunk.get("metadata") or {}
                document_metadata = chunk.get("document_metadata") or {}
                deal_id = chunk.get("deal_id") or "N/A"
                source_id = chunk.get("source_id") or "N/A"
                chunk_index = metadata.get("chunk_index", "N/A")
                text = (chunk.get("text") or "").replace("\n", " ").strip()
                excerpt = text[:300] + ("..." if len(text) > 300 else "")

                self.stdout.write(
                    f"{index}. deal={deal} | deal_id={deal_id} | source={source_title} | source_id={source_id} | source_type={source_type} | chunk_index={chunk_index} | score={score}"
                )
                if metadata:
                    self.stdout.write(f"   metadata={json.dumps(metadata, default=str, ensure_ascii=True)}")
                if document_metadata:
                    self.stdout.write(
                        f"   document_metadata={json.dumps(document_metadata, default=str, ensure_ascii=True)}"
                    )
                if excerpt:
                    self.stdout.write(f"   excerpt={excerpt}")
        self.stdout.write("")

        diagnostics = simulation.get("retrieval_diagnostics") or {}
        self.stdout.write(self.style.SUCCESS("Resolved Scope"))
        self.stdout.write(
            f"  selection_mode={diagnostics.get('selection_mode')} | stats_mode={diagnostics.get('stats_mode')}"
        )
        self.stdout.write(
            f"  resolved_named_deal_ids={json.dumps(diagnostics.get('resolved_named_deal_ids') or [], default=str)}"
        )
        self.stdout.write(
            f"  chunk_scope_deal_ids={json.dumps(diagnostics.get('chunk_scope_deal_ids') or [], default=str)}"
        )
        self.stdout.write(
            f"  chunk_scope_deal_titles={json.dumps(diagnostics.get('chunk_scope_deal_titles') or [], default=str, ensure_ascii=True)}"
        )
        selected_chunk_details = diagnostics.get("selected_chunk_details") or []
        if selected_chunk_details:
            self.stdout.write("  selected_chunk_details:")
            for index, item in enumerate(selected_chunk_details, start=1):
                self.stdout.write(
                    "    "
                    + f"{index}. deal={item.get('deal_title')} | deal_id={item.get('deal_id')} | "
                    + f"source={item.get('source_title')} | source_type={item.get('source_type')} | "
                    + f"chunk_index={item.get('chunk_index')} | score={item.get('score')}"
                )
        self.stdout.write("")

        self.stdout.write(self.style.SUCCESS("Diagnostics"))
        self.stdout.write(json.dumps(diagnostics, default=str, indent=2))

        analysis_input_summary = simulation.get("analysis_input_summary") or {}
        if run_analysis or analysis_input_summary:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("Analysis Input Summary"))
            self.stdout.write(
                f"  selected_deal_count={analysis_input_summary.get('selected_deal_count', 0)} | "
                f"selected_chunk_count={analysis_input_summary.get('selected_chunk_count', 0)} | "
                f"context_chars={analysis_input_summary.get('context_chars', 0)}"
            )
            selected_deals = analysis_input_summary.get("selected_deals") or []
            if selected_deals:
                self.stdout.write("  selected_deals:")
                for index, item in enumerate(selected_deals, start=1):
                    self.stdout.write(
                        f"    {index}. deal={item.get('title')} | deal_id={item.get('deal_id')}"
                    )
            selected_chunk_mappings = analysis_input_summary.get("selected_chunk_mappings") or []
            if selected_chunk_mappings:
                self.stdout.write("  selected_chunk_mappings:")
                for index, item in enumerate(selected_chunk_mappings, start=1):
                    self.stdout.write(
                        f"    {index}. deal={item.get('deal_title')} | deal_id={item.get('deal_id')} | "
                        f"source={item.get('source_title')} | source_type={item.get('source_type')} | source_id={item.get('source_id')}"
                    )

        if run_analysis:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("Analysis"))
            self.stdout.write(f"  model={simulation.get('analysis_model_used') or 'N/A'}")
            answer = simulation.get("analysis_answer") or ""
            if answer:
                self.stdout.write(answer)
            else:
                self.stdout.write("  No analysis answer was produced.")

        if show_analysis_prompt:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("Analysis Prompt Preview"))
            self.stdout.write(simulation.get("analysis_prompt_preview") or "")

        if show_context:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("Context Preview"))
            self.stdout.write(simulation.get("context_preview") or "")
