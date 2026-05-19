import pymysql
import requests
import logging
import time
from dotenv import load_dotenv
import os 

# Load .env file
load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME"),
    "cursorclass": pymysql.cursors.DictCursor,
    "autocommit": False
}

API_URL = os.getenv("API_URL")
API_KEY = os.getenv("API_KEY")

# ----------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

QUERY = """
SELECT id as userid, name, mobile
FROM mf_users
WHERE status = 0
  AND created_date = CURDATE() - INTERVAL 1 DAY;
"""
templateid = "2195811521243183" 

def send_message(name, phone):
    if not name or name.strip() == "":
        name = "User"

    payload = {
        "template": "2195811521243183",
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
                        # 1. Insert log
                        insert_log_query = """ INSERT INTO wa_logs (userid, mobile, templateid, created) VALUES (%s, %s, %s, NOW()) """
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
