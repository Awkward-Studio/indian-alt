from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from deals.models import Deal, DealAnalysis
from deals.services.analysis_next_steps import TASK_INTERFACE_FIELDS, inspect_analysis_next_steps


def analysis_report(analysis: DealAnalysis) -> str:
    payload = analysis.analysis_json if isinstance(analysis.analysis_json, dict) else {}
    report = payload.get("analyst_report")
    if isinstance(report, str) and report.strip():
        return report
    snapshot = payload.get("canonical_snapshot")
    if isinstance(snapshot, dict):
        report = snapshot.get("analyst_report")
        if isinstance(report, str):
            return report
    return ""


class Command(BaseCommand):
    help = "Inspect next-step tables in analysis Markdown and preview normalized task-interface records (read-only)."

    def add_arguments(self, parser):
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument("--analysis-id", help="Inspect one DealAnalysis UUID.")
        source.add_argument("--deal-id", help="Inspect the latest analysis for one Deal UUID.")
        source.add_argument("--deal-title", help="Inspect the latest analysis for one exact deal title.")
        source.add_argument("--markdown-file", help="Inspect a local Markdown file instead of the database.")
        source.add_argument("--all", action="store_true", help="Inspect the latest analysis for every deal.")
        parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of the text report.")
        parser.add_argument("--output", help="Write output to this file instead of stdout.")

    def handle(self, *args, **options):
        documents = self._load_documents(options)
        results = []
        for document in documents:
            result = inspect_analysis_next_steps(document["markdown"])
            result["document"] = {key: value for key, value in document.items() if key != "markdown"}
            results.append(result)

        output = json.dumps(results, indent=2, ensure_ascii=False) if options["json"] else self._text_report(results)
        if options.get("output"):
            output_path = Path(options["output"]).expanduser()
            output_path.write_text(output + "\n", encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Wrote next-step audit to {output_path}"))
        else:
            self.stdout.write(output)

    def _load_documents(self, options):
        if options.get("markdown_file"):
            path = Path(options["markdown_file"]).expanduser()
            if not path.is_file():
                raise CommandError(f"Markdown file not found: {path}")
            return [{"source": str(path), "markdown": path.read_text(encoding="utf-8")}]

        if options.get("analysis_id"):
            try:
                analyses = [DealAnalysis.objects.select_related("deal").get(id=options["analysis_id"])]
            except (DealAnalysis.DoesNotExist, ValueError):
                raise CommandError(f"Analysis not found: {options['analysis_id']}") from None
        elif options.get("deal_id") or options.get("deal_title"):
            query = {"id": options["deal_id"]} if options.get("deal_id") else {"title": options["deal_title"]}
            try:
                deal = Deal.objects.get(**query)
            except (Deal.DoesNotExist, Deal.MultipleObjectsReturned, ValueError) as exc:
                raise CommandError(f"Could not resolve exactly one deal: {exc}") from None
            latest = deal.latest_analysis
            if not latest:
                raise CommandError(f"Deal has no analysis records: {deal.title}")
            analyses = [latest]
        else:
            analyses = []
            seen_deals = set()
            queryset = DealAnalysis.objects.select_related("deal").order_by("deal_id", "-version", "-created_at")
            for analysis in queryset.iterator():
                if analysis.deal_id in seen_deals:
                    continue
                seen_deals.add(analysis.deal_id)
                analyses.append(analysis)

        documents = []
        for analysis in analyses:
            report = analysis_report(analysis)
            documents.append(
                {
                    "source": "database",
                    "deal_id": str(analysis.deal_id),
                    "deal_title": analysis.deal.title,
                    "analysis_id": str(analysis.id),
                    "analysis_version": analysis.version,
                    "markdown": report,
                }
            )
        return documents

    def _text_report(self, results):
        lines = []
        for result in results:
            document = result["document"]
            title = document.get("deal_title") or document.get("source") or "Analysis"
            summary = result["summary"]
            lines.extend(
                [
                    f"=== {title} ===",
                    f"Analysis: {document.get('analysis_id', 'file')} | Version: {document.get('analysis_version', 'n/a')}",
                    (
                        "Tables: "
                        f"{summary['section_tables']} section next-step, "
                        f"{summary['canonical_task_tables']} canonical task, "
                        f"{summary['action_tables']} action"
                    ),
                    f"Task candidates: {summary['task_candidates']}",
                    "Field coverage: "
                    + ", ".join(
                        f"{field}={summary['field_coverage'][field]}/{summary['task_candidates']}"
                        for field in TASK_INTERFACE_FIELDS
                    ),
                ]
            )
            for table in result["tables"]:
                lines.append(
                    f"\n[{table['section']}] {table['table_kind']} "
                    f"(line {table['source_line']}; headers: {' | '.join(table['headers'])})"
                )
                for task in table["tasks"]:
                    category = f"{task['category']}: " if task.get("category") else ""
                    lines.append(f"  - {category}{task['task']}")
                    lines.append(
                        "    "
                        + " | ".join(
                            [
                                f"owner={task.get('owner') or '-'}",
                                f"assignee={task.get('assignee') or '-'}",
                                f"status={task.get('status') or '-'}",
                                f"priority={task.get('priority') or '-'}",
                                f"due_date={task.get('due_date') or '-'}",
                                f"missing={','.join(task['missing_fields']) or 'none'}",
                            ]
                        )
                    )
            lines.append("")
        return "\n".join(lines).rstrip()
