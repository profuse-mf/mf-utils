"""WhatsApp remarketing for users inactive for 60+ days.

Audience:
  Users who have at least one application in application_master, but none
  created in the last 60 days.

WA template matches eligible_not_redirected_wa.py:
  placeholders = [Name,, OfferAmount, LenderName]
  button.url = fixed Trackier campaign with source=WA_60_days
"""

import json
import random
import re
import sys

import pymysql
import requests

from config import db_config

MYSQL_CONFIG = db_config()

WA_API_URL = "https://utilsapi.smsmsg.in/waba/sendmessage"
WA_API_KEY = "e6eb44d10c5bea3233cf88e6dfa2b234"
WA_TEMPLATE_ID = "1571984130956515"
SEND_MESSAGES = False

INACTIVE_DAYS = 60
OFFER_URL = (
    "https://profuse.gotrackier.com/click"
    "?campaign_id=231&pub_id=304&source=WA_60_days&utm_channel=WA"
)

RANDOM_LENDERS = (
    "Emergency Paisa",
    "MPokket",
    "Surya Loan",
    "Ram Fincorp",
)

OFFER_FACTOR_MIN = 0.55
OFFER_FACTOR_MAX = 0.85
OFFER_AMOUNT_MIN = 1500
OFFER_AMOUNT_MAX = 80000

# Users who applied before, but whose latest application is older than 60 days.
INACTIVE_USERS_QUERY = """
SELECT
    u.id AS user_id,
    u.name,
    u.mobile,
    last_app.id AS application_id,
    last_app.loan_amount,
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


def fetch_inactive_users(mysql_conn):
    with mysql_conn.cursor() as cursor:
        cursor.execute(INACTIVE_USERS_QUERY, (INACTIVE_DAYS,))
        return cursor.fetchall()


def build_send_jobs(users):
    jobs = []
    skipped_invalid_mobile = 0

    for user in users:
        phone = format_phone(user.get("mobile"))
        if not phone:
            skipped_invalid_mobile += 1
            continue

        jobs.append(
            {
                "user_id": int(user["user_id"]),
                "application_id": int(user["application_id"]),
                "phone": phone,
                "name": format_user_name(user.get("name")),
                "offer_amount": format_offer_amount(user.get("loan_amount")),
                "lender_name": random.choice(RANDOM_LENDERS),
                "offer_url": OFFER_URL,
                "last_application_created": user.get("last_application_created"),
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


def process_inactive_60_days_whatsapp():
    mysql_conn = pymysql.connect(**MYSQL_CONFIG)

    try:
        users = fetch_inactive_users(mysql_conn)
        jobs, skipped_invalid_mobile = build_send_jobs(users)

        print(f"Inactive window: no applications in last {INACTIVE_DAYS} days")
        print(f"Inactive users found: {len(users)}")
        print(
            f"Messages to send: {len(jobs)} "
            f"(invalid mobile={skipped_invalid_mobile})"
        )
        print(f"Button URL: {OFFER_URL}")

        if not jobs:
            print("No recipients found. Nothing to send.")
            return []

        if not SEND_MESSAGES:
            print("SEND_MESSAGES=False — listing recipients only:")
            for job in jobs:
                print(
                    f"  would send → {job['phone']} | "
                    f"user={job['user_id']} | "
                    f"last_app={job['application_id']} | "
                    f"name={job['name']} | "
                    f"lender={job['lender_name']} | "
                    f"offer=₹{job['offer_amount']}"
                )
            print()
            print(f"Total recipients: {len(jobs)}")
            return []

        sent = []
        failed = 0
        for job in jobs:
            print(
                f"Sending to {job['phone']} "
                f"(user={job['user_id']}, lender={job['lender_name']}, "
                f"offer=₹{job['offer_amount']})..."
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
        process_inactive_60_days_whatsapp()
    except Exception as exc:
        print(f"Inactive-60-days WhatsApp job failed: {exc}", file=sys.stderr)
        sys.exit(1)
