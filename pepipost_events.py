"""Fetch Pepipost / Netcore Email API event logs and sync to MySQL.

Docs:
  https://cpaasdocs.netcorecloud.com/docs/pepipost-api/20c2883924165-fetch-event-logs
  https://emaildocs.netcorecloud.com/reference/logs

API:
  GET {PEPIPOST_EVENTS_API_URL}   # default …/v5.1/events
  Header: api_key: <PEPIPOST_API_KEY>
  Query: startdate (YYYY-MM-DD, required), enddate, events, limit, scrollid, …

Pagination: use scrollid from each response until exhausted.
Max limit per request: 1000 (API schema); description mentions up to 5000.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from email.message import EmailMessage

import pymysql

from config import (
    PEPIPOST_API_KEY,
    PEPIPOST_EVENTS_API_URL,
    PEPIPOST_EVENTS_LIMIT,
    PEPIPOST_EVENTS_LOOKBACK_DAYS,
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
    db_config,
)

MYSQL_CONFIG = db_config()
REQUEST_DELAY_SECONDS = 0.5
REPORT_EMAIL_TO = ["anup@profuseservices.com"]
ALERT_EVENT_TYPES = ("softbounce", "hardbounce", "unsubscribe")

# Non-aggregate event filters from the Events API docs.
DEFAULT_EVENTS = (
    "processed",
    "sent",
    "open",
    "click",
    "unsubscribe",
    "bounce",
    "softbounce",
    "spam",
    "invalid",
    "dropped",
    "hardbounce",
)

ENSURE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mf_pepipost_events (
    id BIGINT NOT NULL AUTO_INCREMENT,
    event_key CHAR(40) NOT NULL,
    event_type VARCHAR(50) DEFAULT NULL,
    event_time DATETIME DEFAULT NULL,
    email VARCHAR(255) DEFAULT NULL,
    from_address VARCHAR(255) DEFAULT NULL,
    subject VARCHAR(500) DEFAULT NULL,
    trans_id VARCHAR(100) DEFAULT NULL,
    xapiheader VARCHAR(255) DEFAULT NULL,
    remarks TEXT,
    raw_json JSON DEFAULT NULL,
    fetched_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_pepipost_event_key (event_key),
    KEY idx_pepipost_event_time (event_time),
    KEY idx_pepipost_email (email),
    KEY idx_pepipost_event_type (event_type)
)
"""


def require_config():
    if not PEPIPOST_API_KEY:
        raise RuntimeError("PEPIPOST_API_KEY is not configured in .env")
    if not PEPIPOST_EVENTS_API_URL:
        raise RuntimeError("PEPIPOST_EVENTS_API_URL is not configured")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Fetch Pepipost/Netcore email event logs"
    )
    parser.add_argument(
        "--startdate",
        help="Start date YYYY-MM-DD (default: lookback days ago)",
    )
    parser.add_argument(
        "--enddate",
        help="End date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--events",
        help=(
            "Comma-separated event types "
            "(default: all non-aggregate events). "
            "Do not mix aggregate totals with regular events."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=PEPIPOST_EVENTS_LIMIT,
        help=f"Page size (default {PEPIPOST_EVENTS_LIMIT}, max 1000)",
    )
    parser.add_argument(
        "--email",
        help="Filter by recipient email",
    )
    parser.add_argument(
        "--fromaddress",
        help="Filter by from address",
    )
    parser.add_argument(
        "--subject",
        help="Filter by subject",
    )
    parser.add_argument(
        "--xapiheader",
        help="Filter by x-apiheader",
    )
    parser.add_argument(
        "--sort",
        choices=("asc", "desc"),
        default="asc",
        help="Sort by send time",
    )
    parser.add_argument(
        "--no-store",
        action="store_true",
        help="Fetch and print only; do not write to mf_pepipost_events",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Stop after N pages (0 = all)",
    )
    return parser.parse_args(argv)


def resolve_dates(args):
    end = date.fromisoformat(args.enddate) if args.enddate else date.today()
    if args.startdate:
        start = date.fromisoformat(args.startdate)
    else:
        start = end - timedelta(days=max(PEPIPOST_EVENTS_LOOKBACK_DAYS, 0))
    if start > end:
        raise ValueError(f"startdate {start} is after enddate {end}")
    return start, end


def resolve_events(args):
    if not args.events:
        return ",".join(DEFAULT_EVENTS)
    return ",".join(
        part.strip() for part in args.events.split(",") if part.strip()
    )


def ensure_events_table(conn):
    with conn.cursor() as cursor:
        cursor.execute(ENSURE_TABLE_SQL)
    conn.commit()


def fetch_events_page(params):
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{PEPIPOST_EVENTS_API_URL}?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "api_key": PEPIPOST_API_KEY,
            "Accept": "application/json",
            "User-Agent": "mf-utils-pepipost-events/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Pepipost events API error {exc.code}: {error_body}"
        ) from exc


def extract_rows_and_scrollid(payload):
    """Normalize varied Pepipost/Netcore response shapes."""
    if not isinstance(payload, dict):
        return [], None

    scrollid = (
        payload.get("scrollid")
        or payload.get("scrollId")
        or payload.get("scroll_id")
    )

    data = payload.get("data")
    if isinstance(data, list):
        return data, scrollid

    if isinstance(data, dict):
        scrollid = (
            data.get("scrollid")
            or data.get("scrollId")
            or data.get("scroll_id")
            or scrollid
        )
        for key in ("events", "logs", "rows", "records", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value, scrollid

    for key in ("events", "logs", "rows", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            return value, scrollid

    return [], scrollid


def first_value(row, *keys):
    for key in keys:
        if key in row and row[key] is not None:
            text = str(row[key]).strip()
            if text:
                return text
    return None


def parse_event_time(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("T", " ").replace("Z", "")
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%d-%m-%Y %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text[:26], fmt)
        except ValueError:
            continue
    return None


def normalize_event_row(row):
    if not isinstance(row, dict):
        return None

    event_type = first_value(
        row,
        "EVENT",
        "event",
        "events",
        "status",
        "STATUS",
        "event_type",
        "eventType",
    )
    email = first_value(
        row,
        "EMAIL",
        "email",
        "rcptEmail",
        "recipient",
        "TOADDRESS",
        "to",
    )
    from_address = first_value(
        row,
        "FROMADDRESS",
        "fromaddress",
        "from",
        "FROM",
    )
    subject = first_value(row, "SUBJECT", "subject")
    trans_id = first_value(
        row,
        "TRANSID",
        "transid",
        "transId",
        "trid",
        "message_id",
        "messageId",
        "MSIZE",
    )
    xapiheader = first_value(
        row,
        "X-APIHEADER",
        "XAPIHEADER",
        "xapiheader",
        "x_apiheader",
        "xApiHeader",
    )
    remarks = first_value(
        row,
        "REMARKS",
        "remarks",
        "REASON",
        "reason",
        "response",
        "error",
    )
    event_time = parse_event_time(
        first_value(
            row,
            "TIMESTAMP",
            "timestamp",
            "EVENT_TIME",
            "event_time",
            "time",
            "deliveryTime",
            "modifiedTime",
            "requestedTime",
            "DATE",
        )
    )

    raw = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
    key_material = "|".join(
        [
            str(trans_id or ""),
            str(event_type or ""),
            str(email or ""),
            str(event_time or ""),
            hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16],
        ]
    )
    event_key = hashlib.sha1(key_material.encode("utf-8")).hexdigest()

    return {
        "event_key": event_key,
        "event_type": event_type,
        "event_time": event_time,
        "email": email,
        "from_address": from_address,
        "subject": (subject[:500] if subject else None),
        "trans_id": (str(trans_id)[:100] if trans_id else None),
        "xapiheader": (xapiheader[:255] if xapiheader else None),
        "remarks": remarks,
        "raw_json": raw,
    }


def upsert_events(conn, rows):
    if not rows:
        return 0
    sql = """
        INSERT INTO mf_pepipost_events (
            event_key, event_type, event_time, email, from_address,
            subject, trans_id, xapiheader, remarks, raw_json, fetched_at
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, CAST(%s AS JSON), NOW()
        )
        ON DUPLICATE KEY UPDATE
            event_type = VALUES(event_type),
            event_time = VALUES(event_time),
            email = VALUES(email),
            from_address = VALUES(from_address),
            subject = VALUES(subject),
            trans_id = VALUES(trans_id),
            xapiheader = VALUES(xapiheader),
            remarks = VALUES(remarks),
            raw_json = VALUES(raw_json),
            fetched_at = NOW()
    """
    values = [
        (
            row["event_key"],
            row["event_type"],
            row["event_time"],
            row["email"],
            row["from_address"],
            row["subject"],
            row["trans_id"],
            row["xapiheader"],
            row["remarks"],
            row["raw_json"],
        )
        for row in rows
    ]
    with conn.cursor() as cursor:
        cursor.executemany(sql, values)
    conn.commit()
    return len(values)


def fetch_all_events(args):
    start, end = resolve_dates(args)
    events = resolve_events(args)
    limit = max(1, min(int(args.limit or 1000), 1000))

    print(f"Pepipost events URL: {PEPIPOST_EVENTS_API_URL}")
    print(f"Date range: {start} → {end}")
    print(f"Events filter: {events}")
    print(f"Page limit: {limit}")

    base_params = {
        "startdate": start.isoformat(),
        "enddate": end.isoformat(),
        "events": events,
        "limit": str(limit),
        "sort": args.sort,
    }
    if args.email:
        base_params["email"] = args.email
    if args.fromaddress:
        base_params["fromaddress"] = args.fromaddress
    if args.subject:
        base_params["subject"] = args.subject
    if args.xapiheader:
        base_params["xapiheader"] = args.xapiheader

    all_rows = []
    scrollid = None
    page = 0
    seen_scrollids = set()

    while True:
        page += 1
        params = dict(base_params)
        if scrollid:
            params["scrollid"] = scrollid

        print(f"Fetching page {page}" + (f" (scrollid={scrollid[:24]}…)" if scrollid else ""))
        payload = fetch_events_page(params)
        rows, next_scrollid = extract_rows_and_scrollid(payload)
        print(f"  Received {len(rows)} row(s)")

        if not rows:
            break

        all_rows.extend(rows)

        if args.max_pages and page >= args.max_pages:
            print(f"Stopping after --max-pages={args.max_pages}")
            break

        if not next_scrollid or next_scrollid == scrollid:
            break
        if next_scrollid in seen_scrollids:
            print("Repeated scrollid; stopping pagination")
            break
        seen_scrollids.add(next_scrollid)
        scrollid = next_scrollid
        time.sleep(REQUEST_DELAY_SECONDS)

    return all_rows


def build_summary(normalized_rows):
    by_date = {}
    overall = Counter()
    alert_emails = {event_type: [] for event_type in ALERT_EVENT_TYPES}

    for row in normalized_rows:
        event_type = (row.get("event_type") or "unknown").lower()
        overall[event_type] += 1
        event_time = row.get("event_time")
        if event_time is None:
            day = "unknown"
        else:
            day = event_time.date().isoformat()
        by_date.setdefault(day, Counter())[event_type] += 1

        if event_type in alert_emails:
            email = row.get("email")
            if email and email != "(missing email)":
                alert_emails[event_type].append(
                    (day, email, row.get("remarks"))
                )

    return {
        "total": len(normalized_rows),
        "overall": overall,
        "by_date": by_date,
        "alert_emails": alert_emails,
    }


def print_summary(summary):
    print()
    print(f"Total event rows: {summary['total']}")

    print("By event type:")
    for event_type, count in sorted(summary["overall"].items()):
        print(f"  {event_type}: {count}")

    print()
    print("By date:")
    for day in sorted(summary["by_date"]):
        counts = summary["by_date"][day]
        total = sum(counts.values())
        parts = [f"{event_type}={counts[event_type]}" for event_type in sorted(counts)]
        print(f"  {day}  total={total}  " + "  ".join(parts))

    print()
    print("Emails — softbounce / hardbounce / unsubscribe:")
    any_alert = False
    for event_type in ALERT_EVENT_TYPES:
        rows = summary["alert_emails"][event_type]
        if not rows:
            continue
        any_alert = True
        print(f"  {event_type} ({len(rows)}):")
        seen = set()
        for day, email, remarks in rows:
            key = (day, email.lower())
            if key in seen:
                continue
            seen.add(key)
            remark_part = f"  remarks={remarks}" if remarks else ""
            print(f"    {day}  {email}{remark_part}")
    if not any_alert:
        print("  (none)")


def collect_alert_email_addresses(summary):
    emails = set()
    for event_type in ALERT_EVENT_TYPES:
        for _day, email, _remarks in summary["alert_emails"][event_type]:
            text = str(email or "").strip()
            if text and "@" in text:
                emails.add(text)
    return sorted(emails, key=str.lower)


def blank_alert_emails_in_mf_users(conn, emails):
    """SET email='' in mf_users for softbounce / hardbounce / unsubscribe addresses."""
    if not emails:
        print("No softbounce/hardbounce/unsubscribe emails to blank in mf_users")
        return 0

    updated = 0
    with conn.cursor() as cursor:
        for email in emails:
            cursor.execute(
                """
                UPDATE mf_users
                SET email = ''
                WHERE email = %s
                  AND TRIM(IFNULL(email, '')) != ''
                """,
                (email,),
            )
            updated += cursor.rowcount
    conn.commit()
    print(
        f"Blanked email on {updated} mf_users row(s) "
        f"for {len(emails)} alert address(es)"
    )
    return updated


def build_report_email(summary, start, end, blanked_count, alert_emails):
    subject = (
        f"Pepipost events report {start} → {end} "
        f"({summary['total']} events)"
    )

    text_lines = [
        "Pepipost / Netcore email events report",
        f"Date range: {start} → {end}",
        f"Total event rows: {summary['total']}",
        f"mf_users emails blanked: {blanked_count}",
        "",
        "By event type:",
    ]
    for event_type, count in sorted(summary["overall"].items()):
        text_lines.append(f"  {event_type}: {count}")

    text_lines.extend(["", "By date:"])
    for day in sorted(summary["by_date"]):
        counts = summary["by_date"][day]
        total = sum(counts.values())
        parts = [f"{event_type}={counts[event_type]}" for event_type in sorted(counts)]
        text_lines.append(f"  {day}  total={total}  " + "  ".join(parts))

    text_lines.extend(
        [
            "",
            f"Blocked email-ids ({len(alert_emails)}) "
            f"[softbounce / hardbounce / unsubscribe]:",
        ]
    )
    blocked_detail_rows = []
    for event_type in ALERT_EVENT_TYPES:
        rows = summary["alert_emails"].get(event_type) or []
        if not rows:
            continue
        text_lines.append(f"  {event_type}:")
        seen = set()
        for day, email, remarks in rows:
            key = (event_type, day, email.lower())
            if key in seen:
                continue
            seen.add(key)
            remark_part = f" | {remarks}" if remarks else ""
            text_lines.append(f"    {day}  {email}{remark_part}")
            blocked_detail_rows.append((event_type, day, email, remarks))

    if not blocked_detail_rows:
        text_lines.append("  (none)")

    date_rows_html = "".join(
        (
            "<tr>"
            f"<td>{html.escape(day)}</td>"
            f"<td>{sum(counts.values())}</td>"
            + "".join(
                f"<td>{counts.get(event_type, 0)}</td>"
                for event_type in (
                    "sent",
                    "open",
                    "click",
                    "softbounce",
                    "hardbounce",
                    "dropped",
                    "unsubscribe",
                )
            )
            + "</tr>"
        )
        for day, counts in sorted(summary["by_date"].items())
    ) or "<tr><td colspan='9'>No events</td></tr>"

    overall_rows_html = "".join(
        f"<tr><td>{html.escape(event_type)}</td><td>{count}</td></tr>"
        for event_type, count in sorted(summary["overall"].items())
    ) or "<tr><td colspan='2'>No events</td></tr>"

    blocked_list_html = "".join(
        f"<tr><td>{html.escape(email)}</td></tr>" for email in alert_emails
    ) or "<tr><td>(none)</td></tr>"

    blocked_detail_html = "".join(
        (
            "<tr>"
            f"<td>{html.escape(event_type)}</td>"
            f"<td>{html.escape(day)}</td>"
            f"<td>{html.escape(email)}</td>"
            f"<td>{html.escape(str(remarks or ''))}</td>"
            "</tr>"
        )
        for event_type, day, email, remarks in blocked_detail_rows
    ) or "<tr><td colspan='4'>(none)</td></tr>"

    html_body = f"""
<html>
  <body>
    <h2>Pepipost email events report</h2>
    <p><strong>Date range:</strong> {html.escape(str(start))} → {html.escape(str(end))}</p>
    <p><strong>Total event rows:</strong> {summary['total']}</p>
    <p><strong>mf_users emails blanked:</strong> {blanked_count}</p>

    <h3>By event type</h3>
    <table border="1" cellpadding="8" cellspacing="0">
      <tr><th>Event</th><th>Count</th></tr>
      {overall_rows_html}
    </table>

    <h3>By date</h3>
    <table border="1" cellpadding="8" cellspacing="0">
      <tr>
        <th>Date</th>
        <th>Total</th>
        <th>Sent</th>
        <th>Open</th>
        <th>Click</th>
        <th>Softbounce</th>
        <th>Hardbounce</th>
        <th>Dropped</th>
        <th>Unsubscribe</th>
      </tr>
      {date_rows_html}
    </table>

    <h3>Blocked email-ids ({len(alert_emails)})</h3>
    <p>Addresses with softbounce / hardbounce / unsubscribe — cleared in mf_users.</p>
    <table border="1" cellpadding="8" cellspacing="0">
      <tr><th>Email</th></tr>
      {blocked_list_html}
    </table>

    <h3>Blocked email-ids — detail</h3>
    <table border="1" cellpadding="8" cellspacing="0">
      <tr><th>Event</th><th>Date</th><th>Email</th><th>Remarks</th></tr>
      {blocked_detail_html}
    </table>
  </body>
</html>
""".strip()

    return subject, "\n".join(text_lines), html_body


def send_report_email(subject, text_body, html_body, to_emails):
    if not SMTP_USER or not SMTP_PASSWORD:
        raise RuntimeError(
            "SMTP is not configured. Set SMTP_USER and SMTP_PASSWORD in .env"
        )
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(to_emails)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
    print(f"Report email sent to {', '.join(to_emails)}: {subject}")


def process_pepipost_events(argv=None):
    require_config()
    args = parse_args(argv)
    start, end = resolve_dates(args)
    raw_rows = fetch_all_events(args)

    normalized = []
    for row in raw_rows:
        item = normalize_event_row(row)
        if item:
            normalized.append(item)

    summary = build_summary(normalized)
    print_summary(summary)
    alert_emails = collect_alert_email_addresses(summary)

    conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        if not args.no_store:
            ensure_events_table(conn)
            stored = upsert_events(conn, normalized)
            print(f"Upserted {stored} row(s) into mf_pepipost_events")
        else:
            print("Skipped DB store (--no-store)")

        blanked_count = blank_alert_emails_in_mf_users(conn, alert_emails)
    finally:
        conn.close()

    subject, text_body, html_body = build_report_email(
        summary, start, end, blanked_count, alert_emails
    )
    send_report_email(subject, text_body, html_body, REPORT_EMAIL_TO)


if __name__ == "__main__":
    try:
        process_pepipost_events()
    except Exception as exc:
        print(f"Pepipost events sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
