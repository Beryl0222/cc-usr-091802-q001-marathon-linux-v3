"""HTTP 接口层：角色门禁、事件接入、判定查询、公示申诉与救治调度。"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit, parse_qs

from . import core, medical
from .journal import Journal
from .rules import Rules
from .timeutil import parse_instant, to_utc, format_utc

SERVICE_ID = "marathon-adjudication"
SERVICE_NAME = "马拉松赛务判定中枢"

ROLES = {"admin", "checkin", "timing", "awards", "medical",
         "dispatch", "marshal", "athlete", "public"}

RULING_ACTIONS = {"disqualify", "reinstate", "adjust_time",
                  "wave_change", "clear_flag", "tie_break"}


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class App:
    """赛务判定应用：持有规则注册表、事件日志与投影缓存。"""

    def __init__(self, rules, journal=None, medical_cfg=None,
                 role_tokens=None, clock=None):
        self.rules_registry = {rules.version: rules}
        self.rules = rules
        self.journal = journal if journal is not None else Journal()
        self.medical_cfg = medical_cfg or {}
        # role_tokens 为空时为开发模式：角色取自 X-Role 头。
        self.role_tokens = role_tokens or {}
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._state = None
        self._state_len = -1

    # ---- 基础设施 ----
    def state(self):
        with self._lock:
            length = len(self.journal)
            if self._state is None or self._state_len != length:
                self._state = core.RaceState.build(
                    self.journal,
                    resources=medical.seed_resources(self.medical_cfg))
                self._state_len = length
            return self._state

    def resolve_role(self, headers):
        auth = headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth[len("Bearer "):].strip()
            role = self.role_tokens.get(token)
            if role is None:
                raise ApiError(401, "invalid_token", "无法识别的访问令牌")
            return role
        if self.role_tokens:
            raise ApiError(401, "missing_token", "缺少访问令牌")
        role = headers.get("x-role", "public")
        if role not in ROLES:
            raise ApiError(401, "unknown_role", f"未知角色: {role}")
        return role

    def _routes(self):
        return [
            ("GET", r"^/health$", "get_health", None),
            ("POST", r"^/v1/events$", "post_events",
             {"checkin", "timing", "admin"}),
            ("GET", r"^/v1/events/(?P<event_id>[^/]+)$", "get_event",
             {"checkin", "timing", "awards", "admin"}),
            ("GET", r"^/v1/athletes/(?P<athlete_id>[^/]+)$", "get_athlete",
             {"checkin", "awards", "admin"}),
            ("GET", r"^/v1/results$", "get_results",
             {"timing", "awards", "admin"}),
            ("POST", r"^/v1/rulings$", "post_ruling", {"awards", "admin"}),
            ("GET", r"^/v1/rulings$", "get_rulings", {"awards", "admin"}),
            ("POST", r"^/v1/leaderboards/publish$", "post_publish",
             {"awards", "admin"}),
            ("GET", r"^/v1/leaderboards/current$", "get_current_board", None),
            ("GET", r"^/v1/leaderboards/(?P<pub_id>[^/]+)$", "get_board", None),
            ("POST", r"^/v1/appeals$", "post_appeal",
             {"athlete", "awards", "admin"}),
            ("GET", r"^/v1/appeals$", "get_appeals", {"awards", "admin"}),
            ("POST", r"^/v1/appeals/(?P<appeal_id>[^/]+)/decide$",
             "post_appeal_decision", {"awards", "admin"}),
            ("POST", r"^/v1/medical/requests$", "post_medical_request",
             {"medical", "dispatch", "admin", "marshal"}),
            ("GET", r"^/v1/medical/cases/(?P<case_id>[^/]+)$",
             "get_medical_case", None),
            ("POST", r"^/v1/medical/cases/(?P<case_id>[^/]+)/transport$",
             "post_transport", {"medical", "dispatch", "admin"}),
            ("POST", r"^/v1/medical/cases/(?P<case_id>[^/]+)/close$",
             "post_close_case", {"medical", "dispatch", "admin"}),
            ("GET", r"^/v1/medical/resources$", "get_resources",
             {"medical", "dispatch", "admin"}),
            ("GET", r"^/v1/finishers/(?P<bib>[^/]+)/trace$", "get_trace",
             {"timing", "awards", "admin"}),
            ("GET", r"^/v1/rules/current$", "get_rules", None),
            ("POST", r"^/v1/rules$", "post_rules", {"admin"}),
        ]

    def handle(self, method, path, query, headers, body):
        """同步处理一次请求，返回 (status, payload)。"""
        headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        try:
            role = self.resolve_role(headers)
        except ApiError as exc:
            return exc.status, _error(exc)
        for route_method, pattern, attr, roles in self._routes():
            if route_method != method:
                continue
            match = re.match(pattern, path)
            if not match:
                continue
            if roles is not None and role not in roles:
                return 403, _error(ApiError(
                    403, "forbidden", "当前角色无权访问该接口"))
            request = _Request(role=role, query=query or {}, body=body,
                               **match.groupdict())
            try:
                with self._lock:
                    return getattr(self, attr)(request)
            except ApiError as exc:
                return exc.status, _error(exc)
            except ValueError as exc:
                return 400, _error(ApiError(400, "bad_request", str(exc)))
        return 404, _error(ApiError(404, "not_found", "接口不存在"))

    def handle_http(self, method, raw_path, headers, raw_body):
        """HTTP 适配层入口：解析路径、查询串与 JSON 请求体。"""
        parts = urlsplit(raw_path)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        body = None
        if raw_body:
            try:
                body = json.loads(raw_body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return 400, {"error": {"code": "bad_json",
                                       "message": "请求体不是合法 JSON"}}
        return self.handle(method, parts.path, query, headers, body)

    # ---- 通用辅助 ----
    @staticmethod
    def _body(request):
        if not isinstance(request.body, dict):
            raise ApiError(400, "bad_request", "请求体必须是 JSON 对象")
        return request.body

    def _now(self, request):
        occurred = self._body(request).get("occurred_at")
        if occurred:
            return format_utc(parse_instant(occurred))
        return format_utc(self.clock())

    def _append(self, event):
        receipts = self.journal.append([event])
        receipt = receipts[0]
        if receipt["status"] != "accepted":
            raise ApiError(409, "event_conflict",
                           receipt.get("error", "事件未被接受"))
        return receipt

    # ---- 基础 ----
    def get_health(self, _request):
        return 200, health_payload()

    def get_rules(self, _request):
        return 200, {"rules": self.rules.doc, "active_version": self.rules.version,
                     "registered_versions": sorted(self.rules_registry)}

    def post_rules(self, request):
        body = self._body(request)
        doc = body.get("rules")
        rules = Rules(doc)  # 不合法会抛 ValueError -> 400
        if rules.version in self.rules_registry:
            raise ApiError(409, "rules_exists", f"规则版本已存在: {rules.version}")
        if rules.event_id != self.rules.event_id:
            raise ApiError(409, "event_mismatch", "规则 event_id 与赛事不一致")
        self.rules_registry[rules.version] = rules
        if body.get("activate"):
            self.rules = rules
        return 201, {"registered": rules.version, "active": self.rules.version}

    # ---- 事件接入 ----
    def post_events(self, request):
        body = self._body(request)
        events = body.get("events")
        if not isinstance(events, list) or not events:
            raise ApiError(400, "bad_request", "events 必须是非空列表")
        receipts = self.journal.append(events)
        summary = {"accepted": 0, "duplicate": 0, "rejected": 0}
        for receipt in receipts:
            summary[receipt["status"]] += 1
        return 200, {"receipts": receipts, "summary": summary}

    def get_event(self, request):
        event = self.journal.get(request.event_id)
        if event is None:
            raise ApiError(404, "not_found", "事件不存在")
        return 200, {"event": event}

    # ---- 运动员与成绩 ----
    def get_athlete(self, request):
        state = self.state()
        athlete_id = request.athlete_id
        registration = state.athletes.get(athlete_id)
        if registration is None:
            raise ApiError(404, "not_found", "运动员不存在")
        verifications = [{
            "phase": v.get("phase"), "decision": v.get("decision"),
            "template_ref": v.get("template_ref"), "device_id": v.get("device_id"),
            "occurred_at": v.get("occurred_at"), "event_id": v["event_id"],
        } for v in state.verifications_for(athlete_id)]
        return 200, {
            "athlete": registration,
            "wave": state.assigned_wave(athlete_id),
            "verifications": verifications,
            "wave_change_requests": state.wave_requests.get(athlete_id, []),
            "eligibility": core.eligibility_detail(state, self.rules, athlete_id),
        }

    def _standings(self, distance, gender):
        state = self.state()
        rows = core.compute_standings(state, self.rules, distance, gender)
        core.compute_awards(rows, self.rules, distance, gender)
        return rows

    def get_results(self, request):
        distance = request.query.get("distance")
        gender = request.query.get("gender")
        if not distance or not gender:
            raise ApiError(400, "missing_param", "需要 distance 与 gender 参数")
        rows = self._standings(distance, gender)
        return 200, {
            "distance": distance, "gender": gender,
            "rules_version": self.rules.version,
            "standings": [core.public_row(row) for row in rows],
        }

    # ---- 裁决 ----
    def post_ruling(self, request):
        body = self._body(request)
        action = body.get("action")
        if action not in RULING_ACTIONS:
            raise ApiError(400, "bad_request", f"未知裁决类型: {action!r}")
        athlete_id = body.get("athlete_id")
        params = body.get("params") or {}
        if action != "tie_break" and not athlete_id:
            raise ApiError(400, "bad_request", "该裁决需要 athlete_id")
        if action == "adjust_time":
            if "point" not in params or "corrected_occurred_at" not in params:
                raise ApiError(400, "bad_request",
                               "adjust_time 需要 point 与 corrected_occurred_at")
            parse_instant(params["corrected_occurred_at"])
        if action == "wave_change":
            if params.get("to_wave") not in self.rules.wave_numbers():
                raise ApiError(400, "bad_request", "to_wave 不在规则分枪列表中")
        if action == "clear_flag" and "flag" not in params:
            raise ApiError(400, "bad_request", "clear_flag 需要 params.flag")
        if action == "tie_break" and not params.get("order"):
            raise ApiError(400, "bad_request", "tie_break 需要 params.order")
        state = self.state()
        ruling_id = f"RUL-{len(state.rulings) + 1:04d}"
        occurred_at = self._now(request)
        event = {
            "event_id": f"evt-{ruling_id}", "type": "ruling",
            "occurred_at": occurred_at, "ruling_id": ruling_id,
            "action": action, "params": params,
            "reason": body.get("reason"), "issued_by": request.role,
        }
        if athlete_id:
            event["athlete_id"] = athlete_id
        if action == "wave_change":
            within = to_utc(parse_instant(occurred_at)) <= \
                self.rules.wave_change_cutoff()
            event["within_cutoff"] = within
        self._append(event)
        return 201, {"ruling": event}

    def get_rulings(self, request):
        state = self.state()
        athlete_id = request.query.get("athlete_id")
        rulings = state.rulings
        if athlete_id:
            rulings = [r for r in rulings if r.get("athlete_id") == athlete_id]
        return 200, {"rulings": rulings}

    # ---- 公示 ----
    def post_publish(self, request):
        body = self._body(request)
        distance = body.get("distance")
        gender = body.get("gender")
        if not distance or not gender:
            raise ApiError(400, "missing_param", "需要 distance 与 gender")
        state = self.state()
        rows = self._standings(distance, gender)
        number = len(state.publications) + 1
        publication_id = f"PUB-{number:04d}"
        version = sum(1 for snap in state.publications.values()
                      if (snap["distance"], snap["gender"]) == (distance, gender)) + 1
        published_at = self._now(request)
        snapshot = {
            "publication_id": publication_id,
            "version": version,
            "distance": distance,
            "gender": gender,
            "published_at": published_at,
            "published_by": request.role,
            "rules_version": self.rules.version,
            "journal_digest": self.journal.digest(),
            "standings": [core.public_row(row) for row in rows],
        }
        self._append({
            "event_id": f"evt-{publication_id}", "type": "publication",
            "occurred_at": published_at, "publication_id": publication_id,
            "distance": distance, "gender": gender, "snapshot": snapshot,
        })
        return 201, {"publication": snapshot}

    def get_current_board(self, request):
        distance = request.query.get("distance")
        gender = request.query.get("gender")
        state = self.state()
        publication_id = state.current_pub.get((distance, gender))
        if publication_id is None:
            raise ApiError(404, "not_published", "该组别尚未公示")
        return 200, {"publication": state.publications[publication_id]}

    def get_board(self, request):
        snapshot = self.state().publications.get(request.pub_id)
        if snapshot is None:
            raise ApiError(404, "not_found", "公示版本不存在")
        return 200, {"publication": snapshot}

    # ---- 申诉 ----
    def post_appeal(self, request):
        body = self._body(request)
        athlete_id = body.get("athlete_id")
        state = self.state()
        registration = state.athletes.get(athlete_id)
        if registration is None:
            raise ApiError(404, "not_found", "运动员不存在")
        publication_id = body.get("publication_id")
        if publication_id is None:
            key = (registration["distance"], registration["gender"])
            publication_id = state.current_pub.get(key)
        snapshot = state.publications.get(publication_id or "")
        if snapshot is None:
            raise ApiError(409, "no_published_result", "没有可申诉的公示成绩")
        rules = self.rules_registry.get(snapshot["rules_version"], self.rules)
        filed_at = to_utc(parse_instant(
            body["occurred_at"])) if body.get("occurred_at") else self.clock()
        deadline = to_utc(parse_instant(snapshot["published_at"])) \
            + rules.appeal_window()
        if filed_at > deadline:
            raise ApiError(409, "appeal_window_closed",
                           f"申诉期限已于 {format_utc(deadline)} 截止"
                           f"（规则版本 {rules.version}）")
        appeal_id = f"AP-{len(state.appeals) + 1:04d}"
        event = {
            "event_id": f"evt-{appeal_id}", "type": "appeal",
            "occurred_at": format_utc(filed_at), "appeal_id": appeal_id,
            "athlete_id": athlete_id, "publication_id": publication_id,
            "target_kind": body.get("target_kind", "result"),
            "grounds": body.get("grounds"),
        }
        self._append(event)
        appeal = self.state().appeals[appeal_id]
        return 201, {"appeal": core.appeal_view(appeal),
                     "deadline": format_utc(deadline)}

    def get_appeals(self, request):
        state = self.state()
        athlete_id = request.query.get("athlete_id")
        status = request.query.get("status")
        views = []
        for appeal in state.appeals.values():
            view = core.appeal_view(appeal)
            if athlete_id and view["athlete_id"] != athlete_id:
                continue
            if status and view["status"] != status:
                continue
            views.append(view)
        return 200, {"appeals": views}

    def post_appeal_decision(self, request):
        body = self._body(request)
        state = self.state()
        appeal = state.appeals.get(request.appeal_id)
        if appeal is None:
            raise ApiError(404, "not_found", "申诉不存在")
        if appeal["decisions"]:
            raise ApiError(409, "already_decided", "申诉已有裁决结论")
        decision = body.get("decision")
        if decision not in ("upheld", "rejected"):
            raise ApiError(400, "bad_request", "decision 只能是 upheld/rejected")
        ruling_id = None
        if body.get("ruling"):
            _status, payload = self.post_ruling(_Request(
                role=request.role, query={}, body=body["ruling"]))
            ruling_id = payload["ruling"]["ruling_id"]
        decisions = sum(len(a["decisions"]) for a in state.appeals.values())
        event = {
            "event_id": f"evt-{request.appeal_id}-dec-{decisions + 1}",
            "type": "appeal_decision",
            "occurred_at": self._now(request),
            "appeal_id": request.appeal_id,
            "decision": decision,
            "reason": body.get("reason"),
            "ruling_id": ruling_id,
        }
        self._append(event)
        updated = self.state().appeals[request.appeal_id]
        return 200, {"appeal": core.appeal_view(updated)}

    # ---- 医疗救治 ----
    def post_medical_request(self, request):
        body = self._body(request)
        location_km = body.get("location_km")
        category = body.get("category")
        if location_km is None or not category:
            raise ApiError(400, "bad_request", "需要 location_km 与 category")
        state = self.state()
        case_id = f"MC-{len(state.medical_cases) + 1:04d}"
        occurred_at = self._now(request)
        self._append({
            "event_id": f"evt-{case_id}-open", "type": "medical_case_opened",
            "occurred_at": occurred_at, "case_id": case_id,
            "location_km": location_km, "category": category,
            "bib": body.get("bib"), "opened_by": request.role,
        })
        prefer = body.get("prefer", "any")
        kinds = {"ambulance": ["ambulance"],
                 "station": ["station"],
                 "any": ["ambulance", "station"]}.get(prefer)
        if kinds is None:
            raise ApiError(400, "bad_request", f"未知 prefer: {prefer!r}")
        resource = medical.nearest_available(
            self.state().resources, location_km, kinds)
        assignment = None
        if resource is not None:
            self._append({
                "event_id": f"evt-{case_id}-lock-{resource['resource_id']}",
                "type": "medical_resource_locked",
                "occurred_at": occurred_at, "case_id": case_id,
                "resource_id": resource["resource_id"],
            })
            assignment = resource["resource_id"]
        case = self.state().medical_cases[case_id]
        return 201, {"case": medical.case_view(case, request.role),
                     "assigned_resource": assignment}

    def get_medical_case(self, request):
        case = self.state().medical_cases.get(request.case_id)
        if case is None:
            raise ApiError(404, "not_found", "病例不存在")
        return 200, {"case": medical.case_view(case, request.role)}

    def post_transport(self, request):
        body = self._body(request)
        state = self.state()
        case = state.medical_cases.get(request.case_id)
        if case is None:
            raise ApiError(404, "not_found", "病例不存在")
        if case["status"] not in ("open", "dispatched"):
            raise ApiError(409, "bad_state", "当前状态不能转运")
        hospital_id = body.get("hospital_id")
        if hospital_id:
            hospital = state.resources.get(hospital_id)
            if hospital is None or hospital["kind"] != "hospital":
                raise ApiError(404, "not_found", "定点医院不存在")
            if hospital["occupied"] >= hospital["capacity"]:
                raise ApiError(409, "hospital_full", "定点医院已满")
        else:
            hospital = medical.nearest_hospital(state.resources,
                                                case["location_km"])
            if hospital is None:
                raise ApiError(409, "hospital_full", "没有可用定点医院")
            hospital_id = hospital["resource_id"]
        self._append({
            "event_id": f"evt-{request.case_id}-transport",
            "type": "medical_transport",
            "occurred_at": self._now(request),
            "case_id": request.case_id, "hospital_id": hospital_id,
        })
        updated = self.state().medical_cases[request.case_id]
        return 200, {"case": medical.case_view(updated, request.role)}

    def post_close_case(self, request):
        body = self._body(request)
        state = self.state()
        case = state.medical_cases.get(request.case_id)
        if case is None:
            raise ApiError(404, "not_found", "病例不存在")
        if case["status"] == "closed":
            raise ApiError(409, "bad_state", "病例已关闭")
        self._append({
            "event_id": f"evt-{request.case_id}-close",
            "type": "medical_case_closed",
            "occurred_at": self._now(request),
            "case_id": request.case_id,
            "outcome": body.get("outcome"),
        })
        updated = self.state().medical_cases[request.case_id]
        return 200, {"case": medical.case_view(updated, request.role)}

    def get_resources(self, _request):
        resources = sorted(self.state().resources.values(),
                           key=lambda r: r["resource_id"])
        return 200, {"resources": resources}

    # ---- 完赛追溯 ----
    def get_trace(self, request):
        state = self.state()
        bib = request.bib
        athlete_id = state.bibs.get(bib)
        if athlete_id is None:
            raise ApiError(404, "not_found", "号码布不存在")
        registration = state.athletes.get(athlete_id, {})
        verifications = [{
            "phase": v.get("phase"), "decision": v.get("decision"),
            "template_ref": v.get("template_ref"), "device_id": v.get("device_id"),
            "occurred_at": v.get("occurred_at"), "event_id": v["event_id"],
        } for v in state.verifications_for(athlete_id)]
        timings = []
        for point in sorted(state.timings.get(bib, {})):
            for event in state.timing_events(bib, point):
                timings.append({
                    "point": point, "occurred_at": event["occurred_at"],
                    "event_id": event["event_id"],
                    "device_id": event.get("device_id"),
                    "wave": event.get("wave"),
                })
        corrections = state.time_corrections(athlete_id)
        result = None
        rows = self._standings(registration.get("distance"),
                               registration.get("gender"))
        for row in rows:
            if row["athlete_id"] == athlete_id:
                result = core.public_row(row)
                break
        starts = state.timing_events(bib, "start")
        cases = [medical.case_view(case, request.role)
                 for case in state.medical_cases.values()
                 if case.get("bib") == bib]
        return 200, {
            "bib": bib,
            "athlete": registration,
            "identity": {
                "registration_template_ref": registration.get("face_template_ref"),
                "verifications": verifications,
            },
            "pickup": state.pickups.get(athlete_id),
            "checkin": state.checkins.get(athlete_id),
            "wave": {
                "assigned": state.assigned_wave(athlete_id),
                "actual_start_wave": starts[0].get("wave") if starts else None,
                "requests": state.wave_requests.get(athlete_id, []),
            },
            "timings": timings,
            "time_corrections": {
                point: {"corrected_occurred_at": format_utc(instant),
                        "ruling_id": ruling_id}
                for point, (instant, ruling_id) in corrections.items()
            },
            "result": result,
            "eligibility": core.eligibility_detail(state, self.rules, athlete_id),
            "rulings": state.rulings_for(athlete_id),
            "appeals": [core.appeal_view(a) for a in state.appeals.values()
                        if a["event"].get("athlete_id") == athlete_id],
            "medical": {"cases": cases},
        }


class _Request:
    def __init__(self, role, query, body, **params):
        self.role = role
        self.query = query
        self.body = body
        for key, value in params.items():
            setattr(self, key, value)


def _error(exc):
    return {"error": {"code": exc.code, "message": exc.message}}


def make_handler(app):
    """把 App 包装成 http.server 处理器。"""

    class Handler(BaseHTTPRequestHandler):
        def _handle(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            status, payload = app.handle_http(
                self.command, self.path, self.headers, raw)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._handle()

        def do_POST(self):
            self._handle()

        def log_message(self, *_args):
            return

    return Handler
