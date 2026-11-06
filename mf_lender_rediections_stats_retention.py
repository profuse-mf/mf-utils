"""Keep only last 30 days of mf_lender_rediections_stats in MySQL; archive in CH.

Age uses mf_lender_rediections_stats.created_date (datetime).
Requires columns created_date + created — see
sql/alter_mf_lender_rediections_stats_created.sql.

Flow (same as mf_email_sends_retention):
  1. Find MySQL rows with created_date < NOW() - 30 days.
  2. If ClickHouse already has those ids → delete them from MySQL.
  3. If not → sync mf_lender_rediections_stats MySQL→CH, re-check, then delete.
  4. Sync / verification failure → email MF_REPORT_EMAIL_TO; no MySQL delete.

Cron example (daily):
  20 3 * * * cd /var/mf-utils && ./venv/bin/python3 mf_lender_rediections_stats_retention.py
"""

from __future__ import annotations

import argparse
import smtplib
import sys
from datetime import datetime, timedelta
from email.message import EmailMessage

import pymysql

from config import (
    CLICKHOUSE_DATABASE,
    CLICKHOUSE_HOST,
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_PORT,
    CLICKHOUSE_USER,
    MF_REPORT_EMAIL_TO,
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
    db_config,
)
from mysql_ch_sync import SYNC_TABLES, sync_table, verify_clickhouse_connection

MYSQL_CONFIG = db_config()
TABLE = "mf_lender_rediections_stats"
RETENTION_DAYS = 30
ID_CHECK_BATCH = 5000


def report_email_recipients():
    recipients = []
    for email in MF_REPORT_EMAIL_TO:
        if email.strip().lower() == "anup.vaze@appkhichadi.com":
            recipients.append("anup@profuseservices.com")
        else:
            recipients.append(email)
    return list(dict.fromkeys(recipients))


def send_alert_email(subject, body):
    to_emails = report_email_recipients()
    if not to_emails:
        print(
            "WARNING: alert needed but MF_REPORT_EMAIL_TO is empty",
            file=sys.stderr,
        )
        print(body, file=sys.stderr)
        return
    if not SMTP_USER or not SMTP_PASSWORD:
        print(
            "WARNING: SMTP not configured; alert body follows:",
            file=sys.stderr,
        )
        print(body, file=sys.stderr)
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(to_emails)
    msg.set_content(body)

    print(
        f"Sending alert via {SMTP_HOST}:{SMTP_PORT} → {', '.join(to_emails)}"
    )
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
    print(f"Alert email sent: {subject}")


def retention_cutoff(now=None, days=RETENTION_DAYS):
    now = now or datetime.now()
    return now - timedelta(days=days)


def fetch_old_mysql_ids(mysql_conn, cutoff):
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT id
            FROM {TABLE}
            WHERE created_date < %s
            ORDER BY id
            """,
            (cutoff,),
        )
        return [int(row["id"]) for row in cursor.fetchall()]


def fetch_old_mysql_stats(mysql_conn, cutoff):
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT
                COUNT(*) AS cnt,
                MIN(id) AS min_id,
                MAX(id) AS max_id,
                MIN(created_date) AS min_created_date,
                MAX(created_date) AS max_created_date
            FROM {TABLE}
            WHERE created_date < %s
            """,
            (cutoff,),
        )
        return cursor.fetchone()


def clickhouse_has_ids(ch_client, ids):
    if not ids:
        return True

    found = 0
    for i in range(0, len(ids), ID_CHECK_BATCH):
        batch = ids[i : i + ID_CHECK_BATCH]
        id_list = ", ".join(str(x) for x in batch)
        result = ch_client.query(
            f"""
            SELECT count() AS cnt
            FROM {TABLE} FINAL
            WHERE id IN ({id_list})
            """
        )
        found += int(result.result_rows[0][0])

    return found == len(ids)


def sync_table_to_clickhouse(ch_client):
    insert_sql = None
    for name, sql in SYNC_TABLES:
        if name == TABLE:
            insert_sql = sql
            break
    if not insert_sql:
        raise RuntimeError(f"{TABLE} is not configured in mysql_ch_sync.SYNC_TABLES")
    sync_table(ch_client, TABLE, insert_sql)


def delete_old_mysql_rows(mysql_conn, cutoff, *, dry_run=False):
    with mysql_conn.cursor() as cursor:
        if dry_run:
            cursor.execute(
                f"SELECT COUNT(*) AS cnt FROM {TABLE} WHERE created_date < %s",
                (cutoff,),
            )
            return int(cursor.fetchone()["cnt"])

        cursor.execute(
            f"DELETE FROM {TABLE} WHERE created_date < %s",
            (cutoff,),
        )
        deleted = cursor.rowcount
    mysql_conn.commit()
    return deleted


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Archive mf_lender_rediections_stats older than 30 days "
            "to ClickHouse, then prune MySQL"
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check / sync only; do not DELETE from MySQL",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=RETENTION_DAYS,
        help=f"Retention days in MySQL (default {RETENTION_DAYS})",
    )
    parser.add_argument("--ch-host", default=CLICKHOUSE_HOST)
    parser.add_argument("--ch-port", type=int, default=CLICKHOUSE_PORT)
    parser.add_argument("--ch-user", default=CLICKHOUSE_USER)
    parser.add_argument("--ch-password", default=CLICKHOUSE_PASSWORD)
    parser.add_argument("--ch-database", default=CLICKHOUSE_DATABASE)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    days = max(1, int(args.days))
    cutoff = retention_cutoff(days=days)

    print(f"Retention: keep MySQL rows with created_date >= {cutoff}")
    print(f"Prune candidates: created_date < {cutoff}")

    mysql_conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        stats = fetch_old_mysql_stats(mysql_conn, cutoff)
        old_count = int(stats["cnt"] or 0)
        print(
            f"MySQL old rows: {old_count} "
            f"(id {stats.get('min_id')}→{stats.get('max_id')}, "
            f"created_date {stats.get('min_created_date')}→"
            f"{stats.get('max_created_date')})"
        )

        if old_count == 0:
            print("Nothing to prune. Done.")
            return

        old_ids = fetch_old_mysql_ids(mysql_conn, cutoff)
        print(f"Loaded {len(old_ids)} old id(s) for ClickHouse verification")

        try:
            ch_client = verify_clickhouse_connection(
                args.ch_host,
                args.ch_port,
                args.ch_user,
                args.ch_password,
                args.ch_database,
            )
        except Exception as exc:
            subject = f"{TABLE} retention: ClickHouse connect failed"
            body = (
                f"Could not connect to ClickHouse while pruning {TABLE}.\n\n"
                f"Cutoff: created_date < {cutoff}\n"
                f"MySQL old rows: {old_count}\n"
                f"Error: {exc}\n\n"
                f"MySQL was NOT pruned.\n"
            )
            send_alert_email(subject, body)
            raise SystemExit(1) from exc

        present = clickhouse_has_ids(ch_client, old_ids)
        if present:
            print("ClickHouse already has all old ids")
        else:
            print(
                "ClickHouse missing some old ids — syncing "
                f"{TABLE} from MySQL..."
            )
            try:
                sync_table_to_clickhouse(ch_client)
            except Exception as exc:
                subject = f"{TABLE} retention: ClickHouse sync failed"
                body = (
                    f"ClickHouse sync of {TABLE} failed during retention.\n\n"
                    f"Cutoff: created_date < {cutoff}\n"
                    f"MySQL old rows pending prune: {old_count}\n"
                    f"Error: {exc}\n\n"
                    f"MySQL was NOT pruned.\n"
                )
                send_alert_email(subject, body)
                raise SystemExit(1) from exc

            present = clickhouse_has_ids(ch_client, old_ids)
            if not present:
                subject = f"{TABLE} retention: CH missing ids after sync"
                body = (
                    f"After syncing {TABLE}, ClickHouse still does not have "
                    f"all MySQL rows older than {days} days.\n\n"
                    f"Cutoff: created_date < {cutoff}\n"
                    f"MySQL old rows: {old_count}\n\n"
                    f"MySQL was NOT pruned. Investigate sync / CH data.\n"
                )
                send_alert_email(subject, body)
                raise SystemExit(1)

            print("Post-sync verification OK")

        if args.dry_run:
            print(
                f"DRY RUN — would DELETE {old_count} MySQL row(s) "
                f"from {TABLE}"
            )
            return

        deleted = delete_old_mysql_rows(mysql_conn, cutoff, dry_run=False)
        print(f"Deleted {deleted} MySQL row(s) from {TABLE}")
        print("Done.")
    finally:
        mysql_conn.close()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        try:
            send_alert_email(
                f"{TABLE} retention: unexpected failure",
                f"Unexpected error while pruning {TABLE}.\n\n"
                f"Error: {exc}\n\nMySQL may not have been pruned.\n",
            )
        except Exception as mail_exc:
            print(f"Also failed sending alert: {mail_exc}", file=sys.stderr)
        print(f"{TABLE} retention failed: {exc}", file=sys.stderr)
        sys.exit(1)
