"""端到端：HTTP 接口上重演四类场景，并从完赛名单逐项追溯。"""

import json

from app.publications import canonical_fingerprint


# --------------------------------------------------------------------------
# 角色与写权限
# --------------------------------------------------------------------------

def test_anonymous_cannot_write_events(client):
    status, _ = client.call("POST", "/v1/guns", {"wave_id": "W1"})
    assert status == 403


def test_medical_role_cannot_publish_results(seeded_client):
    status, body = seeded_client.post(
        "/v1/publications",
        {"list_id": "L", "kind": "results", "scope": "provisional",
         "distance": "marathon"},
        role="medical_commander")
    assert status == 403


# --------------------------------------------------------------------------
# 完赛名单 -> 名次/违规
# --------------------------------------------------------------------------

def test_results_list_flags_imposter_and_dnf(seeded_client):
    status, body = seeded_client.get("/v1/results?distance=marathon&gender=M")
    assert status == 200
    by_bib = {r["bib"]: r for r in body["rows"]}
    # 冒名 A006/A008 与未完赛 A007 都不在有效完赛名单
    assert "A006" not in by_bib and "A008" not in by_bib and "A007" not in by_bib
    # A005 经冲销后恢复有效名次
    assert by_bib["A005"]["disqualified"] is False


def test_tied_places_in_results(seeded_client):
    _, body = seeded_client.get("/v1/results?distance=marathon&gender=M")
    rows = {r["bib"]: r for r in body["rows"]}
    assert rows["A002"]["overall_place"] == rows["A003"]["overall_place"] == 2


# --------------------------------------------------------------------------
# 公示与申诉（超期拒收、榜单不可覆盖）
# --------------------------------------------------------------------------

def test_publish_appeal_then_trace(seeded_client):
    # 1) 发布初榜
    status, pub = seeded_client.post("/v1/publications", {
        "list_id": "FINAL-1", "kind": "results", "scope": "provisional",
        "distance": "marathon", "published_at": "2026-09-20T12:00:00+08:00"})
    assert status == 201 and pub["entry_count"] >= 5

    # 2) 同编号再次发布被拒：不能静默覆盖
    status, _ = seeded_client.post("/v1/publications", {
        "list_id": "FINAL-1", "kind": "results", "scope": "final",
        "distance": "marathon"})
    assert status == 409

    # 3) 26 小时后申诉：超过 v1 公报 24 小时期限 -> 422
    status, _ = seeded_client.post("/v1/appeals", {
        "appeal_id": "AP-LATE", "bib": "A002", "list_id": "FINAL-1",
        "occurred_at": "2026-09-21T14:00:00+08:00"})
    assert status == 422

    # 4) 期限内申诉并裁决
    status, _ = seeded_client.post("/v1/appeals", {
        "appeal_id": "AP-2", "bib": "A002", "list_id": "FINAL-1",
        "reason": "分段计时存疑",
        "occurred_at": "2026-09-20T18:00:00+08:00"})
    assert status == 201
    status, decided = seeded_client.post(
        "/v1/appeals/AP-2/decision",
        {"outcome": "rejected", "note": "录像核实成绩无误",
         "occurred_at": "2026-09-20T19:00:00+08:00"})
    assert status in (200, 201)

    # 5) 旧榜指纹仍可校验
    status, snap = seeded_client.get("/v1/publications/FINAL-1")
    assert status == 200
    assert snap["fingerprint_verified"] is True
    assert canonical_fingerprint(snap) == snap["fingerprint"]


# --------------------------------------------------------------------------
# 逐项追溯：奖励依据 / 申诉历史 / 资源占用
# --------------------------------------------------------------------------

def test_trace_from_finisher_to_award_basis(seeded_client):
    seeded_client.post("/v1/publications", {
        "list_id": "TR-LIST", "kind": "results", "scope": "provisional",
        "distance": "marathon", "published_at": "2026-09-20T12:00:00+08:00"})
    status, trace = seeded_client.get("/v1/runners/A004/trace")
    assert status == 200
    # 奖励：总名次、中国籍、破纪录三项都在
    awards = {a["award"] for a in trace["awards_across_bulletins"]}
    assert {"overall_place", "domestic_place", "course_record"} <= awards
    # 破纪录依据钉在 v1 公报且引用发令/冲线事件
    record = next(a for a in trace["awards_across_bulletins"]
                  if a["award"] == "course_record")
    basis = record["basis"]
    assert basis["bulletin_version"] == 1
    assert basis["gun_event_id"] and basis["finish_event_id"]
    # 公示历史中能找到本人快照行
    published = [p for p in trace["publication_history"] if p["list_id"] == "TR-LIST"]
    assert published and published[0]["entry"]["bib"] == "A004"


def test_trace_includes_appeal_history(seeded_client):
    seeded_client.post("/v1/publications", {
        "list_id": "AP-LIST", "kind": "results", "scope": "provisional",
        "distance": "marathon", "published_at": "2026-09-20T12:00:00+08:00"})
    seeded_client.post("/v1/appeals", {
        "appeal_id": "AP-9", "bib": "A005", "list_id": "AP-LIST",
        "occurred_at": "2026-09-20T13:00:00+08:00"})
    _, trace = seeded_client.get("/v1/runners/A005/trace")
    history = [a for a in trace["appeal_history"] if a["appeal_id"] == "AP-9"]
    assert history and history[0]["within_window"] is True
    # 裁决链中旧 DQ 已被冲销，新裁决保留有效
    assert "RUL-A005-01" in trace["evaluation"]["voided_ruling_ids"]
    _, runner = seeded_client.get("/v1/runners/A005")
    timeline = {r["event_id"]: r["status"] for r in runner["ruling_timeline"]}
    assert timeline["RUL-A005-01"] == "voided"
    assert timeline["RUL-A005-03"] == "active"


def test_trace_medical_occupancy(seeded_client):
    _, trace = seeded_client.get("/v1/runners/A007/trace")
    case = trace["medical_cases"][0]
    # 赛务追溯能看到占用的资源与起止，转运细节被收敛
    locked = {iv["resource_id"] for iv in case["occupancy"]}
    assert {"STA-14", "AMB-03"} <= locked
    assert "transports" not in case and "bib_link" not in case


# --------------------------------------------------------------------------
# 医疗按岗接口
# --------------------------------------------------------------------------

def test_medical_case_role_visibility(seeded_client):
    # 无关站点 404
    status, _ = seeded_client.get("/v1/medical/cases/CASE-7001",
                                  role="station:STA-16")
    assert status == 404
    # 当事站点可见，且无号码布关联
    status, view = seeded_client.get("/v1/medical/cases/CASE-7001",
                                     role="station:STA-14")
    assert status == 200 and "bib_link" not in view
    # 指挥可见全部
    status, commander = seeded_client.get("/v1/medical/cases/CASE-7001",
                                          role="medical_commander")
    assert status == 200 and commander["bib_link"] == "A007"


def test_lock_nearest_via_http_and_conflict(seeded_client):
    # AMB-07 仍被 CASE-OTHER 占用：直接锁定应 409
    status, _ = seeded_client.post(
        "/v1/medical/cases/CASE-NEW/lock",
        {"resource_id": "AMB-07", "occurred_at": "2026-09-20T08:21:00+08:00"},
        role="marshal")
    # CASE-NEW 尚不存在 -> 先 404
    assert status == 404
    seeded_client.post("/v1/medical/sos", {
        "case_id": "CASE-NEW", "severity": "yellow",
        "symptoms": ["cramp"], "location": {"km": 15.4},
        "occurred_at": "2026-09-20T08:21:00+08:00"}, role="marshal")
    status, body = seeded_client.post(
        "/v1/medical/cases/CASE-NEW/lock",
        {"resource_id": "AMB-07", "occurred_at": "2026-09-20T08:21:00+08:00"},
        role="marshal")
    assert status == 409
    # lock-nearest 自动跳过仍被 CASE-OTHER 占用的 AMB-07；
    # 在 AMB-03 已释放（08:53）后调度，最近可用为 AMB-03
    status, picked = seeded_client.post(
        "/v1/medical/cases/CASE-NEW/lock-nearest",
        {"kind": "ambulance", "occurred_at": "2026-09-20T08:54:00+08:00"},
        role="marshal")
    assert status in (200, 201)
    assert picked["resource_id"] == "AMB-03"


# --------------------------------------------------------------------------
# 批量事件重放：乱序 + 重复，不改变最终名次
# --------------------------------------------------------------------------

def test_batch_replay_is_idempotent_and_order_independent(seeded_client, seeded):
    _, before = seeded_client.get("/v1/results?distance=marathon&gender=M")
    # 抽取全部规范事件，打乱顺序，通过通用事件接口重放一遍（带相同 event_id）
    import random
    events = seeded.store.canonical_events()
    replay = [{"event_id": e["event_id"], "event_type": e["event_type"],
               "occurred_at": e["occurred_at"], "payload": e["payload"],
               "device_id": e.get("device_id"),
               "device_seq": e.get("device_seq")} for e in events]
    random.Random(7).shuffle(replay)
    status, resp = seeded_client.post("/v1/events", {"events": replay})
    assert status == 200
    assert all(item["duplicated"] for item in resp["events"])
    _, after = seeded_client.get("/v1/results?distance=marathon&gender=M")
    assert [(r["bib"], r["overall_place"], r["official"]["gun_seconds"])
            for r in before["rows"]] == \
           [(r["bib"], r["overall_place"], r["official"]["gun_seconds"])
            for r in after["rows"]]


def test_health(seeded_client):
    status, body = seeded_client.call("GET", "/health", None)
    assert status == 200 and body["service"] == "marathon-adjudication"
