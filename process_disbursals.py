import csv
import html
import os
import smtplib
import sys
import tempfile
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import boto3
import pymysql

S3_BUCKET = "mf-lender-reports-811822680314-ap-south-1-an"
AWS_ACCESS_KEY = "AKIA32BDHBD5AKKRPZX4"
AWS_SECRET_KEY = "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"
AWS_REGION = "ap-south-1"

LENDER_CONFIGS = [
    {
        "prefix": "Emergency_Paisa",
        "status_column": "status",
        "lender_id": 3,
    },
    {
        "prefix": "Poonawalla_Fincorp",
        "status_column": "Loan Status",
        "lender_id": 2,
    },
    {
        "prefix": "Salary_Top_Up",
        "status_column": "Status",
        "lender_id": 4,
    },
    {
        "prefix": "Ram_Fincorp",
        "status_column": "currentStatus",
        "lender_id": 7,
    },
]

DB_CONFIG = {
    "host": "172.31.41.11",
    "user": "profuse",
    "password": "tripleseven7",
    "database": "mf",
    "cursorclass": pymysql.cursors.DictCursor,
}

EMAIL_FROM = "anup.vaze@appkhichadi.com"
EMAIL_TO = ["it_admin@profuseservices.com"]
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = "anup.vaze@appkhichadi.com"
SMTP_PASSWORD = "qjqn sbpr yvso seiq"


def lender_config_by_id():
    return {config["lender_id"]: config for config in LENDER_CONFIGS}


def explain_s3_error(exc):
    message = str(exc)
    if "AWSCompromisedKeyQuarantineV3" in message or "AccessDenied" in message:
        return (
            f"{message}\n\n"
            "This usually means the AWS access key is blocked or lacks S3 permissions.\n"
            "Actions:\n"
            "  1. Create a new IAM access key (the old mf user key may be quarantined by AWS)\n"
            "  2. Ensure the IAM user can s3:ListBucket and s3:GetObject on:\n"
            f"     arn:aws:s3:::{S3_BUCKET}\n"
            f"     arn:aws:s3:::{S3_BUCKET}/*\n"
            "  3. Export credentials and rerun:\n"
            "     export AWS_ACCESS_KEY_ID='your-new-key'\n"
            "     export AWS_SECRET_ACCESS_KEY='your-new-secret'\n"
            "     python3 process_disbursals.py"
        )
    return message


def get_aws_credentials():
    access_key = os.getenv("AWS_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY") or AWS_ACCESS_KEY
    secret_key = (
        os.getenv("AWS_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_KEY") or AWS_SECRET_KEY
    )
    region = os.getenv("AWS_REGION") or AWS_REGION
    return access_key, secret_key, region


def get_s3_client():
    access_key, secret_key, region = get_aws_credentials()
    return boto3.client(
        "s3",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    )


def list_s3_objects(s3_client):
    objects = []
    paginator = s3_client.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=S3_BUCKET):
        for item in page.get("Contents", []):
            objects.append(item["Key"])

    return objects


def match_lender_config(key):
    stem = Path(key).stem
    for config in LENDER_CONFIGS:
        if stem.startswith(config["prefix"]):
            return config
    return None


def find_column(fieldnames, column_name):
    if not fieldnames:
        return None
    target = column_name.strip().lower()
    for name in fieldnames:
        if name and name.strip().lower() == target:
            return name
    return None


def read_status_values_from_csv(file_path, status_column):
    statuses = set()
    with open(file_path, newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        column = find_column(reader.fieldnames, status_column)
        if not column:
            print(f"  Warning: no '{status_column}' column in {file_path}")
            return statuses

        for row in reader:
            value = row.get(column)
            if value is not None and str(value).strip():
                statuses.add(str(value).strip())

    return statuses


def read_status_values_from_xlsx(file_path, status_column):
    try:
        import openpyxl
    except ImportError as exc:
        raise RuntimeError(
            f"openpyxl is required to read Excel files: {file_path}"
        ) from exc

    statuses = set()
    workbook = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    sheet = workbook.active

    rows = sheet.iter_rows(values_only=True)
    header = next(rows, None)
    if not header:
        workbook.close()
        return statuses

    header = [str(col).strip() if col is not None else "" for col in header]
    column_index = None
    target = status_column.strip().lower()
    for index, name in enumerate(header):
        if name.lower() == target:
            column_index = index
            break

    if column_index is None:
        print(f"  Warning: no '{status_column}' column in {file_path}")
        workbook.close()
        return statuses

    for row in rows:
        if column_index >= len(row):
            continue
        value = row[column_index]
        if value is not None and str(value).strip():
            statuses.add(str(value).strip())

    workbook.close()
    return statuses


def read_status_values(file_path, status_column):
    extension = Path(file_path).suffix.lower()

    if extension == ".csv":
        return read_status_values_from_csv(file_path, status_column)
    if extension in {".xlsx", ".xlsm"}:
        return read_status_values_from_xlsx(file_path, status_column)

    print(f"  Skipping unsupported file type: {file_path}")
    return set()


def fetch_existing_statuses(cursor, lender_id):
    cursor.execute(
        """
        SELECT DISTINCT status
        FROM mf_lender_status_master
        WHERE lender_id = %s
        """,
        (lender_id,),
    )
    return {
        row["status"]
        for row in cursor.fetchall()
        if row["status"] is not None
    }


def insert_new_statuses(cursor, lender_id, statuses):
    first_seen = datetime.now()
    for status in sorted(statuses):
        cursor.execute(
            """
            INSERT INTO mf_lender_status_master (lender_id, status, first_seen)
            VALUES (%s, %s, %s)
            """,
            (lender_id, status, first_seen),
        )


def sync_statuses_to_db(sheet_statuses, lender_id):
    conn = pymysql.connect(**DB_CONFIG)

    try:
        with conn.cursor() as cursor:
            existing_statuses = fetch_existing_statuses(cursor, lender_id)
            new_statuses = sheet_statuses - existing_statuses

            print(f"Existing statuses in DB for lender_id={lender_id}: {sorted(existing_statuses)}")
            print(f"New statuses to insert: {sorted(new_statuses)}")

            if new_statuses:
                insert_new_statuses(cursor, lender_id, new_statuses)
                conn.commit()
                print(f"Inserted {len(new_statuses)} new status(es)")
            else:
                print("No new statuses to insert")

            return new_statuses
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_lender_names(lender_ids):
    if not lender_ids:
        return {}

    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            placeholders = ", ".join(["%s"] * len(lender_ids))
            cursor.execute(
                f"""
                SELECT id, lender_name
                FROM mf_lenders
                WHERE id IN ({placeholders})
                """,
                tuple(lender_ids),
            )
            return {
                row["id"]: row["lender_name"]
                for row in cursor.fetchall()
                if row["lender_name"]
            }
    finally:
        conn.close()


def resolve_lender_name(lender_id, lender_names):
    if lender_id in lender_names:
        return lender_names[lender_id]

    config = lender_config_by_id().get(lender_id)
    if config:
        return config["prefix"].replace("_", " ")

    return f"Lender {lender_id}"


def build_new_status_email(new_statuses_by_lender, lender_names):
    rows = []
    lender_names_for_subject = []

    for lender_id in sorted(new_statuses_by_lender):
        lender_name = resolve_lender_name(lender_id, lender_names)
        lender_names_for_subject.append(lender_name)
        for status in sorted(new_statuses_by_lender[lender_id]):
            rows.append((lender_name, status))

    subject = f"New status code detected - {', '.join(lender_names_for_subject)}"

    html_rows = "".join(
        f"<tr><td>{html.escape(lender_name)}</td><td>{html.escape(status)}</td></tr>"
        for lender_name, status in rows
    )
    html_body = f"""
<html>
  <body>
    <p>The following new lender status codes were detected and inserted:</p>
    <table border="1" cellpadding="8" cellspacing="0">
      <tr>
        <th>Lender Name</th>
        <th>Status</th>
      </tr>
      {html_rows}
    </table>
  </body>
</html>
""".strip()

    text_rows = "\n".join(
        f"{lender_name} | {status}" for lender_name, status in rows
    )
    text_body = (
        "The following new lender status codes were detected and inserted:\n\n"
        "Lender Name | Status\n"
        f"{text_rows}"
    )

    return subject, text_body, html_body


def send_new_status_email(new_statuses_by_lender):
    if not new_statuses_by_lender:
        return

    lender_names = fetch_lender_names(list(new_statuses_by_lender.keys()))
    subject, text_body, html_body = build_new_status_email(
        new_statuses_by_lender, lender_names
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(EMAIL_TO)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)

    print(f"Notification email sent to {', '.join(EMAIL_TO)}")


def local_path_for_key(temp_dir, key):
    safe_name = key.replace("/", "_")
    return os.path.join(temp_dir, safe_name)


def download_all_files(s3_client, temp_dir):
    keys = list_s3_objects(s3_client)
    downloaded_files = []

    print(f"Found {len(keys)} file(s) in s3://{S3_BUCKET}")

    for key in keys:
        local_path = local_path_for_key(temp_dir, key)
        print(f"Downloading {key}")
        s3_client.download_file(S3_BUCKET, key, local_path)
        downloaded_files.append((key, local_path))

    return downloaded_files


def process_disbursals():
    s3_client = get_s3_client()
    statuses_by_lender = {config["lender_id"]: set() for config in LENDER_CONFIGS}
    matched_files_by_lender = {config["lender_id"]: 0 for config in LENDER_CONFIGS}

    with tempfile.TemporaryDirectory() as temp_dir:
        downloaded_files = download_all_files(s3_client, temp_dir)

        for key, local_path in downloaded_files:
            config = match_lender_config(key)
            if not config:
                continue

            lender_id = config["lender_id"]
            matched_files_by_lender[lender_id] += 1
            print(
                f"Processing {key} "
                f"(prefix={config['prefix']}, column={config['status_column']})"
            )
            file_statuses = read_status_values(local_path, config["status_column"])
            statuses_by_lender[lender_id].update(file_statuses)
            print(f"  Unique status values: {sorted(file_statuses)}")

    print()
    processed_any = False
    new_statuses_by_lender = {}

    for config in LENDER_CONFIGS:
        lender_id = config["lender_id"]
        matched_files = matched_files_by_lender[lender_id]
        all_unique_statuses = statuses_by_lender[lender_id]

        if matched_files == 0:
            print(
                f"No files starting with '{config['prefix']}' "
                f"found in s3://{S3_BUCKET}"
            )
            continue

        processed_any = True
        print(f"Processed {matched_files} {config['prefix']} file(s)")
        print(
            f"All unique '{config['status_column']}' values from sheet(s): "
            f"{sorted(all_unique_statuses)}"
        )
        print()
        new_statuses = sync_statuses_to_db(all_unique_statuses, lender_id)
        if new_statuses:
            new_statuses_by_lender[lender_id] = new_statuses
        print()

    if not processed_any:
        prefixes = ", ".join(config["prefix"] for config in LENDER_CONFIGS)
        print(f"No matching lender files found (expected prefixes: {prefixes})")
        return

    send_new_status_email(new_statuses_by_lender)


if __name__ == "__main__":
    try:
        process_disbursals()
    except Exception as exc:
        print(f"Failed to process disbursals: {explain_s3_error(exc)}", file=sys.stderr)
        sys.exit(1)
