"""HTTP 接口层。

只做协议、鉴权与参数校验；判定全部委托 :mod:`app.scoring`、
:mod:`app.publications`、:mod:`app.medical`。状态投影带脏标记：有追加才重放，
查询时拿到的永远是按规范顺序归约的同一份结论。

岗位通过 ``X-Staff-Role`` 头声明（生产环境前置鉴权网关注入）：

- ``official``：赛务岗位，可上报事件、出裁决、发榜单、查完整追溯
- ``medical_commander``：医疗总指挥，可见全部脱敏案件与号码布关联
- ``marshal``：现场志愿者，可发起求助与资源操作
- ``station:<id>`` / ``ambulance:<id>`` / ``hospital:<id>``：
  只能看到与本资源相关的案件，转运细节仅当事救护车与接收医院可见
"""

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from .events import EventStore, ValidationError
from .medical import (ROLE_COMMANDER, case_view, cases_for_role,
                      nearest_available, pseudonymize,
                      resource_occupancy, resource_position)
from .publications import (appeal_history, build_publication,
                           canonical_fingerprint)
from .scoring import (active_rulings, compute_awards, compute_rankings,
                      evaluate_bib)
from .state import State
from .timeutil import parse_iso

ROLE_OFFICIAL = "official"
ROLE_MARSHAL = "marshal"
MEDICAL_WRITERS = {ROLE_OFFICIAL, ROLE_COMMANDER, ROLE_MARSHAL}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _now():
    return datetime.now(timezone.utc).isoformat()


class ApiApp:
    def __init__(self, store=None):
        self.store = store or EventStore()
        self.state = State()
        self._dirty = True

    # -- 归约 --------------------------------------------------------------

    def _snapshot(self):
        """调用方需持 store 锁；按需确定性重放。"""
        if self._dirty:
            self.state.rebuild(self.store.canonical_events())
            self._dirty = False
        return self.state

    # -- 入口 --------------------------------------------------------------

    def handle(self, method, path, headers, body):
        try:
            target = urlsplit(path)
            route = target.path
            query = {k: v[0] for k, v in parse_qs(target.query).items()}
            role = headers.get("x-staff-role")
            data = {}
            if body:
                try:
                    data = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ApiError(400, f"请求体不是合法 JSON: {exc}") from exc
            with self.store.lock():
                return self._route(method, route, query, role, data)
        except ApiError as exc:
            return exc.status, {"error": exc.message}
        except ValidationError as exc:
            return 400, {"error": str(exc)}

    def _route(self, method, route, query, role, data):
        r = route.strip("/").split("/")
        # /v1/...
        if len(r) >= 1 and r[0] == "v1":
            return self._route_v1(method, r[1:], query, role, data)
        if route == "/health":
            from . import SERVICE_ID
            return 200, {"status": "ok", "service": SERVICE_ID,
                         "events": self.store.count()}
        raise ApiError(404, "未知接口")

    # -- 路由表 -------------------------------------------------------------

    def _route_v1(self, method, r, q, role, data):
        if r == ["events"] and method == "POST":
            self._require(role, {ROLE_OFFICIAL})
            return self._post_events(data)
        if r == ["events"] and method == "GET":
            self._require(role, {ROLE_OFFICIAL})
            return 200, {"events": self.store.canonical_events(),
                         "count": self.store.count()}
        if len(r) == 2 and r[0] == "events" and method == "GET":
            state = self._snapshot()
            event = state.event_index.get(r[1])
            if event is None:
                raise ApiError(404, "事件不存在")
            return 200, event

        if r == ["config"] and method == "POST":
            self._require(role, {ROLE_OFFICIAL})
            return self._append("event.configured",
                                self._require_fields(data, ["event_id", "race_date", "waves"]),
                                data)
        if r == ["bulletins"] and method == "POST":
            self._require(role, {ROLE_OFFICIAL})
            return self._post_bulletin(data)
        if r == ["bulletins"] and method == "GET":
            state = self._snapshot()
            return 200, {"bulletins": [state.bulletins[v] for v in state.bulletin_order]}
        if r == ["bulletins", "latest"] and method == "GET":
            state = self._snapshot()
            latest = state.latest_bulletin()
            if latest is None:
                raise ApiError(404, "尚未发布任何公报")
            return 200, latest
        if r == ["guns"] and method == "POST":
            self._require(role, {ROLE_OFFICIAL})
            return self._append("gun.fired",
                                self._require_fields(data, ["wave_id"]), data)

        if r == ["runners"] and method == "POST":
            self._require(role, {ROLE_OFFICIAL})
            payload = self._require_fields(
                data, ["bib", "name", "nationality", "gender", "distance",
                       "wave_id", "template_ref"])
            return self._append("runner.registered", payload, data)

        if r == ["face-verifications"] and method == "POST":
            self._require(role, {ROLE_OFFICIAL})
            payload = self._require_fields(data, ["bib", "context", "result"])
            if payload["context"] not in ("pickup", "checkin", "finish"):
                raise ApiError(400, "context 只能是 pickup/checkin/finish")
            if payload["result"] not in ("pass", "fail"):
                raise ApiError(400, "result 只能是 pass/fail")
            return self._append("face.verified", payload, data)

        if r == ["pickups"] and method == "POST":
            self._require(role, {ROLE_OFFICIAL})
            return self._append("bib.picked",
                                self._require_fields(data, ["bib"]), data)
        if r == ["checkins"] and method == "POST":
            self._require(role, {ROLE_OFFICIAL})
            payload = self._require_fields(data, ["bib", "zone_wave_id"])
            return self._append("checkin.recorded", payload, data)
        if r == ["reassignments"] and method == "POST":
            self._require(role, {ROLE_OFFICIAL})
            payload = self._require_fields(data, ["bib", "to_wave"])
            return self._append("wave.reassigned", payload, data)
        if r == ["timing"] and method == "POST":
            self._require(role, {ROLE_OFFICIAL})
            payload = self._require_fields(data, ["bib", "point_id", "observed_at"])
            # observed_at 提前校验，保证进入日志的时间都带时区
            parse_iso(payload["observed_at"])
            return self._append("timing.recorded", payload, data)

        if r == ["rulings"] and method == "POST":
            self._require(role, {ROLE_OFFICIAL})
            return self._post_ruling(data)

        if r == ["appeals"] and method == "POST":
            self._require(role, {ROLE_OFFICIAL})
            return self._post_appeal(data)
        if len(r) == 3 and r[0] == "appeals" and r[2] == "decision" and method == "POST":
            self._require(role, {ROLE_OFFICIAL})
            return self._post_appeal_decision(r[1], data)

        if r == ["publications"] and method == "POST":
            self._require(role, {ROLE_OFFICIAL})
            return self._post_publication(data)
        if r == ["publications"] and method == "GET":
            state = self._snapshot()
            snaps = [state.publications[i] for i in state.pub_order]
            return 200, {"publications": snaps}
        if len(r) == 2 and r[0] == "publications" and method == "GET":
            state = self._snapshot()
            snap = state.publications.get(r[1])
            if snap is None:
                raise ApiError(404, "榜单不存在")
            verified = canonical_fingerprint(snap) == snap.get("fingerprint")
            return 200, {**snap, "fingerprint_verified": verified}

        if r[:1] == ["rankings"] and method == "GET":
            return self._get_rankings(q)
        if r[:1] == ["results"] and method == "GET":
            return self._get_results(q)
        if r[:1] == ["awards"] and method == "GET":
            return self._get_awards(q)
        if len(r) == 2 and r[0] == "runners" and method == "GET":
            return self._get_runner(r[1], role)
        if len(r) == 3 and r[0] == "runners" and r[2] == "trace" and method == "GET":
            self._require(role, {ROLE_OFFICIAL, ROLE_COMMANDER})
            return self._get_trace(r[1], role)

        # 医疗
        if r == ["medical", "resources"] and method == "POST":
            self._require(role, MEDICAL_WRITERS)
            payload = self._require_fields(
                data, ["resource_id", "kind", "name", "location"])
            if payload["kind"] not in ("station", "ambulance", "hospital"):
                raise ApiError(400, "kind 只能是 station/ambulance/hospital")
            return self._append("resource.registered", payload, data)
        if len(r) == 4 and r[:2] == ["medical", "resources"] and r[3] == "availability" and method == "POST":
            self._require(role, MEDICAL_WRITERS)
            payload = self._require_fields(data, ["available"])
            payload["resource_id"] = r[2]
            return self._append("resource.availability", payload, data)
        if r == ["medical", "sos"] and method == "POST":
            self._require(role, MEDICAL_WRITERS)
            return self._post_sos(data)
        if r == ["medical", "cases"] and method == "GET":
            if not role:
                raise ApiError(403, "缺少岗位身份")
            state = self._snapshot()
            return 200, {"cases": [c for c in cases_for_role(state, role) if c]}
        if len(r) == 3 and r[:2] == ["medical", "cases"] and method == "GET":
            state = self._snapshot()
            view = case_view(state, r[2], role) if role else None
            if view is None:
                raise ApiError(404, "案件不存在或无权查看")
            return 200, view
        if len(r) == 4 and r[:2] == ["medical", "cases"] and r[3] == "lock" and method == "POST":
            self._require(role, MEDICAL_WRITERS)
            return self._post_lock(r[2], data)
        if len(r) == 4 and r[:2] == ["medical", "cases"] and r[3] == "lock-nearest" and method == "POST":
            self._require(role, MEDICAL_WRITERS)
            return self._post_lock_nearest(r[2], data)
        if len(r) == 4 and r[:2] == ["medical", "cases"] and r[3] == "release" and method == "POST":
            self._require(role, MEDICAL_WRITERS)
            return self._post_release(r[2], data)
        if len(r) == 4 and r[:2] == ["medical", "cases"] and r[3] == "transport" and method == "POST":
            self._require(role, {ROLE_OFFICIAL, ROLE_COMMANDER})
            return self._post_transport(r[2], data)
        if len(r) == 4 and r[:2] == ["medical", "cases"] and r[3] == "outcome" and method == "POST":
            self._require(role, MEDICAL_WRITERS)
            return self._post_outcome(r[2], data)
        if r == ["medical", "occupancy"] and method == "GET":
            self._require(role, {ROLE_OFFICIAL, ROLE_COMMANDER})
            return 200, {"resources": resource_occupancy(
                self._snapshot(), q.get("resource_id"))}
        if r == ["medical", "nearest"] and method == "GET":
            self._require(role, MEDICAL_WRITERS)
            return self._get_nearest(q)

        raise ApiError(404, "未知接口")

    # -- 通用写入 ------------------------------------------------------------

    def _append(self, event_type, payload, data, occurred_at=None):
        event, duplicated = self.store.append(
            event_type, payload,
            occurred_at or data.get("occurred_at") or _now(),
            event_id=data.get("event_id"),
            device_id=data.get("device_id"),
            device_seq=data.get("device_seq"))
        self._dirty = True
        return 200 if duplicated else 201, {
            "event_id": event["event_id"], "event_type": event_type,
            "duplicated": duplicated, "occurred_at": event["occurred_at"]}

    def _post_events(self, data):
        raw = data.get("events") if isinstance(data, dict) else None
        if raw is None and isinstance(data, dict) and data.get("event_type"):
            raw = [data]
        if not isinstance(raw, list) or not raw:
            raise ApiError(400, "需要 events 数组或单个事件对象")
        out = []
        for item in raw:
            if not isinstance(item, dict):
                raise ApiError(400, "事件必须是对象")
            payload = item.get("payload")
            if not isinstance(payload, dict):
                raise ApiError(400, f"{item.get('event_type')} 缺少 payload")
            event, duplicated = self.store.append(
                item["event_type"], payload,
                item.get("occurred_at") or _now(),
                event_id=item.get("event_id"),
                device_id=item.get("device_id"),
                device_seq=item.get("device_seq"))
            out.append({"event_id": event["event_id"],
                        "event_type": event["event_type"],
                        "duplicated": duplicated})
        self._dirty = True
        return 200, {"ingested": len(out), "events": out}

    # -- 赛务命令 ------------------------------------------------------------

    def _post_bulletin(self, data):
        payload = self._require_fields(
            data, ["version", "rules", "awards"])
        version = int(payload["version"])
        state = self._snapshot()
        if version in state.bulletins:
            raise ApiError(409, f"公报版本 {version} 已存在，不能覆盖")
        if state.bulletin_order and version <= state.bulletin_order[-1]:
            raise ApiError(409, "公报版本必须递增")
        return self._append("bulletin.published", payload, data)

    def _post_ruling(self, data):
        payload = self._require_fields(data, ["bib", "action"])
        action = payload["action"]
        if action not in ("dq", "penalty", "reinstate", "correction"):
            raise ApiError(400, "action 只能是 dq/penalty/reinstate/correction")
        state = self._snapshot()
        if payload["bib"] not in state.runners:
            raise ApiError(404, "号码布不存在")
        if action == "penalty":
            int(payload.get("seconds", 0))
        for old_id in payload.get("supersedes_ruling_ids", []) or []:
            if old_id not in state.event_index:
                raise ApiError(400, f"被冲销裁决 {old_id} 不存在")
        return self._append("ruling.issued", payload, data)

    def _post_appeal(self, data):
        payload = self._require_fields(data, ["appeal_id", "bib", "list_id"])
        state = self._snapshot()
        snap = state.publications.get(payload["list_id"])
        if snap is None:
            raise ApiError(404, "申诉针对的榜单不存在")
        if payload["appeal_id"] in state.appeal_by_id:
            raise ApiError(409, "申诉编号已存在")
        if payload["bib"] not in state.runners:
            raise ApiError(404, "号码布不存在")
        from .publications import appeal_deadline
        deadline = appeal_deadline(state, payload["list_id"])
        filed_ts = parse_iso(data.get("occurred_at") or _now()).timestamp()
        if filed_ts > deadline + 1e-6:
            raise ApiError(
                422, "申诉已超过榜单公示期限（按该榜单钉住的公报版本计算）")
        return self._append("appeal.filed", payload, data)

    def _post_appeal_decision(self, appeal_id, data):
        payload = self._require_fields(data, ["outcome"])
        if payload["outcome"] not in ("upheld", "rejected"):
            raise ApiError(400, "outcome 只能是 upheld/rejected")
        state = self._snapshot()
        rec = state.appeal_by_id.get(appeal_id)
        if rec is None:
            raise ApiError(404, "申诉不存在")
        if rec["decision"] is not None:
            raise ApiError(409, "申诉已作出决定")
        payload["appeal_id"] = appeal_id
        # 申诉成立时通常伴随新裁决（成绩更正/恢复名次），一并追加并互相引用
        ruling_body = data.get("ruling")
        if ruling_body:
            ruling_body = {**ruling_body, "bib": rec["filed"]["bib"]}
            event, _ = self.store.append(
                "ruling.issued", ruling_body,
                ruling_body.get("occurred_at") or data.get("occurred_at") or _now())
            payload["ruling_event_id"] = event["event_id"]
        status, resp = self._append("appeal.decided", payload, data)
        if ruling_body:
            resp["ruling_event_id"] = payload.get("ruling_event_id")
        return status, resp

    def _post_publication(self, data):
        fields = self._require_fields(
            data, ["list_id", "kind", "scope", "distance"])
        state = self._snapshot()
        if fields["list_id"] in state.publications:
            raise ApiError(409, "榜单编号已存在；公示榜单不可覆盖，请发新版")
        if fields["kind"] not in ("results", "awards"):
            raise ApiError(400, "kind 只能是 results/awards")
        if fields["scope"] not in ("provisional", "final"):
            raise ApiError(400, "scope 只能是 provisional/final")
        version = fields.get("bulletin_version")
        if version is None:
            latest = state.latest_bulletin()
            if latest is None:
                raise ApiError(400, "尚未发布公报，无法物化榜单")
            version = latest["version"]
        if int(version) not in state.bulletins:
            raise ApiError(404, f"公报版本 {version} 不存在")
        for old_id in data.get("supersedes", []) or []:
            if old_id not in state.publications:
                raise ApiError(404, f"被替代榜单 {old_id} 不存在")
        payload = build_publication(
            state,
            list_id=fields["list_id"], kind=fields["kind"],
            scope=fields["scope"], distance=fields["distance"],
            bulletin_version=int(version),
            published_at=data.get("published_at") or _now(),
            supersedes=data.get("supersedes"), note=data.get("note"))
        event, duplicated = self.store.append(
            "results.published", payload, payload.get("published_at"))
        self._dirty = True
        return 201, {"list_id": payload["list_id"],
                     "event_id": event["event_id"],
                     "fingerprint": payload["fingerprint"],
                     "entry_count": payload["entry_count"],
                     "supersedes": payload["supersedes"]}

    # -- 查询 ---------------------------------------------------------------

    def _resolve_bulletin(self, q):
        state = self._snapshot()
        if "bulletin_version" in q:
            bulletin = state.bulletins.get(int(q["bulletin_version"]))
            if bulletin is None:
                raise ApiError(404, "公报版本不存在")
            return bulletin
        latest = state.latest_bulletin()
        if latest is None:
            raise ApiError(404, "尚未发布公报")
        return latest

    def _distance(self, q):
        distance = q.get("distance", "marathon")
        state = self._snapshot()
        if state.config and distance not in state.config.get("distances", []):
            raise ApiError(400, "未知距离项目")
        return distance

    def _get_rankings(self, q):
        bulletin = self._resolve_bulletin(q)
        distance = self._distance(q)
        gender = q.get("gender")
        rows = compute_rankings(self._snapshot(), bulletin, distance,
                                gender=gender)
        return 200, {"distance": distance,
                     "gender": gender,
                     "bulletin_version": bulletin["version"],
                     "ranking_basis": bulletin.get("rules", {}).get("ranking_basis", "gun"),
                     "rows": [_row_summary(row) for row in rows]}

    def _get_results(self, q):
        bulletin = self._resolve_bulletin(q)
        distance = self._distance(q)
        gender = q.get("gender")
        state = self._snapshot()
        rows = compute_rankings(state, bulletin, distance, gender=gender)
        awards = {}
        for item in compute_awards(state, bulletin, distance):
            awards.setdefault(item["bib"], []).append(item)
        return 200, {"distance": distance,
                     "gender": gender,
                     "bulletin_version": bulletin["version"],
                     "rows": [{**_row_summary(row),
                              "trace": f"/v1/runners/{row['bib']}/trace",
                              "awards": [_award_summary(a)
                                         for a in awards.get(row["bib"], [])]}
                             for row in rows]}

    def _get_awards(self, q):
        bulletin = self._resolve_bulletin(q)
        distance = self._distance(q)
        items = compute_awards(self._snapshot(), bulletin, distance)
        return 200, {"distance": distance,
                     "bulletin_version": bulletin["version"],
                     "awards": items}

    def _get_runner(self, bib, role):
        self._require(role, {ROLE_OFFICIAL, ROLE_COMMANDER})
        state = self._snapshot()
        if bib not in state.runners:
            raise ApiError(404, "号码布不存在")
        bulletin = state.latest_bulletin()
        if bulletin is None:
            raise ApiError(404, "尚未发布公报")
        res = evaluate_bib(state, bib, bulletin)
        res["appeal_history"] = appeal_history(state, bib)
        alive, voided = active_rulings(state, bib)
        res["ruling_timeline"] = [{
            "event_id": r["event_id"], "action": r["action"],
            "reason": r.get("reason"), "occurred_at": r["occurred_at"],
            "status": "voided" if r["event_id"] in voided else "active"}
            for r in sorted(state.rulings.get(bib, []),
                            key=lambda x: (x["ts"], x["event_id"]))]
        return 200, res

    def _get_trace(self, bib, role):
        """完赛名单 -> 奖励依据 / 申诉历史 / 资源占用 的逐项追溯聚合。"""
        state = self._snapshot()
        runner = state.runners.get(bib)
        if runner is None:
            raise ApiError(404, "号码布不存在")
        bulletin = state.latest_bulletin()
        if bulletin is None:
            raise ApiError(404, "尚未发布公报")
        evaluation = evaluate_bib(state, bib, bulletin)
        awards = []
        for version in state.bulletin_order:
            b = state.bulletins[version]
            for item in compute_awards(state, b, runner["distance"]):
                if item["bib"] == bib:
                    awards.append(item)
        pub_entries = []
        for list_id in state.pub_order:
            snap = state.publications[list_id]
            entry = next((e for e in snap["entries"] if e["bib"] == bib), None)
            if entry:
                pub_entries.append({
                    "list_id": list_id, "scope": snap["scope"],
                    "status": snap["status"], "superseded_by": snap["superseded_by"],
                    "bulletin_version": snap["bulletin_version"],
                    "published_at": snap["published_at"],
                    "entry": entry})
        medical_cases = [cid for cid, sos in state.cases.items()
                         if sos.get("bib_link") == bib]
        medical = []
        for cid in medical_cases:
            view = case_view(state, cid, ROLE_COMMANDER)
            if role != ROLE_COMMANDER:
                # 赛务岗位可见案件与资源占用，但转运细节按医疗权限收敛
                view = {k: v for k, v in (view or {}).items()
                        if k not in ("transports", "bib_link")}
            occupancy = [i for res in resource_occupancy(state)
                         for i in res["intervals"] if i["case_id"] == cid]
            medical.append({"case_id": cid, **(view or {}),
                            "occupancy": occupancy})
        return 200, {
            "bib": bib,
            "runner": {k: v for k, v in runner.items() if k != "template_ref"},
            "evaluation": evaluation,
            "awards_across_bulletins": awards,
            "appeal_history": appeal_history(state, bib),
            "publication_history": pub_entries,
            "medical_cases": medical,
        }

    # -- 医疗命令 ------------------------------------------------------------

    def _post_sos(self, data):
        payload = self._require_fields(data, ["case_id", "severity", "location"])
        if payload["severity"] not in ("red", "yellow", "green"):
            raise ApiError(400, "severity 只能是 red/yellow/green")
        symptoms = data.get("symptoms", [])
        if not isinstance(symptoms, list):
            raise ApiError(400, "symptoms 必须是症状代码数组")
        state = self._snapshot()
        if payload["case_id"] in state.cases:
            raise ApiError(409, "案件编号已存在")
        # 服务端生成假名；接口不接受姓名/国籍等身份字段
        bib_link = data.get("bib_link")
        payload["bib_link"] = bib_link
        payload["runner_pseudonym"] = pseudonymize(bib_link or payload["case_id"])
        payload["symptoms"] = symptoms
        payload.setdefault("status", "open")
        return self._append("medical.sos", payload, data)

    def _lock_payload(self, case_id, data):
        state = self._snapshot()
        if case_id not in state.cases:
            raise ApiError(404, "案件不存在")
        at_ts = parse_iso(data.get("occurred_at") or _now()).timestamp()
        resource_id = self._require_fields(data, ["resource_id"])["resource_id"]
        res = state.resources.get(resource_id)
        if res is None:
            raise ApiError(404, "资源不存在")
        from .medical import active_lock
        lock = active_lock(state, resource_id, at_ts)
        if lock is not None:
            if lock["case_id"] == case_id:
                return None, {"resource_id": resource_id, "case_id": case_id,
                              "already_locked": True, "lock_event_id": lock["event_id"]}
            raise ApiError(409, f"资源 {resource_id} 已被案件 {lock['case_id']} 占用")
        if not res.get("available", True):
            raise ApiError(409, f"资源 {resource_id} 已停用")
        payload = {"case_id": case_id, "resource_id": resource_id}
        return payload, None

    def _post_lock(self, case_id, data):
        payload, early = self._lock_payload(case_id, data)
        if early is not None:
            return 200, early
        return self._append("resource.locked", payload, data)

    def _post_lock_nearest(self, case_id, data):
        state = self._snapshot()
        sos = state.cases.get(case_id)
        if sos is None:
            raise ApiError(404, "案件不存在")
        kind = self._require_fields(data, ["kind"])["kind"]
        if kind not in ("station", "ambulance", "hospital"):
            raise ApiError(400, "kind 只能是 station/ambulance/hospital")
        at_ts = parse_iso(data.get("occurred_at") or _now()).timestamp()
        picked = nearest_available(state, kind, sos["location"], at_ts,
                                   exclude_cases={case_id})
        if picked is None:
            raise ApiError(409, f"没有可用的 {kind}")
        resource_id, dist = picked
        payload = {"case_id": case_id, "resource_id": resource_id}
        status, resp = self._append("resource.locked", payload, data)
        resp["resource_id"] = resource_id
        resp["distance_km"] = round(dist, 3)
        return status, resp

    def _post_release(self, case_id, data):
        state = self._snapshot()
        if case_id not in state.cases:
            raise ApiError(404, "案件不存在")
        resource_id = self._require_fields(data, ["resource_id"])["resource_id"]
        from .medical import active_lock
        lock = active_lock(state, resource_id,
                           parse_iso(data.get("occurred_at") or _now()).timestamp())
        if lock is None or lock["case_id"] != case_id:
            raise ApiError(409, "该资源未被本案件占用，无需释放")
        payload = {"case_id": case_id, "resource_id": resource_id}
        return self._append("resource.released", payload, data)

    def _post_transport(self, case_id, data):
        payload = self._require_fields(data, ["ambulance_id", "hospital_id"])
        state = self._snapshot()
        if case_id not in state.cases:
            raise ApiError(404, "案件不存在")
        for rid in (payload["ambulance_id"], payload["hospital_id"]):
            if rid not in state.resources:
                raise ApiError(404, f"资源 {rid} 不存在")
        payload["case_id"] = case_id
        return self._append("transport.assigned", payload, data)

    def _post_outcome(self, case_id, data):
        payload = self._require_fields(data, ["outcome"])
        if payload["outcome"] not in ("admitted", "treated_released",
                                      "transferred", "deceased"):
            raise ApiError(400, "outcome 取值非法")
        state = self._snapshot()
        if case_id not in state.cases:
            raise ApiError(404, "案件不存在")
        payload["case_id"] = case_id
        return self._append("transport.outcome", payload, data)

    def _get_nearest(self, q):
        kind = q.get("kind")
        if kind not in ("station", "ambulance", "hospital"):
            raise ApiError(400, "需要 kind=station/ambulance/hospital")
        location = {}
        if "lat" in q and "lng" in q:
            location = {"lat": float(q["lat"]), "lng": float(q["lng"])}
        elif "km" in q:
            location = {"km": float(q["km"])}
        else:
            raise ApiError(400, "需要 lat/lng 或 km 定位")
        at_ts = parse_iso(q.get("at") or _now()).timestamp()
        picked = nearest_available(self._snapshot(), kind, location, at_ts)
        if picked is None:
            return 200, {"available": False}
        rid, dist = picked
        return 200, {"available": True, "resource_id": rid,
                     "distance_km": round(dist, 3),
                     "location": resource_position(self._snapshot(), rid, at_ts)}

    # -- 工具 ---------------------------------------------------------------

    @staticmethod
    def _require(role, allowed):
        if role not in allowed:
            raise ApiError(403, f"当前岗位无权操作，允许: {sorted(allowed)}")

    @staticmethod
    def _require_fields(data, names):
        if not isinstance(data, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        missing = [n for n in names if n not in data or data[n] in (None, "")]
        if missing:
            raise ApiError(400, f"缺少必填字段: {', '.join(missing)}")
        return {n: data[n] for n in names if n in data}


def _row_summary(row):
    return {
        "bib": row["bib"], "name": row["name"],
        "nationality": row["nationality"], "gender": row["gender"],
        "wave_id": row.get("wave_id_effective"),
        "overall_place": row.get("overall_place"),
        "domestic_place": row.get("domestic_place"),
        "official": row["official"],
        "disqualified": row["disqualified"],
        "dq_reasons": row["dq_reasons"],
        "irregularities": row["irregularities"],
        "status": row["status"],
    }


def _award_summary(item):
    return {k: v for k, v in item.items() if k not in ("bib", "distance")}
