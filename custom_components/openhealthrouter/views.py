"""管理员认证的 NDJSON 导出视图。

设计依据：openspec/changes/health-export/design.md D5/D6。
HA 没有视图注销 API：视图只注册一次，运行时对象不在时返回 404。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from aiohttp import web
from homeassistant.components.http.const import KEY_HASS_USER
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import KEY_HASS, HomeAssistantView

from .const import DOMAIN
from .export import ExportSelection, iter_export_batches, to_ndjson
from .ingestion import RuntimeData

EXPORT_URL = f"/api/{DOMAIN}/export.ndjson"
VIEW_NAME = f"api:{DOMAIN}:export"

_ALLOWED_PARAMS = frozenset(
    {
        "metric",
        "since",
        "until",
        "quality_status",
        "include_raw_attributes",
        "include_device_refs",
        "include_subject",
        "include_context",
    }
)
_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})


class InvalidParams(ValueError):
    """查询参数非法。"""


def runtime_for(hass: HomeAssistant) -> RuntimeData | None:
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        return None
    return getattr(entries[0], "runtime_data", None)


def _parse_bool(name: str, raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise InvalidParams(f"{name} 需要布尔值")


def _parse_time(name: str, raw: str | None) -> datetime | None:
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidParams(f"{name} 需要 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise InvalidParams(f"{name} 必须带时区")
    return parsed


def parse_selection(query: Any) -> ExportSelection:
    """把查询串解析为策略；未知参数一律拒绝。"""
    keys = set(query.keys())
    unknown = keys - _ALLOWED_PARAMS
    if unknown:
        raise InvalidParams(f"未知参数: {sorted(unknown)}")

    metrics = frozenset(query.getall("metric", []))
    quality = frozenset(query.getall("quality_status", []))
    return ExportSelection(
        metrics=metrics or None,
        since=_parse_time("since", query.get("since")),
        until=_parse_time("until", query.get("until")),
        quality_status=quality or frozenset({"valid"}),
        include_raw_attributes=_parse_bool(
            "include_raw_attributes", query.get("include_raw_attributes"), False
        ),
        include_device_refs=_parse_bool(
            "include_device_refs", query.get("include_device_refs"), False
        ),
        include_subject=_parse_bool(
            "include_subject", query.get("include_subject"), False
        ),
        include_context=_parse_bool(
            "include_context", query.get("include_context"), False
        ),
    )


class ExportNdjsonView(HomeAssistantView):
    """流式导出；非管理员与不可用状态都不返回任何事件内容。"""

    url = EXPORT_URL
    name = VIEW_NAME
    requires_auth = True

    async def get(self, request: web.Request) -> web.StreamResponse:
        hass: HomeAssistant = request.app[KEY_HASS]
        runtime = runtime_for(hass)
        if runtime is None or not runtime.store.is_available:
            return web.Response(status=404)

        user = request.get(KEY_HASS_USER)
        if user is None or not user.is_admin:
            return web.Response(status=403)

        try:
            selection = parse_selection(request.rel_url.query)
        except InvalidParams as exc:
            return web.json_response(
                {"error": "invalid_params", "detail": str(exc)}, status=400
            )

        response = web.StreamResponse(
            status=200, headers={"Content-Type": "application/x-ndjson"}
        )
        await response.prepare(request)
        async for batch in iter_export_batches(hass, runtime.store.store, selection):
            await response.write(to_ndjson(batch, selection))
        await response.write_eof()
        return response
