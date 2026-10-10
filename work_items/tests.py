from django.contrib.auth.models import User
from django.urls import reverse
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from accounts.models import Profile
from ai_orchestrator.models import AIAuditLog
from deals.models import Deal, DealAnalysis
from .models import Task, TaskActivity, TaskComment, TaskStatus, TaskSuggestion, TaskSuggestionState
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
    def test_nested_headings_keep_parent_section_and_task_column_is_not_owner(self):
        from deals.services.analysis_next_steps import inspect_analysis_next_steps
        parsed = inspect_analysis_next_steps('''## Key Financials
### Further diligence
| Next Step | Owner |
| --- | --- |
| Obtain the audited accounts. | Finance team |
''')
        self.assertEqual(parsed['tasks'][0]['source_section'], 'Key Financials')
        self.assertEqual(parsed['tasks'][0]['task'], 'Obtain the audited accounts.')
        self.assertEqual(parsed['tasks'][0]['owner'], 'Finance team')

    def test_explicit_action_lists_are_picked_up_without_citation_or_risk_bullets(self):
        from deals.services.analysis_next_steps import inspect_analysis_next_steps
        parsed = inspect_analysis_next_steps('''## Risk Factors
- Revenue is concentrated.
## Next Steps
### Pre-IC decision gates
- Obtain the latest cap table.
1. Financials: Reconcile audited accounts against the model.
### Citations
1. Obtain source title only.
''')
        self.assertEqual(len(parsed['tasks']), 2)
        self.assertTrue(all(task['source_section']=='Next Steps' for task in parsed['tasks']))

    def test_merges_matching_canonical_row_and_keeps_unmatched_row(self):
        candidates = merged_task_candidates(REPORT)

        self.assertEqual(len(candidates), 2)
        financial = next(item for item in candidates if "financial" in item["title"])
        self.assertEqual(financial["source_owner"], "Analyst")
        self.assertEqual(len(financial["source_references"]), 2)
        self.assertTrue(financial["matched_canonical"])


class WorkItemAPITests(TestCase):
    def test_synthesis_summary_keeps_previous_report_suggestions_available(self):
        from .services import ensure_latest_suggestions, latest_task_analysis
        sync_deal_suggestions(self.deal, self.analysis)
        DealAnalysis.objects.create(deal=self.deal, version=2,
            analysis_json={'analyst_report': 'Short source-based company summary.', 'metadata': {'field_synthesis_key': 'new-run'}})
        self.assertEqual(latest_task_analysis(self.deal).pk, self.analysis.pk)
        ensure_latest_suggestions(self.deal)
        self.assertEqual(TaskSuggestion.objects.filter(deal=self.deal,state=TaskSuggestionState.PENDING).count(),2)

    def test_accept_supports_reviewed_owner_and_due_date_and_source_filter(self):
        sync_deal_suggestions(self.deal, self.analysis)
        suggestion = TaskSuggestion.objects.filter(deal=self.deal).first()
        response = self.client.post(reverse('task-suggestion-accept',kwargs={'pk':suggestion.pk}),
            {'title':'Verify current financial statements', 'assignee_id': str(self.profile.pk), 'due_date':'2026-12-01'},format='json')
        self.assertEqual(response.status_code,201,response.data)
        self.assertEqual(response.data['assignee']['id'],str(self.profile.pk))
        self.assertEqual(response.data['due_date'],'2026-12-01')
        filtered=self.client.get(reverse('task-list'),{'deal':str(self.deal.pk),'source_section':'Next Steps'})
        self.assertEqual(filtered.data['count'],1)

    def test_subsection_attribution_is_repaired_without_reviving_dismissed_suggestions(self):
        from .services import ensure_latest_suggestions
        sync_deal_suggestions(self.deal, self.analysis)
        suggestion=TaskSuggestion.objects.filter(deal=self.deal).first()
        suggestion.source_section='Pre-IC Decision Gates'
        suggestion.state=TaskSuggestionState.DISMISSED
        suggestion.save()
        ensure_latest_suggestions(self.deal)
        suggestion.refresh_from_db()
        self.assertNotEqual(suggestion.source_section,'Pre-IC Decision Gates')
        self.assertEqual(suggestion.state,TaskSuggestionState.DISMISSED)

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

    def test_assignee_completion_requires_allocator_review_and_supports_comments(self):
        allocator_user = User.objects.create_user(username="allocator@example.com", password="test")
        allocator = Profile.objects.create(user=allocator_user, email="allocator@example.com", name="Allocator")
        task = Task.objects.create(deal=self.deal, title="Review", created_by=allocator, assignee=self.profile)

        response = self.client.patch(reverse("task-detail", kwargs={"pk": task.id}), {"status": "done"}, format="json")
        self.assertEqual(response.json()["status"], TaskStatus.IN_REVIEW)
        comment = self.client.post(reverse("task-comment-list"), {"task": str(task.id), "body": "Ready for review"}, format="json")
        self.assertEqual(comment.status_code, 201)
        self.assertEqual(TaskComment.objects.get().body, "Ready for review")
        self.client.force_authenticate(allocator_user)
        approved = self.client.patch(reverse("task-detail", kwargs={"pk": task.id}), {"status": "done"}, format="json")
        self.assertEqual(approved.json()["status"], TaskStatus.DONE)
        self.assertIsNotNone(approved.json()["completed_at"])
        self.assertIsNone(approved.json()["review_requested_at"])

    def test_assignment_date_changes_only_when_assignment_changes(self):
        task = Task.objects.create(deal=self.deal, title="Assignment", created_by=self.profile)
        self.assertIsNone(task.assigned_at)
        url = reverse("task-detail", kwargs={"pk": task.id})
        response = self.client.patch(url, {"assignee_id": str(self.profile.id)}, format="json")
        self.assertEqual(response.status_code, 200)
        assigned_at = response.json()["assigned_at"]
        self.assertIsNotNone(assigned_at)
        updated = self.client.patch(url, {"description": "Additional details"}, format="json")
        self.assertEqual(updated.json()["assigned_at"], assigned_at)
        removed = self.client.patch(url, {"assignee_id": None}, format="json")
        self.assertIsNone(removed.json()["assigned_at"])

    def test_move_swaps_saved_positions(self):
        first = Task.objects.create(deal=self.deal, title="First", created_by=self.profile, position=1)
        second = Task.objects.create(deal=self.deal, title="Second", created_by=self.profile, position=2)
        response = self.client.post(reverse("task-move", kwargs={"pk": second.id}), {"direction": "up"}, format="json")
        self.assertEqual(response.status_code, 200)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.position, second.position), (2, 1))

    def test_move_to_places_task_at_drop_target(self):
        first = Task.objects.create(deal=self.deal, title="First", created_by=self.profile, position=1)
        second = Task.objects.create(deal=self.deal, title="Second", created_by=self.profile, position=2)
        third = Task.objects.create(deal=self.deal, title="Third", created_by=self.profile, position=3)

        response = self.client.post(
            reverse("task-move-to", kwargs={"pk": third.id}),
            {"target_id": str(first.id), "placement": "before"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        ordered_ids = list(Task.objects.filter(deal=self.deal).order_by("position").values_list("id", flat=True))
        self.assertEqual(ordered_ids, [third.id, first.id, second.id])
        self.assertEqual(TaskActivity.objects.get().action, TaskActivity.Action.REORDERED)

    def test_move_to_supports_one_saved_order_across_deals(self):
        other_deal = Deal.objects.create(title="Other")
        task = Task.objects.create(deal=self.deal, title="First", created_by=self.profile, position=1)
        target = Task.objects.create(deal=other_deal, title="Other", created_by=self.profile, position=2)

        response = self.client.post(
            reverse("task-move-to", kwargs={"pk": task.id}),
            {"target_id": str(target.id), "placement": "after"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        ordered_ids = list(Task.objects.order_by("position").values_list("id", flat=True))
        self.assertEqual(ordered_ids, [target.id, task.id])

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


class ReportGapSuggestionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='gap-reviewer')
        self.profile = Profile.objects.create(user=self.user, email='gaps@example.test', name='Reviewer')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.deal = Deal.objects.create(title='Gap company')
        self.analysis = DealAnalysis.objects.create(deal=self.deal, version=1,
            analysis_json={'analyst_report': REPORT})
        self.parent = AIAuditLog.objects.create(source_type='deal_full_synthesis', source_id=str(self.deal.pk),
            source_metadata={'queue_kind': 'report'}, status='PROCESSING')

    def section(self, warnings, *, section='Key Financials', outcome='saved_with_gaps'):
        return AIAuditLog.objects.create(source_type='vdr_report_section', source_id=str(self.parent.pk),
            status='COMPLETED', source_metadata={'vdr_parent_audit_id': str(self.parent.pk),
                'report_section': section, 'report_section_outcome': outcome,
                'report_validation_warnings': warnings})

    def test_saved_gap_creates_suggestion_first_without_assigning_priority(self):
        from .services import ensure_latest_suggestions
        message = 'Revenue in FY24 Actual could not be independently verified from the cited workbook cells.'
        self.section([{'kind': 'verification', 'message': message, 'details': ['Review Revenue!B4'], 'confirmed': False}])

        ensure_latest_suggestions(self.deal)
        suggestion = TaskSuggestion.objects.get(deal=self.deal, source_table_kind='report_source_gap')

        self.assertEqual(suggestion.source_section, 'Key Financials')
        self.assertEqual(suggestion.source_priority, '')
        from .services import accepted_task_defaults
        self.assertNotIn('priority', accepted_task_defaults(suggestion))
        self.assertIn(message, suggestion.title)
        self.assertIn('does not establish', suggestion.title)
        self.assertIn('Review Revenue!B4', suggestion.title)
        self.assertFalse(suggestion.source_references[0]['confirmed'])
        response = self.client.get(reverse('task-suggestion-list'), {'deal': str(self.deal.pk)})
        self.assertEqual(response.data['results'][0]['id'], str(suggestion.pk))
        accepted = self.client.post(reverse('task-suggestion-accept', kwargs={'pk': suggestion.pk}))
        self.assertEqual(accepted.data['priority'], 'medium')  # Existing task-model default.
        self.assertEqual(accepted.data['position'], 0)

    def test_saved_section_signal_syncs_without_waiting_for_tasks_page(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.section([{'kind': 'calculation', 'message': '2 + 2 = 5 is inconsistent.'}])
        self.assertTrue(TaskSuggestion.objects.filter(deal=self.deal, source_table_kind='report_source_gap').exists())

    def test_repeated_period_warnings_group_and_repeated_sync_does_not_duplicate(self):
        messages = [f'Revenue in FY{year} Actual could not be independently verified from the cited workbook cells.' for year in (24, 25)]
        messages.append('Revenue in FY26 Forecast has no source reference; the amount is not verified.')
        self.section([{'kind': 'verification', 'message': message} for message in messages])
        sync_deal_suggestions(self.deal)
        sync_deal_suggestions(self.deal)
        suggestions = TaskSuggestion.objects.filter(deal=self.deal, source_table_kind='report_source_gap', state='pending')
        self.assertEqual(suggestions.count(), 1)
        self.assertEqual(len(suggestions.get().source_references), 3)
        self.assertTrue(all(message in suggestions.get().title for message in messages))

    def test_newer_saved_section_clears_old_gap_and_draft_does_not_create_one(self):
        from .services import ensure_latest_suggestions
        self.section([{'kind': 'verification', 'message': 'Revenue needs verification.'}])
        ensure_latest_suggestions(self.deal)
        self.section([{'kind': 'calculation', 'message': 'Draft error'}], outcome='draft_ready_with_gaps')
        ensure_latest_suggestions(self.deal)
        self.assertEqual(TaskSuggestion.objects.filter(deal=self.deal, source_table_kind='report_source_gap', state='pending').count(), 1)
        self.section([], outcome='accepted')
        ensure_latest_suggestions(self.deal)
        self.assertFalse(TaskSuggestion.objects.filter(deal=self.deal, source_table_kind='report_source_gap', state='pending').exists())

    def test_dismissed_gap_stays_dismissed_when_report_changes(self):
        from .services import ensure_latest_suggestions
        self.section([{'kind': 'verification', 'message': 'Revenue needs verification.'}])
        ensure_latest_suggestions(self.deal)
        suggestion = TaskSuggestion.objects.get(deal=self.deal, source_table_kind='report_source_gap')
        suggestion.state = TaskSuggestionState.DISMISSED
        suggestion.save()
        DealAnalysis.objects.create(deal=self.deal, version=2, analysis_json={'analyst_report': REPORT + '\nUpdated report.'})
        ensure_latest_suggestions(self.deal)
        self.assertFalse(TaskSuggestion.objects.filter(deal=self.deal, source_table_kind='report_source_gap', state='pending').exists())

    def test_gap_stays_with_its_deal(self):
        from .services import ensure_latest_suggestions
        self.section([{'kind': 'verification', 'message': 'Revenue needs verification.'}])
        other = Deal.objects.create(title='Another company')
        DealAnalysis.objects.create(deal=other, version=1, analysis_json={'analyst_report': REPORT})
        ensure_latest_suggestions(other)
        self.assertFalse(TaskSuggestion.objects.filter(deal=other, source_table_kind='report_source_gap').exists())
