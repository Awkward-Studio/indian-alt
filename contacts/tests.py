from datetime import date
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from banks.models import Bank
from accounts.models import Profile
from ai_orchestrator.models import AIAuditLog
from contacts.models import Contact, WorkplaceVerificationSuggestion
from deals.models import Deal, DealStatus
from meetings.models import Meeting


class BankerAnalyticsAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="relationship-analyst",
            password="test-password",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

        self.bank = Bank.objects.create(
            name="Example Capital",
            website_domain="example.test",
        )
        self.banker = Contact.objects.create(
            name="Asha Banker",
            designation="Director",
            location="Mumbai",
            sector_coverage=["Consumer"],
            bank=self.bank,
        )
        self.additional_contact = Contact.objects.create(
            name="Relationship Participant",
            bank=self.bank,
        )

        self.active_deal = self._deal(
            "Active mandate",
            DealStatus.STAGE_8,
            date(2026, 7, 20),
        )
        self._deal("IC mandate", DealStatus.STAGE_15, date(2026, 7, 10))
        self._deal("Passed mandate", DealStatus.PASSED, date(2026, 6, 2))
        self._deal("Invested mandate", DealStatus.INVESTED, date(2026, 5, 1))
        self._deal("Portfolio mandate", DealStatus.PORTFOLIO, date(2026, 4, 1))
        meeting = Meeting.objects.create(notes="Quarterly relationship review")
        meeting.contacts.add(self.banker)

        self.additional_only_deal = Deal.objects.create(
            title="Participant-only relationship",
            bank=self.bank,
            deal_status=DealStatus.STAGE_3,
            current_phase=DealStatus.STAGE_3,
            received_at=date(2026, 7, 25),
        )
        self.additional_only_deal.additional_contacts.add(self.banker)

    def _deal(self, title, deal_status, received_at):
        return Deal.objects.create(
            title=title,
            bank=self.bank,
            primary_contact=self.banker,
            deal_status=deal_status,
            current_phase=deal_status,
            received_at=received_at,
        )

    def test_banker_list_credits_only_primary_sourcing_relationships(self):
        response = self.client.get(
            reverse("banker-analytics-list"),
            {"entity_type": "banker"},
        )

        self.assertEqual(response.status_code, 200)
        banker = next(
            item
            for item in response.data["results"]
            if item["id"] == str(self.banker.id)
        )
        self.assertEqual(banker["entity_type"], "banker")
        self.assertEqual(banker["total_deals_introduced"], 5)
        self.assertEqual(banker["active_mandates"], 2)
        self.assertEqual(banker["sourced_mandates"], 1)
        self.assertEqual(banker["ic_mandates"], 1)
        self.assertEqual(banker["converted_deals"], 2)
        self.assertEqual(banker["passed_deals"], 1)
        self.assertEqual(banker["conversion_rate"], 40.0)
        self.assertEqual(banker["last_deal_date"], "2026-07-20")
        self.assertEqual(banker["meeting_count"], 1)
        self.assertIsNotNone(banker["last_interaction_at"])
        self.assertNotIn("activity_history", banker)

    def test_banker_detail_returns_bounded_primary_deal_activity(self):
        response = self.client.get(
            reverse(
                "banker-analytics-detail",
                kwargs={"pk": self.banker.id},
            ),
            {"entity_type": "banker", "activity_limit": 2},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["activity_history"]), 2)
        self.assertEqual(
            response.data["activity_history"][0]["deal_id"],
            str(self.active_deal.id),
        )
        activity_ids = {
            item["deal_id"] for item in response.data["activity_history"]
        }
        self.assertNotIn(str(self.additional_only_deal.id), activity_ids)

    def test_bank_metrics_include_all_deals_linked_to_the_bank(self):
        response = self.client.get(
            reverse("banker-analytics-detail", kwargs={"pk": self.bank.id}),
            {"entity_type": "bank"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["entity_type"], "bank")
        self.assertEqual(response.data["banker_count"], 2)
        self.assertEqual(response.data["total_deals_introduced"], 6)
        self.assertEqual(response.data["active_mandates"], 3)
        self.assertEqual(response.data["sourced_mandates"], 2)
        self.assertEqual(response.data["ic_mandates"], 1)
        self.assertEqual(response.data["converted_deals"], 2)
        self.assertEqual(response.data["passed_deals"], 1)
        self.assertEqual(response.data["conversion_rate"], 33.3)
        self.assertEqual(response.data["last_deal_date"], "2026-07-25")
        self.assertEqual(len(response.data["activity_history"]), 6)

    def test_invalid_entity_type_and_activity_limit_are_rejected(self):
        invalid_type = self.client.get(
            reverse("banker-analytics-list"),
            {"entity_type": "firm"},
        )
        invalid_limit = self.client.get(
            reverse(
                "banker-analytics-detail",
                kwargs={"pk": self.banker.id},
            ),
            {"activity_limit": "all"},
        )

        self.assertEqual(invalid_type.status_code, 400)
        self.assertEqual(invalid_limit.status_code, 400)

    def test_analytics_requires_authentication(self):
        self.client.force_authenticate(user=None)

        response = self.client.get(reverse("banker-analytics-list"))

        self.assertEqual(response.status_code, 401)


class ContactDirectoryAPITests(TestCase):
    def setUp(self):
        user = User.objects.create_user(
            username="directory-analyst",
            password="test-password",
        )
        self.client = APIClient()
        self.client.force_authenticate(user)
        bank = Bank.objects.create(name="Directory Search Capital")
        self.md = Contact.objects.create(
            name="Meera Search",
            email="meera@example.test",
            designation="Managing Director",
            location="Mumbai",
            sector_coverage=["Healthcare"],
            bank=bank,
        )
        self.director = Contact.objects.create(
            name="Dev Banker",
            email="dev@example.test",
            designation="Director",
            location="Delhi",
            sector_coverage=["Consumer"],
            bank=bank,
        )
        self.vp = Contact.objects.create(
            name="Vikram Banker",
            designation="Vice President",
            location="Bengaluru",
            sector_coverage=["Technology"],
        )
        self.associate = Contact.objects.create(
            name="Anita Banker",
            designation="Associate",
            location="Mumbai",
            sector_coverage=["Consumer"],
        )
        for index in range(3):
            Deal.objects.create(
                title=f"Meera Deal {index}",
                primary_contact=self.md,
                bank=bank,
            )
        Deal.objects.create(
            title="Director Deal",
            primary_contact=self.director,
            bank=bank,
        )

    def test_designation_quick_filters_keep_md_and_director_distinct(self):
        md_response = self.client.get(
            reverse("contact-list"),
            {"designation": "md"},
        )
        director_response = self.client.get(
            reverse("contact-list"),
            {"designation": "director"},
        )

        self.assertEqual(
            [item["id"] for item in md_response.data["results"]],
            [str(self.md.id)],
        )
        self.assertEqual(
            [item["id"] for item in director_response.data["results"]],
            [str(self.director.id)],
        )

    def test_search_covers_bank_sector_and_email(self):
        bank_response = self.client.get(
            reverse("contact-list"),
            {"search": "Directory Search"},
        )
        sector_response = self.client.get(
            reverse("contact-list"),
            {"search": "Technology"},
        )
        email_response = self.client.get(
            reverse("contact-list"),
            {"search": "meera@example.test"},
        )

        self.assertEqual(bank_response.data["count"], 2)
        self.assertEqual(
            [item["id"] for item in sector_response.data["results"]],
            [str(self.vp.id)],
        )
        self.assertEqual(
            [item["id"] for item in email_response.data["results"]],
            [str(self.md.id)],
        )

    def test_live_deal_count_is_returned_and_sortable(self):
        response = self.client.get(
            reverse("contact-list"),
            {"ordering": "-deal_count"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"][0]["id"], str(self.md.id))
        self.assertEqual(response.data["results"][0]["deal_count"], 3)
        self.assertIsNotNone(response.data["results"][0]["last_deal_date"])

    @patch('ai_orchestrator.services.document_processor.DocumentProcessorService.get_extraction_result')
    def test_visiting_card_can_be_reviewed_into_a_contact(self, extract):
        extract.return_value = {'normalized_text': 'Riya Investor\nPartner\nriya@example.test\n+91 98765 43210'}
        uploaded = self.client.post(
            reverse('contact-card-extraction-list'),
            {'file': SimpleUploadedFile('card.jpg', b'image', content_type='image/jpeg')},
            format='multipart',
        )
        self.assertEqual(uploaded.status_code, 201)
        reviewed = self.client.post(
            reverse('contact-card-extraction-review', kwargs={'pk': uploaded.data['id']}),
            {'extracted_data': {**uploaded.data['extracted_data'], 'contact_type': 'INVESTOR'}},
            format='json',
        )
        self.assertEqual(reviewed.status_code, 200)
        self.assertEqual(reviewed.data['contact']['email'], 'riya@example.test')

    def test_contact_type_can_be_created_and_filtered(self):
        created = self.client.post(
            reverse("contact-list"),
            {"name": "Ira Investor", "contact_type": "INVESTOR"},
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.data["contact_type"], "INVESTOR")

        response = self.client.get(reverse("contact-list"), {"contact_type": "INVESTOR"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["name"] for item in response.data["results"]], ["Ira Investor"])

    def test_invalid_contact_type_is_rejected(self):
        response = self.client.post(
            reverse("contact-list"),
            {"name": "Invalid", "contact_type": "BROKER"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class WorkplaceVerificationAPITests(TestCase):
    def setUp(self):
        self.reviewer = User.objects.create_user(
            username='verification-admin',
            password='test-password',
            is_staff=True,
        )
        self.regular_user = User.objects.create_user(
            username='verification-reader',
            password='test-password',
        )
        Profile.objects.create(
            user=self.regular_user,
            email='verification-reader@example.test',
            is_admin=False,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.reviewer)
        self.old_bank = Bank.objects.create(
            name='Old Capital',
            website_domain='oldcapital.test',
        )
        self.new_bank = Bank.objects.create(
            name='New Capital',
            website_domain='newcapital.test',
        )
        self.contact = Contact.objects.create(
            name='Riya Banker',
            designation='Director',
            bank=self.old_bank,
        )
        self.deal = Deal.objects.create(
            title='Sourced deal',
            primary_contact=self.contact,
            bank=self.old_bank,
        )

    def _verification_url(self):
        return f'/api/contacts/{self.contact.id}/workplace-verification/'

    def _review_url(self, suggestion):
        return (
            f'/api/contacts/{self.contact.id}/workplace-verification/'
            f'{suggestion.id}/review/'
        )

    @patch('contacts.services.workplace_verification.SearXNGProviderService.search_many')
    def test_verification_creates_evidence_without_mutating_contact(self, search_many):
        search_many.return_value = [{
            'title': 'Riya Banker - Managing Director - New Capital',
            'snippet': 'Riya Banker is Managing Director at New Capital.',
            'url': 'https://newcapital.test/team/riya-banker',
            'query': '"Riya Banker" banker current employer designation',
        }]

        response = self.client.post(self._verification_url())

        self.assertEqual(response.status_code, 201)
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.bank, self.old_bank)
        self.assertEqual(self.contact.designation, 'Director')
        suggestion = WorkplaceVerificationSuggestion.objects.get()
        self.assertEqual(suggestion.proposed_bank_name, 'New Capital')
        self.assertEqual(suggestion.proposed_designation, 'Managing Director')
        self.assertEqual(suggestion.status, 'PENDING')
        self.assertGreaterEqual(suggestion.confidence, 0.9)
        audit = AIAuditLog.objects.get(id=suggestion.audit_log_id)
        self.assertEqual(audit.source_type, 'banker_workplace_verification')
        self.assertTrue(audit.source_metadata['human_review_required'])

    @patch('contacts.services.workplace_verification.SearXNGProviderService.search_many')
    def test_authorized_review_applies_only_selected_fields_and_is_terminal(self, search_many):
        search_many.return_value = [{
            'title': 'Riya Banker at New Capital',
            'snippet': 'Riya Banker serves as Managing Director at New Capital.',
            'url': 'https://newcapital.test/leadership/riya',
            'query': '"Riya Banker" current role',
        }]
        suggestion_id = self.client.post(self._verification_url()).data['id']
        suggestion = WorkplaceVerificationSuggestion.objects.get(id=suggestion_id)

        response = self.client.post(
            self._review_url(suggestion),
            {
                'decision': 'ACCEPT',
                'accepted_fields': ['designation'],
                'comment': 'Confirmed from the firm leadership page.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.designation, 'Managing Director')
        self.assertEqual(self.contact.bank, self.old_bank)
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, 'ACCEPTED')
        self.assertEqual(suggestion.accepted_fields, ['designation'])
        self.assertEqual(
            suggestion.audit_log.source_metadata['review']['decision'],
            'ACCEPT',
        )

        repeated = self.client.post(
            self._review_url(suggestion),
            {'decision': 'REJECT'},
            format='json',
        )
        self.assertEqual(repeated.status_code, 400)

    @patch('contacts.services.workplace_verification.SearXNGProviderService.search_many')
    def test_bank_acceptance_updates_primary_sourced_deals(self, search_many):
        search_many.return_value = [{
            'title': 'Riya Banker joins New Capital',
            'snippet': 'New Capital appoints Riya Banker as Director.',
            'url': 'https://newcapital.test/news/appointment',
            'query': '"Riya Banker" current role',
        }]
        suggestion_id = self.client.post(self._verification_url()).data['id']
        suggestion = WorkplaceVerificationSuggestion.objects.get(id=suggestion_id)

        response = self.client.post(
            self._review_url(suggestion),
            {'decision': 'ACCEPT', 'accepted_fields': ['bank']},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.contact.refresh_from_db()
        self.deal.refresh_from_db()
        self.assertEqual(self.contact.bank, self.new_bank)
        self.assertEqual(self.deal.bank, self.new_bank)

    @patch('contacts.services.workplace_verification.SearXNGProviderService.search_many')
    def test_social_results_are_not_used_as_verification_evidence(self, search_many):
        search_many.return_value = [{
            'title': 'Riya Banker | LinkedIn',
            'snippet': 'Managing Director at New Capital',
            'url': 'https://www.linkedin.com/in/riya-banker',
            'query': '"Riya Banker" current role',
        }]

        response = self.client.post(self._verification_url())

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data['suggestion'])
        self.assertFalse(WorkplaceVerificationSuggestion.objects.exists())
        audit = AIAuditLog.objects.get(id=response.data['audit_log'])
        self.assertEqual(audit.source_metadata['result_count'], 0)

    def test_review_requires_contact_authority(self):
        suggestion = WorkplaceVerificationSuggestion.objects.create(
            contact=self.contact,
            old_bank_name='Old Capital',
            old_designation='Director',
            proposed_bank_name='New Capital',
            proposed_designation='Managing Director',
            source_url='https://newcapital.test/team/riya',
            search_query='Riya Banker current employer',
            confidence=0.9,
            retrieved_at=timezone.now(),
        )
        self.client.force_authenticate(self.regular_user)

        response = self.client.post(
            self._review_url(suggestion),
            {'decision': 'REJECT'},
            format='json',
        )

        self.assertEqual(response.status_code, 403)
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, 'PENDING')
