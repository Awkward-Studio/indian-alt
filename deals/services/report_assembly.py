"""Keep structured synthesis payloads out of prose and consolidate legacy report updates."""
from __future__ import annotations

import json
import hashlib
import re

from ai_orchestrator.prompt_contracts import IC_SECTION_TITLES

SEPARATOR = re.compile(r'^\s*---\s*Supplemental Update\s*---\s*$', re.MULTILINE | re.IGNORECASE)
ENVELOPE_KEYS = {'deal_model_data', 'source_relationships', 'metadata', 'canonical_snapshot', 'analyst_report'}


def _heading_key(title: str) -> str:
    title = re.sub(r'^\d+[.)]\s*', '', title).replace('**', '').replace('&', 'and')
    return re.sub(r'[^a-z0-9]+', ' ', title.lower()).strip()


CANONICAL_SECTION_KEYS = {_heading_key(title) for title in IC_SECTION_TITLES}
SECTION_KEYS = set(CANONICAL_SECTION_KEYS)
SECTION_KEYS.update({
    'company overview', 'promoters and their background',
    'strategic fit and market opportunity', 'key financial highlights',
    'financial deep dive', 'financial deep dive include revenue ebitda margins',
    'key peers and valuation multiples', 'risk matrix top 5 risks',
    'red flags and warning signs', 'key observations risks and open points',
    'valuation and exit range', 'next steps data requests',
    'operational due diligence', 'key observations',
})


def _report_prose(value, depth=0) -> str:
    if not isinstance(value, str) or depth > 4:
        return ''
    text = value.strip()
    candidate = re.sub(r'^```(?:json)?\s*\n?', '', text, flags=re.IGNORECASE)
    candidate = re.sub(r'\n?```\s*$', '', candidate)
    try:
        payload = json.loads(candidate)
    except (ValueError, TypeError):
        return text
    if isinstance(payload, dict) and ENVELOPE_KEYS.intersection(payload):
        return _report_prose(payload.get('analyst_report'), depth + 1)
    return text


def clean_report_text(value) -> str:
    if not isinstance(value, str):
        return ''
    sections = {}
    notes = []
    preamble = ''
    for raw_block in SEPARATOR.split(value):
        block = _report_prose(raw_block)
        if not block:
            continue
        headings = [match for match in re.finditer(r'^(#{1,3})\s+(.+?)\s*$', block, re.MULTILINE)
                    if _heading_key(match.group(2)) in SECTION_KEYS or _heading_key(match.group(2)) == 'supplemental updates']
        primary = [match for match in headings if match.group(1) == '##']
        if not primary:
            primary = headings
        if CANONICAL_SECTION_KEYS.issubset({_heading_key(match.group(2)) for match in primary}):
            sections.clear()
            notes.clear()
            preamble = ''
        if not primary:
            if block not in notes:
                notes.append(block)
            continue
        prefix = block[:primary[0].start()].strip()
        if prefix and not preamble:
            preamble = prefix
        for index, match in enumerate(primary):
            end = primary[index + 1].start() if index + 1 < len(primary) else len(block)
            if _heading_key(match.group(2)) == 'supplemental updates':
                note = block[match.end():end].strip()
                if note and note not in notes:
                    notes.append(note)
            else:
                section = block[match.start():end].strip()
                supplemental = re.search(r'^###\s+Supplemental Updates\s*$', section, re.MULTILINE | re.IGNORECASE)
                if supplemental:
                    note = section[supplemental.end():].strip()
                    if note and note not in notes:
                        notes.append(note)
                    section = section[:supplemental.start()].strip()
                sections[_heading_key(match.group(2))] = section
    if not sections:
        return '\n\n'.join(notes)
    if notes:
        rationale = sections.get('investment rationale', '## Investment Rationale')
        sections['investment rationale'] = rationale + '\n\n### Supplemental Updates\n\n' + '\n\n'.join(notes)
    parts = [preamble, *sections.values()]
    return '\n\n'.join(part for part in parts if part)


def merge_report_text(previous, current, *, initial=False) -> str:
    previous = clean_report_text(previous)
    current = clean_report_text(current)
    if not current:
        return previous
    if initial or not previous:
        return current
    if current == previous:
        return previous
    return clean_report_text(f'{previous}\n\n--- Supplemental Update ---\n\n{current}')


def report_text_fingerprints(report) -> set[str]:
    """Support previews made against either stored legacy text or cleaned prose."""
    raw = str(report or '').strip()
    return {hashlib.sha256(text.encode('utf-8')).hexdigest() for text in (raw, clean_report_text(raw))}
