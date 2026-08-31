#!/usr/bin/env python
"""
Master Hosted Database Sync Pipeline for India Alternatives:
- Fund 1: UPDATE ONLY (Refreshes all details for existing deals; NO new deals created)
- Fund 2: UPDATE ONLY (Refreshes all details for existing deals; NO new deals created)
- Fund 3 (V2): UPDATE & CREATE (Refreshes existing deals + Creates ONLY verified non-matching new deals)
"""
import os
import sys
import argparse
import unicodedata
import re
from datetime import datetime, date
from pathlib import Path
from copy import deepcopy
from collections import defaultdict
import openpyxl

import django
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
sys.path.insert(0, str(BASE_DIR))
django.setup()

from django.conf import settings
from django.db import connections, transaction
from deals.models import (
    Deal, DealStatus, DealPhase, FundClassificationState, FundClassificationSourceType,
    DealFieldProvenance,
)
from banks.models import Bank
from contacts.models import Contact
from accounts.models import Profile

DEFAULT_PROD_URL = os.environ.get(
    "PROD_DATABASE_URL",
    "postgresql://postgres:.9uAmOaNMwx2MuLqhC9ln5Xrw-fCwVM5@crossover.proxy.rlwy.net:42815/railway"
)

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
INTERNAL_EMAIL_DOMAINS = {"india-alt.com"}

# Verified domain aliases mapping Excel deal names to existing DB deal records
KNOWN_ALIASES = {
    # Fund 3 aliases
    "sleepy owl": "sleepyowl",
    "indigrid technology": "indigrid",
    "avyahcorp": "avyahcorp - venture catalysts",
    "shuchaye recyclers": "shuchaye",
    "suchaye recylers": "shuchaye",
    "project dealbridge": "dealbridge",
    "project quickroute": "quickroute",
    "trampoline / fableroom": "trampoline (fableroom)",
    "trampoline fableroom": "trampoline (fableroom)",
    "iris life solutions": "iris lifesciences (project poultry - protiviti)",
    "fishmongers": "fishmonger",
    "truboard cleantech": "truboard cleantech - mosiac cap",
    "justdogs": "just dogs",
    "amaris jewelry investment opportunity": "amaris",
    "property pistol": "propertypistol.com",
    "koparo clean": "koparo",
    "koparo business plan": "koparo",
    "bhive workspace": "bhive",
    "sonodyne technologies pvt ltd": "sonodyne - kricon",
    "ssipl lifestyle pvt ltd": "ssipl",
    "marudhar rocks international pvt ltd": "marudhar rocks",
    "rusk opportunity": "rusk media",
    "gromor finance": "gromor",
    "oorjaa": "orjaa",
    "project oak": "project oak aurum",
    "bloom": "project bloom - pushp masala",
    "eisen consultancies ltd.": "world avigation pvt - eisen consultancies",
    "project droid": "project android (auto component)",
    "project sterling": "project sterling",
    "project lithium": "project lithium - fair north",
    "toprankers": "toprankers",
}

STATUS_MAPPING = {
    "new": DealStatus.STAGE_1,
    "1: deal sourced": DealStatus.STAGE_1,
    "passed": DealStatus.PASSED,
    "to be passed": DealStatus.PASSED,
    "to be pass": DealStatus.PASSED,
    "low": DealStatus.STAGE_1,
    "invested": DealStatus.INVESTED,
    "portfolio": DealStatus.PORTFOLIO,
}

PHASE_MAPPING = {
    "new": DealPhase.STAGE_1,
    "1: deal sourced": DealPhase.STAGE_1,
    "passed": DealPhase.PASSED,
    "to be passed": DealPhase.PASSED,
    "to be pass": DealPhase.PASSED,
    "low": DealPhase.STAGE_1,
    "invested": DealPhase.INVESTED,
    "portfolio": DealPhase.PORTFOLIO,
}

def clean_text(val):
    if val is None:
        return ""
    text = str(val).strip()
    if text.lower() in ["nan", "none", "null", "n/a", "na", "-"]:
        return ""
    return text

def parse_date_safely(val):
    if not val:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    text = clean_text(val)
    if not text:
        return None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%Y", "%b-%y", "%B-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None

def parse_bool(val):
    if isinstance(val, bool):
        return val
    text = clean_text(val).lower()
    if text in ["true", "yes", "y", "1"]:
        return True
    if text in ["false", "no", "n", "0", ""]:
        return False
    return False

def extract_contacts(val):
    text = clean_text(val)
    if not text:
        return []
    emails = []
    for email in EMAIL_RE.findall(text):
        norm = email.strip().lower()
        if norm.rsplit("@", 1)[-1] in INTERNAL_EMAIL_DOMAINS:
            continue
        if norm not in emails:
            emails.append(norm)
    return emails

def split_team_initials(val):
    text = clean_text(val)
    if not text:
        return []
    return [item.strip().upper() for item in text.split(",") if item.strip()]

def normalize_key(title):
    t = unicodedata.normalize("NFKD", clean_text(title)).casefold()
    t = re.sub(r'\(.*?\)', '', t)
    t = re.sub(r'\b(pvt|ltd|limited|private|technologies|technology|tech|solutions|india|corp|corporation|services|app|inc|llp|group|investment|opportunity)\b', '', t)
    t = re.sub(r'[^a-z0-9]+', '', t)
    return t

def setup_db(db_alias="production", prod_url=None):
    url = prod_url or DEFAULT_PROD_URL
    source_cfg = settings.DATABASES["default"]
    parsed = dj_database_url.parse(url, conn_max_age=600, ssl_require=False)
    target_config = deepcopy(source_cfg)
    target_config.update(parsed)
    target_config.setdefault("OPTIONS", {})

    settings.DATABASES[db_alias] = target_config
    connections.databases[db_alias] = target_config
    connections[db_alias].close()
    return db_alias


SHEET_DEAL_FIELDS = {
    "excel_sr_no": "excel_sr_no",
    "excel_days_since": "excel_days_since",
    "deal_name": "title",
    "deal_status": "deal_status",
    "current_phase": "current_phase",
    "received_at": "received_at",
    "funding_ask": "funding_ask",
    "source": "bank_name",
    "team_initials": "deal_team",
    "industry": "industry",
    "sector": "sector",
    "is_female_led": "is_female_led",
    "management_meeting": "management_meeting",
    "business_proposal_stage": "business_proposal_stage",
    "ic_stage": "ic_stage",
    "city": "city",
    "comments": "comments",
    "contact_emails": "contact_emails",
    "reasons_for_passing": "reasons_for_passing",
    "rejection_reason": "rejection_reason",
    "deal_summary": "deal_summary",
    "deal_details": "deal_details",
    "company_details": "company_details",
    "fund": "fund",
}


def record_sheet_provenance(deal, payload, previous_values, db_alias):
    """Record an idempotent field-level workbook provenance snapshot for a deal row."""
    source_id = f"{payload['file_name']}:row:{payload['row_idx']}"
    fields = []
    for payload_key, field_name in SHEET_DEAL_FIELDS.items():
        value = payload.get(payload_key)
        if payload_key == "rejection_reason":
            value = payload.get("reasons_for_passing")
        # Preserve explicit False/zero values, but omit genuinely blank workbook cells.
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, (date, datetime)):
            value = value.isoformat()
        previous = previous_values.get(field_name)
        if isinstance(previous, (date, datetime)):
            previous = previous.isoformat()
        fields.append(DealFieldProvenance(
            deal=deal,
            field_name=field_name,
            source_type="SHEET",
            source_id=source_id,
            previous_value=previous,
            value=deepcopy(value),
        ))
    DealFieldProvenance.objects.using(db_alias).filter(
        deal=deal, source_type="SHEET", source_id=source_id
    ).delete()
    if fields:
        DealFieldProvenance.objects.using(db_alias).bulk_create(fields, batch_size=100)
    return len(fields)

def sync_fund_file(excel_path, fund_name, create_new=False, db_alias="production", apply_changes=False):
    print("=" * 85)
    action_type = "UPDATE & CREATE NEW" if create_new else "UPDATE EXISTING ONLY (NO NEW CREATIONS)"
    mode_str = "APPLYING TO HOSTED DB" if apply_changes else "DRY-RUN PREVIEW (NO CHANGES COMMIT)"
    print(f">>> {fund_name.upper()} | {action_type} [{mode_str}]")
    print(f"File: {excel_path}")
    print("=" * 85)

    if not os.path.exists(excel_path):
        print(f"ERROR: Excel file not found at {excel_path}")
        return

    wb = openpyxl.load_workbook(excel_path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))

    db_deals = list(Deal.objects.using(db_alias).all())
    
    # Profiles for Deal Team
    profiles_by_initial = {}
    for p in Profile.objects.using(db_alias).all():
        if p.initials:
            profiles_by_initial[p.initials.strip().upper()] = p
        if p.name:
            profiles_by_initial[p.name.strip().upper()] = p

    # Contacts
    contacts_by_email = {}
    for c in Contact.objects.using(db_alias).filter(email__isnull=False):
        if c.email:
            contacts_by_email[c.email.strip().lower()] = c

    # Banks
    banks_by_name = {}
    for b in Bank.objects.using(db_alias).all():
        if b.name:
            banks_by_name[b.name.strip().lower()] = b

    # Index deals for multi-strategy matching
    deals_by_exact_lower = {}
    deals_by_clean_prefix = {}
    deals_by_normalized = defaultdict(list)

    for d in db_deals:
        raw_title = d.title or ""
        t_lower = raw_title.strip().lower()
        deals_by_exact_lower[t_lower] = d
        
        prefix = re.split(r'\s+[-–—|]\s+', t_lower)[0].strip()
        if prefix:
            deals_by_clean_prefix[prefix] = d
            
        norm = normalize_key(raw_title)
        if norm:
            deals_by_normalized[norm].append(d)

    to_update = []
    to_create = []
    skipped_count = 0

    for row_idx, r in enumerate(rows[1:], start=2):
        if not any(r):
            continue

        raw_status = clean_text(r[1])
        raw_date = r[2]
        excel_sr_no = r[0]
        excel_days_since = r[3]
        deal_name = clean_text(r[4])
        source = clean_text(r[5])
        funding_ask = clean_text(r[6])
        team_initials = split_team_initials(r[7])
        industry = clean_text(r[8])
        sector = clean_text(r[9])
        is_female_led = parse_bool(r[10])
        management_meeting = parse_bool(r[11])
        business_proposal_stage = parse_bool(r[12])
        ic_stage = parse_bool(r[13])
        next_steps = clean_text(r[14])
        city = clean_text(r[15])
        contact_emails = extract_contacts(r[16])
        reasons_for_passing = clean_text(r[17])
        summary = clean_text(r[18])
        details = clean_text(r[19])
        company_info = clean_text(r[20])
        fund_label = clean_text(r[21]) or fund_name.upper()

        if not deal_name:
            continue

        deal_status = STATUS_MAPPING.get(raw_status.lower(), DealStatus.STAGE_1)
        current_phase = PHASE_MAPPING.get(raw_status.lower(), DealPhase.STAGE_1)
        received_at = parse_date_safely(raw_date)

        matched_deal = None
        match_reason = ""

        # 1. Exact lowercase
        name_lower = deal_name.lower()
        if name_lower in deals_by_exact_lower:
            matched_deal = deals_by_exact_lower[name_lower]
            match_reason = "Exact Title"
        
        # 2. Known Alias
        elif name_lower in KNOWN_ALIASES:
            tgt = KNOWN_ALIASES[name_lower]
            matched_deal = deals_by_exact_lower.get(tgt) or deals_by_clean_prefix.get(tgt)
            match_reason = f"Known Alias -> '{tgt}'"

        # 3. Clean Prefix
        if not matched_deal:
            prefix = re.split(r'\s+[-–—|]\s+', name_lower)[0].strip()
            if prefix in deals_by_clean_prefix:
                matched_deal = deals_by_clean_prefix[prefix]
                match_reason = f"Prefix Match ('{prefix}')"

        # 4. Normalized Key
        if not matched_deal:
            norm = normalize_key(deal_name)
            candidates = deals_by_normalized.get(norm, [])
            if len(candidates) == 1:
                matched_deal = candidates[0]
                match_reason = "Normalized Key"
            elif len(candidates) > 1:
                f_match = [c for c in candidates if c.fund == fund_label]
                matched_deal = f_match[0] if f_match else candidates[0]
                match_reason = f"Normalized Key ({len(candidates)} candidates, matched {matched_deal.fund})"

        row_payload = {
            "row_idx": row_idx,
            "excel_sr_no": excel_sr_no,
            "excel_days_since": excel_days_since,
            "deal_name": deal_name,
            "fund": fund_label,
            "deal_status": deal_status,
            "current_phase": current_phase,
            "received_at": received_at,
            "funding_ask": funding_ask,
            "source": source,
            "team_initials": team_initials,
            "industry": industry,
            "sector": sector,
            "is_female_led": is_female_led,
            "management_meeting": management_meeting,
            "business_proposal_stage": business_proposal_stage,
            "ic_stage": ic_stage,
            "city": city,
            "comments": next_steps,
            "contact_emails": contact_emails,
            "reasons_for_passing": reasons_for_passing,
            "deal_summary": summary,
            "deal_details": details,
            "company_details": company_info,
            "file_name": Path(excel_path).name,
        }

        if matched_deal:
            to_update.append({
                "deal": matched_deal,
                "payload": row_payload,
                "match_reason": match_reason
            })
        elif create_new:
            to_create.append(row_payload)
        else:
            skipped_count += 1

    print(f"Total Rows Processed:             {len(to_update) + len(to_create) + skipped_count}")
    print(f"Existing Deals to UPDATE:          {len(to_update)}")
    if create_new:
        print(f"Brand New Deals to CREATE:         {len(to_create)}")
    else:
        print(f"Unmatched Deals (SKIPPED):         {skipped_count} (create_new is DISABLED for {fund_name})")
    
    print(f"Deals with Reasons for Passing:   {sum(1 for x in to_update + [{'payload': p} for p in to_create] if x['payload']['reasons_for_passing'])}")
    print(f"Deals with Date of Receipt:       {sum(1 for x in to_update + [{'payload': p} for p in to_create] if x['payload']['received_at'])}")
    print(f"Deals with Source/Bank:           {sum(1 for x in to_update + [{'payload': p} for p in to_create] if x['payload']['source'])}")
    print(f"Deals with Team Initials:         {sum(1 for x in to_update + [{'payload': p} for p in to_create] if x['payload']['team_initials'])}")

    if apply_changes:
        print(f"\n>>> COMMITTING UPDATES/CREATIONS FOR {fund_name.upper()} TO RAILWAY HOSTED DB...")
        with transaction.atomic(using=db_alias):
            up_count = 0
            cr_count = 0
            contact_links = []
            responsibility_links = []
            relationship_deal_ids = set()

            # Update existing
            for item in to_update:
                d = item["deal"]
                p = item["payload"]
                relationship_deal_ids.add(d.id)
                previous_values = {
                    field_name: getattr(d, field_name, None)
                    for field_name in set(SHEET_DEAL_FIELDS.values())
                    if hasattr(d, field_name)
                }

                bank = None
                if p["source"]:
                    b_key = p["source"].strip().lower()
                    bank = banks_by_name.get(b_key)
                    if not bank:
                        bank, _ = Bank.objects.using(db_alias).get_or_create(
                            name=p["source"],
                            defaults={"description": f"Imported from {p['file_name']}"}
                        )
                        banks_by_name[b_key] = bank
                    d.bank = bank
                    d.bank_name = p["source"]
                    d.legacy_investment_bank = p["source"]

                if p["deal_status"]:
                    d.deal_status = p["deal_status"]
                if p["current_phase"]:
                    d.current_phase = p["current_phase"]
                if p["received_at"]:
                    d.received_at = p["received_at"]
                if p["funding_ask"]:
                    d.funding_ask = p["funding_ask"]
                if p["industry"]:
                    d.industry = p["industry"]
                if p["sector"]:
                    d.sector = p["sector"]
                if p["is_female_led"] is not None:
                    d.is_female_led = p["is_female_led"]
                if p["management_meeting"] is not None:
                    d.management_meeting = p["management_meeting"]
                if p["business_proposal_stage"] is not None:
                    d.business_proposal_stage = p["business_proposal_stage"]
                if p["ic_stage"] is not None:
                    d.ic_stage = p["ic_stage"]
                if p["city"]:
                    d.city = p["city"]
                if p["comments"]:
                    d.comments = p["comments"]
                if p["reasons_for_passing"]:
                    d.reasons_for_passing = p["reasons_for_passing"]
                    d.rejection_reason = p["reasons_for_passing"]
                if p["deal_summary"]:
                    d.deal_summary = p["deal_summary"]
                if p["deal_details"]:
                    d.deal_details = p["deal_details"]
                if p["company_details"]:
                    d.company_details = p["company_details"]

                d.fund_classification_state = FundClassificationState.EXPLICIT
                d.fund_classification_source_type = FundClassificationSourceType.WORKBOOK
                d.fund_classification_source_id = f"{p['file_name']}:row:{p['row_idx']}"

                d.save(using=db_alias)
                record_sheet_provenance(d, p, previous_values, db_alias)

                first_contact = None
                for email in p["contact_emails"]:
                    c = contacts_by_email.get(email)
                    if not c:
                        c, _ = Contact.objects.using(db_alias).get_or_create(
                            email=email,
                            defaults={"name": email.split("@")[0].replace(".", " ").title(), "bank": bank}
                        )
                        contacts_by_email[email] = c
                    elif bank and not c.bank:
                        c.bank = bank
                        c.save(using=db_alias, update_fields=['bank'])

                    contact_links.append((d.id, c.id))
                    relationship_deal_ids.add(d.id)
                    if not first_contact:
                        first_contact = c

                if first_contact and not d.primary_contact:
                    d.primary_contact = first_contact
                    d.save(using=db_alias, update_fields=['primary_contact'])

                for init in p["team_initials"]:
                    prof = profiles_by_initial.get(init)
                    if prof:
                        responsibility_links.append((d.id, prof.id))
                        relationship_deal_ids.add(d.id)

                up_count += 1

            # Create new (Only for Fund 3 when create_new=True)
            for p in to_create:
                bank = None
                if p["source"]:
                    b_key = p["source"].strip().lower()
                    bank = banks_by_name.get(b_key)
                    if not bank:
                        bank, _ = Bank.objects.using(db_alias).get_or_create(
                            name=p["source"],
                            defaults={"description": f"Imported from {p['file_name']}"}
                        )
                        banks_by_name[b_key] = bank

                new_deal = Deal.objects.using(db_alias).create(
                    title=p["deal_name"],
                    fund=p["fund"],
                    deal_status=p["deal_status"],
                    current_phase=p["current_phase"],
                    received_at=p["received_at"],
                    funding_ask=p["funding_ask"],
                    bank=bank,
                    bank_name=p["source"],
                    legacy_investment_bank=p["source"],
                    industry=p["industry"],
                    sector=p["sector"],
                    is_female_led=p["is_female_led"],
                    management_meeting=p["management_meeting"],
                    business_proposal_stage=p["business_proposal_stage"],
                    ic_stage=p["ic_stage"],
                    city=p["city"],
                    comments=p["comments"],
                    reasons_for_passing=p["reasons_for_passing"],
                    rejection_reason=p["reasons_for_passing"],
                    deal_summary=p["deal_summary"],
                    deal_details=p["deal_details"],
                    company_details=p["company_details"],
                    fund_classification_state=FundClassificationState.EXPLICIT,
                    fund_classification_source_type=FundClassificationSourceType.WORKBOOK,
                    fund_classification_source_id=f"{p['file_name']}:row:{p['row_idx']}",
                )
                relationship_deal_ids.add(new_deal.id)
                record_sheet_provenance(new_deal, p, {}, db_alias)

                first_contact = None
                for email in p["contact_emails"]:
                    c = contacts_by_email.get(email)
                    if not c:
                        c, _ = Contact.objects.using(db_alias).get_or_create(
                            email=email,
                            defaults={"name": email.split("@")[0].replace(".", " ").title(), "bank": bank}
                        )
                        contacts_by_email[email] = c
                    elif bank and not c.bank:
                        c.bank = bank
                        c.save(using=db_alias, update_fields=['bank'])

                    contact_links.append((new_deal.id, c.id))
                    relationship_deal_ids.add(new_deal.id)
                    if not first_contact:
                        first_contact = c

                if first_contact:
                    new_deal.primary_contact = first_contact
                    new_deal.save(using=db_alias, update_fields=['primary_contact'])

                for init in p["team_initials"]:
                    prof = profiles_by_initial.get(init)
                    if prof:
                        responsibility_links.append((new_deal.id, prof.id))
                        relationship_deal_ids.add(new_deal.id)

                cr_count += 1

            # Replace workbook-managed many-to-many links in two bulk statements
            # instead of issuing one network round-trip per row/profile.
            contact_through = Deal.additional_contacts.through
            responsibility_through = Deal.responsibility.through
            if relationship_deal_ids:
                contact_through.objects.using(db_alias).filter(deal_id__in=relationship_deal_ids).delete()
                responsibility_through.objects.using(db_alias).filter(deal_id__in=relationship_deal_ids).delete()
            if contact_links:
                contact_through.objects.using(db_alias).bulk_create(
                    [contact_through(deal_id=deal_id, contact_id=contact_id) for deal_id, contact_id in set(contact_links)],
                    ignore_conflicts=True,
                    batch_size=500,
                )
            if responsibility_links:
                responsibility_through.objects.using(db_alias).bulk_create(
                    [responsibility_through(deal_id=deal_id, profile_id=profile_id) for deal_id, profile_id in set(responsibility_links)],
                    ignore_conflicts=True,
                    batch_size=500,
                )

        print(f"✓ SUCCESS! {fund_name.upper()} committed: {up_count} deals updated, {cr_count} new deals created.\n")
    else:
        print(f"[NOTE] Dry-run preview completed for {fund_name.upper()}. No changes persisted.\n")

def main():
    parser = argparse.ArgumentParser(description="Master Sync Pipeline for Funds I, II, III to Railway Hosted Postgres DB.")
    parser.add_argument("--apply", action="store_true", help="Persist updates and new deals to hosted DB.")
    parser.add_argument(
        "--create-missing-all-funds",
        action="store_true",
        help="Create unmatched workbook deals for Funds 1, 2, and 3 (default only creates Fund 3).",
    )
    parser.add_argument("--prod-database-url", default=None, help="Custom Postgres connection URL for hosted DB.")
    args = parser.parse_args()

    db_alias = setup_db("production", args.prod_database_url)

    f1_path = BASE_DIR / "data" / "legacy_dms_files" / "1. Fund I.xlsx"
    f2_path = BASE_DIR / "data" / "legacy_dms_files" / "2. Fund II.xlsx"
    f3_path = BASE_DIR / "data" / "legacy_dms_files" / "3.2 Fund III - V2.xlsx"

    print("\n" + "#" * 85)
    print("STARTING MASTER SYNC PIPELINE ON RAILWAY HOSTED POSTGRES DATABASE")
    print("Mode: " + ("APPLY (LIVE WRITE)" if args.apply else "DRY-RUN (SIMULATION ONLY)"))
    print("#" * 85 + "\n")

    create_missing = args.create_missing_all_funds

    # Fund 1
    sync_fund_file(f1_path, fund_name="FUND1", create_new=create_missing, db_alias=db_alias, apply_changes=args.apply)

    # Fund 2
    sync_fund_file(f2_path, fund_name="FUND2", create_new=create_missing, db_alias=db_alias, apply_changes=args.apply)

    # Fund 3: UPDATE & CREATE NEW
    sync_fund_file(f3_path, fund_name="FUND3", create_new=True, db_alias=db_alias, apply_changes=args.apply)

    print("#" * 85)
    print("MASTER SYNC PIPELINE COMPLETED")
    print("#" * 85)

if __name__ == "__main__":
    main()
