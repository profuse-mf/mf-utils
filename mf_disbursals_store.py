"""Shared mf_disbursals write path used by process_disbursals and lender status APIs.

Columns: user_id, application_id, lender_id, d_status, d_amount, d_date
Dedupe: one row per (application_id, lender_id) — UPDATE if exists, else INSERT
Gate: only write when d_status is a disbursed status (same set as process_disbursals)
"""

from __future__ import annotations

from datetime import date, datetime

# Case-insensitive disbursed statuses (must stay in sync with process_disbursals).
DISBURSED_STATUSES = frozenset(
    {
        "disbursed",
        "approved process",
    }
)


def normalize_amount(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "na"}:
        return None
    text = text.replace(",", "")
    return text[:15]


def normalize_disbursal_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "na"}:
        return None
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%y", "%d/%m/%y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def is_disbursed_status(status):
    return str(status or "").strip().lower() in DISBURSED_STATUSES


def upsert_mf_disbursal(cursor, user_id, application_id, lender_id, d_status, d_amount, d_date):
    """Insert or update mf_disbursals keyed by (application_id, lender_id)."""
    cursor.execute(
        """
        SELECT id
        FROM mf_disbursals
        WHERE application_id = %s
          AND lender_id = %s
        LIMIT 1
        """,
        (application_id, lender_id),
    )
    existing = cursor.fetchone()
    if existing:
        cursor.execute(
            """
            UPDATE mf_disbursals
            SET user_id = COALESCE(%s, user_id),
                d_status = COALESCE(%s, d_status),
                d_amount = COALESCE(%s, d_amount),
                d_date = COALESCE(%s, d_date)
            WHERE id = %s
            """,
            (user_id, d_status, d_amount, d_date, existing["id"]),
        )
        return "updated"
    cursor.execute(
        """
        INSERT INTO mf_disbursals
            (user_id, application_id, lender_id, d_status, d_amount, d_date)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (user_id, application_id, lender_id, d_status, d_amount, d_date),
    )
    return "inserted"


def write_disbursal_if_qualified(
    cursor,
    *,
    user_id,
    application_id,
    lender_id,
    d_status,
    d_amount=None,
    d_date=None,
):
    """Write mf_disbursals only when status qualifies (same gate as process_disbursals).

    Returns 'inserted' | 'updated' | None (skipped).
    """
    if not is_disbursed_status(d_status):
        return None
    if application_id is None or lender_id is None:
        return None
    return upsert_mf_disbursal(
        cursor,
        user_id=user_id,
        application_id=int(application_id),
        lender_id=int(lender_id),
        d_status=d_status,
        d_amount=normalize_amount(d_amount),
        d_date=normalize_disbursal_date(d_date),
    )


def _fetch_lead_keys(cursor, lead_id):
    cursor.execute(
        """
        SELECT user_id, application_id, lender_id
        FROM lead_master
        WHERE id = %s
        LIMIT 1
        """,
        (lead_id,),
    )
    return cursor.fetchone() or {}


def apply_status_update(
    conn,
    *,
    lead_id,
    disburse_status,
    disburse_amount=None,
    disburse_datetime=None,
    user_id=None,
    application_id=None,
    lender_id=None,
    lender_ref_id=None,
):
    """Update lead_master disburse fields; upsert mf_disbursals when disbursed.

    Future lender status scripts should call this (or write_disbursal_if_qualified)
    so mf_disbursals stays consistent with process_disbursals.
    """
    with conn.cursor() as cursor:
        if user_id is None or application_id is None or lender_id is None:
            keys = _fetch_lead_keys(cursor, lead_id)
            if user_id is None:
                user_id = keys.get("user_id")
            if application_id is None:
                application_id = keys.get("application_id")
            if lender_id is None:
                lender_id = keys.get("lender_id")

        if lender_ref_id:
            cursor.execute(
                """
                UPDATE lead_master
                SET disburse_status = %s,
                    disburse_amount = %s,
                    disburse_datetime = %s,
                    lender_ref_id = COALESCE(NULLIF(TRIM(lender_ref_id), ''), %s),
                    disbursal_status_check = NOW()
                WHERE id = %s
                """,
                (
                    disburse_status,
                    disburse_amount,
                    disburse_datetime,
                    lender_ref_id,
                    lead_id,
                ),
            )
        else:
            cursor.execute(
                """
                UPDATE lead_master
                SET disburse_status = %s,
                    disburse_amount = %s,
                    disburse_datetime = %s,
                    disbursal_status_check = NOW()
                WHERE id = %s
                """,
                (disburse_status, disburse_amount, disburse_datetime, lead_id),
            )

        disbursal_action = write_disbursal_if_qualified(
            cursor,
            user_id=user_id,
            application_id=application_id,
            lender_id=lender_id,
            d_status=disburse_status,
            d_amount=disburse_amount,
            d_date=disburse_datetime,
        )

    conn.commit()
    return {
        "user_id": user_id,
        "application_id": application_id,
        "lender_id": lender_id,
        "disbursal": disbursal_action,
    }
