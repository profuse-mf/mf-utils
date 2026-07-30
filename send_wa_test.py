"""Send a one-off WhatsApp template message via Whistle/Ananta."""

import json
import sys

import requests

API_URL = "https://utilsapi.smsmsg.in/waba/sendmessage"
API_KEY = "e6eb44d10c5bea3233cf88e6dfa2b234"
TEMPLATE_ID = "1571984130956515"
PHONE = "+918867188207"

PLACEHOLDERS = [
    "Anup,",
    "1,00,000",
    "MPokket",
]
BUTTON_URL = "https://moneyfatafat.com/"


def send_message():
    payload = {
        "template": TEMPLATE_ID,
        "phone": PHONE,
        "is_short_url": "0",
        "message": {
            "placeholders": PLACEHOLDERS,
            "button": {
                "url": BUTTON_URL,
            },
        },
    }
    headers = {
        "api_key": API_KEY,
        "Content-Type": "application/json",
    }

    print(f"Sending template {TEMPLATE_ID} to {PHONE}")
    print(f"Placeholders: {PLACEHOLDERS}")
    print(f"Button URL: {BUTTON_URL}")

    response = requests.post(API_URL, json=payload, headers=headers, timeout=30)
    print(f"HTTP {response.status_code}")
    print(response.text)

    try:
        body = response.json() if response.text else {}
    except json.JSONDecodeError:
        body = {}

    if response.status_code == 200 and body.get("status") in (True, "true", "success"):
        print("Message sent successfully")
        return True

    print("Message send failed", file=sys.stderr)
    return False


if __name__ == "__main__":
    try:
        ok = send_message()
        sys.exit(0 if ok else 1)
    except Exception as exc:
        print(f"WhatsApp send failed: {exc}", file=sys.stderr)
        sys.exit(1)
