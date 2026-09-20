"""公示榜单不可变、显式替代、申诉期限按榜单钉住公报版本。"""

from app.publications import (appeal_deadline, appeal_history,
                              build_publication, canonical_fingerprint)
from app.timeutil import parse_iso


PUBLISH_AT = "2026-09-20T12:00:00+08:00"


def _publish(state, list_id, *, version=1, supersedes=None, at=PUBLISH_AT):
    payload = build_publication(
        state, list_id=list_id, kind="results", scope="provisional",
        distance="marathon", bulletin_version=version,
        published_at=at, supersedes=supersedes)
    state.apply({
        "event_id": f"EVT-{list_id}", "event_type": "results.published",
        "occurred_at": at, "payload": payload})
    return payload


def test_publication_is_immutable_snapshot(state):
    payload = _publish(state, "LIST-V1")
    # 再给 A001 追加一条更晚的处罚裁决：已发布榜单内容指纹不变
    before = payload["fingerprint"]
    state.apply({"event_id": "LATE-PENALTY", "event_type": "ruling.issued",
                 "occurred_at": "2026-09-20T13:00:00+08:00",
                 "payload": {"bib": "A001", "action": "penalty", "seconds": 30}})
    snap = state.publications["LIST-V1"]
    assert canonical_fingerprint(snap) == before
    assert snap["status"] == "active"


def test_supersede_must_be_explicit_and_old_list_retained(state):
    _publish(state, "LIST-OLD")
    _publish(state, "LIST-NEW", supersedes=["LIST-OLD"],
             at="2026-09-20T13:00:00+08:00")
    old = state.publications["LIST-OLD"]
    new = state.publications["LIST-NEW"]
    assert old["status"] == "superseded"
    assert old["superseded_by"] == "LIST-NEW"
    assert new["status"] == "active"
    # 旧榜单仍可完整取回，没有被静默覆盖
    assert old["entries"] and old["fingerprint"]


def test_appeal_window_pinned_to_publication_bulletin(state):
    # LIST-V1 钉 v1（24 小时）；再发 v2（48 小时）并用 v2 发新榜
    _publish(state, "LIST-V1")
    state.apply({"event_id": "B2", "event_type": "bulletin.published",
                 "occurred_at": "2026-09-20T11:00:00+08:00",
                 "payload": _v2(state)})
    _publish(state, "LIST-V2", version=2,
             at="2026-09-20T12:00:00+08:00")
    d1 = appeal_deadline(state, "LIST-V1")
    d2 = appeal_deadline(state, "LIST-V2")
    assert d1 == parse_iso(PUBLISH_AT).timestamp() + 24 * 3600
    assert d2 == parse_iso(PUBLISH_AT).timestamp() + 48 * 3600


def _v2(state):
    v1 = state.bulletins[1]
    return {**v1, "version": 2,
            "rules": {**v1["rules"], "appeal_window_hours": 48}}


def test_appeal_history_links_filing_decision_and_ruling(state):
    _publish(state, "LIST-V1")
    state.apply({"event_id": "AP1", "event_type": "appeal.filed",
                 "occurred_at": "2026-09-20T18:00:00+08:00",
                 "payload": {"appeal_id": "AP-1", "bib": "A005",
                             "list_id": "LIST-V1",
                             "reason": "起点地垫漏扫"}})
    state.apply({"event_id": "AP1D", "event_type": "appeal.decided",
                 "occurred_at": "2026-09-20T20:00:00+08:00",
                 "payload": {"appeal_id": "AP-1", "outcome": "upheld",
                             "ruling_event_id": "RUL-A005-02"}})
    history = appeal_history(state, "A005")
    mine = [h for h in history if h["appeal_id"] == "AP-1"][0]
    assert mine["within_window"] is True
    assert mine["status"] == "upheld"
    assert mine["decision"]["ruling_event_id"] == "RUL-A005-02"
    assert mine["bulletin_version"] == 1
