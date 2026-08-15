# -*- coding: utf-8 -*-
"""iCloud Mail IMAP gateway.

The gateway keeps Apple app-specific passwords on the server and exposes only
short-lived mailbox leases plus OTP lookup endpoints to this project.
"""
from __future__ import annotations

import email as email_lib
import hashlib
import hmac
import imaplib
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)

DEFAULT_ACCOUNTS_FILE = Path(__file__).with_name("accounts.json")
DEFAULT_IMAP_SERVER = "imap.mail.me.com"
DEFAULT_IMAP_PORT = 993
VALID_STATUSES = {"available", "used", "failed", "disabled"}
_OTP_PATTERNS = (
    re.compile(r"(?:verification|security|login|one[- ]time|验证码|校验码|动态码|code)[^0-9]{0,32}(\d{6})", re.I),
    re.compile(r"\b(\d{6})\b"),
)


class GatewayError(RuntimeError):
    """Expected gateway or mailbox error."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _decode_header(value: str | None) -> str:
    chunks: list[str] = []
    for part, charset in decode_header(value or ""):
        if isinstance(part, bytes):
            try:
                chunks.append(part.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                chunks.append(part.decode("utf-8", errors="replace"))
        else:
            chunks.append(str(part))
    return "".join(chunks)


def _message_text(message: email_lib.message.Message) -> str:
    parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart" or part.get_filename():
                continue
            if part.get_content_type() not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                parts.append(payload.decode(charset, errors="replace"))
            except LookupError:
                parts.append(payload.decode("utf-8", errors="replace"))
    else:
        payload = message.get_payload(decode=True)
        if payload is not None:
            charset = message.get_content_charset() or "utf-8"
            try:
                parts.append(payload.decode(charset, errors="replace"))
            except LookupError:
                parts.append(payload.decode("utf-8", errors="replace"))
    return "\n".join(parts)


def _extract_otp(text: str) -> str | None:
    normalized = re.sub(r"<[^>]+>", " ", text or "")
    normalized = re.sub(r"\s+", " ", normalized)
    for pattern in _OTP_PATTERNS:
        match = pattern.search(normalized)
        if match:
            return match.group(1)
    return None


def _message_timestamp(message: email_lib.message.Message) -> float:
    try:
        value = parsedate_to_datetime(message.get("Date", ""))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _is_target_message(message: email_lib.message.Message, recipient: str) -> bool:
    sender = _decode_header(message.get("From"))
    subject = _decode_header(message.get("Subject"))
    content = f"{sender}\n{subject}\n{_message_text(message)}"
    lower = content.lower()
    if "openai" not in lower and "chatgpt" not in lower:
        return False

    recipient = (recipient or "").strip().lower()
    if recipient:
        recipient_headers = "\n".join(
            _decode_header(message.get(name))
            for name in ("To", "Delivered-To", "X-Original-To", "Envelope-To")
        ).lower()
        # Some iCloud messages omit delivery headers. Only enforce a recipient
        # match when at least one recipient header is present.
        if recipient_headers.strip() and recipient not in recipient_headers:
            return False
    return _extract_otp(content) is not None


def _candidate_usernames(username: str, mailbox_email: str) -> list[str]:
    values: list[str] = []
    for value in (username, mailbox_email, (mailbox_email or "").split("@", 1)[0]):
        value = (value or "").strip()
        if value and value not in values:
            values.append(value)
    return values


def _connect_imap(account: dict, *, server: str, port: int, timeout: int):
    last_exc: Exception | None = None
    for username in _candidate_usernames(account.get("username", ""), account.get("email", "")):
        mail = None
        try:
            mail = imaplib.IMAP4_SSL(server, port, timeout=timeout)
            mail.login(username, account.get("app_password", ""))
            return mail
        except Exception as exc:
            last_exc = exc
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    pass
    raise GatewayError(f"iCloud IMAP login failed: {type(last_exc).__name__}: {last_exc}")


def fetch_latest_otp_once(
    account: dict,
    *,
    after_ts: float,
    server: str = DEFAULT_IMAP_SERVER,
    port: int = DEFAULT_IMAP_PORT,
    timeout: int = 20,
    scan_limit: int = 60,
) -> str | None:
    """Fetch the newest matching six-digit OTP without changing read state."""
    mail = _connect_imap(account, server=server, port=port, timeout=timeout)
    try:
        status, _ = mail.select("INBOX", readonly=True)
        if status != "OK":
            raise GatewayError("iCloud IMAP could not select INBOX")
        since = datetime.fromtimestamp(max(0.0, after_ts - 300), tz=timezone.utc).strftime("%d-%b-%Y")
        status, data = mail.uid("search", None, "SINCE", since)
        if status != "OK":
            raise GatewayError("iCloud IMAP search failed")
        ids = (data[0] or b"").split()[-max(1, int(scan_limit)) :]
        candidates: list[tuple[float, str]] = []
        for uid in reversed(ids):
            status, payload = mail.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK" or not payload:
                continue
            raw = next((item[1] for item in payload if isinstance(item, tuple) and len(item) > 1), None)
            if not raw:
                continue
            message = email_lib.message_from_bytes(raw)
            stamp = _message_timestamp(message)
            if after_ts and (not stamp or stamp + 5 < after_ts):
                continue
            if not _is_target_message(message, account.get("email", "")):
                continue
            content = "\n".join(
                (
                    _decode_header(message.get("Subject")),
                    _decode_header(message.get("From")),
                    _message_text(message),
                )
            )
            code = _extract_otp(content)
            if code:
                candidates.append((stamp, code))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None
    finally:
        try:
            mail.logout()
        except Exception:
            pass


class AccountStore:
    """Thread-safe JSON account pool with server-only IMAP credentials."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GatewayError(f"cannot read accounts file: {exc}") from exc
        rows = data.get("accounts", []) if isinstance(data, dict) else data
        return [dict(row) for row in rows if isinstance(row, dict)]

    def _save(self, rows: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"accounts": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _public(row: dict) -> dict:
        return {
            "email": row.get("email"),
            "status": row.get("status", "available"),
            "lease_id": row.get("lease_id") or "",
        }

    def import_records(self, records: list[dict]) -> tuple[int, int]:
        with self.lock:
            rows = self._load()
            known = {(row.get("email") or "").lower() for row in rows}
            inserted = skipped = 0
            for raw in records:
                mailbox = str(raw.get("email") or "").strip()
                username = str(raw.get("username") or mailbox).strip()
                password = str(raw.get("app_password") or raw.get("password") or "").strip()
                if not mailbox or not username or not password or mailbox.lower() in known:
                    skipped += 1
                    continue
                rows.append(
                    {
                        "email": mailbox,
                        "username": username,
                        "app_password": password,
                        "status": "available",
                        "lease_id": "",
                        "imported_at": _now(),
                    }
                )
                known.add(mailbox.lower())
                inserted += 1
            self._save(rows)
            return inserted, skipped

    def acquire(self, email: str = "", *, reuse: bool = False) -> dict:
        with self.lock:
            rows = self._load()
            target = (email or "").strip().lower()
            if target:
                row = next((item for item in rows if (item.get("email") or "").lower() == target), None)
                if row is None:
                    raise GatewayError("iCloud mailbox not found")
                if row.get("status") in {"failed", "disabled"}:
                    raise GatewayError(f"iCloud mailbox is {row.get('status')}")
                if row.get("status", "available") != "available" and not reuse:
                    raise GatewayError("iCloud mailbox is not available")
            else:
                row = next((item for item in rows if item.get("status", "available") == "available"), None)
            if row is None:
                raise GatewayError("no available iCloud mailbox")
            row["status"] = "used"
            row["lease_id"] = uuid.uuid4().hex
            row["used_at"] = _now()
            self._save(rows)
            return self._public(row)

    def account_for_lease(self, email: str, lease_id: str) -> dict:
        with self.lock:
            row = next((item for item in self._load() if (item.get("email") or "").lower() == email.lower()), None)
            if row is None or not lease_id or not hmac.compare_digest(str(row.get("lease_id") or ""), lease_id):
                raise GatewayError("invalid mailbox lease")
            return dict(row)

    def release(self, email: str, lease_id: str, status: str, note: str = "") -> dict:
        if status not in VALID_STATUSES:
            raise GatewayError("invalid mailbox status")
        with self.lock:
            rows = self._load()
            row = next((item for item in rows if (item.get("email") or "").lower() == email.lower()), None)
            if row is None or not lease_id or not hmac.compare_digest(str(row.get("lease_id") or ""), lease_id):
                raise GatewayError("invalid mailbox lease")
            row["status"] = status
            row["note"] = str(note or "")[:500]
            row["updated_at"] = _now()
            if status == "available":
                row["lease_id"] = ""
                row["used_at"] = None
            self._save(rows)
            return self._public(row)

    def summary(self) -> dict[str, int]:
        with self.lock:
            rows = self._load()
        result = {"total": len(rows), "available": 0, "used": 0, "failed": 0, "disabled": 0}
        for row in rows:
            status = row.get("status", "available")
            result[status] = result.get(status, 0) + 1
        return result


def _parse_import_payload(data: dict) -> list[dict]:
    records = data.get("records")
    if isinstance(records, list):
        return [dict(item) for item in records if isinstance(item, dict)]
    parsed: list[dict] = []
    for line in str(data.get("text") or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in (line.split("----") if "----" in line else line.split("===="))]
        if len(parts) == 2:
            parsed.append({"email": parts[0], "username": parts[0], "app_password": parts[1]})
        elif len(parts) >= 3:
            parsed.append({"email": parts[0], "username": parts[1], "app_password": parts[2]})
    return parsed


def create_app(config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        ICLOUD_GATEWAY_API_KEY=os.getenv("ICLOUD_GATEWAY_API_KEY", ""),
        ICLOUD_ACCOUNTS_FILE=os.getenv("ICLOUD_ACCOUNTS_FILE", str(DEFAULT_ACCOUNTS_FILE)),
        ICLOUD_IMAP_SERVER=os.getenv("ICLOUD_IMAP_SERVER", DEFAULT_IMAP_SERVER),
        ICLOUD_IMAP_PORT=int(os.getenv("ICLOUD_IMAP_PORT", str(DEFAULT_IMAP_PORT))),
        ICLOUD_IMAP_TIMEOUT=int(os.getenv("ICLOUD_IMAP_TIMEOUT", "20")),
    )
    if config:
        app.config.update(config)
    store = AccountStore(app.config["ICLOUD_ACCOUNTS_FILE"])
    app.extensions["icloud_account_store"] = store

    @app.before_request
    def authenticate():
        if request.endpoint == "health":
            return None
        expected = str(app.config.get("ICLOUD_GATEWAY_API_KEY") or "")
        supplied = request.headers.get("X-API-Key", "")
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return None

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "service": "icloud-mail-gateway"})

    @app.post("/api/v1/accounts/import")
    def import_accounts():
        records = _parse_import_payload(request.get_json(silent=True) or {})
        if not records:
            return jsonify({"ok": False, "error": "no valid account records"}), 400
        inserted, skipped = store.import_records(records)
        return jsonify({"ok": True, "inserted": inserted, "skipped": skipped, "parsed": len(records)})

    @app.get("/api/v1/accounts/summary")
    def account_summary():
        return jsonify({"ok": True, **store.summary()})

    @app.post("/api/v1/mailboxes/acquire")
    def acquire_mailbox():
        try:
            data = request.get_json(silent=True) or {}
            return jsonify({
                "ok": True,
                **store.acquire(str(data.get("email") or ""), reuse=bool(data.get("reuse", False))),
            })
        except GatewayError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409

    @app.post("/api/v1/mailboxes/otp")
    def mailbox_otp():
        data = request.get_json(silent=True) or {}
        email_address = str(data.get("email") or "").strip()
        lease_id = str(data.get("lease_id") or "").strip()
        try:
            after_ts = float(data.get("after_ts") or 0)
            account = store.account_for_lease(email_address, lease_id)
            code = fetch_latest_otp_once(
                account,
                after_ts=after_ts,
                server=app.config["ICLOUD_IMAP_SERVER"],
                port=int(app.config["ICLOUD_IMAP_PORT"]),
                timeout=int(app.config["ICLOUD_IMAP_TIMEOUT"]),
            )
        except (TypeError, ValueError, GatewayError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            logger.exception("iCloud OTP lookup failed for %s", email_address)
            return jsonify({"ok": False, "error": f"IMAP lookup failed: {type(exc).__name__}: {exc}"}), 502
        if not code:
            return jsonify({"ok": True, "pending": True}), 202
        return jsonify({"ok": True, "pending": False, "code": code})

    @app.post("/api/v1/mailboxes/release")
    def release_mailbox():
        data = request.get_json(silent=True) or {}
        try:
            row = store.release(
                str(data.get("email") or "").strip(),
                str(data.get("lease_id") or "").strip(),
                str(data.get("status") or "available").strip(),
                str(data.get("note") or ""),
            )
            return jsonify({"ok": True, **row})
        except GatewayError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host=os.getenv("ICLOUD_GATEWAY_HOST", "127.0.0.1"), port=int(os.getenv("ICLOUD_GATEWAY_PORT", "8789")))
