"""SQL helpers for mf_users.mobile / email / pan AES at-rest encryption.

MySQL pattern (key from MF_USERS_AES_KEY, default tripleseven7):
  SELECT CONVERT(AES_DECRYPT(mobile, '…') USING utf8mb4) AS mobile
  SET email = AES_ENCRYPT(%s, '…')
"""

from __future__ import annotations

from config import MF_USERS_AES_KEY


def _sql_quote(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def sql_aes_key() -> str:
    return _sql_quote(MF_USERS_AES_KEY)


def sql_aes_decrypt(column: str, alias: str | None = None) -> str:
    """CONVERT(AES_DECRYPT(column, key) USING utf8mb4) [AS alias]."""
    expr = f"CONVERT(AES_DECRYPT({column}, {sql_aes_key()}) USING utf8mb4)"
    if alias:
        return f"{expr} AS {alias}"
    return expr


def sql_aes_encrypt_param() -> str:
    """AES_ENCRYPT(%s, key) — bind the plaintext as the sole %s."""
    return f"AES_ENCRYPT(%s, {sql_aes_key()})"


def sql_aes_decrypt_not_empty(column: str) -> str:
    """True when encrypted column decrypts to a non-empty trimmed string."""
    decrypted = sql_aes_decrypt(column)
    return (
        f"({column} IS NOT NULL AND TRIM(IFNULL({decrypted}, '')) != '')"
    )
