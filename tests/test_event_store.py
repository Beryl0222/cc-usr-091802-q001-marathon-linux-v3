"""事件日志：重放幂等、乱序确定性、时区保留。"""

from app.events import EventStore, ValidationError


def test_event_id_replay_is_idempotent():
    store = EventStore()
    e1, d1 = store.append("gun.fired", {"wave_id": "W1"},
                          "2026-09-20T07:30:00+08:00", event_id="E1")
    e2, d2 = store.append("gun.fired", {"wave_id": "W1"},
                          "2026-09-20T07:30:00+08:00", event_id="E1")
    assert d1 is False and d2 is True
    assert e1["event_id"] == e2["event_id"]
    assert store.count() == 1


def test_device_seq_dedup_for_offline_reupload():
    store = EventStore()
    store.append("timing.recorded", {"bib": "X"}, "2026-09-20T08:00:00+08:00",
                 device_id="MAT-09", device_seq=9007)
    # 断网恢复后补传，同一 device/seq 重复到达
    _, dup = store.append("timing.recorded", {"bib": "X"},
                          "2026-09-20T08:00:00+08:00",
                          device_id="MAT-09", device_seq=9007)
    assert dup is True
    assert store.count() == 1


def test_out_of_order_arrival_sorts_by_business_time():
    store = EventStore()
    store.append("a.late", {}, "2026-09-20T09:00:00+08:00", event_id="LATE")
    store.append("a.early", {}, "2026-09-20T07:00:00+08:00", event_id="EARLY")
    canonical = [e["event_id"] for e in store.canonical_events()]
    assert canonical == ["EARLY", "LATE"]


def test_naive_time_rejected():
    store = EventStore()
    try:
        store.append("x", {}, "2026-09-20T07:30:00")
    except ValidationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("朴素时间必须被拒绝")


def test_original_offset_preserved_and_utc_normalised():
    store = EventStore()
    event, _ = store.append("x", {}, "2026-09-20T07:30:00+08:00")
    assert event["occurred_at"].endswith("+08:00")          # 原始时区保留
    assert event["occurred_at_utc"].endswith("+00:00")      # UTC 并存
    assert event["occurred_at_utc"].startswith("2026-09-19T23:30:00")


def test_rebuild_from_jsonl_is_identical(tmp_path):
    path = tmp_path / "events.jsonl"
    store = EventStore(str(path))
    for i in range(3):
        store.append("x", {"i": i}, f"2026-09-20T07:0{i}:00+08:00")
    reloaded = EventStore(str(path))
    assert ([e["event_id"] for e in reloaded.canonical_events()]
            == [e["event_id"] for e in store.canonical_events()])
