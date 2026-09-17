from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from microsoft.services.graph_service import GraphAPIService


class DealFolderRootServiceTests(SimpleTestCase):
    @patch(
        "microsoft.services.graph_service.DMS_DEAL_FOLDER_URL",
        "https://indiaalt.sharepoint.com/Shared%20Documents/Deals%20-%20DMS",
    )
    def test_prefers_dedicated_shared_folder_url(self):
        service = object.__new__(GraphAPIService)
        children = {
            "value": [
                {
                    "id": "folder-1",
                    "name": "Acme",
                    "folder": {},
                    "parentReference": {"driveId": "sharepoint-drive"},
                }
            ]
        }

        with patch.object(service, "list_shared_folder", return_value=children) as list_shared:
            result = service.get_deal_folder_root_children("dms@example.com")

        list_shared.assert_called_once_with(
            "https://indiaalt.sharepoint.com/Shared%20Documents/Deals%20-%20DMS",
            "dms@example.com",
        )
        self.assertEqual(result["value"][0]["driveId"], "sharepoint-drive")

    @patch("microsoft.services.graph_service.DMS_DEAL_FOLDER_URL", "")
    @patch("microsoft.services.graph_service.DMS_DEAL_FOLDER_PATH", "Documents/Dataroom/4. Deal Folder/4. All - 16062026")
    @patch("microsoft.services.graph_service.DMS_DRIVE_ID", "drive-1")
    def test_resolves_root_then_returns_all_paginated_children(self):
        # These methods are mocked; bypass MSAL construction so this unit test
        # never performs tenant discovery over the network.
        service = object.__new__(GraphAPIService)

        with (
            patch.object(service, "get_drive_item", return_value={"id": "all-root"}) as get_root,
            patch.object(
                service,
                "get_drive_item_children",
                return_value={"value": [{"id": "folder-1", "name": "Acme", "folder": {}}]},
            ) as get_children,
        ):
            result = service.get_deal_folder_root_children("dms@example.com")

        get_root.assert_called_once_with(
            "drive-1",
            "root:/Documents/Dataroom/4. Deal Folder/4. All - 16062026:",
            "dms@example.com",
        )
        get_children.assert_called_once_with("drive-1", "all-root", "dms@example.com", top=999)
        self.assertEqual(result["value"][0]["driveId"], "drive-1")

    def test_strict_tree_traversal_rejects_partial_results(self):
        service = object.__new__(GraphAPIService)

        with (
            patch.object(service, "get_access_token", return_value="token"),
            patch.object(service, "_make_request", side_effect=RuntimeError("Graph timeout")),
            self.assertRaisesRegex(RuntimeError, "traversal was incomplete"),
        ):
            service.get_folder_tree(
                "drive-1",
                "folder-1",
                user_email="dms@example.com",
                max_depth=None,
                strict=True,
            )


class DealFolderScopeAPITests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(username="folders@example.com", password="test")
        self.client = APIClient()
        self.client.force_authenticate(user)

    @patch("microsoft.views.GraphAPIService.get_deal_folder_root_children")
    def test_deal_folder_scope_uses_dedicated_root(self, get_deal_folders):
        get_deal_folders.return_value = {
            "value": [{"id": "folder-1", "name": "Acme", "folder": {"childCount": 3}}]
        }

        response = self.client.get(reverse("onedrive-list"), {"scope": "deal_folders"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["items"][0]["name"], "Acme")
        get_deal_folders.assert_called_once_with(user_email="dms-demo@india-alt.com")

    def test_rejects_unknown_scope(self):
        response = self.client.get(reverse("onedrive-list"), {"scope": "unknown"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("scope", response.data["details"])

    @patch("microsoft.views.GraphAPIService.get_drive_item_download_url")
    def test_download_uses_item_drive_id(self, get_download_url):
        get_download_url.return_value = "https://download.example/file"

        response = self.client.get(
            reverse("onedrive-download"),
            {"item_id": "file-1", "drive_id": "shared-drive-1"},
        )

        self.assertEqual(response.status_code, 200)
        get_download_url.assert_called_once_with("shared-drive-1", "file-1")
