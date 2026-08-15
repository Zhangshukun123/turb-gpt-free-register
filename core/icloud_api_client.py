# -*- coding: utf-8 -*-
"""Client for the deployable iCloud Mail gateway."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

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


def _settings() -> tuple[str, str, int]:
    base = str(getattr(_email_cfg, "ICLOUD_API_BASE", "") or "").strip().rstrip("/")
    key = str(getattr(_email_cfg, "ICLOUD_API_KEY", "") or "").strip()
    timeout = max(1, int(getattr(_email_cfg, "ICLOUD_REQUEST_TIMEOUT", 25) or 25))
    if not base:
        raise ICloudAPIError("iCloud API 地址未配置，请填写 ICLOUD_API_BASE")
    if not key:
        raise ICloudAPIError("iCloud API Key 未配置，请填写 ICLOUD_API_KEY")
    return base, key, timeout


def _request(method: str, path: str, *, json_data: dict | None = None, allowed_statuses: tuple[int, ...] = (200,)):
    base, key, timeout = _settings()
    try:
        response = requests.request(
            method,
            f"{base}{path}",
            headers={"X-API-Key": key, "Accept": "application/json"},
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
    if not isinstance(payload, dict) or payload.get("ok") is False:
        raise ICloudAPIError(f"iCloud API 返回异常: {payload}")
    return response.status_code, payload


def _acquire_account(email: str = "", *, reuse: bool = False) -> ICloudAccount:
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
    if account is None:
        # Existing registered accounts can request a fresh lease for later
        # Codex OAuth / 2FA retries without exposing IMAP credentials locally.
        account = _acquire_account(email, reuse=True)
    wait_seconds = max(1, int(max_wait if max_wait is not None else getattr(_email_cfg, "OTP_MAX_WAIT", 90)))
    interval = max(1, int(poll_interval if poll_interval is not None else getattr(_email_cfg, "OTP_POLL_INTERVAL", 3)))
    deadline = time.monotonic() + wait_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
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
