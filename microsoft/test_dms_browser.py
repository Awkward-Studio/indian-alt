from unittest.mock import patch
from types import SimpleNamespace

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from microsoft.services.graph_service import GraphAPIService
from microsoft.views import OneDriveDownloadView, OneDriveListView
from ai_orchestrator.vm_status_view import VMStatusView


class DMSBrowserTests(SimpleTestCase):
    def request(self, view, params=None, method="get"):
        request = getattr(APIRequestFactory(), method)("/", params or {})
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
        return view.as_view()(request)

    def test_root_listing_consumes_all_pages(self):
        graph = object.__new__(GraphAPIService)
        with patch.object(graph, "get_access_token", return_value="token"), patch.object(graph, "_make_request", side_effect=[
            {"value": [{"id": "first"}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/next"},
            {"value": [{"id": "second"}]},
        ]) as request:
            result = graph.list_drive_root_children("drive", "dms@example.com")
        self.assertEqual(len(result["value"]), 2)
        self.assertEqual(request.call_count, 2)
        self.assertNotIn("@odata.nextLink", result)

    def test_shared_listing_consumes_all_pages(self):
        graph = object.__new__(GraphAPIService)
        with patch.object(graph, "get_access_token", return_value="token"), patch.object(graph, "_make_request", side_effect=[
            {"value": [{"id": "first"}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/next"},
            {"value": [{"id": "second"}]},
        ]):
            self.assertEqual(len(graph.list_shared_folder("https://example.com/share")["value"]), 2)

    @patch("microsoft.views.GraphAPIService")
    def test_folder_count_includes_empty_folders_and_excludes_files(self, service):
        service.return_value.get_drive_root_children.return_value = {"value": [
            {"id": "folder", "name": "Empty", "folder": {}},
            {"id": "file", "name": "Report.pdf", "file": {}},
        ]}
        response = self.request(OneDriveListView)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["folder_count"], 1)

    @patch("microsoft.views.GraphAPIService")
    def test_open_prefers_native_office_web_link_even_without_download(self, service):
        service.return_value.get_drive_item_download_url.return_value = ""
        service.return_value.get_drive_item.return_value = {"webUrl": "https://tenant.sharepoint.com/report.docx"}
        response = self.request(OneDriveDownloadView, {"item_id": "file", "drive_id": "shared", "intent": "open"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["open_url"], "https://tenant.sharepoint.com/report.docx")
        service.return_value.get_drive_item.assert_called_once_with("shared", "file")

    @patch("microsoft.views.GraphAPIService")
    def test_download_without_url_reports_failure(self, service):
        service.return_value.get_drive_item_download_url.return_value = ""
        self.assertEqual(self.request(OneDriveDownloadView, {"item_id": "file"}).status_code, 404)

    @patch("ai_orchestrator.vm_status_view.VMControlService")
    def test_regular_user_can_read_status_but_not_power_controls(self, service):
        service.return_value.snapshot.return_value = SimpleNamespace(power_state="deallocated", service_state="offline", startup_phase="offline", services={}, error="")
        response = self.request(VMStatusView)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["power_state"], "deallocated")
        self.assertNotIn("allowed_actions", response.data)
        self.assertEqual(self.request(VMStatusView, {"action": "start"}, "post").status_code, 405)

    def test_vm_status_requires_authentication(self):
        response = VMStatusView.as_view()(APIRequestFactory().get("/"))
        self.assertIn(response.status_code, [401, 403])
