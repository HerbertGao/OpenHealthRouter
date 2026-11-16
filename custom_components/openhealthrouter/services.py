"""导出服务：预览与确认投递。

设计依据：openspec/changes/health-export/design.md D7/D8/D11/D14。
权限：`async_register_admin_service` 只在带 user_id 时校验管理员，
因此 handler 内再显式要求 user_id 存在且为管理员。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
from probatio.markers import Optional
from probatio.schema import Schema

from .const import DOMAIN
from .export import (
    ExportSelection,
    filter_digest,
    select_events,
    to_ndjson,
)
from .ingestion import RuntimeData
from .store import DeliveryRecord, time_key
from .views import runtime_for
from .webhook import WebhookTarget, async_deliver

_LOGGER = logging.getLogger(__name__)

SERVICE_EXPORT_WEBHOOK = "export_webhook"


def _opt_datetime(value: Any) -> Any:
    return None if value is None else cv.datetime(value)


def _opt_bool(value: Any) -> Any:
    return None if value is None else cv.boolean(value)


def _opt_str_list(value: Any) -> Any:
    if value is None:
        return None
    return vol.All(cv.ensure_list, [cv.string])(value)


# HA 2026.10 的 ServiceRegistry 只接受 probatio schema（voluptuous 已不再兼容）。
_EXPORT_SCHEMA = Schema(
    {
        Optional("metric", default=None): _opt_str_list,
        Optional("since", default=None): _opt_datetime,
        Optional("until", default=None): _opt_datetime,
        Optional("quality_status", default=None): _opt_str_list,
        Optional("include_raw_attributes", default=None): _opt_bool,
        Optional("include_device_refs", default=None): _opt_bool,
        Optional("include_subject", default=None): _opt_bool,
        Optional("include_context", default=None): _opt_bool,
        Optional("confirm", default=False): _opt_bool,
    }
)


def load_target(hass: HomeAssistant) -> dict[str, Any] | None:
    """从唯一配置条目读出 Webhook 目标；未配置返回 None。"""
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        return None
    target = entries[0].options.get("webhook_target")
    return target if isinstance(target, dict) else None


def _selection_from_call(data: dict[str, Any]) -> ExportSelection:
    metrics = data.get("metric")
    quality = data.get("quality_status")
    return ExportSelection(
        metrics=frozenset(metrics) if metrics else None,
        since=data.get("since"),
        until=data.get("until"),
        quality_status=frozenset(quality) if quality else frozenset({"valid"}),
        include_raw_attributes=bool(data.get("include_raw_attributes", False)),
        include_device_refs=bool(data.get("include_device_refs", False)),
        include_subject=bool(data.get("include_subject", False)),
        include_context=bool(data.get("include_context", False)),
    )


def _preview(
    selection: ExportSelection, target: dict[str, Any], events: list[Any]
) -> dict[str, Any]:
    derived = sum(1 for event in events if event.provenance.type == "derived.vendor")
    with_issues = sum(1 for event in events if event.quality.issues)
    return {
        "target_ref": target.get("name"),
        "target_url": target.get("url"),
        "metrics": None if selection.metrics is None else sorted(selection.metrics),
        "since": None if selection.since is None else selection.since.isoformat(),
        "until": None if selection.until is None else selection.until.isoformat(),
        "event_count": len(events),
        "quality_status": sorted(selection.quality_status),
        "excluded_non_selected_quality": "已排除非所选质量状态的事件",
        "includes_vendor_derived": derived,
        "includes_quality_issue_events": with_issues,
        "superseded_excluded": "被修正旧版本默认不导出",
        "retention": "已发送到外部的数据无法回收",
        "layers": {
            "include_raw_attributes": selection.include_raw_attributes,
            "include_device_refs": selection.include_device_refs,
            "include_subject": selection.include_subject,
            "include_context": selection.include_context,
        },
    }


async def _require_admin(hass: HomeAssistant, call: ServiceCall) -> None:
    user_id = call.context.user_id
    if user_id is None:
        raise Unauthorized(context=call.context)
    user = await hass.auth.async_get_user(user_id)
    if user is None or not user.is_admin:
        raise Unauthorized(context=call.context)


async def async_handle_export_webhook(
    hass: HomeAssistant, call: ServiceCall
) -> dict[str, Any]:
    await _require_admin(hass, call)

    runtime: RuntimeData | None = runtime_for(hass)
    if runtime is None or not runtime.store.is_available:
        raise HomeAssistantError("集成尚未就绪")
    target = load_target(hass)
    if target is None:
        raise HomeAssistantError("未配置 Webhook 目标")

    selection = _selection_from_call(dict(call.data))
    events = await select_events(hass, runtime.store.store, selection)

    if not call.data.get("confirm", False):
        return {"preview": _preview(selection, target, events)}

    delivery_id = uuid.uuid4().hex
    created_at = time_key(datetime.now(UTC))
    since_key = None if selection.since is None else time_key(selection.since)
    until_key = None if selection.until is None else time_key(selection.until)

    if not events:
        await runtime.store.record_delivery(
            DeliveryRecord(
                delivery_id=delivery_id,
                target_ref=str(target.get("name", "")),
                filter_digest=filter_digest(selection),
                since_key=since_key,
                until_key=until_key,
                event_count=0,
                event_ids=[],
                status="skipped_no_events",
                error_category=None,
                created_at=created_at,
            )
        )
        return {
            "delivery_id": delivery_id,
            "status": "skipped_no_events",
            "event_count": 0,
        }

    body = to_ndjson(events, selection)
    outcome = await async_deliver(
        hass,
        WebhookTarget(
            name=str(target.get("name", "")),
            url=str(target.get("url", "")),
            secret=str(target.get("secret", "")),
        ),
        body,
        delivery_id,
    )
    status = "success" if outcome.status == "success" else "failed"
    event_ids: list[str] = [
        event.event_id for event in events if event.event_id is not None
    ]
    if len(event_ids) != len(events):
        raise HomeAssistantError("事件缺少 eventId，无法记录投递")
    await runtime.store.record_delivery(
        DeliveryRecord(
            delivery_id=delivery_id,
            target_ref=str(target.get("name", "")),
            filter_digest=filter_digest(selection),
            since_key=since_key,
            until_key=until_key,
            event_count=len(events),
            event_ids=event_ids,
            status=status,
            error_category=outcome.error_category,
            created_at=created_at,
        )
    )
    _LOGGER.info(
        "投递完成 target_ref=%s delivery_id=%s event_count=%s status=%s",
        target.get("name"),
        delivery_id,
        len(events),
        status,
    )
    return {
        "delivery_id": delivery_id,
        "status": status,
        "event_count": len(events),
        "error_category": outcome.error_category,
    }


def async_register_services(hass: HomeAssistant) -> None:
    # 管理服务注册本身会拦非管理员；handler 内还有一道显式校验，
    # 因为注册器在 call.context.user_id 为空时会放行。
    async def _handler(call: ServiceCall) -> dict[str, Any]:
        return await async_handle_export_webhook(hass, call)

    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_EXPORT_WEBHOOK,
        _handler,
        schema=_EXPORT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
