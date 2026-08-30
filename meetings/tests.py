import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from accounts.models import Profile
from ai_orchestrator.models import AIAuditLog
from deals.models import Deal
from meetings.models import MeetingNote, MeetingSignalFlag
from meetings.services.meeting_signal_analysis import MeetingSignalAnalysisService
from meetings.serializers import MeetingNoteSerializer
from contacts.models import Contact, ContactInteraction


class MeetingContactLinkTests(TestCase):
    @patch('ai_orchestrator.services.embedding_processor.EmbeddingService.vectorize_meeting_note', return_value=True)
    def test_meeting_note_accepts_contacts_and_creates_interaction(self, _vectorize):
        contact = Contact.objects.create(name='Meeting attendee')
        serializer = MeetingNoteSerializer(data={'title': 'Founder call', 'summary': 'Discussed progress.', 'body': '', 'contact_ids': [str(contact.id)]})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        note = serializer.save()
        self.assertEqual(list(note.contacts.all()), [contact])
        self.assertTrue(ContactInteraction.objects.filter(contact=contact, meeting_note=note, kind='MEETING').exists())


class MeetingSignalAnalysisAuditTests(SimpleTestCase):
    def setUp(self):
        self.deal = SimpleNamespace(id=uuid4(), title="Audit Deal")
        self.note = SimpleNamespace(
            id=uuid4(),
            title="Management call",
            meeting_at=None,
            summary="Revenue grew.",
            body="Management discussed revenue growth and customer concentration.",
        )

    @patch("meetings.services.meeting_signal_analysis.AIAuditLog.objects.create")
    @patch("meetings.services.meeting_signal_analysis.MeetingSignalAnalysisService.persist_signals", return_value=[])
    @patch("meetings.services.meeting_signal_analysis.PromptCatalogService.get", return_value="system")
    @patch("ai_orchestrator.services.llm_providers.VLLMProviderService.execute_standard")
    def test_success_persists_completed_ai_history_entry(self, vm_execute, _prompt, _persist, create_audit_log):
        audit_log = MagicMock(
            id=uuid4(),
            source_metadata={"workflow": "cross_meeting_signal_analysis"},
        )
        create_audit_log.return_value = audit_log
        service = MeetingSignalAnalysisService()
        service._broadcast_audit = MagicMock()
        raw_result = {
            "executive_summary": "Growth is positive, with concentration risk.",
            "green_signals": [],
            "red_signals": [],
            "open_questions": ["Provide concentration data."],
        }
        vm_execute.return_value = {"response": json.dumps(raw_result)}

        result = service.analyze_deal(self.deal, [self.note])

        self.assertEqual(result["audit_log_id"], str(audit_log.id))
        self.assertEqual(audit_log.status, "COMPLETED")
        self.assertTrue(audit_log.is_success)
        self.assertEqual(audit_log.parsed_json, result)
        self.assertEqual(
            create_audit_log.call_args.kwargs["source_type"],
            "meeting_signal_analysis",
        )
        self.assertEqual(audit_log.save.call_count, 2)
        service._broadcast_audit.assert_called_with(audit_log, done=True)


class PersistentMeetingSignalTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='signal-reviewer', password='password')
        self.profile = Profile.objects.create(
            user=self.user,
            name='Signal Reviewer',
            email='signal-reviewer@example.com',
        )
        self.deal = Deal.objects.create(title='Signal Deal')
        self.deal.responsibility.add(self.profile)
        self.note = MeetingNote.objects.create(title='Management meeting', body='Customer concentration increased.')
        self.note.deals.add(self.deal)
        self.audit = AIAuditLog.objects.create(
            source_type='meeting_signal_analysis',
            source_id=str(self.deal.id),
            model_used='test-model',
            system_prompt='system',
            user_prompt='prompt',
            raw_response='{}',
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _result(self, passage='Customer A is 45% of revenue.'):
        return {
            'red_signals': [{
                'title': 'Customer concentration',
                'detail': 'Revenue depends on one customer.',
                'confidence': 'high',
                'evidence': [passage],
            }],
            'green_signals': [],
            'open_questions': [],
        }

    def _persist(self, result=None, audit=None):
        return MeetingSignalAnalysisService.persist_signals(
            deal=self.deal,
            notes=[self.note],
            audit_log=audit or self.audit,
            result=result or self._result(),
        )

    def test_unchanged_rerun_preserves_terminal_review_and_updates_trace(self):
        signal_id = self._persist()[0]['id']
        signal = MeetingSignalFlag.objects.get(id=signal_id)
        signal.review_status = MeetingSignalFlag.ReviewStatus.DISMISSED
        signal.reviewer = self.user
        signal.reviewed_at = signal.last_detected_at
        signal.review_comment = 'Not material.'
        signal.save()
        next_audit = AIAuditLog.objects.create(
            source_type='meeting_signal_analysis', source_id=str(self.deal.id),
            model_used='test-model', system_prompt='system', user_prompt='prompt', raw_response='{}',
        )

        rerun = self._persist(audit=next_audit)[0]

        self.assertEqual(rerun['id'], signal_id)
        self.assertEqual(rerun['review_status'], 'DISMISSED')
        self.assertEqual(rerun['review_comment'], 'Not material.')
        self.assertEqual(rerun['detection_count'], 2)
        self.assertEqual(rerun['latest_audit_log_id'], str(next_audit.id))

    def test_materially_changed_evidence_creates_new_unreviewed_revision(self):
        original = self._persist()[0]
        changed = self._persist(self._result('Customer A is now 62% of revenue.'))[0]

        self.assertNotEqual(original['id'], changed['id'])
        self.assertEqual(changed['review_status'], 'UNREVIEWED')
        self.assertEqual(MeetingSignalFlag.objects.filter(deal=self.deal).count(), 2)

    def test_evidence_keeps_retrievable_note_and_audit_references(self):
        persisted = self._persist()[0]

        self.assertEqual(persisted['source_note_ids'], [str(self.note.id)])
        self.assertEqual(persisted['evidence'][0]['source_note_ids'], [str(self.note.id)])
        self.assertEqual(persisted['first_audit_log_id'], str(self.audit.id))

    def test_review_api_is_attributable_filterable_and_terminal(self):
        signal = self._persist()[0]
        response = self.client.patch(
            f'/api/meetings/meeting-notes/deal-signals/{self.deal.id}/{signal["id"]}/',
            {
                'review_status': 'CONFIRMED',
                'comment': 'Verified against the transcript.',
                'expected_last_detected_at': signal['last_detected_at'],
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['review_status'], 'CONFIRMED')
        self.assertEqual(response.data['reviewer']['name'], 'Signal Reviewer')
        listed = self.client.get(
            f'/api/meetings/meeting-notes/deal-signals/{self.deal.id}/',
            {'status': 'CONFIRMED', 'kind': 'RED'},
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data['count'], 1)
        invalid = self.client.patch(
            f'/api/meetings/meeting-notes/deal-signals/{self.deal.id}/{signal["id"]}/',
            {'review_status': 'DISMISSED'},
            format='json',
        )
        self.assertEqual(invalid.status_code, 409)

    def test_cross_deal_and_unauthorized_access_are_rejected(self):
        signal = self._persist()[0]
        other_deal = Deal.objects.create(title='Other Deal')
        other_deal.responsibility.add(self.profile)
        cross_deal = self.client.patch(
            f'/api/meetings/meeting-notes/deal-signals/{other_deal.id}/{signal["id"]}/',
            {'review_status': 'CONFIRMED'},
            format='json',
        )
        self.assertEqual(cross_deal.status_code, 404)

        other_user = User.objects.create_user(username='other')
        Profile.objects.create(user=other_user, name='Other', email='other-signal@example.com')
        self.client.force_authenticate(other_user)
        unauthorized = self.client.get(f'/api/meetings/meeting-notes/deal-signals/{self.deal.id}/')
        self.assertEqual(unauthorized.status_code, 403)

    @patch("meetings.services.meeting_signal_analysis.AIAuditLog.objects.create")
    @patch("meetings.services.meeting_signal_analysis.PromptCatalogService.get", return_value="system")
    @patch("ai_orchestrator.services.llm_providers.VLLMProviderService.execute_standard", side_effect=RuntimeError("vm offline"))
    def test_failure_persists_failed_ai_history_entry(self, _vm, _prompt, create_audit_log):
        audit_log = MagicMock(
            id=uuid4(),
            source_metadata={"workflow": "cross_meeting_signal_analysis"},
        )
        create_audit_log.return_value = audit_log
        service = MeetingSignalAnalysisService()
        service._broadcast_audit = MagicMock()

        with self.assertRaisesRegex(RuntimeError, "VM meeting signal analysis failed"):
            service.analyze_deal(self.deal, [self.note])

        self.assertEqual(audit_log.status, "FAILED")
        self.assertFalse(audit_log.is_success)
        self.assertIn("offline", audit_log.error_message)
        audit_log.save.assert_called_once()
        service._broadcast_audit.assert_called_with(audit_log, done=True)
