"""任务 6.1–6.7：NDJSON 下载、Webhook 投递、审计与隐私验收。"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import socket
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer, make_mocked_request
from homeassistant.components.http.const import KEY_HASS_USER
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers.http import KEY_HASS
from homeassistant.setup import async_setup_component

from custom_components.openhealthrouter.const import DOMAIN
from custom_components.openhealthrouter.services import SERVICE_EXPORT_WEBHOOK
from custom_components.openhealthrouter.views import (
    EXPORT_URL,
    ExportNdjsonView,
    runtime_for,
)

SECRET = "synthetic-test-secret"

# 本模块要起本地接收端验证签名与传输；PHACC 默认封 socket。
pytestmark = pytest.mark.usefixtures("socket_enabled")


def _as_dict(response: Any) -> dict[str, Any]:
    assert isinstance(response, dict), response
    return response


class Receiver:
    """本地接收端：记录原始 body、头部与请求次数。"""

    def __init__(self, *, status: int = 200, delay: float = 0.0) -> None:
        self.bodies: list[bytes] = []
        self.headers: list[dict[str, str]] = []
        self.status = status
        self.delay = delay

    async def handle(self, request: web.Request) -> web.Response:
        import asyncio

        self.bodies.append(await request.read())
        self.headers.append(dict(request.headers))
        if self.delay:
            await asyncio.sleep(self.delay)
        return web.Response(status=self.status)


async def _start(receiver: Receiver) -> TestServer:
    app = web.Application()
    app.router.add_post("/hook", receiver.handle)
    server = TestServer(app)
    await server.start_server()
    return server


def _url(server: TestServer) -> str:
    return str(server.make_url("/hook"))


async def _set_target(hass: HomeAssistant, entry: Any, url: str) -> None:
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "webhook_target": {"name": "local-test", "url": url, "secret": SECRET},
        },
    )


async def _non_admin(hass: HomeAssistant) -> Any:
    """HA 把首个创建的用户提升为 owner；先占位再建普通用户。"""
    await hass.auth.async_create_user("ohr-owner", group_ids=["system-admin"])
    user = await hass.auth.async_create_user("ohr-plain", group_ids=["system-users"])
    assert user.is_admin is False
    return user


async def _admin_context(hass: HomeAssistant) -> Context:
    user = await hass.auth.async_create_user("ohr-admin", group_ids=["system-admin"])
    return Context(user_id=user.id)


def _mocked(hass: HomeAssistant, user: Any, path: str = EXPORT_URL) -> Any:
    # app 必须是真正的 aiohttp Application；用户由 HA 中间件写进 request state
    app = web.Application()
    app[KEY_HASS] = hass
    request = make_mocked_request("GET", path, app=app)
    request[KEY_HASS_USER] = user
    return request


async def _deliver(
    hass: HomeAssistant, context: Context, data: dict[str, Any]
) -> dict[str, Any]:
    return _as_dict(
        await hass.services.async_call(
            DOMAIN,
            SERVICE_EXPORT_WEBHOOK,
            data,
            blocking=True,
            return_response=True,
            context=context,
        )
    )


async def test_view_rejects_non_admin(hass: HomeAssistant, setup_entry: Any) -> None:
    await setup_entry([])
    user = await _non_admin(hass)
    response = await ExportNdjsonView().get(_mocked(hass, user))
    assert response.status == 403


async def test_view_returns_404_without_runtime(hass: HomeAssistant) -> None:
    user = await hass.auth.async_create_user("ohr-admin2", group_ids=["system-admin"])
    response = await ExportNdjsonView().get(_mocked(hass, user))
    assert response.status == 404


async def test_view_rejects_unknown_params(
    hass: HomeAssistant, setup_entry: Any
) -> None:
    await setup_entry([])
    user = await hass.auth.async_create_user("ohr-admin3", group_ids=["system-admin"])
    response = await ExportNdjsonView().get(
        _mocked(hass, user, f"{EXPORT_URL}?bogus=1")
    )
    assert response.status == 400


async def test_view_streams_without_content_length(
    hass: HomeAssistant,
    hass_client: Any,
    setup_entry: Any,
    make_event: Any,
    seed_events: Any,
) -> None:
    assert await async_setup_component(hass, "http", {})  # 先建 http，视图才会注册
    entry = await setup_entry([])
    client = await hass_client()
    await seed_events(entry, [make_event(value=70.0 + index) for index in range(3)])

    response = await client.get(EXPORT_URL)
    assert response.status == 200
    assert "Content-Length" not in response.headers
    body = await response.read()
    assert len(body.decode("utf-8").splitlines()) == 3


async def test_view_zero_match_returns_empty_200(
    hass: HomeAssistant, hass_client: Any, setup_entry: Any
) -> None:
    assert await async_setup_component(hass, "http", {})
    await setup_entry([])
    client = await hass_client()
    response = await client.get(f"{EXPORT_URL}?metric=body.bmi")
    assert response.status == 200
    assert await response.read() == b""


async def test_preview_does_not_send_or_record(
    hass: HomeAssistant, setup_entry: Any, make_event: Any, seed_events: Any
) -> None:
    entry = await setup_entry([])
    await seed_events(entry, [make_event()])
    receiver = Receiver()
    server = await _start(receiver)
    try:
        await _set_target(hass, entry, _url(server))
        context = await _admin_context(hass)
        result = await _deliver(hass, context, {})

        preview = result["preview"]
        assert isinstance(preview, dict)
        assert preview["event_count"] == 1
        assert preview["includes_vendor_derived"] == 0
        assert preview["quality_status"] == ["valid"]
        assert preview["superseded_excluded"]
        assert preview["retention"]
        assert receiver.bodies == []

        store = entry.runtime_data.store.store
        assert await hass.async_add_executor_job(store.list_deliveries) == []
    finally:
        await server.close()


async def test_confirm_sends_and_records_with_matching_signature(
    hass: HomeAssistant, setup_entry: Any, make_event: Any, seed_events: Any
) -> None:
    entry = await setup_entry([])
    await seed_events(entry, [make_event(value=70.0 + index) for index in range(3)])
    receiver = Receiver()
    server = await _start(receiver)
    try:
        await _set_target(hass, entry, _url(server))
        context = await _admin_context(hass)
        result = await _deliver(hass, context, {"confirm": True})

        assert result["status"] == "success"
        assert result["event_count"] == 3

        assert len(receiver.bodies) == 1
        body = receiver.bodies[0]
        expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        headers = receiver.headers[0]
        assert headers["X-OpenHealthRouter-Signature"] == f"sha256={expected}"
        assert headers["X-OpenHealthRouter-Delivery-Id"] == result["delivery_id"]

        store = entry.runtime_data.store.store
        records = await hass.async_add_executor_job(store.list_deliveries)
        assert len(records) == 1
        record = records[0]
        assert record["status"] == "success"
        assert record["event_count"] == len(body.decode().splitlines())
        assert len(record["event_ids"]) == record["event_count"]
        assert SECRET not in json.dumps(record)
    finally:
        await server.close()


async def test_two_outlets_produce_identical_bytes(
    hass: HomeAssistant,
    hass_client: Any,
    setup_entry: Any,
    make_event: Any,
    seed_events: Any,
) -> None:
    assert await async_setup_component(hass, "http", {})
    entry = await setup_entry([])
    events = [
        make_event(
            value=70.0 + index,
            observedAt={
                "start": f"2026-11-12T08:{index // 60:02d}:{index % 60:02d}+08:00",
                "precision": "second",
                "timezone": "Asia/Shanghai",
            },
        )
        for index in range(205)
    ]
    await seed_events(entry, events)

    client = await hass_client()
    response = await client.get(EXPORT_URL)
    downloaded = await response.read()

    receiver = Receiver()
    server = await _start(receiver)
    try:
        await _set_target(hass, entry, _url(server))
        context = await _admin_context(hass)
        await _deliver(hass, context, {"confirm": True})
        assert receiver.bodies[0] == downloaded
    finally:
        await server.close()


async def test_redirect_is_not_followed(
    hass: HomeAssistant, setup_entry: Any, make_event: Any, seed_events: Any
) -> None:
    entry = await setup_entry([])
    await seed_events(entry, [make_event()])
    receiver = Receiver(status=302)
    server = await _start(receiver)
    try:
        await _set_target(hass, entry, _url(server))
        context = await _admin_context(hass)
        result = await _deliver(hass, context, {"confirm": True})
        assert result["status"] == "failed"
        assert result["error_category"] == "http_error"
        assert len(receiver.bodies) == 1
    finally:
        await server.close()


async def test_timeout_is_classified_and_events_kept(
    hass: HomeAssistant,
    setup_entry: Any,
    make_event: Any,
    seed_events: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.openhealthrouter.webhook as webhook_module

    monkeypatch.setattr(webhook_module, "WEBHOOK_TIMEOUT_S", 0.05)
    entry = await setup_entry([])
    await seed_events(entry, [make_event()])

    receiver = Receiver(delay=0.3)
    server = await _start(receiver)
    try:
        await _set_target(hass, entry, _url(server))
        context = await _admin_context(hass)
        result = await _deliver(hass, context, {"confirm": True})
        assert result["status"] == "failed"
        assert result["error_category"] == "timeout"

        store = entry.runtime_data.store.store
        events_left, total = await hass.async_add_executor_job(store.query)
        assert total == 1
        assert events_left[0].value == pytest.approx(72.4)
    finally:
        await server.close()


async def test_non_admin_service_call_is_unauthorised(
    hass: HomeAssistant, setup_entry: Any
) -> None:
    await setup_entry([])
    user = await _non_admin(hass)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_EXPORT_WEBHOOK,
            {},
            blocking=True,
            context=Context(user_id=user.id),
        )


async def test_missing_target_fails_without_request(
    hass: HomeAssistant, setup_entry: Any, make_event: Any, seed_events: Any
) -> None:
    entry = await setup_entry([])
    await seed_events(entry, [make_event()])
    context = await _admin_context(hass)
    with pytest.raises(HomeAssistantError):
        await _deliver(hass, context, {"confirm": True})
    store = entry.runtime_data.store.store
    assert await hass.async_add_executor_job(store.list_deliveries) == []


async def test_zero_events_is_skipped_without_request(
    hass: HomeAssistant, setup_entry: Any, make_event: Any, seed_events: Any
) -> None:
    entry = await setup_entry([])
    await seed_events(entry, [make_event()])
    receiver = Receiver()
    server = await _start(receiver)
    try:
        await _set_target(hass, entry, _url(server))
        context = await _admin_context(hass)
        result = await _deliver(
            hass, context, {"confirm": True, "metric": ["body.bmi"]}
        )
        assert result["status"] == "skipped_no_events"
        assert result["event_count"] == 0
        assert receiver.bodies == []
    finally:
        await server.close()


async def test_retry_creates_new_record(
    hass: HomeAssistant, setup_entry: Any, make_event: Any, seed_events: Any
) -> None:
    entry = await setup_entry([])
    await seed_events(entry, [make_event()])
    receiver = Receiver()
    server = await _start(receiver)
    try:
        await _set_target(hass, entry, _url(server))
        context = await _admin_context(hass)
        first = await _deliver(hass, context, {"confirm": True})
        second = await _deliver(hass, context, {"confirm": True})
        assert first["delivery_id"] != second["delivery_id"]
        store = entry.runtime_data.store.store
        records = await hass.async_add_executor_job(store.list_deliveries)
        assert len(records) == 2
    finally:
        await server.close()


async def test_audit_and_logs_are_minimal(
    hass: HomeAssistant,
    setup_entry: Any,
    make_event: Any,
    seed_events: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await setup_entry([])
    await seed_events(entry, [make_event()])
    receiver = Receiver()
    server = await _start(receiver)
    try:
        await _set_target(hass, entry, _url(server))
        context = await _admin_context(hass)
        with caplog.at_level(logging.DEBUG):
            await _deliver(hass, context, {"confirm": True})

        store = entry.runtime_data.store.store
        records = await hass.async_add_executor_job(store.list_deliveries)
        serialised = json.dumps(records)
        for forbidden in ("72.4", "unit_of_measurement", SECRET, _url(server)):
            assert forbidden not in serialised

        text = caplog.text
        assert "local-test" in text
        for forbidden in ("72.4", SECRET, _url(server), "Traceback"):
            assert forbidden not in text
    finally:
        await server.close()


async def test_idle_setup_makes_no_outbound_request(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("空闲期不应有出站连接")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "sendto", deny, raising=False)
    await setup_entry([mapping_factory()])
    await hass.async_block_till_done()
    assert runtime_for(hass) is not None


async def test_quality_filter_defaults_and_override(
    hass: HomeAssistant, setup_entry: Any, make_event: Any, seed_events: Any
) -> None:
    entry = await setup_entry([])
    await seed_events(
        entry,
        [
            make_event(value=71.0),
            make_event(
                value=72.0,
                quality={
                    "status": "suspect",
                    "confirmation": "unconfirmed",
                    "issues": [],
                },
            ),
            make_event(
                value=73.0,
                quality={
                    "status": "invalid",
                    "confirmation": "unconfirmed",
                    "issues": [],
                },
            ),
        ],
    )
    store = entry.runtime_data.store.store
    from custom_components.openhealthrouter.export import (
        ExportSelection,
        select_events,
    )

    default_events = await select_events(hass, store, ExportSelection())
    assert [event.value for event in default_events] == [pytest.approx(71.0)]

    with_suspect = await select_events(
        hass, store, ExportSelection(quality_status=frozenset({"suspect"}))
    )
    assert [event.value for event in with_suspect] == [pytest.approx(72.0)]


async def test_empty_filtered_batch_does_not_truncate(
    hass: HomeAssistant, setup_entry: Any, make_event: Any, seed_events: Any
) -> None:
    """前一批全被质量过滤时，迭代器必须继续读，不能静默截断。"""
    entry = await setup_entry([])
    await seed_events(
        entry,
        [
            make_event(
                value=70.0,
                quality={
                    "status": "suspect",
                    "confirmation": "unconfirmed",
                    "issues": [],
                },
            ),
            make_event(
                value=71.0,
                quality={
                    "status": "suspect",
                    "confirmation": "unconfirmed",
                    "issues": [],
                },
            ),
            make_event(value=72.0),
        ],
    )
    store = entry.runtime_data.store.store
    from custom_components.openhealthrouter.export import (
        ExportSelection,
        iter_export_batches,
    )

    seen: list[float] = []
    async for batch in iter_export_batches(hass, store, ExportSelection(), batch=1):
        seen.extend(event.value for event in batch if event.value is not None)
    assert seen == [pytest.approx(72.0)]


async def test_unauthenticated_request_is_401(
    hass: HomeAssistant, hass_client_no_auth: Any, setup_entry: Any
) -> None:
    assert await async_setup_component(hass, "http", {})
    await setup_entry([])
    client = await hass_client_no_auth()
    response = await client.get(EXPORT_URL)
    assert response.status == 401


async def test_view_returns_404_after_unload(
    hass: HomeAssistant, hass_client: Any, setup_entry: Any
) -> None:
    assert await async_setup_component(hass, "http", {})
    entry = await setup_entry([])
    client = await hass_client()
    assert (await client.get(EXPORT_URL)).status == 200
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert (await client.get(EXPORT_URL)).status == 404


async def test_service_is_removed_on_unload(
    hass: HomeAssistant, setup_entry: Any
) -> None:
    entry = await setup_entry([])
    assert hass.services.has_service(DOMAIN, SERVICE_EXPORT_WEBHOOK)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, SERVICE_EXPORT_WEBHOOK)


async def test_service_call_without_user_is_rejected(
    hass: HomeAssistant, setup_entry: Any
) -> None:
    await setup_entry([])
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN, SERVICE_EXPORT_WEBHOOK, {}, blocking=True
        )


async def test_tls_failure_is_network_error(
    hass: HomeAssistant, setup_entry: Any, make_event: Any, seed_events: Any
) -> None:
    """目标是纯 HTTP 端口却用 https:// 访问：握手失败必须归为网络错误。"""
    entry = await setup_entry([])
    await seed_events(entry, [make_event()])
    receiver = Receiver()
    server = await _start(receiver)
    try:
        plain = _url(server).replace("http://", "https://")
        await _set_target(hass, entry, plain)
        context = await _admin_context(hass)
        result = await _deliver(hass, context, {"confirm": True})
        assert result["status"] == "failed"
        assert result["error_category"] == "network_error"
        assert receiver.bodies == []
    finally:
        await server.close()


async def test_target_config_validation_and_no_secret_prefill(
    hass: HomeAssistant, setup_entry: Any
) -> None:
    from homeassistant.data_entry_flow import FlowResultType

    entry = await setup_entry([])
    setup = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(
        setup["flow_id"], {"next_step_id": "webhook"}
    )
    assert form.get("type") is FlowResultType.FORM

    # 密钥不回填：空提交时 schema 给出的 secret 默认值为空串
    schema = form.get("data_schema")
    assert callable(schema)
    validated = schema({})
    assert isinstance(validated, dict)
    assert validated["secret"] == ""

    cases = [
        ({"url": "http://example.test/hook"}, "target_url_not_https"),
        (
            {"url": "https://user:pass@example.test/hook"},
            "target_url_has_credentials",
        ),
        ({"url": "https://example.test/hook?token=1"}, "target_url_has_query"),
        ({"name": "x://y"}, "target_name_invalid"),
    ]
    base = {"name": "local", "url": "https://example.test/hook", "secret": SECRET}
    for overrides, expected in cases:
        result = await hass.config_entries.options.async_configure(
            setup["flow_id"], {**base, **overrides}
        )
        assert result.get("type") is FlowResultType.FORM, expected
        errors = result.get("errors") or {}
        assert expected in errors, (expected, errors)
        assert "webhook_target" not in entry.options

    result = await hass.config_entries.options.async_configure(setup["flow_id"], base)
    assert result.get("type") is FlowResultType.CREATE_ENTRY
    assert entry.options["webhook_target"]["url"] == "https://example.test/hook"


async def test_day_precision_window_across_timezones(
    hass: HomeAssistant, setup_entry: Any, make_event: Any, seed_events: Any
) -> None:
    """窗口按 UTC 键左闭右开；日期级事件按相交计入，归组用事件自身时区。"""
    entry = await setup_entry([])
    await seed_events(
        entry,
        [
            make_event(
                value=70.0,
                observedAt={
                    "start": "2026-11-12",
                    "precision": "day",
                    "timezone": "Asia/Shanghai",
                },
            ),
            make_event(
                value=71.0,
                observedAt={
                    "start": "2026-11-11",
                    "precision": "day",
                    "timezone": "Asia/Shanghai",
                },
            ),
            make_event(
                value=72.0,
                observedAt={
                    "start": "2026-11-12",
                    "precision": "day",
                    "timezone": "America/New_York",
                },
            ),
            make_event(
                value=73.0,
                observedAt={
                    "start": "2026-11-13",
                    "precision": "day",
                    "timezone": "America/New_York",
                },
            ),
        ],
    )
    store = entry.runtime_data.store.store
    from datetime import datetime

    from custom_components.openhealthrouter.export import (
        ExportSelection,
        select_events,
    )

    selection = ExportSelection(
        since=datetime.fromisoformat("2026-11-12T00:00:00+08:00"),
        until=datetime.fromisoformat("2026-11-13T00:00:00+08:00"),
    )
    events = await select_events(hass, store, selection)
    assert [event.value for event in events] == [
        pytest.approx(72.0),
        pytest.approx(70.0),
    ], "上海 12 日与纽约 12 日相交计入，11 日与纽约 13 日排除"


async def test_server_error_is_http_error(
    hass: HomeAssistant, setup_entry: Any, make_event: Any, seed_events: Any
) -> None:
    entry = await setup_entry([])
    await seed_events(entry, [make_event()])
    receiver = Receiver(status=500)
    server = await _start(receiver)
    try:
        await _set_target(hass, entry, _url(server))
        context = await _admin_context(hass)
        result = await _deliver(hass, context, {"confirm": True})
        assert result["status"] == "failed"
        assert result["error_category"] == "http_error"
    finally:
        await server.close()


async def test_timestamp_header_is_recent_epoch(
    hass: HomeAssistant, setup_entry: Any, make_event: Any, seed_events: Any
) -> None:
    import time

    entry = await setup_entry([])
    await seed_events(entry, [make_event()])
    receiver = Receiver()
    server = await _start(receiver)
    try:
        await _set_target(hass, entry, _url(server))
        context = await _admin_context(hass)
        await _deliver(hass, context, {"confirm": True})
        stamp = int(receiver.headers[0]["X-OpenHealthRouter-Timestamp"])
        assert abs(time.time() - stamp) < 60
    finally:
        await server.close()


async def test_streaming_uses_executor_without_blocking_warning(
    hass: HomeAssistant,
    hass_client: Any,
    setup_entry: Any,
    make_event: Any,
    seed_events: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert await async_setup_component(hass, "http", {})
    entry = await setup_entry([])
    await seed_events(entry, [make_event(value=70.0 + index) for index in range(3)])
    client = await hass_client()
    with caplog.at_level(logging.WARNING):
        response = await client.get(EXPORT_URL)
        assert response.status == 200
        await response.read()
    assert "Detected blocking call" not in caplog.text


async def test_two_outlets_leave_store_untouched(
    hass: HomeAssistant,
    hass_client: Any,
    setup_entry: Any,
    make_event: Any,
    seed_events: Any,
) -> None:
    assert await async_setup_component(hass, "http", {})
    entry = await setup_entry([])
    await seed_events(entry, [make_event(value=70.0 + index) for index in range(5)])
    store = entry.runtime_data.store.store
    before = await hass.async_add_executor_job(store.query)

    client = await hass_client()
    assert (await client.get(EXPORT_URL)).status == 200

    after = await hass.async_add_executor_job(store.query)
    assert after[1] == before[1]
    assert [event.event_id for event in after[0]] == [
        event.event_id for event in before[0]
    ]


async def test_repeated_setup_does_not_double_register_view(
    hass: HomeAssistant, setup_entry: Any
) -> None:
    from custom_components.openhealthrouter import _VIEW_REGISTERED

    assert await async_setup_component(hass, "http", {})
    first = await setup_entry([])
    assert hass.data[DOMAIN][_VIEW_REGISTERED] is True
    await hass.config_entries.async_unload(first.entry_id)
    await hass.async_block_till_done()
    await hass.config_entries.async_setup(first.entry_id)
    await hass.async_block_till_done()
    assert hass.data[DOMAIN][_VIEW_REGISTERED] is True
