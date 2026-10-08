"""WhatsApp users who were eligible for a lender on selected past days but not redirected.

Logic mirrors eligible_not_redirected_email.py exactly (day windows, A−B
eligibility, Ram disbursal excludes, lender priority / one-per-user
grouping). Only the delivery channel differs: WhatsApp instead of email.

Default target days (no args): D-1, D-2, D-3, D-5, D-7, D-10, D-15
  (today excluded; those exact calendar days only)

UTM (lender_type=2):
  A = application_bre_logs (ClickHouse) with empty criteria_missed,
      created on the target dates
  B = mf_lender_rediections_stats (MySQL) for that lender

API (lender_type=1):
  A = lead_master (MySQL) with status=1, created on the target dates
  B = mf_lender_rediections_stats (MySQL) for that lender

For Ram Fincorp lender_ids (1, 7), also exclude users who already have
mf_disbursals.d_status in (Success, DISBURSED) — case-insensitive — for
either lender_id 1 or 7.

Target apps = A - B (- disbursed users for 1/7).
At most one WA message per user. If a user is eligible for multiple
lenders/apps, prefer LENDER_PRIORITY (P1→P6); lenders not on that list
fall back to lowest lender_id. For the chosen lender, use the latest
application_id for URL/amount.

WA payload (template 982121851603771):
  placeholders = [Name, OfferAmount/- , LenderName]
  button.url = Trackier / fallback URL with wa_remarketing params
  phone = 10-digit mobile (no +91 prefix)

Usage:
  python3 eligible_not_redirected_wa.py          # D-1,2,3,5,7,10,15
  python3 eligible_not_redirected_wa.py -n 7     # continuous last 7 past days
"""

import argparse
import json
import random
import re
import sys
from collections import Counter
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
from mf_user_crypto import sql_aes_decrypt

MYSQL_CONFIG = db_config()

WA_API_URL = "https://utilsapi.smsmsg.in/waba/sendmessage"
WA_API_KEY = "e6eb44d10c5bea3233cf88e6dfa2b234"
WA_TEMPLATE_ID = "982121851603771"
SEND_MESSAGES = True
CAMPAIGN_CHANNEL = "WA"
CAMPAIGN_NAME = "D-1 Remarketing"

LENDER_TYPE_API = 1
LENDER_TYPE_UTM = 2
FALLBACK_OFFER_URL = "https://moneyfatafat.com"
TRACKIER_PUB_ID = 218
WA_REMARKETING_SOURCE = "wa_remarketing"
OFFER_FACTOR_MIN = 0.55
OFFER_FACTOR_MAX = 0.85
OFFER_AMOUNT_MIN = 1500
OFFER_AMOUNT_MAX = 80000

# Default: exact past days relative to today (D-1 … D-15 subset).
DEFAULT_DAY_OFFSETS = (1, 2, 3, 5, 7, 10, 15)

# Ram Fincorp product lines — also exclude already-disbursed users via mf_disbursals.
DISBURSAL_EXCLUDE_LENDER_IDS = (1, 7)
DISBURSAL_EXCLUDE_STATUSES = ("success", "disbursed")

# Lender priority when a user is eligible for multiple lenders (P1 highest).
# Only status=1 lenders are loaded; names match mf_lenders.lender_name loosely.
# Lenders not listed fall back to lowest lender_id.
LENDER_PRIORITY = (
    (14, ("rupeedhan",)),  # P1 Rupeedhan
    (None, ("toofan",)),  # P2 Toofan (id may vary; match by name)
    (12, ("creditsea",)),  # P3 CreditSea
    (16, ("b4salary",)),  # P4 B4Salary
    (18, ("actoloan",)),  # P5 Actoloan
    (20, ("fastrupees", "fastrupee")),  # P6 Fast Rupees
)


def _trackier_url(campaign_id):
    return (
        "https://profuse.gotrackier.com/click"
        f"?campaign_id={campaign_id}"
        f"&pub_id={TRACKIER_PUB_ID}"
    )


def append_wa_remarketing_params(url, application_id):
    separator = "&" if "?" in url else "?"
    return (
        f"{url}{separator}source={WA_REMARKETING_SOURCE}"
        f"&p1={application_id}"
    )


# lender_id → redirect URL (same map as eligible_not_redirected_email.py)
LENDER_REDIRECT_URLS = {
    1: _trackier_url(211),  # Ram Fincorp
    2: _trackier_url(210),  # Poonawalla Fincorp
    3: _trackier_url(212),  # Emergency Paisa
    4: _trackier_url(200),  # Salary Top Up
    5: _trackier_url(134),  # Salary On Time
    6: _trackier_url(187),  # Surya Loan
    7: _trackier_url(211),  # Ram Fincorp (alt product)
    8: _trackier_url(210),  # Poonawalla Fincorp (alt product)
    9: _trackier_url(221),  # mPokket
    10: "https://www.mymoneybazaar.com",  # My Money Bazaar
    11: _trackier_url(227),  # CASHe
    12: _trackier_url(235),  # CreditSea
    13: _trackier_url(234),  # PayMe
    14: _trackier_url(236),  # Rupeedhan
}


def resolve_offer_url(lender_id, application_id, lender_name=None):
    """Per-lender CTA URL with WA remarketing tracking params."""
    url = LENDER_REDIRECT_URLS.get(int(lender_id)) if lender_id is not None else None
    if not url:
        url = FALLBACK_OFFER_URL
    return append_wa_remarketing_params(url, application_id)


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
            WHERE status = 1
            ORDER BY id
            """
        )
        return cursor.fetchall()


def format_lender_display_name(lender_name, product_offering):
    name = (lender_name or "Unknown").strip() or "Unknown"
    offering = (product_offering or "").strip()
    if offering:
        return f"{name} - {offering}"
    return name


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
    # WA template expects amount with "/-" suffix (email HTML adds it separately).
    return f"{amount:,}/-"


def fetch_utm_eligible_application_ids(ch_client, lender_id, target_dates):
    if not target_dates:
        return set()
    date_literals = ", ".join(f"toDate('{d.isoformat()}')" for d in target_dates)
    query = f"""
        SELECT DISTINCT application_id
        FROM application_bre_logs
        WHERE lender_id = {{lender_id:UInt64}}
          AND toDate(created) IN ({date_literals})
          AND replaceRegexpAll(trimBoth(ifNull(criteria_missed, '')), '\\s', '')
              IN ('{{}}', '[]', '')
    """
    result = ch_client.query(
        query,
        parameters={"lender_id": int(lender_id)},
    )
    return {int(row[0]) for row in result.result_rows if row[0] is not None}


def fetch_api_eligible_application_ids(mysql_conn, lender_id, target_dates):
    if not target_dates:
        return set()
    placeholders = ", ".join(["%s"] * len(target_dates))
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT DISTINCT application_id
            FROM lead_master
            WHERE lender_id = %s
              AND status = 1
              AND application_id IS NOT NULL
              AND application_id != 0
              AND DATE(created) IN ({placeholders})
            """,
            (lender_id, *target_dates),
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


def fetch_disbursed_user_ids(mysql_conn, lender_ids):
    """Users with Success/DISBURSED in mf_disbursals for the given lender_ids."""
    if not lender_ids:
        return set()
    lender_placeholders = ", ".join(["%s"] * len(lender_ids))
    status_placeholders = ", ".join(["%s"] * len(DISBURSAL_EXCLUDE_STATUSES))
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT DISTINCT user_id
            FROM mf_disbursals
            WHERE lender_id IN ({lender_placeholders})
              AND user_id IS NOT NULL
              AND user_id != 0
              AND LOWER(TRIM(IFNULL(d_status, ''))) IN ({status_placeholders})
            """,
            (*lender_ids, *DISBURSAL_EXCLUDE_STATUSES),
        )
        return {int(row["user_id"]) for row in cursor.fetchall()}


def fetch_disbursed_application_ids(mysql_conn, lender_ids):
    """Applications with Success/DISBURSED in mf_disbursals for the given lender_ids."""
    if not lender_ids:
        return set()
    lender_placeholders = ", ".join(["%s"] * len(lender_ids))
    status_placeholders = ", ".join(["%s"] * len(DISBURSAL_EXCLUDE_STATUSES))
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT DISTINCT application_id
            FROM mf_disbursals
            WHERE lender_id IN ({lender_placeholders})
              AND application_id IS NOT NULL
              AND application_id != 0
              AND LOWER(TRIM(IFNULL(d_status, ''))) IN ({status_placeholders})
            """,
            (*lender_ids, *DISBURSAL_EXCLUDE_STATUSES),
        )
        return {int(row["application_id"]) for row in cursor.fetchall()}


def fetch_application_user_details(mysql_conn, application_ids):
    if not application_ids:
        return {}

    placeholders = ", ".join(["%s"] * len(application_ids))
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                am.id AS application_id,
                am.userid AS user_id,
                am.loan_amount,
                {mobile_col},
                u.name
            FROM application_master AS am
            JOIN mf_users AS u ON u.id = am.userid
            WHERE am.id IN ({placeholders})
            """.format(
                mobile_col=sql_aes_decrypt("u.mobile", "mobile"),
                placeholders=placeholders,
            ),
            tuple(application_ids),
        )
        return {int(row["application_id"]): row for row in cursor.fetchall()}


def collect_eligible_not_redirected(mysql_conn, ch_client, target_dates):
    """Return list of {lender_id, lender_name, application_id} for A - B."""
    lenders = fetch_lenders(mysql_conn)
    targets = []

    disbursed_user_ids = fetch_disbursed_user_ids(
        mysql_conn, DISBURSAL_EXCLUDE_LENDER_IDS
    )
    disbursed_application_ids = fetch_disbursed_application_ids(
        mysql_conn, DISBURSAL_EXCLUDE_LENDER_IDS
    )
    print(
        f"mf_disbursals Success/DISBURSED for lenders "
        f"{list(DISBURSAL_EXCLUDE_LENDER_IDS)}: "
        f"users={len(disbursed_user_ids)}, "
        f"applications={len(disbursed_application_ids)}"
    )

    print(f"Loaded {len(lenders)} lender(s)")
    print(
        "Target dates: "
        + ", ".join(d.isoformat() for d in target_dates)
    )
    print()

    for lender in lenders:
        lender_id = lender["id"]
        lender_name = (lender.get("lender_name") or "Unknown").strip() or "Unknown"
        lender_label = format_lender_display_name(
            lender.get("lender_name"),
            lender.get("product_offering"),
        )
        lender_type = int(lender["lender_type"] or 0)

        if lender_type == LENDER_TYPE_UTM:
            eligible_ids = fetch_utm_eligible_application_ids(
                ch_client, lender_id, target_dates
            )
        elif lender_type == LENDER_TYPE_API:
            eligible_ids = fetch_api_eligible_application_ids(
                mysql_conn, lender_id, target_dates
            )
        else:
            print(
                f"Skipping lender_id={lender_id} ({lender_label}): "
                f"unknown lender_type={lender_type}"
            )
            continue

        redirected_ids = fetch_redirected_application_ids(mysql_conn, lender_id)
        not_redirected_ids = eligible_ids - redirected_ids

        excluded_disbursed_count = 0
        if int(lender_id) in DISBURSAL_EXCLUDE_LENDER_IDS:
            before = len(not_redirected_ids)
            not_redirected_ids -= disbursed_application_ids
            excluded_disbursed_count = before - len(not_redirected_ids)

        print(
            f"lender_id={lender_id} ({lender_label}): "
            f"eligible={len(eligible_ids)}, "
            f"redirected={len(eligible_ids & redirected_ids)}, "
            f"excluded_disbursed={excluded_disbursed_count}, "
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

    return targets, disbursed_user_ids


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


def lender_choice_sort_key(lender_id, lender_name, application_id):
    """Prefer higher priority, then lower lender_id, then newer application."""
    return (
        lender_priority_rank(lender_id, lender_name),
        int(lender_id),
        -int(application_id),
    )


def build_send_jobs(targets, details_by_app, disbursed_user_ids=None):
    """Build at most one WA job per user_id.

    Multi-lender users: LENDER_PRIORITY (P1→P6) first; others by lowest
    lender_id. Same lender: latest application_id for amount + Trackier p1.
    """
    jobs = []
    skipped_no_mobile = 0
    skipped_missing_app = 0
    skipped_disbursed_user = 0
    skipped_duplicate_user = 0
    disbursed_user_ids = disbursed_user_ids or set()

    best_by_user = {}
    for target in targets:
        application_id = target["application_id"]
        detail = details_by_app.get(application_id)
        if not detail:
            skipped_missing_app += 1
            continue

        user_id = detail.get("user_id")
        if user_id is None:
            skipped_missing_app += 1
            continue
        user_id = int(user_id)
        lender_id = int(target["lender_id"])
        lender_name = target.get("lender_name")

        if (
            lender_id in DISBURSAL_EXCLUDE_LENDER_IDS
            and user_id in disbursed_user_ids
        ):
            skipped_disbursed_user += 1
            continue

        phone = format_phone(detail.get("mobile"))
        if not phone:
            skipped_no_mobile += 1
            continue

        new_key = lender_choice_sort_key(lender_id, lender_name, application_id)
        existing = best_by_user.get(user_id)
        if existing is not None:
            skipped_duplicate_user += 1
            existing_key = lender_choice_sort_key(
                int(existing["target"]["lender_id"]),
                existing["target"].get("lender_name"),
                existing["application_id"],
            )
            if new_key >= existing_key:
                continue

        best_by_user[user_id] = {
            "target": target,
            "detail": detail,
            "application_id": application_id,
            "user_id": user_id,
            "phone": phone,
        }

    for item in best_by_user.values():
        target = item["target"]
        detail = item["detail"]
        application_id = item["application_id"]
        lendername = target["lender_name"]
        name = format_user_name(detail.get("name"))
        offer_amount = format_offer_amount(detail.get("loan_amount"))
        offer_url = resolve_offer_url(
            target["lender_id"], application_id, lendername
        )

        jobs.append(
            {
                "phone": item["phone"],
                "user_id": item["user_id"],
                "application_id": application_id,
                "lender_id": target["lender_id"],
                "lender_name": lendername,
                "name": name,
                "offer_amount": offer_amount,
                "offer_url": offer_url,
            }
        )

    jobs.sort(key=lambda job: int(job["user_id"]))
    return (
        jobs,
        skipped_no_mobile,
        skipped_missing_app,
        skipped_disbursed_user,
        skipped_duplicate_user,
    )


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


def build_lender_stats(sent_jobs):
    counts = Counter(
        (job.get("lender_name") or "Unknown").strip() or "Unknown"
        for job in sent_jobs
    )
    return dict(sorted(counts.items(), key=lambda item: item[0].lower()))


def insert_campaign_record(mysql_conn, submitted_count, stats):
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
                json.dumps(stats, ensure_ascii=False),
                WA_TEMPLATE_ID,
                CAMPAIGN_NAME,
            ),
        )
        campaign_id = cursor.lastrowid
    mysql_conn.commit()
    return campaign_id


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "WhatsApp users eligible for a lender on selected past days "
            "but not redirected"
        )
    )
    parser.add_argument(
        "-n",
        "--days",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Optional continuous lookback of last N past days "
            f"(default: exact offsets {list(DEFAULT_DAY_OFFSETS)})"
        ),
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        metavar="ID",
        help="Send only to this mf_users.id (after eligibility filters)",
    )
    args = parser.parse_args(argv)
    if args.days is not None and args.days < 1:
        parser.error("-n/--days must be >= 1")
    if args.user_id is not None and args.user_id < 1:
        parser.error("--user-id must be >= 1")
    return args


def resolve_target_dates(days=None):
    """Return sorted target dates.

    Default: exact D-1, D-2, D-3, D-5, D-7, D-10, D-15.
    With -n N: continuous [today-N … today-1].
    """
    today = date.today()
    if days is None:
        return sorted(today - timedelta(days=offset) for offset in DEFAULT_DAY_OFFSETS)
    return sorted(today - timedelta(days=offset) for offset in range(1, days + 1))


def process_eligible_not_redirected_whatsapp(argv=None):
    args = parse_args(argv)
    target_dates = resolve_target_dates(args.days)
    mysql_conn = pymysql.connect(**MYSQL_CONFIG)
    ch_client = get_clickhouse_client()

    try:
        targets, disbursed_user_ids = collect_eligible_not_redirected(
            mysql_conn, ch_client, target_dates
        )
        print()
        print(f"Total eligible-not-redirected lender/app pairs: {len(targets)}")

        app_ids = sorted({item["application_id"] for item in targets})
        details_by_app = fetch_application_user_details(mysql_conn, app_ids)
        (
            jobs,
            skipped_no_mobile,
            skipped_missing_app,
            skipped_disbursed_user,
            skipped_duplicate_user,
        ) = build_send_jobs(targets, details_by_app, disbursed_user_ids)
        print(
            f"Messages to send: {len(jobs)} "
            f"(skipped missing mobile={skipped_no_mobile}, "
            f"missing application/user={skipped_missing_app}, "
            f"disbursed users={skipped_disbursed_user}, "
            f"duplicate users={skipped_duplicate_user})"
        )

        if args.user_id is not None:
            before = len(jobs)
            jobs = [job for job in jobs if int(job["user_id"]) == args.user_id]
            print(
                f"Filtered to --user-id={args.user_id}: "
                f"{len(jobs)} of {before} job(s)"
            )

        if not jobs:
            print("No recipients found. Nothing to send.")
            return []

        if not SEND_MESSAGES:
            print("SEND_MESSAGES=False — listing recipients only:")
            for job in jobs:
                print(
                    f"  would send → {job['phone']} | "
                    f"lender={job['lender_name']} | "
                    f"user_id={job['user_id']} | "
                    f"name={job['name']} | "
                    f"offer=₹{job['offer_amount']} | "
                    f"url={job['offer_url']} | "
                    f"app={job['application_id']}"
                )
            return []

        sent_jobs = []
        failed = 0
        for job in jobs:
            print(
                f"Sending to {job['phone']} "
                f"(lender={job['lender_name']}, "
                f"user_id={job['user_id']}, "
                f"app={job['application_id']}, "
                f"offer=₹{job['offer_amount']}, "
                f"url={job['offer_url']})..."
            )
            try:
                result = send_whatsapp(job)
                print(f"  Sent: {result}")
                sent_jobs.append(job)
            except Exception as exc:
                failed += 1
                print(f"  Failed: {exc}", file=sys.stderr)

        stats = build_lender_stats(sent_jobs)
        campaign_id = insert_campaign_record(
            mysql_conn, len(sent_jobs), stats
        )
        print()
        print(
            f"Done. Sent={len(sent_jobs)}, Failed={failed}, "
            f"mf_campaigns.id={campaign_id}, stats={stats}"
        )
        return [job["phone"] for job in sent_jobs]
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
