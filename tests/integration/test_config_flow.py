"""任务 4.2–4.3：Config Flow 与 Options Flow。"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.openhealthrouter.const import DOMAIN

from .conftest import WEIGHT_ENTITY


async def test_first_config_creates_single_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result.get("type") is FlowResultType.CREATE_ENTRY
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_second_config_aborts(hass: HomeAssistant) -> None:
    await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result.get("type") is FlowResultType.ABORT
    assert result.get("reason") == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


def _form_input(mapping: dict[str, Any]) -> dict[str, Any]:
    """表单只接受 schema 里的字段：去掉 mapping_id，空值转空串。"""
    payload = {k: v for k, v in mapping.items() if k != "mapping_id"}
    for key, value in list(payload.items()):
        if value is None:
            payload[key] = ""
    return payload


async def _start_add(hass: HomeAssistant, entry: Any) -> ConfigFlowResult:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result.get("type") is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add"}
    )


async def test_add_valid_mapping_is_saved_and_reloaded(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
) -> None:
    set_state()
    entry = await setup_entry([])

    form = await _start_add(hass, entry)
    assert form.get("type") is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        form["flow_id"], user_input=_form_input(mapping_factory())
    )
    await hass.async_block_till_done()

    assert result.get("type") is FlowResultType.CREATE_ENTRY
    assert len(entry.options["mappings"]) == 1
    assert entry.options["mappings"][0]["source_entity"] == WEIGHT_ENTITY


async def test_invalid_mappings_return_error_keys(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
) -> None:
    set_state()
    entry = await setup_entry([])
    cases = [
        ({"source_entity": "sensor.missing"}, "entity_not_found"),
        (
            {"source_entity": "sensor.openhealthrouter_ingested_events"},
            "entity_is_self",
        ),
        ({"metric": "body.muscle_mass"}, "metric_not_registered"),
        ({"unit": "jin"}, "unit_not_allowed"),
        # 未注册质量标记由 SelectSelector 在表单层就拒绝，只能由单元测试覆盖
        ({"provenance_type": "derived.vendor"}, "estimated_required"),
        ({"observed_at_attribute": None}, "time_source_missing"),
    ]
    for overrides, expected in cases:
        form = await _start_add(hass, entry)
        result = await hass.config_entries.options.async_configure(
            form["flow_id"], user_input=_form_input(mapping_factory(**overrides))
        )
        assert result.get("type") is FlowResultType.FORM, expected
        assert expected in result.get("errors", {}), (expected, result.get("errors"))
        assert entry.options.get("mappings", []) == []


async def test_delete_mapping_stops_ingestion(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
    read_events: Any,
) -> None:
    set_state()
    entry = await setup_entry([mapping_factory()])
    assert entry.runtime_data.listeners == 1

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "delete"}
    )
    assert result.get("type") is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"mapping_id": entry.options["mappings"][0]["mapping_id"]},
    )
    await hass.async_block_till_done()

    assert result.get("type") is FlowResultType.CREATE_ENTRY
    assert entry.options.get("mappings") == []
    assert entry.runtime_data.listeners == 0
    assert read_events(entry) == []


async def test_edit_that_changes_metric_removes_old_mapping(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
) -> None:
    """编辑改指标后只能剩一条映射：旧 mapping_id 不得残留（否则重复订阅）。"""
    set_state()
    entry = await setup_entry([mapping_factory()])
    original_id = entry.options["mappings"][0]["mapping_id"]

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"mapping_id": original_id}
    )
    assert result.get("type") is FlowResultType.FORM

    changed = mapping_factory(
        metric="body.bmi",
        unit="kg/m²",
        provenance_type="derived.openhealthrouter",
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=_form_input(changed)
    )
    await hass.async_block_till_done()

    mappings = entry.options["mappings"]
    assert len(mappings) == 1, mappings
    assert mappings[0]["metric"] == "body.bmi"
    assert mappings[0]["mapping_id"] != original_id
    assert entry.runtime_data.listeners == 1
