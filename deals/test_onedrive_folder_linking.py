from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from deals.models import Deal
from deals.management.commands.link_missing_onedrive_folders import Command as LinkFoldersCommand
from deals.services.onedrive_folder_matching import (
    normalize_folder_deal_name,
    score_folder_deal_name,
)


class OneDriveFolderMatchingTests(SimpleTestCase):
    def test_normalization_handles_prefixes_ampersands_and_legal_suffixes(self):
        self.assertEqual(
            normalize_folder_deal_name("Project A&B Pvt. Ltd."),
            "a and b",
        )

    def test_exact_and_safe_variant_matches_are_high_confidence(self):
        self.assertEqual(score_folder_deal_name("AB Coffee", "AB Coffee").score, 1.0)
        self.assertEqual(
            score_folder_deal_name("AB Coffee", "AB Coffee Private Limited").score,
            1.0,
        )
        self.assertEqual(
            score_folder_deal_name("AB Coffee", "AB Coffee India Opportunity").score,
            0.94,
        )

    def test_weak_partial_names_do_not_match(self):
        self.assertEqual(score_folder_deal_name("A", "Alpha").score, 0.0)
        self.assertEqual(score_folder_deal_name("Alpha", "Beta").score, 0.0)
        self.assertEqual(score_folder_deal_name("Aayush Hospitals", "NU Hospitals").score, 0.0)

    def test_tui_parser_supports_ranges_and_ambiguous_alternatives(self):
        self.assertEqual(
            LinkFoldersCommand._parse_tui_selections("1,3-5,7:2", 20),
            [(1, None), (3, None), (4, None), (5, None), (7, 2)],
        )
        self.assertIsNone(LinkFoldersCommand._parse_tui_selections("21", 20))
        self.assertIsNone(LinkFoldersCommand._parse_tui_selections("5-2", 20))


class LinkMissingOneDriveFoldersCommandTests(TestCase):
    @patch("deals.management.commands.link_missing_onedrive_folders.GraphAPIService")
    def test_dry_run_does_not_write_and_apply_links_only_unique_match(self, graph_class):
        missing = Deal.objects.create(title="AB Coffee")
        ambiguous_one = Deal.objects.create(title="Nova Health")
        ambiguous_two = Deal.objects.create(title="Nova Health India")
        already_linked = Deal.objects.create(title="Already Linked", source_onedrive_id="folder-old")
        graph_class.return_value.get_deal_folder_root_children.return_value = {
            "value": [
                {"id": "folder-ab", "name": "AB Coffee", "folder": {}, "driveId": "drive-1"},
                {"id": "folder-nova", "name": "Nova Health", "folder": {}, "driveId": "drive-1"},
                {"id": "folder-old", "name": "Already Linked", "folder": {}, "driveId": "drive-1"},
            ]
        }

        output = StringIO()
        call_command("link_missing_onedrive_folders", stdout=output)
        missing.refresh_from_db()
        self.assertIsNone(missing.source_onedrive_id)
        self.assertIn("DRY RUN", output.getvalue())
        self.assertIn("ready: 1", output.getvalue())

        call_command("link_missing_onedrive_folders", "--apply", stdout=StringIO())
        missing.refresh_from_db()
        ambiguous_one.refresh_from_db()
        already_linked.refresh_from_db()
        self.assertEqual(missing.source_onedrive_id, "folder-ab")
        self.assertIsNone(ambiguous_one.source_onedrive_id)
        self.assertEqual(already_linked.source_onedrive_id, "folder-old")

    @patch("deals.management.commands.link_missing_onedrive_folders.GraphAPIService")
    def test_interactive_mode_confirms_each_link(self, graph_class):
        accepted = Deal.objects.create(title="Accepted Deal")
        declined = Deal.objects.create(title="Declined Deal")
        graph_class.return_value.get_deal_folder_root_children.return_value = {
            "value": [
                {"id": "folder-accepted", "name": "Accepted Deal", "folder": {}, "driveId": "drive-1"},
                {"id": "folder-declined", "name": "Declined Deal", "folder": {}, "driveId": "drive-1"},
            ]
        }

        output = StringIO()
        with patch("builtins.input", side_effect=["y", "n"]):
            call_command("link_missing_onedrive_folders", "--interactive", stdout=output)

        accepted.refresh_from_db()
        declined.refresh_from_db()
        self.assertEqual(accepted.source_onedrive_id, "folder-accepted")
        self.assertIsNone(declined.source_onedrive_id)
        self.assertIn("INTERACTIVE", output.getvalue())

    @patch("deals.management.commands.link_missing_onedrive_folders.GraphAPIService")
    @patch("builtins.input", return_value="1")
    def test_smart_interactive_auto_links_certain_and_asks_for_ambiguous(self, _input, graph_class):
        certain = Deal.objects.create(title="Certain Deal")
        ambiguous_one = Deal.objects.create(title="Nova Health")
        ambiguous_two = Deal.objects.create(title="Nova Health India")
        graph_class.return_value.get_deal_folder_root_children.return_value = {
            "value": [
                {"id": "folder-certain", "name": "Certain Deal", "folder": {}, "driveId": "drive-1"},
                {"id": "folder-nova", "name": "Nova Health", "folder": {}, "driveId": "drive-1"},
            ]
        }

        call_command("link_missing_onedrive_folders", "--smart-interactive", stdout=StringIO())

        certain.refresh_from_db()
        ambiguous_one.refresh_from_db()
        ambiguous_two.refresh_from_db()
        self.assertEqual(certain.source_onedrive_id, "folder-certain")
        self.assertEqual(ambiguous_one.source_onedrive_id, "folder-nova")
        self.assertIsNone(ambiguous_two.source_onedrive_id)
        _input.assert_called_once()

    @patch("deals.management.commands.link_missing_onedrive_folders.GraphAPIService")
    def test_tui_applies_multiple_rows_from_one_page(self, graph_class):
        first = Deal.objects.create(title="First TUI Deal")
        second = Deal.objects.create(title="Second TUI Deal")
        graph_class.return_value.get_deal_folder_root_children.return_value = {
            "value": [
                {"id": "folder-first", "name": "First TUI Deal", "folder": {}, "driveId": "drive-1"},
                {"id": "folder-second", "name": "Second TUI Deal", "folder": {}, "driveId": "drive-1"},
            ]
        }

        with patch("builtins.input", return_value="1,2"):
            call_command("link_missing_onedrive_folders", "--tui", "--apply", stdout=StringIO())

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.source_onedrive_id, "folder-first")
        self.assertEqual(second.source_onedrive_id, "folder-second")
