"""WhatsApp remarketing for users inactive for 60+ days.

Audience:
  Users who have at least one application in application_master, but none
  created in the last 60 days.

For each such user:
  1. Run BRE against all mf_lenders with status=1 using mf_lender_rules
     (same evaluateRule logic as mf-api apply loan).
  2. Keep only eligible lenders; sort by email LENDER_PRIORITY (P1→P6).
  3. Need at least 2 eligible lenders — otherwise skip the user.
  4. Send WA template with placeholders [name, lender1, lender2].

WA template 1344586910943344:
  placeholders = [Name, LenderName1, LenderName2]
  button.url = fixed Trackier campaign with source=WA_60_days
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict

import pymysql
import requests

from config import db_config
from mf_user_crypto import sql_aes_decrypt

MYSQL_CONFIG = db_config()

WA_API_URL = "https://utilsapi.smsmsg.in/waba/sendmessage"
WA_API_KEY = "e6eb44d10c5bea3233cf88e6dfa2b234"
WA_TEMPLATE_ID = "1344586910943344"
SEND_MESSAGES = True
CAMPAIGN_CHANNEL = "WA"
CAMPAIGN_NAME = "60 Days Inactive"

INACTIVE_DAYS = 60
OFFER_URL = (
    "https://profuse.gotrackier.com/click"
    "?campaign_id=231&pub_id=304&source=WA_60_days&utm_channel=WA"
)

# Same priority as eligible_not_redirected_email.py (P1 highest).
LENDER_PRIORITY = (
    (14, ("rupeedhan",)),  # P1 Rupeedhan
    (None, ("toofan",)),  # P2 Toofan
    (12, ("creditsea",)),  # P3 CreditSea
    (16, ("b4salary",)),  # P4 B4Salary
    (18, ("actoloan",)),  # P5 Actoloan
    (20, ("fastrupees", "fastrupee")),  # P6 Fast Rupees
)

INACTIVE_USERS_QUERY = f"""
SELECT
    u.id AS user_id,
    u.name,
    TIMESTAMPDIFF(YEAR, u.dob, CURDATE()) AS age,
    u.monthly_income,
    u.employment_type,
    u.salary_mode,
    u.res_pincode,
    u.total_exp,
    u.current_exp,
    u.emp_name,
    {sql_aes_decrypt("u.mobile", "mobile")},
    last_app.id AS application_id,
    last_app.loan_amount,
    last_app.loan_tenure,
    last_app.created AS last_application_created
FROM (
    SELECT
        userid,
        MAX(id) AS last_app_id,
        MAX(created) AS last_created
    FROM application_master
    WHERE userid IS NOT NULL
      AND userid != 0
    GROUP BY userid
    HAVING MAX(created) < NOW() - INTERVAL %s DAY
) AS inactive
JOIN mf_users AS u ON u.id = inactive.userid
JOIN application_master AS last_app ON last_app.id = inactive.last_app_id
ORDER BY u.id
"""


def format_user_name(name):
    if not name or not str(name).strip():
        return "User"
    return " ".join(word.capitalize() for word in str(name).strip().split())


def format_phone(mobile):
    digits = re.sub(r"\D", "", str(mobile or ""))
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return digits


def _extract_leading_int(value):
    if value is None:
        return None
    match = re.search(r"\d+", str(value))
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def normalize_lender_key(lender_name):
    return re.sub(r"[^a-z0-9]", "", (lender_name or "").lower())


def lender_priority_rank(lender_id, lender_name):
    """Lower rank = higher priority. Unlisted lenders sort after P1–P6 by lender_id."""
    key = normalize_lender_key(lender_name)
    for index, (priority_id, names) in enumerate(LENDER_PRIORITY):
        if priority_id is not None and int(lender_id) == int(priority_id):
            return index
        if any(name in key for name in names):
            return index
    return len(LENDER_PRIORITY) + int(lender_id)


def sort_lenders_by_priority(lenders):
    return sorted(
        lenders,
        key=lambda row: (
            lender_priority_rank(row["id"], row.get("lender_name")),
            int(row["id"]),
        ),
    )


def build_user_object(user_row):
    """Profile fields used by mf-api BRE evaluateRule."""
    return {
        "age": user_row.get("age"),
        "monthly_income": user_row.get("monthly_income"),
        "employment_type": user_row.get("employment_type"),
        "salary_mode": user_row.get("salary_mode"),
        "res_pincode": user_row.get("res_pincode"),
        "total_exp": _extract_leading_int(user_row.get("total_exp")),
        "emp_name": user_row.get("emp_name"),
        # API special-case recognised_company_list reads current_org.
        "current_org": user_row.get("emp_name"),
        "total_current_exp": _extract_leading_int(user_row.get("current_exp")),
        "loan_amount": user_row.get("loan_amount"),
        "loan_tenure": user_row.get("loan_tenure"),
    }


def fetch_inactive_users(mysql_conn):
    with mysql_conn.cursor() as cursor:
        cursor.execute(INACTIVE_USERS_QUERY, (INACTIVE_DAYS,))
        return cursor.fetchall()


def fetch_active_lenders(mysql_conn):
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, lender_name, lender_type, product_offering
            FROM mf_lenders
            WHERE status = 1
            ORDER BY id
            """
        )
        return cursor.fetchall()


def fetch_active_lender_rules(mysql_conn):
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT r.*
            FROM mf_lender_rules r
            INNER JOIN mf_lenders l ON l.id = r.lender_id
            WHERE r.status = 1
              AND l.status = 1
            ORDER BY r.lender_id, r.id
            """
        )
        return cursor.fetchall()


def evaluate_rule(cursor, user_object, rule):
    """Port of mf-api user.controller.js evaluateRule (boolean result)."""
    attribute_code = rule.get("attribute_code")
    lender_id = rule.get("lender_id")

    if attribute_code == "servicable_pincodes_list":
        pincode = user_object.get("res_pincode")
        if not pincode:
            return False
        try:
            safe_lender_id = int(lender_id)
        except (TypeError, ValueError):
            return False
        table_name = f"servicable_pincodes_{safe_lender_id}"
        # Table name is derived from integer lender_id only.
        try:
            cursor.execute(
                f"SELECT id FROM `{table_name}` WHERE pincode = %s LIMIT 1",
                (pincode,),
            )
            return cursor.fetchone() is not None
        except Exception:
            return False

    if attribute_code == "recognised_company_list":
        company_name = user_object.get("current_org")
        if not company_name:
            return False
        cursor.execute(
            """
            SELECT id
            FROM company_master
            WHERE lenderid = %s
              AND company_name = %s
            LIMIT 1
            """,
            (lender_id, company_name),
        )
        return cursor.fetchone() is not None

    user_value = user_object.get(attribute_code)
    if user_value is None or user_value == "":
        return False

    operator = str(rule.get("operator") or "").upper()
    try:
        numeric_user = float(user_value)
    except (TypeError, ValueError):
        numeric_user = None
    try:
        numeric_v1 = float(rule.get("value1")) if rule.get("value1") is not None else None
    except (TypeError, ValueError):
        numeric_v1 = None
    try:
        numeric_v2 = float(rule.get("value2")) if rule.get("value2") is not None else None
    except (TypeError, ValueError):
        numeric_v2 = None

    if operator in ("=", "=="):
        return str(user_value).lower() == str(rule.get("value1") or "").lower()
    if operator == "!=":
        return str(user_value).lower() != str(rule.get("value1") or "").lower()
    if operator == ">":
        return numeric_user is not None and numeric_v1 is not None and numeric_user > numeric_v1
    if operator == ">=":
        return numeric_user is not None and numeric_v1 is not None and numeric_user >= numeric_v1
    if operator == "<":
        return numeric_user is not None and numeric_v2 is not None and numeric_user < numeric_v2
    if operator == "<=":
        return numeric_user is not None and numeric_v2 is not None and numeric_user <= numeric_v2
    if operator == "BETWEEN":
        return (
            numeric_user is not None
            and numeric_v1 is not None
            and numeric_v2 is not None
            and numeric_v1 <= numeric_user <= numeric_v2
        )
    if operator == "IN":
        options = [
            part.strip().lower()
            for part in str(rule.get("value1") or "").split(",")
            if part.strip()
        ]
        return str(user_value).lower() in options
    return False


def find_eligible_lenders(cursor, user_object, active_lenders, rules_by_lender):
    """Return eligible active lenders (mf-api: no rules ⇒ eligible)."""
    eligible = []
    for lender in active_lenders:
        lender_id = int(lender["id"])
        rules = rules_by_lender.get(lender_id) or []
        if not rules:
            eligible.append(lender)
            continue
        if all(evaluate_rule(cursor, user_object, rule) for rule in rules):
            eligible.append(lender)
    return eligible


def build_send_jobs(mysql_conn, users, active_lenders, rules_by_lender):
    jobs = []
    skipped_invalid_mobile = 0
    skipped_lt_two_lenders = 0

    with mysql_conn.cursor() as cursor:
        for user in users:
            phone = format_phone(user.get("mobile"))
            if not phone:
                skipped_invalid_mobile += 1
                continue

            user_object = build_user_object(user)
            eligible = find_eligible_lenders(
                cursor, user_object, active_lenders, rules_by_lender
            )
            prioritized = sort_lenders_by_priority(eligible)
            if len(prioritized) < 2:
                skipped_lt_two_lenders += 1
                continue

            lender1 = (prioritized[0].get("lender_name") or "").strip() or "Lender"
            lender2 = (prioritized[1].get("lender_name") or "").strip() or "Lender"

            jobs.append(
                {
                    "user_id": int(user["user_id"]),
                    "application_id": int(user["application_id"]),
                    "phone": phone,
                    "name": format_user_name(user.get("name")),
                    "lender_name_1": lender1,
                    "lender_name_2": lender2,
                    "eligible_count": len(prioritized),
                    "offer_url": OFFER_URL,
                    "last_application_created": user.get("last_application_created"),
                }
            )

    return jobs, skipped_invalid_mobile, skipped_lt_two_lenders


def send_whatsapp(job):
    payload = {
        "template": WA_TEMPLATE_ID,
        "phone": job["phone"],
        "is_short_url": "0",
        "message": {
            "placeholders": [
                job["name"],
                job["lender_name_1"],
                job["lender_name_2"],
            ],
            "button": {"url": job["offer_url"]},
        },
    }
    headers = {
        "api_key": WA_API_KEY,
        "Content-Type": "application/json",
    }

    response = requests.post(
        WA_API_URL, json=payload, headers=headers, timeout=30
    )
    try:
        body = response.json() if response.text else {}
    except json.JSONDecodeError:
        body = {}

    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
    if body.get("status") not in (True, "true", "success"):
        raise RuntimeError(f"WA API rejected message: {response.text}")
    return body


def insert_campaign_record(mysql_conn, submitted_count, stats=None):
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO mf_campaigns
                (channel, submitted_count, stats, created, template, campaign_name)
            VALUES
                (%s, %s, CAST(%s AS JSON), NOW(), %s, %s)
            """,
            (
                CAMPAIGN_CHANNEL,
                int(submitted_count),
                json.dumps(stats or {}, ensure_ascii=False),
                WA_TEMPLATE_ID,
                CAMPAIGN_NAME,
            ),
        )
        campaign_id = cursor.lastrowid
    mysql_conn.commit()
    return campaign_id


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="WhatsApp remarketing for 60+ day inactive applicants"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List recipients only; do not send",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        metavar="ID",
        help="Process only this mf_users.id",
    )
    return parser.parse_args(argv)


def process_inactive_60_days_whatsapp(argv=None):
    args = parse_args(argv)
    send_messages = SEND_MESSAGES and not args.dry_run
    mysql_conn = pymysql.connect(**MYSQL_CONFIG)

    try:
        users = fetch_inactive_users(mysql_conn)
        if args.user_id is not None:
            users = [u for u in users if int(u["user_id"]) == args.user_id]
            print(f"Filtered to --user-id={args.user_id}: {len(users)} user(s)")

        active_lenders = fetch_active_lenders(mysql_conn)
        rules = fetch_active_lender_rules(mysql_conn)
        rules_by_lender = defaultdict(list)
        for rule in rules:
            rules_by_lender[int(rule["lender_id"])].append(rule)

        print(f"Inactive window: no applications in last {INACTIVE_DAYS} days")
        print(f"Inactive users found: {len(users)}")
        print(
            f"Active lenders={len(active_lenders)}, "
            f"active rules={len(rules)}"
        )
        print(f"Template: {WA_TEMPLATE_ID}")
        print(f"Button URL: {OFFER_URL}")

        jobs, skipped_invalid_mobile, skipped_lt_two = build_send_jobs(
            mysql_conn, users, active_lenders, rules_by_lender
        )
        print(
            f"Messages to send: {len(jobs)} "
            f"(invalid mobile={skipped_invalid_mobile}, "
            f"lt_2_eligible_lenders={skipped_lt_two})"
        )

        if not jobs:
            print("No recipients found. Nothing to send.")
            return []

        if not send_messages:
            print("DRY RUN / SEND_MESSAGES=False — listing recipients only:")
            for job in jobs:
                print(
                    f"  would send → {job['phone']} | "
                    f"user={job['user_id']} | "
                    f"last_app={job['application_id']} | "
                    f"name={job['name']} | "
                    f"lenders={job['lender_name_1']}, {job['lender_name_2']} | "
                    f"eligible={job['eligible_count']}"
                )
            print()
            print(f"Total recipients: {len(jobs)}")
            return []

        sent_jobs = []
        failed = 0
        for job in jobs:
            print(
                f"Sending to {job['phone']} "
                f"(user={job['user_id']}, "
                f"lenders={job['lender_name_1']}/{job['lender_name_2']})..."
            )
            try:
                result = send_whatsapp(job)
                print(f"  Sent: {result}")
                sent_jobs.append(job)
            except Exception as exc:
                failed += 1
                print(f"  Failed: {exc}", file=sys.stderr)

        stats = {
            "eligible_pair_users": len(jobs),
            "skipped_lt_two_lenders": skipped_lt_two,
            "skipped_invalid_mobile": skipped_invalid_mobile,
        }
        campaign_id = insert_campaign_record(mysql_conn, len(sent_jobs), stats)
        print(
            f"Done. Sent={len(sent_jobs)}, Failed={failed}, "
            f"mf_campaigns.id={campaign_id}, stats={stats}"
        )
        return [job["phone"] for job in sent_jobs]
    finally:
        mysql_conn.close()


if __name__ == "__main__":
    try:
        process_inactive_60_days_whatsapp()
    except Exception as exc:
        print(f"Inactive-60-days WhatsApp job failed: {exc}", file=sys.stderr)
        sys.exit(1)
