import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from deals.models import Deal
from meetings.models import MeetingNote, MeetingNoteSource

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GranolaMeetingPayload:
    deal: Deal
    title: str
    summary: str
    transcript: str
    meeting_at: datetime
    deal_name_source: str
    resolution_evidence: dict


@dataclass(frozen=True)
class DealMatchResolution:
    deal: Optional[Deal]
    status: str
    source: str
    query: str
    candidates: tuple[dict, ...] = ()

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "source": self.source,
            "query": self.query,
            "deal_id": str(self.deal.id) if self.deal else None,
            "candidates": list(self.candidates),
        }


class GranolaMeetingEmailIngestionService:
    """
    Converts meeting-note emails into deal-linked MeetingNote records.

    The routing rule uses exact deal-title matching first, then a high-confidence
    fuzzy title match, and the body must include a summary plus transcript/notes
    section.
    """

    DEAL_NAME_RE = re.compile(
        r"deal_name\s*=\s*(?P<quote>['\"]?)(?P<name>[^'\"\r\n;|]+)(?P=quote)",
        re.IGNORECASE,
    )
    SECTION_HEADER_RE = re.compile(
        r"^\s*#{0,6}\s*(summary|transcript|notes|date|meeting date|attendees|action items|decisions)\s*:?\s*$",
        re.IGNORECASE,
    )
    DATE_LINE_RE = re.compile(
        r"^\s*(?:meeting\s+date|date|when)\s*:\s*(?P<value>.+?)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )

    GRANOLA_MARKER = "granola"
    FUZZY_DEAL_MATCH_THRESHOLD = 0.88
    FUZZY_DEAL_MATCH_MIN_MARGIN = 0.08
    DEAL_TITLE_STOPWORDS = {
        "advisors",
        "deal",
        "growth",
        "india",
        "intequant",
        "investment",
        "limited",
        "ltd",
        "notes",
        "opportunity",
        "private",
        "project",
        "projects",
        "pvt",
        "round",
        "series",
        "store",
        "test",
    }

    @classmethod
    def process_email(cls, email) -> Optional[MeetingNote]:
        existing_note = MeetingNote.objects.filter(source_email=email).first()
        if existing_note:
            return existing_note

        payload = cls.extract_payload(email)
        if payload is None:
            if cls.is_meeting_note_email(email):
                resolution = cls.resolve_deal(email.subject or "", cls._email_text(email))
                if resolution.status in {"ambiguous", "unmatched"}:
                    metadata = dict(email.graph_metadata or {})
                    metadata["meeting_deal_resolution"] = resolution.as_dict()
                    email.graph_metadata = metadata
                    email.processing_error = (
                        "Meeting note was not auto-linked because deal resolution was "
                        f"{resolution.status}. Review candidates and attach it manually."
                    )
                    email.save(update_fields=["graph_metadata", "processing_error", "updated_at"])
            return None

        with transaction.atomic():
            note, _ = MeetingNote.objects.update_or_create(
                source_email=email,
                defaults={
                    "title": payload.title,
                    "body": payload.transcript,
                    "summary": payload.summary,
                    "meeting_at": payload.meeting_at,
                    "source": MeetingNoteSource.EMAIL,
                    "metadata": {
                        "source": "granola",
                        "email_graph_id": email.graph_id,
                        "email_subject": email.subject,
                        "email_from": email.from_email,
                        "deal_name_source": payload.deal_name_source,
                        "deal_resolution": payload.resolution_evidence,
                    },
                },
            )
            note.deals.set([payload.deal])

            from ai_orchestrator.services.embedding_processor import EmbeddingService

            embedding_service = EmbeddingService()
            if embedding_service.is_embedding_available(timeout=1):
                try:
                    embedding_service.vectorize_meeting_note(note)
                except Exception as exc:
                    logger.exception("Failed to vectorize Granola meeting note %s: %s", note.id, exc)
                    note.embedding_error = str(exc)
                    note.save(update_fields=["embedding_error", "updated_at"])
            else:
                note.is_indexed = False
                note.chunk_count = 0
                note.embedding_error = (
                    "Meeting note saved and linked, but embeddings were skipped because "
                    f"the embedding service is unavailable. {embedding_service._last_embedding_error}"
                ).strip()
                note.save(update_fields=["is_indexed", "chunk_count", "embedding_error", "updated_at"])

            update_fields = []
            if email.deal_id != payload.deal.id:
                email.deal = payload.deal
                update_fields.append("deal")
            if not email.is_processed:
                email.is_processed = True
                update_fields.append("is_processed")
            if email.processing_status != "completed":
                email.processing_status = "completed"
                update_fields.append("processing_status")
            if email.extracted_text != payload.transcript:
                email.extracted_text = payload.transcript
                update_fields.append("extracted_text")
            if email.processed_at is None:
                email.processed_at = timezone.now()
                update_fields.append("processed_at")
            if update_fields:
                email.save(update_fields=update_fields)

        return note

    @classmethod
    def process_email_for_deal(cls, email, deal: Deal) -> MeetingNote:
        """
        Manually convert an email into a deal-linked meeting note.

        This bypasses automatic deal-name routing, but it does not bypass the
        meeting-note embedding requirement. If embeddings are not created, the
        note/email link is not committed.
        """
        body = cls._email_text(email)
        summary = cls._extract_section(body, "summary") or (email.body_preview or "")
        transcript = cls._extract_section(body, "transcript") or cls._extract_section(body, "notes") or body
        transcript = transcript.strip()
        if not transcript and not summary.strip():
            raise ValueError("Email has no meeting-note text to attach.")

        meeting_at = cls._extract_meeting_datetime(body) or email.date_sent or email.date_received or timezone.now()
        if timezone.is_naive(meeting_at):
            meeting_at = timezone.make_aware(meeting_at, timezone.get_current_timezone())

        title = (email.subject or "").strip() or f"Meeting note - {deal.title}"

        with transaction.atomic():
            note, _ = MeetingNote.objects.update_or_create(
                source_email=email,
                defaults={
                    "title": title[:255],
                    "body": transcript,
                    "summary": summary.strip(),
                    "meeting_at": meeting_at,
                    "source": MeetingNoteSource.EMAIL,
                    "metadata": {
                        "source": "manual_email_attach",
                        "email_graph_id": email.graph_id,
                        "email_subject": email.subject,
                        "email_from": email.from_email,
                        "deal_name_source": "manual",
                    },
                },
            )
            note.deals.set([deal])

            from ai_orchestrator.services.embedding_processor import EmbeddingService

            embedding_service = EmbeddingService()
            if embedding_service.is_embedding_available(timeout=1):
                if not embedding_service.vectorize_meeting_note(note):
                    note.refresh_from_db(fields=["embedding_error"])
                    raise ValueError(note.embedding_error or "Meeting note embeddings were not created.")
            else:
                note.is_indexed = False
                note.chunk_count = 0
                note.embedding_error = (
                    "Meeting note saved and linked, but embeddings were skipped because "
                    f"the embedding service is unavailable. {embedding_service._last_embedding_error}"
                ).strip()
                note.save(update_fields=["is_indexed", "chunk_count", "embedding_error", "updated_at"])

        return note

    @classmethod
    def extract_payload(cls, email) -> Optional[GranolaMeetingPayload]:
        subject = email.subject or ""
        body = cls._email_text(email)
        resolution = cls.resolve_deal(subject, body)
        if resolution.deal is None:
            return None
        deal = resolution.deal

        summary = cls._extract_section(body, "summary")
        transcript = cls._extract_section(body, "transcript") or cls._extract_section(body, "notes")
        if not summary or not transcript:
            logger.info(
                "Skipping Granola email %s because summary/transcript could not be extracted.",
                email.id,
            )
            return None

        meeting_at = cls._extract_meeting_datetime(body) or email.date_sent or email.date_received or timezone.now()
        if timezone.is_naive(meeting_at):
            meeting_at = timezone.make_aware(meeting_at, timezone.get_current_timezone())

        return GranolaMeetingPayload(
            deal=deal,
            title=subject.strip() or f"Granola meeting - {deal.title}",
            summary=summary,
            transcript=transcript,
            meeting_at=meeting_at,
            deal_name_source=resolution.source,
            resolution_evidence=resolution.as_dict(),
        )

    @classmethod
    def is_meeting_note_email(cls, email) -> bool:
        """
        Return whether an email has the shape of a meeting-note email.

        This intentionally does not require a resolvable deal. The emails UI
        uses this to decide whether manual deal attachment should be offered.
        """
        body = cls._email_text(email)
        has_notes_body = bool(
            cls._extract_section(body, "summary")
            and (cls._extract_section(body, "transcript") or cls._extract_section(body, "notes"))
        )
        if has_notes_body:
            return True

        subject = (email.subject or "").casefold()
        return cls.GRANOLA_MARKER in subject and bool(cls._extract_section(body, "notes") or cls._extract_section(body, "transcript"))

    @classmethod
    def _is_granola_email(cls, email) -> bool:
        sender = (email.from_email or "").lower()
        metadata = email.graph_metadata if isinstance(email.graph_metadata, dict) else {}
        sender_name = str(metadata.get("sender") or metadata.get("from") or "").lower()
        return cls.GRANOLA_MARKER in sender or cls.GRANOLA_MARKER in sender_name

    @classmethod
    def _resolve_deal(cls, subject: str, body: str) -> tuple[Optional[Deal], str]:
        resolution = cls.resolve_deal(subject, body)
        return resolution.deal, resolution.source if resolution.deal else ""

    @classmethod
    def resolve_deal(cls, subject: str, body: str) -> DealMatchResolution:
        deal_name = cls._extract_deal_name(subject) or cls._extract_deal_name(body)
        source_prefix = "deal_name" if deal_name else "subject"
        query = deal_name or cls._normalize_subject(subject)
        if not query:
            return DealMatchResolution(None, "unmatched", source_prefix, "")

        exact_matches = cls._deals_by_exact_title(query)
        if len(exact_matches) == 1:
            return DealMatchResolution(
                exact_matches[0],
                "matched",
                source_prefix,
                query,
                ({"deal_id": str(exact_matches[0].id), "title": exact_matches[0].title, "score": 1.0},),
            )
        if len(exact_matches) > 1:
            candidates = tuple(
                {"deal_id": str(deal.id), "title": deal.title, "score": 1.0}
                for deal in exact_matches
            )
            return DealMatchResolution(None, "ambiguous", source_prefix, query, candidates)

        candidates = cls._fuzzy_deal_candidates(query)
        if not candidates or candidates[0][1] < cls.FUZZY_DEAL_MATCH_THRESHOLD:
            evidence = tuple(
                {"deal_id": str(deal.id), "title": deal.title, "score": round(score, 4)}
                for deal, score in candidates[:3]
            )
            return DealMatchResolution(None, "unmatched", source_prefix, query, evidence)

        best_deal, best_score = candidates[0]
        runner_score = candidates[1][1] if len(candidates) > 1 else 0.0
        evidence = tuple(
            {"deal_id": str(deal.id), "title": deal.title, "score": round(score, 4)}
            for deal, score in candidates[:3]
        )
        if runner_score >= cls.FUZZY_DEAL_MATCH_THRESHOLD and best_score - runner_score < cls.FUZZY_DEAL_MATCH_MIN_MARGIN:
            return DealMatchResolution(None, "ambiguous", source_prefix, query, evidence)

        return DealMatchResolution(
            best_deal,
            "matched",
            f"{source_prefix}_fuzzy:{best_score:.2f}",
            query,
            evidence,
        )

    @classmethod
    def _extract_deal_name(cls, text: str) -> str:
        match = cls.DEAL_NAME_RE.search(text or "")
        if not match:
            return ""
        return cls._clean_value(match.group("name"))

    @staticmethod
    def _deal_by_exact_title(name: str) -> Optional[Deal]:
        matches = GranolaMeetingEmailIngestionService._deals_by_exact_title(name)
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _deals_by_exact_title(name: str) -> list[Deal]:
        normalized = GranolaMeetingEmailIngestionService._normalize_match_text(name)
        matches = []
        for deal in Deal.objects.exclude(title__isnull=True).exclude(title="").order_by("title", "id"):
            if GranolaMeetingEmailIngestionService._normalize_match_text(deal.title) == normalized:
                matches.append(deal)
        return matches

    @classmethod
    def _deal_by_fuzzy_title(cls, name: str) -> tuple[Optional[Deal], float]:
        candidates = cls._fuzzy_deal_candidates(name)
        if not candidates:
            return None, 0.0
        best_deal, best_score = candidates[0]
        runner_score = candidates[1][1] if len(candidates) > 1 else 0.0
        if (
            best_score >= cls.FUZZY_DEAL_MATCH_THRESHOLD
            and not (
                runner_score >= cls.FUZZY_DEAL_MATCH_THRESHOLD
                and best_score - runner_score < cls.FUZZY_DEAL_MATCH_MIN_MARGIN
            )
        ):
            return best_deal, best_score
        return None, best_score

    @classmethod
    def _fuzzy_deal_candidates(cls, name: str) -> list[tuple[Deal, float]]:
        candidates = []
        for deal in Deal.objects.exclude(title__isnull=True).exclude(title="").order_by("title", "id"):
            score = cls._title_match_score(name, deal.title)
            if score > 0:
                candidates.append((deal, score))
        return sorted(candidates, key=lambda item: (-item[1], str(item[0].id)))

    @classmethod
    def _title_match_score(cls, alias: str, title: str) -> float:
        alias_norm = cls._normalize_match_text(alias)
        title_norm = cls._normalize_match_text(title)
        if not alias_norm or not title_norm:
            return 0.0
        if alias_norm == title_norm:
            return 1.0

        alias_tokens = cls._match_tokens(alias)
        title_tokens = cls._match_tokens(title)
        if not alias_tokens or not title_tokens:
            return 0.0

        overlap = alias_tokens & title_tokens
        if not overlap:
            return 0.0

        title_containment = len(overlap) / len(title_tokens)
        if title_containment == 1.0:
            return 0.95

        precision = len(overlap) / len(alias_tokens)
        recall = len(overlap) / len(title_tokens)
        jaccard = len(overlap) / len(alias_tokens | title_tokens)
        return max((precision + recall) / 2, jaccard)

    @classmethod
    def _match_tokens(cls, value: str) -> set[str]:
        return {
            token
            for token in cls._normalize_match_text(value).split()
            if len(token) >= 3 and token not in cls.DEAL_TITLE_STOPWORDS
        }

    @staticmethod
    def _normalize_match_text(value: str) -> str:
        value = re.sub(r"[^a-zA-Z0-9]+", " ", str(value or ""))
        value = re.sub(r"\s+", " ", value).strip()
        return value.casefold()

    @classmethod
    def _normalize_subject(cls, subject: str) -> str:
        value = cls._clean_value(subject)
        while True:
            cleaned = re.sub(r"^\s*(re|fw|fwd)\s*:\s*", "", value, flags=re.IGNORECASE).strip()
            if cleaned == value:
                return cleaned
            value = cleaned

    @staticmethod
    def _clean_value(value: str) -> str:
        value = re.sub(r"<[^>]+>", " ", str(value or ""))
        value = re.sub(r"\s+", " ", value).strip()
        return value.strip("'\" ;|,")

    @staticmethod
    def _email_text(email) -> str:
        parts = [email.body_text or "", email.body_preview or ""]
        return "\n\n".join(part for part in parts if part).strip()

    @classmethod
    def _extract_section(cls, body: str, section_name: str) -> str:
        lines = (body or "").splitlines()
        section_aliases = {
            "summary": {"summary"},
            "transcript": {"transcript"},
            "notes": {"notes"},
        }
        wanted = section_aliases.get(section_name, {section_name})
        captured: list[str] = []
        in_section = False

        for line in lines:
            stripped = line.strip()
            header = cls.SECTION_HEADER_RE.match(stripped)
            if header:
                header_name = header.group(1).lower()
                if in_section and header_name not in wanted:
                    break
                in_section = header_name in wanted
                continue
            if in_section:
                captured.append(line)

        text = "\n".join(captured).strip()
        if text:
            return text

        inline = re.search(
            rf"\b{re.escape(section_name)}\s*:\s*(?P<value>.+?)(?=\n\s*(?:summary|transcript|notes|date|meeting date|attendees|action items|decisions)\s*:|\Z)",
            body or "",
            flags=re.IGNORECASE | re.DOTALL,
        )
        return inline.group("value").strip() if inline else ""

    @classmethod
    def _extract_meeting_datetime(cls, body: str) -> Optional[datetime]:
        match = cls.DATE_LINE_RE.search(body or "")
        if not match:
            return None
        raw_value = cls._clean_value(match.group("value"))
        parsed = parse_datetime(raw_value)
        if parsed:
            return parsed

        for date_format in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%B %d, %Y", "%d %B %Y"):
            try:
                return datetime.strptime(raw_value, date_format)
            except ValueError:
                continue
        return None
