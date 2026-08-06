import hashlib
import json
import uuid
from datetime import timedelta
from pathlib import Path

from django.core import signing
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ai_orchestrator.models import AIAuditLog, DocumentChunk
from ai_orchestrator.services.embedding_processor import EmbeddingService
from deals.models import Deal
from deals.services.analysis_section_rewrite import AnalysisSectionRewriteService
from meetings.models import MeetingNote, MeetingNoteSource
from meetings.services.meeting_signal_analysis import MeetingSignalAnalysisService


VEKO_MEETINGS = (
    {
        "title": "Veko Care test meeting – FY22 revenue reconciliation",
        "summary": "Test management session reconciling the report's INR 21 Cr and INR 206.1 Cr FY22 revenue references.",
        "attendees": "Vikrant (Promoter), CFO, Finance Controller, India Alternatives deal team",
        "body": """TEST FIXTURE — NOT A HISTORICAL MEETING. Management explained that INR 206.1 Cr in the source pack is a unit-labeling error: the underlying audited schedule reports INR 206.1 million, equivalent to INR 20.61 Cr. The rounded INR 21 Cr figure therefore represents consolidated FY22 operating revenue. Management stated FY21 operating revenue was INR 14.0 Cr, implying 47.2% FY22 growth. FY22 EBITDA was clarified as INR 31.5 million, or INR 3.15 Cr, equal to a 15.3% EBITDA margin. PAT remained a loss of INR 0.9 Cr. The team requested the signed audited schedule and general-ledger mapping before treating the reconciliation as verified.""",
        "decisions": "For this test scenario, present FY22 revenue as INR 20.61 Cr and EBITDA as INR 3.15 Cr, both marked [VERIFY] pending audited schedules.",
        "action_items": "Obtain signed FY22 financial statements, revenue ledger, and unit reconciliation from the statutory auditor.",
    },
    {
        "title": "Veko Care test meeting – export and customer diligence",
        "summary": "Test commercial session reconciling export revenue and adding customer concentration evidence.",
        "attendees": "Head of International Business, Domestic Sales Head, CFO, India Alternatives deal team",
        "body": """TEST FIXTURE — NOT A HISTORICAL MEETING. The commercial team reconciled FY22 export sales at INR 15.95 Cr and domestic sales at INR 4.52 Cr; the remaining INR 0.14 Cr related to other operating income and rounding. The INR 159.5 Cr export reference in the report was described as the same INR-million-to-crore unit error. The largest customer contributed 11.8% of FY22 revenue and the top five contributed 38.6%. No distributor exceeded 8% of receivables at year end. Management reported commercial sales in 42 countries, with registration activity in 20 additional countries, but agreed that country-level sales and regulatory registrations require document verification.""",
        "decisions": "Use INR 15.95 Cr export revenue and INR 4.52 Cr domestic revenue only as management-confirmed test evidence marked [VERIFY].",
        "action_items": "Collect country-level revenue, top-customer invoices, distributor ageing, and registration certificates.",
    },
    {
        "title": "Veko Care test meeting – working capital and regulatory diligence",
        "summary": "Test finance and quality session covering working capital, liquidity, registrations, and capex.",
        "attendees": "CFO, Quality Head, Plant Head, India Alternatives deal team",
        "body": """TEST FIXTURE — NOT A HISTORICAL MEETING. Finance confirmed FY22 net working capital of INR 6.16 Cr, inventory of INR 4.41 Cr, operating cash flow of INR 2.21 Cr, and closing cash of INR 0.16 Cr after converting the source schedules from INR million to INR crore. Management acknowledged that liquidity remained tight. The Quality Head stated that 42 countries had active commercial registrations, while 20 applications remained in process. A proposed export-compliance expansion would require approximately USD 1 million and more than 24 months before facility readiness, with commercialization potentially taking up to five years. No commitment was made to incur that capex before completing market-by-market return analysis.""",
        "decisions": "Retain tight liquidity and long regulatory lead times as explicit risks in the rewritten financial section.",
        "action_items": "Verify working-capital schedules, bank statements, regulatory certificates, capex quotations, and market-level return assumptions.",
    },
)


class Command(BaseCommand):
    help = "Run a confirmation-gated meeting pipeline against an existing analyzed deal."

    def add_arguments(self, parser):
        parser.add_argument("--deal-id", required=True)
        parser.add_argument("--report-json", default="/tmp/existing-deal-meeting-pipeline.json")

    def handle(self, *args, **options):
        deal = Deal.objects.filter(id=options["deal_id"]).first()
        if not deal:
            raise CommandError("Deal not found.")
        analysis = deal.latest_analysis
        if not analysis:
            raise CommandError("The selected deal has no stored analysis.")
        report = (analysis.analysis_json or {}).get("analyst_report") or ""
        if not report.strip():
            raise CommandError("The selected analysis has no analyst_report.")
        section_title = "Key Financials"
        original_section = AnalysisSectionRewriteService.locate_section(report, section_title)[0]
        run_id = uuid.uuid4().hex[:10]

        notes = list(
            deal.meeting_notes.filter(
                metadata__synthetic_test_fixture=True,
                metadata__target_deal_id=str(deal.id),
            ).order_by("created_at")[:3]
        )
        indexing = []
        if len(notes) != 3:
            notes = []
            for index, fixture in enumerate(VEKO_MEETINGS):
                note = MeetingNote.objects.create(
                    **fixture,
                    source=MeetingNoteSource.MANUAL,
                    meeting_at=timezone.now() - timedelta(days=12 - index * 4),
                    metadata={
                        "test_run_id": run_id,
                        "synthetic_test_fixture": True,
                        "target_deal_id": str(deal.id),
                        "sequence": index + 1,
                    },
                )
                note.deals.add(deal)
                notes.append(note)
        for note in notes:
            embedded = EmbeddingService().vectorize_meeting_note(note)
            note.refresh_from_db()
            db_chunks = DocumentChunk.objects.filter(
                deal=deal,
                source_type="meeting_note",
                source_id=str(note.id),
                embedding__isnull=False,
            ).count()
            indexing.append({
                "meeting_note_id": str(note.id),
                "indexed": note.is_indexed,
                "vectorize_returned": embedded,
                "chunk_count": note.chunk_count,
                "embedded_db_chunks": db_chunks,
                "embedding_error": note.embedding_error,
            })

        if not all(item["indexed"] and item["embedded_db_chunks"] for item in indexing):
            raise CommandError("One or more test meetings were not embedded.")

        signals = MeetingSignalAnalysisService().analyze_deal(deal, notes)
        signal_audit = AIAuditLog.objects.filter(id=signals.get("audit_log_id"), is_success=True).first()
        if not signal_audit:
            raise CommandError("Meeting signal analysis did not complete successfully.")

        instruction = """Use the relevant indexed Veko Care test meetings to rewrite Key Financials. Lead with one compact reconciliation table containing every required figure: FY22 operating revenue INR 20.61 Cr, FY21 INR 14.0 Cr, FY22 growth 47.2%, FY22 EBITDA INR 3.15 Cr and 15.3% margin, PAT loss INR 0.9 Cr, export revenue INR 15.95 Cr, domestic revenue INR 4.52 Cr, net working capital INR 6.16 Cr, operating cash flow INR 2.21 Cr, and closing cash INR 0.16 Cr. State that every new figure is synthetic management-confirmed test evidence and remains [VERIFY] pending audited documents. Preserve tight-liquidity and regulatory-capex caveats. Do not repeat legacy segment or geography tables, do not alter another section, and keep the section under 900 words."""
        rewrite_service = AnalysisSectionRewriteService()
        rewritten = rewrite_service.rewrite(
            deal=deal,
            section_title=section_title,
            section_markdown=original_section,
            instruction=instruction,
            full_report=report,
            version=analysis.version,
        )
        required = ["20.61", "14.0", "47.2", "3.15", "15.3%", "0.9", "15.95", "4.52", "6.16", "2.21", "0.16", "VERIFY"]
        missing = [value for value in required if value not in rewritten]
        analysis.refresh_from_db()
        unchanged = (analysis.analysis_json or {}).get("analyst_report") == report
        token = signing.dumps(
            {
                "deal_id": str(deal.id),
                "version": str(analysis.version),
                "section_title": section_title,
                "report_sha256": hashlib.sha256(report.encode("utf-8")).hexdigest(),
                "section_markdown": rewritten,
            },
            salt="analysis-section-rewrite",
            compress=True,
        )
        result = {
            "status": "awaiting_confirmation" if not missing and unchanged else "failed",
            "run_id": run_id,
            "synthetic_test_fixture": True,
            "deal_id": str(deal.id),
            "deal_title": deal.title,
            "analysis_id": str(analysis.id),
            "analysis_version": analysis.version,
            "meeting_note_ids": [str(note.id) for note in notes],
            "indexing": indexing,
            "meeting_signal_analysis": signals,
            "rewrite_preview": rewritten,
            "missing_required_facts": missing,
            "stored_report_unchanged_before_confirmation": unchanged,
            "confirmation_token": token,
        }
        output = Path(options["report_json"])
        output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        self.stdout.write(json.dumps(result, indent=2, ensure_ascii=False))
        if result["status"] == "failed":
            raise CommandError(f"Pipeline failed; see {output}")
        self.stdout.write(self.style.WARNING("Real deal report unchanged. Explicit confirmation is required."))
