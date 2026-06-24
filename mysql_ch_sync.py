import argparse
import sys
from datetime import datetime

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
""",
    ),
    (
        "application_master",
        f"""
INSERT INTO application_master
(
    id,
    userid,
    loan_amount,
    loan_tenure,
    created,
    updated,
    status,
    utm_source,
    utm_medium,
    utm_channel,
    utm_partner,
    credit_consent
)
SELECT
    toUInt64(id),
    toUInt32(ifNull(userid, 0)),
    toInt32(ifNull(loan_amount, 0)),
    toInt16(ifNull(loan_tenure, 0)),
    created,
    updated,
    toUInt8(ifNull(status, 0)),
    ifNull(utm_source, ''),
    ifNull(utm_medium, ''),
    ifNull(utm_channel, ''),
    ifNull(utm_partner, ''),
    toUInt8(ifNull(credit_consent, 0))
FROM {mysql_source("application_master")}
""",
    ),
    (
        "lead_master",
        f"""
INSERT INTO lead_master
(
    id,
    application_id,
    user_id,
    lender_id,
    lender_ref_id,
    api_name,
    api_status,
    stackcomplete,
    status,
    created_at,
    created,
    updated_at,
    updated,
    disburse_status,
    disburse_amount,
    disburse_datetime,
    disbursal_status_check
)
SELECT
    toUInt64(id),
    toUInt64(ifNull(application_id, 0)),
    toUInt64(ifNull(user_id, 0)),
    toUInt16(ifNull(lender_id, 0)),
    ifNull(toString(lender_ref_id), ''),
    ifNull(api_name, ''),
    toUInt8(ifNull(api_status, 0)),
    toUInt8(ifNull(stackcomplete, 0)),
    toUInt8(ifNull(status, 0)),
    ifNull(created_at, now()),
    ifNull(created, today()),
    ifNull(updated_at, now()),
    ifNull(updated, today()),
    ifNull(disburse_status, ''),
    ifNull(toString(disburse_amount), ''),
    ifNull(disburse_datetime, toDateTime('1970-01-01 00:00:00')),
    ifNull(disbursal_status_check, toDateTime('1970-01-01 00:00:00'))
FROM {mysql_source("lead_master")}
""",
    ),
    (
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
""",
    ),
    (
        "mf_users",
        f"""
INSERT INTO mf_users
(
    id,
    mobile,
    name,
    email,
    dob,
    pan,
    res_pincode,
    res_type,
    address,
    res_duration,
    employment_type,
    emp_type,
    emp_domain,
    emp_name,
    total_exp,
    ofc_pincode,
    monthly_income,
    utm_source,
    utm_channel,
    utm_medium,
    utm_partner,
    status,
    created,
    updated,
    userhash,
    state,
    district,
    locality,
    current_exp,
    salary_mode,
    unemployment_status,
    unemployment_reason,
    has_current_income_or_support,
    current_income_source
)
SELECT
    toUInt32(id),
    ifNull(mobile, ''),
    ifNull(name, ''),
    ifNull(email, ''),
    dob,
    ifNull(pan, ''),
    ifNull(res_pincode, ''),
    toInt16(ifNull(res_type, 0)),
    ifNull(address, ''),
    toInt16(ifNull(res_duration, 0)),
    toInt16(ifNull(employment_type, 0)),
    toInt16(ifNull(emp_type, 0)),
    toInt16(ifNull(emp_domain, 0)),
    ifNull(emp_name, ''),
    ifNull(total_exp, ''),
    ifNull(ofc_pincode, ''),
    toInt32(ifNull(monthly_income, 0)),
    ifNull(utm_source, ''),
    ifNull(utm_channel, ''),
    ifNull(utm_medium, ''),
    ifNull(utm_partner, ''),
    toUInt8(ifNull(status, 0)),
    created,
    updated,
    ifNull(userhash, ''),
    ifNull(state, ''),
    ifNull(district, ''),
    ifNull(locality, ''),
    ifNull(current_exp, ''),
    toInt16(ifNull(salary_mode, 0)),
    ifNull(toString(unemployment_status), ''),
    ifNull(unemployment_reason, ''),
    toUInt8(ifNull(has_current_income_or_support, 0)),
    ifNull(current_income_source, '')
FROM {mysql_source("mf_users")}
""",
    ),
]


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


def sync_table(client, table_name, insert_sql):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] Syncing {table_name}...")
    client.command(insert_sql)
    client.command(f"OPTIMIZE TABLE {table_name} FINAL")
    print(f"[{datetime.now().isoformat(timespec='seconds')}] Completed {table_name}")


def parse_args():
    parser = argparse.ArgumentParser(description="Sync MySQL mf tables to ClickHouse")
    parser.add_argument("--ch-host", default=CLICKHOUSE_HOST)
    parser.add_argument("--ch-port", type=int, default=CLICKHOUSE_PORT)
    parser.add_argument("--ch-user", default=CLICKHOUSE_USER)
    parser.add_argument("--ch-password", default=CLICKHOUSE_PASSWORD)
    parser.add_argument("--ch-database", default=CLICKHOUSE_DATABASE)
    return parser.parse_args()


def run_sync(host, port, user, password, database):
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

    for table_name, insert_sql in SYNC_TABLES:
        sync_table(client, table_name, insert_sql)

    print(f"[{datetime.now().isoformat(timespec='seconds')}] All tables synced successfully")


if __name__ == "__main__":
    args = parse_args()
    try:
        run_sync(
            args.ch_host,
            args.ch_port,
            args.ch_user,
            args.ch_password,
            args.ch_database,
        )
    except Exception as exc:
        print(f"Sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
