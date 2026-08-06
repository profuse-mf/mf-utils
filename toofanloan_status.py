"""Sync Toofan Loan lead status into lead_master.

Status API (Marketplace_Aggregator_Integration_Guide.pdf):
  POST {TOOFAN_BASE_URL}/status
  Headers: Content-Type: application/json, x-api-credential
  Body (any one): { leadId } | { mobile } | { pancard }

leadId is taken from lead_master.lender_ref_id (lead_create leadID).
"""

import base64
import json
import sys
import time
import urllib.error
import urllib.request

import pymysql

from config import (
    TOOFAN_ACCESS_KEY,
    TOOFAN_API_CREDENTIAL,
    TOOFAN_BASE_URL,
    TOOFAN_LENDER_ID,
    TOOFAN_SECRET_KEY,
    TOOFAN_STATUS_API_URL,
    db_config,
)

MYSQL_CONFIG = db_config()
STALE_DAYS = 30
REQUEST_DELAY_SECONDS = 1

SKIP_DISBURSE_STATUSES = (
    "disbursed",
    "rejected",
)

LEADS_QUERY = """
SELECT
    lm.id,
    lm.user_id,
    lm.lender_id,
    lm.lender_ref_id
FROM lead_master AS lm
WHERE lm.lender_id = %s
  AND lm.status = 1
  AND lm.created >= NOW() - INTERVAL %s DAY
  AND lm.lender_ref_id IS NOT NULL
  AND TRIM(lm.lender_ref_id) != ''
  AND LOWER(TRIM(IFNULL(lm.disburse_status, ''))) NOT IN ({skip_placeholders})
ORDER BY lm.id
""".format(
    skip_placeholders=", ".join(["%s"] * len(SKIP_DISBURSE_STATUSES)),
)


def resolve_api_credential():
    """Match mf-api toofanloan.controller.js resolveApiCredential."""
    precomputed = str(TOOFAN_API_CREDENTIAL or "").strip()
    if precomputed:
        return precomputed

    access = str(TOOFAN_ACCESS_KEY or "").strip()
    secret = str(TOOFAN_SECRET_KEY or "").strip()
    if not access and not secret:
        return ""

    if secret and not access:
        return secret

    if access and secret:
        try:
            decoded = base64.b64decode(secret).decode("utf-8")
            if decoded.startswith(f"{access}:"):
                return secret
        except Exception:
            pass
        return base64.b64encode(f"{access}:{secret}".encode("utf-8")).decode("ascii")

    return ""


def require_config():
    missing = []
    if not TOOFAN_STATUS_API_URL:
        missing.append("TOOFAN_STATUS_API_URL / TOOFAN_BASE_URL")
    if not resolve_api_credential():
        missing.append(
            "TOOFAN_API_CREDENTIAL or TOOFAN_ACCESS_KEY + TOOFAN_SECRET_KEY"
        )
    if missing:
        raise RuntimeError(
            "Toofan Loan config missing. Set in .env: " + ", ".join(missing)
        )


def resolve_lender_id():
    if TOOFAN_LENDER_ID:
        return int(TOOFAN_LENDER_ID)

    conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id
                FROM mf_lenders
                WHERE LOWER(lender_name) LIKE %s
                   OR LOWER(IFNULL(product_offering, '')) LIKE %s
                ORDER BY id
                LIMIT 1
                """,
                ("%toofan%", "%toofan%"),
            )
            row = cursor.fetchone()
            if not row:
                raise RuntimeError(
                    "Could not resolve Toofan lender_id from mf_lenders. "
                    "Set TOOFAN_LENDER_ID in .env."
                )
            return int(row["id"])
    finally:
        conn.close()


def normalize_value(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == "NA":
        return None
    return text


def fetch_leads(lender_id):
    conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                LEADS_QUERY,
                (lender_id, STALE_DAYS, *SKIP_DISBURSE_STATUSES),
            )
            return cursor.fetchall()
    finally:
        conn.close()


def fetch_toofan_status(lead_id):
    payload = {"leadId": str(lead_id).strip()}
    body = json.dumps(payload).encode("utf-8")
    credential = resolve_api_credential()

    request = urllib.request.Request(
        TOOFAN_STATUS_API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-api-credential": credential,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Toofan API error {exc.code} for leadId={lead_id}: {error_body}"
        ) from exc


def extract_status_payload(response_body):
    if not isinstance(response_body, dict):
        return None

    data = response_body.get("data")
    if isinstance(data, dict) and data:
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]

    if any(
        key in response_body
        for key in ("status", "unique_id", "disbursed_amount", "stage")
    ):
        return response_body

    return None


def map_disburse_fields(item):
    status = normalize_value(item.get("status"))
    stage = normalize_value(item.get("stage"))
    step_code = normalize_value(item.get("step_code"))

    disburse_status = status
    if status and status.lower() == "pending" and (stage or step_code):
        parts = [status]
        if stage:
            parts.append(stage)
        if step_code:
            parts.append(step_code)
        disburse_status = " / ".join(parts)
    elif status and status.lower() == "rejected":
        reason = normalize_value(item.get("rejection_reason"))
        if reason:
            disburse_status = f"{status}: {reason}"

    disburse_amount = normalize_value(
        item.get("disbursed_amount")
        or item.get("disbursal_amount")
        or item.get("offeredAmount")
    )
    disburse_datetime = normalize_value(
        item.get("disbursement_date")
        or item.get("disbursed_date")
        or item.get("disburse_datetime")
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


def process_toofan_statuses():
    require_config()
    lender_id = resolve_lender_id()
    leads = fetch_leads(lender_id)

    print(f"Toofan status URL: {TOOFAN_STATUS_API_URL}")
    print(f"Toofan base URL: {TOOFAN_BASE_URL}")
    print(f"lender_id={lender_id}")
    print(f"Found {len(leads)} lead(s) created in the last {STALE_DAYS} days")

    updated_count = 0
    skipped_count = 0
    failed_count = 0

    for lead in leads:
        lead_id = lead["id"]
        lender_ref_id = str(lead["lender_ref_id"]).strip()
        print(
            f"Processing lead_id={lead_id}, "
            f"lender_ref_id={lender_ref_id}"
        )

        try:
            response_body = fetch_toofan_status(lender_ref_id)
            print(f"  Response: {json.dumps(response_body, default=str)[:500]}")
            item = extract_status_payload(response_body)
            if not item:
                print(
                    f"  Skipped: no usable status payload "
                    f"(success={response_body.get('success')}, "
                    f"message={response_body.get('message')})"
                )
                skipped_count += 1
                continue

            disburse_status, disburse_amount, disburse_datetime = map_disburse_fields(
                item
            )
            if not disburse_status:
                print("  Skipped: status field empty in payload")
                skipped_count += 1
                continue

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

        time.sleep(REQUEST_DELAY_SECONDS)

    print()
    print(
        f"Done. Updated={updated_count}, Skipped={skipped_count}, Failed={failed_count}"
    )


if __name__ == "__main__":
    try:
        process_toofan_statuses()
    except Exception as exc:
        print(f"Toofan Loan status sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
