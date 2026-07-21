from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from app.config import Settings
from app.db import Database


ALLOWED_DINGTALK_HOSTS = {"oapi.dingtalk.com", "api.dingtalk.com"}


class DingTalkConfigError(ValueError):
    pass


class DingTalkSendError(RuntimeError):
    pass


class DingTalkNotifier:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self._send_lock = asyncio.Lock()

    def _value(self, key: str, fallback: str | None = None) -> str | None:
        stored = self.db.get_setting(key)
        return stored if stored is not None else fallback

    def status(self) -> dict[str, Any]:
        webhook = self._value("dingtalk_webhook", self.settings.dingtalk_webhook)
        secret = self._value("dingtalk_secret", self.settings.dingtalk_secret)
        stored_enabled = self._value("dingtalk_enabled")
        enabled = stored_enabled == "1" if stored_enabled is not None else self.settings.dingtalk_enabled
        masked = None
        if webhook:
            parsed = urlparse(webhook)
            masked = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?access_token=***"
        return {
            "enabled": bool(enabled),
            "configured": bool(webhook and secret),
            "webhook_masked": masked,
        }

    def _config(self) -> tuple[bool, str | None, str | None]:
        state = self.status()
        return (
            bool(state["enabled"]),
            self._value("dingtalk_webhook", self.settings.dingtalk_webhook),
            self._value("dingtalk_secret", self.settings.dingtalk_secret),
        )

    @staticmethod
    def validate_webhook(webhook: str) -> str:
        value = webhook.strip()
        parsed = urlparse(value)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_DINGTALK_HOSTS:
            raise DingTalkConfigError("Webhook 必须是钉钉官方 HTTPS 机器人地址")
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if not params.get("access_token"):
            raise DingTalkConfigError("Webhook 中缺少 access_token")
        return value

    def configure(
        self,
        enabled: bool,
        webhook: str | None = None,
        secret: str | None = None,
    ) -> dict[str, Any]:
        if webhook is not None and webhook.strip():
            self.db.set_setting("dingtalk_webhook", self.validate_webhook(webhook))
        if secret is not None and secret.strip():
            self.db.set_setting("dingtalk_secret", secret.strip())
        current_webhook = self._value("dingtalk_webhook", self.settings.dingtalk_webhook)
        current_secret = self._value("dingtalk_secret", self.settings.dingtalk_secret)
        if enabled and not (current_webhook and current_secret):
            raise DingTalkConfigError("启用通知前需要填写 Webhook 和加签密钥")
        self.db.set_setting("dingtalk_enabled", "1" if enabled else "0")
        return self.status()

    @staticmethod
    def signed_url(webhook: str, secret: str, timestamp: int | None = None) -> str:
        timestamp = timestamp or int(time.time() * 1000)
        string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
        digest = hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
        signature = base64.b64encode(digest).decode("ascii")
        parsed = urlparse(webhook)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        query.extend([("timestamp", str(timestamp)), ("sign", signature)])
        return urlunparse(parsed._replace(query=urlencode(query)))

    async def send(self, title: str, details: dict[str, Any], level: str = "info") -> bool:
        enabled, webhook, secret = self._config()
        if not enabled:
            return False
        if not webhook or not secret:
            raise DingTalkConfigError("钉钉通知尚未完整配置")
        icon = {"success": "✅", "warning": "⚠️", "error": "❌"}.get(level, "ℹ️")
        lines = [f"### {icon} {title}", ""]
        for key, value in details.items():
            if value is None or value == "":
                continue
            rendered = str(value).replace("\n", "  \n")
            lines.append(f"- **{key}**：{rendered}")
        lines.extend(["", f"> 发送时间：{datetime.now().astimezone().isoformat(timespec='seconds')}"])
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title[:40], "text": "\n".join(lines)},
        }
        async with self._send_lock:
            last_error: str | None = None
            for attempt in range(3):
                try:
                    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                        response = await client.post(self.signed_url(webhook, secret), json=payload)
                    if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                        last_error = f"钉钉临时状态码 {response.status_code}"
                        if attempt < 2:
                            continue
                        raise DingTalkSendError(last_error)
                    if response.status_code < 200 or response.status_code >= 300:
                        raise DingTalkSendError(f"钉钉 HTTP 状态码 {response.status_code}")
                    try:
                        result = response.json()
                    except ValueError as exc:
                        raise DingTalkSendError("钉钉返回了无法解析的响应") from exc
                    if result.get("errcode") != 0:
                        raise RuntimeError(
                            f"钉钉返回 errcode={result.get('errcode')}：{result.get('errmsg', '未知错误')}"
                        )
                    return True
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    if attempt < 2:
                        continue
                    raise DingTalkSendError(last_error) from exc
        return False
