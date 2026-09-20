"""赛事演练事件流。

用已有赛程、计时与救治事件重演四类典型情形，供赛务组端到端核对：

- ``imposter``：冒名入场——替跑者用本人抓拍模板通过号码布 A 的领物/检录，
  同一张抓拍出现在另一号码布 B 下，触发 ``shared_face_probe`` 且完赛人脸失败；
- ``tie``：并列成绩——两名中国籍选手整秒成绩相同，竞赛排名并列（1,1,3）；
- ``cross_wave``：临时改枪后仍在原分区起点地毯计时——跨枪计时被检出；
  随后一条新裁决（成绩更正）冲销旧判罚，演示“新裁决冲销旧结果”；
- ``medical``：途中急救——脱敏求助、锁定最近医疗站与救护车、转运定点医院、
  释放资源，并验证无关岗位看不到案件。

所有时间以东八区上报（保留原始偏移），归约按 UTC 排序；设备 ``MAT-09``
在断网恢复后补传迟到的 15 公里读数，并重复上报同一 device_seq。
"""

# 比赛日 2026-09-20，发令四枪（每枪 15 分钟）
WAVES = [
    {"wave_id": "W1", "sequence": 1, "scheduled_start": "2026-09-20T07:30:00+08:00",
     "zone_id": "Z1"},
    {"wave_id": "W2", "sequence": 2, "scheduled_start": "2026-09-20T07:45:00+08:00",
     "zone_id": "Z2"},
    {"wave_id": "W3", "sequence": 3, "scheduled_start": "2026-09-20T08:00:00+08:00",
     "zone_id": "Z3"},
    {"wave_id": "W4", "sequence": 4, "scheduled_start": "2026-09-20T08:15:00+08:00",
     "zone_id": "Z4"},
]

# 计时点：四个分区起点 + 若干分段 + 终点
def _points():
    pts = []
    for w in WAVES:
        pts.append({"point_id": f"S-{w['wave_id']}", "kind": "start",
                    "wave_id": w["wave_id"], "km": 0.0})
    for km, pid in [(5, "P05"), (10, "P10"), (15, "P15"),
                    (21.1, "PHALF"), (30, "P30"), (35, "P35"), (40, "P40")]:
        pts.append({"point_id": pid, "kind": "split", "km": km})
    pts.append({"point_id": "FIN", "kind": "finish", "km": 42.195})
    return pts


BULLETIN_V1 = {
    "version": 1,
    "title": "太原马拉松竞赛规程（发布版 v1）",
    "domestic_nationality": "CHN",
    "rules": {
        "ranking_basis": "gun",
        "time_precision": 0,          # 整秒
        "tie_policy": "competition",  # 并列：1,1,3
        "appeal_window_hours": 24,
        "cross_wave_start": "dq",
        "require_face": ["pickup", "checkin", "finish"],
        "required_points": {
            "marathon": ["P10", "P15", "P30", "P40"],
            "half_marathon": ["P05", "P10", "P15"],
        },
    },
    "awards": {
        "overall_places": 100,
        "domestic_places": 8,
        "record_prize": 80000,
        "place_prize": {"1": 50000, "2": 30000, "3": 20000, "default": 5000},
        "domestic_place_prize": {"1": 20000, "2": 12000, "default": 3000},
    },
    "records": {
        "marathon": {
            "M": {"seconds": 7500, "holder": "赛会男子纪录"},
            "W": {"seconds": 8400, "holder": "赛会女子纪录"},
        },
    },
}

BULLETIN_V2 = {
    "version": 2,
    "title": "太原马拉松竞赛规程（发布版 v2，申诉期调整）",
    "domestic_nationality": "CHN",
    "rules": {**BULLETIN_V1["rules"], "appeal_window_hours": 48},
    "awards": BULLETIN_V1["awards"],
    "records": BULLETIN_V1["records"],
}


def _iso(h, m, s=0, day=20):
    return f"2026-09-{day:02d}T{h:02d}:{m:02d}:{s:02d}+08:00"


def _runner(bib, name, nationality, gender, wave, *, distance="marathon",
            template=None):
    return ("runner.registered", {
        "bib": bib, "name": name, "nationality": nationality,
        "gender": gender, "distance": distance, "wave_id": wave,
        "template_ref": template or f"TPL-{bib}",
    }, _iso(10, 0, day=1))


def _face(bib, ctx, result, when, probe, device="GATE-01", score=0.97):
    return ("face.verified", {
        "bib": bib, "context": ctx, "result": result, "score": score,
        "template_ref": f"TPL-{bib}", "probe_ref": probe,
    }, when, device)


def _pickup(bib, when, device="KIT-01"):
    return ("bib.picked", {"bib": bib}, when, device)


def _checkin(bib, zone_wave, when, device="GATE-01"):
    return ("checkin.recorded", {"bib": bib, "zone_wave_id": zone_wave},
            when, device)


def _read(bib, point, h, m, s=0, device="MAT-01", seq=None, when=None):
    observed = _iso(h, m, s)
    payload = {"bib": bib, "point_id": point, "observed_at": observed}
    return ("timing.recorded", payload, when or observed, device, seq)


# 同一块地垫会连续扫过所有选手，device_seq 在设备维度全局递增，
# 绝不能每个选手都从同一序号开始（否则会被幂等去重吞掉）
_SEQ_COUNTER = {"MAT-01": 5000, "MAT-04": 7000}


def _read_shared_mat(bib, point, h, m, s=0):
    seq = _SEQ_COUNTER["MAT-01"]
    _SEQ_COUNTER["MAT-01"] += 1
    return _read(bib, point, h, m, s, device="MAT-01", seq=seq)


def _reassign(bib, to_wave, when):
    return ("wave.reassigned", {"bib": bib, "from_wave": "W2",
                                "to_wave": to_wave, "reason": "伤病复核后调整"},
            when, "OPS-01")


def build_events():
    """返回 (event_type, payload, occurred_at[, device_id[, device_seq]]) 元组列表。"""
    ev = []

    ev.append(("event.configured", {
        "event_id": "ty-marathon-2026",
        "event_name": "2026 太原马拉松",
        "race_date": "2026-09-20",
        "waves": WAVES,
        "distances": ["marathon", "half_marathon"],
        "points": _points(),
        "entries": {"marathon": 18000, "half_marathon": 22000},
        "awards": {"overall_places": 100, "domestic_places": 8},
        "medical": {"stations": 40, "ambulances": 44, "aed": 100, "hospitals": 13},
    }, _iso(9, 0, day=1), "OPS-01"))

    ev.append(("bulletin.published", BULLETIN_V1, _iso(12, 0, day=1), "OPS-01"))

    # 选手：精英（破纪录）、并列甲乙、跨枪选手、冒名者与被冒名者、途中急救选手
    runners = [
        ("A001", "张磊", "CHN", "M", "W1", "TPL-A001"),
        ("A002", "李岩", "CHN", "M", "W1", "TPL-A002"),   # 与 A003 并列
        ("A003", "王强", "CHN", "M", "W1", "TPL-A003"),
        ("A004", "赵敏", "CHN", "W", "W1", "TPL-A004"),   # 女子破赛会纪录
        ("A005", "Chen Wei", "SGP", "M", "W2", "TPL-A005"),  # 改枪后跨枪
        ("A006", "孙伟", "CHN", "M", "W2", "TPL-A006"),   # 被冒名（本人未到场）
        ("A008", "替身", "CHN", "M", "W2", "TPL-A008"),   # 替跑者真身
        ("A007", "周航", "CHN", "M", "W1", "TPL-A007"),   # 途中急救
        ("H001", "吴琳", "CHN", "W", "W4", "TPL-H001"),   # 半程
    ]
    for bib, name, nat, gender, wave, tpl in runners:
        dist = "half_marathon" if bib.startswith("H") else "marathon"
        ev.append(_runner(bib, name, nat, gender, wave, distance=dist,
                          template=tpl))

    # 发令（四枪，每枪间隔 15 分钟：07:30/07:45/08:00/08:15）
    for idx, w in enumerate(WAVES):
        total = 7 * 60 + 30 + 15 * idx
        ev.append(("gun.fired", {"wave_id": w["wave_id"]},
                   _iso(total // 60, total % 60), "GUN"))

    # ---- 正常选手 A001：净/枪成绩，W1 07:30 发令 -------------------------
    _chain(ev, "A001", "W1", "PRB-A001",
           {"S-W1": (7, 30, 5), "P10": (8, 0, 3), "P15": (8, 15, 10),
            "P30": (8, 58, 0), "P40": (9, 27, 0), "FIN": (9, 35, 10)},
           finish_probe="PRB-A001-F")

    # ---- A002 / A003：枪成绩并列 2:10:00（7800 秒）------------------------
    _chain(ev, "A002", "W1", "PRB-A002",
           {"S-W1": (7, 30, 2), "P10": (8, 0, 0), "P15": (8, 15, 0),
            "P30": (8, 58, 0), "P40": (9, 39, 50), "FIN": (9, 40, 0)},
           finish_probe="PRB-A002-F")
    _chain(ev, "A003", "W1", "PRB-A003",
           {"S-W1": (7, 30, 9), "P10": (8, 0, 4), "P15": (8, 15, 6),
            "P30": (8, 58, 4), "P40": (9, 39, 55), "FIN": (9, 40, 0)},
           finish_probe="PRB-A003-F")

    # ---- A004：女子，枪成绩 2:18:00 = 8280 < 8400 破纪录 ------------------
    _chain(ev, "A004", "W1", "PRB-A004",
           {"S-W1": (7, 30, 3), "P10": (8, 3, 0), "P15": (8, 19, 0),
            "P30": (9, 6, 0), "P40": (9, 47, 0), "FIN": (9, 48, 0)},
           finish_probe="PRB-A004-F")

    # ---- A005：报名 W2，开赛前改到 W1，却仍踩 W2 起点地毯 -> 跨枪 ----------
    ev.append(_reassign("A005", "W1", _iso(6, 50)))
    ev.append(_pickup("A005", _iso(6, 55)))
    ev.append(_face("A005", "pickup", "pass", _iso(6, 55, 20), "PRB-A005"))
    ev.append(_checkin("A005", "W1", _iso(7, 10)))
    ev.append(_face("A005", "checkin", "pass", _iso(7, 10, 15), "PRB-A005"))
    # 起点踩错：W2 地毯（实际已属 W1）
    ev.append(_read("A005", "S-W2", 7, 45, 2, device="MAT-02", seq=41))
    ev.append(_read("A005", "P10", 8, 17, 0, device="MAT-02", seq=42))
    ev.append(_read("A005", "P15", 8, 32, 0, device="MAT-02", seq=43))
    ev.append(_read("A005", "P30", 9, 16, 0, device="MAT-02", seq=44))
    ev.append(_read("A005", "P40", 9, 58, 0, device="MAT-02", seq=45))
    ev.append(_read("A005", "FIN", 10, 6, 0, device="MAT-02", seq=46))
    ev.append(_face("A005", "finish", "pass", _iso(10, 6, 3), "PRB-A005-F",
                    device="FIN-01"))

    # ---- A006：本人未到场；替跑者 A008 用同一张抓拍 PRB-X 通过 A006 的
    # 领物/检录，同一张抓拍也出现在 A008 本人的检录中 -> 替跑强信号；
    # A006 终点人脸比对失败 ------------------------------------------------
    ev.append(_pickup("A006", _iso(7, 0)))
    ev.append(_face("A006", "pickup", "pass", _iso(7, 0, 10), "PRB-IMPOSTER"))
    ev.append(_checkin("A006", "W2", _iso(7, 20)))
    ev.append(_face("A006", "checkin", "pass", _iso(7, 20, 10), "PRB-IMPOSTER"))
    # 替跑者真身 A008：以本人号码布完成检录与全程，检录抓拍同属一人
    ev.append(_pickup("A008", _iso(7, 2)))
    ev.append(_face("A008", "pickup", "pass", _iso(7, 2, 10), "PRB-A008"))
    ev.append(_checkin("A008", "W2", _iso(7, 21)))
    ev.append(_face("A008", "checkin", "pass", _iso(7, 21, 5), "PRB-IMPOSTER",
                    device="GATE-02"))
    ev.append(_read("A006", "S-W2", 7, 45, 10, device="MAT-03", seq=101))
    ev.append(_read("A006", "P10", 8, 20, 0, device="MAT-03", seq=102))
    ev.append(_read("A006", "P15", 8, 35, 0, device="MAT-03", seq=103))
    ev.append(_read("A006", "P30", 9, 20, 0, device="MAT-03", seq=104))
    ev.append(_read("A006", "P40", 10, 0, 0, device="MAT-03", seq=105))
    ev.append(_read("A006", "FIN", 10, 9, 0, device="MAT-03", seq=106))
    ev.append(_face("A006", "finish", "fail", _iso(10, 9, 2), "PRB-IMPOSTER-F",
                    device="FIN-01", score=0.21))
    ev.append(_read("A008", "S-W2", 7, 45, 11, device="MAT-04", seq=201))
    ev.append(_read("A008", "P10", 8, 20, 5, device="MAT-04", seq=202))
    ev.append(_read("A008", "P15", 8, 35, 5, device="MAT-04", seq=203))
    ev.append(_read("A008", "P30", 9, 20, 5, device="MAT-04", seq=204))
    ev.append(_read("A008", "P40", 10, 0, 5, device="MAT-04", seq=205))
    ev.append(_read("A008", "FIN", 10, 9, 5, device="MAT-04", seq=206))
    ev.append(_face("A008", "finish", "pass", _iso(10, 9, 7), "PRB-A008-F",
                    device="FIN-01"))

    # ---- A007：W1 正常起跑，15 公里处急救（弃赛），设备断网补传 ------------
    _chain(ev, "A007", "W1", "PRB-A007",
           {"S-W1": (7, 30, 7), "P10": (8, 2, 0), "P15": (8, 18, 30)},
           finish=False)
    # 断网：MAT-09 的 15km 读数晚于医疗事件之后补传（设备时间仍是 08:18:30），
    # 同一 device_seq 重放两次
    delayed = _read("A007", "P15", 8, 18, 31, device="MAT-09", seq=9007,
                    when=_iso(8, 46, 0))
    ev.append(delayed)
    ev.append(delayed)  # 重放：去重后只保留一条

    # ---- H001 半程 --------------------------------------------------------
    ev.append(_pickup("H001", _iso(7, 20)))
    ev.append(_face("H001", "pickup", "pass", _iso(7, 20, 10), "PRB-H001"))
    ev.append(_checkin("H001", "W4", _iso(8, 0)))
    ev.append(_face("H001", "checkin", "pass", _iso(8, 0, 10), "PRB-H001"))
    ev.append(_read("H001", "S-W4", 8, 15, 4, device="MAT-40", seq=11))
    ev.append(_read("H001", "P05", 8, 32, 0, device="MAT-40", seq=12))
    ev.append(_read("H001", "P10", 8, 49, 0, device="MAT-40", seq=13))
    ev.append(_read("H001", "P15", 9, 7, 0, device="MAT-40", seq=14))
    ev.append(_read("H001", "PHALF", 9, 24, 0, device="MAT-40", seq=15))
    ev.append(_face("H001", "finish", "pass", _iso(9, 24, 2), "PRB-H001-F",
                    device="FIN-01"))

    # ---- 医疗资源与途中急救（A007，15 公里附近）----------------------------
    ev += _medical_events()

    # ---- 裁决：跨枪 DQ（旧裁决），随后被录像复核的新裁决冲销 ---------------
    # 复核确认 A005 改枪合规，W1 起点地垫漏扫：冲销旧判罚并更正成绩
    ev.append(_ruling("RUL-A005-01", "A005", "dq",
                      reason="临时改枪后仍通过原分区起点地毯，跨枪计时",
                      when=_iso(10, 30)))
    ev.append(_ruling("RUL-A005-02", "A005", "reinstate",
                      reason="录像复核：改枪合规，W1 起点地垫漏扫",
                      supersedes=["RUL-A005-01"],
                      clears=["cross_wave_start", "official_dq"],
                      when=_iso(11, 10)))
    ev.append(_ruling("RUL-A005-03", "A005", "correction",
                      reason="按 W1 发令与终点冲线核定",
                      net_seconds=9355, gun_seconds=9360,
                      when=_iso(11, 11)))

    return ev


def _ruling(rid, bib, action, *, reason, when, supersedes=None, clears=None,
            net_seconds=None, gun_seconds=None):
    payload = {"bib": bib, "action": action, "reason": reason}
    if supersedes:
        payload["supersedes_ruling_ids"] = supersedes
    if clears:
        payload["clears_codes"] = clears
    if net_seconds is not None:
        payload["net_seconds"] = net_seconds
    if gun_seconds is not None:
        payload["gun_seconds"] = gun_seconds
    return ("ruling.issued", payload, when, "JUDGE", None, rid)


def _chain(ev, bib, wave, probe, reads, *, finish_probe=None, finish=True):
    """生成领物/人脸/检录的标准身份链与计时读数。"""
    first = min(reads.values())
    ev.append(_pickup(bib, _iso(first[0] - 2, first[1])))
    ev.append(_face(bib, "pickup", "pass",
                    _iso(first[0] - 2, first[1], 20), probe))
    ev.append(_checkin(bib, wave, _iso(first[0] - 1, 20)))
    ev.append(_face(bib, "checkin", "pass",
                    _iso(first[0] - 1, 19, 40), probe))
    seq = 10
    for point, clock in reads.items():
        h, m, s = clock
        ev.append(_read_shared_mat(bib, point, h, m, s))
        seq += 1
    if finish:
        fclock = reads["FIN"]
        ev.append(_face(bib, "finish", "pass",
                        _iso(fclock[0], fclock[1], fclock[2] + 2),
                        finish_probe or probe + "-F", device="FIN-01"))


def _medical_events():
    ev = []
    # 沿赛道的两座医疗站（15km 附近）、两辆救护车、一所定点医院
    ev.append(("resource.registered", {
        "resource_id": "STA-14", "kind": "station",
        "name": "15 公里医疗站", "location": {"km": 15.0},
    }, _iso(6, 0), "MED-ADMIN"))
    ev.append(("resource.registered", {
        "resource_id": "STA-16", "kind": "station",
        "name": "17 公里医疗站", "location": {"km": 17.2},
    }, _iso(6, 0), "MED-ADMIN"))
    ev.append(("resource.registered", {
        "resource_id": "AMB-03", "kind": "ambulance",
        "name": "3 号救护车", "location": {"km": 16.0},
    }, _iso(6, 0), "MED-ADMIN"))
    ev.append(("resource.registered", {
        "resource_id": "AMB-07", "kind": "ambulance",
        "name": "7 号救护车", "location": {"km": 12.5},
    }, _iso(6, 0), "MED-ADMIN"))
    ev.append(("resource.registered", {
        "resource_id": "HOSP-CENTER", "kind": "hospital",
        "name": "市中心医院（定点）",
        "location": {"lat": 37.87, "lng": 112.55},
    }, _iso(6, 0), "MED-ADMIN"))
    # AMB-07 已被另一案件占用（锁而未释），最近可用选择应跳过它
    ev.append(("resource.locked", {"case_id": "CASE-OTHER",
                                   "resource_id": "AMB-07"},
               _iso(8, 0), "MED-CMD"))
    # A007 倒地：志愿者发起脱敏求助（不带姓名），位置 15.1km
    ev.append(("medical.sos", {
        "case_id": "CASE-7001", "severity": "red",
        "symptoms": ["collapse", "no_pulse"],
        "location": {"km": 15.1}, "bib_link": "A007",
    }, _iso(8, 19, 0), "MARSHAL-15"))
    # 锁定最近医疗站：STA-14（0.1km）
    ev.append(("resource.locked", {"case_id": "CASE-7001",
                                   "resource_id": "STA-14"},
               _iso(8, 19, 20), "MED-CMD"))
    # 锁定最近救护车：AMB-03（0.9km），AMB-07 被占用跳过
    ev.append(("resource.locked", {"case_id": "CASE-7001",
                                   "resource_id": "AMB-03"},
               _iso(8, 20, 0), "MED-CMD"))
    # 救护车行进位置上报（断网补传）
    ev.append(("ambulance.located", {
        "resource_id": "AMB-03", "location": {"km": 15.2},
    }, _iso(8, 24, 0), "AMB-03"))
    # 转运定点医院
    ev.append(("transport.assigned", {
        "case_id": "CASE-7001", "ambulance_id": "AMB-03",
        "hospital_id": "HOSP-CENTER",
    }, _iso(8, 26, 0), "MED-CMD"))
    # 医疗站就地释放；救护车到院后释放
    ev.append(("resource.released", {"case_id": "CASE-7001",
                                     "resource_id": "STA-14"},
               _iso(8, 26, 30), "MED-CMD"))
    ev.append(("transport.outcome", {
        "case_id": "CASE-7001", "outcome": "admitted",
        "hospital_id": "HOSP-CENTER",
    }, _iso(8, 52, 0), "HOSP-CENTER"))
    ev.append(("resource.released", {"case_id": "CASE-7001",
                                     "resource_id": "AMB-03"},
               _iso(8, 53, 0), "MED-CMD"))
    return ev


def load(app):
    """把演练事件直接灌入 ApiApp（绕过 HTTP 岗位校验），返回入库回执列表。"""
    receipts = []
    for item in build_events():
        etype, payload, occurred = item[0], item[1], item[2]
        device = item[3] if len(item) > 3 else None
        seq = item[4] if len(item) > 4 else None
        explicit_id = item[5] if len(item) > 5 else None
        event, duplicated = app.store.append(
            etype, payload, occurred, event_id=explicit_id,
            device_id=device, device_seq=seq)
        receipts.append({"event_id": event["event_id"],
                         "event_type": etype, "duplicated": duplicated})
    app._dirty = True
    return receipts
