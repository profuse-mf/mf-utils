"""Fetch Trackier clicks and insert into mf_lender_rediections_stats.

API: GET https://api.trackier.com/v2/reports/clicks
Docs: https://api-docs.trackier.io/docs/perf-admin-api-docs/e4a1e1388a249-clicks-report

Mapping campaign_id → lender_id:
  134 → 5 (Salary On Time)
  187 → 6
  200 → 4
  211 → 1 if BRE eligible for lender 1, else 7 if BRE eligible for lender 7, else skip
  212 → 3
  221 → 9
  227 → 11 (CASHe)
  234 → 13 (PayMe)
  235 → 12 (CreditSea)
  236 → 14 (Rupeedhan)

Unmapped campaign_ids trigger an alert email to MF_REPORT_EMAIL_TO
(same daily-report recipients used by mis_new.py / daily ops).
"""

import json
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from email.message import EmailMessage

import pymysql

from config import (
    MF_REPORT_EMAIL_TO,
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
    TRACKIER_API_KEY,
    TRACKIER_CLICKS_API_URL,
    TRACKIER_CLICKS_LIMIT,
    TRACKIER_CLICKS_LOOKBACK_DAYS,
    TRACKIER_PUB_IDS,
    TRACKIER_TIMEZONE,
    db_config,
)

MYSQL_CONFIG = db_config()
CLICK_FIELDS = ("campaign_id", "p1")

# Static campaign_id → lender_id (except 211, resolved via BRE)
CAMPAIGN_LENDER_MAP = {
    134: 5,   # Salary On Time
    187: 6,
    200: 4,
    212: 3,
    221: 9,
    227: 11,  # CASHe
    234: 13,  # PayMe
    235: 12,  # CreditSea
    236: 14,  # Rupeedhan
}
CAMPAIGN_211 = 211
CAMPAIGN_211_LENDER_PREFERENCE = (1, 7)


def require_config():
    missing = []
    if not TRACKIER_API_KEY:
        missing.append("TRACKIER_API_KEY")
    if not TRACKIER_PUB_IDS:
        missing.append("TRACKIER_PUB_IDS")
    if missing:
        raise RuntimeError(
            "Trackier config missing. Set in .env: " + ", ".join(missing)
        )


def report_email_recipients():
    """Daily report recipients (mis_new / MF_REPORT_EMAIL_TO)."""
    recipients = []
    for email in MF_REPORT_EMAIL_TO:
        if email.strip().lower() == "anup.vaze@appkhichadi.com":
            recipients.append("anup@profuseservices.com")
        else:
            recipients.append(email)
    return list(dict.fromkeys(recipients))


def build_query(start_date, end_date, page_token=None):
    params = [
        ("apiKey", TRACKIER_API_KEY),
        ("start", start_date.isoformat()),
        ("end", end_date.isoformat()),
        ("zone", TRACKIER_TIMEZONE),
        ("limit", str(TRACKIER_CLICKS_LIMIT)),
    ]
    for pub_id in TRACKIER_PUB_IDS:
        params.append(("pub_ids[]", str(pub_id)))
    for field in CLICK_FIELDS:
        params.append(("fields[]", field))
    if page_token:
        params.append(("pageToken", page_token))
    return urllib.parse.urlencode(params)


def fetch_clicks_page(start_date, end_date, page_token=None):
    query = build_query(start_date, end_date, page_token=page_token)
    url = f"{TRACKIER_CLICKS_API_URL}?{query}"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Trackier clicks API error {exc.code}: {error_body}"
        ) from exc


def fetch_all_clicks(start_date, end_date):
    clicks = []
    page_token = None
    page = 1

    while True:
        print(f"Fetching page {page}...")
        payload = fetch_clicks_page(start_date, end_date, page_token=page_token)
        page_clicks = payload.get("clicks") or []
        if not isinstance(page_clicks, list):
            raise RuntimeError(
                f"Unexpected clicks payload type: {type(page_clicks).__name__}"
            )

        clicks.extend(page_clicks)
        print(f"  Received {len(page_clicks)} click(s) (total={len(clicks)})")

        has_next = bool(payload.get("hasNextPage"))
        page_token = payload.get("nextPageToken") or payload.get("pageToken")
        if not has_next or not page_token:
            break
        page += 1

    return clicks


def normalize_campaign_id(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_application_id(value):
    if value is None or value == "":
        return None
    try:
        application_id = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if application_id <= 0:
        return None
    return application_id


def is_empty_criteria_missed(criteria_missed):
    if criteria_missed is None:
        return True
    if isinstance(criteria_missed, (bytes, bytearray)):
        criteria_missed = criteria_missed.decode("utf-8", errors="replace")
    if isinstance(criteria_missed, str):
        text = criteria_missed.strip()
        if not text:
            return True
        try:
            criteria_missed = json.loads(text)
        except json.JSONDecodeError:
            return False
    if isinstance(criteria_missed, dict):
        return len(criteria_missed) == 0
    if isinstance(criteria_missed, (list, tuple)):
        return len(criteria_missed) == 0
    return False


def normalize_click_rows(clicks):
    rows = []
    for click in clicks:
        if not isinstance(click, dict):
            continue
        rows.append(
            {
                "campaign_id": normalize_campaign_id(click.get("campaign_id")),
                "p1": normalize_application_id(click.get("p1")),
                "raw_campaign_id": click.get("campaign_id"),
                "raw_p1": click.get("p1"),
            }
        )
    return rows


def print_summary(rows, start_date, end_date):
    with_p1 = sum(1 for row in rows if row.get("p1") is not None)
    with_campaign = sum(1 for row in rows if row.get("campaign_id") is not None)
    unique_campaign_ids = sorted(
        {row["campaign_id"] for row in rows if row.get("campaign_id") is not None}
    )

    print()
    print(f"Date range: {start_date} → {end_date} ({TRACKIER_TIMEZONE})")
    print(f"Publishers: {', '.join(TRACKIER_PUB_IDS)}")
    print(f"Total clicks: {len(rows)}")
    print(f"With campaign_id: {with_campaign}")
    print(f"With p1: {with_p1}")
    print(f"Unique campaign_ids ({len(unique_campaign_ids)}):")
    for campaign_id in unique_campaign_ids:
        print(f"  {campaign_id}")


def fetch_existing_pairs(mysql_conn, application_ids):
    if not application_ids:
        return set()

    existing = set()
    application_ids = sorted(set(application_ids))
    chunk_size = 500
    with mysql_conn.cursor() as cursor:
        for index in range(0, len(application_ids), chunk_size):
            chunk = application_ids[index : index + chunk_size]
            placeholders = ", ".join(["%s"] * len(chunk))
            cursor.execute(
                f"""
                SELECT lender_id, application_id
                FROM mf_lender_rediections_stats
                WHERE application_id IN ({placeholders})
                """,
                tuple(chunk),
            )
            for row in cursor.fetchall():
                if row["lender_id"] is None or row["application_id"] is None:
                    continue
                existing.add((int(row["lender_id"]), int(row["application_id"])))
    return existing


def fetch_bre_eligible_lenders(mysql_conn, application_ids, lender_ids):
    """
    Return {application_id: set(lender_id)} where criteria_missed is empty
    for the given lenders.
    """
    if not application_ids or not lender_ids:
        return {}

    result = {}
    application_ids = sorted(set(application_ids))
    lender_ids = tuple(lender_ids)
    chunk_size = 500

    with mysql_conn.cursor() as cursor:
        for index in range(0, len(application_ids), chunk_size):
            chunk = application_ids[index : index + chunk_size]
            app_placeholders = ", ".join(["%s"] * len(chunk))
            lender_placeholders = ", ".join(["%s"] * len(lender_ids))
            cursor.execute(
                f"""
                SELECT application_id, lender_id, criteria_missed
                FROM application_bre_logs
                WHERE application_id IN ({app_placeholders})
                  AND lender_id IN ({lender_placeholders})
                """,
                tuple(chunk) + lender_ids,
            )
            for row in cursor.fetchall():
                if not is_empty_criteria_missed(row.get("criteria_missed")):
                    continue
                application_id = int(row["application_id"])
                lender_id = int(row["lender_id"])
                result.setdefault(application_id, set()).add(lender_id)

    return result


def resolve_lender_id(campaign_id, application_id, bre_eligible_by_app):
    if campaign_id in CAMPAIGN_LENDER_MAP:
        return CAMPAIGN_LENDER_MAP[campaign_id], None

    if campaign_id == CAMPAIGN_211:
        eligible = bre_eligible_by_app.get(application_id, set())
        for lender_id in CAMPAIGN_211_LENDER_PREFERENCE:
            if lender_id in eligible:
                return lender_id, None
        return None, "campaign_211_no_bre_match"

    return None, "unmapped_campaign"


def insert_redirection(mysql_conn, lender_id, application_id):
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO mf_lender_rediections_stats (lender_id, application_id)
            VALUES (%s, %s)
            """,
            (lender_id, application_id),
        )
    mysql_conn.commit()


def send_unmapped_campaign_email(unmapped_campaign_ids, start_date, end_date):
    if not unmapped_campaign_ids:
        return

    to_emails = report_email_recipients()
    if not to_emails:
        print(
            "WARNING: unmapped campaign_ids found but MF_REPORT_EMAIL_TO is empty",
            file=sys.stderr,
        )
        return
    if not SMTP_USER or not SMTP_PASSWORD:
        raise RuntimeError(
            "SMTP is not configured. Set SMTP_USER and SMTP_PASSWORD in .env"
        )

    campaign_list = ", ".join(str(cid) for cid in sorted(unmapped_campaign_ids))
    subject = (
        f"Trackier sync: unmapped campaign_ids "
        f"({start_date} → {end_date})"
    )
    body = (
        f"Trackier clicks sync found campaign_id(s) that are not mapped "
        f"to any lender.\n\n"
        f"Date range: {start_date} → {end_date} ({TRACKIER_TIMEZONE})\n"
        f"Publishers: {', '.join(TRACKIER_PUB_IDS)}\n\n"
        f"Unmapped campaign_id(s):\n{campaign_list}\n\n"
        f"Please add mapping(s) in sync_trackier_clicks.py.\n"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(to_emails)
    msg.set_content(body)

    print(
        f"Sending unmapped-campaign alert via {SMTP_HOST}:{SMTP_PORT} "
        f"→ {', '.join(to_emails)}"
    )
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
    print("Unmapped-campaign alert email sent")


def process_and_insert_clicks(mysql_conn, rows):
    inserted = 0
    skipped_existing = 0
    skipped_invalid = 0
    skipped_211 = 0
    unmapped_campaign_ids = set()

    valid_rows = []
    campaign_211_app_ids = set()

    for row in rows:
        campaign_id = row.get("campaign_id")
        application_id = row.get("p1")
        if campaign_id is None or application_id is None:
            skipped_invalid += 1
            continue
        valid_rows.append(row)
        if campaign_id == CAMPAIGN_211:
            campaign_211_app_ids.add(application_id)

    bre_eligible_by_app = fetch_bre_eligible_lenders(
        mysql_conn,
        campaign_211_app_ids,
        CAMPAIGN_211_LENDER_PREFERENCE,
    )

    application_ids = [row["p1"] for row in valid_rows]
    existing_pairs = fetch_existing_pairs(mysql_conn, application_ids)
    seen_this_run = set()

    for row in valid_rows:
        campaign_id = row["campaign_id"]
        application_id = row["p1"]
        lender_id, skip_reason = resolve_lender_id(
            campaign_id, application_id, bre_eligible_by_app
        )

        if lender_id is None:
            if skip_reason == "unmapped_campaign":
                unmapped_campaign_ids.add(campaign_id)
            elif skip_reason == "campaign_211_no_bre_match":
                skipped_211 += 1
            else:
                skipped_invalid += 1
            continue

        pair = (lender_id, application_id)
        if pair in existing_pairs or pair in seen_this_run:
            skipped_existing += 1
            continue

        insert_redirection(mysql_conn, lender_id, application_id)
        existing_pairs.add(pair)
        seen_this_run.add(pair)
        inserted += 1

    return {
        "inserted": inserted,
        "skipped_existing": skipped_existing,
        "skipped_invalid": skipped_invalid,
        "skipped_211": skipped_211,
        "unmapped_campaign_ids": unmapped_campaign_ids,
    }


def sync_trackier_clicks():
    require_config()

    end_date = date.today()
    start_date = end_date - timedelta(days=TRACKIER_CLICKS_LOOKBACK_DAYS)

    print(
        f"Calling Trackier clicks report "
        f"(start={start_date}, end={end_date}, zone={TRACKIER_TIMEZONE})"
    )
    clicks = fetch_all_clicks(start_date, end_date)
    rows = normalize_click_rows(clicks)
    print_summary(rows, start_date, end_date)

    mysql_conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        stats = process_and_insert_clicks(mysql_conn, rows)
    finally:
        mysql_conn.close()

    print()
    print(
        f"Insert stats: inserted={stats['inserted']}, "
        f"skipped_existing={stats['skipped_existing']}, "
        f"skipped_invalid_p1_or_campaign={stats['skipped_invalid']}, "
        f"skipped_campaign_211_no_bre={stats['skipped_211']}"
    )

    if stats["unmapped_campaign_ids"]:
        print(
            "Unmapped campaign_ids: "
            + ", ".join(str(cid) for cid in sorted(stats["unmapped_campaign_ids"]))
        )
        send_unmapped_campaign_email(
            stats["unmapped_campaign_ids"], start_date, end_date
        )
    else:
        print("No unmapped campaign_ids")

    return rows


if __name__ == "__main__":
    try:
        sync_trackier_clicks()
    except Exception as exc:
        print(f"Trackier clicks sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
