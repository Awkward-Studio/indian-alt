"""Create and inspect a real synthetic deal through the live email/T4 pipeline."""

import json
import uuid
from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ai_orchestrator.models import AIAuditLog
from ai_orchestrator.services.ai_processor import AIProcessorService
from deals.models import Deal
from deals.services.deal_creation import DealCreationService
from deals.tasks import finalize_thread_analysis_async
from microsoft.models import Email, EmailAccount
from microsoft.services.email_thread_unfolder import EmailThreadUnfolder


class Command(BaseCommand):
    help = "Run five substantive emails through live T4 synthesis, create the deal, and inspect persistence."

    def add_arguments(self, parser):
        parser.add_argument("--report-json", default="/tmp/long-email-deal-pipeline.json")

    def handle(self, *args, **options):
        run_key = uuid.uuid4().hex[:10]
        conversation_id = f"synthetic-long-deal-{run_key}"
        account, _ = EmailAccount.objects.get_or_create(email="pipeline-test@india-alt.test")
        messages = self._create_messages(account, conversation_id, run_key)
        deltas = EmailThreadUnfolder.unfold(messages)
        if len([item for item in deltas if item.text]) != 5:
            raise CommandError("Expected five non-empty unfolded messages")

        audit = AIAuditLog.objects.create(
            source_type="email_pipeline_live_deal_test",
            source_id=str(messages[0].id),
            context_label=f"Synthetic Project Banyan {run_key}",
            model_used="configured-runtime-model",
            system_prompt="Live end-to-end email deal test",
            user_prompt="Synthesize the five-message Project Banyan investment thread",
            raw_response="",
            status="PROCESSING",
            is_success=False,
            source_metadata={
                "email_id": str(messages[0].id),
                "proposed_intel": {"company_name": f"Project Banyan E2E {run_key}"},
                "thread_stats": {"message_count": 5, "body_deltas": [item.as_dict() for item in deltas]},
            },
        )
        service = AIProcessorService()
        results = []
        for delta, email in zip(deltas, messages, strict=True):
            normalized = service.process_content(
                content=delta.text,
                skill_name="document_normalization",
                source_type="email_pipeline_live_deal_test",
                metadata={
                    "chat_template_kwargs": {"enable_thinking": False},
                    "max_tokens": 2048,
                    "request_timeout": 180,
                },
            )
            results.append({
                "status": "passed",
                "file_id": f"body_{email.id}",
                "file_name": f"Email {delta.position + 1}: {email.subject}",
                "normalized_text": delta.text,
                "normalized_json": normalized if isinstance(normalized, dict) else {},
            })

        synthesis = finalize_thread_analysis_async.run(results, None, str(audit.id))
        audit.refresh_from_db()
        if synthesis.get("error") or not isinstance(audit.parsed_json, dict):
            raise CommandError(f"Thread synthesis failed: {synthesis}")

        title = (
            audit.parsed_json.get("deal_model_data", {}).get("title")
            or f"Project Banyan E2E {run_key}"
        )
        deal = Deal.objects.create(title=title)
        DealCreationService.process_deal_creation(
            deal,
            {
                "source_email_id": str(messages[-1].id),
                "analysis_json": audit.parsed_json,
            },
        )
        deal.refresh_from_db()
        analysis = deal.latest_analysis
        thread = Email.objects.filter(conversation_id=conversation_id)
        attachment_ids = sorted(deal.documents.values_list("onedrive_id", flat=True))
        report = {
            "status": "passed",
            "run_key": run_key,
            "deal": {
                "id": str(deal.id),
                "title": deal.title,
                "primary_contact_id": str(deal.primary_contact_id) if deal.primary_contact_id else None,
                "primary_contact_name": deal.primary_contact.name if deal.primary_contact else None,
                "primary_contact_email": deal.primary_contact.email if deal.primary_contact else None,
                "industry": deal.industry,
                "funding_ask": deal.funding_ask,
                "deal_summary": deal.deal_summary,
            },
            "thread": {
                "conversation_id": conversation_id,
                "message_count": thread.count(),
                "linked_message_count": thread.filter(deal=deal).count(),
                "delta_strategies": [item.strategy for item in deltas],
                "retained_business_facts": self._retained_facts(deltas),
            },
            "attachments": {
                "document_count": deal.documents.count(),
                "ids": attachment_ids,
                "titles": list(deal.documents.order_by("created_at").values_list("title", flat=True)),
            },
            "analysis": {
                "id": str(analysis.id) if analysis else None,
                "version": analysis.version if analysis else None,
                "kind": analysis.analysis_kind if analysis else None,
                "analyst_report": (analysis.analysis_json or {}).get("analyst_report") if analysis else None,
                "ambiguities": analysis.ambiguities if analysis else [],
                "analysis_json": analysis.analysis_json if analysis else {},
            },
            "audit_log_id": str(audit.id),
        }
        failures = []
        if report["thread"]["linked_message_count"] != 5:
            failures.append("not all five emails linked to the created deal")
        if report["attachments"]["document_count"] != 5:
            failures.append("not all five attachments persisted")
        if report["deal"]["primary_contact_email"] != "maya.rao@avendus.example":
            failures.append("oldest external sender was not selected as primary banker")
        if not analysis or analysis.analysis_kind != "initial":
            failures.append("initial analysis was not persisted")
        required_facts = ["125", "80", "22", "Pune", "October 2026"]
        serialized_analysis = json.dumps(report["analysis"], default=str).casefold()
        missing = [fact for fact in required_facts if fact.casefold() not in serialized_analysis]
        if missing:
            failures.append(f"analysis omitted required facts: {missing}")
        if failures:
            report["status"] = "failed"
            report["failures"] = failures

        report_path = Path(options["report_json"])
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
        self.stdout.write(json.dumps({
            "status": report["status"],
            "deal": report["deal"],
            "thread": report["thread"],
            "attachments": report["attachments"],
            "analysis": {key: report["analysis"][key] for key in ("id", "version", "kind", "analyst_report", "ambiguities")},
            "report": str(report_path),
        }, indent=2, default=str))
        if failures:
            raise CommandError("; ".join(failures))

    @staticmethod
    def _create_messages(account, conversation_id, run_key):
        now = timezone.now()
        contributions = [
            "Project Banyan is raising INR 125 crore for a Pune recycling plant. FY26 revenue was INR 80 crore. Maya Rao at Avendus is introducing the opportunity.",
            "The India Alternatives team requests the top-ten customer concentration, plant commissioning date, and a bridge from revenue to EBITDA.",
            "Customer concentration is 22 percent, EBITDA margin is 14 percent, and management expects the Pune plant to commission in October 2026.",
            "The investment team asks for the capex schedule and confirmation that environmental approvals cover the expanded Pune capacity.",
            "Forwarding the externally continued discussion: approvals are valid, and Maya will send the INR 46 crore capex schedule by Friday.",
        ]
        senders = [
            "maya.rao@avendus.example",
            account.email,
            "maya.rao@avendus.example",
            account.email,
            "maya.rao@avendus.example",
        ]
        messages = []
        for index, contribution in enumerate(contributions):
            subject = "Project Banyan investment opportunity" if index == 0 else "Re: Project Banyan investment opportunity"
            body = contribution
            if index:
                body = f"{contribution}\n\nOn Wed, Aug 5, 2026 at 10:0{index} AM Prior Sender wrote:\n{contributions[index - 1]}"
            if index == 4:
                subject = "Fwd: Project Banyan investment opportunity"
                body = (
                    f"Forwarding the conversation back to the fund after external follow-up.\n\n"
                    f"---------- Original Message ----------\n{contribution}\n\n"
                    f"From: Investment Team <{account.email}>\nSent: Wednesday\n"
                    f"Subject: Re: Project Banyan\n\n{contributions[index - 1]}"
                )
            messages.append(Email.objects.create(
                email_account=account,
                graph_id=f"live-e2e-{run_key}-{index}",
                conversation_id=conversation_id,
                subject=subject,
                from_email=senders[index],
                body_text=body,
                attachments=[{"id": f"live-att-{run_key}-{index}", "name": ["Teaser.pdf", "Questions.xlsx", "MIS.xlsx", "Capex Request.docx", "Capex Schedule.xlsx"][index]}],
                date_received=now + timedelta(minutes=index * 5),
            ))
        return messages

    @staticmethod
    def _retained_facts(deltas):
        combined = "\n".join(item.text for item in deltas)
        facts = ["INR 125 crore", "FY26 revenue", "22 percent", "14 percent", "October 2026", "environmental approvals", "INR 46 crore", "Friday"]
        return {fact: combined.casefold().count(fact.casefold()) for fact in facts}
