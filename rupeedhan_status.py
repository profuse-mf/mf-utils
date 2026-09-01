"""Sync RupeeDhan lead status into lead_master (+ mf_disbursals when Disbursed).

Status API:
  GET {RUPEEDHAN_BASE_URL}/lead/status?mobile={mobile}
  Headers: x-api-key

Looks up RupeeDhan leads (lender_id=14) via mf_users.mobile.
On success, stores data.status / data.disbursal_amount and optionally
data.lead_id into lender_ref_id.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

import pymysql

from config import (
    RUPEEDHAN_API_KEY,
    RUPEEDHAN_BASE_URL,
    RUPEEDHAN_LENDER_ID,
    RUPEEDHAN_STATUS_API_URL,
    db_config,
)
from mf_disbursals_store import apply_status_update

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
    lm.application_id,
    lm.lender_id,
    lm.lender_ref_id,
    u.mobile
FROM lead_master AS lm
JOIN mf_users AS u ON u.id = lm.user_id
WHERE lm.lender_id = %s
  AND lm.status = 1
  AND lm.created >= NOW() - INTERVAL %s DAY
  AND LOWER(TRIM(IFNULL(lm.disburse_status, ''))) NOT IN ({skip_placeholders})
ORDER BY lm.id
""".format(
    skip_placeholders=", ".join(["%s"] * len(SKIP_DISBURSE_STATUSES)),
)


def require_config():
    missing = []
    if not RUPEEDHAN_STATUS_API_URL:
        missing.append("RUPEEDHAN_STATUS_API_URL / RUPEEDHAN_BASE_URL")
    if not RUPEEDHAN_API_KEY:
        missing.append("RUPEEDHAN_API_KEY")
    if missing:
        raise RuntimeError(
            "RupeeDhan config missing. Set in .env: " + ", ".join(missing)
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


def fetch_leads():
    conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                LEADS_QUERY,
                (RUPEEDHAN_LENDER_ID, STALE_DAYS, *SKIP_DISBURSE_STATUSES),
            )
            return cursor.fetchall()
    finally:
        conn.close()


def fetch_rupeedhan_status(mobile):
    query = urllib.parse.urlencode({"mobile": mobile})
    url = f"{RUPEEDHAN_STATUS_API_URL}?{query}"
    headers = {
        "Accept": "application/json",
        "x-api-key": RUPEEDHAN_API_KEY,
    }

    print("  Request:")
    print("    GET", url)
    print("    x-api-key: ***")

    request = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            http_status = response.getcode()
            raw_body = response.read().decode("utf-8")
            print(f"  Response HTTP {http_status}:")
            print(f"    {raw_body}")
            return json.loads(raw_body) if raw_body else {}
    except urllib.error.HTTPError as exc:
        raw_body = exc.read().decode("utf-8", errors="replace")
        print(f"  Response HTTP {exc.code}:")
        print(f"    {raw_body}")
        try:
            payload = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            payload = None
        # 404 "No lead found" returns JSON — treat as a normal not-found response.
        if isinstance(payload, dict) and (
            exc.code == 404 or payload.get("success") is False
        ):
            return payload
        raise RuntimeError(
            f"RupeeDhan API error {exc.code} for mobile={mobile}: {raw_body}"
        ) from exc


def extract_status_payload(response_body):
    if not isinstance(response_body, dict):
        return None
    if response_body.get("success") is False:
        return None

    data = response_body.get("data")
    if isinstance(data, dict) and data.get("status"):
        return data
    return None


def map_disburse_fields(item):
    disburse_status = normalize_value(item.get("status"))
    disburse_amount = normalize_value(
        item.get("disbursal_amount")
        or item.get("disbursed_amount")
        or item.get("disbursedAmount")
    )
    # API sample has no disbursement date.
    disburse_datetime = normalize_value(
        item.get("disbursement_date")
        or item.get("disbursed_date")
        or item.get("disbursedAt")
    )
    lead_id = normalize_value(
        item.get("lead_id") or item.get("leadId") or item.get("leadID")
    )
    return disburse_status, disburse_amount, disburse_datetime, lead_id


def update_lead_in_mysql(
    lead_id,
    disburse_status,
    disburse_amount,
    disburse_datetime,
    lender_ref_id=None,
    *,
    user_id=None,
    application_id=None,
    lender_id=None,
):
    conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        return apply_status_update(
            conn,
            lead_id=lead_id,
            disburse_status=disburse_status,
            disburse_amount=disburse_amount,
            disburse_datetime=disburse_datetime,
            user_id=user_id,
            application_id=application_id,
            lender_id=lender_id if lender_id is not None else RUPEEDHAN_LENDER_ID,
            lender_ref_id=lender_ref_id,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def process_rupeedhan_statuses():
    require_config()
    leads = fetch_leads()

    print(f"RupeeDhan status URL: {RUPEEDHAN_STATUS_API_URL}")
    print(f"RupeeDhan base URL: {RUPEEDHAN_BASE_URL}")
    print(f"lender_id={RUPEEDHAN_LENDER_ID}")
    print(f"Found {len(leads)} lead(s) created in the last {STALE_DAYS} days")

    leads_by_phone = defaultdict(list)
    skipped_missing_mobile = 0
    for lead in leads:
        phone = normalize_phone(lead.get("mobile"))
        if not phone:
            skipped_missing_mobile += 1
            print(
                f"Skipped lead_id={lead['id']}: missing/invalid mobile "
                f"({lead.get('mobile')!r})"
            )
            continue
        leads_by_phone[phone].append(lead)

    phones = sorted(leads_by_phone)
    print(f"Unique phones to query: {len(phones)}")

    updated_count = 0
    skipped_count = skipped_missing_mobile
    failed_count = 0
    disbursals_count = 0

    for phone in phones:
        phone_leads = leads_by_phone[phone]
        print(f"Processing mobile={phone} ({len(phone_leads)} lead(s))")

        try:
            response_body = fetch_rupeedhan_status(phone)
            item = extract_status_payload(response_body)
            if not item:
                print(
                    f"  Skipped: no lead "
                    f"(success={response_body.get('success')}, "
                    f"message={response_body.get('message')!r})"
                )
                skipped_count += len(phone_leads)
                time.sleep(REQUEST_DELAY_SECONDS)
                continue

            disburse_status, disburse_amount, disburse_datetime, api_lead_id = (
                map_disburse_fields(item)
            )
            if not disburse_status:
                print("  Skipped: status empty in payload")
                skipped_count += len(phone_leads)
                time.sleep(REQUEST_DELAY_SECONDS)
                continue

            for lead in phone_leads:
                lead_id = lead["id"]
                try:
                    result = update_lead_in_mysql(
                        lead_id,
                        disburse_status,
                        disburse_amount,
                        disburse_datetime,
                        lender_ref_id=api_lead_id,
                        user_id=lead.get("user_id"),
                        application_id=lead.get("application_id"),
                        lender_id=lead.get("lender_id") or RUPEEDHAN_LENDER_ID,
                    )
                    updated_count += 1
                    print(
                        f"  Updated lead_id={lead_id}: "
                        f"disburse_status={disburse_status}, "
                        f"disburse_amount={disburse_amount}, "
                        f"disburse_datetime={disburse_datetime}, "
                        f"lender_ref_id={api_lead_id}"
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
            failed_count += len(phone_leads)
            print(f"  Failed mobile={phone}: {exc}", file=sys.stderr)

        time.sleep(REQUEST_DELAY_SECONDS)

    print()
    print(
        f"Done. Updated={updated_count}, Skipped={skipped_count}, "
        f"Failed={failed_count}, mf_disbursals={disbursals_count}"
    )


if __name__ == "__main__":
    try:
        process_rupeedhan_statuses()
    except Exception as exc:
        print(f"RupeeDhan status sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
