# -*- coding: utf-8 -*-
"""Client for either the bundled gateway or the server inventory service."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

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

INVENTORY_EMAILS_PATH = "/api/integrations/registration-inventory/emails"


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
    request_kwargs = {
        "headers": {auth_header: key, "Accept": "application/json"},
        "json": json_data,
        "timeout": timeout,
    }
    # GET and workbench code lookup are read-only, so a TLS/proxy EOF can be
    # retried without duplicating a lease or a result callback.  Alternate the
    # environment-proxy route with the direct/TUN route to survive a flaky
    # localhost proxy while retaining normal DNS behavior on other machines.
    retryable = method.upper() == "GET" or path.startswith("/api/integrations/workbench/")
    attempts = 4 if retryable else 1
    response = None
    last_error: requests.RequestException | None = None
    for attempt in range(attempts):
        try:
            if attempt % 2:
                with requests.Session() as session:
                    session.trust_env = False
                    response = session.request(method, f"{base}{path}", **request_kwargs)
            else:
                response = requests.request(method, f"{base}{path}", **request_kwargs)
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(min(0.5 * (2 ** attempt), 2.0))
    if response is None:
        raise ICloudAPIError(f"iCloud API 请求失败（已重试 {attempts} 次）: {last_error}") from last_error
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


def list_inventory_emails(
    *,
    status: str | None = None,
    q: str = "",
    page: int = 1,
    page_size: int = 500,
) -> dict:
    """Pull the authenticated iCloud inventory into the local mailbox-pool shape."""

    if _mode() != "inventory":
        raise ICloudAPIError("iCloud 邮箱池拉取仅支持 inventory 模式")
    params = {
        "page": max(1, int(page or 1)),
        "pageSize": max(1, min(5000, int(page_size or 500))),
    }
    if status:
        params["status"] = str(status).strip()
    if q:
        params["q"] = str(q).strip()
    _, payload = _request("GET", f"{INVENTORY_EMAILS_PATH}?{urlencode(params)}")
    remote_items = payload.get("items")
    if not isinstance(remote_items, list):
        raise ICloudAPIError("iCloud 服务器邮箱池响应缺少 items")

    items: list[dict] = []
    for raw in remote_items:
        if not isinstance(raw, dict):
            continue
        email = str(raw.get("email") or "").strip()
        if not email:
            continue
        status_value = str(raw.get("status") or "available").strip().lower()
        items.append(
            {
                "email": email,
                "source": "icloud",
                "status": status_value,
                "remote_state": str(raw.get("state") or ""),
                "lease_status": str(raw.get("leaseStatus") or ""),
                "label": str(raw.get("label") or ""),
                "note": str(raw.get("note") or raw.get("leaseMessage") or ""),
                "created_at": str(raw.get("createdAt") or ""),
                "imported_at": str(raw.get("createdAt") or ""),
                "used_at": str(raw.get("completedAt") or ""),
                "updated_at": str(raw.get("updatedAt") or ""),
                "copy_line": email,
                "readonly": True,
                "otp_source": "服务器取码",
            }
        )
    return {
        "ok": True,
        "items": items,
        "total": int(payload.get("total") or len(items)),
        "page": int(payload.get("page") or params["page"]),
        "page_size": int(payload.get("pageSize") or params["pageSize"]),
        "counts": payload.get("counts") if isinstance(payload.get("counts"), dict) else {},
    }


def inventory_pool_summary() -> dict[str, int]:
    result = list_inventory_emails(page=1, page_size=1)
    counts = result.get("counts") or {}
    return {
        "total": int(result.get("total") or 0),
        "available": int(counts.get("available") or 0),
        "used": int(counts.get("used") or 0),
        "failed": int(counts.get("failed") or 0),
    }


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
