# -*- coding: utf-8 -*-
"""Client for either the bundled gateway or the server inventory service."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from config import email as _email_cfg

logger = logging.getLogger(__name__)


class ICloudAPIError(RuntimeError):
    """iCloud gateway request failed."""


@dataclass
class ICloudAccount:
    email: str
    lease_id: str


_CONTEXT_CACHE: dict[str, ICloudAccount] = {}
_LOCK = threading.RLock()


def _mode() -> str:
    mode = str(getattr(_email_cfg, "ICLOUD_API_MODE", "gateway") or "gateway").strip().lower()
    if mode not in {"gateway", "inventory"}:
        raise ICloudAPIError("ICLOUD_API_MODE 只支持 gateway 或 inventory")
    return mode


def _settings() -> tuple[str, str, int, str]:
    base = str(getattr(_email_cfg, "ICLOUD_API_BASE", "") or "").strip().rstrip("/")
    key = str(getattr(_email_cfg, "ICLOUD_API_KEY", "") or "").strip()
    timeout = max(1, int(getattr(_email_cfg, "ICLOUD_REQUEST_TIMEOUT", 25) or 25))
    if not base:
        raise ICloudAPIError("iCloud API 地址未配置，请填写 ICLOUD_API_BASE")
    if not key:
        raise ICloudAPIError("iCloud API Key 未配置，请填写 ICLOUD_API_KEY")
    return base, key, timeout, _mode()


def _request(method: str, path: str, *, json_data: dict | None = None, allowed_statuses: tuple[int, ...] = (200,)):
    base, key, timeout, mode = _settings()
    auth_header = "X-HME-Import-Token" if mode == "inventory" else "X-API-Key"
    try:
        response = requests.request(
            method,
            f"{base}{path}",
            headers={auth_header: key, "Accept": "application/json"},
            json=json_data,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise ICloudAPIError(f"iCloud API 请求失败: {exc}") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code not in allowed_statuses:
        message = payload.get("error") if isinstance(payload, dict) else ""
        raise ICloudAPIError(f"iCloud API HTTP {response.status_code}: {message or response.text[:200]}")
    if not isinstance(payload, dict) or (response.status_code == 200 and payload.get("ok") is False):
        raise ICloudAPIError(f"iCloud API 返回异常: {payload}")
    return response.status_code, payload


def _acquire_account(email: str = "", *, reuse: bool = False) -> ICloudAccount:
    mode = _mode()
    if mode == "inventory":
        if email or reuse:
            raise ICloudAPIError("服务器库存模式不支持按指定地址重新领取")
        _, payload = _request(
            "POST",
            "/api/integrations/registration-inventory/lease",
            json_data={"clientId": "turb-gpt-free-register", "label": "OpenAI registration"},
        )
        lease = payload.get("lease") if isinstance(payload, dict) else None
        if not isinstance(lease, dict):
            raise ICloudAPIError("iCloud 服务器库存响应缺少 lease")
        account_email = str(lease.get("email") or "").strip()
        lease_id = str(lease.get("leaseId") or "").strip()
        if not account_email or not lease_id:
            raise ICloudAPIError("iCloud 服务器库存响应缺少 email/leaseId")
        account = ICloudAccount(email=account_email, lease_id=lease_id)
        with _LOCK:
            _CONTEXT_CACHE[account_email.lower()] = account
        return account

    request_data = {"email": email, "reuse": reuse} if email else {}
    _, payload = _request("POST", "/api/v1/mailboxes/acquire", json_data=request_data)
    email = str(payload.get("email") or "").strip()
    lease_id = str(payload.get("lease_id") or "").strip()
    if not email or not lease_id:
        raise ICloudAPIError("iCloud API 领取响应缺少 email/lease_id")
    account = ICloudAccount(email=email, lease_id=lease_id)
    with _LOCK:
        _CONTEXT_CACHE[email.lower()] = account
    return account


def pick_account() -> ICloudAccount:
    return _acquire_account()


def get_account_context(email: str) -> ICloudAccount | None:
    with _LOCK:
        return _CONTEXT_CACHE.get((email or "").lower())


def fetch_latest_otp(
    email: str,
    *,
    after_ts: float,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    del settle_seconds  # Gateway already returns the newest matching message.
    account = get_account_context(email)
    mode = _mode()
    if account is None and mode == "gateway":
        # Existing registered accounts can request a fresh lease for later
        # Codex OAuth / 2FA retries without exposing IMAP credentials locally.
        account = _acquire_account(email, reuse=True)
    elif account is None:
        # The server code endpoint can retrieve OTP for an exact alias without
        # creating a new inventory lease (for example a later OAuth/2FA retry).
        account = ICloudAccount(email=email, lease_id="")
    wait_seconds = max(1, int(max_wait if max_wait is not None else getattr(_email_cfg, "OTP_MAX_WAIT", 90)))
    interval = max(1, int(poll_interval if poll_interval is not None else getattr(_email_cfg, "OTP_POLL_INTERVAL", 3)))
    deadline = time.monotonic() + wait_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if mode == "inventory":
                since = datetime.fromtimestamp(float(after_ts), tz=timezone.utc).isoformat()
                status, payload = _request(
                    "POST",
                    "/api/integrations/workbench/openai-code",
                    json_data={"email": account.email, "since": since},
                    allowed_statuses=(200, 404),
                )
            else:
                status, payload = _request(
                    "POST",
                    "/api/v1/mailboxes/otp",
                    json_data={"email": account.email, "lease_id": account.lease_id, "after_ts": float(after_ts)},
                    allowed_statuses=(200, 202),
                )
            code = str(payload.get("code") or "").strip()
            if status == 200 and code.isdigit() and len(code) == 6:
                logger.info("[iCloud] 收到 OTP: email=%s", email)
                return code
        except ICloudAPIError as exc:
            last_error = exc
            logger.warning("[iCloud] OTP 查询失败，将重试: %s", exc)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(interval, remaining))
    suffix = f"; last={last_error}" if last_error else ""
    raise ICloudAPIError(f"等待 iCloud OTP 超时（{wait_seconds}s）: {email}{suffix}")


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    account = get_account_context(email)
    if account is None:
        return
    if _mode() == "inventory":
        _request(
            "POST",
            "/api/integrations/registration-inventory/result",
            json_data={
                "email": account.email,
                "leaseId": account.lease_id,
                "success": str(status or "").strip().lower() in {"used", "success", "registered"},
                "message": note or "",
            },
        )
    else:
        _request(
            "POST",
            "/api/v1/mailboxes/release",
            json_data={
                "email": account.email,
                "lease_id": account.lease_id,
                "status": status,
                "note": note or "",
            },
        )
    with _LOCK:
        _CONTEXT_CACHE.pop(email.lower(), None)
