"""脱敏求助、最近可用资源锁定、转运按岗可见。"""

import pytest

from app.medical import (case_view, is_available, nearest_available,
                         resource_occupancy, role_can_see_case)
from app.timeutil import parse_iso


def test_nearest_available_skips_occupied_and_picks_closest(state):
    at = parse_iso("2026-09-20T08:19:30+08:00").timestamp()
    # AMB-07 在 12.5km 离 15.1km 更近，但被 CASE-OTHER 占用，必须跳过；
    # AMB-03 在 16.0km 是最近的可用救护车
    picked = nearest_available(state, "ambulance", {"km": 15.1}, at)
    assert picked[0] == "AMB-03"
    assert picked[1] == pytest.approx(0.9, abs=0.05)
    assert is_available(state, "AMB-07", at) is False
    assert is_available(state, "AMB-03", at) is True


def test_resource_released_after_transport(state):
    # 08:53 之后 AMB-03 与 STA-14 均已释放，重新可用
    at = parse_iso("2026-09-20T09:00:00+08:00").timestamp()
    assert is_available(state, "AMB-03", at) is True
    assert is_available(state, "STA-14", at) is True


def test_occupancy_timeline(state):
    occ = {r["resource_id"]: r for r in resource_occupancy(state)}
    intervals = occ["AMB-03"]["intervals"]
    assert len(intervals) == 1
    iv = intervals[0]
    assert iv["case_id"] == "CASE-7001" and iv["active"] is False
    assert iv["released_at"] is not None


def test_case_is_pseudonymized_for_field_roles(state):
    station = case_view(state, "CASE-7001", "station:STA-14")
    assert station["runner_pseudonym"].startswith("R-")
    assert "bib_link" not in station                 # 站点拿不到号码布关联
    assert "name" not in station
    commander = case_view(state, "CASE-7001", "medical_commander")
    assert commander["bib_link"] == "A007"           # 总指挥可见关联


def test_unrelated_role_cannot_see_case_or_transport(state):
    assert role_can_see_case("station:STA-16", state, "CASE-7001") is False
    assert case_view(state, "CASE-7001", "station:STA-16") is None
    # 当事救护车能看到转运，未参与的救护车看不到
    amb03 = case_view(state, "CASE-7001", "ambulance:AMB-03")
    amb07 = case_view(state, "CASE-7001", "ambulance:AMB-07")
    assert any(t["kind"] == "assigned" for t in amb03["transports"])
    assert amb07 is None


def test_hospital_sees_transport_but_station_does_not(state):
    hospital = case_view(state, "CASE-7001", "hospital:HOSP-CENTER")
    assert hospital is not None
    assert hospital["transports"]                    # 接收医院可见转运
    station = case_view(state, "CASE-7001", "station:STA-14")
    assert station["transports"] == []               # 站点不泄露转运细节
