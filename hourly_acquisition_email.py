"""Hourly acquisition summary email.

Counts for the previous complete clock hour (e.g. run at 10:02 → 09:00–10:00):
  - users created (mf_users.created — datetime; not created_date)
  - applications received (application_master.created)

Recipients: MF_REPORT_EMAIL_TO (same list as Trackier / MIS reports).
SMTP: SMTP_HOST / SMTP_USER / SMTP_PASSWORD from .env.

Cron example (2nd minute of every hour):
  2 * * * * cd /var/mf-utils && ./venv/bin/python3 hourly_acquisition_email.py
"""

from __future__ import annotations

import argparse
import html
import smtplib
import sys
from datetime import datetime, timedelta
from email.message import EmailMessage

import pymysql

from config import (
    MF_REPORT_EMAIL_TO,
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
    db_config,
)

MYSQL_CONFIG = db_config()


def report_email_recipients():
    """Same recipient remapping as sync_trackier_clicks / MIS."""
    recipients = []
    for email in MF_REPORT_EMAIL_TO:
        if email.strip().lower() == "anup.vaze@appkhichadi.com":
            recipients.append("anup@profuseservices.com")
        else:
            recipients.append(email)
    return list(dict.fromkeys(recipients))


def previous_hour_window(now=None):
    """Return [start, end) for the previous complete clock hour."""
    now = now or datetime.now()
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=1)
    return start, end


def fetch_counts(mysql_conn, start, end):
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM mf_users
            WHERE created >= %s
              AND created < %s
            """,
            (start, end),
        )
        users = int(cursor.fetchone()["cnt"])

        cursor.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM application_master
            WHERE created >= %s
              AND created < %s
            """,
            (start, end),
        )
        applications = int(cursor.fetchone()["cnt"])

    return users, applications


def build_email(start, end, users, applications):
    window = f"{start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%Y-%m-%d %H:%M')}"
    subject = f"MF hourly acquisition ({start.strftime('%d %b %H:%M')}–{end.strftime('%H:%M')})"

    text_body = (
        f"MoneyFatafat hourly acquisition\n"
        f"Window: {window}\n\n"
        f"Users created:        {users}\n"
        f"Applications received: {applications}\n"
    )

    html_body = f"""
<html>
  <body style="font-family: Arial, sans-serif; color: #222;">
    <h2 style="margin-bottom: 4px;">MoneyFatafat hourly acquisition</h2>
    <p style="margin-top: 0; color: #555;">Window: {html.escape(window)}</p>
    <table cellpadding="8" cellspacing="0" border="1"
           style="border-collapse: collapse; border-color: #ccc;">
      <tr style="background: #f5f5f5;">
        <th align="left">Metric</th>
        <th align="right">Count</th>
      </tr>
      <tr>
        <td>Users created</td>
        <td align="right">{users}</td>
      </tr>
      <tr>
        <td>Applications received</td>
        <td align="right">{applications}</td>
      </tr>
    </table>
  </body>
</html>
""".strip()

    return subject, text_body, html_body


def send_report_email(subject, text_body, html_body, to_emails):
    if not to_emails:
        raise RuntimeError("MF_REPORT_EMAIL_TO is empty in .env")
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

    print(
        f"Sending via {SMTP_HOST}:{SMTP_PORT} → {', '.join(to_emails)}"
    )
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
    print(f"Email sent: {subject}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Email hourly user + application acquisition counts"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print counts only; do not send email",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    start, end = previous_hour_window()

    mysql_conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        users, applications = fetch_counts(mysql_conn, start, end)
    finally:
        mysql_conn.close()

    subject, text_body, html_body = build_email(
        start, end, users, applications
    )
    print(text_body)

    if args.dry_run:
        print("DRY RUN — email not sent")
        return

    send_report_email(
        subject, text_body, html_body, report_email_recipients()
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Hourly acquisition email failed: {exc}", file=sys.stderr)
        sys.exit(1)
