"""Sync CASHe lead status into lead_master.

Status API:
  POST {CASHE_BASE_URL}/partner/customer_status
  Headers: Content-Type, Check-Sum (HMAC-SHA1 of Python json.dumps body)
  Body: { partner_name, partner_customer_id }

Checksum matches mf-api cashe.controller.js / CASHe Python sample:
  HMAC-SHA1(secret, json.dumps(payload)) → Base64

partner_customer_id is taken from lead_master.lender_ref_id.
"""

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

MYSQL_CONFIG = db_config()
CASHE_LENDER_ID = 11
STALE_DAYS = 30

LEADS_QUERY = """
SELECT
    lm.id,
    lm.user_id,
    lm.lender_ref_id
FROM lead_master AS lm
WHERE lm.lender_id = %s
  AND lm.status = 1
  AND lm.lender_ref_id IS NOT NULL
  AND TRIM(lm.lender_ref_id) != ''
  AND lm.created >= NOW() - INTERVAL %s DAY
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


def fetch_leads():
    conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(LEADS_QUERY, (CASHE_LENDER_ID, STALE_DAYS))
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


def update_lead_in_mysql(lead_id, disburse_status, disburse_amount, disburse_datetime):
    conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE lead_master
                SET disburse_status = %s,
                    disburse_amount = %s,
                    disburse_datetime = %s,
                    disbursal_status_check = NOW()
                WHERE id = %s
                """,
                (disburse_status, disburse_amount, disburse_datetime, lead_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def process_cashe_statuses():
    require_config()
    leads = fetch_leads()
    print(f"CASHe status URL: {CASHE_STATUS_API_URL}")
    print(f"Found {len(leads)} lead(s) created in the last {STALE_DAYS} days")

    updated_count = 0
    skipped_count = 0
    failed_count = 0

    for lead in leads:
        lead_id = lead["id"]
        partner_customer_id = str(lead["lender_ref_id"]).strip()
        print(
            f"Processing lead_id={lead_id}, "
            f"partner_customer_id={partner_customer_id}"
        )

        try:
            response_body = fetch_cashe_status(partner_customer_id)
            print(f"  Response: {json.dumps(response_body, default=str)[:500]}")
            item = extract_status_payload(response_body)
            if not item:
                print(
                    f"  Skipped: no usable status payload "
                    f"(status={response_body.get('status')}, "
                    f"message={response_body.get('message')})"
                )
                skipped_count += 1
                continue

            disburse_status, disburse_amount, disburse_datetime = map_disburse_fields(
                item
            )
            update_lead_in_mysql(
                lead_id,
                disburse_status,
                disburse_amount,
                disburse_datetime,
            )
            updated_count += 1
            print(
                f"  Updated: disburse_status={disburse_status}, "
                f"disburse_amount={disburse_amount}, "
                f"disburse_datetime={disburse_datetime}"
            )
        except Exception as exc:
            failed_count += 1
            print(f"  Failed: {exc}", file=sys.stderr)

        time.sleep(1)

    print()
    print(
        f"Done. Updated={updated_count}, Skipped={skipped_count}, Failed={failed_count}"
    )


if __name__ == "__main__":
    try:
        process_cashe_statuses()
    except Exception as exc:
        print(f"CASHe status sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
