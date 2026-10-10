from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from difflib import SequenceMatcher

from django.db import transaction

from deals.models import Deal, DealAnalysis
from deals.services.analysis_next_steps import inspect_analysis_next_steps
from .models import Task, TaskActivity, TaskPriority, TaskStatus, TaskSuggestion, TaskSuggestionState


MATCH_THRESHOLD = 0.72

TASK_ACTIVITY_FIELDS = (
    "title", "description", "status", "priority", "due_date", "assignee_id",
)


def _bounded_activity_value(value, limit=1000):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}…"


def task_activity_snapshot(task: Task) -> dict:
    return {
        "title": _bounded_activity_value(task.title, 500),
        "description": _bounded_activity_value(task.description, 1000),
        "status": task.status,
        "priority": task.priority,
        "due_date": _bounded_activity_value(task.due_date),
        "assignee_id": str(task.assignee_id) if task.assignee_id else None,
    }


def task_activity_action(before: dict, after: dict) -> str:
    changed = {key for key in TASK_ACTIVITY_FIELDS if before.get(key) != after.get(key)}
    if changed == {"status"}:
        if after.get("status") == TaskStatus.DONE:
            return TaskActivity.Action.COMPLETED
        if before.get("status") == TaskStatus.DONE:
            return TaskActivity.Action.REOPENED
    if changed == {"assignee_id"}:
        return TaskActivity.Action.ASSIGNED
    if changed == {"priority"}:
        return TaskActivity.Action.PRIORITIZED
    if changed == {"due_date"}:
        return TaskActivity.Action.DUE_DATE_CHANGED
    return TaskActivity.Action.UPDATED


def record_task_activity(
    task: Task,
    *,
    actor,
    action=None,
    before=None,
    after=None,
    source_context=None,
):
    before = before or {}
    after = after if after is not None else task_activity_snapshot(task)
    changed_fields = [
        field for field in TASK_ACTIVITY_FIELDS if before.get(field) != after.get(field)
    ]
    resolved_action = action or task_activity_action(before, after)
    if resolved_action == TaskActivity.Action.UPDATED and not changed_fields:
        return None
    return TaskActivity.objects.create(
        task=task if task.pk and Task.objects.filter(pk=task.pk).exists() else None,
        task_id_snapshot=task.id,
        task_title=_bounded_activity_value(task.title, 500),
        deal=task.deal,
        actor=actor,
        action=resolved_action,
        changed_fields=changed_fields,
        before={key: before.get(key) for key in changed_fields},
        after={key: after.get(key) for key in changed_fields},
        source_context=source_context or {},
    )


def analysis_report(analysis: DealAnalysis | None, deal: Deal | None = None) -> str:
    if analysis:
        payload = analysis.analysis_json if isinstance(analysis.analysis_json, dict) else {}
        snapshot = payload.get("canonical_snapshot")
        if isinstance(snapshot, dict) and isinstance(snapshot.get("analyst_report"), str) and snapshot['analyst_report'].strip():
            return snapshot["analyst_report"]
        report = payload.get("analyst_report")
        if isinstance(report, str) and report.strip():
            return report
    return (deal.deal_summary if deal else "") or ""


def latest_task_analysis(deal):
    """Keep task-bearing reports available while synthesis or a partial report runs."""
    latest = deal.latest_analysis
    for analysis in deal.analyses.order_by('-version', '-created_at').iterator(chunk_size=1):
        report = analysis_report(analysis)
        if re.search(r'^#{1,3}\s+(?:\d+[.)]\s*)?Next Steps\b', report, re.I | re.M) or inspect_analysis_next_steps(report)['tasks']:
            return analysis
    return latest


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
    overlap = len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))
    return max(sequence, overlap)


def _reference(task: dict) -> dict:
    return {
        "section": task.get("source_section") or "Document",
        "table": task.get("source_table"),
        "line": task.get("source_line"),
        "table_kind": task.get("table_kind") or "",
        "row": task.get("source_row") or [],
    }


def merged_task_candidates(markdown: str, deal: Deal | None = None) -> list[dict]:
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
    if deal is not None:
        gaps = report_gap_candidates(deal)
        by_fingerprint = {candidate['fingerprint']: candidate for candidate in candidates}
        for gap in gaps:
            existing = by_fingerprint.get(gap['fingerprint'])
            if existing:
                existing['source_references'].extend(gap['source_references'])
                existing['source_table_kind'] = 'report_source_gap'
            else:
                candidates.append(gap)
        candidates.sort(key=lambda item: item['source_table_kind'] != 'report_source_gap')
    return candidates


def report_gap_candidates(deal: Deal) -> list[dict]:
    from deals.services.report_coverage import recorded_report_gaps

    grouped = {}
    for gap in recorded_report_gaps(deal):
        message = str(gap['error']).strip()
        verification = gap.get('issue_kind') == 'verification' or bool(re.search(
            r'could not be independently verified|no source reference|needs period verification', message, re.I))
        metric = re.split(r'\s+in\s+|\s+has no\s+|\s+could not\s+', message, maxsplit=1, flags=re.I)[0]
        # One verification task per metric and section; retain every period and
        # qualification in the evidence instead of repeating the same task title.
        key = (gap['title'], 'verification' if verification else gap.get('issue_kind'),
            normalized_task_text(metric if verification else message))
        group = grouped.setdefault(key, {'gap': gap, 'verification': verification, 'messages': [], 'references': []})
        for detail in [message, *(gap.get('details') or [])]:
            if isinstance(detail, str) and detail.strip() and detail not in group['messages']:
                group['messages'].append(detail)
        group['references'].append({'section': gap['title'], 'table_kind': 'report_source_gap',
            'source_type': 'report_validation', 'source_id': gap['source_id'],
            'issue_kind': gap.get('issue_kind'), 'confirmed': gap.get('confirmed', False),
            'message': message, 'details': gap.get('details') or []})

    candidates = []
    for key, group in grouped.items():
        gap, verification = group['gap'], group['verification']
        section = gap['title']
        if verification:
            metric = re.split(r'\s+in\s+|\s+has no\s+|\s+could not\s+', gap['error'], maxsplit=1, flags=re.I)[0]
            category = f'Verify {metric} in {section}'[:160]
            introduction = f'Verify source support in {section}. Verification is incomplete; this does not establish that the source is missing or the figure is wrong.'
        else:
            category = f"Review {gap.get('issue_kind') or 'source issue'} in {section}: {gap['error'].split(':', 1)[0]}"[:160]
            introduction = f'Review the recorded {gap.get("issue_kind") or "source"} issue in {section} against the original source documents.'
        candidates.append({'fingerprint': task_fingerprint('report source gap ' + json.dumps(key)),
            'title': introduction + '\n\n' + '\n'.join('- ' + message for message in group['messages']),
            'category': category, 'source_section': section, 'source_table_kind': 'report_source_gap',
            'source_owner': '', 'source_assignee': '', 'source_status': 'Needs verification' if verification else 'Needs review',
            'source_priority': '', 'source_references': group['references'], 'matched_canonical': False})
    return candidates


def suggestion_snapshot(markdown: str, deal: Deal):
    candidates = merged_task_candidates(markdown, deal)
    gaps = [item for item in candidates if item['source_table_kind'] == 'report_source_gap']
    # Keep existing hashes unchanged for reports that have no recorded warnings.
    content = markdown + ('\n' + json.dumps(gaps, sort_keys=True) if gaps else '')
    return candidates, hashlib.sha256(content.encode('utf-8')).hexdigest()


def _priority(value: str) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in TaskPriority.values else TaskPriority.MEDIUM


@transaction.atomic
def sync_deal_suggestions(deal: Deal, analysis: DealAnalysis | None = None) -> dict:
    Deal.objects.select_for_update().get(pk=deal.pk)
    analysis = analysis or latest_task_analysis(deal)
    markdown = analysis_report(analysis, deal)
    candidates, report_hash = suggestion_snapshot(markdown, deal)

    TaskSuggestion.objects.filter(deal=deal, state=TaskSuggestionState.PENDING).exclude(
        report_hash=report_hash
    ).update(state=TaskSuggestionState.SUPERSEDED)

    current_fingerprints = set()
    created = updated = 0
    for candidate in candidates:
        fingerprint = candidate["fingerprint"]
        current_fingerprints.add(fingerprint)
        existing_task = Task.objects.filter(deal=deal, fingerprint=fingerprint).first()
        prior = TaskSuggestion.objects.filter(deal=deal, fingerprint=fingerprint).order_by('-created_at', '-updated_at').first()
        initial_state = TaskSuggestionState.ACCEPTED if existing_task else (
            TaskSuggestionState.DISMISSED if prior and prior.state == TaskSuggestionState.DISMISSED else TaskSuggestionState.PENDING)
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
                "state": initial_state,
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
        if suggestion.state == TaskSuggestionState.SUPERSEDED:
            suggestion.state = TaskSuggestionState.ACCEPTED if existing_task else TaskSuggestionState.PENDING
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
    return sync_deal_suggestions(deal, latest_task_analysis(deal))


def ensure_latest_suggestions(deal: Deal) -> None:
    analysis = latest_task_analysis(deal)
    markdown = analysis_report(analysis, deal)
    candidates, report_hash = suggestion_snapshot(markdown, deal)
    current = TaskSuggestion.objects.filter(deal=deal, report_hash=report_hash)
    expected = {(candidate['fingerprint'], candidate['source_section']) for candidate in candidates}
    stored = set(current.exclude(state=TaskSuggestionState.SUPERSEDED).values_list('fingerprint', 'source_section'))
    if expected != stored:
        sync_deal_suggestions(deal, analysis)


def accepted_task_defaults(suggestion: TaskSuggestion) -> dict:
    defaults = {
        "deal": suggestion.deal,
        "title": concise_task_title(suggestion),
        "description": suggestion.title,
        "origin": Task.Origin.ANALYSIS,
        "fingerprint": suggestion.fingerprint,
    }
    if suggestion.source_priority.strip():
        defaults['priority'] = _priority(suggestion.source_priority)
    return defaults
