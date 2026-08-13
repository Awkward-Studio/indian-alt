from django.contrib.auth.models import User
from django.urls import reverse
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from accounts.models import Profile
from deals.models import Deal, DealAnalysis
from .models import Task, TaskActivity, TaskStatus, TaskSuggestion, TaskSuggestionState
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
        activity = TaskActivity.objects.get()
        self.assertEqual(activity.action, TaskActivity.Action.SUGGESTION_ACCEPTED)
        self.assertEqual(activity.actor, self.profile)
        self.assertEqual(activity.source_context["suggestion_id"], str(suggestion.id))

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

    def test_create_update_complete_reopen_assign_prioritize_and_due_date_are_audited(self):
        created = self.client.post(
            reverse("task-list"),
            {"deal": str(self.deal.id), "title": "Review market", "description": "Initial"},
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        task_id = created.json()["id"]
        self.assertEqual(TaskActivity.objects.get().action, TaskActivity.Action.CREATED)

        updates = [
            ({"status": "done"}, TaskActivity.Action.COMPLETED, ["status"]),
            ({"status": "todo"}, TaskActivity.Action.REOPENED, ["status"]),
            ({"assignee_id": str(self.profile.id)}, TaskActivity.Action.ASSIGNED, ["assignee_id"]),
            ({"priority": "high"}, TaskActivity.Action.PRIORITIZED, ["priority"]),
            ({"due_date": "2026-09-01"}, TaskActivity.Action.DUE_DATE_CHANGED, ["due_date"]),
            ({"title": "Review addressable market"}, TaskActivity.Action.UPDATED, ["title"]),
        ]
        for payload, expected_action, expected_fields in updates:
            response = self.client.patch(
                reverse("task-detail", kwargs={"pk": task_id}), payload, format="json"
            )
            self.assertEqual(response.status_code, 200)
            activity = TaskActivity.objects.order_by("-created_at", "-id").first()
            self.assertEqual(activity.action, expected_action)
            self.assertEqual(activity.changed_fields, expected_fields)
            self.assertEqual(activity.actor, self.profile)
            self.assertTrue(activity.created_at)

    def test_noop_update_does_not_create_activity(self):
        task = Task.objects.create(deal=self.deal, title="No-op", created_by=self.profile)
        response = self.client.patch(
            reverse("task-detail", kwargs={"pk": task.id}),
            {"status": TaskStatus.TODO},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(TaskActivity.objects.exists())

    def test_delete_activity_retains_task_and_deal_snapshot(self):
        task = Task.objects.create(deal=self.deal, title="Retained deletion", created_by=self.profile)
        task_id = task.id

        response = self.client.delete(reverse("task-detail", kwargs={"pk": task.id}))

        self.assertEqual(response.status_code, 204)
        activity = TaskActivity.objects.get()
        self.assertEqual(activity.action, TaskActivity.Action.DELETED)
        self.assertIsNone(activity.task)
        self.assertEqual(activity.task_id_snapshot, task_id)
        self.assertEqual(activity.task_title, "Retained deletion")
        self.assertEqual(activity.deal, self.deal)
        self.assertEqual(activity.before["title"], "Retained deletion")

    def test_activity_api_is_read_only_filterable_and_paginated(self):
        task = Task.objects.create(deal=self.deal, title="Timeline", created_by=self.profile)
        for index in range(3):
            TaskActivity.objects.create(
                task=task,
                task_id_snapshot=task.id,
                task_title=task.title,
                deal=self.deal,
                actor=self.profile,
                action=TaskActivity.Action.UPDATED,
                changed_fields=["title"],
                before={"title": f"Before {index}"},
                after={"title": f"After {index}"},
            )

        response = self.client.get(
            reverse("task-activity-list"),
            {"deal": str(self.deal.id), "task": str(task.id), "action": "updated", "page_size": 2},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 3)
        self.assertEqual(len(response.json()["results"]), 2)
        self.assertEqual(response.json()["results"][0]["actor"]["id"], str(self.profile.id))
        self.assertEqual(self.client.post(reverse("task-activity-list"), {}, format="json").status_code, 405)

    def test_disabled_profile_cannot_access_activity(self):
        self.profile.is_disabled = True
        self.profile.save(update_fields=["is_disabled"])

        self.assertEqual(self.client.get(reverse("task-activity-list")).status_code, 403)
