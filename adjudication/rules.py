"""赛事规则版本：国籍资格、奖励范围、赛会纪录与申诉期限均按发布版本计算。"""

from __future__ import annotations

from datetime import timedelta

from .timeutil import parse_instant, parse_duration, to_utc


class RulesError(ValueError):
    """规则文档不合法。"""


class Rules:
    """一份不可变的赛事发布规则。每次判定都记录所使用的版本号。"""

    def __init__(self, doc):
        if not isinstance(doc, dict):
            raise RulesError("规则必须是对象")
        self.doc = dict(doc)
        self._validate()

    def _validate(self):
        doc = self.doc
        for key in ("version", "event_id", "published_at", "wave_change_cutoff",
                    "appeal_window_hours", "waves", "points", "awards"):
            if key not in doc:
                raise RulesError(f"规则缺少字段: {key}")
        parse_instant(doc["published_at"])
        parse_instant(doc["wave_change_cutoff"])
        window = doc["appeal_window_hours"]
        if not isinstance(window, (int, float)) or window <= 0:
            raise RulesError("appeal_window_hours 必须是正数")
        waves = doc["waves"]
        if not isinstance(waves, list) or not waves:
            raise RulesError("waves 必须是非空列表")
        seen = set()
        for wave in waves:
            if "wave" not in wave or "gun_time" not in wave:
                raise RulesError("每个 wave 需要 wave 与 gun_time")
            parse_instant(wave["gun_time"])
            if wave["wave"] in seen:
                raise RulesError(f"wave 重复: {wave['wave']}")
            seen.add(wave["wave"])
        if not isinstance(doc["points"], dict) or not doc["points"]:
            raise RulesError("points 必须按距离给出计时点列表")
        awards = doc["awards"]
        for name in ("overall", "domestic"):
            if name not in awards:
                raise RulesError(f"awards 缺少 {name}")
            if awards[name].get("places", 0) <= 0:
                raise RulesError(f"awards.{name}.places 必须是正数")
        if "nationality" not in awards["domestic"]:
            raise RulesError("awards.domestic 缺少 nationality 资格限定")

    @property
    def version(self):
        return self.doc["version"]

    @property
    def event_id(self):
        return self.doc["event_id"]

    def ranking_basis(self):
        """名次判定基准：net（净成绩）或 gun（枪声成绩）。"""
        return self.doc.get("ranking", {}).get("basis", "net")

    def appeal_window(self):
        return timedelta(hours=self.doc["appeal_window_hours"])

    def wave_change_cutoff(self):
        return to_utc(parse_instant(self.doc["wave_change_cutoff"]))

    def wave_numbers(self):
        return [w["wave"] for w in self.doc["waves"]]

    def wave_gun_utc(self, wave):
        for item in self.doc["waves"]:
            if item["wave"] == wave:
                return to_utc(parse_instant(item["gun_time"]))
        return None

    def required_points(self, distance):
        return list(self.doc["points"].get(distance, ["start", "finish"]))

    def award_cfg(self, name):
        return self.doc["awards"].get(name)

    def record(self, distance, gender):
        records = self.doc["awards"].get("record_bonus", {}).get("records", {})
        value = records.get(distance, {}).get(gender)
        return parse_duration(value) if value else None


def default_rules(fixture):
    """由赛事基础参数生成首个发布版本。"""
    waves = [
        {"wave": 1, "gun_time": "2026-09-20T07:00:00+08:00"},
        {"wave": 2, "gun_time": "2026-09-20T07:15:00+08:00"},
        {"wave": 3, "gun_time": "2026-09-20T07:30:00+08:00"},
        {"wave": 4, "gun_time": "2026-09-20T07:45:00+08:00"},
    ]
    if len(waves) != fixture["starts"]:
        raise RulesError("分枪数与赛事参数不一致")
    return Rules({
        "version": "2026.1",
        "event_id": fixture["event_id"],
        "published_at": "2026-08-01T10:00:00+08:00",
        "wave_change_cutoff": "2026-09-19T18:00:00+08:00",
        "appeal_window_hours": 24,
        "ranking": {"basis": "net"},
        "waves": waves,
        "points": {
            "marathon": ["start", "10k", "half", "30k", "finish"],
            "half_marathon": ["start", "10k", "finish"],
        },
        "awards": {
            "overall": {"places": fixture["awards"]["overall_places"], "basis": "net"},
            "domestic": {"places": fixture["awards"]["domestic_places"],
                         "basis": "net", "nationality": "CHN"},
            "record_bonus": {"basis": "gun", "records": {
                "marathon": {"male": "02:08:30", "female": "02:26:00"},
                "half_marathon": {"male": "01:02:00", "female": "01:10:00"},
            }},
        },
    })
