import argparse
import sys
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo

import clickhouse_connect

from config import (
    CLICKHOUSE_DATABASE,
    CLICKHOUSE_HOST,
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_PORT,
    CLICKHOUSE_USER,
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_USER,
    MYSQL_PORT,
)

MYSQL_HOST = DB_HOST
MYSQL_DATABASE = DB_NAME
MYSQL_USER = DB_USER
MYSQL_PASSWORD = DB_PASSWORD

ist = ZoneInfo("Asia/Kolkata")
today = datetime.now(ist).date()
yesterday = today - timedelta(days=1)

start_date = yesterday.strftime("%Y-%m-%d 00:00:00")
end_date = today.strftime("%Y-%m-%d 00:00:00")

def mysql_source(table_name):
    return (
        f"mysql('{MYSQL_HOST}:{MYSQL_PORT}', "
        f"'{MYSQL_DATABASE}', "
        f"'{table_name}', "
        f"'{MYSQL_USER}', "
        f"'{MYSQL_PASSWORD}')"
    )

SYNC_TABLES = [
    (
        "application_bre_logs",
        f"""
INSERT INTO application_bre_logs
(
    id,
    application_id,
    lender_id,
    criteria_missed,
    created
)
SELECT
    toUInt64(id),
    toUInt64(application_id),
    toUInt64(lender_id),
    ifNull(toString(criteria_missed), ''),
    ifNull(created, now())
FROM {mysql_source("application_bre_logs")}
WHERE created >= '{start_date}'
  AND created < '{end_date}'
""",
    ), (
            "lender_api_logs",
            f"""
    INSERT INTO lender_api_logs
    (
        id,
        application_id,
        user_id,
        lead_id,
        lenderid,
        apiname,
        apirequest,
        apiresponse,
        created_at,
        created
    )
    SELECT
        toUInt64(id),
        toUInt64(ifNull(application_id, 0)),
        toUInt64(ifNull(user_id, 0)),
        toUInt64(ifNull(lead_id, 0)),
        toUInt16(ifNull(lenderid, 0)),
        ifNull(apiname, ''),
        ifNull(toString(apirequest), ''),
        ifNull(toString(apiresponse), ''),
        ifNull(created_at, now()),
        ifNull(created, today())
    FROM {mysql_source("lender_api_logs")}
    WHERE created_at >= '{start_date}'
  AND created_at < '{end_date}'
    """,
        )(
                "mf_disbursals",
                f"""
        INSERT INTO mf_disbursals
        (
            id,
            user_id,
            application_id,
            lender_id,
            d_status,
            d_amount,
            d_date
        )
        SELECT
            toInt32(id),
            CAST(user_id AS Nullable(Int32)),
            CAST(application_id AS Nullable(Int32)),
            CAST(lender_id AS Nullable(Int32)),
            CAST(d_status AS Nullable(String)),
            CAST(d_amount AS Nullable(String)),
            CAST(d_date AS Nullable(Date))
        FROM {mysql_source("mf_disbursals")}
        """,
            ),
            (
                    "mf_email_sends",
                    f"""
            INSERT INTO mf_email_sends
            (
                id,
                channel,
                campaign_name,
                campaign_id,
                user_id,
                application_id,
                lender_id,
                email,
                xapiheader,
                message_id,
                status,
                error_text,
                created
            )
            SELECT
                toInt64(id),
                toString(channel),
                CAST(campaign_name AS Nullable(String)),
                CAST(campaign_id AS Nullable(Int64)),
                CAST(user_id AS Nullable(Int64)),
                CAST(application_id AS Nullable(Int64)),
                CAST(lender_id AS Nullable(Int32)),
                toString(email),
                toString(xapiheader),
                CAST(message_id AS Nullable(String)),
                toString(status),
                CAST(error_text AS Nullable(String)),
                toDateTime(created)
            FROM {mysql_source("mf_email_sends")}
            """,
                ),
                (
                        "mf_lender_rediections_stats",
                        f"""
                INSERT INTO mf_lender_rediections_stats
                (
                    id,
                    lender_id,
                    application_id,
                    created_date,
                    created
                )
                SELECT
                    toInt64(id),
                    CAST(lender_id AS Nullable(Int32)),
                    CAST(application_id AS Nullable(Int64)),
                    CAST(created_date AS Nullable(DateTime)),
                    CAST(created AS Nullable(Date))
                FROM {mysql_source("mf_lender_rediections_stats")}
                """,
                    ),
            (
                    "mf_lender_status_logs",
                    f"""
            INSERT INTO mf_lender_status_logs
            (
                id,
                lead_id,
                application_id,
                user_id,
                lender_id,
                lender_ref_id,
                status,
                response_json,
                created
            )
            SELECT
                toInt64(id),
                CAST(lead_id AS Nullable(Int64)),
                CAST(application_id AS Nullable(Int64)),
                CAST(user_id AS Nullable(Int32)),
                toInt32(lender_id),
                CAST(lender_ref_id AS Nullable(String)),
                CAST(status AS Nullable(String)),
                CAST(toString(response_json) AS Nullable(String)),
                toDateTime(created)
            FROM {mysql_source("mf_lender_status_logs")}
            """,
                )
        ]

def sync_table(client, table_name, insert_sql):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] Syncing {table_name}...")
    client.command(insert_sql)
    client.command(f"OPTIMIZE TABLE {table_name} FINAL")
    print(f"[{datetime.now().isoformat(timespec='seconds')}] Completed {table_name}")

def parse_args():
    known = [name for name, _ in SYNC_TABLES]
    parser = argparse.ArgumentParser(description="Sync MySQL mf tables to ClickHouse")
    parser.add_argument("--ch-host", default=CLICKHOUSE_HOST)
    parser.add_argument("--ch-port", type=int, default=CLICKHOUSE_PORT)
    parser.add_argument("--ch-user", default=CLICKHOUSE_USER)
    parser.add_argument("--ch-password", default=CLICKHOUSE_PASSWORD)
    parser.add_argument("--ch-database", default=CLICKHOUSE_DATABASE)
    parser.add_argument(
        "--table",
        "-t",
        action="append",
        dest="tables",
        metavar="NAME",
        choices=known,
        help=(
            "Sync only this table (repeatable). "
            f"Choices: {', '.join(known)}. Default: all."
        ),
    )
    return parser.parse_args()

def get_clickhouse_client(host, port, user, password, database):
    return clickhouse_connect.get_client(
        host=host,
        port=port,
        username=user,
        password=password,
        database=database,
        connect_timeout=30,
        send_receive_timeout=3600,
    )


def verify_clickhouse_connection(host, port, user, password, database):
    print(
        f"Connecting to ClickHouse at {host}:{port} "
        f"(database={database}, user={user})..."
    )
    client = get_clickhouse_client(host, port, user, password, database)
    client.command("SELECT 1")
    print("ClickHouse connection OK")
    return client


def run_sync(host, port, user, password, database, tables=None):
    try:
        client = verify_clickhouse_connection(host, port, user, password, database)
    except Exception as exc:
        print(
            f"\nCould not connect to ClickHouse at {host}:{port}\n"
            f"Error: {exc}\n\n"
            "Checks:\n"
            "  1. Is clickhouse-server running?\n"
            "     sudo systemctl status clickhouse-server\n"
            "  2. Does HTTP respond?\n"
            f"     curl http://{host}:{port}/ping\n"
            "  3. If ClickHouse is on another host, set CLICKHOUSE_HOST or use --ch-host\n",
            file=sys.stderr,
        )
        raise

    selected = set(tables) if tables else None
    synced = []
    for table_name, insert_sql in SYNC_TABLES:
        if selected is not None and table_name not in selected:
            continue
        sync_table(client, table_name, insert_sql)
        synced.append(table_name)

    if not synced:
        raise RuntimeError("No matching tables to sync")

    label = ", ".join(synced)
    print(
        f"[{datetime.now().isoformat(timespec='seconds')}] "
        f"Synced successfully: {label}"
    )


if __name__ == "__main__":
    args = parse_args()
    try:
        run_sync(
            args.ch_host,
            args.ch_port,
            args.ch_user,
            args.ch_password,
            args.ch_database,
            tables=args.tables,
        )
    except Exception as exc:
        print(f"Sync failed: {exc}", file=sys.stderr)
        sys.exit(1)

