"""Sync CreditSea loan status into lead_master.

Status API (Status_API_Doc.pdf):
  POST {CREDITSEA_BASE_URL}/api/v1/dsa/get-loan-status
  Headers: apiKey, sourceId, Content-Type: application/json
  Body: { "phoneNumbers": ["9999999999", ...] }  # 1–100 per request

Looks up CreditSea leads (lender_id=12) via mf_users.mobile and
updates disburse_status / disburse_amount / disburse_datetime.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

import pymysql

from config import (
    CREDITSEA_BASE_URL,
    CREDITSEA_LENDER_ID,
    CREDITSEA_SOURCE_ID,
    CREDITSEA_STATUS_API_KEY,
    CREDITSEA_STATUS_API_URL,
    db_config,
)

MYSQL_CONFIG = db_config()
STALE_DAYS = 30
REQUEST_DELAY_SECONDS = 1
BATCH_SIZE = 100

SKIP_DISBURSE_STATUSES = (
    "disbursed",
    "rejected",
)

LEADS_QUERY = """
SELECT
    lm.id,
    lm.user_id,
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
    if not CREDITSEA_STATUS_API_URL:
        missing.append("CREDITSEA_STATUS_API_URL / CREDITSEA_BASE_URL")
    if not CREDITSEA_STATUS_API_KEY:
        missing.append("CREDITSEA_STATUS_API_KEY")
    if not CREDITSEA_SOURCE_ID:
        missing.append("CREDITSEA_SOURCE_ID")
    if missing:
        raise RuntimeError(
            "CreditSea config missing. Set in .env: " + ", ".join(missing)
        )


def normalize_value(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == "NA":
        return None
    return text


def normalize_phone(mobile):
    if mobile is None:
        return None
    phone = str(mobile).strip().replace("+", "").replace(" ", "")
    digits = "".join(ch for ch in phone if ch.isdigit())
    if digits.startswith("91") and len(digits) > 10:
        digits = digits[-10:]
    if len(digits) != 10:
        return None
    return digits


def fetch_leads():
    conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                LEADS_QUERY,
                (CREDITSEA_LENDER_ID, STALE_DAYS, *SKIP_DISBURSE_STATUSES),
            )
            return cursor.fetchall()
    finally:
        conn.close()


def chunked(values, size):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def fetch_creditsea_statuses(phone_numbers):
    payload = json.dumps({"phoneNumbers": phone_numbers}).encode("utf-8")
    request = urllib.request.Request(
        CREDITSEA_STATUS_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "apiKey": CREDITSEA_STATUS_API_KEY,
            "sourceId": str(CREDITSEA_SOURCE_ID),
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 404:
            # No loans for any of the phones in this batch.
            return {"message": error_body or "No loans found", "data": {}}
        raise RuntimeError(
            f"CreditSea API error {exc.code} for "
            f"{len(phone_numbers)} phone(s): {error_body}"
        ) from exc


def pick_loan_for_lead(loans, lender_ref_id):
    """Prefer loan matching lender_ref_id; else most recent by disbursedAt/loanCreationDate."""
    if not loans:
        return None

    ref = normalize_value(lender_ref_id)
    if ref:
        for loan in loans:
            lead_id = normalize_value(
                loan.get("leadId") or loan.get("leadID") or loan.get("lead_id")
            )
            if lead_id and lead_id == ref:
                return loan

    def sort_key(loan):
        return (
            normalize_value(loan.get("disbursedAt"))
            or normalize_value(loan.get("loanCreationDate"))
            or ""
        )

    return sorted(loans, key=sort_key, reverse=True)[0]


def map_disburse_fields(loan):
    disburse_status = normalize_value(
        loan.get("loanStatus") or loan.get("loan_status") or loan.get("status")
    )
    disburse_amount = normalize_value(
        loan.get("disbursedAmount")
        or loan.get("disbursed_amount")
        or loan.get("disbursal_amount")
    )
    disburse_datetime = normalize_value(
        loan.get("disbursedAt")
        or loan.get("disbursed_at")
        or loan.get("disbursement_date")
    )
    return disburse_status, disburse_amount, disburse_datetime


def update_lead_in_mysql(
    lead_id, disburse_status, disburse_amount, disburse_datetime, lender_ref_id=None
):
    conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        with conn.cursor() as cursor:
            if lender_ref_id:
                cursor.execute(
                    """
                    UPDATE lead_master
                    SET disburse_status = %s,
                        disburse_amount = %s,
                        disburse_datetime = %s,
                        lender_ref_id = COALESCE(NULLIF(TRIM(lender_ref_id), ''), %s),
                        disbursal_status_check = NOW()
                    WHERE id = %s
                    """,
                    (
                        disburse_status,
                        disburse_amount,
                        disburse_datetime,
                        lender_ref_id,
                        lead_id,
                    ),
                )
            else:
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


def process_creditsea_statuses():
    require_config()
    leads = fetch_leads()
    print(f"CreditSea status URL: {CREDITSEA_STATUS_API_URL}")
    print(f"CreditSea base URL: {CREDITSEA_BASE_URL}")
    print(f"lender_id={CREDITSEA_LENDER_ID}, sourceId={CREDITSEA_SOURCE_ID}")
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

    for batch in chunked(phones, BATCH_SIZE):
        print(f"Requesting status for {len(batch)} phone(s)…")
        try:
            response_body = fetch_creditsea_statuses(batch)
            print(
                f"  Response message: {response_body.get('message')!r} "
                f"(keys={len((response_body.get('data') or {}))})"
            )
        except Exception as exc:
            failed_count += len(batch)
            print(f"  Batch failed: {exc}", file=sys.stderr)
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        data = response_body.get("data")
        if not isinstance(data, dict):
            data = {}

        for phone in batch:
            phone_leads = leads_by_phone[phone]
            loans = data.get(phone)
            if loans is None:
                # Try alternate key forms the API might return.
                loans = data.get(f"+91{phone}") or data.get(f"91{phone}") or []
            if not isinstance(loans, list):
                loans = []

            for lead in phone_leads:
                lead_id = lead["id"]
                try:
                    loan = pick_loan_for_lead(loans, lead.get("lender_ref_id"))
                    if not loan:
                        print(f"  lead_id={lead_id} phone={phone}: no loan data")
                        skipped_count += 1
                        continue

                    disburse_status, disburse_amount, disburse_datetime = (
                        map_disburse_fields(loan)
                    )
                    if not disburse_status:
                        print(
                            f"  lead_id={lead_id} phone={phone}: "
                            f"empty loanStatus in payload"
                        )
                        skipped_count += 1
                        continue

                    api_lead_id = normalize_value(
                        loan.get("leadId")
                        or loan.get("leadID")
                        or loan.get("lead_id")
                    )
                    update_lead_in_mysql(
                        lead_id,
                        disburse_status,
                        disburse_amount,
                        disburse_datetime,
                        lender_ref_id=api_lead_id,
                    )
                    updated_count += 1
                    print(
                        f"  Updated lead_id={lead_id} phone={phone}: "
                        f"disburse_status={disburse_status}, "
                        f"disburse_amount={disburse_amount}, "
                        f"disburse_datetime={disburse_datetime}"
                    )
                except Exception as exc:
                    failed_count += 1
                    print(f"  Failed lead_id={lead_id}: {exc}", file=sys.stderr)

        time.sleep(REQUEST_DELAY_SECONDS)

    print()
    print(
        f"Done. Updated={updated_count}, Skipped={skipped_count}, Failed={failed_count}"
    )


if __name__ == "__main__":
    try:
        process_creditsea_statuses()
    except Exception as exc:
        print(f"CreditSea status sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
