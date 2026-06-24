import os
from datetime import date

import boto3
import pymysql

from config import (
    AWS_ACCESS_KEY_ID,
    AWS_REGION,
    AWS_SECRET_ACCESS_KEY,
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_USER,
    PP_DB_NAME,
    S3_BUCKET_SCHEMA_BACKUPS,
)

S3_BUCKET = S3_BUCKET_SCHEMA_BACKUPS

DATABASES = {
    "mf": DB_NAME,
    "pp": PP_DB_NAME,
}

MF_FULL_DATA_TABLES = {
    "mf_bre_attributes_master",
    "mf_lenders",
    "pincodes_master",
}
MF_FULL_DATA_TABLE_PREFIX = "servicable_pincodes_"


def schema_filename(db_key):
    today = date.today().strftime("%d_%m_%Y")
    return f"{db_key}_schema_{today}.sql"


def fetch_tables(cursor, database_name):
    cursor.execute(
        """
        SELECT TABLE_NAME
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s
          AND TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_NAME
        """,
        (database_name,),
    )
    return [row["TABLE_NAME"] for row in cursor.fetchall()]


def needs_full_data_backup(db_key, table_name):
    if db_key != "mf":
        return False
    if table_name in MF_FULL_DATA_TABLES:
        return True
    return table_name.startswith(MF_FULL_DATA_TABLE_PREFIX)


def dump_table_data(cursor, table):
    cursor.execute(f"SELECT * FROM `{table}`")
    columns = [desc[0] for desc in cursor.description]
    if not columns:
        return []

    col_list = ", ".join(f"`{column}`" for column in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    insert_template = f"INSERT INTO `{table}` ({col_list}) VALUES ({placeholders})"

    lines = []
    while True:
        rows = cursor.fetchmany(500)
        if not rows:
            break
        for row in rows:
            values = tuple(row[column] for column in columns)
            insert_sql = cursor.mogrify(insert_template, values)
            if isinstance(insert_sql, bytes):
                insert_sql = insert_sql.decode("utf-8")
            lines.append(f"{insert_sql};")

    return lines


def dump_database_schema(db_key, database_name, output_dir="."):
    output_path = os.path.join(output_dir, schema_filename(db_key))

    conn = pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        database=database_name,
        cursorclass=pymysql.cursors.DictCursor,
    )

    try:
        with conn.cursor() as cursor:
            tables = fetch_tables(cursor, database_name)
            data_tables = [
                table for table in tables if needs_full_data_backup(db_key, table)
            ]
            lines = [
                f"-- Backup for `{database_name}`",
                f"-- Generated on {date.today().isoformat()}",
                "-- All tables include schema; selected mf tables also include full data",
                "",
                "SET NAMES utf8mb4;",
                "SET FOREIGN_KEY_CHECKS = 0;",
                "",
            ]

            for table in tables:
                cursor.execute(f"SHOW CREATE TABLE `{table}`")
                row = cursor.fetchone()
                create_sql = row["Create Table"]

                lines.append(f"DROP TABLE IF EXISTS `{table}`;")
                lines.append(f"{create_sql};")
                lines.append("")

                if needs_full_data_backup(db_key, table):
                    lines.append(f"-- Data backup for `{table}`")
                    data_lines = dump_table_data(cursor, table)
                    lines.extend(data_lines)
                    lines.append("")

            lines.append("SET FOREIGN_KEY_CHECKS = 1;")
            lines.append("")

        os.makedirs(output_dir, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as sql_file:
            sql_file.write("\n".join(lines))

        return output_path, len(tables), len(data_tables)
    finally:
        conn.close()


def s3_object_key(db_key, filename):
    today = date.today()
    return f"{db_key}/{today.year}/{today.month:02d}/{filename}"


def upload_to_s3(db_key, file_path):
    s3_client = boto3.client(
        "s3",
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        region_name=AWS_REGION,
    )
    key = s3_object_key(db_key, os.path.basename(file_path))
    s3_client.upload_file(file_path, S3_BUCKET, key)
    return f"s3://{S3_BUCKET}/{key}"


def run_backup(output_dir="."):
    created_files = []

    for db_key, database_name in DATABASES.items():
        output_path, table_count, data_table_count = dump_database_schema(
            db_key, database_name, output_dir
        )
        s3_uri = upload_to_s3(db_key, output_path)
        created_files.append((output_path, table_count, data_table_count, s3_uri))
        print(f"Created {output_path} ({table_count} tables, {data_table_count} with data)")
        print(f"Uploaded to {s3_uri}")

    return created_files


if __name__ == "__main__":
    run_backup(os.getcwd())
