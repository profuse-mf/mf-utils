"""Sync Emergency Paisa lead status into lead_master.

Status API (Ep_Lead_Status.pdf):
  GET {EMERGENCY_PAISA_BASE_URL}/api/v1/open-partners/lead-status?leadid={leadid}
  Headers:
    Authorization: Bearer {EMERGENCY_PAISA_PARTNER_TOKEN}
    Referer: {EMERGENCY_PAISA_REFERER}

leadid is taken from lead_master.lender_ref_id (lead_push leadId).
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pymysql

from config import (
    EMERGENCY_PAISA_BASE_URL,
    EMERGENCY_PAISA_LENDER_ID,
    EMERGENCY_PAISA_PARTNER_TOKEN,
    EMERGENCY_PAISA_REFERER,
    EMERGENCY_PAISA_STATUS_API_URL,
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


def require_config():
    missing = []
    if not EMERGENCY_PAISA_STATUS_API_URL:
        missing.append("EMERGENCY_PAISA_STATUS_API_URL / EMERGENCY_PAISA_BASE_URL")
    if not EMERGENCY_PAISA_PARTNER_TOKEN:
        missing.append("EMERGENCY_PAISA_PARTNER_TOKEN")
    if missing:
        raise RuntimeError(
            "Emergency Paisa config missing. Set in .env: " + ", ".join(missing)
        )


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
            cursor.execute(
                LEADS_QUERY,
                (EMERGENCY_PAISA_LENDER_ID, STALE_DAYS, *SKIP_DISBURSE_STATUSES),
            )
            return cursor.fetchall()
    finally:
        conn.close()


def fetch_emergency_paisa_status(leadid):
    query = urllib.parse.urlencode({"leadid": str(leadid).strip()})
    url = f"{EMERGENCY_PAISA_STATUS_API_URL}?{query}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {EMERGENCY_PAISA_PARTNER_TOKEN}",
    }
    referer = str(EMERGENCY_PAISA_REFERER or "").strip()
    if referer:
        headers["Referer"] = referer

    request = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        # EP often returns HTTP 400 with a valid success payload
        # ({"status":1,"data":{"success":true,"lead_status":...}}).
        try:
            payload = json.loads(error_body) if error_body else {}
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and extract_status_payload(payload):
            return payload
        raise RuntimeError(
            f"Emergency Paisa API error {exc.code} for leadid={leadid}: {error_body}"
        ) from exc


def extract_status_payload(response_body):
    if not isinstance(response_body, dict):
        return None

    # Top-level status=1 means request succeeded.
    top_status = response_body.get("status")
    if top_status is not None and int(top_status) != 1:
        return None

    data = response_body.get("data")
    if isinstance(data, dict) and data:
        return data

    if any(
        key in response_body
        for key in ("lead_status", "disbursed_amount", "disbursed_date", "leadid")
    ):
        return response_body

    return None


def map_disburse_fields(item):
    disburse_status = normalize_value(
        item.get("lead_status")
        or item.get("leadStatus")
        or item.get("status_name")
        or item.get("status")
    )
    # Don't store numeric API success flags as disburse_status.
    if disburse_status in {"1", "0", "true", "false"}:
        disburse_status = normalize_value(item.get("lead_status"))

    disburse_amount = normalize_value(
        item.get("disbursed_amount")
        or item.get("disbursedAmount")
        or item.get("disbursal_amount")
    )
    disburse_datetime = normalize_value(
        item.get("disbursed_date")
        or item.get("disbursedDate")
        or item.get("disbursement_date")
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


def process_emergency_paisa_statuses():
    require_config()
    leads = fetch_leads()

    print(f"Emergency Paisa status URL: {EMERGENCY_PAISA_STATUS_API_URL}")
    print(f"Emergency Paisa base URL: {EMERGENCY_PAISA_BASE_URL}")
    print(f"lender_id={EMERGENCY_PAISA_LENDER_ID}")
    print(f"Found {len(leads)} lead(s) created in the last {STALE_DAYS} days")

    updated_count = 0
    skipped_count = 0
    failed_count = 0

    for lead in leads:
        lead_id = lead["id"]
        lender_ref_id = str(lead["lender_ref_id"]).strip()
        print(f"Processing lead_id={lead_id}, leadid={lender_ref_id}")

        try:
            response_body = fetch_emergency_paisa_status(lender_ref_id)
            print(f"  Response: {json.dumps(response_body, default=str)[:500]}")
            item = extract_status_payload(response_body)
            if not item:
                data = response_body.get("data")
                data_success = data.get("success") if isinstance(data, dict) else None
                print(
                    f"  Skipped: no usable status payload "
                    f"(status={response_body.get('status')}, "
                    f"success={data_success})"
                )
                skipped_count += 1
                continue

            disburse_status, disburse_amount, disburse_datetime = map_disburse_fields(
                item
            )
            if not disburse_status:
                print("  Skipped: lead_status empty in payload")
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
        process_emergency_paisa_statuses()
    except Exception as exc:
        print(f"Emergency Paisa status sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
