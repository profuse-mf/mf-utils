import logging
import time

import pymysql
import requests

from config import (
    MPOKKET_WA_API_KEY,
    MPOKKET_WA_API_URL,
    MPOKKET_WA_PLATFORM,
    MPOKKET_WA_TEMPLATE_ID,
    db_config,
    require_wa_config,
)

DB_CONFIG = db_config(autocommit=False)

MPOKKET_LENDER_ID = 9
TEMPLATE_ID = MPOKKET_WA_TEMPLATE_ID
TRACKIER_URL = "https://profuse.gotrackier.com/click?campaign_id=221&pub_id=218"
HARDCODED_MOBILE = "8867188207"
LEAD_LIMIT = 5
STALE_DAYS = 15

LEADS_QUERY = """
SELECT
    lm.application_id,
    lm.user_id,
    am.loan_amount,
    u.name,
    u.mobile
FROM lead_master AS lm
JOIN application_master AS am ON am.id = lm.application_id
JOIN mf_users AS u ON u.id = lm.user_id
WHERE lm.lender_id = %s
  AND lm.status = 1
  AND lm.disburse_status = 'Initiated'
  AND lm.created > NOW() - INTERVAL %s DAY
LIMIT %s
"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def format_name(name):
    if not name or not str(name).strip():
        return "User"
    return " ".join(word.capitalize() for word in str(name).strip().split())


def send_message(name, phone, url):
    payload = {
        "template": TEMPLATE_ID,
        "phone": str(phone).replace("+", ""),
        "message": {
            "placeholders": [name, url],
        },
    }
    if MPOKKET_WA_PLATFORM:
        payload["platform"] = MPOKKET_WA_PLATFORM

    headers = {
        "api_key": MPOKKET_WA_API_KEY,
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            MPOKKET_WA_API_URL, json=payload, headers=headers, timeout=10
        )
        if response.status_code == 200:
            body = response.json() if response.text else {}
            if body.get("status") in (True, "true", "success"):
                return True
            logging.error(f"Failed → {phone} | {response.status_code} | {response.text}")
            return False
        logging.error(f"Failed → {phone} | {response.status_code} | {response.text}")
        return False
    except Exception as exc:
        logging.error(f"Exception → {phone} | {exc}")
        return False


def main():
    require_wa_config(api_url=MPOKKET_WA_API_URL, api_key=MPOKKET_WA_API_KEY)
    if not MPOKKET_WA_PLATFORM:
        logging.warning(
            "MPOKKET_WA_PLATFORM not set — if Whistle returns error 1353, "
            "add the platform ID for template %s in .env",
            TEMPLATE_ID,
        )
    connection = None

    try:
        connection = pymysql.connect(**DB_CONFIG)

        with connection.cursor() as cursor:
            cursor.execute(
                LEADS_QUERY,
                (MPOKKET_LENDER_ID, STALE_DAYS, LEAD_LIMIT),
            )
            leads = cursor.fetchall()

            logging.info(f"Total leads fetched: {len(leads)}")

            for lead in leads:
                user_id = lead["user_id"]
                name = format_name(lead.get("name"))
                loan_amount = lead.get("loan_amount")
                phone = HARDCODED_MOBILE

                logging.info(
                    f"Processing user_id={user_id}, loan_amount={loan_amount}, "
                    f"name={name}, mobile={phone}"
                )

                success = send_message(name, phone, TRACKIER_URL)

                if success:
                    try:
                        cursor.execute(
                            """
                            INSERT INTO wa_logs (userid, mobile, templateid, created)
                            VALUES (%s, %s, %s, NOW())
                            """,
                            (user_id, phone, TEMPLATE_ID),
                        )
                        connection.commit()
                        logging.info(f"Sent and logged → user_id: {user_id}")
                    except Exception as exc:
                        connection.rollback()
                        logging.error(f"Log insert failed → user_id: {user_id} | {exc}")

                time.sleep(0.3)

    except Exception as exc:
        logging.error(f"DB Error: {exc}")

    finally:
        if connection:
            connection.close()


if __name__ == "__main__":
    main()
