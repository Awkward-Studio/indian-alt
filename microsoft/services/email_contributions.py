"""Loss-preserving email segmentation. A quote boundary is never a deletion rule."""
from dataclasses import dataclass, asdict
from email.utils import parseaddr
import hashlib
import re

from .email_html_sanitizer import EmailHtmlSanitizer


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def normalize(text):
    return re.sub(r'[ \t]+', ' ', (text or '').replace('\r\n', '\n').replace('\r', '\n')).strip()


@dataclass
class Contribution:
    text: str
    fingerprint: str
    headers: dict
    start: int
    end: int
    diagnostics: list

    def as_dict(self):
        return asdict(self)


class EmailContributionParser:
    # Header blocks must contain a date. Ordinary prose beginning with "From:"
    # alone is insufficient to infer a new message identity.
    HEADER = re.compile(
        r'(?im)^\s*From:\s*[^\n]+\n(?:\s*(?:Sent|Date|To|Cc|Subject|Message-ID):[^\n]*\n?){1,8}'
    )
    REPLY = re.compile(r'(?im)^\s*On [^\n]{3,300} wrote:\s*$')

    @staticmethod
    def source_text(source):
        if source.get('body_html'):
            return normalize(EmailHtmlSanitizer.text_with_link_targets(source['body_html']))
        return normalize(source.get('body_text') or source.get('body_preview') or '')

    @classmethod
    def parse(cls, source, *, email_id, known=()):
        text = cls.source_text(source)
        boundaries = {0, len(text)}
        header_at = {}
        for match in cls.HEADER.finditer(text):
            headers = {k.lower(): v.strip() for k, v in re.findall(r'(?im)^\s*([\w-]+):\s*([^\n]*)', match.group())}
            if not (headers.get('sent') or headers.get('date')):
                continue
            boundaries.update([match.start(), match.end()])
            header_at[match.start()] = headers
        for match in cls.REPLY.finditer(text):
            boundaries.add(match.start())
        # Only committed, scoped contributions are supplied by the caller.
        # Split every exact known span, never discard a suffix after one match.
        known_at = {}
        for item in known:
            prior = normalize(item.text)
            if len(prior) < 80:
                continue
            start = 0
            while prior and (idx := text.find(prior, start)) >= 0:
                end = idx + len(prior)
                # Whole line boundaries avoid matching a value inside changed prose.
                if (idx == 0 or text[idx - 1] == '\n') and (end == len(text) or text[end] == '\n'):
                    boundaries.update([idx, end])
                    known_at[(idx, end)] = item
                start = end
        points = sorted(boundaries)
        headers = {'from': source.get('from_email') or '', 'date': source.get('date_sent') or source.get('date_received') or '', 'subject': source.get('subject') or ''}
        result = []
        for start, end in zip(points, points[1:]):
            if start in header_at:
                headers = header_at[start]
            part = text[start:end].strip()
            if not part:
                continue
            prior = known_at.get((start, end))
            identity = headers.get('message-id') or ''
            sender = parseaddr(headers.get('from', ''))[1].lower()
            date = headers.get('date') or headers.get('sent') or ''
            # Unknown identity is deliberately scoped to the source message.
            identity = identity or (sender + '|' + date if sender and date else str(email_id))
            fingerprint = prior.fingerprint if prior else digest(identity + '\n' + normalize(part))
            result.append(Contribution(part, fingerprint, dict(headers), start, end,
                ['verified_prior_contribution'] if prior else ['retained_source']))
        return result
