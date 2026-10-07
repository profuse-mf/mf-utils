"""Sync CASHe lead status into lead_master.

Status API:
  POST {CASHE_BASE_URL}/partner/customer_status
  Headers: Content-Type, Check-Sum (HMAC-SHA1 of Python json.dumps body)
  Body: { partner_name, partner_customer_id }

Checksum matches mf-api cashe.controller.js / CASHe Python sample:
  HMAC-SHA1(secret, json.dumps(payload)) → Base64

partner_customer_id is taken from lead_master.lender_ref_id.
"""

import argparse
import base64
import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.request

import pymysql

from config import (
    CASHE_CHECKSUM_SECRET,
    CASHE_PARTNER_NAME,
    CASHE_STATUS_API_URL,
    db_config,
)
from mf_disbursals_store import persist_lender_status_poll

MYSQL_CONFIG = db_config()
CASHE_LENDER_ID = 11
STALE_DAYS = 30

LEADS_QUERY = """
SELECT
    lm.id,
    lm.user_id,
    lm.application_id,
    lm.lender_id,
    lm.lender_ref_id
FROM lead_master AS lm
WHERE lm.lender_id = %s
  AND lm.status = 1
  AND lm.lender_ref_id IS NOT NULL
  AND TRIM(lm.lender_ref_id) != ''
  {date_filter}
ORDER BY lm.id
"""


def require_config():
    missing = []
    if not CASHE_STATUS_API_URL:
        missing.append("CASHE_STATUS_API_URL / CASHE_BASE_URL")
    if not CASHE_PARTNER_NAME:
        missing.append("CASHE_PARTNER_NAME")
    if not CASHE_CHECKSUM_SECRET:
        missing.append("CASHE_CHECKSUM_SECRET")
    if missing:
        raise RuntimeError(
            "CASHe config missing. Set in .env: " + ", ".join(missing)
        )


def generate_checksum(payload, secret_key):
    """Match Python json.dumps defaults + HMAC-SHA1 → Base64 (CASHe / mf-api)."""
    body_string = json.dumps(payload)
    digest = hmac.new(
        secret_key.encode("utf-8"),
        body_string.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    checksum = base64.b64encode(digest).decode("ascii")
    return body_string, checksum


def normalize_value(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == "NA":
        return None
    return text


def fetch_leads(include_all=False):
    date_filter = (
        "" if include_all else "AND lm.created >= NOW() - INTERVAL %s DAY"
    )
    query = LEADS_QUERY.format(date_filter=date_filter)
    params = (CASHE_LENDER_ID,) if include_all else (CASHE_LENDER_ID, STALE_DAYS)
    conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(query, params)
            return cursor.fetchall()
    finally:
        conn.close()


def fetch_cashe_status(partner_customer_id):
    payload = {
        "partner_name": CASHE_PARTNER_NAME,
        "partner_customer_id": str(partner_customer_id),
    }
    body_string, checksum = generate_checksum(payload, CASHE_CHECKSUM_SECRET)

    request = urllib.request.Request(
        CASHE_STATUS_API_URL,
        data=body_string.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Check-Sum": checksum,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"CASHe API error {exc.code} for "
            f"partner_customer_id={partner_customer_id}: {error_body}"
        ) from exc


def extract_status_payload(response_body):
    """Prefer nested data/payLoad object; otherwise use top-level response."""
    if not isinstance(response_body, dict):
        return None

    for key in ("data", "payLoad", "payload", "result"):
        value = response_body.get(key)
        if isinstance(value, dict) and value:
            return value
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value[0]

    status = str(response_body.get("status") or "").upper()
    if status in {"ERROR", "VALIDATION_ERROR", "FAIL", "FAILED"}:
        return None

    if any(
        key in response_body
        for key in (
            "loan_status",
            "status",
            "customer_status",
            "disbursement_amount",
            "loan_amount",
            "disbursement_date",
        )
    ):
        return response_body

    return None


def map_disburse_fields(item):
    disburse_status = normalize_value(
        item.get("loan_status")
        or item.get("customer_status")
        or item.get("application_status")
        or item.get("status")
    )
    disburse_amount = normalize_value(
        item.get("disbursement_amount")
        or item.get("disburse_amount")
        or item.get("loan_disbursement_amount")
        or item.get("loan_amount")
        or item.get("approved_amount")
    )
    disburse_datetime = normalize_value(
        item.get("disbursement_date")
        or item.get("disburse_datetime")
        or item.get("loan_disbursement_timestamp")
        or item.get("disbursed_on")
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
            lender_id=lender_id if lender_id is not None else CASHE_LENDER_ID,
            apply_to_lead_and_disbursals=apply_to_lead_and_disbursals,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def process_cashe_statuses(include_all=False):
    require_config()
    leads = fetch_leads(include_all=include_all)
    print(f"CASHe status URL: {CASHE_STATUS_API_URL}")
    window = "all time" if include_all else f"last {STALE_DAYS} days"
    print(f"Found {len(leads)} lead(s) ({window})")

    updated_count = 0
    logged_only_count = 0
    failed_count = 0
    disbursals_count = 0

    for lead in leads:
        lead_id = lead["id"]
        partner_customer_id = str(lead["lender_ref_id"]).strip()
        print(
            f"Processing lead_id={lead_id}, "
            f"partner_customer_id={partner_customer_id}"
        )

        try:
            response_body = fetch_cashe_status(partner_customer_id)
            print("  Response (raw):")
            print(
                f"    {json.dumps(response_body, ensure_ascii=False, default=str)}"
            )
            item = extract_status_payload(response_body) or {}
            disburse_status, disburse_amount, disburse_datetime = map_disburse_fields(
                item
            )
            if not disburse_status and isinstance(response_body, dict):
                disburse_status = normalize_value(
                    response_body.get("status") or response_body.get("message")
                )
            apply_full = isinstance(response_body, dict)
            result = persist_poll(
                lead_id,
                response_json=response_body,
                disburse_status=disburse_status,
                disburse_amount=disburse_amount,
                disburse_datetime=disburse_datetime,
                user_id=lead.get("user_id"),
                application_id=lead.get("application_id"),
                lender_id=lead.get("lender_id") or CASHE_LENDER_ID,
                apply_to_lead_and_disbursals=apply_full,
            )
            if apply_full:
                updated_count += 1
                print(
                    f"  Persisted: disburse_status={disburse_status}, "
                    f"disburse_amount={disburse_amount}, "
                    f"disburse_datetime={disburse_datetime}, "
                    f"status_log_id={result.get('status_log_id')}"
                )
            else:
                logged_only_count += 1
            if result.get("disbursal"):
                disbursals_count += 1
                print(
                    f"  mf_disbursals {result['disbursal']}: "
                    f"application_id={result.get('application_id')}, "
                    f"lender_id={result.get('lender_id')}"
                )
        except Exception as exc:
            try:
                result = persist_poll(
                    lead_id,
                    response_json={
                        "error": str(exc),
                        "partner_customer_id": partner_customer_id,
                    },
                    user_id=lead.get("user_id"),
                    application_id=lead.get("application_id"),
                    lender_id=lead.get("lender_id") or CASHE_LENDER_ID,
                    apply_to_lead_and_disbursals=False,
                )
                logged_only_count += 1
                print(
                    f"  Logged error status_log_id={result.get('status_log_id')}",
                    file=sys.stderr,
                )
            except Exception as log_exc:
                failed_count += 1
                print(f"  Failed logging: {log_exc}", file=sys.stderr)
            print(f"  Failed: {exc}", file=sys.stderr)

        time.sleep(1)

    print()
    print(
        f"Done. Updated={updated_count}, LoggedOnly={logged_only_count}, "
        f"Failed={failed_count}, mf_disbursals={disbursals_count}"
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Sync CASHe lead statuses")
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
        process_cashe_statuses(include_all=args.include_all)
    except Exception as exc:
        print(f"CASHe status sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
