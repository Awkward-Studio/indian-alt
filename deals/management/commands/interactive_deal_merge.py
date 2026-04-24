from __future__ import annotations

import os
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from deals.models import Deal
from deals.services.deal_merge import (
    SCALAR_FIELDS,
    MERGE_TEXT_FIELDS,
    move_related_objects,
    merge_text,
    merge_list,
)

class Command(BaseCommand):
    help = "Interactively merge two Deal records, allowing field-by-field value selection."

    def add_arguments(self, parser):
        parser.add_argument("canonical_id", help="UUID of the canonical deal (the one that stays)")
        parser.add_argument("duplicate_id", help="UUID of the duplicate deal (the one that will be merged and deleted)")

    def handle(self, *args, **options):
        canonical_id = options["canonical_id"]
        duplicate_id = options["duplicate_id"]

        try:
            canonical = Deal.objects.get(id=canonical_id)
        except Deal.DoesNotExist:
            raise CommandError(f"Canonical deal with ID {canonical_id} not found.")

        try:
            duplicate = Deal.objects.get(id=duplicate_id)
        except Deal.DoesNotExist:
            raise CommandError(f"Duplicate deal with ID {duplicate_id} not found.")

        if canonical.id == duplicate.id:
            raise CommandError("Cannot merge a deal into itself.")

        self.stdout.write(self.style.MIGRATE_HEADING(f"Merging Deal: {duplicate.title} -> {canonical.title}"))
        self.stdout.write(f"Canonical: {canonical.id}")
        self.stdout.write(f"Duplicate: {duplicate.id}\n")

        changed_fields = []

        # Handle Scalar Fields
        comparison_fields = ["title"] + SCALAR_FIELDS + ["themes", "other_contacts", "deal_flow_decisions"]
        for field in comparison_fields:
            val1 = getattr(canonical, field)
            val2 = getattr(duplicate, field)

            if val1 == val2:
                continue

            self.stdout.write(self.style.SUCCESS(f"\nField: {field}"))
            self.stdout.write(f"  [1] Canonical: {val1}")
            self.stdout.write(f"  [2] Duplicate: {val2}")
            
            options_text = "[1] Keep Canonical, [2] Use Duplicate"
            can_merge = field in MERGE_TEXT_FIELDS or field in ["themes", "other_contacts"]
            if can_merge:
                options_text += ", [M] Merge Both"
            options_text += ", [C] Custom Value, [S] Skip/Keep Canonical"

            while True:
                choice = input(f"Select choice ({options_text}): ").strip().lower()
                if choice in ("1", "s", ""):
                    break
                elif choice == "2":
                    setattr(canonical, field, val2)
                    changed_fields.append(field)
                    break
                elif choice == "m" and can_merge:
                    if field in MERGE_TEXT_FIELDS:
                        setattr(canonical, field, merge_text(val1, val2))
                    else:
                        setattr(canonical, field, merge_list(val1, val2))
                    changed_fields.append(field)
                    break
                elif choice == "c":
                    custom_val = input(f"Enter custom value for {field}: ").strip()
                    setattr(canonical, field, custom_val)
                    changed_fields.append(field)
                    break
                else:
                    self.stdout.write(self.style.ERROR("Invalid choice."))

        # Handle Booleans
        bool_fields = ["is_indexed", "is_female_led", "management_meeting", "business_proposal_stage", "ic_stage"]
        for field in bool_fields:
            val1 = getattr(canonical, field)
            val2 = getattr(duplicate, field)
            if val1 == val2:
                continue

            self.stdout.write(self.style.SUCCESS(f"\nField: {field}"))
            self.stdout.write(f"  [1] Canonical: {val1}")
            self.stdout.write(f"  [2] Duplicate: {val2}")
            
            while True:
                choice = input("Select choice ([1] Canonical, [2] Duplicate, [O] OR/True if either is true): ").strip().lower()
                if choice == "1":
                    break
                elif choice == "2":
                    setattr(canonical, field, val2)
                    changed_fields.append(field)
                    break
                elif choice == "o":
                    setattr(canonical, field, val1 or val2)
                    changed_fields.append(field)
                    break
                else:
                    self.stdout.write(self.style.ERROR("Invalid choice."))

        if not changed_fields:
            self.stdout.write("\nNo scalar fields changed.")
        else:
            self.stdout.write(f"\nModified fields: {', '.join(changed_fields)}")

        confirm = input("\nProceed with merge? This will move all documents, analyses and DELETE the duplicate deal. (y/N): ").strip().lower()
        if confirm != 'y':
            self.stdout.write(self.style.WARNING("Merge cancelled."))
            return

        with transaction.atomic():
            if changed_fields:
                canonical.save(update_fields=list(dict.fromkeys(changed_fields)))
            
            self.stdout.write("Moving related objects and deleting duplicate...")
            move_related_objects(canonical, duplicate)
            
        self.stdout.write(self.style.SUCCESS(f"Successfully merged {duplicate_id} into {canonical_id}."))
