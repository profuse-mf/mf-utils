"""Email users who were eligible for a lender yesterday but were not redirected.

UTM (lender_type=2):
  A = application_bre_logs (ClickHouse) with empty criteria_missed, created = yesterday
  B = mf_lender_rediections_stats (MySQL) for that lender

API (lender_type=1):
  A = lead_master (MySQL) with status=1, created = yesterday
  B = mf_lender_rediections_stats (MySQL) for that lender

Target apps = A - B.
Personalized Pepipost email per (lender, application).
"""

import random
import sys
from datetime import date, timedelta

import clickhouse_connect
import pymysql
from pepipost.exceptions.api_exception import APIException
from pepipost.models.content import Content
from pepipost.models.email_struct import EmailStruct
from pepipost.models.mfrom import From
from pepipost.models.personalizations import Personalizations
from pepipost.models.send import Send
from pepipost.models.type_enum import TypeEnum
from pepipost.pepipost_client import PepipostClient

from config import (
    CLICKHOUSE_DATABASE,
    CLICKHOUSE_HOST,
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_PORT,
    CLICKHOUSE_USER,
    PEPIPOST_API_KEY,
    PEPIPOST_FROM_EMAIL,
    PEPIPOST_FROM_NAME,
    db_config,
)

MYSQL_CONFIG = db_config()
LENDER_TYPE_API = 1
LENDER_TYPE_UTM = 2
FALLBACK_OFFER_URL = "https://moneyfatafat.com"
TRACKIER_PUB_ID = 218
EMAIL_REMARKETING_SOURCE = "email_remarketing"
OFFER_FACTOR_MIN = 0.55
OFFER_FACTOR_MAX = 0.85
OFFER_AMOUNT_MIN = 1500
OFFER_AMOUNT_MAX = 80000


def _trackier_url(campaign_id):
    return (
        "https://profuse.gotrackier.com/click"
        f"?campaign_id={campaign_id}"
        f"&pub_id={TRACKIER_PUB_ID}"
    )


def append_email_remarketing_params(url, application_id):
    separator = "&" if "?" in url else "?"
    return (
        f"{url}{separator}source={EMAIL_REMARKETING_SOURCE}"
        f"&p1={application_id}"
    )


# lender_id → redirect URL (from moneyfatafat apply.tsx / partners.tsx)
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
}


def resolve_offer_url(lender_id, application_id, lender_name=None):
    """Per-lender CTA URL with email remarketing tracking params."""
    url = LENDER_REDIRECT_URLS.get(int(lender_id)) if lender_id is not None else None
    if not url:
        name_key = (lender_name or "").strip().lower().replace(" ", "")
        if "cashe" in name_key:
            url = _trackier_url(227)
        else:
            url = FALLBACK_OFFER_URL
    return append_email_remarketing_params(url, application_id)

EMAIL_SUBJECT_TEMPLATE = "Your Pre-Qualified Loan Offer from {lendername}"

EMAIL_BODY_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Your Pre-Qualified Loan Offer</title>
</head>

<body style="margin: 0; padding: 0; background-color: #f4f6f8; font-family: Arial, Helvetica, sans-serif; color: #222222;">

    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color: #f4f6f8;">
        <tr>
            <td align="center" style="padding: 30px 15px;">

                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
                       style="max-width: 600px; background-color: #ffffff; border-radius: 12px; overflow: hidden;">

                    <tr>
                        <td align="center" style="background-color: #0b57d0; padding: 24px;">
                            <h1 style="margin: 0; color: #ffffff; font-size: 26px; line-height: 34px;">
                                MoneyFatafat
                            </h1>
                        </td>
                    </tr>

                    <tr>
                        <td style="padding: 32px 30px;">

                            <p style="margin: 0 0 18px; font-size: 16px; line-height: 26px;">
                                Hey {name},
                            </p>

                            <p style="margin: 0 0 22px; font-size: 16px; line-height: 26px;">
                                Good news! You have a pre-qualified loan offer specially curated for you.
                            </p>

                            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
                                   style="margin-bottom: 24px; background-color: #f1f6ff; border: 1px solid #d6e5ff; border-radius: 10px;">
                                <tr>
                                    <td align="center" style="padding: 24px 20px;">

                                        <p style="margin: 0 0 8px; color: #555555; font-size: 14px;">
                                            Pre-Qualified Loan Offer
                                        </p>

                                        <p style="margin: 0 0 12px; color: #0b57d0; font-size: 32px; font-weight: bold;">
                                            ₹{xxxx}/-
                                        </p>

                                        <p style="margin: 0; color: #333333; font-size: 15px; line-height: 23px;">
                                            Offered by <strong>{lendername}</strong>
                                        </p>

                                    </td>
                                </tr>
                            </table>

                            <p style="margin: 0 0 22px; font-size: 16px; line-height: 26px;">
                                This offer is valid only until <strong>{date}</strong>. Complete your application journey before the offer expires.
                            </p>

                            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                                <tr>
                                    <td align="center" style="padding: 4px 0 26px;">
                                        <a href="{url}"
                                           style="display: inline-block; background-color: #0b57d0; color: #ffffff;
                                                  text-decoration: none; font-size: 16px; font-weight: bold;
                                                  padding: 14px 30px; border-radius: 8px;">
                                            Complete Your Application
                                        </a>
                                    </td>
                                </tr>
                            </table>

                            <p style="margin: 0 0 22px; color: #555555; font-size: 14px; line-height: 22px; text-align: center;">
                                The process is quick, simple, and completely digital.
                            </p>

                            <p style="margin: 0; font-size: 16px; line-height: 25px;">
                                Warm regards,<br>
                                <strong>Team MoneyFatafat</strong>
                            </p>

                        </td>
                    </tr>

                    <tr>
                        <td align="center" style="background-color: #f8f9fa; padding: 20px 25px; border-top: 1px solid #eeeeee;">
                            <p style="margin: 0; color: #777777; font-size: 12px; line-height: 19px;">
                                The loan amount, interest rate, tenure, and final approval are subject to the lender's eligibility criteria and verification process.
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

# Set to False to only print recipients without sending
SEND_EMAILS = True
SEND_TO_EMAIL = "anup.vaze@gmail.com"


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


def format_offer_expiry_date():
    return (date.today() + timedelta(days=7)).strftime("%d %b %Y")


def fetch_utm_eligible_application_ids(ch_client, lender_id, target_date):
    query = f"""
        SELECT DISTINCT application_id
        FROM application_bre_logs
        WHERE lender_id = {{lender_id:UInt64}}
          AND toDate(created) = {{target_date:Date}}
          AND replaceRegexpAll(trimBoth(ifNull(criteria_missed, '')), '\\s', '')
              IN ('{{}}', '[]', '')
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
                u.email,
                u.name
            FROM application_master AS am
            JOIN mf_users AS u ON u.id = am.userid
            WHERE am.id IN ({placeholders})
            """,
            tuple(application_ids),
        )
        return {int(row["application_id"]): row for row in cursor.fetchall()}


def build_personalized_subject(lendername):
    return EMAIL_SUBJECT_TEMPLATE.format(lendername=lendername)


def build_personalized_html(lendername, name, offer_amount, expiry_date, url):
    return EMAIL_BODY_TEMPLATE.format(
        lendername=lendername,
        name=name,
        xxxx=offer_amount,
        date=expiry_date,
        url=url,
    )


def build_send_request(to_email, subject, html_body):
    body = Send()
    body.mfrom = From()
    body.mfrom.email = PEPIPOST_FROM_EMAIL
    body.mfrom.name = PEPIPOST_FROM_NAME
    body.subject = subject

    body.content = [Content()]
    body.content[0].mtype = TypeEnum.HTML
    body.content[0].value = html_body

    personalization = Personalizations()
    recipient = EmailStruct()
    recipient.email = to_email
    recipient.name = to_email.split("@")[0]
    personalization.to = [recipient]

    body.personalizations = [personalization]
    body.tags = ["MoneyFatafat", "EligibleNotRedirected"]
    return body


def send_email_via_pepipost(to_email, subject, html_body):
    if not PEPIPOST_API_KEY:
        raise RuntimeError("PEPIPOST_API_KEY is not configured in .env")

    client = PepipostClient(PEPIPOST_API_KEY)
    mail_send_controller = client.mail_send
    body = build_send_request(to_email, subject, html_body)
    return mail_send_controller.create_generatethemailsendrequest(body)


def collect_eligible_not_redirected(mysql_conn, ch_client, target_date):
    """Return list of {lender_id, lender_name, application_id} for A - B."""
    lenders = fetch_lenders(mysql_conn)
    targets = []

    print(f"Loaded {len(lenders)} lender(s)")
    print(f"Target date (yesterday): {target_date}")
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
                ch_client, lender_id, target_date
            )
        elif lender_type == LENDER_TYPE_API:
            eligible_ids = fetch_api_eligible_application_ids(
                mysql_conn, lender_id, target_date
            )
        else:
            print(
                f"Skipping lender_id={lender_id} ({lender_label}): "
                f"unknown lender_type={lender_type}"
            )
            continue

        redirected_ids = fetch_redirected_application_ids(mysql_conn, lender_id)
        not_redirected_ids = eligible_ids - redirected_ids

        print(
            f"lender_id={lender_id} ({lender_label}): "
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


def build_send_jobs(targets, details_by_app):
    jobs = []
    skipped_no_email = 0
    skipped_missing_app = 0
    expiry_date = format_offer_expiry_date()

    for target in targets:
        application_id = target["application_id"]
        detail = details_by_app.get(application_id)
        if not detail:
            skipped_missing_app += 1
            continue

        email = (detail.get("email") or "").strip()
        if not email:
            skipped_no_email += 1
            continue

        lendername = target["lender_name"]
        name = format_user_name(detail.get("name"))
        offer_amount = format_offer_amount(detail.get("loan_amount"))
        offer_url = resolve_offer_url(
            target["lender_id"], application_id, lendername
        )
        subject = build_personalized_subject(lendername)
        html_body = build_personalized_html(
            lendername=lendername,
            name=name,
            offer_amount=offer_amount,
            expiry_date=expiry_date,
            url=offer_url,
        )

        jobs.append(
            {
                "email": email,
                "user_id": detail.get("user_id"),
                "application_id": application_id,
                "lender_id": target["lender_id"],
                "lender_name": lendername,
                "name": name,
                "offer_amount": offer_amount,
                "offer_url": offer_url,
                "subject": subject,
                "html_body": html_body,
            }
        )

    return jobs, skipped_no_email, skipped_missing_app


def process_eligible_not_redirected_emails():
    target_date = date.today() - timedelta(days=1)
    mysql_conn = pymysql.connect(**MYSQL_CONFIG)
    ch_client = get_clickhouse_client()

    try:
        targets = collect_eligible_not_redirected(
            mysql_conn, ch_client, target_date
        )
        print()
        print(f"Total eligible-not-redirected lender/app pairs: {len(targets)}")

        app_ids = sorted({item["application_id"] for item in targets})
        details_by_app = fetch_application_user_details(mysql_conn, app_ids)
        jobs, skipped_no_email, skipped_missing_app = build_send_jobs(
            targets, details_by_app
        )
        print(
            f"Emails to send: {len(jobs)} "
            f"(skipped missing email={skipped_no_email}, "
            f"missing application/user={skipped_missing_app})"
        )

        if not jobs:
            print("No recipients found. Nothing to send.")
            return []

        if not SEND_EMAILS:
            print("SEND_EMAILS=False — listing recipients only:")
            for job in jobs:
                print(
                    f"  would send → {SEND_TO_EMAIL} "
                    f"(original={job['email']}) | "
                    f"lender={job['lender_name']} | "
                    f"name={job['name']} | "
                    f"offer=₹{job['offer_amount']} | "
                    f"url={job['offer_url']} | "
                    f"app={job['application_id']}"
                )
            return []

        sent = []
        failed = 0
        for job in jobs:
            original_email = job["email"]
            send_to = SEND_TO_EMAIL
            print(
                f"Sending to {send_to} "
                f"(original={original_email}, "
                f"lender={job['lender_name']}, "
                f"app={job['application_id']}, "
                f"offer=₹{job['offer_amount']}, "
                f"url={job['offer_url']})..."
            )
            try:
                result = send_email_via_pepipost(
                    send_to, job["subject"], job["html_body"]
                )
                print(f"  Sent: {result}")
                sent.append(send_to)
            except APIException as exc:
                failed += 1
                print(
                    f"  Pepipost error for {send_to} "
                    f"(original={original_email}): {exc}",
                    file=sys.stderr,
                )

        print()
        print(f"Done. Sent={len(sent)}, Failed={failed}")
        return sent
    finally:
        mysql_conn.close()


if __name__ == "__main__":
    try:
        process_eligible_not_redirected_emails()
    except Exception as exc:
        print(
            f"Eligible-not-redirected email job failed: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
