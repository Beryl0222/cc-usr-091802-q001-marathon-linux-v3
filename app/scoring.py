"""成绩、违规、名次与奖励判定。

判定全部是纯函数：输入 :class:`app.state.State` 与某一版赛事公报，输出
可解释的结果。每次调用都重新归约，因此事件补传/重放/乱序只会得到同一个
结论；裁决以“新裁决冲销旧裁决”的方式生效，历史链条仍然保留。

名次并列规则取公报 ``tie_policy``：默认竞赛排名（1,1,3），同一计时精度
（公报 ``time_precision``，默认整秒）内即并列。
"""

from .timeutil import parse_iso


# --------------------------------------------------------------------------
# 基础归约
# --------------------------------------------------------------------------

def effective_wave_at(state, bib, at_ts):
    """某时刻该号码布生效的分区：以发生在该时刻之前（含）的最后一次改枪为准。"""
    runner = state.runners.get(bib)
    wave = runner["wave_id"] if runner else None
    for change in sorted(state.reassignments.get(bib, []), key=lambda r: r["ts"]):
        if change["ts"] <= at_ts:
            wave = change["to_wave"]
        else:
            break
    return wave


def _gun_ts(state, wave_id):
    fires = state.guns.get(wave_id, [])
    if not fires:
        return None
    return min(fires, key=lambda g: g["ts"])["ts"]


def active_rulings(state, bib):
    """返回 ``(有效裁决列表, 被冲销裁决集合)``。

    新裁决可在 ``supersedes_ruling_ids`` 中点名冲销旧裁决；被冲销的裁决
    不再参与判定，但仍保留在历史中并标注 voided。
    """
    rulings = sorted(state.rulings.get(bib, []), key=lambda r: (r["ts"], r["event_id"]))
    voided = set()
    for ruling in rulings:
        for old_id in ruling.get("supersedes_ruling_ids", []) or []:
            voided.add(old_id)
    alive = [r for r in rulings if r["event_id"] not in voided]
    return alive, voided


def _face_chain(state, bib):
    """报名底库 -> 领物 -> 检录 -> 完赛的人脸证据，只含模板引用与结论。"""
    bucket = state.faces.get(bib)
    chain = {"template_ref": bucket.get("template_ref") if bucket else None, "steps": []}
    if not bucket:
        return chain
    for ctx in ("pickup", "checkin", "finish"):
        attempts = sorted(bucket["by_context"].get(ctx, []),
                          key=lambda a: (a["ts"], a["event_id"]))
        chain["steps"].append({
            "context": ctx,
            "attempts": [{"result": a["result"], "score": a.get("score"),
                          "template_ref": a.get("template_ref"),
                          "probe_ref": a.get("probe_ref"),
                          "device_id": a.get("device_id"),
                          "occurred_at": a["occurred_at"],
                          "event_id": a["event_id"]} for a in attempts],
        })
    return chain


def _irregularity(code, severity, message, evidence):
    return {"code": code, "severity": severity, "message": message,
            "evidence": list(evidence)}


# --------------------------------------------------------------------------
# 单人判定
# --------------------------------------------------------------------------

def evaluate_bib(state, bib, bulletin, as_of_ts=None):
    runner = state.runners.get(bib)
    rules = bulletin.get("rules", {})
    reads = [r for r in state.timing_sorted(bib)
             if as_of_ts is None or r["ts"] <= as_of_ts]

    result = {
        "bib": bib,
        "registered": runner is not None,
        "distance": runner.get("distance") if runner else None,
        "gender": runner.get("gender") if runner else None,
        "nationality": runner.get("nationality") if runner else None,
        "name": runner.get("name") if runner else None,
        "wave_id_registered": runner.get("wave_id") if runner else None,
        "irregularities": [],
        "disqualified": False,
        "dq_reasons": [],
        "evidence": [],
        "raw": {},
        "official": {},
        "face_chain": _face_chain(state, bib),
        "status": "dnf",
    }
    if runner:
        result["evidence"].append(runner["registration_event_id"])

    def dq(code, message, evidence):
        result["disqualified"] = True
        result["dq_reasons"].append(code)
        result["irregularities"].append(_irregularity(code, "dq", message, evidence))

    # -- 起点 / 跨枪 -------------------------------------------------------
    start_reads = [r for r in reads if state.start_point_wave(r["point_id"])]
    finish_reads = [r for r in reads if state.is_finish(r["point_id"])]
    start_read = None
    cross_wave = False
    for read in start_reads:
        mat_wave = state.start_point_wave(read["point_id"])
        owner_wave = effective_wave_at(state, bib, read["ts"])
        if mat_wave == owner_wave:
            start_read = start_read or read
        else:
            cross_wave = True
    if start_read is None and start_reads:
        # 所有起点读数都与当时生效分区不符：仍取最早一条用于呈现，但判定跨枪
        start_read = start_reads[0]
    if len({r["point_id"] for r in start_reads}) > 1 or cross_wave:
        msg = "计时点所在枪组与选手当时生效分区不一致（跨枪计时）"
        if rules.get("cross_wave_start", "dq") == "dq":
            dq("cross_wave_start", msg, [r["event_id"] for r in start_reads])
        else:
            result["irregularities"].append(_irregularity(
                "cross_wave_start", "warning", msg,
                [r["event_id"] for r in start_reads]))
    if start_read is not None:
        result["evidence"].append(start_read["event_id"])
        result["start_event_id"] = start_read["event_id"]

    effective_wave = (effective_wave_at(state, bib, start_read["ts"])
                      if start_read else (runner.get("wave_id") if runner else None))
    result["wave_id_effective"] = effective_wave
    for change in state.reassignments.get(bib, []):
        if as_of_ts is None or change["ts"] <= as_of_ts:
            result["evidence"].append(change["event_id"])

    # -- 重复过线：取第一次有效冲线 ----------------------------------------
    finish_read = finish_reads[0] if finish_reads else None
    if len(finish_reads) > 1:
        result["irregularities"].append(_irregularity(
            "duplicate_finish", "warning",
            f"终点出现 {len(finish_reads)} 次读数，仅首次过线计入成绩",
            [r["event_id"] for r in finish_reads]))
    if finish_read is not None:
        result["evidence"].append(finish_read["event_id"])
        result["finish_event_id"] = finish_read["event_id"]

    # -- 身份链：报名 / 领物 / 检录 / 完赛人脸 ------------------------------
    if runner is None:
        dq("unregistered_bib", "号码布无报名记录", [])
    result["status"] = "finished" if finish_read is not None else "dnf"
    _evaluate_identity(state, bib, rules, result, dq, as_of_ts)

    # -- 计时完整性 --------------------------------------------------------
    required = list(rules.get("required_points", {}).get(
        result["distance"] or "", []))
    present = {r["point_id"]: r for r in reads}
    if finish_read is not None:
        missing = [pid for pid in required if pid not in present]
        if missing:
            dq("missing_checkpoint", f"缺少必经计时点: {','.join(missing)}",
               [present[pid]["event_id"] for pid in required if pid in present])
    if start_read is None:
        dq("missing_start", "缺少起点计时地毯读数",
           [r["event_id"] for r in reads])
    if finish_read is None:
        # 未完赛不叫 DQ，单独状态（身份评估前已置 dnf）
        pass
    else:
        result["status"] = "finished"

    # 顺序合理性：计时点必须随公里数单调递增
    seq = [r for r in reads if r.get("km") is not None]
    seq.sort(key=lambda r: (r["km"], r["ts"]))
    last_ts_by_km = {}
    for read in reads:
        km = read.get("km")
        if km is None:
            continue
        if km in last_ts_by_km and abs(read["ts"] - last_ts_by_km[km]) > 1:
            result["irregularities"].append(_irregularity(
                "duplicate_split", "warning",
                f"计时点 {read['point_id']} 重复读数",
                [read["event_id"]]))
        last_ts_by_km[km] = read["ts"]
    ordered = sorted((r for r in reads if r.get("km") is not None),
                     key=lambda r: r["km"])
    for prev, curr in zip(ordered, ordered[1:]):
        if curr["ts"] < prev["ts"]:
            dq("impossible_sequence",
                f"计时点 {curr['point_id']} 早于 {prev['point_id']}，顺序不可能",
                [prev["event_id"], curr["event_id"]])
            break

    # -- 原始成绩 ----------------------------------------------------------
    raw_net = raw_gun = None
    gun_ts = _gun_ts(state, effective_wave) if effective_wave else None
    if gun_ts is not None:
        result["gun_event_id"] = next(
            (g["event_id"] for g in state.guns.get(effective_wave, [])
             if g["ts"] == gun_ts), None)
    if start_read and finish_read:
        raw_net = finish_read["ts"] - start_read["ts"]
        if raw_net < 0:
            dq("negative_time", "终点读数早于起点读数",
                [start_read["event_id"], finish_read["event_id"]])
    if gun_ts is not None and finish_read is not None:
        raw_gun = finish_read["ts"] - gun_ts
        if raw_gun < 0:
            dq("finish_before_gun", "冲线早于所属枪组发令",
                [result.get("gun_event_id"), finish_read["event_id"]])
    result["raw"] = {"net_seconds": raw_net, "gun_seconds": raw_gun}

    # -- 裁决（含冲销链）----------------------------------------------------
    alive, voided = active_rulings(state, bib)
    penalty = 0
    net_override = gun_override = None
    ruling_history = []
    for ruling in alive:
        if as_of_ts is not None and ruling["ts"] > as_of_ts:
            continue
        entry = {"event_id": ruling["event_id"], "action": ruling["action"],
                 "reason": ruling.get("reason"),
                 "occurred_at": ruling["occurred_at"], "voided": False}
        action = ruling["action"]
        if action == "penalty":
            penalty += int(ruling.get("seconds", 0))
            entry["seconds"] = int(ruling.get("seconds", 0))
        elif action == "dq":
            result["disqualified"] = True
            result["dq_reasons"].append(f"ruling:{ruling.get('reason', 'dq')}")
            result["irregularities"].append(_irregularity(
                "official_dq", "dq",
                f"赛务裁决取消成绩: {ruling.get('reason', '')}",
                [ruling["event_id"]]))
        elif action == "reinstate":
            # 申诉成立/录像复核：新裁决在 clears_codes 中点名冲销旧判罚
            # （自动判罚代码或 official_dq）。被冲销项移出结论，历史仍保留。
            clears = ruling.get("clears_codes") or []
            entry["clears"] = clears
            result["irregularities"] = [
                irr for irr in result["irregularities"] if irr["code"] not in clears]
            result["dq_reasons"] = [c for c in result["dq_reasons"] if c not in clears]
        elif action == "correction":
            if ruling.get("net_seconds") is not None:
                net_override = float(ruling["net_seconds"])
            if ruling.get("gun_seconds") is not None:
                gun_override = float(ruling["gun_seconds"])
            entry["net_seconds"] = ruling.get("net_seconds")
            entry["gun_seconds"] = ruling.get("gun_seconds")
        ruling_history.append(entry)
        result["evidence"].append(ruling["event_id"])
    result["rulings"] = ruling_history
    # 最终是否有效：冲销链处理完后，仍有 dq 级判罚即为取消成绩
    result["disqualified"] = any(
        irr["severity"] == "dq" for irr in result["irregularities"])
    result["voided_ruling_ids"] = sorted(voided)

    net = net_override if net_override is not None else (
        raw_net + penalty if raw_net is not None else None)
    gun = gun_override if gun_override is not None else (
        raw_gun + penalty if raw_gun is not None else None)
    result["penalty_seconds"] = penalty
    result["official"] = {"net_seconds": net, "gun_seconds": gun}

    # 申诉引用
    result["appeals"] = [a["filed"]["appeal_id"] for a in state.appeals.get(bib, [])]
    return result


def _evaluate_identity(state, bib, rules, result, dq, as_of_ts):
    def attempts(ctx):
        bucket = state.faces.get(bib)
        if not bucket:
            return []
        items = bucket["by_context"].get(ctx, [])
        if as_of_ts is not None:
            items = [a for a in items if a["ts"] <= as_of_ts]
        return sorted(items, key=lambda a: (a["ts"], a["event_id"]))

    pickup = state.pickups.get(bib)
    if as_of_ts is not None and pickup and pickup["ts"] > as_of_ts:
        pickup = None
    required_face = rules.get("require_face", ["pickup", "checkin", "finish"])

    if pickup is None:
        dq("no_bib_pickup", "无号码布领取记录", [])
    else:
        result["evidence"].append(pickup["event_id"])

    for ctx, label, err_code in (
            ("pickup", "领物", "pickup_face_failed"),
            ("checkin", "检录", "checkin_face_failed"),
            ("finish", "完赛", "finish_face_failed")):
        attempts_list = attempts(ctx)
        passed = any(a["result"] == "pass" for a in attempts_list)
        failed = any(a["result"] == "fail" for a in attempts_list)
        if failed and not passed:
            dq(err_code, f"{label}环节人脸比对未通过",
               [a["event_id"] for a in attempts_list])
        elif not attempts_list and ctx in required_face:
            # 应做而未做人脸核验；未完赛者的完赛环节由 DNF 体现，不重复罚
            if ctx != "finish" or result["status"] == "finished":
                dq(f"missing_face_{ctx}", f"缺少{label}人脸核验记录", [])

    # 检录必须落在本人当时生效的分区
    checkins = [c for c in state.checkins.get(bib, [])
                if as_of_ts is None or c["ts"] <= as_of_ts]
    if not checkins:
        dq("no_checkin", "无分区检录记录", [])
    else:
        result["evidence"].append(checkins[-1]["event_id"])
        valid_zone = any(
            c.get("zone_wave_id") == effective_wave_at(state, bib, c["ts"])
            for c in checkins)
        if not valid_zone:
            dq("wrong_checkin_zone", "检录分区与生效枪组不一致",
               [c["event_id"] for c in checkins])

    # 同一抓拍模板在两个不同号码布下通过 -> 替跑/冒用的强信号
    shared = _shared_probe_bibs(state)
    if bib in shared:
        others, probe_refs = shared[bib]
        dq("shared_face_probe",
            f"同一人脸模板出现在号码布 {','.join(sorted(others))} 的核验中",
            [])


def _shared_probe_bibs(state):
    """probe_ref -> set(bib)，返回 bib -> (其他 bib, probe_refs)。"""
    seen = {}
    for bib, bucket in state.faces.items():
        for ctx, attempts in bucket["by_context"].items():
            if ctx == "finish":
                continue
            for a in attempts:
                if a["result"] == "pass" and a.get("probe_ref"):
                    seen.setdefault(a["probe_ref"], set()).add(bib)
    out = {}
    for probe_ref, bibs in seen.items():
        if len(bibs) > 1:
            for bib in bibs:
                others = bibs - {bib}
                slot = out.setdefault(bib, (set(), []))
                slot[0].update(others)
                slot[1].append(probe_ref)
    return out


# --------------------------------------------------------------------------
# 排名与奖励
# --------------------------------------------------------------------------

def _quantize(seconds, precision):
    return int(round(seconds * (10 ** precision)))


def finishers(state, bulletin, as_of_ts=None, distance=None):
    rows = []
    for bib in state.runners:
        res = evaluate_bib(state, bib, bulletin, as_of_ts)
        if res["status"] != "finished":
            continue
        if distance and res["distance"] != distance:
            continue
        rows.append(res)
    return rows


def _rank(rows, key_fn, precision, tie_policy):
    ranked = sorted(rows, key=key_fn)
    last_key = None
    place = dense = 0
    for idx, row in enumerate(ranked, start=1):
        key = key_fn(row)
        if key != last_key:
            dense += 1
            place = idx if tie_policy == "competition" else dense
            last_key = key
        row["_tie_group_key"] = key
        yield place, row


def compute_rankings(state, bulletin, distance, as_of_ts=None, gender=None):
    """排名按 (距离, 性别) 分组：总名次奖与中国籍奖男女分别取名次。

    传 ``gender`` 只返回该性别；不传则对每个性别分组排名后合并，
    每行的 ``overall_place``/``domestic_place`` 都是其性别组内名次。
    """
    rules = bulletin.get("rules", {})
    basis = rules.get("ranking_basis", "gun")
    precision = int(rules.get("time_precision", 0))
    tie_policy = rules.get("tie_policy", "competition")
    rows = [r for r in finishers(state, bulletin, as_of_ts, distance)
            if not r["disqualified"]
            and r["official"].get(f"{basis}_seconds") is not None
            and (gender is None or r["gender"] == gender)]

    def key_fn(row):
        return _quantize(row["official"][f"{basis}_seconds"], precision)

    domestic_code = bulletin.get("domestic_nationality", "CHN")
    out = []
    for gender_key in sorted({r["gender"] for r in rows}):
        group = [r for r in rows if r["gender"] == gender_key]
        ranked = list(_rank(group, key_fn, precision, tie_policy))
        for _, row in ranked:
            row["domestic_place"] = None     # 外籍选手不参与中国籍排名
        for place, row in ranked:
            row["overall_place"] = place
        domestic_rows = [r for _, r in ranked
                         if r["nationality"] == domestic_code]
        for place, row in _rank(domestic_rows, key_fn, precision, tie_policy):
            row["domestic_place"] = place
        out.extend(r for _, r in ranked)
    out.sort(key=lambda r: (r["gender"], r["overall_place"], r["bib"]))
    return out


def compute_awards(state, bulletin, distance, as_of_ts=None):
    """组装奖励结论与依据；奖金范围、国籍资格、纪录全部按该版公报。"""
    rules = bulletin.get("rules", {})
    basis = rules.get("ranking_basis", "gun")
    ranked = compute_rankings(state, bulletin, distance, as_of_ts)
    overall_n = int(bulletin.get("awards", {}).get("overall_places", 0))
    domestic_n = int(bulletin.get("awards", {}).get("domestic_places", 0))
    schedule = bulletin.get("awards", {}).get("place_prize", {})
    records = bulletin.get("records", {}).get(distance, {})
    awards = []

    for row in ranked:
        earned = []
        if row.get("overall_place") and row["overall_place"] <= overall_n:
            place = row["overall_place"]
            earned.append({
                "award": "overall_place",
                "place": place,
                "prize": schedule.get(str(place)) or schedule.get("default"),
                "basis": _award_basis(row, bulletin, basis, label="总名次"),
            })
        if row.get("domestic_place") and row["domestic_place"] <= domestic_n:
            place = row["domestic_place"]
            d_schedule = bulletin.get("awards", {}).get("domestic_place_prize", {})
            earned.append({
                "award": "domestic_place",
                "place": place,
                "prize": d_schedule.get(str(place)) or d_schedule.get("default"),
                "qualifying_nationality": bulletin.get("domestic_nationality", "CHN"),
                "basis": _award_basis(row, bulletin, basis, label="中国籍特别奖"),
            })
        rec = records.get(row.get("gender"))
        seconds = row["official"].get(f"{basis}_seconds")
        if rec and seconds is not None and seconds < float(rec["seconds"]):
            earned.append({
                "award": "course_record",
                "record_seconds": float(rec["seconds"]),
                "actual_seconds": _quantize(seconds, int(rules.get("time_precision", 0)))
                / (10 ** int(rules.get("time_precision", 0))),
                "prize": bulletin.get("awards", {}).get("record_prize"),
                "basis": _award_basis(row, bulletin, basis, label="破赛会纪录"),
            })
        for item in earned:
            awards.append({"bib": row["bib"], "distance": distance, **item})
    return awards


def _award_basis(row, bulletin, basis, label):
    """每项奖励都能逐事件追溯。"""
    return {
        "label": label,
        "bulletin_version": bulletin["version"],
        "ranking_basis": basis,
        "official_seconds": row["official"].get(f"{basis}_seconds"),
        "start_event_id": row.get("start_event_id"),
        "finish_event_id": row.get("finish_event_id"),
        "gun_event_id": row.get("gun_event_id"),
        "ruling_event_ids": [r["event_id"] for r in row["rulings"]],
        "face_event_ids": [a["event_id"] for step in row["face_chain"]["steps"]
                           for a in step["attempts"]],
        "appeal_ids": row.get("appeals", []),
    }
