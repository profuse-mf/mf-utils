import os
import sys

import pymysql
from pepipost.exceptions.api_exception import APIException
from pepipost.models.content import Content
from pepipost.models.email_struct import EmailStruct
from pepipost.models.mfrom import From
from pepipost.models.personalizations import Personalizations
from pepipost.models.send import Send
from pepipost.models.type_enum import TypeEnum
from pepipost.pepipost_client import PepipostClient

PEPIPOST_API_KEY = os.getenv("PEPIPOST_API_KEY", "d140257ef255e515a5872780ac2a4253")

FROM_EMAIL = "info@moneyfatafat.com"
FROM_NAME = "MoneyFatafat"
SUBJECT = "Welcome To Moneyfatafat"

DB_CONFIG = {
    "host": "172.31.41.11",
    "user": "profuse",
    "password": "tripleseven7",
    "database": "mf",
    "cursorclass": pymysql.cursors.DictCursor,
}

EMAIL_BODY = """Hello,

Welcome to MoneyFatafat!

We're excited to have you with us.

Need funds for a dream purchase, a family expense, home improvements, travel plans, or unexpected emergencies? MoneyFatafat helps you explore loan options from trusted lending partners—all through a simple, digital process.

Why choose MoneyFatafat?

✅ Quick online application in just a few minutes
✅ Compare loan offers from multiple lending partners
✅ 100% digital and paperless process
✅ Fast eligibility checks and approvals
✅ Transparent experience with no hidden surprises

Getting started is easy:

1. Complete your application
2. Check your eligibility
3. Compare available offers
4. Choose the loan that works best for you

No lengthy paperwork. No unnecessary delays. Just a faster and smarter way to access the funds you need.

Your financial goals are important, and we're here to help you take the next step with confidence.

Let's get started!

Warm regards,
Team MoneyFatafat
"""


def fetch_all_users():
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, email
                FROM mf_users
                ORDER BY id DESC
                """
            )
            return cursor.fetchall()
    finally:
        conn.close()


def get_recipients(users):
    recipients = []

    for user in users:
        email = (user.get("email") or "").strip()
        if not email:
            continue

        recipients.append(email)
        print(f"Recipient user id={user['id']}, email={email}")

    return list(dict.fromkeys(recipients))


def build_html_body(text_body):
    escaped = (
        text_body.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    html_content = escaped.replace("\n", "<br>\n")
    return (
        "<html><body style='font-family: Arial, sans-serif; line-height: 1.6;'>"
        f"{html_content}</body></html>"
    )


def build_send_request(to_emails, subject, text_body):
    body = Send()
    body.mfrom = From()
    body.mfrom.email = FROM_EMAIL
    body.mfrom.name = FROM_NAME
    body.subject = subject

    body.content = [Content()]
    body.content[0].mtype = TypeEnum.HTML
    body.content[0].value = build_html_body(text_body)

    personalization = Personalizations()
    personalization.to = []
    for email in to_emails:
        recipient = EmailStruct()
        recipient.email = email
        recipient.name = email.split("@")[0]
        personalization.to.append(recipient)

    body.personalizations = [personalization]
    body.tags = ["MoneyFatafat", "Welcome", "OTA"]
    return body


def send_email_via_pepipost(to_emails, subject, text_body):
    client = PepipostClient(PEPIPOST_API_KEY)
    mail_send_controller = client.mail_send
    body = build_send_request(to_emails, subject, text_body)
    return mail_send_controller.create_generatethemailsendrequest(body)


def send_welcome_emails():
    users = fetch_all_users()
    recipients = get_recipients(users)

    if not recipients:
        print("No users with email found. Email not sent.")
        return []

    sent_recipients = []
    for recipient in recipients:
        print(f"Sending email to {recipient}...")
        try:
            result = send_email_via_pepipost([recipient], SUBJECT, EMAIL_BODY)
        except APIException as exc:
            raise RuntimeError(
                f"Pepipost API error for {recipient}: {exc}"
            ) from exc

        print(f"Email sent successfully to {recipient}: {result}")
        sent_recipients.append(recipient)

    return sent_recipients


if __name__ == "__main__":
    try:
        send_welcome_emails()
    except Exception as exc:
        print(f"Failed to send email: {exc}", file=sys.stderr)
        sys.exit(1)
