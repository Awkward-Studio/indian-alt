from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from deals.models import Deal, DealDocument


class DuplicateDealPruningTests(TestCase):
    def test_dry_run_is_safe_and_apply_merges_unlinked_duplicates_into_linked_deal(self):
        canonical = Deal.objects.create(
            title="Rentomojo",
            source_onedrive_id="folder-rentomojo",
            source_drive_id="drive-1",
        )
        duplicate = Deal.objects.create(title="Project Rentomojo")
        DealDocument.objects.create(deal=duplicate, title="Duplicate memo")

        output = StringIO()
        call_command("prune_duplicate_deals", stdout=output)
        self.assertTrue(Deal.objects.filter(pk=duplicate.pk).exists())
        self.assertIn("DRY RUN", output.getvalue())

        call_command("prune_duplicate_deals", "--apply", stdout=StringIO())
        self.assertFalse(Deal.objects.filter(pk=duplicate.pk).exists())
        self.assertTrue(DealDocument.objects.filter(deal=canonical, title="Duplicate memo").exists())

    def test_multiple_linked_duplicates_are_not_proposed(self):
        Deal.objects.create(title="Project Amber", source_onedrive_id="folder-1")
        Deal.objects.create(title="Amber", source_onedrive_id="folder-2")
        Deal.objects.create(title="Amber")

        output = StringIO()
        call_command("prune_duplicate_deals", stdout=output)

        self.assertIn("Found 0 safe duplicate group(s)", output.getvalue())
