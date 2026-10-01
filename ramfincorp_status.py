import argparse
import base64
import json
import sys
import time
import uuid
import urllib.error
import urllib.request

import pymysql

from config import (
    RAMFINCORP_BASIC_AUTH_TOKEN,
    RAMFINCORP_BASIC_PASSWORD,
    RAMFINCORP_BASIC_USER,
    RAMFINCORP_CLIENT_ID,
    RAMFINCORP_JWE_TTL_SEC,
    RAMFINCORP_PUBLIC_KEY_PEM,
    RAMFINCORP_PUBLIC_PEM_PATH,
    RAMFINCORP_STATUS_API_URL,
    RAMFINCORP_USE_JOSE,
    RAMFINCORP_UTM_SOURCE,
    db_config,
)
from mf_disbursals_store import apply_status_update

MYSQL_CONFIG = db_config()
RAMFINCORP_LENDER_IDS = (1, 7)
STALE_DAYS = 30
REQUEST_DELAY_SECONDS = 1

SKIP_DISBURSE_STATUSES = (
    "Rejected On Request",
    "Rejected",
    "Rejected Process",
    "Disbursed",
)

LEADS_QUERY_TEMPLATE = """
SELECT
    lm.id,
    lm.user_id,
    lm.application_id,
    lm.lender_id,
    lm.lender_ref_id
FROM lead_master AS lm
WHERE lm.lender_id IN ({lender_placeholders})
  AND lm.status = 1
  {date_filter}
  AND lm.lender_ref_id IS NOT NULL
  AND lm.lender_ref_id != ''
  AND IFNULL(lm.disburse_status, '') NOT IN ({skip_placeholders})
ORDER BY lm.id
"""


def normalize_value(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == "NA":
        return None
    return text


def basic_auth_header():
    if RAMFINCORP_BASIC_AUTH_TOKEN:
        token = RAMFINCORP_BASIC_AUTH_TOKEN
        if token.lower().startswith("basic "):
            return token
        return f"Basic {token}"
    token = base64.b64encode(
        f"{RAMFINCORP_BASIC_USER}:{RAMFINCORP_BASIC_PASSWORD}".encode("utf-8")
    ).decode("ascii")
    return f"Basic {token}"


def load_public_pem():
    if RAMFINCORP_PUBLIC_PEM_PATH:
        with open(RAMFINCORP_PUBLIC_PEM_PATH, "rb") as pem_file:
            return pem_file.read()
    return RAMFINCORP_PUBLIC_KEY_PEM.encode("utf-8")


def encrypt_jose_payload(payload):
    try:
        from jwcrypto import jwk, jwe
    except ImportError as exc:
        raise RuntimeError(
            "jwcrypto is required for JOSE encryption. Install with: pip install jwcrypto"
        ) from exc

    public_key = jwk.JWK.from_pem(load_public_pem())
    now = int(time.time())
    envelope = {
        "iat": now,
        "exp": now + RAMFINCORP_JWE_TTL_SEC,
        "jti": str(uuid.uuid4()),
        "data": payload,
    }
    protected = {
        "alg": "ECDH-ES",
        "enc": "A256GCM",
        "client_id": RAMFINCORP_CLIENT_ID,
    }
    token = jwe.JWE(
        json.dumps(envelope).encode("utf-8"),
        protected=json.dumps(protected),
    )
    token.add_recipient(public_key)
    return token.serialize(compact=True)


def fetch_leads(include_all=False):
    date_filter = (
        "" if include_all else "AND lm.created >= NOW() - INTERVAL %s DAY"
    )
    query = LEADS_QUERY_TEMPLATE.format(
        lender_placeholders=", ".join(["%s"] * len(RAMFINCORP_LENDER_IDS)),
        skip_placeholders=", ".join(["%s"] * len(SKIP_DISBURSE_STATUSES)),
        date_filter=date_filter,
    )
    if include_all:
        params = (*RAMFINCORP_LENDER_IDS, *SKIP_DISBURSE_STATUSES)
    else:
        params = (*RAMFINCORP_LENDER_IDS, STALE_DAYS, *SKIP_DISBURSE_STATUSES)
    conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(query, params)
            return cursor.fetchall()
    finally:
        conn.close()


def fetch_ramfincorp_status(lead_id):
    try:
        lead_id_value = int(str(lead_id).strip())
    except (TypeError, ValueError):
        lead_id_value = lead_id

    payload = {
        "leadID": lead_id_value,
        "utmSource": RAMFINCORP_UTM_SOURCE,
        "utm_source": RAMFINCORP_UTM_SOURCE,
    }

    if RAMFINCORP_USE_JOSE:
        body = encrypt_jose_payload(payload).encode("utf-8")
        content_type = "application/jose"
    else:
        # Status API.pdf sample cURL uses plain JSON (preprod).
        body = json.dumps(payload).encode("utf-8")
        content_type = "application/json"

    request = urllib.request.Request(
        RAMFINCORP_STATUS_API_URL,
        data=body,
        headers={
            "Content-Type": content_type,
            "Authorization": basic_auth_header(),
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
            f"Ram Fincorp API error {exc.code} for leadID={lead_id}: {error_body}"
        ) from exc


def extract_status_payload(response_body):
    if not isinstance(response_body, dict):
        return None

    data = response_body.get("data")
    if isinstance(data, dict) and data:
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    # Some responses may put status fields at the top level.
    if any(
        key in response_body
        for key in ("currentStatus", "status", "loan_status", "message")
    ):
        return response_body
    return None


def get_disburse_status(item, response_body):
    step = item.get("step") if isinstance(item.get("step"), dict) else {}
    return normalize_value(
        item.get("currentLeadStatus")
        or item.get("currentStatus")
        or item.get("status")
        or item.get("loan_status")
        or item.get("disburse_status")
        or step.get("step_name")
        or item.get("currentIncompleteStep")
    )


def get_pending_step(item):
    pending = normalize_value(
        item.get("currentIncompleteStep")
        or item.get("current_incomplete_step")
        or item.get("pendingStep")
    )
    if pending:
        return pending
    steps = item.get("customerStatuses") or item.get("customer_statuses") or []
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            if str(step.get("status") or "").strip().upper() == "PENDING":
                return normalize_value(step.get("stepName") or step.get("step_name"))
    return None


def get_disburse_amount(item):
    return normalize_value(
        item.get("disbursalAmount")
        or item.get("disbursal_amount")
        or item.get("loan_disbursement_amount")
        or item.get("approveAmount")
        or item.get("loan_amount")
    )


def get_disburse_datetime(item):
    return normalize_value(
        item.get("disbursalDate")
        or item.get("disbursement_date")
        or item.get("disburse_datetime")
        or item.get("loan_disbursement_timestamp")
        or item.get("approveProcessedDate")
    )


def update_lead_in_mysql(
    lead_id,
    disburse_status,
    disburse_amount,
    disburse_datetime,
    *,
    user_id=None,
    application_id=None,
    lender_id=None,
    pending_step=None,
    response_json=None,
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
            lender_id=lender_id,
            pending_step=pending_step,
            response_json=response_json,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def process_ramfincorp_statuses(include_all=False):
    if not RAMFINCORP_STATUS_API_URL:
        raise RuntimeError(
            "RAMFINCORP_STATUS_API_URL is not set. Set the prod endpoint in .env when available."
        )
    if not RAMFINCORP_BASIC_AUTH_TOKEN and (
        not RAMFINCORP_BASIC_USER or not RAMFINCORP_BASIC_PASSWORD
    ):
        raise RuntimeError(
            "Ram Fincorp Basic auth is not configured. "
            "Set RAM_FINCORP_BASIC_AUTH_TOKEN (or RAMFINCORP_BASIC_USER/PASSWORD)."
        )

    leads = fetch_leads(include_all=include_all)
    mode = "jose" if RAMFINCORP_USE_JOSE else "json"
    window = "all time" if include_all else f"last {STALE_DAYS} days"
    print(
        f"Found {len(leads)} lead(s) ({window}) "
        f"(mode={mode}, url={RAMFINCORP_STATUS_API_URL})"
    )

    updated_count = 0
    skipped_count = 0
    failed_count = 0
    disbursals_count = 0

    for lead in leads:
        lead_id = lead["id"]
        user_id = lead["user_id"]
        lender_id = lead["lender_id"]
        lender_ref_id = lead["lender_ref_id"]
        print(
            f"Processing lead_id={lead_id}, user_id={user_id}, "
            f"lender_id={lender_id}, lender_ref_id={lender_ref_id}"
        )

        try:
            response_body = fetch_ramfincorp_status(lender_ref_id)
            print("  Response (raw):")
            print(
                f"    {json.dumps(response_body, ensure_ascii=False, default=str)}"
            )
            item = extract_status_payload(response_body)
            if not item:
                print(
                    f"  Skipped: API did not return data "
                    f"(message={response_body.get('message') if isinstance(response_body, dict) else None})"
                )
                skipped_count += 1
                continue

            disburse_status = get_disburse_status(item, response_body)
            disburse_amount = get_disburse_amount(item)
            disburse_datetime = get_disburse_datetime(item)
            pending_step = get_pending_step(item)

            result = update_lead_in_mysql(
                lead_id,
                disburse_status,
                disburse_amount,
                disburse_datetime,
                user_id=user_id,
                application_id=lead.get("application_id"),
                lender_id=lender_id,
                pending_step=pending_step,
                response_json=response_body,
            )
            updated_count += 1
            print(
                f"  Updated: disburse_status={disburse_status}, "
                f"pending_step={pending_step}, "
                f"disburse_amount={disburse_amount}, "
                f"disburse_datetime={disburse_datetime}"
            )
            if result.get("status_log_id"):
                print(f"  status_log id={result.get('status_log_id')}")
            if result.get("disbursal"):
                disbursals_count += 1
                print(
                    f"  mf_disbursals {result['disbursal']}: "
                    f"application_id={result.get('application_id')}, "
                    f"lender_id={result.get('lender_id')}"
                )
        except Exception as exc:
            failed_count += 1
            print(f"  Failed: {exc}", file=sys.stderr)

        time.sleep(REQUEST_DELAY_SECONDS)

    print()
    print(
        f"Done. Updated={updated_count}, Skipped={skipped_count}, "
        f"Failed={failed_count}, mf_disbursals={disbursals_count}"
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Sync Ram Fincorp lead statuses")
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
        process_ramfincorp_statuses(include_all=args.include_all)
    except Exception as exc:
        print(f"Ram Fincorp status sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
