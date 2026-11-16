"""配置流程：单实例条目 + 映射管理。

设计依据：openspec/changes/ha-entity-ingestion/design.md D1/D2/D10。

注意：HA 2026.10 的 `async_show_form` 期望 `probatio.schema.Schema`
（voluptuous 的后继），因此表单 schema 用 probatio 构建，字段仍用 HA Selector。
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from probatio.markers import Optional, Required
from probatio.schema import Schema

from .const import DOMAIN
from .mapping import EntityMapping, validate_mapping
from .metrics import ISSUE_CODES, list_metrics

_PROVENANCE_OPTIONS = [
    "observation",
    "derived.vendor",
    "derived.openhealthrouter",
    "derived.user",
]


def _metric_options() -> list[str]:
    return sorted(list_metrics())


def _mapping_schema(defaults: dict[str, Any] | None = None) -> Schema:
    current = defaults or {}

    def default(key: str, fallback: Any) -> Any:
        value = current.get(key)
        return fallback if value is None else value

    return Schema(
        {
            Required(
                "source_entity", default=default("source_entity", "")
            ): EntitySelector(EntitySelectorConfig(multiple=False)),
            Required("subject_ref", default=default("subject_ref", "")): str,
            Required("metric", default=default("metric", "")): SelectSelector(
                SelectSelectorConfig(
                    options=_metric_options(),
                    mode=SelectSelectorMode.DROPDOWN,
                    custom_value=True,
                )
            ),
            Required("unit", default=default("unit", "")): str,
            Optional("unit_attribute", default=default("unit_attribute", "")): str,
            Optional("value_attribute", default=default("value_attribute", "")): str,
            Optional("device_ref", default=default("device_ref", "")): str,
            Optional(
                "native_id_attribute", default=default("native_id_attribute", "")
            ): str,
            Optional(
                "observed_at_attribute", default=default("observed_at_attribute", "")
            ): str,
            Optional("fallback_time", default=bool(current.get("fallback_time"))): (
                BooleanSelector()
            ),
            Optional(
                "provenance_type", default=default("provenance_type", "observation")
            ): SelectSelector(SelectSelectorConfig(options=_PROVENANCE_OPTIONS)),
            Optional(
                "quality_issues", default=list(current.get("quality_issues") or [])
            ): SelectSelector(
                SelectSelectorConfig(
                    options=sorted(ISSUE_CODES),
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            Optional("enabled", default=bool(current.get("enabled", True))): (
                BooleanSelector()
            ),
        }
    )


def _blank_to_none(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _to_mapping(user_input: dict[str, Any]) -> EntityMapping:
    entity = str(user_input["source_entity"])
    metric = str(user_input["metric"])
    fallback = bool(user_input.get("fallback_time"))
    return EntityMapping(
        mapping_id=f"{entity}::{metric}",
        source_entity=entity,
        subject_ref=str(user_input["subject_ref"]).strip(),
        metric=metric,
        unit=str(user_input["unit"]).strip(),
        unit_attribute=_blank_to_none(user_input.get("unit_attribute")),
        value_attribute=_blank_to_none(user_input.get("value_attribute")),
        device_ref=_blank_to_none(user_input.get("device_ref")),
        native_id_attribute=_blank_to_none(user_input.get("native_id_attribute")),
        observed_at_attribute=_blank_to_none(user_input.get("observed_at_attribute")),
        fallback_time="state.last_changed" if fallback else None,
        provenance_type=str(user_input.get("provenance_type", "observation")),
        quality_issues=list(user_input.get("quality_issues") or []),
        enabled=bool(user_input.get("enabled", True)),
    )


def _mapping_defaults(mapping: EntityMapping) -> dict[str, Any]:
    data = mapping.model_dump()
    data["fallback_time"] = mapping.fallback_time is not None
    return data


class OhrConfigFlow(ConfigFlow, domain=DOMAIN):
    """只允许一条条目：unique_id + abort 组合阻止第二次配置。"""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="OpenHealthRouter", data={})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return OhrOptionsFlow()


class OhrOptionsFlow(OptionsFlow):
    """映射的增删改；保存后重载条目让订阅按新映射重建。"""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["add", "edit", "delete"]
        )

    def _mappings(self) -> list[dict[str, Any]]:
        return list(self.config_entry.options.get("mappings", []))

    async def _store_mappings(self, mappings: list[dict[str, Any]]) -> ConfigFlowResult:
        hass: HomeAssistant = self.hass
        new_options = dict(self.config_entry.options)
        new_options["mappings"] = mappings
        hass.config_entries.async_update_entry(self.config_entry, options=new_options)
        await hass.config_entries.async_reload(self.config_entry.entry_id)
        return self.async_create_entry(title="", data=new_options)

    def _validation_errors(self, mapping: EntityMapping) -> dict[str, str]:
        hass: HomeAssistant = self.hass

        def entity_check(entity_id: str) -> bool:
            return hass.states.get(entity_id) is not None

        return {code: code for code in validate_mapping(mapping, entity_check)}

    async def async_step_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                mapping = _to_mapping(user_input)
            except ValueError:
                errors["base"] = "invalid"
            else:
                errors = self._validation_errors(mapping)
                if not errors:
                    mappings = self._mappings()
                    mappings.append(mapping.model_dump())
                    return await self._store_mappings(mappings)
        return self.async_show_form(
            step_id="add", data_schema=_mapping_schema(user_input), errors=errors
        )

    def _select_mapping_schema(self, step_id: str) -> Schema:
        return Schema(
            {
                Required("mapping_id"): SelectSelector(
                    SelectSelectorConfig(
                        options=[m["mapping_id"] for m in self._mappings()]
                    )
                )
            }
        )

    async def async_step_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        mappings = self._mappings()
        if not mappings:
            return self.async_abort(reason="no_mappings")
        if user_input is not None and "mapping_id" in user_input:
            selected = next(
                (m for m in mappings if m["mapping_id"] == user_input["mapping_id"]),
                None,
            )
            if selected is not None:
                return self.async_show_form(
                    step_id="edit",
                    data_schema=_mapping_schema(
                        _mapping_defaults(EntityMapping(**selected))
                    ),
                    description_placeholders={"mapping_id": selected["mapping_id"]},
                )
        errors: dict[str, str] = {}
        if user_input is not None and "source_entity" in user_input:
            try:
                edited = _to_mapping(user_input)
            except ValueError:
                errors["base"] = "invalid"
            else:
                errors = self._validation_errors(edited)
                if not errors:
                    updated: list[dict[str, Any]] = [
                        edited.model_dump()
                        if item["mapping_id"] == edited.mapping_id
                        else item
                        for item in mappings
                    ]
                    if not any(
                        item["mapping_id"] == edited.mapping_id for item in mappings
                    ):
                        updated.append(edited.model_dump())
                    return await self._store_mappings(updated)
        return self.async_show_form(
            step_id="edit", data_schema=_mapping_schema(user_input), errors=errors
        )

    async def async_step_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        mappings = self._mappings()
        if not mappings:
            return self.async_abort(reason="no_mappings")
        if user_input is not None:
            kept = [m for m in mappings if m["mapping_id"] != user_input["mapping_id"]]
            return await self._store_mappings(kept)
        return self.async_show_form(
            step_id="delete", data_schema=self._select_mapping_schema("delete")
        )
