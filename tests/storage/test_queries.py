"""任务 3.1–3.4、4.1–4.2：查询过滤、时间边界矩阵、分页、修正链可见性。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.openhealthrouter.models import HealthEvent
from custom_components.openhealthrouter.store import (
    EventStore,
    QueryFilters,
    observation_keys,
    time_key,
)

SH = ZoneInfo("Asia/Shanghai")
NY = ZoneInfo("America/New_York")


@pytest.fixture
def store(tmp_path: Path):
    instance = EventStore.open(tmp_path / "events.db")
    try:
        yield instance
    finally:
        instance.close()


def _event(
    make_event,
    *,
    start: datetime,
    end: datetime | None = None,
    value: float = 72.0,
) -> HealthEvent:
    observed: dict = {
        "start": start.isoformat(),
        "precision": "second",
        "timezone": str(start.tzinfo),
    }
    if end is not None:
        observed["end"] = end.isoformat()
    return make_event(observedAt=observed, value=value)


def _derived(make_event, **overrides) -> HealthEvent:
    """合成派生事件（厂商推算需 estimated；本项目计算不需要）。"""
    payload = {
        "metric": "body.fat_percentage",
        "unit": "%",
        "value": 18.5,
        "provenance": {"type": "derived.vendor"},
        "quality": {"issues": ["estimated"]},
        "observedAt": {
            "start": "2026-11-12T09:00:00+08:00",
            "precision": "second",
            "timezone": "Asia/Shanghai",
        },
    }
    payload.update(overrides)
    return make_event(**payload)


def test_filter_by_metric_and_range(store: EventStore, make_event) -> None:
    in_range = _event(make_event, start=datetime(2026, 11, 12, 8, 0, tzinfo=SH))
    out_of_range = _event(make_event, start=datetime(2026, 11, 20, 8, 0, tzinfo=SH))
    other_metric = _derived(make_event)
    store.insert_events([in_range, out_of_range, other_metric])

    events, total = store.query(
        QueryFilters(
            metric="body.weight",
            since=datetime(2026, 11, 12, 0, 0, tzinfo=SH),
            until=datetime(2026, 11, 13, 0, 0, tzinfo=SH),
        )
    )
    assert total == 1
    assert [event.event_id for event in events] == [in_range.event_id]


def test_empty_result_is_not_an_error(store: EventStore, make_event) -> None:
    store.insert_events([make_event()])
    events, total = store.query(
        QueryFilters(
            metric="body.bmi",
            since=datetime(2030, 1, 1, tzinfo=SH),
            until=datetime(2030, 1, 2, tzinfo=SH),
        )
    )
    assert events == []
    assert total == 0


def test_day_precision_event_matches_its_own_day(store: EventStore, make_event) -> None:
    day_event = _derived(
        make_event,
        observedAt={
            "start": "2026-11-12",
            "precision": "day",
            "timezone": "Asia/Shanghai",
        },
    )
    store.insert_events([day_event])

    def hit(day: int) -> bool:
        events, _ = store.query(
            QueryFilters(
                since=datetime(2026, 11, day, 0, 0, tzinfo=SH),
                until=datetime(2026, 11, day + 1, 0, 0, tzinfo=SH),
            )
        )
        return bool(events)

    assert hit(11) is False
    assert hit(12) is True
    assert hit(13) is False

    events, _ = store.query(
        QueryFilters(
            since=datetime(2026, 11, 12, 0, 0, tzinfo=SH),
            until=datetime(2026, 11, 13, 0, 0, tzinfo=SH),
        )
    )
    assert events[0].observed_at.start.isoformat() == "2026-11-12"
    assert events[0].observed_at.precision == "day"


def test_day_interval_event_covers_all_declared_days(
    store: EventStore, make_event
) -> None:
    multi_day = _derived(
        make_event,
        metric="body.bmi",
        unit="kg/m²",
        value=23.1,
        provenance={"type": "derived.openhealthrouter"},
        quality={"issues": []},
        observedAt={
            "start": "2026-11-12",
            "end": "2026-11-15",
            "precision": "day",
            "timezone": "Asia/Shanghai",
        },
    )
    store.insert_events([multi_day])

    start_key, end_key = observation_keys(multi_day)
    assert end_key is not None
    assert end_key == time_key(datetime(2026, 11, 16, 0, 0, tzinfo=SH))
    assert start_key < end_key

    def hit(day: int) -> bool:
        events, _ = store.query(
            QueryFilters(
                since=datetime(2026, 11, day, 0, 0, tzinfo=SH),
                until=datetime(2026, 11, day + 1, 0, 0, tzinfo=SH),
            )
        )
        return bool(events)

    assert hit(11) is False
    assert hit(12) is True
    assert hit(14) is True
    assert hit(15) is True
    assert hit(16) is False


def test_two_day_interval_hits_both_days(store: EventStore, make_event) -> None:
    two_day = _derived(
        make_event,
        metric="body.bmi",
        unit="kg/m²",
        value=23.1,
        provenance={"type": "derived.openhealthrouter"},
        quality={"issues": []},
        observedAt={
            "start": "2026-11-12",
            "end": "2026-11-13",
            "precision": "day",
            "timezone": "Asia/Shanghai",
        },
    )
    store.insert_events([two_day])
    _, end_key = observation_keys(two_day)
    assert end_key == time_key(datetime(2026, 11, 14, 0, 0, tzinfo=SH))

    def hit(day: int) -> bool:
        events, _ = store.query(
            QueryFilters(
                since=datetime(2026, 11, day, 0, 0, tzinfo=SH),
                until=datetime(2026, 11, day + 1, 0, 0, tzinfo=SH),
            )
        )
        return bool(events)

    assert hit(12) is True
    assert hit(13) is True
    assert hit(14) is False


def test_instant_and_interval_boundaries(store: EventStore, make_event) -> None:
    day = datetime(2026, 11, 12, 0, 0, tzinfo=SH)
    q_lo = day
    q_hi = day + timedelta(days=1)

    cases = [
        ("instant_at_lo", q_lo, None, 70.1, True),
        ("instant_at_hi", q_hi, None, 70.2, False),
        ("instant_inside", day + timedelta(hours=12), None, 70.3, True),
        ("interval_end_at_lo", day - timedelta(days=2), q_lo, 70.4, False),
        ("cross_in", day - timedelta(days=1), day + timedelta(hours=6), 70.5, True),
        ("cross_out", day + timedelta(hours=6), day + timedelta(days=2), 70.6, True),
        ("covering", day - timedelta(days=3), day + timedelta(days=3), 70.7, True),
        ("out_before", day - timedelta(days=5), day - timedelta(days=4), 70.8, False),
    ]
    events = [
        _event(make_event, start=start, end=end, value=value)
        for _, start, end, value, _ in cases
    ]
    store.insert_events(events)

    found, _ = store.query(QueryFilters(since=q_lo, until=q_hi))
    found_values = sorted(
        float(event.value) for event in found if event.value is not None
    )
    expected_values = sorted(float(value) for *_, value, hit in cases if hit)
    assert found_values == expected_values


def test_dst_day_is_23_hours(store: EventStore, make_event) -> None:
    dst_day = _derived(
        make_event,
        observedAt={
            "start": "2026-03-08",
            "precision": "day",
            "timezone": "America/New_York",
        },
    )
    start_key, end_key = observation_keys(dst_day)
    assert end_key is not None
    start_utc = datetime.fromisoformat(start_key.replace("Z", "+00:00"))
    end_utc = datetime.fromisoformat(end_key.replace("Z", "+00:00"))
    assert end_utc - start_utc == timedelta(hours=23)
    assert start_key == "2026-03-08T05:00:00.000000Z"
    assert end_key == "2026-03-09T04:00:00.000000Z"

    store.insert_events([dst_day])

    def hit(day: datetime) -> bool:
        events, _ = store.query(QueryFilters(since=day, until=day + timedelta(days=1)))
        return bool(events)

    assert hit(datetime(2026, 3, 8, tzinfo=NY)) is True
    assert hit(datetime(2026, 3, 7, tzinfo=NY)) is False
    assert hit(datetime(2026, 3, 9, tzinfo=NY)) is False


def test_utc_aligned_range_intersects_day_event(store: EventStore, make_event) -> None:
    day_event = _derived(
        make_event,
        observedAt={
            "start": "2026-11-12",
            "precision": "day",
            "timezone": "Asia/Shanghai",
        },
    )
    store.insert_events([day_event])
    since = datetime(2026, 11, 12, 0, 0, tzinfo=UTC)
    until = since + timedelta(days=1)
    events, _ = store.query(QueryFilters(since=since, until=until))
    assert [event.event_id for event in events] == [day_event.event_id]


def test_time_key_is_offset_independent() -> None:
    same_instant_sh = datetime(2026, 11, 12, 0, 0, tzinfo=SH)
    same_instant_ny = datetime(2026, 11, 11, 11, 0, tzinfo=NY)
    assert time_key(same_instant_sh) == time_key(same_instant_ny)


def test_pagination_is_stable_and_disjoint(store: EventStore, make_event) -> None:
    events = [
        _event(
            make_event,
            start=datetime(2026, 11, 12, 8, index, tzinfo=SH),
            value=70.0 + index,
        )
        for index in range(5)
    ]
    store.insert_events(events)
    full, total = store.query(QueryFilters(limit=100))
    assert total == 5

    paged: list[HealthEvent] = []
    offset = 0
    while offset < 5:
        page, page_total = store.query(QueryFilters(limit=2, offset=offset))
        assert page_total == total
        assert len(page) <= 2
        paged.extend(page)
        offset += 2
    assert [event.event_id for event in paged] == [event.event_id for event in full]


def test_total_matches_limit(store: EventStore, make_event) -> None:
    events = [
        _event(
            make_event,
            start=datetime(2026, 11, 12, 8, index, tzinfo=SH),
            value=70.0 + index,
        )
        for index in range(3)
    ]
    store.insert_events(events)
    page, total = store.query(QueryFilters(limit=1))
    assert total == 3
    assert len(page) == 1


def test_get_event_returns_superseded_flag(store: EventStore, make_event) -> None:
    original = make_event()
    correction = make_event(
        value=73.1,
        provenance={"type": "derived.user"},
        correctionOf=original.event_id,
    )
    store.insert_events([original, correction])

    stored_original = store.get_event(original.event_id)
    assert stored_original is not None
    assert stored_original.superseded is True
    assert stored_original.event.value == pytest.approx(72.4)

    stored_correction = store.get_event(correction.event_id)
    assert stored_correction is not None
    assert stored_correction.superseded is False

    assert store.get_event("evt_missing") is None


def test_default_query_hides_superseded(store: EventStore, make_event) -> None:
    a = make_event()
    b = make_event(
        value=73.1,
        provenance={"type": "derived.user"},
        correctionOf=a.event_id,
    )
    c = make_event(
        value=73.9,
        provenance={"type": "derived.user"},
        correctionOf=b.event_id,
    )
    store.insert_events([a, b, c])

    default_events, _ = store.query()
    assert [event.event_id for event in default_events] == [c.event_id]

    all_events, total = store.query(QueryFilters(include_superseded=True))
    assert total == 3
    assert {event.event_id for event in all_events} == {
        a.event_id,
        b.event_id,
        c.event_id,
    }


def test_pagination_tie_break_with_identical_start_keys(
    store: EventStore, make_event
) -> None:
    """同一观察时刻的事件必须靠 inserted_at_utc/event_id 稳定分页。"""
    same_start = datetime(2026, 11, 12, 8, 0, tzinfo=SH)
    events = [
        _event(make_event, start=same_start, value=70.0 + index) for index in range(3)
    ]
    store.insert_events(events)
    full, total = store.query(QueryFilters(limit=100))
    assert total == 3
    assert len({event.observed_at.start for event in full}) == 1

    paged: list[HealthEvent] = []
    for offset in range(3):
        page, page_total = store.query(QueryFilters(limit=1, offset=offset))
        assert page_total == 3
        assert len(page) == 1
        paged.extend(page)
    assert [event.event_id for event in paged] == [event.event_id for event in full]
    assert len({event.event_id for event in paged}) == 3


def test_multi_metric_filter(tmp_path: Path, make_event) -> None:
    store = EventStore.open(tmp_path / "events.db")
    try:
        weight = make_event()
        fat = make_event(
            metric="body.fat_percentage",
            unit="%",
            value=18.5,
            provenance={"type": "derived.vendor"},
            quality={"issues": ["estimated"]},
        )
        other = make_event(metric="body.bmi", unit="kg/m²", value=23.1)
        store.insert_events([weight, fat, other])

        events, total = store.query(
            QueryFilters(metrics=frozenset({"body.weight", "body.fat_percentage"}))
        )
        assert total == 2
        assert {event.metric for event in events} == {
            "body.weight",
            "body.fat_percentage",
        }
    finally:
        store.close()


def test_fetch_page_matches_query_and_has_no_duplicates(
    tmp_path: Path, make_event
) -> None:
    store = EventStore.open(tmp_path / "events.db")
    try:
        events = [
            make_event(
                value=70.0 + index,
                observedAt={
                    "start": f"2026-11-12T08:0{index}:00+08:00",
                    "precision": "second",
                    "timezone": "Asia/Shanghai",
                },
            )
            for index in range(5)
        ]
        store.insert_events(events)
        filters = QueryFilters(limit=2)

        full, total = store.query(QueryFilters(limit=100))
        assert total == 5

        paged: list[HealthEvent] = []
        after = None
        while True:
            page = store.fetch_page(filters, after=after, limit=2)
            paged.extend(page.events)
            if not page.has_more:
                break
            after = page.cursor
        assert [event.event_id for event in paged] == [event.event_id for event in full]
        assert len({event.event_id for event in paged}) == 5
    finally:
        store.close()


def test_fetch_page_survives_identical_sort_keys(tmp_path: Path, make_event) -> None:
    """同一 (start_key, inserted_at_utc) 下多个 event_id 必须跨页且不重复不丢。"""
    import sqlite3

    from custom_components.openhealthrouter.store import dump_payload

    db = tmp_path / "events.db"
    store = EventStore.open(db)
    try:
        # 内容不同 → event_id 不同；但排序前两列完全相同，专打混合游标的边界
        events = [make_event(value=70.0 + index) for index in range(5)]
        conn = sqlite3.connect(db)
        try:
            for event in events:
                conn.execute(
                    "INSERT INTO events (event_id, subject_ref, metric, "
                    "observed_start_key, observed_end_key, precision, superseded, "
                    "payload_json, inserted_at_utc) VALUES (?,?,?,?,?,?,0,?,?)",
                    (
                        event.event_id,
                        event.subject_ref,
                        event.metric,
                        "2026-11-12T00:00:00.000000Z",
                        None,
                        "second",
                        dump_payload(event),
                        "2026-11-12T00:00:01.000000Z",
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        ids: list[str] = []
        after = None
        while True:
            page = store.fetch_page(QueryFilters(limit=2), after=after, limit=2)
            ids.extend(event.event_id for event in page.events if event.event_id)
            if not page.has_more:
                break
            after = page.cursor

        assert len(ids) == 5, ids
        assert len(set(ids)) == 5, ids
        assert ids == sorted(ids), "同组内 event_id 升序"
    finally:
        store.close()
