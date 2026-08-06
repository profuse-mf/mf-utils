import csv
import html
import os
import smtplib
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import boto3
import pymysql

from config import (
    AWS_ACCESS_KEY_ID,
    AWS_REGION,
    AWS_SECRET_ACCESS_KEY,
    DISBURSAL_ALERT_EMAIL_TO,
    S3_BUCKET_LENDER_REPORTS,
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
    db_config,
)

S3_BUCKET = S3_BUCKET_LENDER_REPORTS
DB_CONFIG = db_config()
EMAIL_FROM = SMTP_FROM
EMAIL_TO = DISBURSAL_ALERT_EMAIL_TO
PROCESSING_REPORT_EMAIL_TO = ["anup@profuseservices.com"]

# Filenames: lenderid_{lender_id}_...
# Status column still depends on lender report format.
# App ID column defaults to common headers; override with app_id_column if needed.
LENDER_CONFIGS = [
    {
        "lender_id": 1,
        "status_column": "currentStatus",
    },
    {
        "lender_id": 2,
        "status_column": "Loan Status",
    },
    {
        "lender_id": 3,
        "status_column": "status",
    },
    {
        "lender_id": 4,
        "status_column": "Status",
    },
    {
        "lender_id": 7,
        "status_column": "currentStatus",
    },
    {
        "lender_id": 8,
        "status_column": "Loan Status",
    },
]

# Same lender, different product lines — try both IDs when matching leads.
LENDER_ID_ALIASES = {
    1: (1, 7),
    7: (7, 1),
    2: (2, 8),
    8: (8, 2),
}

DEFAULT_APP_ID_COLUMNS = (
    "App ID",
    "application_id",
    "Application ID",
    "app_id",
    "ApplicationId",
)

# Temporary: treat only "Disbursed" as disbursed (case-insensitive).
# Expand later when full status list is confirmed.
DISBURSED_STATUSES = ("disbursed",)


def file_prefix_for_lender(lender_id):
    return f"lenderid_{int(lender_id)}_"


def lookup_lender_ids(lender_id):
    """Preferred lender_id first, then product-line alias if any."""
    lender_id = int(lender_id)
    return LENDER_ID_ALIASES.get(lender_id, (lender_id,))


def explain_s3_error(exc):
    message = str(exc)
    if "AWSCompromisedKeyQuarantineV3" in message or "AccessDenied" in message:
        return (
            f"{message}\n\n"
            "This usually means the AWS access key is blocked or lacks S3 permissions.\n"
            "Actions:\n"
            "  1. Create a new IAM access key (the old mf user key may be quarantined by AWS)\n"
            "  2. Ensure the IAM user can s3:ListBucket, s3:GetObject, and s3:DeleteObject on:\n"
            f"     arn:aws:s3:::{S3_BUCKET}\n"
            f"     arn:aws:s3:::{S3_BUCKET}/*\n"
            "  3. Export credentials and rerun:\n"
            "     export AWS_ACCESS_KEY_ID='your-new-key'\n"
            "     export AWS_SECRET_ACCESS_KEY='your-new-secret'\n"
            "     python3 process_disbursals.py"
        )
    return message


def get_aws_credentials():
    return AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION


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
    stem = Path(key).name
    for config in LENDER_CONFIGS:
        prefix = file_prefix_for_lender(config["lender_id"])
        if stem.startswith(prefix):
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


def find_first_column(fieldnames, column_names):
    for column_name in column_names:
        matched = find_column(fieldnames, column_name)
        if matched:
            return matched
    return None


def app_id_column_candidates(config):
    configured = config.get("app_id_column")
    if configured:
        return (configured,)
    return DEFAULT_APP_ID_COLUMNS


def normalize_application_id(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "na"}:
        return None
    if text.endswith(".0"):
        text = text[:-2]
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def normalize_status(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    return text


def is_disbursed_status(status):
    return str(status or "").strip().lower() in DISBURSED_STATUSES


def read_status_rows_from_csv(file_path, status_column, app_id_columns):
    rows_out = []
    with open(file_path, newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        status_col = find_column(reader.fieldnames, status_column)
        app_id_col = find_first_column(reader.fieldnames, app_id_columns)
        if not status_col:
            print(f"  Warning: no '{status_column}' column in {file_path}")
            return rows_out
        if not app_id_col:
            print(
                f"  Warning: no application id column "
                f"({', '.join(app_id_columns)}) in {file_path}"
            )
            return rows_out

        print(f"  Using columns: app_id='{app_id_col}', status='{status_col}'")
        for row in reader:
            application_id = normalize_application_id(row.get(app_id_col))
            status = normalize_status(row.get(status_col))
            if application_id is None or status is None:
                continue
            rows_out.append((application_id, status))
    return rows_out


def read_status_rows_from_xlsx(file_path, status_column, app_id_columns):
    try:
        import openpyxl
    except ImportError as exc:
        raise RuntimeError(
            f"openpyxl is required to read Excel files: {file_path}"
        ) from exc

    rows_out = []
    workbook = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    sheet = workbook.active

    rows = sheet.iter_rows(values_only=True)
    header = next(rows, None)
    if not header:
        workbook.close()
        return rows_out

    header = [str(col).strip() if col is not None else "" for col in header]
    status_col = find_column(header, status_column)
    app_id_col = find_first_column(header, app_id_columns)
    if not status_col:
        print(f"  Warning: no '{status_column}' column in {file_path}")
        workbook.close()
        return rows_out
    if not app_id_col:
        print(
            f"  Warning: no application id column "
            f"({', '.join(app_id_columns)}) in {file_path}"
        )
        workbook.close()
        return rows_out

    status_index = header.index(status_col)
    app_id_index = header.index(app_id_col)
    print(f"  Using columns: app_id='{app_id_col}', status='{status_col}'")

    for row in rows:
        application_id = normalize_application_id(
            row[app_id_index] if app_id_index < len(row) else None
        )
        status = normalize_status(
            row[status_index] if status_index < len(row) else None
        )
        if application_id is None or status is None:
            continue
        rows_out.append((application_id, status))

    workbook.close()
    return rows_out


def read_status_rows(file_path, config):
    extension = Path(file_path).suffix.lower()
    status_column = config["status_column"]
    app_id_columns = app_id_column_candidates(config)

    if extension == ".csv":
        return read_status_rows_from_csv(file_path, status_column, app_id_columns)
    if extension in {".xlsx", ".xlsm"}:
        return read_status_rows_from_xlsx(file_path, status_column, app_id_columns)

    print(f"  Skipping unsupported file type: {file_path}")
    return []


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


def find_lead_for_update(cursor, application_id, preferred_lender_id):
    """Return (lead_id, matched_lender_id) trying preferred then alias lender ids."""
    for lender_id in lookup_lender_ids(preferred_lender_id):
        cursor.execute(
            """
            SELECT id, lender_id
            FROM lead_master
            WHERE application_id = %s
              AND lender_id = %s
            LIMIT 1
            """,
            (application_id, lender_id),
        )
        row = cursor.fetchone()
        if row:
            return int(row["id"]), int(row["lender_id"])
    return None, None


def update_lead_disburse_status_by_id(cursor, lead_id, disburse_status):
    cursor.execute(
        """
        UPDATE lead_master
        SET disburse_status = %s,
            disbursal_status_check = NOW()
        WHERE id = %s
        """,
        (disburse_status, lead_id),
    )
    return cursor.rowcount


def sync_file_rows_to_lead_master(status_rows, preferred_lender_id):
    """Update lead_master.disburse_status when application_id+lender_id (or alias) exists."""
    updated = 0
    skipped_missing_lead = 0
    updated_by_lender = defaultdict(int)
    alias_matched = 0

    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            for application_id, status in status_rows:
                lead_id, matched_lender_id = find_lead_for_update(
                    cursor, application_id, preferred_lender_id
                )
                if lead_id is None:
                    skipped_missing_lead += 1
                    continue
                if matched_lender_id != int(preferred_lender_id):
                    alias_matched += 1
                updated += update_lead_disburse_status_by_id(
                    cursor, lead_id, status
                )
                updated_by_lender[matched_lender_id] += 1
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "updated": updated,
        "skipped_missing_lead": skipped_missing_lead,
        "alias_matched": alias_matched,
        "updated_by_lender": dict(updated_by_lender),
    }


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


def fetch_lender_names(lender_ids=None):
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            if lender_ids:
                placeholders = ", ".join(["%s"] * len(lender_ids))
                cursor.execute(
                    f"""
                    SELECT id, lender_name, product_offering
                    FROM mf_lenders
                    WHERE id IN ({placeholders})
                    """,
                    tuple(lender_ids),
                )
            else:
                cursor.execute(
                    """
                    SELECT id, lender_name, product_offering
                    FROM mf_lenders
                    ORDER BY id
                    """
                )
            return {
                row["id"]: row
                for row in cursor.fetchall()
            }
    finally:
        conn.close()


def resolve_lender_name(lender_id, lender_names):
    row = lender_names.get(lender_id)
    if not row:
        return f"Lender {lender_id}"
    name = (row.get("lender_name") or f"Lender {lender_id}").strip()
    offering = (row.get("product_offering") or "").strip()
    if offering:
        return f"{name} - {offering}"
    return name


def fetch_disbursed_counts_by_period():
    """Lender-wise disbursed counts for weekly/monthly/30d/quarter/lifetime."""
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    lm.lender_id,
                    COALESCE(NULLIF(TRIM(l.lender_name), ''), CONCAT('Lender ', lm.lender_id))
                        AS lender_name,
                    SUM(
                        CASE
                            WHEN COALESCE(lm.disburse_datetime, lm.disbursal_status_check)
                                 >= DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY)
                            THEN 1 ELSE 0
                        END
                    ) AS weekly,
                    SUM(
                        CASE
                            WHEN COALESCE(lm.disburse_datetime, lm.disbursal_status_check)
                                 >= DATE_FORMAT(CURDATE(), '%%Y-%%m-01')
                            THEN 1 ELSE 0
                        END
                    ) AS monthly,
                    SUM(
                        CASE
                            WHEN COALESCE(lm.disburse_datetime, lm.disbursal_status_check)
                                 >= NOW() - INTERVAL 30 DAY
                            THEN 1 ELSE 0
                        END
                    ) AS last_30_days,
                    SUM(
                        CASE
                            WHEN COALESCE(lm.disburse_datetime, lm.disbursal_status_check)
                                 >= DATE_ADD(
                                     MAKEDATE(YEAR(CURDATE()), 1),
                                     INTERVAL QUARTER(CURDATE()) - 1 QUARTER
                                 )
                            THEN 1 ELSE 0
                        END
                    ) AS this_quarter,
                    COUNT(*) AS lifetime
                FROM lead_master AS lm
                LEFT JOIN mf_lenders AS l ON l.id = lm.lender_id
                WHERE LOWER(TRIM(lm.disburse_status)) = 'disbursed'
                GROUP BY lm.lender_id, lender_name
                ORDER BY lender_name, lm.lender_id
                """
            )
            return cursor.fetchall()
    finally:
        conn.close()


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


def send_email(subject, text_body, html_body, to_emails):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(to_emails)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)

    print(f"Email sent to {', '.join(to_emails)}: {subject}")


def send_new_status_email(new_statuses_by_lender):
    if not new_statuses_by_lender:
        return
    if not EMAIL_TO:
        print("Skipping new-status email: DISBURSAL_ALERT_EMAIL_TO is empty")
        return

    lender_names = fetch_lender_names(list(new_statuses_by_lender.keys()))
    subject, text_body, html_body = build_new_status_email(
        new_statuses_by_lender, lender_names
    )
    send_email(subject, text_body, html_body, EMAIL_TO)


def build_processing_report_email(summary):
    lender_names = summary["lender_names"]
    run_by_lender = summary["run_updates_by_lender"]
    period_rows = summary["period_disbursals"]

    run_rows_html = "".join(
        f"<tr><td>{html.escape(resolve_lender_name(lender_id, lender_names))}</td>"
        f"<td>{count}</td></tr>"
        for lender_id, count in sorted(run_by_lender.items())
    ) or "<tr><td colspan='2'>No lead_master updates</td></tr>"

    period_rows_html = "".join(
        (
            "<tr>"
            f"<td>{html.escape(str(row['lender_name']))} (id={row['lender_id']})</td>"
            f"<td>{int(row['weekly'] or 0)}</td>"
            f"<td>{int(row['monthly'] or 0)}</td>"
            f"<td>{int(row['last_30_days'] or 0)}</td>"
            f"<td>{int(row['this_quarter'] or 0)}</td>"
            f"<td>{int(row['lifetime'] or 0)}</td>"
            "</tr>"
        )
        for row in period_rows
    ) or "<tr><td colspan='6'>No disbursed leads found</td></tr>"

    subject = (
        f"Disbursal processing report - "
        f"{summary['total_updated']} updated - {datetime.now().date()}"
    )

    html_body = f"""
<html>
  <body>
    <h2>Disbursal file processing report</h2>
    <p><strong>Total lead_master updates this run:</strong> {summary['total_updated']}</p>
    <p><strong>Files processed:</strong> {summary['files_processed']}</p>
    <p><strong>Rows read (app_id + status):</strong> {summary['rows_read']}</p>
    <p><strong>Skipped (no matching lead):</strong> {summary['skipped_missing_lead']}</p>
    <p><strong>Matched via lender alias (1↔7 / 2↔8):</strong> {summary['alias_matched']}</p>
    <p><em>Disbursed status filter (temporary): case-insensitive "Disbursed"</em></p>

    <h3>This run — lender-wise updates</h3>
    <table border="1" cellpadding="8" cellspacing="0">
      <tr><th>Lender</th><th>Updates</th></tr>
      {run_rows_html}
    </table>

    <h3>Actual disbursals (disburse_status = Disbursed)</h3>
    <table border="1" cellpadding="8" cellspacing="0">
      <tr>
        <th>Lender</th>
        <th>This week</th>
        <th>This month</th>
        <th>Last 30 days</th>
        <th>This quarter</th>
        <th>Lifetime</th>
      </tr>
      {period_rows_html}
    </table>
  </body>
</html>
""".strip()

    text_lines = [
        "Disbursal file processing report",
        f"Total lead_master updates this run: {summary['total_updated']}",
        f"Files processed: {summary['files_processed']}",
        f"Rows read: {summary['rows_read']}",
        f"Skipped (no matching lead): {summary['skipped_missing_lead']}",
        f"Matched via lender alias: {summary['alias_matched']}",
        "",
        "This run — lender-wise updates:",
    ]
    for lender_id, count in sorted(run_by_lender.items()):
        text_lines.append(
            f"  {resolve_lender_name(lender_id, lender_names)}: {count}"
        )
    text_lines.extend(["", "Actual disbursals (status=Disbursed):"])
    for row in period_rows:
        text_lines.append(
            f"  {row['lender_name']} (id={row['lender_id']}): "
            f"week={int(row['weekly'] or 0)}, "
            f"month={int(row['monthly'] or 0)}, "
            f"30d={int(row['last_30_days'] or 0)}, "
            f"quarter={int(row['this_quarter'] or 0)}, "
            f"lifetime={int(row['lifetime'] or 0)}"
        )

    return subject, "\n".join(text_lines), html_body


def send_processing_report_email(summary):
    subject, text_body, html_body = build_processing_report_email(summary)
    send_email(subject, text_body, html_body, PROCESSING_REPORT_EMAIL_TO)


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


def delete_processed_files_from_s3(s3_client, keys):
    for key in keys:
        print(f"Deleting {key} from s3://{S3_BUCKET}")
        s3_client.delete_object(Bucket=S3_BUCKET, Key=key)

    if keys:
        print(f"Deleted {len(keys)} processed file(s) from S3")


def process_disbursals():
    s3_client = get_s3_client()
    statuses_by_lender = {config["lender_id"]: set() for config in LENDER_CONFIGS}
    rows_by_lender = {config["lender_id"]: [] for config in LENDER_CONFIGS}
    matched_files_by_lender = {config["lender_id"]: 0 for config in LENDER_CONFIGS}
    processed_keys = []

    with tempfile.TemporaryDirectory() as temp_dir:
        downloaded_files = download_all_files(s3_client, temp_dir)

        for key, local_path in downloaded_files:
            config = match_lender_config(key)
            if not config:
                continue

            lender_id = config["lender_id"]
            prefix = file_prefix_for_lender(lender_id)
            matched_files_by_lender[lender_id] += 1
            processed_keys.append(key)
            print(
                f"Processing {key} "
                f"(prefix={prefix}, column={config['status_column']})"
            )
            file_rows = read_status_rows(local_path, config)
            rows_by_lender[lender_id].extend(file_rows)
            file_statuses = {status for _, status in file_rows}
            statuses_by_lender[lender_id].update(file_statuses)
            print(f"  Rows with app_id+status: {len(file_rows)}")
            print(f"  Unique status values: {sorted(file_statuses)}")

    print()
    processed_any = False
    new_statuses_by_lender = {}
    total_updated = 0
    total_skipped = 0
    total_alias_matched = 0
    run_updates_by_lender = defaultdict(int)
    rows_read = 0

    for config in LENDER_CONFIGS:
        lender_id = config["lender_id"]
        matched_files = matched_files_by_lender[lender_id]
        all_unique_statuses = statuses_by_lender[lender_id]
        status_rows = rows_by_lender[lender_id]
        prefix = file_prefix_for_lender(lender_id)
        rows_read += len(status_rows)

        if matched_files == 0:
            print(
                f"No files starting with '{prefix}' "
                f"found in s3://{S3_BUCKET}"
            )
            continue

        processed_any = True
        print(f"Processed {matched_files} {prefix}* file(s)")
        print(
            f"All unique '{config['status_column']}' values from sheet(s): "
            f"{sorted(all_unique_statuses)}"
        )
        print(f"Lookup lender ids (preferred + alias): {lookup_lender_ids(lender_id)}")

        result = sync_file_rows_to_lead_master(status_rows, lender_id)
        total_updated += result["updated"]
        total_skipped += result["skipped_missing_lead"]
        total_alias_matched += result["alias_matched"]
        for matched_lender_id, count in result["updated_by_lender"].items():
            run_updates_by_lender[matched_lender_id] += count

        print(
            f"lead_master updates for file lender_id={lender_id}: "
            f"updated={result['updated']}, "
            f"skipped_no_matching_lead={result['skipped_missing_lead']}, "
            f"alias_matched={result['alias_matched']}"
        )
        print()
        new_statuses = sync_statuses_to_db(all_unique_statuses, lender_id)
        if new_statuses:
            new_statuses_by_lender[lender_id] = new_statuses
        print()

    if not processed_any:
        prefixes = ", ".join(
            file_prefix_for_lender(config["lender_id"]) for config in LENDER_CONFIGS
        )
        print(f"No matching lender files found (expected prefixes: {prefixes})")
        return

    send_new_status_email(new_statuses_by_lender)

    period_disbursals = fetch_disbursed_counts_by_period()
    lender_ids_for_names = set(run_updates_by_lender) | {
        int(row["lender_id"]) for row in period_disbursals
    }
    send_processing_report_email(
        {
            "total_updated": total_updated,
            "files_processed": len(processed_keys),
            "rows_read": rows_read,
            "skipped_missing_lead": total_skipped,
            "alias_matched": total_alias_matched,
            "run_updates_by_lender": dict(run_updates_by_lender),
            "period_disbursals": period_disbursals,
            "lender_names": fetch_lender_names(list(lender_ids_for_names)),
        }
    )

    # S3 deletion temporarily disabled
    # delete_processed_files_from_s3(s3_client, processed_keys)
    print(
        f"S3 deletion skipped ({len(processed_keys)} processed file(s) retained)"
    )


if __name__ == "__main__":
    try:
        process_disbursals()
    except Exception as exc:
        print(f"Failed to process disbursals: {explain_s3_error(exc)}", file=sys.stderr)
        sys.exit(1)
