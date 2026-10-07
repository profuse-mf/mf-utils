"""Sync Turant Loan lead status into lead_master (+ mf_disbursals / status logs).

Status API (Turant_Loan___Lead_Status_API.pdf):
  POST {TURANT_BASE_URL}/partner/check-lead-status
  Headers: Content-Type: application/json, X-Api-Key
  Body: { partner_id, phone, pan }

Looks up Turant leads (lender_id=24) via mf_users.mobile + pan.
Maps data.lead_status → disburse_status; loan_amt_approved / disbursal_date
when present (Disbursed / Closed / Part Payment / Settlement).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

import pymysql

from config import (
    TURANT_API_KEY,
    TURANT_BASE_URL,
    TURANT_LENDER_ID,
    TURANT_PARTNER_ID,
    TURANT_STATUS_API_URL,
    db_config,
)
from mf_disbursals_store import persist_lender_status_poll
from mf_user_crypto import sql_aes_decrypt

MYSQL_CONFIG = db_config()
STALE_DAYS = 30
REQUEST_DELAY_SECONDS = 1

SKIP_DISBURSE_STATUSES = (
    "disbursed",
    "rejected",
)

LEADS_QUERY_TEMPLATE = """
SELECT
    lm.id,
    lm.user_id,
    lm.application_id,
    lm.lender_id,
    lm.lender_ref_id,
    {mobile_col},
    {pan_col}
FROM lead_master AS lm
JOIN mf_users AS u ON u.id = lm.user_id
WHERE lm.lender_id = %s
  AND lm.status = 1
  {date_filter}
  AND LOWER(TRIM(IFNULL(lm.disburse_status, ''))) NOT IN ({skip_placeholders})
ORDER BY lm.id
"""


def require_config():
    missing = []
    if not TURANT_STATUS_API_URL:
        missing.append("TURANT_STATUS_API_URL / TURANT_BASE_URL")
    if not TURANT_API_KEY:
        missing.append("TURANT_API_KEY")
    if not TURANT_PARTNER_ID:
        missing.append("TURANT_PARTNER_ID")
    if missing:
        raise RuntimeError(
            "Turant Loan config missing. Set in .env: " + ", ".join(missing)
        )


def normalize_value(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == "NA":
        return None
    return text


def normalize_phone(mobile):
    digits = re.sub(r"\D", "", str(mobile or ""))
    last10 = digits[-10:]
    if not re.fullmatch(r"[6-9]\d{9}", last10):
        return None
    return last10


def normalize_pan(pan):
    text = str(pan or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", text):
        return None
    return text


def fetch_leads(include_all=False):
    date_filter = (
        "" if include_all else "AND lm.created >= NOW() - INTERVAL %s DAY"
    )
    query = LEADS_QUERY_TEMPLATE.format(
        mobile_col=sql_aes_decrypt("u.mobile", "mobile"),
        pan_col=sql_aes_decrypt("u.pan", "pan"),
        skip_placeholders=", ".join(["%s"] * len(SKIP_DISBURSE_STATUSES)),
        date_filter=date_filter,
    )
    if include_all:
        params = (TURANT_LENDER_ID, *SKIP_DISBURSE_STATUSES)
    else:
        params = (TURANT_LENDER_ID, STALE_DAYS, *SKIP_DISBURSE_STATUSES)
    conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(query, params)
            return cursor.fetchall()
    finally:
        conn.close()


def fetch_turant_status(phone, pan):
    payload = {
        "partner_id": TURANT_PARTNER_ID,
        "phone": phone,
        "pan": pan,
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Api-Key": TURANT_API_KEY,
    }

    print("  Request:")
    print("    POST", TURANT_STATUS_API_URL)
    print(f"    body: {json.dumps({**payload, 'pan': pan[:5] + '****' + pan[-1:]})}")
    print("    X-Api-Key: ***")

    request = urllib.request.Request(
        TURANT_STATUS_API_URL,
        data=body,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            http_status = response.getcode()
            raw_body = response.read().decode("utf-8")
            print(f"  Response (raw) HTTP {http_status}:")
            print(f"    {raw_body}")
            return json.loads(raw_body) if raw_body else {}
    except urllib.error.HTTPError as exc:
        raw_body = exc.read().decode("utf-8", errors="replace")
        print(f"  Response (raw) HTTP {exc.code}:")
        print(f"    {raw_body}")
        try:
            payload_json = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            payload_json = None
        # Soft business outcomes sometimes arrive as non-2xx with JSON body.
        if isinstance(payload_json, dict) and (
            payload_json.get("data") is not None
            or payload_json.get("success") is False
            or exc.code in (400, 404, 412)
        ):
            return payload_json
        raise RuntimeError(
            f"Turant API error {exc.code} for phone={phone}: {raw_body}"
        ) from exc


def extract_status_payload(response_body):
    if not isinstance(response_body, dict):
        return None

    data = response_body.get("data")
    if isinstance(data, dict) and data.get("lead_status"):
        return data

    # Some errors put lead_status-like codes at top level / message.
    lead_status = normalize_value(response_body.get("lead_status"))
    if lead_status:
        return {"lead_status": lead_status}

    return None


def map_disburse_fields(item):
    disburse_status = normalize_value(item.get("lead_status"))
    disburse_amount = normalize_value(
        item.get("loan_amt_approved")
        or item.get("loan_amount_approved")
        or item.get("loanAmtApproved")
    )
    disburse_datetime = normalize_value(
        item.get("disbursal_date")
        or item.get("disbursement_date")
        or item.get("disbursed_date")
    )
    return disburse_status, disburse_amount, disburse_datetime


def persist_poll(
    lead_id,
    *,
    response_json,
    disburse_status=None,
    disburse_amount=None,
    disburse_datetime=None,
    user_id=None,
    application_id=None,
    lender_id=None,
    apply_to_lead_and_disbursals=True,
):
    conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        return persist_lender_status_poll(
            conn,
            lead_id=lead_id,
            response_json=response_json,
            disburse_status=disburse_status,
            disburse_amount=disburse_amount,
            disburse_datetime=disburse_datetime,
            user_id=user_id,
            application_id=application_id,
            lender_id=lender_id if lender_id is not None else TURANT_LENDER_ID,
            apply_to_lead_and_disbursals=apply_to_lead_and_disbursals,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def process_turant_statuses(include_all=False):
    require_config()
    leads = fetch_leads(include_all=include_all)

    print(f"Turant status URL: {TURANT_STATUS_API_URL}")
    print(f"Turant base URL: {TURANT_BASE_URL}")
    print(f"lender_id={TURANT_LENDER_ID}, partner_id={TURANT_PARTNER_ID}")
    window = "all time" if include_all else f"last {STALE_DAYS} days"
    print(f"Found {len(leads)} lead(s) ({window})")

    leads_by_key = defaultdict(list)
    skipped_missing = 0
    for lead in leads:
        phone = normalize_phone(lead.get("mobile"))
        pan = normalize_pan(lead.get("pan"))
        if not phone or not pan:
            skipped_missing += 1
            print(
                f"Skipped lead_id={lead['id']}: missing/invalid phone or pan "
                f"(phone={phone!r}, pan={'set' if pan else None})"
            )
            continue
        leads_by_key[(phone, pan)].append(lead)

    keys = sorted(leads_by_key)
    print(f"Unique phone+pan pairs to query: {len(keys)}")

    updated_count = 0
    logged_only_count = 0
    skipped_count = skipped_missing
    failed_count = 0
    disbursals_count = 0

    for phone, pan in keys:
        key_leads = leads_by_key[(phone, pan)]
        print(f"Processing phone={phone} pan={pan[:5]}**** ({len(key_leads)} lead(s))")

        try:
            response_body = fetch_turant_status(phone, pan)
            item = extract_status_payload(response_body) or {}
            disburse_status, disburse_amount, disburse_datetime = map_disburse_fields(
                item
            )
            # Any JSON response for these leads is persisted. Soft statuses
            # (journey_not_started, not_found, …) still write logs + mf_disbursals.
            apply_full = isinstance(response_body, dict)
            if not disburse_status and isinstance(response_body, dict):
                disburse_status = normalize_value(
                    response_body.get("message") or response_body.get("status")
                )

            for lead in key_leads:
                lead_id = lead["id"]
                try:
                    result = persist_poll(
                        lead_id,
                        response_json=response_body,
                        disburse_status=disburse_status,
                        disburse_amount=disburse_amount,
                        disburse_datetime=disburse_datetime,
                        user_id=lead.get("user_id"),
                        application_id=lead.get("application_id"),
                        lender_id=lead.get("lender_id") or TURANT_LENDER_ID,
                        apply_to_lead_and_disbursals=apply_full,
                    )
                    if apply_full:
                        updated_count += 1
                        print(
                            f"  Persisted lead_id={lead_id}: "
                            f"disburse_status={disburse_status}, "
                            f"disburse_amount={disburse_amount}, "
                            f"disburse_datetime={disburse_datetime}, "
                            f"status_log_id={result.get('status_log_id')}"
                        )
                    else:
                        logged_only_count += 1
                        print(
                            f"  Logged (no apply) lead_id={lead_id}: "
                            f"status_log_id={result.get('status_log_id')}"
                        )
                    if result.get("disbursal"):
                        disbursals_count += 1
                        print(
                            f"  mf_disbursals {result['disbursal']}: "
                            f"application_id={result.get('application_id')}, "
                            f"lender_id={result.get('lender_id')}"
                        )
                except Exception as exc:
                    failed_count += 1
                    print(f"  Failed lead_id={lead_id}: {exc}", file=sys.stderr)
        except Exception as exc:
            # API hard-failure: still log the error payload for every lead.
            error_payload = {"error": str(exc), "phone": phone}
            for lead in key_leads:
                lead_id = lead["id"]
                try:
                    result = persist_poll(
                        lead_id,
                        response_json=error_payload,
                        disburse_status=None,
                        user_id=lead.get("user_id"),
                        application_id=lead.get("application_id"),
                        lender_id=lead.get("lender_id") or TURANT_LENDER_ID,
                        apply_to_lead_and_disbursals=False,
                    )
                    logged_only_count += 1
                    print(
                        f"  Logged error for lead_id={lead_id}: "
                        f"status_log_id={result.get('status_log_id')}"
                    )
                except Exception as log_exc:
                    failed_count += 1
                    print(
                        f"  Failed logging lead_id={lead_id}: {log_exc}",
                        file=sys.stderr,
                    )
            print(f"  Failed phone={phone}: {exc}", file=sys.stderr)

        time.sleep(REQUEST_DELAY_SECONDS)

    print()
    print(
        f"Done. Updated={updated_count}, LoggedOnly={logged_only_count}, "
        f"SkippedMissingKeys={skipped_count}, Failed={failed_count}, "
        f"mf_disbursals={disbursals_count}"
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Sync Turant Loan lead statuses")
    parser.add_argument(
        "--all",
        dest="include_all",
        action="store_true",
        help=f"Process all matching leads (ignore {STALE_DAYS}-day window)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    try:
        args = parse_args()
        process_turant_statuses(include_all=args.include_all)
    except Exception as exc:
        print(f"Turant Loan status sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
