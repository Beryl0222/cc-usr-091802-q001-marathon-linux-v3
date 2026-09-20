"""赛务判定中枢的场景重演测试。

覆盖：断网补传/重放/乱序、冒名入场、并列成绩、跨枪计时、成绩更正冲销、
公示不可变、申诉期限、中国籍特别奖与破纪录奖励、途中急救调度与脱敏。
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from adjudication import core
from adjudication.api import App
from adjudication.journal import Journal
from adjudication.rules import Rules, RulesError, default_rules
from adjudication.timeutil import format_duration, parse_duration

CST = timezone(timedelta(hours=8))
GUN = {
    1: datetime(2026, 9, 20, 7, 0, tzinfo=CST),
    2: datetime(2026, 9, 20, 7, 15, tzinfo=CST),
    3: datetime(2026, 9, 20, 7, 30, tzinfo=CST),
    4: datetime(2026, 9, 20, 7, 45, tzinfo=CST),
}


def load_fixture():
    return json.loads(Path("fixtures/sample.json").read_text(encoding="utf-8"))


def make_app(journal=None):
    fixture = load_fixture()
    return App(default_rules(fixture),
               journal=journal if journal is not None else Journal(),
               medical_cfg=fixture["medical"])


def call(app, method, path, role="admin", query=None, body=None):
    return app.handle(method, path, query or {}, {"X-Role": role}, body)


def iso(dt):
    return dt.isoformat()


def ev_registration(aid, tpl, name=None, nationality="CHN", gender="male",
                    distance="marathon"):
    return {"event_id": f"reg-{aid}", "type": "registration",
            "occurred_at": "2026-09-01T10:00:00+08:00",
            "athlete": {"athlete_id": aid, "name": name or aid,
                        "gender": gender, "nationality": nationality,
                        "distance": distance, "id_doc_hash": f"sha256:{aid}",
                        "face_template_ref": tpl}}


def ev_verify(aid, tpl, phase="checkin", decision="match", eid=None):
    return {"event_id": eid or f"ver-{aid}-{phase}", "type": "face_verification",
            "occurred_at": "2026-09-20T06:30:00+08:00", "athlete_id": aid,
            "phase": phase, "template_ref": tpl, "decision": decision,
            "device_id": "GATE-1"}


def ev_pickup(aid, bib, wave):
    return {"event_id": f"pick-{aid}", "type": "bib_pickup",
            "occurred_at": "2026-09-19T10:00:00+08:00",
            "athlete_id": aid, "bib": bib, "wave": wave, "corral": "A"}


def ev_checkin(aid, wave):
    return {"event_id": f"chk-{aid}", "type": "corral_checkin",
            "occurred_at": "2026-09-20T06:40:00+08:00",
            "athlete_id": aid, "wave": wave, "corral": "A"}


def ev_timing(bib, point, at, wave=None, eid=None, device="MAT-1"):
    event = {"event_id": eid or f"t-{bib}-{point}-{at}", "type": "timing",
             "occurred_at": at, "bib": bib, "point": point,
             "device_id": device}
    if wave is not None:
        event["wave"] = wave
    return event


def course_events(bib, wave, net_seconds, suffix=""):
    """生成完整赛程计时：起跑后 5 秒过起点线，按净成绩铺设计时点。"""
    start = GUN[wave] + timedelta(seconds=5)
    finish = start + timedelta(seconds=net_seconds)
    points = [("start", start), ("10k", start + timedelta(minutes=30)),
              ("half", start + timedelta(minutes=60)),
              ("30k", start + timedelta(minutes=95)), ("finish", finish)]
    return [ev_timing(bib, point, iso(at), wave=wave if point == "start" else None,
                      eid=f"t-{bib}-{point}{suffix}")
            for point, at in points]


def enter_athlete(app, aid, bib, wave, tpl=None, net=7200, nationality="CHN"):
    tpl = tpl or f"tpl-{aid}"
    events = [ev_registration(aid, tpl, nationality=nationality),
              ev_verify(aid, tpl, phase="pickup"),
              ev_pickup(aid, bib, wave),
              ev_verify(aid, tpl, phase="checkin"),
              ev_checkin(aid, wave)]
    events += course_events(bib, wave, net)
    status, payload = call(app, "POST", "/v1/events", role="timing",
                           body={"events": events})
    assert status == 200, payload
    assert payload["summary"]["rejected"] == 0, payload
    return payload


def standings(app, distance="marathon", gender="male"):
    status, payload = call(app, "GET", "/v1/results", role="awards",
                           query={"distance": distance, "gender": gender})
    assert status == 200, payload
    return payload["standings"]


def publish(app, at, distance="marathon", gender="male"):
    status, payload = call(app, "POST", "/v1/leaderboards/publish",
                           role="awards",
                           body={"distance": distance, "gender": gender,
                                 "occurred_at": at})
    assert status == 201, payload
    return payload["publication"]


class ReplayAndOrderingTest(unittest.TestCase):
    """断网补传、同一事件重放、乱序到达与重复过线。"""

    def test_out_of_order_replay_and_backfill(self):
        app = make_app()
        # 三个运动员的完整流程事件
        flow = []
        for aid, bib, net in (("A1", "B1", 7200), ("A2", "B2", 7140),
                              ("A3", "B3", 7260)):
            tpl = f"tpl-{aid}"
            flow += [ev_registration(aid, tpl), ev_verify(aid, tpl, "pickup"),
                     ev_pickup(aid, bib, 1), ev_verify(aid, tpl, "checkin"),
                     ev_checkin(aid, 1)]
            flow += course_events(bib, 1, net)
        timing_events = [e for e in flow if e["type"] == "timing"]
        other_events = [e for e in flow if e["type"] != "timing"]

        # 计时点设备离线后补传：终点先到、起点后到，整体乱序
        backfill = list(reversed(timing_events))
        status, payload = call(app, "POST", "/v1/events", role="timing",
                               body={"events": backfill})
        self.assertEqual(payload["summary"]["accepted"], len(backfill))
        # 同一批事件重放：全部判为重复，不改变状态
        status, payload = call(app, "POST", "/v1/events", role="timing",
                               body={"events": backfill})
        self.assertEqual(payload["summary"]["duplicate"], len(backfill))
        self.assertEqual(payload["summary"]["accepted"], 0)
        # 重复过线：同一号码布终点二次过线（更晚），最早成绩有效
        dup_finish = ev_timing("B1", "finish", iso(GUN[1] + timedelta(
            seconds=7205 + 5)), eid="t-B1-finish-late")
        call(app, "POST", "/v1/events", role="timing",
             body={"events": [dup_finish]})
        # 报名与检录流程最后到达
        call(app, "POST", "/v1/events", role="checkin",
             body={"events": other_events})

        rows = standings(app)
        self.assertEqual([r["athlete_id"] for r in rows], ["A2", "A1", "A3"])
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3])
        self.assertEqual(rows[1]["net_time"], "02:00:00.000")

        # 事件重排后重建，名次完全一致
        shuffled = Journal()
        shuffled.append(list(reversed(flow)) + [dup_finish])
        rows2 = standings(make_app(shuffled))
        self.assertEqual(
            [(r["athlete_id"], r["rank"], r["net_time"]) for r in rows],
            [(r["athlete_id"], r["rank"], r["net_time"]) for r in rows2])

    def test_journal_persistence_roundtrip(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "journal.jsonl")
            app = make_app(Journal(path=path))
            enter_athlete(app, "A1", "B1", 1, net=7200)
            digest = app.journal.digest()
            # 重启后从文件重放，判定结果一致
            app2 = make_app(Journal(path=path))
            self.assertEqual(app2.journal.digest(), digest)
            self.assertEqual(standings(app2)[0]["athlete_id"], "A1")


class ImpersonationTest(unittest.TestCase):
    """冒名入场：人脸模板与报名模板不一致。"""

    def test_foreign_template_flagged_and_excluded(self):
        app = make_app()
        enter_athlete(app, "REAL", "B1", 1, net=7300)
        # 替跑者持 REAL 的号码布流程之外，另有一人冒用他人模板检录
        events = [ev_registration("FAKE", "tpl-FAKE"),
                  ev_verify("FAKE", "tpl-FAKE", phase="pickup"),
                  ev_pickup("FAKE", "B2", 1),
                  # 检录时人脸比对通过，但命中的是别人（REAL）的模板
                  ev_verify("FAKE", "tpl-REAL", phase="checkin",
                            eid="ver-FAKE-checkin"),
                  ev_checkin("FAKE", 1)]
        events += course_events("B2", 1, 7000)  # 替跑成绩全场最快
        call(app, "POST", "/v1/events", role="checkin",
             body={"events": events})

        rows = standings(app)
        fake = next(r for r in rows if r["athlete_id"] == "FAKE")
        self.assertIn("identity_mismatch:checkin", fake["flags"])
        self.assertIn("foreign_template:checkin", fake["flags"])
        self.assertFalse(fake["award_eligible"])
        # 名次保留（确有过线成绩），但名次奖顺延给 REAL
        self.assertEqual(fake["rank"], 1)
        real = next(r for r in rows if r["athlete_id"] == "REAL")
        self.assertEqual(real["awards"][0]["type"], "overall")
        self.assertEqual(real["awards"][0]["place"], 1)
        self.assertEqual(fake["awards"], [])

        # 仲裁确认为误判后，clear_flag 冲销标记，资格恢复
        for flag in ("identity_mismatch:checkin", "foreign_template:checkin"):
            status, _ = call(app, "POST", "/v1/rulings", role="awards",
                             body={"action": "clear_flag", "athlete_id": "FAKE",
                                   "params": {"flag": flag},
                                   "reason": "设备模板库串号，人工复核为本人"})
            self.assertEqual(status, 201)
        fake = next(r for r in standings(app) if r["athlete_id"] == "FAKE")
        self.assertTrue(fake["award_eligible"])
        self.assertEqual(fake["awards"][0]["place"], 1)

    def test_no_match_verification_flagged(self):
        app = make_app()
        events = [ev_registration("A9", "tpl-A9"),
                  ev_verify("A9", "tpl-A9", phase="pickup"),
                  ev_pickup("A9", "B9", 1),
                  ev_verify("A9", "tpl-A9", phase="checkin",
                            decision="no_match"),
                  ev_checkin("A9", 1)]
        events += course_events("B9", 1, 7200)
        call(app, "POST", "/v1/events", role="checkin",
             body={"events": events})
        row = standings(app)[0]
        self.assertIn("verification_failed:checkin", row["flags"])
        self.assertFalse(row["award_eligible"])

    def test_biometric_payload_rejected(self):
        app = make_app()
        bad_reg = ev_registration("AX", "tpl-AX")
        bad_reg["athlete"]["photo"] = "base64..."
        bad_ver = ev_verify("AX", "tpl-AX")
        bad_ver["template_data"] = "raw-feature-vector"
        _, payload = call(app, "POST", "/v1/events", role="checkin",
                          body={"events": [bad_reg, bad_ver]})
        self.assertEqual(payload["summary"]["rejected"], 2)
        self.assertIn("生物特征", payload["receipts"][0]["error"])


class TieBreakTest(unittest.TestCase):
    """并列成绩：默认共享名次，终点摄影仲裁后拆分。"""

    def test_shared_rank_then_tie_break_ruling(self):
        app = make_app()
        enter_athlete(app, "A1", "B1", 1, net=7200)
        enter_athlete(app, "A2", "B2", 1, net=7200)
        enter_athlete(app, "A3", "B3", 1, net=7201)
        rows = standings(app)
        self.assertEqual([r["rank"] for r in rows], [1, 1, 3])
        places = {r["athlete_id"]: r["awards"][0]["place"] for r in rows}
        self.assertEqual(places, {"A1": 1, "A2": 1, "A3": 3})

        status, payload = call(
            app, "POST", "/v1/rulings", role="awards",
            body={"action": "tie_break",
                  "params": {"distance": "marathon", "gender": "male",
                             "order": ["A2", "A1"]},
                  "reason": "终点摄影：A2 躯干先过线"})
        self.assertEqual(status, 201, payload)
        rows = standings(app)
        ranks = {r["athlete_id"]: r["rank"] for r in rows}
        self.assertEqual(ranks, {"A2": 1, "A1": 2, "A3": 3})


class CrossWaveTest(unittest.TestCase):
    """跨枪计时与临时改枪。"""

    def test_wave_violation_then_legitimized(self):
        app = make_app()
        tpl = "tpl-W1"
        events = [ev_registration("W1", tpl),
                  ev_verify("W1", tpl, phase="pickup"),
                  ev_pickup("W1", "BW", 1),   # 领物分在第一枪
                  ev_verify("W1", tpl, phase="checkin",
                            eid="ver-W1-checkin"),
                  ev_checkin("W1", 1)]
        # 实际从第二枪起跑
        events += course_events("BW", 2, 7500)
        call(app, "POST", "/v1/events", role="checkin",
             body={"events": events})
        row = standings(app)[0]
        self.assertIn("wave_violation", row["flags"])
        self.assertFalse(row["award_eligible"])

        # 赛前已提交改枪申请，仲裁补录 wave_change 裁决后违规消除
        call(app, "POST", "/v1/events", role="checkin", body={"events": [
            {"event_id": "wcr-W1", "type": "wave_change_request",
             "occurred_at": "2026-09-19T17:00:00+08:00",
             "athlete_id": "W1", "from_wave": 1, "to_wave": 2,
             "reason": "伤病降为第二枪"}]})
        status, payload = call(app, "POST", "/v1/rulings", role="awards",
                               body={"action": "wave_change",
                                     "athlete_id": "W1",
                                     "params": {"to_wave": 2},
                                     "reason": "批准赛前改枪申请"})
        self.assertEqual(status, 201, payload)
        row = standings(app)[0]
        self.assertEqual(row["flags"], [])
        self.assertTrue(row["award_eligible"])
        self.assertEqual(row["wave"], 2)

        # 追溯：能看到改枪申请与裁决链
        status, trace = call(app, "GET", "/v1/finishers/BW/trace", role="awards")
        self.assertEqual(status, 200)
        self.assertEqual(trace["wave"]["assigned"], 2)
        self.assertEqual(trace["wave"]["actual_start_wave"], 2)
        self.assertEqual(len(trace["wave"]["requests"]), 1)
        self.assertEqual(trace["rulings"][0]["action"], "wave_change")


class CorrectionAndPublicationTest(unittest.TestCase):
    """成绩更正以新裁决冲销旧结果；已公示榜单不可静默覆盖。"""

    def test_correction_supersedes_and_snapshots_immutable(self):
        app = make_app()
        enter_athlete(app, "A1", "B1", 1, net=7200)
        enter_athlete(app, "A2", "B2", 1, net=7300)
        pub1 = publish(app, "2026-09-20T12:00:00+08:00")
        self.assertEqual(pub1["version"], 1)
        self.assertEqual(pub1["standings"][0]["athlete_id"], "A1")

        # 计时设备时钟误差：A2 终点成绩更正为 1:58:25（净），名次反转
        corrected_finish = iso(GUN[1] + timedelta(seconds=5 + 7105))
        status, payload = call(
            app, "POST", "/v1/rulings", role="awards",
            body={"action": "adjust_time", "athlete_id": "A2",
                  "params": {"point": "finish",
                             "corrected_occurred_at": corrected_finish},
                  "reason": "终点设备时钟漂移，按备份计时修正"})
        self.assertEqual(status, 201, payload)

        # 已公示的 v1 不被静默覆盖：仍然是 A1 第一
        status, board = call(app, "GET", "/v1/leaderboards/PUB-0001",
                             role="public")
        self.assertEqual(board["publication"]["standings"][0]["athlete_id"],
                         "A1")
        # 当前榜单指针仍指向 v1，直到再次公示
        status, current = call(app, "GET", "/v1/leaderboards/current",
                               role="public",
                               query={"distance": "marathon", "gender": "male"})
        self.assertEqual(current["publication"]["publication_id"], "PUB-0001")

        pub2 = publish(app, "2026-09-20T13:00:00+08:00")
        self.assertEqual(pub2["version"], 2)
        self.assertEqual(pub2["standings"][0]["athlete_id"], "A2")
        self.assertEqual(pub2["standings"][0]["net_time"], "01:58:25.000")
        # v1 依旧原样
        status, board = call(app, "GET", "/v1/leaderboards/PUB-0001",
                             role="public")
        self.assertEqual(board["publication"]["standings"][0]["athlete_id"],
                         "A1")

        # 取消资格与恢复资格同样以裁决冲销
        call(app, "POST", "/v1/rulings", role="awards",
             body={"action": "disqualify", "athlete_id": "A2",
                   "reason": "申诉复核中"})
        pub3 = publish(app, "2026-09-20T14:00:00+08:00")
        self.assertEqual(pub3["standings"][0]["athlete_id"], "A1")
        self.assertEqual(pub3["standings"][1]["status"], "dsq")
        call(app, "POST", "/v1/rulings", role="awards",
             body={"action": "reinstate", "athlete_id": "A2",
                   "reason": "申诉不成立，恢复成绩"})
        pub4 = publish(app, "2026-09-20T15:00:00+08:00")
        self.assertEqual(pub4["standings"][0]["athlete_id"], "A2")
        # 四个版本全部可回溯
        for pub_id in ("PUB-0001", "PUB-0002", "PUB-0003", "PUB-0004"):
            status, _ = call(app, "GET", f"/v1/leaderboards/{pub_id}",
                             role="public")
            self.assertEqual(status, 200)


class AppealTest(unittest.TestCase):
    """申诉期限按公示所用规则版本计算，处理历史可追溯。"""

    def test_window_enforced_and_history_traced(self):
        app = make_app()
        enter_athlete(app, "A1", "B1", 1, net=7200)
        pub = publish(app, "2026-09-20T12:00:00+08:00")

        # 窗口内（公示后 1 小时）申诉受理
        status, payload = call(app, "POST", "/v1/appeals", role="athlete",
                               body={"athlete_id": "A1",
                                     "target_kind": "result",
                                     "grounds": "净成绩少记 5 秒",
                                     "occurred_at":
                                         "2026-09-20T13:00:00+08:00"})
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["appeal"]["status"], "open")
        appeal_id = payload["appeal"]["appeal_id"]

        # 超出 24 小时窗口的申诉被拒绝
        status, payload = call(app, "POST", "/v1/appeals", role="athlete",
                               body={"athlete_id": "A1",
                                     "grounds": "超时申诉",
                                     "occurred_at":
                                         "2026-09-21T13:00:00+08:00"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "appeal_window_closed")

        # 申诉成立：连带生成成绩更正裁决
        corrected = iso(GUN[1] + timedelta(seconds=5 + 7195))
        status, payload = call(
            app, "POST", f"/v1/appeals/{appeal_id}/decide", role="awards",
            body={"decision": "upheld", "reason": "备份计时确认",
                  "ruling": {"action": "adjust_time", "athlete_id": "A1",
                             "params": {"point": "finish",
                                        "corrected_occurred_at": corrected},
                             "reason": "依申诉裁决更正"}})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["appeal"]["status"], "upheld")
        self.assertEqual(payload["appeal"]["history"][-1]["ruling_id"],
                         "RUL-0001")

        # 追溯接口能看到申诉历史与更正依据
        status, trace = call(app, "GET", "/v1/finishers/B1/trace",
                             role="awards")
        self.assertEqual(trace["appeals"][0]["status"], "upheld")
        self.assertIn("finish", trace["time_corrections"])
        self.assertEqual(trace["result"]["net_time"], "01:59:55.000")


class AwardScopeTest(unittest.TestCase):
    """名次奖、中国籍前八特别奖与破赛会纪录奖励。"""

    def test_domestic_and_record_bonus(self):
        app = make_app()
        # 外籍选手全场最快并破纪录：拿名次奖与破纪录奖，无中国籍特别奖
        enter_athlete(app, "KEN1", "B1", 1, net=6600, nationality="KEN")
        # 中国籍前两名（第二名枪声成绩 2:10:05，未破纪录）
        enter_athlete(app, "CN1", "B2", 1, net=7000)
        enter_athlete(app, "CN2", "B3", 1, net=7800)
        rows = standings(app)
        by_id = {r["athlete_id"]: r for r in rows}

        ken = by_id["KEN1"]
        self.assertEqual(ken["rank"], 1)
        types = [a["type"] for a in ken["awards"]]
        self.assertIn("overall", types)
        self.assertIn("record_bonus", types)
        self.assertNotIn("domestic", types)

        cn1 = by_id["CN1"]
        domestic = next(a for a in cn1["awards"] if a["type"] == "domestic")
        self.assertEqual(domestic["place"], 1)
        self.assertEqual(domestic["nationality"], "CHN")
        self.assertEqual(domestic["rules_version"], "2026.1")
        # CN1 枪声成绩 1:56:45 同样破 2:08:30 的赛会纪录
        self.assertIn("record_bonus", [a["type"] for a in cn1["awards"]])
        bonus = next(a for a in cn1["awards"] if a["type"] == "record_bonus")
        self.assertEqual(bonus["previous_record"], "02:08:30.000")

        cn2 = by_id["CN2"]
        domestic2 = next(a for a in cn2["awards"] if a["type"] == "domestic")
        self.assertEqual(domestic2["place"], 2)
        self.assertNotIn("record_bonus",
                         [a["type"] for a in cn2["awards"]])


class RulesVersionTest(unittest.TestCase):
    """奖励范围与申诉期限按各自的发布版本计算。"""

    def test_new_rules_version_applies_prospectively(self):
        app = make_app()
        enter_athlete(app, "A1", "B1", 1, net=7200)
        pub1 = publish(app, "2026-09-20T12:00:00+08:00")
        self.assertEqual(pub1["rules_version"], "2026.1")

        fixture = load_fixture()
        doc = default_rules(fixture).doc
        doc["version"] = "2026.2"
        doc["appeal_window_hours"] = 1
        doc["awards"]["domestic"]["places"] = 4
        status, payload = call(app, "POST", "/v1/rules", role="admin",
                               body={"rules": doc, "activate": True})
        self.assertEqual(status, 201, payload)

        pub2 = publish(app, "2026-09-20T13:00:00+08:00")
        self.assertEqual(pub2["rules_version"], "2026.2")

        # 对 v1 榜单的申诉按 2026.1 的 24 小时窗口：公示后 2 小时仍受理
        status, _ = call(app, "POST", "/v1/appeals", role="athlete",
                         body={"athlete_id": "A1",
                               "publication_id": pub1["publication_id"],
                               "grounds": "按旧版规则申诉",
                               "occurred_at": "2026-09-20T14:00:00+08:00"})
        self.assertEqual(status, 201)
        # 对 v2 榜单的申诉按 2026.2 的 1 小时窗口：公示后 2 小时已截止
        status, payload = call(app, "POST", "/v1/appeals", role="athlete",
                               body={"athlete_id": "A1",
                                     "publication_id": pub2["publication_id"],
                                     "grounds": "按新版规则已超时",
                                     "occurred_at":
                                         "2026-09-20T14:30:00+08:00"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "appeal_window_closed")


class MedicalTest(unittest.TestCase):
    """途中急救：脱敏求助、最近资源锁定、转运隔离与资源释放。"""

    def test_dispatch_locks_nearest_and_desensitizes(self):
        app = make_app()
        enter_athlete(app, "A1", "B1", 1, net=7200)

        # 20.0km 处求助：锁定最近救护车 AMB-21
        status, payload = call(app, "POST", "/v1/medical/requests",
                               role="marshal",
                               body={"location_km": 20.0,
                                     "category": "cardiac", "bib": "B1",
                                     "prefer": "ambulance"})
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["assigned_resource"], "AMB-21")
        case_id = payload["case"]["case_id"]

        # 附近第二起求助：AMB-21 已锁定，顺延到次近的 AMB-22
        status, payload2 = call(app, "POST", "/v1/medical/requests",
                                role="marshal",
                                body={"location_km": 20.1,
                                      "category": "trauma",
                                      "prefer": "ambulance"})
        self.assertEqual(payload2["assigned_resource"], "AMB-22")

        # 无关岗位（ marshal ）看不到身份与转运信息
        status, view = call(app, "GET", f"/v1/medical/cases/{case_id}",
                            role="marshal")
        self.assertNotIn("bib", view["case"])
        self.assertNotIn("hospital_id", view["case"])
        self.assertNotIn("resource_id", view["case"])
        # 医疗角色可见完整信息
        status, view = call(app, "GET", f"/v1/medical/cases/{case_id}",
                            role="medical")
        self.assertEqual(view["case"]["bib"], "B1")
        self.assertEqual(view["case"]["resource_id"], "AMB-21")

        # 转运到最近且有床位的定点医院 HOSP-07
        status, payload = call(app, "POST",
                               f"/v1/medical/cases/{case_id}/transport",
                               role="dispatch", body={})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["case"]["hospital_id"], "HOSP-07")
        self.assertEqual(payload["case"]["status"], "transporting")

        # 资源台账：救护车被占用、医院床位 -1；awards 角色无权查看
        status, payload = call(app, "GET", "/v1/medical/resources",
                               role="awards")
        self.assertEqual(status, 403)
        status, payload = call(app, "GET", "/v1/medical/resources",
                               role="dispatch")
        resources = {r["resource_id"]: r for r in payload["resources"]}
        self.assertEqual(resources["AMB-21"]["status"], "locked")
        self.assertEqual(resources["AMB-21"]["case_id"], case_id)
        self.assertEqual(resources["HOSP-07"]["occupied"], 1)

        # 病例关闭后资源全部释放
        status, payload = call(app, "POST",
                               f"/v1/medical/cases/{case_id}/close",
                               role="medical",
                               body={"outcome": "stable"})
        self.assertEqual(payload["case"]["status"], "closed")
        status, payload = call(app, "GET", "/v1/medical/resources",
                               role="dispatch")
        resources = {r["resource_id"]: r for r in payload["resources"]}
        self.assertEqual(resources["AMB-21"]["status"], "available")
        self.assertIsNone(resources["AMB-21"]["case_id"])
        self.assertEqual(resources["HOSP-07"]["occupied"], 0)

    def test_trace_links_medical_occupancy(self):
        app = make_app()
        enter_athlete(app, "A1", "B1", 1, net=7200)
        call(app, "POST", "/v1/medical/requests", role="marshal",
             body={"location_km": 20.0, "category": "heat", "bib": "B1"})
        call(app, "POST", "/v1/medical/cases/MC-0001/transport",
             role="dispatch", body={})
        # admin 追溯：完赛 -> 奖励依据 -> 救治资源占用全链路
        status, trace = call(app, "GET", "/v1/finishers/B1/trace",
                             role="admin")
        self.assertEqual(status, 200)
        case = trace["medical"]["cases"][0]
        self.assertEqual(case["resource_id"], "AMB-21")
        self.assertEqual(case["hospital_id"], "HOSP-07")
        # awards 角色追溯同一号码布：转运目的地被脱敏
        status, trace = call(app, "GET", "/v1/finishers/B1/trace",
                             role="awards")
        case = trace["medical"]["cases"][0]
        self.assertNotIn("hospital_id", case)
        self.assertNotIn("bib", case)


class RbacTest(unittest.TestCase):
    """角色门禁。"""

    def test_role_enforcement(self):
        app = make_app()
        status, _ = call(app, "POST", "/v1/events", role="public",
                         body={"events": []})
        self.assertEqual(status, 403)
        status, _ = call(app, "GET", "/v1/results", role="public",
                         query={"distance": "marathon", "gender": "male"})
        self.assertEqual(status, 403)
        status, payload = call(app, "GET", "/v1/results", role="hacker",
                               query={"distance": "marathon",
                                      "gender": "male"})
        self.assertEqual(status, 401)
        status, payload = call(app, "GET", "/health", role="public")
        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], "marathon-adjudication")

    def test_token_mode(self):
        fixture = load_fixture()
        app = App(default_rules(fixture), medical_cfg=fixture["medical"],
                  role_tokens={"tok-admin": "admin", "tok-med": "medical"})
        status, _ = app.handle("GET", "/v1/medical/resources", {},
                               {"Authorization": "Bearer tok-med"}, None)
        self.assertEqual(status, 200)
        status, _ = app.handle("GET", "/v1/medical/resources", {},
                               {"X-Role": "medical"}, None)
        self.assertEqual(status, 401)  # 令牌模式下 X-Role 不再生效


class HttpSmokeTest(unittest.TestCase):
    """真实 HTTP 往返。"""

    def test_http_roundtrip(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from adjudication.api import make_handler

        server = ThreadingHTTPServer(("127.0.0.1", 0),
                                     make_handler(make_app()))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health") as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(payload["service"], "marathon-adjudication")

            body = json.dumps({"events": [ev_registration(
                "A1", "tpl-A1")]}).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/events", data=body,
                headers={"Content-Type": "application/json",
                         "X-Role": "checkin"})
            with urllib.request.urlopen(req) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(payload["summary"]["accepted"], 1)
        finally:
            server.shutdown()
            server.server_close()


class UnitTest(unittest.TestCase):
    """基础组件行为。"""

    def test_duration_roundtrip(self):
        self.assertEqual(parse_duration("02:08:30.500"),
                         timedelta(hours=2, minutes=8, seconds=30.5))
        self.assertEqual(format_duration(parse_duration("02:08:30.500")),
                         "02:08:30.500")
        self.assertEqual(parse_duration("59:59"), timedelta(minutes=59,
                                                            seconds=59))

    def test_rules_validation(self):
        fixture = load_fixture()
        doc = default_rules(fixture).doc
        doc.pop("appeal_window_hours")
        with self.assertRaises(RulesError):
            Rules(doc)

    def test_journal_rejects_unknown_type(self):
        journal = Journal()
        receipts = journal.append([{"event_id": "x", "type": "nope",
                                    "occurred_at": "2026-09-20T00:00:00+08:00"}])
        self.assertEqual(receipts[0]["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
