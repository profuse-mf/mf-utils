import json
import os
import sys
import urllib.error
import urllib.request

NETCORE_API_URL = "https://emailapi.netcorecloud.net/v5/mail/send"
NETCORE_API_KEY = os.getenv("NETCORE_API_KEY", "073cc1f4cc791557c0f58a70e9f65deb")

FROM_EMAIL = "info@moneyfatafat.com"
FROM_NAME = "MoneyFatafat"
SUBJECT = "Welcome To Moneyfatafat"

RECIPIENTS = [
    "anup.vaze@gmail.com",
]

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


def build_html_body(text_body):
    escaped = (
        text_body.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    html_content = escaped.replace("\n", "<br>\n")
    return f"<html><body style='font-family: Arial, sans-serif; line-height: 1.6;'>{html_content}</body></html>"


def build_payload(to_emails, subject, text_body):
    return {
        "from": {
            "email": FROM_EMAIL,
            "name": FROM_NAME,
        },
        "subject": subject,
        "content": [
            {
                "type": "html",
                "value": build_html_body(text_body),
            },
        ],
        "personalizations": [
            {
                "to": [{"email": email} for email in to_emails],
            }
        ],
    }


def explain_netcore_error(status_code, error_body):
    if status_code == 401:
        return (
            f"Netcore API error 401: {error_body}\n\n"
            "The API key may be invalid or expired. Generate a new key in "
            "Netcore CE dashboard → Settings → Integrations → API."
        )
    if status_code == 403 and "whitelist" in error_body.lower():
        return (
            f"Netcore API error 403: {error_body}\n\n"
            "Your server IP must be whitelisted in Netcore:\n"
            "  Settings → Integrations → API → edit your API key → add IP address"
        )
    return f"Netcore API error {status_code}: {error_body}"


def send_email_via_netcore(to_emails, subject, text_body):
    payload = build_payload(to_emails, subject, text_body)
    request_data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        NETCORE_API_URL,
        data=request_data,
        headers={
            "content-type": "application/json",
            "api_key": NETCORE_API_KEY,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8")
            return json.loads(response_body) if response_body else {}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(explain_netcore_error(exc.code, error_body)) from exc


def send_welcome_email():
    print(f"Sending email to {', '.join(RECIPIENTS)}...")
    result = send_email_via_netcore(RECIPIENTS, SUBJECT, EMAIL_BODY)
    print(f"Email sent successfully: {result}")
    return result


if __name__ == "__main__":
    try:
        send_welcome_email()
    except Exception as exc:
        print(f"Failed to send email: {exc}", file=sys.stderr)
        sys.exit(1)
