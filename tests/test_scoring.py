"""成绩、并列名次、跨枪与改枪、裁决冲销的判定测试。"""

import pytest

from app.scoring import (compute_awards, compute_rankings, effective_wave_at,
                         evaluate_bib)


@pytest.fixture
def state(seeded):
    return seeded.state


def bulletin(state):
    return state.latest_bulletin()


def test_gun_and_net_times(state):
    res = evaluate_bib(state, "A001", bulletin(state))
    # W1 07:30 发令，起点 07:30:05，终点 09:35:10
    assert res["official"]["gun_seconds"] == pytest.approx(2 * 3600 + 5 * 60 + 10)
    assert res["official"]["net_seconds"] == pytest.approx(2 * 3600 + 5 * 60 + 5)
    assert res["disqualified"] is False


def test_tied_runners_share_place_competition_ranking(state):
    rows = {r["bib"]: r for r in
            compute_rankings(state, bulletin(state), "marathon", gender="M")}
    assert rows["A002"]["overall_place"] == 2
    assert rows["A003"]["overall_place"] == 2
    # 并列两人占两名次，下一名为 4（竞赛排名 1,1,3 风格的 2,2,4）
    assert rows["A005"]["overall_place"] == 4


def test_cross_wave_dq_then_reinstatement_and_correction(state):
    # 改枪在 06:50 生效：起跑时刻有效分区为 W1，却踩了 W2 地毯
    assert effective_wave_at(
        state, "A005",
        __import__("app.timeutil", fromlist=["parse_iso"]).parse_iso(
            "2026-09-20T07:45:02+08:00").timestamp()) == "W1"
    res = evaluate_bib(state, "A005", bulletin(state))
    # 旧 DQ 已被 RUL-A005-02 冲销，成绩由 RUL-A005-03 更正
    assert res["disqualified"] is False
    assert "RUL-A005-01" in res["voided_ruling_ids"]
    assert res["official"]["gun_seconds"] == 9360
    actions = {r["event_id"]: r["action"] for r in res["rulings"]}
    assert actions == {"RUL-A005-02": "reinstate",
                       "RUL-A005-03": "correction"}


def test_as_of_before_reinstatement_still_dq(state):
    from app.timeutil import parse_iso
    # 仅看 10:31（旧 DQ 之后、冲销之前）的状态
    as_of = parse_iso("2026-09-20T10:31:00+08:00").timestamp()
    res = evaluate_bib(state, "A005", bulletin(state), as_of_ts=as_of)
    assert res["disqualified"] is True


def test_duplicate_finish_counts_once(state):
    # 给 A001 再补一条更晚的终点读数：成绩仍按首次过线
    from app.timeutil import parse_iso
    state.timing["A001"].append({
        "bib": "A001", "point_id": "FIN", "km": 42.195,
        "observed_at": "2026-09-20T09:40:00+08:00",
        "ts": parse_iso("2026-09-20T09:40:00+08:00").timestamp(),
        "event_id": "EXTRA-FIN"})
    res = evaluate_bib(state, "A001", bulletin(state))
    assert res["official"]["gun_seconds"] == pytest.approx(7510)
    assert any(i["code"] == "duplicate_finish" for i in res["irregularities"])


def test_domestic_eligibility_excludes_foreign_runner(state):
    rows = {r["bib"]: r for r in
            compute_rankings(state, bulletin(state), "marathon", gender="M")}
    assert rows["A005"]["domestic_place"] is None     # SGP 不参与中国籍奖
    assert rows["A005"]["overall_place"] == 4


def test_course_record_awarded_against_bulletin(state):
    awards = [a for a in compute_awards(state, bulletin(state), "marathon")
              if a["award"] == "course_record"]
    assert [a["bib"] for a in awards] == ["A004"]
    # 女子纪录 8400s，实际 8280s，依据可追溯
    item = awards[0]
    assert item["record_seconds"] == 8400
    assert item["basis"]["bulletin_version"] == 1
    assert item["basis"]["gun_event_id"]


def test_award_scope_respects_bulletin_version(state):
    # v2 把奖励范围缩小到前 50 名；A005 第 4 名在两版都在榜，边界选手仅在 v1
    v2 = {**state.bulletins[1], "version": 2,
          "awards": {**state.bulletins[1]["awards"], "overall_places": 1}}
    state.bulletins[2] = v2
    state.bulletin_order.append(2)
    v1_awards = {a["bib"] for a in compute_awards(state, state.bulletins[1], "marathon")
                 if a["award"] == "overall_place"}
    v2_awards = {a["bib"] for a in compute_awards(state, state.bulletins[2], "marathon")
                 if a["award"] == "overall_place"}
    assert "A005" in v1_awards
    assert "A005" not in v2_awards      # 男子第 4 名掉出“仅第 1 名”的新版范围
