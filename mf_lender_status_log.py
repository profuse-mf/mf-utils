"""Raw lender status API logging — history + current snapshot.

Each lender returns a different response shape. This module does NOT parse
or normalize lender payloads.

Stored per poll:
  - lead_id, application_id, user_id, lender_id, lender_ref_id
  - status (inferred disburse_status from lender JSON)
  - response_json (raw lender payload, as-is)
  - created / last_checked

Tables
------
mf_lender_status_logs     append-only history
mf_lender_status_current  one row per lead_id (latest response)

Write path: mf_disbursals_store.apply_status_update → record_lender_status
"""

from __future__ import annotations

import json

ENSURE_LOGS_SQL = """
CREATE TABLE IF NOT EXISTS mf_lender_status_logs (
    id BIGINT NOT NULL AUTO_INCREMENT,
    lead_id BIGINT DEFAULT NULL,
    application_id BIGINT DEFAULT NULL,
    user_id INT DEFAULT NULL,
    lender_id INT NOT NULL,
    lender_ref_id VARCHAR(128) DEFAULT NULL,
    status VARCHAR(64) DEFAULT NULL,
    response_json JSON DEFAULT NULL,
    created DATETIME NOT NULL,
    PRIMARY KEY (id),
    KEY idx_lsl_app_lender_created (application_id, lender_id, created),
    KEY idx_lsl_lead_created (lead_id, created),
    KEY idx_lsl_user_lender_created (user_id, lender_id, created),
    KEY idx_lsl_lender_created (lender_id, created),
    KEY idx_lsl_status (status)
)
"""

ENSURE_CURRENT_SQL = """
CREATE TABLE IF NOT EXISTS mf_lender_status_current (
    lead_id BIGINT NOT NULL,
    application_id BIGINT DEFAULT NULL,
    user_id INT DEFAULT NULL,
    lender_id INT NOT NULL,
    lender_ref_id VARCHAR(128) DEFAULT NULL,
    status VARCHAR(64) DEFAULT NULL,
    response_json JSON DEFAULT NULL,
    last_checked DATETIME NOT NULL,
    last_log_id BIGINT DEFAULT NULL,
    PRIMARY KEY (lead_id),
    KEY idx_lsc_app_lender (application_id, lender_id),
    KEY idx_lsc_user (user_id),
    KEY idx_lsc_lender (lender_id),
    KEY idx_lsc_status (status),
    KEY idx_lsc_checked (last_checked)
)
"""

# Drop legacy normalized columns if an older schema is present.
# Keep `status` — it stores inferred disburse_status.
MIGRATE_DROP_LOG_COLUMNS = (
    "pending_step",
    "status_detail",
    "amount",
    "status_at",
    "is_change",
)
MIGRATE_DROP_CURRENT_COLUMNS = (
    "pending_step",
    "status_detail",
    "amount",
    "status_at",
    "last_changed",
)

_tables_ready = False


def _norm_text(value, max_len=None):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "na"}:
        return None
    if max_len is not None:
        return text[:max_len]
    return text


def _serialize_response(response_json):
    if response_json is None:
        return None
    if isinstance(response_json, (str, bytes)):
        return response_json
    return json.dumps(response_json, ensure_ascii=False, default=str)


def _column_exists(cursor, table_name, column_name):
    cursor.execute(
        """
        SELECT 1
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
        LIMIT 1
        """,
        (table_name, column_name),
    )
    return cursor.fetchone() is not None


def _drop_column_if_exists(cursor, table_name, column_name):
    if _column_exists(cursor, table_name, column_name):
        cursor.execute(f"ALTER TABLE {table_name} DROP COLUMN `{column_name}`")


def _add_column_if_missing(cursor, table_name, column_name, column_ddl):
    if not _column_exists(cursor, table_name, column_name):
        cursor.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_ddl}"
        )


def ensure_status_tables(cursor):
    global _tables_ready
    if _tables_ready:
        return
    cursor.execute(ENSURE_LOGS_SQL)
    cursor.execute(ENSURE_CURRENT_SQL)
    for column_name in MIGRATE_DROP_LOG_COLUMNS:
        _drop_column_if_exists(cursor, "mf_lender_status_logs", column_name)
    for column_name in MIGRATE_DROP_CURRENT_COLUMNS:
        _drop_column_if_exists(cursor, "mf_lender_status_current", column_name)
    _add_column_if_missing(
        cursor,
        "mf_lender_status_logs",
        "status",
        "status VARCHAR(64) DEFAULT NULL AFTER lender_ref_id",
    )
    _add_column_if_missing(
        cursor,
        "mf_lender_status_logs",
        "response_json",
        "response_json JSON DEFAULT NULL AFTER status",
    )
    _add_column_if_missing(
        cursor,
        "mf_lender_status_current",
        "status",
        "status VARCHAR(64) DEFAULT NULL AFTER lender_ref_id",
    )
    _add_column_if_missing(
        cursor,
        "mf_lender_status_current",
        "response_json",
        "response_json JSON DEFAULT NULL AFTER status",
    )
    _tables_ready = True


def record_lender_status(
    cursor,
    *,
    lead_id,
    application_id=None,
    user_id=None,
    lender_id,
    lender_ref_id=None,
    status=None,
    response_json=None,
    # Accepted but ignored — kept so older call sites don't break.
    pending_step=None,
    status_detail=None,
    amount=None,
    status_at=None,
):
    """Append history + upsert current with ids, inferred status, raw response_json."""
    if lead_id is None or lender_id is None:
        return {"log_id": None}

    ensure_status_tables(cursor)

    lender_ref_id = _norm_text(lender_ref_id, 128)
    status = _norm_text(status, 64)
    response_payload = _serialize_response(response_json)

    cursor.execute(
        """
        INSERT INTO mf_lender_status_logs (
            lead_id, application_id, user_id, lender_id, lender_ref_id,
            status, response_json, created
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, NOW()
        )
        """,
        (
            int(lead_id),
            int(application_id) if application_id is not None else None,
            int(user_id) if user_id is not None else None,
            int(lender_id),
            lender_ref_id,
            status,
            response_payload,
        ),
    )
    log_id = cursor.lastrowid

    cursor.execute(
        """
        INSERT INTO mf_lender_status_current (
            lead_id, application_id, user_id, lender_id, lender_ref_id,
            status, response_json, last_checked, last_log_id
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, NOW(), %s
        )
        ON DUPLICATE KEY UPDATE
            application_id = VALUES(application_id),
            user_id = VALUES(user_id),
            lender_id = VALUES(lender_id),
            lender_ref_id = COALESCE(VALUES(lender_ref_id), lender_ref_id),
            status = VALUES(status),
            response_json = VALUES(response_json),
            last_checked = NOW(),
            last_log_id = VALUES(last_log_id)
        """,
        (
            int(lead_id),
            int(application_id) if application_id is not None else None,
            int(user_id) if user_id is not None else None,
            int(lender_id),
            lender_ref_id,
            status,
            response_payload,
            log_id,
        ),
    )

    return {"log_id": log_id}
