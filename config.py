import os

import pymysql
from dotenv import load_dotenv

load_dotenv()


def _env(key, default=None):
    return os.getenv(key, default)


def _env_int(key, default):
    return int(os.getenv(key, str(default)))


def _env_list(key, default=""):
    raw = os.getenv(key, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


DB_HOST = _env("DB_HOST", "172.31.41.11")
DB_USER = _env("DB_USER", "profuse")
DB_PASSWORD = _env("DB_PASSWORD")
DB_NAME = _env("DB_NAME", "mf")
PP_DB_NAME = _env("PP_DB_NAME", "policy_pilot")
MYSQL_PORT = _env_int("MYSQL_PORT", 3306)

CLICKHOUSE_HOST = _env("CLICKHOUSE_HOST", "172.31.9.40")
CLICKHOUSE_PORT = _env_int("CLICKHOUSE_PORT", 8123)
CLICKHOUSE_USER = _env("CLICKHOUSE_USER", "default")
CLICKHOUSE_PASSWORD = _env("CLICKHOUSE_PASSWORD")
CLICKHOUSE_DATABASE = _env("CLICKHOUSE_DATABASE", "mf")

AWS_ACCESS_KEY_ID = _env("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = _env("AWS_SECRET_ACCESS_KEY")
AWS_REGION = _env("AWS_REGION", "ap-south-1")

S3_BUCKET_SCHEMA_BACKUPS = _env("S3_BUCKET_SCHEMA_BACKUPS")
S3_BUCKET_LENDER_REPORTS = _env("S3_BUCKET_LENDER_REPORTS")

SMTP_HOST = _env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = _env_int("SMTP_PORT", 587)
SMTP_USER = _env("SMTP_USER")
SMTP_PASSWORD = _env("SMTP_PASSWORD")
SMTP_FROM = _env("SMTP_FROM", SMTP_USER)

PEPIPOST_API_KEY = _env("PEPIPOST_API_KEY")
PEPIPOST_FROM_EMAIL = _env("PEPIPOST_FROM_EMAIL", "info@moneyfatafat.com")
PEPIPOST_FROM_NAME = _env("PEPIPOST_FROM_NAME", "MoneyFatafat")

MPOKKET_API_KEY = _env("MPOKKET_API_KEY")
MPOKKET_API_BASE = _env(
    "MPOKKET_API_BASE",
    "https://api.mpkt.in/acquisition-affiliate/v1/user",
)

MMB_API_KEY = _env(
    "MMB_API_KEY",
    "39e8e411-4ed4-4808-9745-8eca43943f7e",
)
MMB_MERCHANT_ID = _env(
    "MMB_MERCHANT_ID",
    "069d367f-8eda-4e4e-a911-de4ef38963a8",
)
MMB_API_URL = _env(
    "MMB_API_URL",
    "https://mm-app-backend.mymoneybazaar.com/api/merchant/lead_details/",
)

# Legacy wa_reminder scripts used API_URL / API_KEY in .env
WA_API_URL = _env("WA_API_URL") or _env("API_URL")
WA_API_KEY = _env("WA_API_KEY") or _env("API_KEY")
WA_TEMPLATE_ID = _env("WA_TEMPLATE_ID")
WA_PLATFORM = _env("WA_PLATFORM")

# Whistle/Ananta (utilsapi.smsmsg.in): MPokket remarketing WABA
MPOKKET_WA_API_URL = _env("MPOKKET_WA_API_URL") or WA_API_URL
MPOKKET_WA_API_KEY = _env("MPOKKET_WA_API_KEY") or WA_API_KEY
# Platform ID is WABA-specific — do not fall back to WA_PLATFORM
MPOKKET_WA_PLATFORM = _env("MPOKKET_WA_PLATFORM")
MPOKKET_WA_TEMPLATE_ID = _env(
    "MPOKKET_WA_TEMPLATE_ID",
    "1341052670909718",
)
# Set to 1 to send via Moneyfatafat WA_API_KEY (if template lives on that WABA)
MPOKKET_WA_USE_MF_CREDENTIALS = _env("MPOKKET_WA_USE_MF_CREDENTIALS", "").lower() in (
    "1",
    "true",
    "yes",
)


def mpokket_wa_settings():
    if MPOKKET_WA_USE_MF_CREDENTIALS:
        return WA_API_URL, WA_API_KEY, WA_PLATFORM
    return MPOKKET_WA_API_URL, MPOKKET_WA_API_KEY, MPOKKET_WA_PLATFORM


def require_wa_config(api_url=None, api_key=None, platform=None, require_platform=False):
    missing = []
    if not (api_url or WA_API_URL):
        missing.append("WA_API_URL (or legacy API_URL)")
    if not (api_key or WA_API_KEY):
        missing.append("WA_API_KEY (or legacy API_KEY)")
    if require_platform and not platform:
        missing.append("MPOKKET_WA_PLATFORM (or WA_PLATFORM)")
    if missing:
        raise RuntimeError(
            "WhatsApp API not configured. Set in .env: " + ", ".join(missing)
        )

MF_REPORT_EMAIL_TO = _env_list("MF_REPORT_EMAIL_TO")
PP_REPORT_EMAIL_TO = _env_list("PP_REPORT_EMAIL_TO")
DISBURSAL_ALERT_EMAIL_TO = _env_list("DISBURSAL_ALERT_EMAIL_TO")

MF_REPORT_PDF_PATH = _env("MF_REPORT_PDF_PATH", "/var/moneyfatafat_daily_report.pdf")
PP_REPORT_PDF_PATH = _env("PP_REPORT_PDF_PATH", "/var/policypilot_daily_report.pdf")


def db_config(database=None, autocommit=None):
    config = {
        "host": DB_HOST,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "database": database or DB_NAME,
        "cursorclass": pymysql.cursors.DictCursor,
    }
    if autocommit is not None:
        config["autocommit"] = autocommit
    return config
