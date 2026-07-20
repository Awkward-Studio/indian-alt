from django.contrib.auth.models import User
from django.urls import reverse
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from accounts.models import Profile
from deals.models import Deal, DealAnalysis
from .models import Task, TaskStatus, TaskSuggestion, TaskSuggestionState
from .services import merged_task_candidates, sync_deal_suggestions


REPORT = """
## Key Financials
| Next steps / further diligence / red flags | Details |
| --- | --- |
| Financial Validation | Request audited financial statements and validate revenue. |

## Next Steps
| Serial Number | Tasks / Next Step | Task Owner | Task assigned to | Status |
| --- | --- | --- | --- | --- |
| 1 | Request audited financial statements and validate revenue. | Analyst | Deal Team | Pending |
| 2 | Obtain the latest cap table. | Legal | Deal Team | Pending |
"""


class SuggestionMergeTests(SimpleTestCase):
    def test_merges_matching_canonical_row_and_keeps_unmatched_row(self):
        candidates = merged_task_candidates(REPORT)

        self.assertEqual(len(candidates), 2)
        financial = next(item for item in candidates if "financial" in item["title"])
        self.assertEqual(financial["source_owner"], "Analyst")
        self.assertEqual(len(financial["source_references"]), 2)
        self.assertTrue(financial["matched_canonical"])


class WorkItemAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="analyst@example.com", password="test")
        self.profile = Profile.objects.create(user=self.user, email="analyst@example.com", name="Analyst")
        self.deal = Deal.objects.create(title="Acme")
        self.analysis = DealAnalysis.objects.create(
            deal=self.deal,
            version=1,
            analysis_json={"analyst_report": REPORT},
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_sync_is_idempotent(self):
        first = sync_deal_suggestions(self.deal, self.analysis)
        second = sync_deal_suggestions(self.deal, self.analysis)

        self.assertEqual(first["candidates"], 2)
        self.assertEqual(TaskSuggestion.objects.filter(deal=self.deal).count(), 2)
        self.assertEqual(second["created"], 0)

    def test_accept_is_idempotent_and_creates_unassigned_todo(self):
        sync_deal_suggestions(self.deal, self.analysis)
        suggestion = TaskSuggestion.objects.filter(deal=self.deal).first()
        url = reverse("task-suggestion-accept", kwargs={"pk": suggestion.id})

        first = self.client.post(url)
        second = self.client.post(url)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(Task.objects.count(), 1)
        task = Task.objects.get()
        self.assertEqual(task.status, TaskStatus.TODO)
        self.assertIsNone(task.assignee)
        self.assertEqual(task.title, suggestion.category or task.title)
        self.assertEqual(task.description, suggestion.title)
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.state, TaskSuggestionState.ACCEPTED)

    def test_done_timestamp_and_permanent_delete_dismisses_source(self):
        sync_deal_suggestions(self.deal, self.analysis)
        suggestion = TaskSuggestion.objects.first()
        accepted = self.client.post(reverse("task-suggestion-accept", kwargs={"pk": suggestion.id})).json()

        updated = self.client.patch(
            reverse("task-detail", kwargs={"pk": accepted["id"]}), {"status": "done"}, format="json"
        )
        self.assertEqual(updated.status_code, 200)
        self.assertIsNotNone(updated.json()["completed_at"])

        deleted = self.client.delete(reverse("task-detail", kwargs={"pk": accepted["id"]}))
        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(Task.objects.filter(id=accepted["id"]).exists())
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.state, TaskSuggestionState.DISMISSED)
        self.assertIsNone(suggestion.task)

    def test_disabled_profile_cannot_access_tasks(self):
        self.profile.is_disabled = True
        self.profile.save(update_fields=["is_disabled"])

        response = self.client.get(reverse("task-list"))

        self.assertEqual(response.status_code, 403)
