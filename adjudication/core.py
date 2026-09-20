"""赛务投影：由事件日志折叠出报名、检录、计时、裁决、公示与救治状态。

所有判定都是事件日志的纯函数：计时事件按发生时间排序，重复过线取最早
有效成绩，成绩更正以新裁决冲销旧结果，已公示榜单作为不可变快照保留。
"""

from __future__ import annotations

from .timeutil import parse_instant, to_utc, format_utc, format_duration

# 阻断奖励资格的标记（可由 clear_flag 裁决逐项清除）。
BLOCKING_FLAGS = {
    "identity_mismatch", "verification_failed", "template_reuse",
    "foreign_template", "wave_violation", "no_pickup", "no_checkin",
    "missed_point",
}


def _utc(event, field="occurred_at"):
    return to_utc(parse_instant(event[field]))


class RaceState:
    """事件日志的折叠结果。"""

    def __init__(self, resources=None):
        self.athletes = {}          # athlete_id -> 报名资料
        self.athlete_bib = {}       # athlete_id -> bib
        self.bibs = {}              # bib -> athlete_id
        self.pickups = {}           # athlete_id -> 领物事件
        self.checkins = {}          # athlete_id -> 检录事件
        self.verifications = []     # 全部人脸核验结论事件
        self.wave_requests = {}     # athlete_id -> [改枪申请]
        self.timings = {}           # bib -> point -> [计时事件]
        self.templates = {}         # 模板引用 -> {athlete_id}
        self.rulings = []           # 裁决事件（按到达顺序）
        self.appeals = {}           # appeal_id -> {"event", "decisions"}
        self.publications = {}      # publication_id -> 快照
        self.current_pub = {}       # (distance, gender) -> publication_id
        self.medical_cases = {}     # case_id -> 病例投影
        self.resources = resources or {}
        self.events = {}            # event_id -> event

    @classmethod
    def build(cls, journal, resources=None):
        state = cls(resources=resources)
        for _seq, event in journal:
            state.apply(event)
        return state

    def apply(self, event):
        self.events[event["event_id"]] = event
        handler = getattr(self, "_on_" + event["type"], None)
        if handler:
            handler(event)

    # ---- 报名与身份 ----
    def _on_registration(self, event):
        athlete = dict(event["athlete"])
        athlete_id = athlete["athlete_id"]
        self.athletes[athlete_id] = athlete
        template = athlete.get("face_template_ref")
        if template:
            self.templates.setdefault(template, set()).add(athlete_id)

    def _on_face_verification(self, event):
        self.verifications.append(event)

    def _on_bib_pickup(self, event):
        athlete_id = event["athlete_id"]
        self.pickups[athlete_id] = event
        self.athlete_bib[athlete_id] = event["bib"]
        self.bibs[event["bib"]] = athlete_id

    def _on_corral_checkin(self, event):
        self.checkins[event["athlete_id"]] = event

    def _on_wave_change_request(self, event):
        self.wave_requests.setdefault(event["athlete_id"], []).append(event)

    def _on_timing(self, event):
        points = self.timings.setdefault(event["bib"], {})
        points.setdefault(event["point"], []).append(event)

    # ---- 裁决 / 申诉 / 公示 ----
    def _on_ruling(self, event):
        self.rulings.append(event)

    def _on_appeal(self, event):
        self.appeals[event["appeal_id"]] = {"event": event, "decisions": []}

    def _on_appeal_decision(self, event):
        appeal = self.appeals.get(event["appeal_id"])
        if appeal is not None:
            appeal["decisions"].append(event)

    def _on_publication(self, event):
        snapshot = dict(event["snapshot"])
        snapshot.setdefault("publication_id", event["publication_id"])
        self.publications[event["publication_id"]] = snapshot
        key = (event["distance"], event["gender"])
        self.current_pub[key] = event["publication_id"]

    # ---- 医疗救治 ----
    def _on_medical_case_opened(self, event):
        self.medical_cases[event["case_id"]] = {
            "case_id": event["case_id"],
            "opened_at": event["occurred_at"],
            "location_km": event["location_km"],
            "category": event["category"],
            "bib": event.get("bib"),
            "opened_by": event.get("opened_by"),
            "status": "open",
            "resource_id": None,
            "hospital_id": None,
            "outcome": None,
            "history": [event["event_id"]],
        }

    def _on_medical_resource_locked(self, event):
        case = self.medical_cases.get(event["case_id"])
        resource = self.resources.get(event["resource_id"])
        if resource is not None and resource["status"] == "available":
            resource["status"] = "locked"
            resource["case_id"] = event["case_id"]
            if case is not None:
                case["resource_id"] = event["resource_id"]
                case["status"] = "dispatched"
                case["history"].append(event["event_id"])

    def _on_medical_resource_released(self, event):
        resource = self.resources.get(event["resource_id"])
        if resource is not None and resource.get("case_id") == event["case_id"]:
            resource["status"] = "available"
            resource["case_id"] = None

    def _on_medical_transport(self, event):
        case = self.medical_cases.get(event["case_id"])
        hospital = self.resources.get(event["hospital_id"])
        if case is not None and hospital is not None:
            hospital["occupied"] += 1
            case["hospital_id"] = event["hospital_id"]
            case["status"] = "transporting"
            case["history"].append(event["event_id"])

    def _on_medical_case_closed(self, event):
        case = self.medical_cases.get(event["case_id"])
        if case is None:
            return
        resource = self.resources.get(case.get("resource_id") or "")
        if resource is not None and resource.get("case_id") == case["case_id"]:
            resource["status"] = "available"
            resource["case_id"] = None
        hospital = self.resources.get(case.get("hospital_id") or "")
        if hospital is not None and hospital["occupied"] > 0:
            hospital["occupied"] -= 1
        case["status"] = "closed"
        case["outcome"] = event.get("outcome")
        case["history"].append(event["event_id"])

    # ---- 查询辅助 ----
    def verifications_for(self, athlete_id):
        return [v for v in self.verifications if v.get("athlete_id") == athlete_id]

    def rulings_for(self, athlete_id):
        return [r for r in self.rulings if r.get("athlete_id") == athlete_id]

    def timing_events(self, bib, point):
        """同一计时点的全部过线事件，按发生时间排序（重复过线取最早）。"""
        events = self.timings.get(bib, {}).get(point, [])
        return sorted(events, key=lambda e: (_utc(e), e["event_id"]))

    def assigned_wave(self, athlete_id):
        """当前有效分枪：领物分枪被 wave_change 裁决冲销后取最新值。"""
        wave = None
        pickup = self.pickups.get(athlete_id)
        if pickup:
            wave = pickup.get("wave")
        for ruling in self.rulings_for(athlete_id):
            if ruling["action"] == "wave_change":
                wave = ruling["params"]["to_wave"]
        return wave

    def is_disqualified(self, athlete_id):
        """disqualify 与 reinstate 按到达顺序冲销，取最新状态。"""
        disqualified = False
        for ruling in self.rulings_for(athlete_id):
            if ruling["action"] == "disqualify":
                disqualified = True
            elif ruling["action"] == "reinstate":
                disqualified = False
        return disqualified

    def cleared_flags(self, athlete_id):
        return {
            ruling["params"]["flag"]
            for ruling in self.rulings_for(athlete_id)
            if ruling["action"] == "clear_flag" and "flag" in ruling.get("params", {})
        }

    def time_corrections(self, athlete_id):
        """adjust_time 裁决给出的计时更正：point -> (utc, ruling_id)。"""
        corrections = {}
        for ruling in self.rulings_for(athlete_id):
            if ruling["action"] == "adjust_time":
                params = ruling["params"]
                corrections[params["point"]] = (
                    to_utc(parse_instant(params["corrected_occurred_at"])),
                    ruling["ruling_id"],
                )
        return corrections

    def effective_times(self, athlete_id):
        """每个计时点的有效时间：裁决更正优先，否则取最早一次过线。"""
        bib = self.athlete_bib.get(athlete_id)
        times = {}
        if bib is not None:
            for point, events in self.timings.get(bib, {}).items():
                first = min(events, key=lambda e: (_utc(e), e["event_id"]))
                times[point] = {"utc": _utc(first), "source": "device",
                                "event_id": first["event_id"]}
        for point, (instant, ruling_id) in self.time_corrections(athlete_id).items():
            times[point] = {"utc": instant, "source": "ruling",
                            "ruling_id": ruling_id}
        return times

    def identity_flags(self, athlete_id):
        """人脸核验结论与模板引用的一致性检查（替跑线索）。"""
        registration = self.athletes.get(athlete_id)
        if registration is None:
            return ["unregistered"]
        flags = []
        template = registration.get("face_template_ref")
        if template and len(self.templates.get(template, ())) > 1:
            flags.append("template_reuse")
        for verif in self.verifications_for(athlete_id):
            phase = verif.get("phase", "unknown")
            presented = verif.get("template_ref")
            if verif.get("decision") == "no_match":
                flags.append(f"verification_failed:{phase}")
            elif verif.get("decision") == "match" and template \
                    and presented and presented != template:
                flags.append(f"identity_mismatch:{phase}")
            if presented and presented != template:
                holders = self.templates.get(presented, set())
                if any(holder != athlete_id for holder in holders):
                    flags.append(f"foreign_template:{phase}")
        return sorted(set(flags))

    def process_flags(self, athlete_id):
        flags = []
        if athlete_id not in self.pickups:
            flags.append("no_pickup")
        if athlete_id not in self.checkins:
            flags.append("no_checkin")
        return flags

    def course_flags(self, athlete_id, rules):
        """赛道完整性：必需计时点缺失、实际起跑枪次与有效分枪不一致。"""
        registration = self.athletes[athlete_id]
        bib = self.athlete_bib.get(athlete_id)
        flags = []
        times = self.effective_times(athlete_id)
        for point in rules.required_points(registration["distance"]):
            if point not in times:
                flags.append(f"missed_point:{point}")
        assigned = self.assigned_wave(athlete_id)
        starts = self.timing_events(bib, "start") if bib else []
        if starts and assigned is not None:
            actual_wave = starts[0].get("wave")
            if actual_wave is not None and actual_wave != assigned:
                flags.append("wave_violation")
        return flags

    def athlete_flags(self, athlete_id, rules):
        flags = (self.identity_flags(athlete_id) + self.process_flags(athlete_id)
                 + self.course_flags(athlete_id, rules))
        cleared = self.cleared_flags(athlete_id)
        return sorted({flag for flag in flags if flag not in cleared})

    def award_eligible(self, athlete_id, rules):
        if self.is_disqualified(athlete_id):
            return False
        return not any(flag.split(":")[0] in BLOCKING_FLAGS
                       for flag in self.athlete_flags(athlete_id, rules))


def eligibility_detail(state, rules, athlete_id):
    """奖励资格逐项依据，供追溯接口返回。"""
    flags = state.athlete_flags(athlete_id, rules)
    disqualified = state.is_disqualified(athlete_id)
    blocking = [flag for flag in flags if flag.split(":")[0] in BLOCKING_FLAGS]
    return {
        "registered": athlete_id in state.athletes,
        "bib_picked_up": athlete_id in state.pickups,
        "checked_in": athlete_id in state.checkins,
        "disqualified": disqualified,
        "flags": flags,
        "blocking_flags": blocking,
        "award_eligible": not disqualified and not blocking,
    }


def compute_standings(state, rules, distance, gender):
    """计算某距离/性别的名次表。并列同名次，成绩更正与裁决即时生效。"""
    rows = []
    for athlete_id, registration in sorted(state.athletes.items()):
        if registration.get("distance") != distance \
                or registration.get("gender") != gender:
            continue
        times = state.effective_times(athlete_id)
        if "finish" not in times:
            continue
        bib = state.athlete_bib.get(athlete_id)
        wave = state.assigned_wave(athlete_id)
        gun_start = rules.wave_gun_utc(wave) if wave is not None else None
        finish_utc = times["finish"]["utc"]
        start_utc = times["start"]["utc"] if "start" in times else None
        gun_time = finish_utc - gun_start if gun_start else None
        net_time = (finish_utc - start_utc) if start_utc else gun_time
        flags = state.athlete_flags(athlete_id, rules)
        disqualified = state.is_disqualified(athlete_id)
        blocking = [f for f in flags if f.split(":")[0] in BLOCKING_FLAGS]
        rows.append({
            "athlete_id": athlete_id,
            "bib": bib,
            "name": registration.get("name"),
            "nationality": registration.get("nationality"),
            "gender": gender,
            "distance": distance,
            "wave": wave,
            "finish_utc": format_utc(finish_utc),
            "gun_time": format_duration(gun_time),
            "net_time": format_duration(net_time),
            "_gun": gun_time,
            "_net": net_time,
            "flags": flags,
            "status": "dsq" if disqualified else "ranked",
            "award_eligible": not disqualified and not blocking,
            "rank": None,
            "awards": [],
        })
    basis = rules.ranking_basis()
    ranked = []
    for row in rows:
        if row["status"] != "ranked":
            continue
        primary = row["_net"] if basis == "net" else row["_gun"]
        fallback = row["_gun"] if basis == "net" else row["_net"]
        row["_basis"] = primary if primary is not None else fallback
        if row["_basis"] is None:
            row["status"] = "incomplete"
            continue
        ranked.append(row)
    ranked.sort(key=lambda r: (
        r["_basis"],
        r["_gun"] if r["_gun"] is not None else r["_basis"],
        r["athlete_id"],
    ))
    previous_time, previous_rank = None, 0
    for index, row in enumerate(ranked, 1):
        if row["_basis"] != previous_time:
            previous_rank = index
            previous_time = row["_basis"]
        row["rank"] = previous_rank
    _apply_tie_breaks(state, ranked, distance, gender)
    ordered = sorted(ranked, key=lambda r: (r["rank"], r["athlete_id"]))
    rest = [r for r in rows if r["status"] != "ranked"]
    rest.sort(key=lambda r: (r["status"], r["athlete_id"]))
    return ordered + rest


def _apply_tie_breaks(state, ranked, distance, gender):
    """tie_break 裁决把并列组拆成有序名次（如终点摄影仲裁）。"""
    rulings = [
        r for r in state.rulings
        if r["action"] == "tie_break"
        and r.get("params", {}).get("distance", distance) == distance
        and r.get("params", {}).get("gender", gender) == gender
    ]
    if not rulings:
        return
    groups = {}
    for row in ranked:
        groups.setdefault(row["rank"], []).append(row)
    for ruling in rulings:
        order = ruling["params"].get("order") or []
        position = {athlete_id: idx for idx, athlete_id in enumerate(order)}
        for rank, members in groups.items():
            if len(members) < 2:
                continue
            if all(m["athlete_id"] in position for m in members):
                members.sort(key=lambda m: position[m["athlete_id"]])
                for offset, member in enumerate(members):
                    member["rank"] = rank + offset


def _place_rows(ordered_rows, places, key):
    """按 key 竞赛排名取前 places 名；边界并列全部入选。"""
    out = []
    previous_key, previous_rank = None, 0
    for index, row in enumerate(ordered_rows, 1):
        value = key(row)
        if value != previous_key:
            previous_rank = index
            previous_key = value
        if previous_rank > places:
            break
        out.append((row, previous_rank))
    return out


def compute_awards(rows, rules, distance, gender):
    """按规则版本计算名次奖、中国籍特别奖与破纪录奖励，写入每行 awards。"""
    for row in rows:
        row["awards"] = []

    def basis_value(row, basis):
        if basis == "gun":
            return row["_gun"]
        return row["_net"] if row["_net"] is not None else row["_gun"]

    eligible = [r for r in rows if r["status"] == "ranked" and r["award_eligible"]]

    def ordered(pool, basis):
        return sorted(pool, key=lambda r: (
            basis_value(r, basis),
            r["_gun"] if r["_gun"] is not None else basis_value(r, basis),
            r["athlete_id"],
        ))

    overall_cfg = rules.award_cfg("overall")
    if overall_cfg:
        basis = overall_cfg.get("basis", rules.ranking_basis())
        for row, place in _place_rows(
                ordered(eligible, basis), overall_cfg["places"],
                lambda r: basis_value(r, basis)):
            row["awards"].append({
                "type": "overall", "place": place, "basis": basis,
                "time": format_duration(basis_value(row, basis)),
                "rules_version": rules.version,
            })

    domestic_cfg = rules.award_cfg("domestic")
    if domestic_cfg:
        basis = domestic_cfg.get("basis", rules.ranking_basis())
        pool = [r for r in eligible
                if r.get("nationality") == domestic_cfg.get("nationality")]
        for row, place in _place_rows(
                ordered(pool, basis), domestic_cfg["places"],
                lambda r: basis_value(r, basis)):
            row["awards"].append({
                "type": "domestic", "place": place, "basis": basis,
                "nationality": domestic_cfg.get("nationality"),
                "time": format_duration(basis_value(row, basis)),
                "rules_version": rules.version,
            })

    record_cfg = rules.award_cfg("record_bonus")
    if record_cfg:
        record = rules.record(distance, gender)
        if record is not None:
            basis = record_cfg.get("basis", "gun")
            for row in eligible:
                value = basis_value(row, basis)
                if value is not None and value < record:
                    row["awards"].append({
                        "type": "record_bonus", "basis": basis,
                        "time": format_duration(value),
                        "previous_record": format_duration(record),
                        "rules_version": rules.version,
                    })
    return rows


def public_row(row):
    """去掉内部计算字段，返回可序列化的名次行。"""
    return {key: value for key, value in row.items() if not key.startswith("_")}


def appeal_view(appeal):
    """申诉及其处理历史。"""
    event = appeal["event"]
    decisions = appeal["decisions"]
    status = decisions[-1]["decision"] if decisions else "open"
    history = [{"type": "filed", "occurred_at": event["occurred_at"]}]
    for decision in decisions:
        history.append({
            "type": "decision",
            "decision": decision["decision"],
            "reason": decision.get("reason"),
            "occurred_at": decision["occurred_at"],
            "ruling_id": decision.get("ruling_id"),
        })
    return {
        "appeal_id": event["appeal_id"],
        "athlete_id": event["athlete_id"],
        "publication_id": event.get("publication_id"),
        "target_kind": event.get("target_kind"),
        "grounds": event.get("grounds"),
        "filed_at": event["occurred_at"],
        "status": status,
        "history": history,
    }
