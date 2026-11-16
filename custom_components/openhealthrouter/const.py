"""集成常量。"""

from __future__ import annotations

DOMAIN = "openhealthrouter"
DB_DIR = "openhealthrouter"
DB_FILENAME = "events.db"
PLATFORMS = ["sensor"]

# 接入状态投影的固定实体后缀（entity_id 与 unique_id 都不得含指标或测量者）
SENSOR_INGESTED_TOTAL = "ingested_total"
SENSOR_LAST_INGESTED = "last_ingested"
SENSOR_LAST_SKIP_REASON = "last_skip_reason"
