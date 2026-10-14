"""Retarget partial signup users (mf_users.status = 0) via Email or WhatsApp.

Audience (same for both channels):
  mf_users with status = 0 whose created_date falls on D-1, D-2, or D-3
  (today excluded). Example: today = 12 Oct → target 9, 10, 11 Oct.

Usage:
  python3 partial_user_retargeting.py --channel email
  python3 partial_user_retargeting.py --channel whatsapp
  python3 partial_user_retargeting.py --channel email --dry-run
  python3 partial_user_retargeting.py --show-template
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, timedelta

import pymysql
import requests

from config import (
    PEPIPOST_API_KEY,
    PEPIPOST_FROM_EMAIL,
    PEPIPOST_FROM_NAME,
    WA_API_KEY as CONFIG_WA_API_KEY,
    WA_API_URL as CONFIG_WA_API_URL,
    WA_TEMPLATE_ID as CONFIG_WA_TEMPLATE_ID,
    db_config,
)
from mf_user_crypto import sql_aes_decrypt

MYSQL_CONFIG = db_config()

# Lookback: exact calendar days today-3 … today-1 (3 days, excluding today).
PARTIAL_DAY_OFFSETS = (1, 2, 3)

APPLY_URL = "https://moneyfatafat.com/apply"
EMAIL_SOURCE = "partial_email_retargeting"
WA_SOURCE = "partial_wa_retargeting"

WA_API_URL = CONFIG_WA_API_URL or "https://utilsapi.smsmsg.in/waba/sendmessage"
WA_API_KEY = CONFIG_WA_API_KEY or "e6eb44d10c5bea3233cf88e6dfa2b234"
# Same style as wa_daily_reminder: placeholders=[name], button → /apply
WA_TEMPLATE_ID = CONFIG_WA_TEMPLATE_ID

CAMPAIGN_NAME = "Partial User Retargeting"
EMAIL_TEMPLATE_LABEL = "PARTIAL-TEMPL-1"

OFFER_TYPES = (
    {
        "title": "Instant Personal Loan",
        "detail": "Get funds in hours from RBI-registered partners",
        "range": "₹5,000 – ₹5 Lakh",
    },
    {
        "title": "Salary-Linked Advance",
        "detail": "Quick top-ups matched to your monthly income",
        "range": "₹5,000 – ₹1 Lakh",
    },
    {
        "title": "Emergency Cash",
        "detail": "Short-term credit when you need it most",
        "range": "₹10,000 – ₹5 Lakh",
    },
    {
        "title": "Flexible Tenure Options",
        "detail": "Choose a repayment plan that fits your budget",
        "range": "7 days – 36 months",
    },
)

EMAIL_SUBJECT = "Complete your pending application to avail instant loan offers"

EMAIL_BODY_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Finish your application</title>
</head>
<body style="margin:0;padding:0;background-color:#f0f4f8;font-family:Arial,Helvetica,sans-serif;color:#1a1a1a;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color:#f0f4f8;">
    <tr>
      <td align="center" style="padding:28px 14px;">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
               style="max-width:600px;background-color:#ffffff;border-radius:14px;overflow:hidden;">

          <!-- Header -->
          <tr>
            <td align="center" style="background:linear-gradient(135deg,#0b57d0 0%,#0842a0 100%);padding:28px 24px;">
              <p style="margin:0 0 6px;color:#cfe0ff;font-size:12px;letter-spacing:1.2px;text-transform:uppercase;">
                Almost there
              </p>
              <h1 style="margin:0;color:#ffffff;font-size:28px;line-height:36px;font-weight:bold;">
                MoneyFatafat
              </h1>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:32px 28px 12px;">
              <p style="margin:0 0 16px;font-size:17px;line-height:26px;">
                Hey {name},
              </p>
              <p style="margin:0 0 18px;font-size:16px;line-height:26px;color:#333333;">
                You started your loan application but left halfway. No worries —
                your progress is waiting, and we still have
                <strong>multiple offers</strong> lined up for you.
              </p>
              <p style="margin:0 0 22px;font-size:16px;line-height:26px;color:#333333;">
                Finish in a few minutes and unlock options from trusted lending partners.
              </p>
            </td>
          </tr>

          <!-- Offer types -->
          <tr>
            <td style="padding:0 28px 8px;">
              <p style="margin:0 0 14px;font-size:13px;font-weight:bold;letter-spacing:0.6px;
                         text-transform:uppercase;color:#0b57d0;">
                Offers waiting for you
              </p>
              {offer_rows}
            </td>
          </tr>

          <!-- CTA -->
          <tr>
            <td align="center" style="padding:20px 28px 28px;">
              <a href="{url}"
                 style="display:inline-block;background-color:#0b57d0;color:#ffffff;
                        text-decoration:none;font-size:16px;font-weight:bold;
                        padding:15px 34px;border-radius:10px;">
                Complete Your Application
              </a>
              <p style="margin:16px 0 0;color:#666666;font-size:13px;line-height:20px;">
                Takes just a few minutes · 100% digital · No paperwork
              </p>
            </td>
          </tr>

          <!-- Sign-off -->
          <tr>
            <td style="padding:0 28px 28px;">
              <p style="margin:0;font-size:15px;line-height:24px;color:#333333;">
                Warm regards,<br>
                <strong>Team MoneyFatafat</strong>
              </p>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td align="center" style="background-color:#f8f9fb;padding:18px 24px;border-top:1px solid #e8ecf1;">
              <p style="margin:0;color:#888888;font-size:11px;line-height:18px;">
                Loan amount, interest rate, tenure, and final approval are subject to
                each lender's eligibility criteria and verification process.
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
""".strip()

OFFER_ROW_TEMPLATE = """
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
       style="margin-bottom:10px;background-color:#f5f8ff;border:1px solid #d9e6ff;border-radius:10px;">
  <tr>
    <td style="padding:14px 16px;">
      <p style="margin:0 0 4px;font-size:15px;font-weight:bold;color:#0b57d0;">{title}</p>
      <p style="margin:0 0 4px;font-size:13px;line-height:20px;color:#444444;">{detail}</p>
      <p style="margin:0;font-size:13px;color:#1a1a1a;"><strong>{range}</strong></p>
    </td>
  </tr>
</table>
""".strip()


def resolve_target_dates(today=None):
    """Return sorted dates for D-3, D-2, D-1 relative to today."""
    today = today or date.today()
    return sorted(today - timedelta(days=offset) for offset in PARTIAL_DAY_OFFSETS)


def format_user_name(name):
    if not name or not str(name).strip():
        return "there"
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


def build_apply_url(source, user_id):
    return f"{APPLY_URL}?source={source}&p1={int(user_id)}"


def build_offer_rows_html():
    return "\n".join(
        OFFER_ROW_TEMPLATE.format(
            title=offer["title"],
            detail=offer["detail"],
            range=offer["range"],
        )
        for offer in OFFER_TYPES
    )


def build_email_html(name, url):
    return EMAIL_BODY_TEMPLATE.format(
        name=format_user_name(name),
        url=url,
        offer_rows=build_offer_rows_html(),
    )


def fetch_partial_users(mysql_conn, target_dates):
    if not target_dates:
        return []
    placeholders = ", ".join(["%s"] * len(target_dates))
    query = f"""
        SELECT
            u.id AS user_id,
            u.name,
            u.created_date,
            {sql_aes_decrypt("u.email", "email")},
            {sql_aes_decrypt("u.mobile", "mobile")}
        FROM mf_users AS u
        WHERE u.status = 0
          AND DATE(u.created_date) IN ({placeholders})
        ORDER BY u.id
    """
    with mysql_conn.cursor() as cursor:
        cursor.execute(query, tuple(target_dates))
        return cursor.fetchall()


def build_email_jobs(users):
    jobs = []
    skipped = 0
    for user in users:
        email = (user.get("email") or "").strip()
        if not email or "@" not in email:
            skipped += 1
            continue
        user_id = int(user["user_id"])
        url = build_apply_url(EMAIL_SOURCE, user_id)
        name = format_user_name(user.get("name"))
        jobs.append(
            {
                "user_id": user_id,
                "email": email,
                "name": name,
                "subject": EMAIL_SUBJECT,
                "html_body": build_email_html(name, url),
                "offer_url": url,
                "created_date": user.get("created_date"),
            }
        )
    return jobs, skipped


def build_wa_jobs(users):
    jobs = []
    skipped = 0
    for user in users:
        phone = format_phone(user.get("mobile"))
        if not phone:
            skipped += 1
            continue
        user_id = int(user["user_id"])
        jobs.append(
            {
                "user_id": user_id,
                "phone": phone,
                "name": format_user_name(user.get("name")),
                "offer_url": build_apply_url(WA_SOURCE, user_id),
                "created_date": user.get("created_date"),
            }
        )
    return jobs, skipped


def _pepipost_modules():
    try:
        from pepipost.exceptions.api_exception import APIException
        from pepipost.models.content import Content
        from pepipost.models.email_struct import EmailStruct
        from pepipost.models.mfrom import From
        from pepipost.models.personalizations import Personalizations
        from pepipost.models.send import Send
        from pepipost.models.type_enum import TypeEnum
        from pepipost.pepipost_client import PepipostClient
    except ImportError as exc:
        raise RuntimeError(
            "pepipost package is required for --channel email. "
            "Install requirements.txt first."
        ) from exc
    return {
        "APIException": APIException,
        "Content": Content,
        "EmailStruct": EmailStruct,
        "From": From,
        "Personalizations": Personalizations,
        "Send": Send,
        "TypeEnum": TypeEnum,
        "PepipostClient": PepipostClient,
    }


def build_send_request(to_email, subject, html_body):
    m = _pepipost_modules()
    body = m["Send"]()
    body.mfrom = m["From"]()
    body.mfrom.email = PEPIPOST_FROM_EMAIL
    body.mfrom.name = PEPIPOST_FROM_NAME
    body.subject = subject

    body.content = [m["Content"]()]
    body.content[0].mtype = m["TypeEnum"].HTML
    body.content[0].value = html_body

    personalization = m["Personalizations"]()
    recipient = m["EmailStruct"]()
    recipient.email = to_email
    recipient.name = to_email.split("@")[0]
    personalization.to = [recipient]

    body.personalizations = [personalization]
    body.tags = ["MoneyFatafat", "PartialUserRetargeting"]
    return body


def send_email_via_pepipost(to_email, subject, html_body):
    if not PEPIPOST_API_KEY:
        raise RuntimeError("PEPIPOST_API_KEY is not configured in .env")
    m = _pepipost_modules()
    client = m["PepipostClient"](PEPIPOST_API_KEY)
    mail_send_controller = client.mail_send
    body = build_send_request(to_email, subject, html_body)
    return mail_send_controller.create_generatethemailsendrequest(body)


def send_whatsapp(job):
    if not WA_API_URL or not WA_API_KEY:
        raise RuntimeError("WA_API_URL / WA_API_KEY not configured")
    if not WA_TEMPLATE_ID:
        raise RuntimeError(
            "WA_TEMPLATE_ID is not set in .env "
            "(partial WA uses name + apply-button template)"
        )
    payload = {
        "template": WA_TEMPLATE_ID,
        "phone": job["phone"],
        "is_short_url": "0",
        "message": {
            "placeholders": [job["name"]],
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
    if body and body.get("status") not in (True, "true", "success"):
        raise RuntimeError(f"WA API rejected message: {response.text}")
    return body if body else {"http_status": response.status_code}


def format_pepipost_response(result):
    if result is None:
        return "null"
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False, default=str)
    text = str(result)
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, default=str)
    except (TypeError, ValueError, json.JSONDecodeError):
        return text


def build_date_stats(sent_jobs):
    counts = Counter()
    for job in sent_jobs:
        created = job.get("created_date")
        if hasattr(created, "isoformat"):
            key = created.isoformat()[:10]
        else:
            key = str(created)[:10] if created else "unknown"
        counts[key] += 1
    return dict(sorted(counts.items()))


def insert_campaign_record(mysql_conn, channel, submitted_count, stats, template):
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO mf_campaigns
                (channel, submitted_count, stats, created, template, campaign_name)
            VALUES
                (%s, %s, CAST(%s AS JSON), NOW(), %s, %s)
            """,
            (
                channel,
                int(submitted_count),
                json.dumps(stats, ensure_ascii=False),
                template,
                CAMPAIGN_NAME,
            ),
        )
        campaign_id = cursor.lastrowid
    mysql_conn.commit()
    return campaign_id


def normalize_channel(raw):
    value = (raw or "").strip().lower()
    if value in {"email", "e", "mail"}:
        return "email"
    if value in {"whatsapp", "wa", "w"}:
        return "whatsapp"
    raise ValueError(
        f"Unsupported channel {raw!r}. Use --channel email or --channel whatsapp"
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Retarget partial users (status=0) on D-1..D-3 via Email or WhatsApp"
    )
    parser.add_argument(
        "--channel",
        required=False,
        help="Delivery channel: email | whatsapp",
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
        help="Send only to this mf_users.id (after filters)",
    )
    parser.add_argument(
        "--show-template",
        action="store_true",
        help="Print the Email HTML template sample and exit",
    )
    args = parser.parse_args(argv)
    if args.show_template:
        return args
    if not args.channel:
        parser.error("--channel is required (email | whatsapp)")
    try:
        args.channel = normalize_channel(args.channel)
    except ValueError as exc:
        parser.error(str(exc))
    if args.user_id is not None and args.user_id < 1:
        parser.error("--user-id must be >= 1")
    return args


def show_email_template():
    sample_url = build_apply_url(EMAIL_SOURCE, 0)
    html = build_email_html("Anup", sample_url)
    print("Subject:", EMAIL_SUBJECT)
    print("CTA:", sample_url)
    print("Offer types:")
    for offer in OFFER_TYPES:
        print(f"  - {offer['title']}: {offer['detail']} ({offer['range']})")
    print()
    print("--- HTML ---")
    print(html)


def process_partial_user_retargeting(argv=None):
    args = parse_args(argv)
    if args.show_template:
        show_email_template()
        return []

    channel = args.channel
    target_dates = resolve_target_dates()
    mysql_conn = pymysql.connect(**MYSQL_CONFIG)

    try:
        print(f"Channel: {channel}")
        print(
            "Target dates (partial users): "
            + ", ".join(d.isoformat() for d in target_dates)
        )
        users = fetch_partial_users(mysql_conn, target_dates)
        print(f"Partial users (status=0) fetched: {len(users)}")

        if channel == "email":
            jobs, skipped = build_email_jobs(users)
            skip_label = "missing/invalid email"
            campaign_channel = "Email"
            template_label = EMAIL_TEMPLATE_LABEL
        else:
            jobs, skipped = build_wa_jobs(users)
            skip_label = "missing/invalid mobile"
            campaign_channel = "WA"
            template_label = str(WA_TEMPLATE_ID)

        print(
            f"Messages to send: {len(jobs)} "
            f"(skipped {skip_label}={skipped})"
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

        if args.dry_run:
            print("DRY RUN — listing recipients only:")
            for job in jobs:
                if channel == "email":
                    print(
                        f"  would email → {job['email']} | "
                        f"user_id={job['user_id']} | "
                        f"name={job['name']} | "
                        f"created={job.get('created_date')} | "
                        f"url={job['offer_url']}"
                    )
                else:
                    print(
                        f"  would WA → {job['phone']} | "
                        f"user_id={job['user_id']} | "
                        f"name={job['name']} | "
                        f"created={job.get('created_date')} | "
                        f"url={job['offer_url']}"
                    )
            return []

        sent_jobs = []
        failed = 0
        for job in jobs:
            try:
                if channel == "email":
                    print(
                        f"Sending email to {job['email']} "
                        f"(user_id={job['user_id']})..."
                    )
                    result = send_email_via_pepipost(
                        job["email"], job["subject"], job["html_body"]
                    )
                    print(f"  Pepipost: {format_pepipost_response(result)}")
                else:
                    print(
                        f"Sending WA to {job['phone']} "
                        f"(user_id={job['user_id']})..."
                    )
                    result = send_whatsapp(job)
                    print(f"  Sent: {result}")
                sent_jobs.append(job)
            except Exception as exc:
                failed += 1
                print(f"  Failed: {exc}", file=sys.stderr)

        stats = build_date_stats(sent_jobs)
        campaign_id = insert_campaign_record(
            mysql_conn,
            campaign_channel,
            len(sent_jobs),
            stats,
            template_label,
        )
        print()
        print(
            f"Done. Sent={len(sent_jobs)}, Failed={failed}, "
            f"mf_campaigns.id={campaign_id}, stats={stats}"
        )
        if channel == "email":
            return [job["email"] for job in sent_jobs]
        return [job["phone"] for job in sent_jobs]
    finally:
        mysql_conn.close()


if __name__ == "__main__":
    try:
        process_partial_user_retargeting()
    except Exception as exc:
        print(f"Partial user retargeting failed: {exc}", file=sys.stderr)
        sys.exit(1)
