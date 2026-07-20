from __future__ import annotations

import hashlib
import re
import unicodedata
from difflib import SequenceMatcher

from django.db import transaction

from deals.models import Deal, DealAnalysis
from deals.services.analysis_next_steps import inspect_analysis_next_steps
from .models import Task, TaskPriority, TaskSuggestion, TaskSuggestionState


MATCH_THRESHOLD = 0.72


def analysis_report(analysis: DealAnalysis | None, deal: Deal | None = None) -> str:
    if analysis:
        payload = analysis.analysis_json if isinstance(analysis.analysis_json, dict) else {}
        report = payload.get("analyst_report")
        if isinstance(report, str) and report.strip():
            return report
        snapshot = payload.get("canonical_snapshot")
        if isinstance(snapshot, dict) and isinstance(snapshot.get("analyst_report"), str):
            return snapshot["analyst_report"]
    return (deal.deal_summary if deal else "") or ""


def normalized_task_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    return " ".join(re.findall(r"[a-z0-9]+", value))


def task_fingerprint(value: str) -> str:
    return hashlib.sha256(normalized_task_text(value).encode("utf-8")).hexdigest()


def concise_task_title(suggestion: TaskSuggestion) -> str:
    category = (suggestion.category or "").strip()
    if category:
        return category[:160]
    description = (suggestion.title or "").strip()
    colon_heading = description.split(":", 1)[0].strip() if ":" in description else ""
    if 3 <= len(colon_heading) <= 100:
        return colon_heading
    first_sentence = re.split(r"(?<=[.!?])\s+", description, maxsplit=1)[0].strip()
    if first_sentence and len(first_sentence) <= 100:
        return first_sentence
    return f"{description[:97].rstrip()}..." if len(description) > 100 else description


def _similarity(left: str, right: str) -> float:
    left_normalized = normalized_task_text(left)
    right_normalized = normalized_task_text(right)
    if not left_normalized or not right_normalized:
        return 0.0
    sequence = SequenceMatcher(None, left_normalized, right_normalized).ratio()
    left_tokens, right_tokens = set(left_normalized.split()), set(right_normalized.split())
    overlap = len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))
    return max(sequence, overlap)


def _reference(task: dict) -> dict:
    return {
        "section": task.get("source_section") or "Document",
        "table": task.get("source_table"),
        "line": task.get("source_line"),
        "table_kind": task.get("table_kind") or "",
        "row": task.get("source_row") or [],
    }


def merged_task_candidates(markdown: str) -> list[dict]:
    parsed = inspect_analysis_next_steps(markdown)
    tasks = []
    for table in parsed["tables"]:
        for item in table["tasks"]:
            tasks.append({**item, "table_kind": table["table_kind"]})

    section_tasks = [task for task in tasks if task["table_kind"] != "canonical_task_table"]
    canonical_tasks = [task for task in tasks if task["table_kind"] == "canonical_task_table"]
    candidates: list[dict] = []
    by_fingerprint: dict[str, dict] = {}

    for task in section_tasks:
        fingerprint = task_fingerprint(task.get("task") or "")
        if not fingerprint:
            continue
        if fingerprint in by_fingerprint:
            by_fingerprint[fingerprint]["source_references"].append(_reference(task))
            continue
        candidate = {
            "fingerprint": fingerprint,
            "title": task.get("task") or "",
            "category": task.get("category") or "",
            "source_section": task.get("source_section") or "Document",
            "source_table_kind": task["table_kind"],
            "source_owner": task.get("owner") or "",
            "source_assignee": task.get("assignee") or "",
            "source_status": task.get("status") or "",
            "source_priority": task.get("priority") or "",
            "source_references": [_reference(task)],
            "matched_canonical": False,
        }
        candidates.append(candidate)
        by_fingerprint[fingerprint] = candidate

    matched_candidate_ids: set[int] = set()
    for task in canonical_tasks:
        fingerprint = task_fingerprint(task.get("task") or "")
        exact = by_fingerprint.get(fingerprint)
        best = exact
        if not best:
            scored = sorted(
                ((_similarity(task.get("task") or "", candidate["title"]), candidate) for candidate in candidates),
                key=lambda pair: pair[0],
                reverse=True,
            )
            if scored and scored[0][0] >= MATCH_THRESHOLD and id(scored[0][1]) not in matched_candidate_ids:
                best = scored[0][1]
        if best:
            matched_candidate_ids.add(id(best))
            best["matched_canonical"] = True
            best["source_references"].append(_reference(task))
            for target, source in (
                ("source_owner", "owner"), ("source_assignee", "assignee"),
                ("source_status", "status"), ("source_priority", "priority"),
            ):
                if not best[target] and task.get(source):
                    best[target] = task[source]
            continue
        candidate = {
            "fingerprint": fingerprint,
            "title": task.get("task") or "",
            "category": task.get("category") or "",
            "source_section": task.get("source_section") or "Next Steps",
            "source_table_kind": task["table_kind"],
            "source_owner": task.get("owner") or "",
            "source_assignee": task.get("assignee") or "",
            "source_status": task.get("status") or "",
            "source_priority": task.get("priority") or "",
            "source_references": [_reference(task)],
            "matched_canonical": True,
        }
        candidates.append(candidate)
        by_fingerprint[fingerprint] = candidate
    return candidates


def _priority(value: str) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in TaskPriority.values else TaskPriority.MEDIUM


@transaction.atomic
def sync_deal_suggestions(deal: Deal, analysis: DealAnalysis | None = None) -> dict:
    analysis = analysis or deal.latest_analysis
    markdown = analysis_report(analysis, deal)
    report_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    candidates = merged_task_candidates(markdown)

    TaskSuggestion.objects.filter(deal=deal, state=TaskSuggestionState.PENDING).exclude(
        report_hash=report_hash
    ).update(state=TaskSuggestionState.SUPERSEDED)

    current_fingerprints = set()
    created = updated = 0
    for candidate in candidates:
        fingerprint = candidate["fingerprint"]
        current_fingerprints.add(fingerprint)
        existing_task = Task.objects.filter(deal=deal, fingerprint=fingerprint).first()
        persisted_candidate = {
            key: value for key, value in candidate.items()
            if key in {
                "title", "category", "source_section", "source_table_kind", "source_owner",
                "source_assignee", "source_status", "source_priority", "source_references",
            }
        }
        suggestion, was_created = TaskSuggestion.objects.get_or_create(
            deal=deal,
            report_hash=report_hash,
            fingerprint=fingerprint,
            defaults={
                **persisted_candidate,
                "analysis": analysis,
                "analysis_version": analysis.version if analysis else None,
                "task": existing_task,
                "state": TaskSuggestionState.ACCEPTED if existing_task else TaskSuggestionState.PENDING,
            },
        )
        if was_created:
            created += 1
            continue
        for field in (
            "title", "category", "source_section", "source_table_kind", "source_owner",
            "source_assignee", "source_status", "source_priority", "source_references",
        ):
            setattr(suggestion, field, candidate[field])
        suggestion.analysis = analysis
        suggestion.analysis_version = analysis.version if analysis else None
        if existing_task and suggestion.state not in (TaskSuggestionState.DISMISSED, TaskSuggestionState.ACCEPTED):
            suggestion.task = existing_task
            suggestion.state = TaskSuggestionState.ACCEPTED
        suggestion.save()
        updated += 1

    TaskSuggestion.objects.filter(
        deal=deal, report_hash=report_hash, state=TaskSuggestionState.PENDING
    ).exclude(fingerprint__in=current_fingerprints).update(state=TaskSuggestionState.SUPERSEDED)
    return {"created": created, "updated": updated, "candidates": len(candidates), "report_hash": report_hash}


def sync_latest_deal_suggestions(deal_id) -> dict:
    deal = Deal.objects.get(id=deal_id)
    return sync_deal_suggestions(deal, deal.latest_analysis)


def ensure_latest_suggestions(deal: Deal) -> None:
    analysis = deal.latest_analysis
    markdown = analysis_report(analysis, deal)
    report_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    if not TaskSuggestion.objects.filter(deal=deal, report_hash=report_hash).exists():
        sync_deal_suggestions(deal, analysis)


def accepted_task_defaults(suggestion: TaskSuggestion) -> dict:
    return {
        "deal": suggestion.deal,
        "title": concise_task_title(suggestion),
        "description": suggestion.title,
        "priority": _priority(suggestion.source_priority),
        "origin": Task.Origin.ANALYSIS,
        "fingerprint": suggestion.fingerprint,
    }
