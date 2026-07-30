"""WhatsApp users eligible for a lender yesterday but not redirected.

Eligibility and lender URLs mirror eligible_not_redirected_email.py.
Only one message is sent per user:
  * if the user has multiple candidate applications, choose the second one
    in chronological order;
  * for that application, choose the first lender by lender ID.
"""

import json
import random
import re
import sys
from collections import defaultdict
from datetime import date, timedelta

import clickhouse_connect
import pymysql
import requests

from config import (
    CLICKHOUSE_DATABASE,
    CLICKHOUSE_HOST,
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_PORT,
    CLICKHOUSE_USER,
    db_config,
)

MYSQL_CONFIG = db_config()

WA_API_URL = "https://utilsapi.smsmsg.in/waba/sendmessage"
WA_API_KEY = "e6eb44d10c5bea3233cf88e6dfa2b234"
WA_TEMPLATE_ID = "1571984130956515"
SEND_MESSAGES = True

LENDER_TYPE_API = 1
LENDER_TYPE_UTM = 2
FALLBACK_OFFER_URL = "https://moneyfatafat.com"
TRACKIER_PUB_ID = 218
REMARKETING_SOURCE = "email_remarketing"

OFFER_FACTOR_MIN = 0.55
OFFER_FACTOR_MAX = 0.85
OFFER_AMOUNT_MIN = 1500
OFFER_AMOUNT_MAX = 80000


def _trackier_url(campaign_id):
    return (
        "https://profuse.gotrackier.com/click"
        f"?campaign_id={campaign_id}&pub_id={TRACKIER_PUB_ID}"
    )


LENDER_REDIRECT_URLS = {
    1: _trackier_url(211),  # Ram Fincorp
    2: _trackier_url(210),  # Poonawalla Fincorp
    3: _trackier_url(212),  # Emergency Paisa
    4: _trackier_url(200),  # Salary Top Up
    5: _trackier_url(134),  # Salary On Time
    6: _trackier_url(187),  # Surya Loan
    7: _trackier_url(211),  # Ram Fincorp (alternate product)
    8: _trackier_url(210),  # Poonawalla Fincorp (alternate product)
    9: _trackier_url(221),  # mPokket
    10: "https://www.mymoneybazaar.com",  # My Money Bazaar
    11: _trackier_url(227),  # CASHe
}


def resolve_offer_url(lender_id, application_id):
    url = LENDER_REDIRECT_URLS.get(int(lender_id), FALLBACK_OFFER_URL)
    separator = "&" if "?" in url else "?"
    return (
        f"{url}{separator}source={REMARKETING_SOURCE}"
        f"&p1={application_id}"
    )


def get_clickhouse_client():
    return clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
        database=CLICKHOUSE_DATABASE,
    )


def fetch_lenders(mysql_conn):
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, lender_name, product_offering, lender_type
            FROM mf_lenders
            ORDER BY id
            """
        )
        return cursor.fetchall()


def fetch_utm_eligible_application_ids(ch_client, lender_id, target_date):
    query = """
        SELECT DISTINCT application_id
        FROM application_bre_logs
        WHERE lender_id = {lender_id:UInt64}
          AND toDate(created) = {target_date:Date}
          AND replaceRegexpAll(trimBoth(ifNull(criteria_missed, '')), '\\s', '')
              IN ('{}', '[]', '')
    """
    result = ch_client.query(
        query,
        parameters={
            "lender_id": int(lender_id),
            "target_date": target_date.isoformat(),
        },
    )
    return {int(row[0]) for row in result.result_rows if row[0] is not None}


def fetch_api_eligible_application_ids(mysql_conn, lender_id, target_date):
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT application_id
            FROM lead_master
            WHERE lender_id = %s
              AND status = 1
              AND application_id IS NOT NULL
              AND application_id != 0
              AND DATE(created) = %s
            """,
            (lender_id, target_date),
        )
        return {int(row["application_id"]) for row in cursor.fetchall()}


def fetch_redirected_application_ids(mysql_conn, lender_id):
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT application_id
            FROM mf_lender_rediections_stats
            WHERE lender_id = %s
              AND application_id IS NOT NULL
              AND application_id != 0
            """,
            (lender_id,),
        )
        return {int(row["application_id"]) for row in cursor.fetchall()}


def collect_eligible_not_redirected(mysql_conn, ch_client, target_date):
    targets = []
    lenders = fetch_lenders(mysql_conn)

    print(f"Loaded {len(lenders)} lender(s)")
    print(f"Target date (yesterday): {target_date}")

    for lender in lenders:
        lender_id = int(lender["id"])
        lender_name = (lender.get("lender_name") or "Unknown").strip()
        lender_type = int(lender.get("lender_type") or 0)

        if lender_type == LENDER_TYPE_UTM:
            eligible_ids = fetch_utm_eligible_application_ids(
                ch_client, lender_id, target_date
            )
        elif lender_type == LENDER_TYPE_API:
            eligible_ids = fetch_api_eligible_application_ids(
                mysql_conn, lender_id, target_date
            )
        else:
            print(
                f"Skipping lender_id={lender_id} ({lender_name}): "
                f"unknown lender_type={lender_type}"
            )
            continue

        redirected_ids = fetch_redirected_application_ids(mysql_conn, lender_id)
        not_redirected_ids = eligible_ids - redirected_ids

        print(
            f"lender_id={lender_id} ({lender_name}): "
            f"eligible={len(eligible_ids)}, "
            f"redirected={len(eligible_ids & redirected_ids)}, "
            f"eligible_not_redirected={len(not_redirected_ids)}"
        )

        for application_id in sorted(not_redirected_ids):
            targets.append(
                {
                    "lender_id": lender_id,
                    "lender_name": lender_name,
                    "application_id": application_id,
                }
            )

    return targets


def fetch_application_user_details(mysql_conn, application_ids):
    if not application_ids:
        return {}

    placeholders = ", ".join(["%s"] * len(application_ids))
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT
                am.id AS application_id,
                am.userid AS user_id,
                am.loan_amount,
                am.created AS application_created,
                u.mobile,
                u.name
            FROM application_master AS am
            JOIN mf_users AS u ON u.id = am.userid
            WHERE am.id IN ({placeholders})
            """,
            tuple(application_ids),
        )
        return {int(row["application_id"]): row for row in cursor.fetchall()}


def format_user_name(name):
    if not name or not str(name).strip():
        return "User,"
    formatted = " ".join(
        word.capitalize() for word in str(name).strip().split()
    )
    return f"{formatted},"


def format_phone(mobile):
    digits = re.sub(r"\D", "", str(mobile or ""))
    if digits.startswith("91") and len(digits) == 12:
        return f"+{digits}"
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return f"+91{digits}"


def format_offer_amount(loan_amount):
    try:
        base = float(loan_amount)
    except (TypeError, ValueError):
        base = 0.0
    if base <= 0:
        base = 50000.0

    amount = int(round(base * random.uniform(OFFER_FACTOR_MIN, OFFER_FACTOR_MAX)))
    amount = max(OFFER_AMOUNT_MIN, min(OFFER_AMOUNT_MAX, amount))
    amount = int(round(amount / 1000) * 1000)
    amount = max(OFFER_AMOUNT_MIN, min(OFFER_AMOUNT_MAX, amount))
    return f"{amount:,}"


def choose_one_target_per_user(targets, details_by_app):
    """Choose the second chronological app, then its first lender, per user."""
    targets_by_user = defaultdict(lambda: defaultdict(list))
    missing_details = 0

    for target in targets:
        detail = details_by_app.get(target["application_id"])
        if not detail:
            missing_details += 1
            continue
        targets_by_user[int(detail["user_id"])][target["application_id"]].append(
            target
        )

    selected = []
    for user_id, targets_by_app in targets_by_user.items():
        application_ids = sorted(
            targets_by_app,
            key=lambda app_id: (
                details_by_app[app_id].get("application_created"),
                app_id,
            ),
        )
        selected_application_id = (
            application_ids[1] if len(application_ids) > 1 else application_ids[0]
        )
        first_lender = min(
            targets_by_app[selected_application_id],
            key=lambda target: target["lender_id"],
        )
        selected.append(
            {
                **first_lender,
                **details_by_app[selected_application_id],
                "user_id": user_id,
            }
        )

    selected.sort(key=lambda item: item["user_id"])
    return selected, missing_details


def build_send_jobs(selected_targets):
    jobs = []
    skipped_invalid_mobile = 0

    for target in selected_targets:
        phone = format_phone(target.get("mobile"))
        if not phone:
            skipped_invalid_mobile += 1
            continue

        application_id = int(target["application_id"])
        jobs.append(
            {
                "user_id": int(target["user_id"]),
                "application_id": application_id,
                "lender_id": int(target["lender_id"]),
                "lender_name": target["lender_name"],
                "phone": phone,
                "name": format_user_name(target.get("name")),
                "offer_amount": format_offer_amount(target.get("loan_amount")),
                "offer_url": resolve_offer_url(
                    target["lender_id"], application_id
                ),
            }
        )

    return jobs, skipped_invalid_mobile


def send_whatsapp(job):
    payload = {
        "template": WA_TEMPLATE_ID,
        "phone": job["phone"],
        "is_short_url": "0",
        "message": {
            "placeholders": [
                job["name"],
                job["offer_amount"],
                job["lender_name"],
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


def process_eligible_not_redirected_whatsapp():
    target_date = date.today() - timedelta(days=1)
    mysql_conn = pymysql.connect(**MYSQL_CONFIG)
    ch_client = get_clickhouse_client()

    try:
        targets = collect_eligible_not_redirected(
            mysql_conn, ch_client, target_date
        )
        app_ids = sorted({target["application_id"] for target in targets})
        details_by_app = fetch_application_user_details(mysql_conn, app_ids)
        selected, missing_details = choose_one_target_per_user(
            targets, details_by_app
        )
        jobs, skipped_invalid_mobile = build_send_jobs(selected)

        print()
        print(f"Eligible-not-redirected lender/app pairs: {len(targets)}")
        print(f"Unique users selected: {len(selected)}")
        print(
            f"Messages to send: {len(jobs)} "
            f"(missing application/user={missing_details}, "
            f"invalid mobile={skipped_invalid_mobile})"
        )

        if not SEND_MESSAGES:
            print("SEND_MESSAGES=False — listing recipients only:")
            for job in jobs:
                print(
                    f"  would send → {job['phone']} | "
                    f"user={job['user_id']} | app={job['application_id']} | "
                    f"lender={job['lender_name']} | "
                    f"offer=₹{job['offer_amount']} | url={job['offer_url']}"
                )
            return []

        sent = []
        failed = 0
        for job in jobs:
            print(
                f"Sending to {job['phone']} "
                f"(user={job['user_id']}, app={job['application_id']}, "
                f"lender={job['lender_name']})..."
            )
            try:
                result = send_whatsapp(job)
                print(f"  Sent: {result}")
                sent.append(job["phone"])
            except Exception as exc:
                failed += 1
                print(f"  Failed: {exc}", file=sys.stderr)

        print(f"Done. Sent={len(sent)}, Failed={failed}")
        return sent
    finally:
        mysql_conn.close()


if __name__ == "__main__":
    try:
        process_eligible_not_redirected_whatsapp()
    except Exception as exc:
        print(
            f"Eligible-not-redirected WhatsApp job failed: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
