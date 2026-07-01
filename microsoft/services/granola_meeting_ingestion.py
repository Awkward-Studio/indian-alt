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


class GranolaMeetingEmailIngestionService:
    """
    Converts meeting-note emails into deal-linked MeetingNote records.

    The routing rule is intentionally strict: an email is processed only when
    the subject is exactly a deal title, or a deal_name= value in the
    subject/body exactly matches an existing deal title, and the body includes
    a summary plus transcript/notes section.
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

    @classmethod
    def process_email(cls, email) -> Optional[MeetingNote]:
        existing_note = MeetingNote.objects.filter(source_email=email).first()
        if existing_note:
            return existing_note

        payload = cls.extract_payload(email)
        if payload is None:
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
                    },
                },
            )
            note.deals.set([payload.deal])

            from ai_orchestrator.services.embedding_processor import EmbeddingService

            try:
                EmbeddingService().vectorize_meeting_note(note)
            except Exception as exc:
                logger.exception("Failed to vectorize Granola meeting note %s: %s", note.id, exc)
                note.embedding_error = str(exc)
                note.save(update_fields=["embedding_error", "updated_at"])

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

            if not EmbeddingService().vectorize_meeting_note(note):
                note.refresh_from_db(fields=["embedding_error"])
                raise ValueError(note.embedding_error or "Meeting note embeddings were not created.")

        return note

    @classmethod
    def extract_payload(cls, email) -> Optional[GranolaMeetingPayload]:
        subject = email.subject or ""
        body = cls._email_text(email)
        deal, deal_name_source = cls._resolve_deal(subject, body)
        if deal is None:
            return None

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
            deal_name_source=deal_name_source,
        )

    @classmethod
    def _is_granola_email(cls, email) -> bool:
        sender = (email.from_email or "").lower()
        metadata = email.graph_metadata if isinstance(email.graph_metadata, dict) else {}
        sender_name = str(metadata.get("sender") or metadata.get("from") or "").lower()
        return cls.GRANOLA_MARKER in sender or cls.GRANOLA_MARKER in sender_name

    @classmethod
    def _resolve_deal(cls, subject: str, body: str) -> tuple[Optional[Deal], str]:
        deal_name = cls._extract_deal_name(subject) or cls._extract_deal_name(body)
        if deal_name:
            deal = cls._deal_by_exact_title(deal_name)
            return deal, "deal_name" if deal else ""

        subject_name = cls._normalize_subject(subject)
        if not subject_name:
            return None, ""
        deal = cls._deal_by_exact_title(subject_name)
        return deal, "subject" if deal else ""

    @classmethod
    def _extract_deal_name(cls, text: str) -> str:
        match = cls.DEAL_NAME_RE.search(text or "")
        if not match:
            return ""
        return cls._clean_value(match.group("name"))

    @staticmethod
    def _deal_by_exact_title(name: str) -> Optional[Deal]:
        normalized = GranolaMeetingEmailIngestionService._normalize_match_text(name)
        for deal in Deal.objects.exclude(title__isnull=True).exclude(title=""):
            if GranolaMeetingEmailIngestionService._normalize_match_text(deal.title) == normalized:
                return deal
        return None

    @staticmethod
    def _normalize_match_text(value: str) -> str:
        value = re.sub(r"\s+", " ", str(value or "")).strip()
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
