"""脱敏医疗调度。

设计要点：

- 求助事件只携带**假名**（``runner_pseudonym``）与症状代码，不携带姓名/国籍；
  号码布关联仅保存在 ``bib_link`` 字段，按角色决定是否可见。
- 资源（医疗站/救护车/定点医院）的锁定、释放、转运都是只追加事件，占用时间线
  通过重放得到，补传与乱序不改变“某时刻谁占用了什么”。
- 最近可用资源按实时位置直线距离选取，已被未释放锁定占用的资源跳过；
  距离相同时按 resource_id 确定性打破平局。
- 视图按岗位过滤：站点/救护车/医院只能看到与自己相关的案件，转运信息只对
  当事救护车、接收医院和总指挥开放。
"""

import hashlib
import math

from .timeutil import parse_iso

ROLE_COMMANDER = "medical_commander"


def pseudonymize(bib, salt="ty2026"):
    """由号码布派生稳定假名，医疗视图里用假名代替身份。"""
    digest = hashlib.sha256(f"{salt}:{bib}".encode("utf-8")).hexdigest()[:10]
    return f"R-{digest.upper()}"


# --------------------------------------------------------------------------
# 资源状态
# --------------------------------------------------------------------------

def resource_position(state, resource_id, at_ts=None):
    """资源在某时刻的位置。

    医疗站与医院位置固定；救护车读取 ``resource_availability`` 之外的位置上报，
    位置以事件形式记录在 ``ambulance_located`` 上（如存在），否则用注册位置。
    """
    res = state.resources.get(resource_id)
    if res is None:
        return None
    pos = dict(res.get("location") or {})
    moves = sorted(
        [e for e in state.event_index.values()
         if e["event_type"] == "ambulance.located"
         and e["payload"].get("resource_id") == resource_id
         and (at_ts is None or parse_iso(e["occurred_at"]).timestamp() <= at_ts)],
        key=lambda e: (e["occurred_at"], e["event_id"]))
    if moves:
        last = moves[-1]["payload"].get("location")
        if last:
            pos = dict(last)
    return pos or None


def active_lock(state, resource_id, at_ts=None):
    """资源在某时刻的未释放锁定（占用），没有则 None。"""
    active = None
    for case_id, locks in state.locks.items():
        for lock in locks:
            if lock["resource_id"] != resource_id:
                continue
            if at_ts is not None and lock["ts"] > at_ts:
                continue
            released = any(rel["resource_id"] == resource_id and rel["ts"] >= lock["ts"]
                           for rel in state.releases.get(case_id, []))
            if at_ts is not None:
                released = released and any(
                    rel["resource_id"] == resource_id
                    and lock["ts"] <= rel["ts"] <= at_ts
                    for rel in state.releases.get(case_id, []))
            if not released:
                if active is None or lock["ts"] > active["ts"]:
                    active = lock
    return active


def is_available(state, resource_id, at_ts=None):
    res = state.resources.get(resource_id)
    if res is None or not res.get("available", True):
        return False
    return active_lock(state, resource_id, at_ts) is None


def _distance_km(a, b):
    if a is None or b is None:
        return None
    if "km" in a and "km" in b:
        return abs(float(a["km"]) - float(b["km"]))
    if {"lat", "lng"} <= set(a) and {"lat", "lng"} <= set(b):
        # 等距圆柱投影，城市尺度下选最近资源足够精确
        lat1, lat2 = math.radians(float(a["lat"])), math.radians(float(b["lat"]))
        dx = (math.radians(float(b["lng"]) - float(a["lng"]))
              * math.cos((lat1 + lat2) / 2) * 6371.0)
        dy = (lat2 - lat1) * 6371.0
        return math.hypot(dx, dy)
    return None


def nearest_available(state, kind, location, at_ts, exclude_cases=None):
    """返回 (resource_id, distance_km)；已占用或停用的资源不参与。"""
    exclude_cases = exclude_cases or set()
    candidates = []
    for rid, res in state.resources.items():
        if res.get("kind") != kind:
            continue
        lock = active_lock(state, rid, at_ts)
        if lock is not None and lock["case_id"] not in exclude_cases:
            continue
        if not res.get("available", True):
            continue
        dist = _distance_km(location, resource_position(state, rid, at_ts))
        if dist is None:
            continue
        candidates.append((dist, rid))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (round(item[0], 6), item[1]))
    dist, rid = candidates[0]
    return rid, dist


# --------------------------------------------------------------------------
# 案件时间线与按岗视图
# --------------------------------------------------------------------------

def case_timeline(state, case_id):
    """一个求助案件的完整事件链：求助 -> 锁定 -> 转运 -> 结果 -> 释放。"""
    sos = state.cases.get(case_id)
    if sos is None:
        return None
    items = [{"kind": "sos", "event_id": sos["event_id"],
              "occurred_at": sos["occurred_at"]}]
    for lock in sorted(state.locks.get(case_id, []), key=lambda e: e["ts"]):
        items.append({"kind": "lock", "event_id": lock["event_id"],
                      "resource_id": lock["resource_id"],
                      "occurred_at": lock["occurred_at"]})
    for tr in sorted(state.transports.get(case_id, []), key=lambda e: e["ts"]):
        items.append({"kind": tr["kind"], "event_id": tr["event_id"],
                      "ambulance_id": tr.get("ambulance_id"),
                      "hospital_id": tr.get("hospital_id"),
                      "outcome": tr.get("outcome"),
                      "occurred_at": tr["occurred_at"]})
    for rel in sorted(state.releases.get(case_id, []), key=lambda e: e["ts"]):
        items.append({"kind": "release", "event_id": rel["event_id"],
                      "resource_id": rel["resource_id"],
                      "occurred_at": rel["occurred_at"]})
    items.sort(key=lambda x: (parse_iso(x["occurred_at"]).timestamp(),
                              x["event_id"]))
    return items


def involved_resources(state, case_id):
    ids = set()
    for lock in state.locks.get(case_id, []):
        ids.add(lock["resource_id"])
    for tr in state.transports.get(case_id, []):
        for key in ("ambulance_id", "hospital_id"):
            if tr.get(key):
                ids.add(tr[key])
    return ids


def role_can_see_case(role, state, case_id):
    if role == ROLE_COMMANDER:
        return True
    if not role:
        return False
    kind, _, rid = role.partition(":")
    if kind in ("station", "ambulance", "hospital") and rid:
        return rid in involved_resources(state, case_id)
    return False


def case_view(state, case_id, role):
    """按岗位脱敏后的案件视图；无权访问返回 None。"""
    sos = state.cases.get(case_id)
    if sos is None or not role_can_see_case(role, state, case_id):
        return None
    view = {
        "case_id": case_id,
        "runner_pseudonym": sos.get("runner_pseudonym"),
        "severity": sos.get("severity"),
        "symptoms": sos.get("symptoms", []),
        "location": sos.get("location"),
        "reported_at": sos["occurred_at"],
        "status": sos.get("status", "open"),
        "timeline": case_timeline(state, case_id),
        "locked_resources": sorted(
            {lk["resource_id"] for lk in state.locks.get(case_id, [])}),
    }
    # 号码布关联仅总指挥可见；站点/救护车/医院拿到的始终是脱敏视图。
    if role == ROLE_COMMANDER:
        view["bib_link"] = sos.get("bib_link")
    # 转运细节只对总指挥、当事救护车、接收医院开放
    transports = []
    for tr in state.transports.get(case_id, []):
        if role == ROLE_COMMANDER or role in (
                f"ambulance:{tr.get('ambulance_id')}",
                f"hospital:{tr.get('hospital_id')}"):
            transports.append({k: v for k, v in tr.items() if k != "case_id"})
    view["transports"] = transports
    return view


def cases_for_role(state, role):
    return [case_view(state, cid, role)
            for cid in sorted(state.cases)
            if role_can_see_case(role, state, cid)]


def resource_occupancy(state, resource_id=None):
    """资源占用时间线，用于从案件反查资源占用，也供指挥侧核对。"""
    out = []
    for rid, res in sorted(state.resources.items()):
        if resource_id and rid != resource_id:
            continue
        intervals = []
        for case_id, locks in state.locks.items():
            for lock in locks:
                if lock["resource_id"] != rid:
                    continue
                rel = next((r for r in sorted(state.releases.get(case_id, []),
                                              key=lambda e: e["ts"])
                            if r["ts"] >= lock["ts"]), None)
                intervals.append({
                    "case_id": case_id,
                    "resource_id": rid,
                    "locked_at": lock["occurred_at"],
                    "released_at": rel["occurred_at"] if rel else None,
                    "active": rel is None,
                    "lock_event_id": lock["event_id"],
                    "release_event_id": rel["event_id"] if rel else None,
                })
        out.append({
            "resource_id": rid,
            "kind": res.get("kind"),
            "name": res.get("name"),
            "available": res.get("available", True),
            "intervals": sorted(intervals, key=lambda i: i["locked_at"]),
        })
    return out
