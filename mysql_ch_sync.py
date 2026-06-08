import sys
from datetime import datetime

import clickhouse_connect

MYSQL_HOST = "172.31.41.11"
MYSQL_PORT = 3306
MYSQL_DATABASE = "mf"
MYSQL_USER = "profuse"
MYSQL_PASSWORD = "tripleseven7"

CLICKHOUSE_HOST = "localhost"
CLICKHOUSE_PORT = 8123
CLICKHOUSE_USER = "default"
CLICKHOUSE_PASSWORD = ""
CLICKHOUSE_DATABASE = "default"


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
    api_name,
    api_status,
    stackcomplete,
    status,
    created_at,
    created,
    updated_at,
    updated
)
SELECT
    toUInt64(id),
    toUInt64(ifNull(application_id, 0)),
    toUInt64(ifNull(user_id, 0)),
    toUInt16(ifNull(lender_id, 0)),
    ifNull(api_name, ''),
    toUInt8(ifNull(api_status, 0)),
    toUInt8(ifNull(stackcomplete, 0)),
    toUInt8(ifNull(status, 0)),
    ifNull(created_at, now()),
    ifNull(created, today()),
    ifNull(updated_at, now()),
    ifNull(updated, today())
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
    salary_mode
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
    toInt16(ifNull(salary_mode, 0))
FROM {mysql_source("mf_users")}
""",
    ),
]


def get_clickhouse_client():
    return clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
        database=CLICKHOUSE_DATABASE,
    )


def sync_table(client, table_name, insert_sql):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] Syncing {table_name}...")
    client.command(insert_sql)
    client.command(f"OPTIMIZE TABLE {table_name} FINAL")
    print(f"[{datetime.now().isoformat(timespec='seconds')}] Completed {table_name}")


def run_sync():
    client = get_clickhouse_client()

    for table_name, insert_sql in SYNC_TABLES:
        sync_table(client, table_name, insert_sql)

    print(f"[{datetime.now().isoformat(timespec='seconds')}] All tables synced successfully")


if __name__ == "__main__":
    try:
        run_sync()
    except Exception as exc:
        print(f"Sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
