"""WhatsApp remarketing for lender statuses flagged remarketing=1.

Flow:
  1. Load unique (lender_id, status) from mf_lender_status_master
     where remarketing = 1.
  2. Find mf_disbursals rows matching those lender_id + d_status, whose
     application_master.created is within the last N days (default 15).
  3. Load user from mf_users; send WA template 1159104473247066:
       placeholders = [name, application_id, amt, lender_name, status]
       amt = 65%–100% of application_master.loan_amount, rounded to nearest 1000
       button.url = SSO from lender_api_logs for that app+lender
                   (mf-api stores SSO in apiresponse), else mf_lenders.redirect_url

Usage:
  python3 status_remarketing_wa.py
  python3 status_remarketing_wa.py --dry-run
  python3 status_remarketing_wa.py --days 15
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import defaultdict

import pymysql
import requests

from config import WA_API_KEY as CONFIG_WA_API_KEY
from config import WA_API_URL as CONFIG_WA_API_URL
from config import db_config
from mf_user_crypto import sql_aes_decrypt

MYSQL_CONFIG = db_config()

WA_API_URL = CONFIG_WA_API_URL or "https://utilsapi.smsmsg.in/waba/sendmessage"
WA_API_KEY = CONFIG_WA_API_KEY or "e6eb44d10c5bea3233cf88e6dfa2b234"
WA_TEMPLATE_ID = "1159104473247066"
SEND_MESSAGES = True
CAMPAIGN_CHANNEL = "WA"
CAMPAIGN_NAME = "Status Remarketing"
DEFAULT_LOOKBACK_DAYS = 15
OFFER_FACTOR_MIN = 0.65
OFFER_FACTOR_MAX = 1.00
OFFER_AMOUNT_FALLBACK = 25000
TRACKIER_SOURCE = "wa_status_remarketing"
CHECK_STATUS_URL = (
    "https://moneyfatafat.com/apply"
    "?source=wa_status_remarketing&p1={application_id}"
)


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
    """65%–100% of application_master.loan_amount, rounded to nearest 1000.

    Example: 27000 * 0.74 = 19980 → 20000
    """
    try:
        base = float(str(loan_amount).strip().replace(",", ""))
    except (TypeError, ValueError, AttributeError):
        base = 0.0
    if base <= 0:
        base = float(OFFER_AMOUNT_FALLBACK)

    amount = base * random.uniform(OFFER_FACTOR_MIN, OFFER_FACTOR_MAX)
    amount = int(round(amount / 1000.0) * 1000)
    if amount <= 0:
        amount = 1000
    return f"{amount:,}"


def _as_url(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("null", "none"):
        return None
    if not (text.startswith("http://") or text.startswith("https://")):
        return None
    return text


def extract_sso_url(apiresponse):
    """Extract lender SSO / redirect URL from lender_api_logs.apiresponse.

    Same preference order as mf-api lender controllers:
    data.sso / sso / ssoUrl / ssoUtmLink / utmLink / redirectUrl.
    """
    if apiresponse is None:
        return None

    payload = apiresponse
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", errors="ignore")
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return _as_url(text)

    if not isinstance(payload, dict):
        return None

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}

    candidates = (
        data.get("sso"),
        payload.get("sso"),
        data.get("ssoUrl"),
        data.get("sso_url"),
        payload.get("ssoUrl"),
        payload.get("sso_url"),
        payload.get("ssoUtmLink"),
        payload.get("sso_utm_link"),
        payload.get("ssoUtm"),
        data.get("ssoUtmLink"),
        data.get("sso_utm_link"),
        payload.get("utmLink"),
        payload.get("utm_link"),
        data.get("utmLink"),
        data.get("redirectUrl"),
        data.get("redirect_url"),
        payload.get("redirectUrl"),
        payload.get("redirect_url"),
    )
    for value in candidates:
        url = _as_url(value)
        if url:
            return url
    return None


def resolve_lender_url(sso_url, lender_redirect_url, application_id):
    """Prefer per-lead SSO from API logs; else mf_lenders.redirect_url."""
    url = _as_url(sso_url)
    if url:
        return url

    fallback = (lender_redirect_url or "").strip()
    if fallback:
        if "${trackierSource}" in fallback:
            fallback = fallback.replace("${trackierSource}", TRACKIER_SOURCE)
        url = _as_url(fallback)
        if url:
            return url

    return CHECK_STATUS_URL.format(application_id=application_id)


def fetch_remarketing_status_pairs(mysql_conn):
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT lender_id, status
            FROM mf_lender_status_master
            WHERE remarketing = 1
              AND lender_id IS NOT NULL
              AND status IS NOT NULL
              AND TRIM(status) != ''
            ORDER BY lender_id, status
            """
        )
        return cursor.fetchall()


def fetch_matching_targets(mysql_conn, pairs, lookback_days):
    """Return disbursal/application/user rows matching remarketing pairs."""
    if not pairs:
        return []

    clauses = []
    params = [int(lookback_days)]
    for row in pairs:
        clauses.append(
            "(d.lender_id = %s AND LOWER(TRIM(IFNULL(d.d_status, ''))) = %s)"
        )
        params.extend(
            [
                int(row["lender_id"]),
                str(row["status"]).strip().lower(),
            ]
        )
    pair_sql = " OR ".join(clauses)

    query = f"""
        SELECT
            d.id AS disbursal_id,
            d.application_id,
            d.lender_id,
            d.d_status,
            a.loan_amount,
            a.created AS application_created,
            l.lender_name,
            l.redirect_url AS lender_redirect_url,
            u.id AS user_id,
            u.name,
            {sql_aes_decrypt("u.mobile", "mobile")}
        FROM mf_disbursals AS d
        INNER JOIN application_master AS a ON a.id = d.application_id
        INNER JOIN mf_lenders AS l ON l.id = d.lender_id
        INNER JOIN mf_users AS u ON u.id = COALESCE(d.user_id, a.userid)
        WHERE d.application_id IS NOT NULL
          AND a.created >= NOW() - INTERVAL %s DAY
          AND ({pair_sql})
        ORDER BY a.created DESC, d.id DESC
    """

    with mysql_conn.cursor() as cursor:
        cursor.execute(query, tuple(params))
        return cursor.fetchall()


def fetch_sso_urls_by_app_lender(mysql_conn, targets):
    """Map (application_id, lender_id) → SSO URL from lender_api_logs.

    SSO is not a lead_master column; mf-api stores the lender response on
    lender_api_logs (linked by application_id + lenderid / lead). Prefer the
    newest log row that yields a usable URL.
    """
    pairs = {
        (int(row["application_id"]), int(row["lender_id"]))
        for row in targets
        if row.get("application_id") is not None and row.get("lender_id") is not None
    }
    if not pairs:
        return {}

    pair_list = sorted(pairs)
    placeholders = ", ".join(["(%s, %s)"] * len(pair_list))
    params = []
    for application_id, lender_id in pair_list:
        params.extend([application_id, lender_id])

    query = f"""
        SELECT application_id, lenderid AS lender_id, apiresponse
        FROM lender_api_logs
        WHERE (application_id, lenderid) IN ({placeholders})
          AND apiresponse IS NOT NULL
        ORDER BY id DESC
    """

    sso_by_pair = {}
    with mysql_conn.cursor() as cursor:
        cursor.execute(query, tuple(params))
        for row in cursor.fetchall():
            key = (int(row["application_id"]), int(row["lender_id"]))
            if key in sso_by_pair:
                continue
            url = extract_sso_url(row.get("apiresponse"))
            if url:
                sso_by_pair[key] = url
    return sso_by_pair


def build_send_jobs(targets, sso_by_pair=None):
    jobs = []
    skipped_invalid_mobile = 0
    # One message per application_id (keep first = latest created).
    seen_apps = set()
    sso_by_pair = sso_by_pair or {}
    sso_hits = 0
    redirect_hits = 0

    for row in targets:
        application_id = int(row["application_id"])
        if application_id in seen_apps:
            continue
        seen_apps.add(application_id)

        phone = format_phone(row.get("mobile"))
        if not phone:
            skipped_invalid_mobile += 1
            continue

        status = (row.get("d_status") or "").strip() or "Pending"
        lender_name = (row.get("lender_name") or "").strip() or "Lender"
        lender_id = int(row["lender_id"])
        sso_url = sso_by_pair.get((application_id, lender_id))
        offer_url = resolve_lender_url(
            sso_url,
            row.get("lender_redirect_url"),
            application_id,
        )
        if sso_url and offer_url == sso_url:
            sso_hits += 1
        elif (row.get("lender_redirect_url") or "").strip():
            redirect_hits += 1

        jobs.append(
            {
                "user_id": int(row["user_id"]),
                "application_id": application_id,
                "lender_id": lender_id,
                "phone": phone,
                "name": format_user_name(row.get("name")),
                "application_id_str": str(application_id),
                "offer_amount": format_offer_amount(row.get("loan_amount")),
                "lender_name": lender_name,
                "status": status,
                "offer_url": offer_url,
                "application_created": row.get("application_created"),
            }
        )

    return jobs, skipped_invalid_mobile, sso_hits, redirect_hits


def send_whatsapp(job):
    payload = {
        "template": WA_TEMPLATE_ID,
        "phone": job["phone"],
        "is_short_url": "0",
        "message": {
            "placeholders": [
                job["name"],
                job["application_id_str"],
                job["offer_amount"],
                job["lender_name"],
                job["status"],
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
        description=(
            "WhatsApp remarketing for mf_lender_status_master.remarketing=1"
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List recipients only; do not send",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"Application created lookback days (default {DEFAULT_LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        metavar="ID",
        help="Process only this mf_users.id",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap number of messages (0 = all)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    lookback_days = max(1, int(args.days))
    send_messages = SEND_MESSAGES and not args.dry_run

    mysql_conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        pairs = fetch_remarketing_status_pairs(mysql_conn)
        print(f"Remarketing status pairs: {len(pairs)}")
        by_lender = defaultdict(int)
        for row in pairs:
            by_lender[int(row["lender_id"])] += 1
            print(
                f"  lender_id={row['lender_id']} status={row['status']!r}"
            )
        print(f"Pairs by lender_id: {dict(by_lender)}")

        if not pairs:
            print("No remarketing=1 statuses. Nothing to send.")
            return []

        targets = fetch_matching_targets(mysql_conn, pairs, lookback_days)
        print(
            f"Matching disbursals (app created last {lookback_days}d): "
            f"{len(targets)}"
        )

        if args.user_id is not None:
            targets = [
                t for t in targets if int(t["user_id"]) == args.user_id
            ]
            print(f"Filtered to --user-id={args.user_id}: {len(targets)}")

        sso_by_pair = fetch_sso_urls_by_app_lender(mysql_conn, targets)
        print(f"SSO URLs found for app+lender pairs: {len(sso_by_pair)}")

        jobs, skipped_invalid_mobile, sso_hits, redirect_hits = build_send_jobs(
            targets, sso_by_pair
        )
        if args.limit and args.limit > 0:
            jobs = jobs[: int(args.limit)]

        print(
            f"Messages to send: {len(jobs)} "
            f"(invalid mobile={skipped_invalid_mobile}, "
            f"url_sso={sso_hits}, url_redirect={redirect_hits})"
        )
        print(f"Template: {WA_TEMPLATE_ID}")

        if not jobs:
            print("No recipients. Nothing to send.")
            return []

        if not send_messages:
            print("DRY RUN — listing recipients only:")
            for job in jobs:
                print(
                    f"  would send → {job['phone']} | "
                    f"user={job['user_id']} | "
                    f"app={job['application_id']} | "
                    f"name={job['name']} | "
                    f"amt={job['offer_amount']} | "
                    f"lender={job['lender_name']} | "
                    f"status={job['status']} | "
                    f"url={job['offer_url']}"
                )
            print(f"Total recipients: {len(jobs)}")
            return []

        sent_jobs = []
        failed = 0
        for job in jobs:
            print(
                f"Sending to {job['phone']} "
                f"(app={job['application_id']}, "
                f"lender={job['lender_name']}, status={job['status']})..."
            )
            try:
                result = send_whatsapp(job)
                print(f"  Sent: {result}")
                sent_jobs.append(job)
            except Exception as exc:
                failed += 1
                print(f"  Failed: {exc}", file=sys.stderr)

        stats = {
            "remarketing_pairs": len(pairs),
            "matching_disbursals": len(targets),
            "jobs": len(jobs),
            "skipped_invalid_mobile": skipped_invalid_mobile,
            "url_sso": sso_hits,
            "url_redirect": redirect_hits,
            "lookback_days": lookback_days,
        }
        campaign_id = insert_campaign_record(
            mysql_conn, len(sent_jobs), stats
        )
        print(
            f"Done. Sent={len(sent_jobs)}, Failed={failed}, "
            f"mf_campaigns.id={campaign_id}, stats={stats}"
        )
        return [job["phone"] for job in sent_jobs]
    finally:
        mysql_conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Status remarketing WhatsApp job failed: {exc}", file=sys.stderr)
        sys.exit(1)
