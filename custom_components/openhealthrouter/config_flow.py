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

    _editing_id: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # 回到菜单意味着上一次编辑被放弃：复位阶段标记，避免下次直接跳过选择
        self._editing_id = None
        return self.async_show_menu(
            step_id="init", menu_options=["add", "edit", "delete", "webhook"]
        )

    def _mappings(self) -> list[dict[str, Any]]:
        return list(self.config_entry.options.get("mappings", []))

    async def _store_mappings(self, mappings: list[dict[str, Any]]) -> ConfigFlowResult:
        hass: HomeAssistant = self.hass
        new_options = dict(self.config_entry.options)
        new_options["mappings"] = mappings
        # 先写入再重载，让订阅按新映射重建；随后以同一份 options 结束流程
        # （OptionsFlow 的 create_entry data 会覆盖 options，必须与之一致）
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
                    duplicate = any(
                        item["mapping_id"] == mapping.mapping_id for item in mappings
                    )
                    if duplicate:
                        errors["base"] = "mapping_exists"
                    else:
                        mappings.append(mapping.model_dump())
                        return await self._store_mappings(mappings)
        return self.async_show_form(
            step_id="add", data_schema=_mapping_schema(user_input), errors=errors
        )

    def _target_schema(self) -> Schema:
        current = dict(self.config_entry.options.get("webhook_target") or {})
        return Schema(
            {
                Required("name", default=str(current.get("name", ""))): str,
                Required("url", default=str(current.get("url", ""))): str,
                # 密钥不回填：每次都要重新输入
                Required("secret", default=""): str,
            }
        )

    def _validate_target(self, user_input: dict[str, Any]) -> dict[str, str]:
        from urllib.parse import urlsplit

        errors: dict[str, str] = {}
        name = str(user_input.get("name", "")).strip()
        url = str(user_input.get("url", "")).strip()
        secret = str(user_input.get("secret", ""))
        if not name or len(name) > 64 or "://" in name:
            errors["target_name_invalid"] = "target_name_invalid"
        if not url.lower().startswith("https://"):
            errors["target_url_not_https"] = "target_url_not_https"
        else:
            parts = urlsplit(url)
            if parts.username or parts.password:
                errors["target_url_has_credentials"] = "target_url_has_credentials"
            if parts.query:
                errors["target_url_has_query"] = "target_url_has_query"
        if not secret:
            errors["target_secret_required"] = "target_secret_required"
        return errors

    async def async_step_webhook(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._validate_target(user_input)
            if not errors:
                new_options = dict(self.config_entry.options)
                new_options["webhook_target"] = {
                    "name": str(user_input["name"]).strip(),
                    "url": str(user_input["url"]).strip(),
                    "secret": str(user_input["secret"]),
                }
                self.hass.config_entries.async_update_entry(
                    self.config_entry, options=new_options
                )
                return self.async_create_entry(title="", data=new_options)
        return self.async_show_form(
            step_id="webhook", data_schema=self._target_schema(), errors=errors
        )

    def _select_mapping_schema(self) -> Schema:
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
        """两个阶段：先选映射，再填表单。用 `_editing_id` 区分，不靠字段嗅探。"""
        mappings = self._mappings()
        if not mappings:
            return self.async_abort(reason="no_mappings")

        if self._editing_id is None:
            if user_input is None:
                return self.async_show_form(
                    step_id="edit", data_schema=self._select_mapping_schema()
                )
            selected_id = str(user_input.get("mapping_id", ""))
            selected = next(
                (item for item in mappings if item["mapping_id"] == selected_id), None
            )
            if selected is None:
                return self.async_abort(reason="mapping_missing")
            try:
                defaults = _mapping_defaults(EntityMapping(**selected))
            except ValueError, KeyError:
                return self.async_abort(reason="invalid_mapping")
            self._editing_id = selected_id
            return self.async_show_form(
                step_id="edit",
                data_schema=_mapping_schema(defaults),
                description_placeholders={"mapping_id": selected_id},
            )

        errors: dict[str, str] = {}
        original_id = self._editing_id
        try:
            edited = _to_mapping(user_input or {})
        except ValueError, KeyError:
            errors["base"] = "invalid"
        else:
            errors = self._validation_errors(edited)
            if not errors:
                if not any(item["mapping_id"] == original_id for item in mappings):
                    return self.async_abort(reason="mapping_missing")
                clash = any(
                    item["mapping_id"] == edited.mapping_id
                    and item["mapping_id"] != original_id
                    for item in mappings
                )
                if clash:
                    errors["base"] = "mapping_exists"
                else:
                    updated = [
                        edited.model_dump()
                        if item["mapping_id"] == original_id
                        else item
                        for item in mappings
                    ]
                    self._editing_id = None
                    return await self._store_mappings(updated)
        return self.async_show_form(
            step_id="edit",
            data_schema=_mapping_schema(user_input),
            errors=errors,
            description_placeholders={"mapping_id": original_id},
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
            step_id="delete", data_schema=self._select_mapping_schema()
        )
