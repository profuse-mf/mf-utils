import pymysql
import requests
import logging
import time

from config import WA_API_KEY, WA_API_URL, WA_TEMPLATE_ID, db_config

DB_CONFIG = db_config(autocommit=False)
API_URL = WA_API_URL
API_KEY = WA_API_KEY
TEMPLATE_ID = WA_TEMPLATE_ID

# ----------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

QUERY = """
SELECT pu.userid, u.name, u.mobile
FROM mf_users u
JOIN mf_partial_users pu ON u.id = pu.userid
WHERE pu.created < NOW() - INTERVAL 20 MINUTE;
"""

templateid = TEMPLATE_ID

def send_message(name, phone):
    if not name or name.strip() == "":
        name = "User"

    payload = {
        "template": TEMPLATE_ID,
        "phone": str(phone).replace("+", ""),
        "message": {
            "placeholders": [name],
            "button": {
                "url": "https://moneyfatafat.com/apply"
            }
        }
    }

    headers = {
        "api_key": API_KEY,
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(API_URL, json=payload, headers=headers, timeout=10)

        if response.status_code == 200:
            return True
        else:
            logging.error(f"Failed → {phone} | {response.status_code} | {response.text}")
            return False

    except Exception as e:
        logging.error(f"Exception → {phone} | {str(e)}")
        return False


def main():
    connection = None

    try:
        connection = pymysql.connect(**DB_CONFIG)

        with connection.cursor() as cursor:
            cursor.execute(QUERY)
            users = cursor.fetchall()

            logging.info(f"Total users fetched: {len(users)}")

            for user in users:
                userid = user.get("userid")
                name = user.get("name")
                phone = user.get("mobile")

                if not phone:
                    logging.warning(f"Skipping missing phone → {userid}")
                    continue

                success = send_message(name, phone)

                if success:
                    try:
                        insert_log_query = """INSERT INTO wa_logs (userid, mobile, templateid, created) VALUES (%s, %s, %s, NOW()) """
                        cursor.execute(insert_log_query, (userid, phone, templateid))
                        
                        delete_query = "DELETE FROM mf_partial_users WHERE userid = %s LIMIT 1"
                        cursor.execute(delete_query, (userid,))
                        connection.commit()

                        logging.info(f"Deleted → userid: {userid}")

                    except Exception as e:
                        connection.rollback()
                        logging.error(f"Delete failed → {userid} | {str(e)}")

                # basic rate limit
                time.sleep(0.3)

    except Exception as e:
        logging.error(f"DB Error: {str(e)}")

    finally:
        if connection:
            connection.close()


if __name__ == "__main__":
    main()
