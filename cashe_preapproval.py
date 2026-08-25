"""CASHe Pre Approval API — batch / single user / last-N-days apps / BRE-eligible.

API:
  POST {CASHE_BASE_URL}/report/getLoanApprovalDetails
  Headers: Content-Type: application/json, Check-Sum (HMAC-SHA1 of json.dumps body)

Modes:
  --batch                 All mf_users with required fields
  --user-id ID            Single user
  --days N                Users with application_master created in last N days
  --bre-eligible          BRE-eligible for CASHe (lender_id=11, empty criteria_missed)
                          Optional: combine with --days N to limit window

Examples:
  python3 cashe_preapproval.py --user-id 12345
  python3 cashe_preapproval.py --days 7
  python3 cashe_preapproval.py --bre-eligible --days 30
  python3 cashe_preapproval.py --batch
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

import pymysql

from config import (
    CASHE_BASE_URL,
    CASHE_CHECKSUM_SECRET,
    CASHE_LENDER_ID,
    CASHE_PARTNER_NAME,
    CASHE_PREAPPROVAL_API_URL,
    db_config,
)

MYSQL_CONFIG = db_config()
REQUEST_DELAY_SECONDS = 1
DEFAULT_LOAN_AMOUNT = "50000"

ENSURE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mf_cashe_preapprovals (
    id BIGINT NOT NULL AUTO_INCREMENT,
    user_id INT DEFAULT NULL,
    application_id BIGINT DEFAULT NULL,
    pan VARCHAR(12) DEFAULT NULL,
    mobile VARCHAR(15) DEFAULT NULL,
    approval_status VARCHAR(80) DEFAULT NULL,
    approved_amount VARCHAR(30) DEFAULT NULL,
    http_status INT DEFAULT NULL,
    request_json JSON DEFAULT NULL,
    response_json JSON DEFAULT NULL,
    created DATETIME NOT NULL,
    PRIMARY KEY (id),
    KEY idx_cashe_preapp_user (user_id),
    KEY idx_cashe_preapp_app (application_id),
    KEY idx_cashe_preapp_status (approval_status),
    KEY idx_cashe_preapp_created (created)
)
"""

USER_SELECT = """
    u.id AS user_id,
    u.name,
    u.email,
    u.mobile,
    u.pan,
    u.dob,
    u.monthly_income,
    u.employment_type,
    u.salary_mode,
    u.emp_name,
    u.address,
    u.locality,
    u.district,
    u.state,
    u.res_pincode
"""


def require_config():
    missing = []
    if not CASHE_PREAPPROVAL_API_URL:
        missing.append("CASHE_PREAPPROVAL_API_URL / CASHE_BASE_URL")
    if not CASHE_PARTNER_NAME:
        missing.append("CASHE_PARTNER_NAME")
    if not CASHE_CHECKSUM_SECRET:
        missing.append("CASHE_CHECKSUM_SECRET")
    if missing:
        raise RuntimeError("CASHe config missing. Set in .env: " + ", ".join(missing))


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


def normalize_text(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"NA", "NONE", "NULL"}:
        return None
    return text


def normalize_mobile(value):
    digits = re.sub(r"\D", "", str(value or ""))
    last10 = digits[-10:]
    if not re.fullmatch(r"[6-9]\d{9}", last10):
        return None
    return last10


def normalize_pan(value):
    pan = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", pan):
        return None
    return pan


def normalize_email(value):
    email = normalize_text(value)
    if not email or "@" not in email:
        return None
    return email


def normalize_pincode(value):
    digits = re.sub(r"\D", "", str(value or ""))[:6]
    if not re.fullmatch(r"[1-9]\d{5}", digits):
        return None
    return digits


def format_dob(value):
    """Pre Approval sample: YYYY-MM-DD 00:00:00"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d 00:00:00")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d 00:00:00")
    text = str(value).strip()
    if not text:
        return None
    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if iso:
        return f"{iso.group(1)}-{iso.group(2)}-{iso.group(3)} 00:00:00"
    dmy = re.match(r"^(\d{2})-(\d{2})-(\d{4})$", text)
    if dmy:
        return f"{dmy.group(3)}-{dmy.group(2)}-{dmy.group(1)} 00:00:00"
    return None


def map_salary_received_type(raw):
    """1 Cash, 2 Cheque, 3 Direct Account Transfer (default 3)."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 3
    if n in (1, 2, 3):
        return n
    return 3


def map_employment_type(raw):
    """Pass mf_users.employment_type as-is when 1–7; default 1 (Salaried)."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1
    if 1 <= n <= 7:
        return n
    return 1


def is_empty_criteria_missed(value):
    if value is None:
        return True
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return True
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            text = re.sub(r"\s+", "", text)
            return text in {"[]", "{}", "null", "None"}
    if isinstance(value, dict):
        return len(value) == 0
    if isinstance(value, (list, tuple)):
        return len(value) == 0
    return False


def ensure_table(conn):
    with conn.cursor() as cursor:
        cursor.execute(ENSURE_TABLE_SQL)
    conn.commit()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="CASHe Pre Approval — fetch offer for users",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes (pick one):
  --batch                         All mf_users with required fields
  --user-id ID                    Single user
  --days N                        Apps created in last N days
  --bre-eligible [--days N]       BRE-eligible for CASHe (lender_id=11);
                                  --days defaults to 30 when omitted

Examples:
  python3 cashe_preapproval.py --user-id 12345
  python3 cashe_preapproval.py --days 7
  python3 cashe_preapproval.py --bre-eligible
  python3 cashe_preapproval.py --bre-eligible --days 30
  python3 cashe_preapproval.py --batch --limit 100 --dry-run
""",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Run for all mf_users with required fields",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        metavar="ID",
        help="Run for a single mf_users.id",
    )
    parser.add_argument(
        "--days",
        type=int,
        metavar="N",
        help=(
            "Last N days apps (standalone mode), or lookback for "
            "--bre-eligible (default 30 when used with --bre-eligible)"
        ),
    )
    parser.add_argument(
        "--bre-eligible",
        action="store_true",
        help=(
            f"Users BRE-eligible for CASHe (lender_id={CASHE_LENDER_ID}, "
            "empty criteria_missed). Optional --days for lookback."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max users to process (0 = no limit)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=REQUEST_DELAY_SECONDS,
        help=f"Seconds between API calls (default: {REQUEST_DELAY_SECONDS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build/print payloads only; do not call API or write DB",
    )
    args = parser.parse_args(argv)

    modes = sum(
        [
            bool(args.batch),
            args.user_id is not None,
            args.days is not None and not args.bre_eligible,
            bool(args.bre_eligible),
        ]
    )
    if modes != 1:
        parser.error(
            "Pick exactly one mode: --batch | --user-id ID | --days N | "
            "--bre-eligible [--days N]"
        )
    if args.days is not None and args.days < 1:
        parser.error("--days must be >= 1")
    if args.bre_eligible and args.days is None:
        args.days = 30
    return args


def fetch_users_batch(conn, limit=0):
    sql = f"""
        SELECT {USER_SELECT},
               (
                   SELECT am.id
                   FROM application_master AS am
                   WHERE am.userid = u.id
                   ORDER BY am.id DESC
                   LIMIT 1
               ) AS application_id,
               (
                   SELECT am.loan_amount
                   FROM application_master AS am
                   WHERE am.userid = u.id
                   ORDER BY am.id DESC
                   LIMIT 1
               ) AS loan_amount
        FROM mf_users AS u
        WHERE u.pan IS NOT NULL AND TRIM(u.pan) != ''
          AND u.mobile IS NOT NULL AND TRIM(u.mobile) != ''
        ORDER BY u.id
    """
    if limit and limit > 0:
        sql += f" LIMIT {int(limit)}"
    with conn.cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchall()


def fetch_user_by_id(conn, user_id):
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT {USER_SELECT},
                   (
                       SELECT am.id
                       FROM application_master AS am
                       WHERE am.userid = u.id
                       ORDER BY am.id DESC
                       LIMIT 1
                   ) AS application_id,
                   (
                       SELECT am.loan_amount
                       FROM application_master AS am
                       WHERE am.userid = u.id
                       ORDER BY am.id DESC
                       LIMIT 1
                   ) AS loan_amount
            FROM mf_users AS u
            WHERE u.id = %s
            LIMIT 1
            """,
            (user_id,),
        )
        row = cursor.fetchone()
        return [row] if row else []


def fetch_users_by_recent_apps(conn, days, limit=0):
    sql = f"""
        SELECT {USER_SELECT},
               am.id AS application_id,
               am.loan_amount
        FROM application_master AS am
        JOIN mf_users AS u ON u.id = am.userid
        WHERE am.created >= NOW() - INTERVAL %s DAY
          AND u.pan IS NOT NULL AND TRIM(u.pan) != ''
          AND u.mobile IS NOT NULL AND TRIM(u.mobile) != ''
        ORDER BY am.id DESC
    """
    params = [days]
    if limit and limit > 0:
        sql += " LIMIT %s"
        params.append(int(limit))
    with conn.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        return cursor.fetchall()


def fetch_bre_eligible_users(conn, days, limit=0):
    """Users with empty criteria_missed for CASHe in application_bre_logs."""
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT application_id, criteria_missed, id
            FROM application_bre_logs
            WHERE lender_id = %s
              AND created >= NOW() - INTERVAL %s DAY
            ORDER BY id DESC
            """,
            (CASHE_LENDER_ID, days),
        )
        eligible_app_ids = []
        seen = set()
        for row in cursor.fetchall():
            app_id = row.get("application_id")
            if app_id is None or app_id in seen:
                continue
            if is_empty_criteria_missed(row.get("criteria_missed")):
                seen.add(app_id)
                eligible_app_ids.append(int(app_id))
                if limit and len(eligible_app_ids) >= limit:
                    break

    if not eligible_app_ids:
        return []

    placeholders = ", ".join(["%s"] * len(eligible_app_ids))
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT {USER_SELECT},
                   am.id AS application_id,
                   am.loan_amount
            FROM application_master AS am
            JOIN mf_users AS u ON u.id = am.userid
            WHERE am.id IN ({placeholders})
            ORDER BY am.id DESC
            """,
            tuple(eligible_app_ids),
        )
        return cursor.fetchall()


def build_payload(row):
    pan = normalize_pan(row.get("pan"))
    mobile = normalize_mobile(row.get("mobile"))
    name = normalize_text(row.get("name"))
    email = normalize_email(row.get("email"))
    pincode = normalize_pincode(row.get("res_pincode"))
    locality = normalize_text(row.get("locality"))
    district = normalize_text(row.get("district"))
    state = normalize_text(row.get("state"))
    address = normalize_text(row.get("address"))
    dob = format_dob(row.get("dob"))
    salary = normalize_text(
        str(row.get("monthly_income")).replace(",", "")
        if row.get("monthly_income") is not None
        else None
    )
    loan_amount = normalize_text(
        str(row.get("loan_amount")).replace(",", "")
        if row.get("loan_amount") is not None
        else None
    ) or DEFAULT_LOAN_AMOUNT
    company = normalize_text(row.get("emp_name")) or "NA"

    address_line1 = locality or district or address
    city = district or locality

    missing = []
    if not pan:
        missing.append("pan")
    if not mobile:
        missing.append("mobileNo")
    if not name:
        missing.append("name")
    if not email:
        missing.append("emailId")
    if not address_line1:
        missing.append("addressLine1")
    if not pincode:
        missing.append("pinCode")
    if not state:
        missing.append("state")
    if not city:
        missing.append("city")
    if not dob:
        missing.append("dob")
    if not salary:
        missing.append("salary")

    if missing:
        return None, missing

    payload = {
        "partner_name": CASHE_PARTNER_NAME,
        "pan": pan,
        "mobileNo": mobile,
        "name": name,
        "addressLine1": address_line1,
        "locality": locality or city,
        "pinCode": pincode,
        "gender": "M",
        "salary": salary,
        "state": state,
        "city": city,
        "dob": dob,
        "employmentType": map_employment_type(row.get("employment_type")),
        "salaryReceivedType": map_salary_received_type(row.get("salary_mode")),
        "emailId": email,
        "companyName": company,
        "loanAmount": loan_amount,
    }
    return payload, []


def call_preapproval(payload):
    body_string, checksum = generate_checksum(payload, CASHE_CHECKSUM_SECRET)
    request = urllib.request.Request(
        CASHE_PREAPPROVAL_API_URL,
        data=body_string.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Check-Sum": checksum,
        },
        method="POST",
    )

    print("  Request:")
    print(f"    POST {CASHE_PREAPPROVAL_API_URL}")
    print(f"    Check-Sum: {checksum}")
    print(f"    Body: {body_string}")

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            http_status = response.getcode()
            raw = response.read().decode("utf-8")
            print(f"  Response HTTP {http_status}:")
            print(f"    {raw}")
            return http_status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        print(f"  Response HTTP {exc.code}:")
        print(f"    {raw}")
        try:
            payload_out = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload_out = {"raw": raw}
        return exc.code, payload_out


def extract_approval_fields(response_body):
    if not isinstance(response_body, dict):
        return None, None

    candidates = [response_body]
    for key in ("data", "payLoad", "payload", "result", "response"):
        value = response_body.get(key)
        if isinstance(value, dict):
            candidates.append(value)
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            candidates.append(value[0])

    status = None
    amount = None
    for item in candidates:
        status = (
            status
            or normalize_text(item.get("approvalStatus"))
            or normalize_text(item.get("approval_status"))
            or normalize_text(item.get("preApprovalStatus"))
            or normalize_text(item.get("loanApprovalStatus"))
            or normalize_text(item.get("status"))
            or normalize_text(item.get("Status"))
        )
        amount = (
            amount
            or normalize_text(item.get("approvedAmount"))
            or normalize_text(item.get("approved_amount"))
            or normalize_text(item.get("loanAmount"))
            or normalize_text(item.get("totalLoanAmount"))
            or normalize_text(item.get("eligibleAmount"))
            or normalize_text(item.get("amount"))
        )
    return status, amount


def insert_preapproval_row(
    conn,
    user_id,
    application_id,
    pan,
    mobile,
    approval_status,
    approved_amount,
    http_status,
    request_payload,
    response_payload,
):
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO mf_cashe_preapprovals (
                user_id, application_id, pan, mobile,
                approval_status, approved_amount, http_status,
                request_json, response_json, created
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s,
                CAST(%s AS JSON), CAST(%s AS JSON), NOW()
            )
            """,
            (
                user_id,
                application_id,
                pan,
                mobile,
                approval_status,
                approved_amount,
                http_status,
                json.dumps(request_payload, ensure_ascii=False, default=str),
                json.dumps(response_payload, ensure_ascii=False, default=str),
            ),
        )
    conn.commit()


def load_users(conn, args):
    if args.batch:
        print("Mode: batch (all mf_users)")
        return fetch_users_batch(conn, args.limit)
    if args.user_id is not None:
        print(f"Mode: single user_id={args.user_id}")
        return fetch_user_by_id(conn, args.user_id)
    if args.bre_eligible:
        print(
            f"Mode: BRE-eligible for CASHe lender_id={CASHE_LENDER_ID} "
            f"(last {args.days} day(s))"
        )
        return fetch_bre_eligible_users(conn, args.days, args.limit)
    if args.days is not None:
        print(f"Mode: applications in last {args.days} day(s)")
        return fetch_users_by_recent_apps(conn, args.days, args.limit)
    raise RuntimeError("No mode selected")


def process_cashe_preapprovals(argv=None):
    require_config()
    args = parse_args(argv)

    print(f"CASHe Pre Approval URL: {CASHE_PREAPPROVAL_API_URL}")
    print(f"CASHe base URL: {CASHE_BASE_URL}")
    print(f"partner_name: {CASHE_PARTNER_NAME}")

    conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        if not args.dry_run:
            ensure_table(conn)

        users = load_users(conn, args)
        print(f"Users to process: {len(users)}")

        updated = 0
        skipped = 0
        failed = 0

        for row in users:
            user_id = row.get("user_id")
            application_id = row.get("application_id")
            print(
                f"Processing user_id={user_id}, application_id={application_id}"
            )

            payload, missing = build_payload(row)
            if not payload:
                skipped += 1
                print(f"  Skipped: missing fields {missing}")
                continue

            if args.dry_run:
                body_string, checksum = generate_checksum(
                    payload, CASHE_CHECKSUM_SECRET
                )
                print(f"  Dry-run Check-Sum: {checksum}")
                print(f"  Dry-run Body: {body_string}")
                updated += 1
                continue

            try:
                http_status, response_body = call_preapproval(payload)
                approval_status, approved_amount = extract_approval_fields(
                    response_body
                )
                insert_preapproval_row(
                    conn,
                    user_id=user_id,
                    application_id=application_id,
                    pan=payload.get("pan"),
                    mobile=payload.get("mobileNo"),
                    approval_status=approval_status,
                    approved_amount=approved_amount,
                    http_status=http_status,
                    request_payload=payload,
                    response_payload=response_body,
                )
                updated += 1
                print(
                    f"  Saved: approval_status={approval_status}, "
                    f"approved_amount={approved_amount}, "
                    f"http_status={http_status}"
                )
            except Exception as exc:
                failed += 1
                print(f"  Failed: {exc}", file=sys.stderr)

            time.sleep(max(args.delay, 0))

        print()
        print(
            f"Done. Saved/processed={updated}, Skipped={skipped}, Failed={failed}"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        process_cashe_preapprovals()
    except Exception as exc:
        print(f"CASHe Pre Approval failed: {exc}", file=sys.stderr)
        sys.exit(1)
