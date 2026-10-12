"""Meta Conversions API (CAPI) — fire Disbursed on Success disbursals.

Triggered whenever lead_master / mf_disbursals is written with a Success /
DISBURSED status (via mf_disbursals_store or process_disbursals).

User matching from mf_users (for the application):
  hashed: name (fn/ln), age, email, phone, dob→db
  raw:    fbp, fbc, attrid (sent as fbclid / used to build fbc)

Uses META_PIXEL_ID / META_CAPI_ACCESS_TOKEN / META_CAPI_TEST_EVENT_CODE from config.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import uuid
from datetime import date, datetime
from urllib import error as urlerror
from urllib import request as urlrequest

from config import (
    META_CAPI_ACCESS_TOKEN,
    META_CAPI_API_VERSION,
    META_CAPI_ENABLED,
    META_CAPI_TEST_EVENT_CODE,
    META_PIXEL_ID,
    db_config,
)
from mf_user_crypto import sql_aes_decrypt

# Align with remarketing Success/DISBURSED checks.
SUCCESS_DISBURSAL_STATUSES = frozenset({"success", "disbursed"})

META_GRAPH_BASE = "https://graph.facebook.com"


def is_success_disbursal_status(status):
    return str(status or "").strip().lower() in SUCCESS_DISBURSAL_STATUSES


def _sha256_normalize(value):
    text = str(value or "").strip().lower()
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_email(email):
    return str(email or "").strip().lower()


def _normalize_phone(mobile):
    digits = re.sub(r"\D", "", str(mobile or ""))
    if digits.startswith("91") and len(digits) == 12:
        return digits
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10:
        return "91" + digits
    return digits or None


def _split_name(name):
    parts = [p for p in str(name or "").strip().split() if p]
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


def _parse_dob(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:19] if " " in text else text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _age_from_dob(dob, today=None):
    if not dob:
        return None
    today = today or date.today()
    years = today.year - dob.year - (
        (today.month, today.day) < (dob.month, dob.day)
    )
    return years if years >= 0 else None


def _build_fbc(fbc, attrid):
    fbc = (fbc or "").strip()
    if fbc:
        return fbc
    fbclid = (attrid or "").strip()
    if not fbclid:
        return None
    # Meta click-id cookie format when only fbclid/attrid is stored.
    return f"fb.1.{int(time.time())}.{fbclid}"


def hash_user_data(user):
    """Build Meta user_data + extras from a mf_users row dict."""
    email = _normalize_email(user.get("email"))
    phone = _normalize_phone(user.get("mobile"))
    first, last = _split_name(user.get("name"))
    dob = _parse_dob(user.get("dob"))
    age = _age_from_dob(dob)

    user_data = {}
    em = _sha256_normalize(email) if email else None
    ph = _sha256_normalize(phone) if phone else None
    fn = _sha256_normalize(first) if first else None
    ln = _sha256_normalize(last) if last else None
    db = _sha256_normalize(dob.strftime("%Y%m%d")) if dob else None
    age_hash = _sha256_normalize(str(age)) if age is not None else None

    if em:
        user_data["em"] = [em]
    if ph:
        user_data["ph"] = [ph]
    if fn:
        user_data["fn"] = [fn]
    if ln:
        user_data["ln"] = [ln]
    if db:
        user_data["db"] = [db]
    if user.get("user_id") is not None:
        ext = _sha256_normalize(str(user["user_id"]))
        if ext:
            user_data["external_id"] = [ext]

    fbp = (user.get("fbp") or "").strip() or None
    fbc = _build_fbc(user.get("fbc"), user.get("attrid"))
    if fbp:
        user_data["fbp"] = fbp
    if fbc:
        user_data["fbc"] = fbc

    return {
        "user_data": user_data,
        "age": age,
        "age_hash": age_hash,
        "fbclid": (user.get("attrid") or "").strip() or None,
    }


def fetch_user_for_application(cursor, application_id=None, user_id=None):
    """Load match fields for the application’s user (or explicit user_id)."""
    if user_id is None and application_id is None:
        return None

    # Prefer columns user asked for; fall back if fbp/fbc/attrid absent.
    base_select = f"""
        SELECT
            u.id AS user_id,
            u.name,
            u.dob,
            {sql_aes_decrypt("u.email", "email")},
            {sql_aes_decrypt("u.mobile", "mobile")}
            {{extra}}
        FROM mf_users AS u
        {{join}}
        WHERE {{where}}
        LIMIT 1
    """

    if user_id is not None:
        join = ""
        where = "u.id = %s"
        params = (int(user_id),)
    else:
        join = "JOIN application_master AS am ON am.userid = u.id"
        where = "am.id = %s"
        params = (int(application_id),)

    for extra in (
        ", u.fbp, u.fbc, u.attrid",
        ", u.fbp, u.fbc, u.fbclid AS attrid",
        "",
    ):
        query = base_select.format(extra=extra, join=join, where=where)
        try:
            cursor.execute(query, params)
            row = cursor.fetchone()
            if row and "attrid" not in row:
                row["attrid"] = None
            if row and "fbp" not in row:
                row["fbp"] = None
            if row and "fbc" not in row:
                row["fbc"] = None
            return row
        except Exception as exc:
            msg = str(exc).lower()
            if "unknown column" in msg:
                continue
            raise
    return None


def build_disbursed_event(
    *,
    user,
    application_id,
    lender_id,
    amount=None,
    event_time=None,
    event_id=None,
):
    hashed = hash_user_data(user)
    try:
        value = float(str(amount).replace(",", "")) if amount is not None else 0.0
    except (TypeError, ValueError):
        value = 0.0

    custom_data = {
        "currency": "INR",
        "value": value,
        "content_type": "product",
        "content_ids": [str(lender_id)] if lender_id is not None else [],
        "application_id": int(application_id) if application_id is not None else None,
        "lender_id": int(lender_id) if lender_id is not None else None,
    }
    if hashed.get("age") is not None:
        custom_data["age"] = hashed["age"]
    if hashed.get("age_hash"):
        custom_data["age_hash"] = hashed["age_hash"]
    if hashed.get("fbclid"):
        custom_data["fbclid"] = hashed["fbclid"]

    # Drop Nones from custom_data
    custom_data = {k: v for k, v in custom_data.items() if v is not None}

    event = {
        "event_name": "Disbursed",
        "event_time": int(event_time or time.time()),
        "event_id": event_id
        or f"disbursal_{application_id}_{lender_id}_{hashed['user_data'].get('external_id', ['x'])[0][:12]}",
        "action_source": "website" if (hashed["user_data"].get("fbp") or hashed["user_data"].get("fbc")) else "other",
        "user_data": hashed["user_data"],
        "custom_data": custom_data,
    }
    return event


# Back-compat alias
build_purchase_event = build_disbursed_event


def send_capi_events(events, *, test_event_code=None, timeout=30):
    if not META_CAPI_ENABLED:
        return {"skipped": True, "reason": "META_CAPI_ENABLED is false"}
    if not META_PIXEL_ID or not META_CAPI_ACCESS_TOKEN:
        return {
            "skipped": True,
            "reason": "META_PIXEL_ID / META_CAPI_ACCESS_TOKEN not configured",
        }
    if not events:
        return {"skipped": True, "reason": "no events"}

    payload = {
        "data": events,
        "access_token": META_CAPI_ACCESS_TOKEN,
    }
    code = test_event_code if test_event_code is not None else META_CAPI_TEST_EVENT_CODE
    if code:
        payload["test_event_code"] = code

    url = (
        f"{META_GRAPH_BASE}/{META_CAPI_API_VERSION}/"
        f"{META_PIXEL_ID}/events"
    )
    body = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            return {"ok": True, "http_status": resp.status, "response": parsed}
    except urlerror.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "http_status": exc.code,
            "response": err_body,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def notify_success_disbursal(
    *,
    user_id=None,
    application_id=None,
    lender_id=None,
    d_status=None,
    d_amount=None,
    cursor=None,
    conn=None,
    test_event_code=None,
    silent=True,
):
    """Send CAPI Disbursed if d_status is Success/DISBURSED.

    Opens its own DB connection when cursor/conn not provided.
    Never raises when silent=True (default) — logs to stderr instead.
    """
    if not is_success_disbursal_status(d_status):
        return {"skipped": True, "reason": "not_success_status"}

    owns_conn = False
    try:
        if cursor is None:
            if conn is None:
                conn = __import__("pymysql").connect(**db_config())
                owns_conn = True
            cursor = conn.cursor()

        user = fetch_user_for_application(
            cursor, application_id=application_id, user_id=user_id
        )
        if not user:
            result = {"skipped": True, "reason": "user_not_found"}
            if not silent:
                print(f"Meta CAPI skipped: {result}", file=sys.stderr)
            return result

        event_id = (
            f"disbursal_{application_id}_{lender_id}_"
            f"{uuid.uuid5(uuid.NAMESPACE_URL, f'{application_id}:{lender_id}:success').hex[:16]}"
        )
        event = build_disbursed_event(
            user=user,
            application_id=application_id,
            lender_id=lender_id,
            amount=d_amount,
            event_id=event_id,
        )
        result = send_capi_events([event], test_event_code=test_event_code)
        if not result.get("ok") and not result.get("skipped"):
            print(
                f"Meta CAPI failed application_id={application_id} "
                f"lender_id={lender_id}: {result}",
                file=sys.stderr,
            )
        elif not silent:
            print(
                f"Meta CAPI application_id={application_id} "
                f"lender_id={lender_id}: {result}"
            )
        return result
    except Exception as exc:
        if silent:
            print(
                f"Meta CAPI error application_id={application_id}: {exc}",
                file=sys.stderr,
            )
            return {"ok": False, "error": str(exc)}
        raise
    finally:
        if owns_conn and conn is not None:
            conn.close()


def parse_args(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Meta CAPI helpers / test send")
    parser.add_argument(
        "--test-send",
        action="store_true",
        help="Send a test Disbursed event using META_CAPI_TEST_EVENT_CODE",
    )
    parser.add_argument("--application-id", type=int, default=None)
    parser.add_argument("--user-id", type=int, default=None)
    parser.add_argument("--lender-id", type=int, default=1)
    parser.add_argument("--amount", default="1000")
    parser.add_argument(
        "--test-event-code",
        default=None,
        help="Override test event code (default from .env)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    if not args.test_send:
        print(
            "Usage: python3 meta_capi.py --test-send "
            "[--application-id ID | --user-id ID]"
        )
        sys.exit(0)
    result = notify_success_disbursal(
        user_id=args.user_id,
        application_id=args.application_id,
        lender_id=args.lender_id,
        d_status="Success",
        d_amount=args.amount,
        test_event_code=args.test_event_code,
        silent=False,
    )
    print(json.dumps(result, ensure_ascii=False, default=str))
    sys.exit(0 if result.get("ok") or result.get("skipped") else 1)
