import os
from datetime import date

import boto3
import pymysql

DB_HOST = "172.31.41.11"
DB_USER = "profuse"
DB_PASSWORD = "tripleseven7"

S3_BUCKET = "schema-backups-811822680314-ap-south-1-an"
AWS_ACCESS_KEY = "AKIA32BDHBD5AKKRPZX4"
AWS_SECRET_KEY = "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"
AWS_REGION = "ap-south-1"

DATABASES = {
    "mf": "mf",
    "pp": "policy_pilot",
}


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
            lines = [
                f"-- Schema backup for `{database_name}`",
                f"-- Generated on {date.today().isoformat()}",
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

            lines.append("SET FOREIGN_KEY_CHECKS = 1;")
            lines.append("")

        os.makedirs(output_dir, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as sql_file:
            sql_file.write("\n".join(lines))

        return output_path, len(tables)
    finally:
        conn.close()


def upload_to_s3(file_path):
    s3_client = boto3.client(
        "s3",
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY,
        region_name=AWS_REGION,
    )
    key = os.path.basename(file_path)
    s3_client.upload_file(file_path, S3_BUCKET, key)
    return f"s3://{S3_BUCKET}/{key}"


def run_backup(output_dir="."):
    created_files = []

    for db_key, database_name in DATABASES.items():
        output_path, table_count = dump_database_schema(
            db_key, database_name, output_dir
        )
        s3_uri = upload_to_s3(output_path)
        created_files.append((output_path, table_count, s3_uri))
        print(f"Created {output_path} ({table_count} tables)")
        print(f"Uploaded to {s3_uri}")

    return created_files


if __name__ == "__main__":
    run_backup(os.getcwd())
