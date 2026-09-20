"""事件流 -> 赛务状态投影。

投影只建索引，所有“判定”（名次、违规、奖励、医疗可见性）都在查询时按
业务时间排序后计算，因此迟到的补传事件进入索引后不会留下乱序痕迹；
公示榜单是发布当时物化的不可变快照，写在事件载荷里，后续任何变化都
只能通过新榜单显式替代。
"""

from dataclasses import dataclass, field
from datetime import timezone

from .timeutil import parse_iso

# 人脸核验的三个业务环节，构成报名身份 -> 领物 -> 检录 -> 完赛的证据链
FACE_CONTEXTS = ("pickup", "checkin", "finish")


def _ts(value):
    return parse_iso(value).timestamp()


class State:
    def __init__(self):
        self.reset()

    def reset(self):
        self.config = None
        self.guns = {}                 # wave_id -> [{fired_at, ts, event_id}]
        self.bulletins = {}            # version -> bulletin 载荷
        self.bulletin_order = []
        self.runners = {}              # bib -> 注册信息
        self.faces = {}                # bib -> {template_ref, by_context: {ctx: [核验]}}
        self.pickups = {}              # bib -> 领物事件
        self.checkins = {}             # bib -> [检录事件]
        self.reassignments = {}        # bib -> [改枪事件]
        self.timing = {}               # bib -> [计时读数]
        self.rulings = {}              # bib -> [裁决事件]
        self.appeals = {}              # bib -> [申诉事件]
        self.appeal_by_id = {}         # appeal_id -> 事件对
        self.publications = {}         # list_id -> 快照事件
        self.pub_order = []
        # 医疗
        self.resources = {}            # resource_id -> 资源档案
        self.cases = {}                # case_id -> 求助事件
        self.locks = {}                # case_id -> [锁定事件]
        self.releases = {}             # case_id -> [释放事件]
        self.transports = {}           # case_id -> [转运事件]
        self.event_index = {}          # event_id -> event（裁决证据引用用）

    # -- 重放 -------------------------------------------------------------

    def rebuild(self, canonical_events):
        self.reset()
        for event in canonical_events:
            self.apply(event)
        return self

    def apply(self, event):
        etype = event["event_type"]
        payload = event["payload"]
        ts = _ts(event["occurred_at"])
        method = getattr(self, f"_on_{etype.replace('.', '_')}", None)
        if method is not None:
            method(payload, ts, event)
        self.event_index[event["event_id"]] = event

    # -- 赛事配置 ----------------------------------------------------------

    def _on_event_configured(self, p, ts, event):
        self.config = {
            "event_id": p["event_id"],
            "event_name": p.get("event_name", p["event_id"]),
            "race_date": p["race_date"],
            "waves": {w["wave_id"]: w for w in p["waves"]},
            "distances": p.get("distances", ["marathon", "half_marathon"]),
            "points": {pt["point_id"]: pt for pt in p.get("points", [])},
            "event_id_meta": event["event_id"],
        }

    def _on_gun_fired(self, p, ts, event):
        self.guns.setdefault(p["wave_id"], []).append({
            "fired_at": event["occurred_at"], "ts": ts,
            "event_id": event["event_id"],
        })

    # -- 公报（规则发布版本）-------------------------------------------------

    def _on_bulletin_published(self, p, ts, event):
        version = int(p["version"])
        self.bulletins[version] = {**p, "event_id": event["event_id"],
                                   "published_at": event["occurred_at"]}
        if version not in self.bulletin_order:
            self.bulletin_order.append(version)
            self.bulletin_order.sort()

    def latest_bulletin(self):
        if not self.bulletin_order:
            return None
        return self.bulletins[self.bulletin_order[-1]]

    # -- 报名 / 人脸 --------------------------------------------------------

    def _on_runner_registered(self, p, ts, event):
        bib = p["bib"]
        self.runners[bib] = {
            **p,
            "registered_at": event["occurred_at"],
            "registration_event_id": event["event_id"],
        }
        self.faces[bib] = {"template_ref": p.get("template_ref"),
                           "by_context": {c: [] for c in FACE_CONTEXTS}}

    def _on_face_verified(self, p, ts, event):
        bib = p["bib"]
        bucket = self.faces.setdefault(
            bib, {"template_ref": p.get("template_ref"),
                  "by_context": {c: [] for c in FACE_CONTEXTS}})
        ctx = p["context"]
        bucket["by_context"].setdefault(ctx, []).append({
            "result": p["result"],           # pass / fail —— 只存结论
            "score": p.get("score"),
            "template_ref": p.get("template_ref"),       # 报名底库模板引用
            "probe_ref": p.get("probe_ref"),             # 现场抓拍模板引用
            "device_id": event.get("device_id"),
            "occurred_at": event["occurred_at"], "ts": ts,
            "event_id": event["event_id"],
        })

    def _on_bib_picked(self, p, ts, event):
        self.pickups[p["bib"]] = {
            **p, "occurred_at": event["occurred_at"], "ts": ts,
            "event_id": event["event_id"],
        }

    # -- 检录 / 改枪 --------------------------------------------------------

    def _on_checkin_recorded(self, p, ts, event):
        self.checkins.setdefault(p["bib"], []).append({
            **p, "occurred_at": event["occurred_at"], "ts": ts,
            "event_id": event["event_id"],
        })

    def _on_wave_reassigned(self, p, ts, event):
        self.reassignments.setdefault(p["bib"], []).append({
            **p, "occurred_at": event["occurred_at"], "ts": ts,
            "event_id": event["event_id"],
        })

    # -- 计时 --------------------------------------------------------------

    def _on_timing_recorded(self, p, ts, event):
        read = {
            "bib": p["bib"], "point_id": p["point_id"],
            "km": p.get("km", self._km_of(p["point_id"])),
            "observed_at": p["observed_at"], "ts": _ts(p["observed_at"]),
            "event_id": event["event_id"],
            "device_id": event.get("device_id"),
            "device_seq": event.get("device_seq"),
        }
        self.timing.setdefault(p["bib"], []).append(read)

    def _km_of(self, point_id):
        if self.config and point_id in self.config["points"]:
            return self.config["points"][point_id].get("km")
        return None

    # -- 裁决 / 申诉 --------------------------------------------------------

    def _on_ruling_issued(self, p, ts, event):
        self.rulings.setdefault(p["bib"], []).append({
            **p, "occurred_at": event["occurred_at"], "ts": ts,
            "event_id": event["event_id"], "voided_by": None,
        })

    def _on_appeal_filed(self, p, ts, event):
        rec = {"filed": {**p, "occurred_at": event["occurred_at"], "ts": ts,
                         "event_id": event["event_id"]},
               "decision": None}
        self.appeals.setdefault(p["bib"], []).append(rec)
        self.appeal_by_id[p["appeal_id"]] = rec

    def _on_appeal_decided(self, p, ts, event):
        rec = self.appeal_by_id.get(p["appeal_id"])
        if rec is not None:
            rec["decision"] = {**p, "occurred_at": event["occurred_at"],
                               "ts": ts, "event_id": event["event_id"]}

    # -- 公示榜单 -----------------------------------------------------------

    def _on_results_published(self, p, ts, event):
        list_id = p["list_id"]
        snapshot = {
            **p,
            "event_id": event["event_id"],
            "published_at": event["occurred_at"], "ts": ts,
            "status": "active",
            "superseded_by": None,
        }
        self.publications[list_id] = snapshot
        self.pub_order.append(list_id)
        # 显式冲销链：新榜单点名替代旧榜单，顺序本身可重放
        for old_id in p.get("supersedes", []):
            old = self.publications.get(old_id)
            if old is not None:
                old["status"] = "superseded"
                old["superseded_by"] = list_id

    # -- 医疗 --------------------------------------------------------------

    def _on_resource_registered(self, p, ts, event):
        self.resources[p["resource_id"]] = {
            **p,
            "registered_at": event["occurred_at"],
            "available": p.get("available", True),
            "status_events": [],
        }

    def _on_resource_availability(self, p, ts, event):
        res = self.resources.get(p["resource_id"])
        if res is not None:
            res["available"] = bool(p["available"])
            res["status_events"].append({
                "available": bool(p["available"]),
                "reason": p.get("reason"),
                "occurred_at": event["occurred_at"], "ts": ts,
            })

    def _on_medical_sos(self, p, ts, event):
        from .medical import pseudonymize
        self.cases[p["case_id"]] = {
            **p,
            "runner_pseudonym": p.get("runner_pseudonym")
            or pseudonymize(p.get("bib_link") or p["case_id"]),
            "occurred_at": event["occurred_at"], "ts": ts,
            "event_id": event["event_id"],
        }

    def _on_resource_locked(self, p, ts, event):
        self.locks.setdefault(p["case_id"], []).append({
            **p, "occurred_at": event["occurred_at"], "ts": ts,
            "event_id": event["event_id"],
        })

    def _on_resource_released(self, p, ts, event):
        self.releases.setdefault(p["case_id"], []).append({
            **p, "occurred_at": event["occurred_at"], "ts": ts,
            "event_id": event["event_id"],
        })

    def _on_transport_assigned(self, p, ts, event):
        self.transports.setdefault(p["case_id"], []).append({
            "kind": "assigned", **p,
            "occurred_at": event["occurred_at"], "ts": ts,
            "event_id": event["event_id"],
        })

    def _on_transport_outcome(self, p, ts, event):
        self.transports.setdefault(p["case_id"], []).append({
            "kind": "outcome", **p,
            "occurred_at": event["occurred_at"], "ts": ts,
            "event_id": event["event_id"],
        })

    # -- 查询辅助 -----------------------------------------------------------

    def timing_sorted(self, bib):
        return sorted(self.timing.get(bib, []), key=lambda r: (r["ts"], r["event_id"]))

    def start_point_wave(self, point_id):
        pt = (self.config or {}).get("points", {}).get(point_id)
        return pt.get("wave_id") if pt and pt.get("kind") == "start" else None

    def is_finish(self, point_id):
        pt = (self.config or {}).get("points", {}).get(point_id)
        return bool(pt and pt.get("kind") == "finish")

    def active_publications(self, kind=None, scope=None):
        out = []
        for list_id in self.pub_order:
            snap = self.publications[list_id]
            if snap["status"] != "active":
                continue
            if kind and snap.get("kind") != kind:
                continue
            if scope and snap.get("scope") != scope:
                continue
            out.append(snap)
        return out
