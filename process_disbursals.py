import csv
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import boto3
import pymysql

S3_BUCKET = "mf-lender-reports-811822680314-ap-south-1-an"
AWS_ACCESS_KEY = "AKIA32BDHBD5AKKRPZX4"
AWS_SECRET_KEY = "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"
AWS_REGION = "ap-south-1"

EMERGENCY_PAISA_SUFFIX = "Emergency_Paisa"
EMERGENCY_PAISA_LENDER_ID = 3

DB_CONFIG = {
    "host": "172.31.41.11",
    "user": "profuse",
    "password": "tripleseven7",
    "database": "mf",
    "cursorclass": pymysql.cursors.DictCursor,
}


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


def is_emergency_paisa_file(key):
    stem = Path(key).stem
    return stem.endswith(EMERGENCY_PAISA_SUFFIX)


def find_status_column(fieldnames):
    if not fieldnames:
        return None
    for name in fieldnames:
        if name and name.strip().lower() == "status":
            return name
    return None


def read_status_values_from_csv(file_path):
    statuses = set()
    with open(file_path, newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        status_col = find_status_column(reader.fieldnames)
        if not status_col:
            print(f"  Warning: no status column in {file_path}")
            return statuses

        for row in reader:
            value = row.get(status_col)
            if value is not None and str(value).strip():
                statuses.add(str(value).strip())

    return statuses


def read_status_values_from_xlsx(file_path):
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
    status_index = None
    for index, name in enumerate(header):
        if name.lower() == "status":
            status_index = index
            break

    if status_index is None:
        print(f"  Warning: no status column in {file_path}")
        workbook.close()
        return statuses

    for row in rows:
        if status_index >= len(row):
            continue
        value = row[status_index]
        if value is not None and str(value).strip():
            statuses.add(str(value).strip())

    workbook.close()
    return statuses


def read_status_values(file_path):
    extension = Path(file_path).suffix.lower()

    if extension == ".csv":
        return read_status_values_from_csv(file_path)
    if extension in {".xlsx", ".xlsm"}:
        return read_status_values_from_xlsx(file_path)

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
    all_unique_statuses = set()
    matched_files = 0

    with tempfile.TemporaryDirectory() as temp_dir:
        downloaded_files = download_all_files(s3_client, temp_dir)

        for key, local_path in downloaded_files:
            if not is_emergency_paisa_file(key):
                continue

            matched_files += 1
            print(f"Processing {key}")
            file_statuses = read_status_values(local_path)
            all_unique_statuses.update(file_statuses)
            print(f"  Unique status values: {sorted(file_statuses)}")

    print()
    if matched_files == 0:
        print(f"No files ending with '{EMERGENCY_PAISA_SUFFIX}' found in s3://{S3_BUCKET}")
        return

    print(f"Processed {matched_files} Emergency_Paisa file(s)")
    print(f"All unique status values from sheet(s): {sorted(all_unique_statuses)}")
    print()
    sync_statuses_to_db(all_unique_statuses, EMERGENCY_PAISA_LENDER_ID)


if __name__ == "__main__":
    try:
        process_disbursals()
    except Exception as exc:
        print(f"Failed to process disbursals: {explain_s3_error(exc)}", file=sys.stderr)
        sys.exit(1)
