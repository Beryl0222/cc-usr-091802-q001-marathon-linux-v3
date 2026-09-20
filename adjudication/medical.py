"""现场救治资源：医疗站、救护车与定点医院的锁定、转运与释放。

求助单对外脱敏（不含号码布与身份），调度锁定最近可用资源，
转运目的地仅医疗、调度与管理角色可见。
"""

from __future__ import annotations

COURSE_KM = 42.195

# 可以看到身份与转运目的地的角色。
FULL_DETAIL_ROLES = {"medical", "dispatch", "admin"}


def seed_resources(cfg):
    """按赛事参数生成资源台账：医疗站/救护车沿赛道布点，定点医院带容量。"""
    resources = {}
    stations = cfg.get("stations", 0)
    for index in range(1, stations + 1):
        resource_id = f"ST-{index:02d}"
        resources[resource_id] = {
            "resource_id": resource_id, "kind": "station",
            "km": round(COURSE_KM * index / (stations + 1), 3),
            "status": "available", "case_id": None,
        }
    ambulances = cfg.get("ambulances", 0)
    for index in range(1, ambulances + 1):
        resource_id = f"AMB-{index:02d}"
        resources[resource_id] = {
            "resource_id": resource_id, "kind": "ambulance",
            "km": round(COURSE_KM * (index - 0.5) / ambulances, 3),
            "status": "available", "case_id": None,
        }
    hospitals = cfg.get("hospitals", 0)
    for index in range(1, hospitals + 1):
        resource_id = f"HOSP-{index:02d}"
        resources[resource_id] = {
            "resource_id": resource_id, "kind": "hospital",
            "km": round(COURSE_KM * (index - 0.5) / hospitals, 3),
            "capacity": 20, "occupied": 0,
        }
    return resources


def nearest_available(resources, km, kinds):
    """锁定最近可用资源；距离相同按资源编号保证确定性。"""
    best = None
    for resource in resources.values():
        if resource["kind"] not in kinds or resource["status"] != "available":
            continue
        key = (abs(resource["km"] - km), resource["resource_id"])
        if best is None or key < best[0]:
            best = (key, resource)
    return best[1] if best else None


def nearest_hospital(resources, km):
    """最近且仍有床位的定点医院。"""
    candidates = [r for r in resources.values()
                  if r["kind"] == "hospital" and r["occupied"] < r["capacity"]]
    if not candidates:
        return None
    return min(candidates, key=lambda r: (abs(r["km"] - km), r["resource_id"]))


def case_view(case, role):
    """按角色脱敏的病例视图：无关岗位看不到身份与转运目的地。"""
    view = {
        "case_id": case["case_id"],
        "location_km": case["location_km"],
        "category": case["category"],
        "status": case["status"],
        "opened_at": case["opened_at"],
    }
    if role in FULL_DETAIL_ROLES:
        view.update({
            "bib": case.get("bib"),
            "opened_by": case.get("opened_by"),
            "resource_id": case.get("resource_id"),
            "hospital_id": case.get("hospital_id"),
            "outcome": case.get("outcome"),
            "history": list(case["history"]),
        })
    return view
