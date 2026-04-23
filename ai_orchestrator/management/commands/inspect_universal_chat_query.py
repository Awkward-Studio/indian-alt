import json
import os
import resource
import time
import traceback

from django.core.management.base import BaseCommand, CommandError

from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.universal_chat import UniversalChatService


class Command(BaseCommand):
    help = "Run a dynamic universal-chat retrieval simulation and print the planner, selected deals, and selected chunks."
    STAGE_CHOICES = ("plan", "deals", "chunks", "serialize", "context", "analysis")

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
            "--compact-output",
            action="store_true",
            help="Omit heavyweight payload fields from printed output while keeping diagnostics.",
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
        parser.add_argument(
            "--skip-rerank",
            action="store_true",
            help="Disable reranker calls for this inspection run (useful when debugging OOM or endpoint limits).",
        )
        parser.add_argument(
            "--light",
            action="store_true",
            help="Run with reduced retrieval/context budgets to avoid heavy memory usage.",
        )
        parser.add_argument(
            "--legacy-budgets",
            action="store_true",
            help="Run with the previous high-depth retrieval/context budgets for regression testing.",
        )
        parser.add_argument(
            "--unsafe-disable-hard-caps",
            action="store_true",
            help="Disable runtime hard caps for this diagnostic run only. Use with --stop-after to bisect OOM safely.",
        )
        parser.add_argument(
            "--diagnose-live",
            action="store_true",
            help="Print stage-by-stage progress, elapsed time, and memory snapshots while running.",
        )
        parser.add_argument(
            "--stop-after",
            choices=self.STAGE_CHOICES,
            help="Stop execution after the specified stage for targeted debugging.",
        )
        parser.add_argument(
            "--vector-limit",
            type=int,
            help="Override chunk_retrieval.vector_limit for this run.",
        )
        parser.add_argument(
            "--fallback-candidate-limit",
            type=int,
            help="Override chunk_retrieval.fallback_candidate_limit for this run.",
        )
        parser.add_argument(
            "--synthesis-document-candidate-limit",
            type=int,
            help="Override chunk_retrieval.synthesis_document_candidate_limit for this run.",
        )
        parser.add_argument(
            "--chunk-rerank-candidate-limit",
            type=int,
            help="Override chunk_rerank.chunk_rerank_candidate_limit for this run.",
        )
        parser.add_argument(
            "--max-total-chunks",
            type=int,
            help="Override context_assembly.max_total_chunks for this run.",
        )
        parser.add_argument(
            "--max-context-chars",
            type=int,
            help="Override context_assembly.max_context_chars for this run.",
        )
        parser.add_argument(
            "--chunk-excerpt-chars",
            type=int,
            help="Override context_assembly.chunk_excerpt_chars for this run.",
        )
        parser.add_argument(
            "--deal-limit",
            type=int,
            help="Force planner deal_limit after plan generation.",
        )
        parser.add_argument(
            "--chunks-per-deal",
            type=int,
            help="Force planner chunks_per_deal after plan generation.",
        )
        parser.add_argument(
            "--global-chunk-limit",
            type=int,
            help="Force planner global_chunk_limit after plan generation.",
        )

    def _read_vmrss_kb(self) -> int | None:
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            return int(parts[1])
        except Exception:
            return None
        return None

    def _ru_maxrss_kb(self) -> int:
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss or 0)

    def _emit_stage_progress(self, enabled: bool, stage: str, elapsed_ms: float, extra: dict):
        if not enabled:
            return
        vmrss_kb = self._read_vmrss_kb()
        maxrss_kb = self._ru_maxrss_kb()
        details = ", ".join(f"{k}={v}" for k, v in extra.items())
        self.stderr.write(
            f"[diag] stage={stage} elapsed_ms={elapsed_ms:.2f} vmrss_kb={vmrss_kb} maxrss_kb={maxrss_kb}"
            + (f" {details}" if details else "")
        )

    def _stage_record(self, stage: str, started_at: float, **extra):
        return {
            "stage": stage,
            "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 2),
            "vmrss_kb": self._read_vmrss_kb(),
            "maxrss_kb": self._ru_maxrss_kb(),
            **extra,
        }

    def _apply_light_mode(self, service: UniversalChatService):
        stage_by_id = {stage.get("id"): stage for stage in service.flow_config.get("stages", [])}

        planner = (stage_by_id.get("query_planner") or {}).get("settings", {})
        planner.update(
            {
                "default_deal_limit": 8,
                "max_deal_limit": 12,
                "default_chunks_per_deal": 4,
                "max_chunks_per_deal": 8,
            }
        )

        filtering = (stage_by_id.get("deal_filtering") or {}).get("settings", {})
        filtering.update(
            {
                "candidate_pool_limit": 80,
                "result_limit": 8,
            }
        )

        chunk_retrieval = (stage_by_id.get("chunk_retrieval") or {}).get("settings", {})
        chunk_retrieval.update(
            {
                "vector_limit": 80,
                "fallback_candidate_limit": 120,
                "synthesis_document_candidate_limit": 48,
            }
        )

        context_assembly = (stage_by_id.get("context_assembly") or {}).get("settings", {})
        context_assembly.update(
            {
                "max_total_chunks": 24,
                "soft_max_total_chunks": 18,
                "fallback_max_total_chunks": 24,
                "max_context_chars": 50000,
                "chunk_excerpt_chars": 900,
                "min_chunks_per_selected_deal": 2,
                "max_chunks_per_selected_deal": 8,
            }
        )

    def _apply_legacy_budgets(self, service: UniversalChatService):
        stage_by_id = {stage.get("id"): stage for stage in service.flow_config.get("stages", [])}

        planner = (stage_by_id.get("query_planner") or {}).get("settings", {})
        planner.update(
            {
                "default_deal_limit": 20,
                "default_chunks_per_deal": 12,
                "max_deal_limit": 30,
                "max_chunks_per_deal": 20,
            }
        )

        filtering = (stage_by_id.get("deal_filtering") or {}).get("settings", {})
        filtering.update(
            {
                "candidate_pool_limit": 250,
                "result_limit": 20,
            }
        )

        chunk_retrieval = (stage_by_id.get("chunk_retrieval") or {}).get("settings", {})
        chunk_retrieval.update(
            {
                "vector_limit": 500,
                "sqlite_candidate_limit": 800,
                "fallback_candidate_limit": 600,
                "synthesis_document_candidate_limit": 240,
                "default_chunks_per_deal": 12,
            }
        )

        chunk_rerank = (stage_by_id.get("chunk_rerank") or {}).get("settings", {})
        chunk_rerank.update({"chunk_rerank_candidate_limit": 500})

        context_assembly = (stage_by_id.get("context_assembly") or {}).get("settings", {})
        context_assembly.update(
            {
                "max_total_chunks": 120,
                "soft_max_total_chunks": 90,
                "fallback_max_total_chunks": 120,
                "max_context_chars": 260000,
                "chunk_excerpt_chars": 2600,
                "deal_summary_excerpt_chars": 1400,
                "min_chunks_per_selected_deal": 4,
                "max_chunks_per_selected_deal": 32,
                "few_deal_chunk_boost_threshold": 4,
                "few_deal_chunk_boost": 6,
                "single_deal_chunk_boost": 16,
            }
        )

    def _apply_runtime_overrides(self, service: UniversalChatService, options: dict) -> dict:
        stage_by_id = {stage.get("id"): stage for stage in service.flow_config.get("stages", [])}
        chunk_retrieval = (stage_by_id.get("chunk_retrieval") or {}).get("settings", {})
        chunk_rerank = (stage_by_id.get("chunk_rerank") or {}).get("settings", {})
        context_assembly = (stage_by_id.get("context_assembly") or {}).get("settings", {})
        applied = {}

        for key, stage_settings, option_key in [
            ("vector_limit", chunk_retrieval, "vector_limit"),
            ("fallback_candidate_limit", chunk_retrieval, "fallback_candidate_limit"),
            ("synthesis_document_candidate_limit", chunk_retrieval, "synthesis_document_candidate_limit"),
            ("chunk_rerank_candidate_limit", chunk_rerank, "chunk_rerank_candidate_limit"),
            ("max_total_chunks", context_assembly, "max_total_chunks"),
            ("max_context_chars", context_assembly, "max_context_chars"),
            ("chunk_excerpt_chars", context_assembly, "chunk_excerpt_chars"),
        ]:
            value = options.get(option_key)
            if value is None:
                continue
            stage_settings[key] = int(value)
            applied[key] = int(value)
        return applied

    def _run_diagnostic_pipeline(
        self,
        *,
        service: UniversalChatService,
        query: str,
        conversation_id: str,
        run_analysis: bool,
        include_analysis_prompt: bool,
        stop_after: str,
        diagnose_live: bool,
        compact_output: bool,
        plan_overrides: dict,
    ) -> dict:
        records = []
        current_stage = "plan"
        plan = {}
        deals = []
        chunks = []
        chunk_diagnostics = {}
        context_preview = ""
        context_diagnostics = {}
        analysis_input_summary = {}
        serialized_deals = []
        serialized_chunks = []
        analysis_payload = {
            "analysis_answer": None,
            "analysis_model_used": None,
            "analysis_context_preview": None,
            "analysis_prompt_preview": None,
        }
        failure = None
        stopped_early = False

        try:
            t0 = time.perf_counter()
            plan = service._build_query_plan(query, conversation_id)
            for key, value in plan_overrides.items():
                plan[key] = value
            plan_record = self._stage_record(
                "plan",
                t0,
                query_type=plan.get("query_type"),
                result_shape=plan.get("result_shape"),
                selection_mode=plan.get("selection_mode"),
                deal_limit=plan.get("deal_limit"),
                chunks_per_deal=plan.get("chunks_per_deal"),
                global_chunk_limit=plan.get("global_chunk_limit"),
            )
            records.append(plan_record)
            self._emit_stage_progress(diagnose_live, "plan", plan_record["elapsed_ms"], {"deal_limit": plan.get("deal_limit")})
            if stop_after == "plan":
                raise StopIteration

            current_stage = "deals"
            t0 = time.perf_counter()
            deals = service._get_candidate_deals(plan)
            deals_record = self._stage_record("deals", t0, deals_selected=len(deals))
            records.append(deals_record)
            self._emit_stage_progress(diagnose_live, "deals", deals_record["elapsed_ms"], {"deals_selected": len(deals)})
            if stop_after == "deals":
                raise StopIteration

            current_stage = "chunks"
            t0 = time.perf_counter()
            chunks, chunk_diagnostics = service._search_ranked_chunks(plan, deals)
            chunks_record = self._stage_record(
                "chunks",
                t0,
                candidate_chunk_count=chunk_diagnostics.get("candidate_chunk_count", 0),
                selected_chunk_count=chunk_diagnostics.get("selected_chunk_count", len(chunks)),
            )
            records.append(chunks_record)
            self._emit_stage_progress(
                diagnose_live,
                "chunks",
                chunks_record["elapsed_ms"],
                {
                    "candidate_chunk_count": chunk_diagnostics.get("candidate_chunk_count", 0),
                    "selected_chunk_count": chunk_diagnostics.get("selected_chunk_count", len(chunks)),
                },
            )
            if stop_after == "chunks":
                raise StopIteration

            current_stage = "serialize"
            t0 = time.perf_counter()
            serialized_deals = [service._serialize_deal(deal) for deal in deals]
            serialized_chunks = [service._serialize_chunk(item) for item in chunks]
            if compact_output:
                for deal in serialized_deals:
                    deal.pop("current_analysis", None)
                    if deal.get("summary_excerpt"):
                        deal["summary_excerpt"] = str(deal["summary_excerpt"])[:600]
                for chunk in serialized_chunks:
                    chunk["text"] = str(chunk.get("text") or "")[:500]
                    chunk["metadata"] = {}
                    chunk["document_metadata"] = {
                        "document_name": (chunk.get("document_metadata") or {}).get("document_name"),
                        "document_type": (chunk.get("document_metadata") or {}).get("document_type"),
                    }
            serialize_record = self._stage_record(
                "serialize",
                t0,
                serialized_deal_count=len(serialized_deals),
                serialized_chunk_count=len(serialized_chunks),
            )
            records.append(serialize_record)
            self._emit_stage_progress(
                diagnose_live,
                "serialize",
                serialize_record["elapsed_ms"],
                {
                    "serialized_deal_count": len(serialized_deals),
                    "serialized_chunk_count": len(serialized_chunks),
                },
            )
            if stop_after == "serialize":
                raise StopIteration


            current_stage = "context"
            t0 = time.perf_counter()
            context_preview, context_diagnostics = service._format_context_data(
                plan,
                serialized_deals,
                serialized_chunks,
                diagnostics=chunk_diagnostics,
            )
            context_record = self._stage_record(
                "context",
                t0,
                context_chars=len(context_preview),
                omitted_chunk_count=context_diagnostics.get("omitted_chunk_count", 0),
            )
            records.append(context_record)
            self._emit_stage_progress(
                diagnose_live,
                "context",
                context_record["elapsed_ms"],
                {"context_chars": len(context_preview)},
            )
            if stop_after == "context":
                raise StopIteration

            analysis_input_summary = service._build_analysis_input_summary(
                deals=serialized_deals,
                chunks=serialized_chunks,
                context_data=context_preview,
            )

            if run_analysis:
                current_stage = "analysis"
                t0 = time.perf_counter()
                analysis_payload = service._run_analysis_debug(
                    user_message=query,
                    conversation_id=conversation_id,
                    plan=plan,
                    context_data=context_preview,
                    history_context="",
                    include_prompt=include_analysis_prompt,
                )
                analysis_record = self._stage_record(
                    "analysis",
                    t0,
                    analysis_answer_chars=len(str(analysis_payload.get("analysis_answer") or "")),
                )
                records.append(analysis_record)
                self._emit_stage_progress(diagnose_live, "analysis", analysis_record["elapsed_ms"], {})

        except StopIteration:
            stopped_early = True
        except Exception as exc:
            failure = {
                "failed_stage": current_stage,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }

        if deals and not serialized_deals and (not stopped_early or stop_after not in {"plan", "deals", "chunks"}):
            serialized_deals = [service._serialize_deal(deal) for deal in deals]
            if compact_output:
                for deal in serialized_deals:
                    deal.pop("current_analysis", None)
                    if deal.get("summary_excerpt"):
                        deal["summary_excerpt"] = str(deal["summary_excerpt"])[:600]
        elif deals and not serialized_deals:
            serialized_deals = [
                {
                    "deal_id": str(deal.id),
                    "title": str(deal.title or ""),
                    "industry": str(deal.industry or ""),
                    "sector": str(deal.sector or ""),
                    "current_phase": str(deal.current_phase or ""),
                    "retrieval_score": getattr(deal, "_retrieval_score", None),
                    "retrieval_components": getattr(deal, "_retrieval_components", None),
                }
                for deal in deals
            ]
        if chunks and not serialized_chunks and (not stopped_early or stop_after not in {"chunks"}):
            serialized_chunks = [service._serialize_chunk(item) for item in chunks]
            if compact_output:
                for chunk in serialized_chunks:
                    chunk["text"] = str(chunk.get("text") or "")[:500]
                    chunk["metadata"] = {}
                    chunk["document_metadata"] = {
                        "document_name": (chunk.get("document_metadata") or {}).get("document_name"),
                        "document_type": (chunk.get("document_metadata") or {}).get("document_type"),
                    }
        elif chunks and not serialized_chunks:
            serialized_chunks = [
                {
                    "deal": str(item["chunk"].deal.title or ""),
                    "deal_id": str(item["chunk"].deal_id),
                    "source_type": item["chunk"].source_type,
                    "source_id": str(item["chunk"].source_id or ""),
                    "source_title": (item["chunk"].metadata or {}).get("title") or (item["chunk"].metadata or {}).get("filename"),
                    "score": item.get("score"),
                    "text": "",
                    "metadata": {},
                    "document_metadata": {},
                }
                for item in chunks
            ]

        return {
            "query_plan": plan,
            "candidate_deals": serialized_deals,
            "top_chunks": serialized_chunks,
            "context_preview": context_preview[:4000] if context_preview else "",
            "retrieval_diagnostics": {
                **(chunk_diagnostics or {}),
                **(context_diagnostics or {}),
                "planner_requested_deal_limit": plan.get("deal_limit"),
                "planner_requested_chunks_per_deal": plan.get("chunks_per_deal"),
                "selection_mode": plan.get("selection_mode"),
                "stats_mode": plan.get("stats_mode"),
                "resolved_named_deal_ids": plan.get("_resolved_named_deal_ids", []),
                "deals_selected": len(serialized_deals),
            },
            "analysis_input_summary": analysis_input_summary,
            **analysis_payload,
            "stage_diagnostics": records,
            "pipeline_failure": failure,
        }

    def handle(self, *args, **options):
        query = (options.get("query") or "").strip()
        if not query:
            raise CommandError("A query is required. Example: python manage.py inspect_universal_chat_query \"Tell me about Acme\"")

        conversation_id = str(options.get("conversation_id") or "admin-preview")
        as_json = bool(options.get("as_json"))
        compact_output = bool(options.get("compact_output"))
        show_context = bool(options.get("show_context"))
        run_analysis = bool(options.get("run_analysis"))
        show_analysis_prompt = bool(options.get("show_analysis_prompt"))
        skip_rerank = bool(options.get("skip_rerank"))
        light_mode = bool(options.get("light"))
        legacy_budgets = bool(options.get("legacy_budgets"))
        unsafe_disable_hard_caps = bool(options.get("unsafe_disable_hard_caps"))
        diagnose_live = bool(options.get("diagnose_live"))
        stop_after = str(options.get("stop_after") or ("analysis" if run_analysis else "context"))

        service = UniversalChatService(AIProcessorService())
        if unsafe_disable_hard_caps:
            service.disable_hard_caps = True
        if legacy_budgets:
            self._apply_legacy_budgets(service)
        if light_mode:
            self._apply_light_mode(service)
            skip_rerank = True
        if skip_rerank:
            service.embed_service.reranker_model = ""
        applied_overrides = self._apply_runtime_overrides(service, options)
        plan_overrides = {}
        for key in ("deal_limit", "chunks_per_deal", "global_chunk_limit"):
            value = options.get(key)
            if value is not None:
                plan_overrides[key] = int(value)

        simulation = self._run_diagnostic_pipeline(
            service=service,
            query=query,
            conversation_id=conversation_id,
            run_analysis=run_analysis,
            include_analysis_prompt=show_analysis_prompt,
            stop_after=stop_after,
            diagnose_live=diagnose_live,
            compact_output=compact_output,
            plan_overrides=plan_overrides,
        )
        simulation["flow_version"] = getattr(service.flow_version, "version", None)
        simulation["answer_prompt_preview"] = service._stage_settings("answer_generation").get("prompt_template")
        simulation["runtime_overrides"] = {
            "light_mode": light_mode,
            "legacy_budgets": legacy_budgets,
            "unsafe_disable_hard_caps": unsafe_disable_hard_caps,
            "skip_rerank": skip_rerank,
            "stop_after": stop_after,
            "compact_output": compact_output,
            "stage_settings_overrides": applied_overrides,
            "plan_overrides": plan_overrides,
        }

        if as_json:
            self.stdout.write(json.dumps(simulation, default=str, indent=2))
            if simulation.get("pipeline_failure"):
                raise CommandError(
                    f"Pipeline failed at stage={simulation['pipeline_failure'].get('failed_stage')}: "
                    f"{simulation['pipeline_failure'].get('error')}"
                )
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
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Stage Diagnostics"))
        stage_diagnostics = simulation.get("stage_diagnostics") or []
        if not stage_diagnostics:
            self.stdout.write("  No stage diagnostics were captured.")
        else:
            for item in stage_diagnostics:
                self.stdout.write(
                    f"  - stage={item.get('stage')} elapsed_ms={item.get('elapsed_ms')} "
                    f"vmrss_kb={item.get('vmrss_kb')} maxrss_kb={item.get('maxrss_kb')}"
                )
        failure = simulation.get("pipeline_failure")
        if failure:
            self.stdout.write("")
            self.stdout.write(self.style.ERROR("Pipeline Failure"))
            self.stdout.write(
                f"  stage={failure.get('failed_stage')} error={failure.get('error')}"
            )

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

        if failure:
            raise CommandError(
                f"Pipeline failed at stage={failure.get('failed_stage')}: {failure.get('error')}"
            )
