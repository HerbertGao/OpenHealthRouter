"""Webhook 投递与签名。

设计依据：openspec/changes/health-export/design.md D9/D11。
签名一律基于**实际发送的 body 字节**；不跟随重定向、不关闭共享 session、不设 skip_ssl。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

WEBHOOK_TIMEOUT_S = 10

SIGNATURE_HEADER = "X-OpenHealthRouter-Signature"
TIMESTAMP_HEADER = "X-OpenHealthRouter-Timestamp"
DELIVERY_ID_HEADER = "X-OpenHealthRouter-Delivery-Id"


@dataclass(frozen=True)
class WebhookTarget:
    """目标配置。secret 只用于签名，不进入 URL、审计或日志。"""

    name: str
    url: str
    secret: str


@dataclass(frozen=True)
class DeliveryOutcome:
    status: str
    error_category: str | None = None


def sign_body(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def build_headers(
    secret: str,
    body: bytes,
    delivery_id: str,
    *,
    timestamp: int | None = None,
) -> dict[str, str]:
    moment = int(time.time()) if timestamp is None else int(timestamp)
    return {
        "Content-Type": "application/x-ndjson",
        TIMESTAMP_HEADER: str(moment),
        DELIVERY_ID_HEADER: delivery_id,
        SIGNATURE_HEADER: f"sha256={sign_body(secret, body)}",
    }


async def async_deliver(
    hass: HomeAssistant,
    target: WebhookTarget,
    body: bytes,
    delivery_id: str,
) -> DeliveryOutcome:
    """投递一次；任何失败都折叠成可分类结果，不抛给调用方。"""
    session = async_get_clientsession(hass)
    headers = build_headers(target.secret, body, delivery_id)
    timeout = aiohttp.ClientTimeout(total=WEBHOOK_TIMEOUT_S)
    try:
        async with session.post(
            target.url,
            data=body,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        ) as response:
            if 200 <= response.status < 300:
                return DeliveryOutcome("success")
            return DeliveryOutcome("failed", "http_error")
    except TimeoutError:
        return DeliveryOutcome("failed", "timeout")
    except aiohttp.ClientError:
        return DeliveryOutcome("failed", "network_error")
