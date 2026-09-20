"""公示榜单与申诉。

榜单是发布当时物化的**不可变快照**：行内容、所用公报版本、奖励结论都拷贝进
事件载荷并计算指纹；之后成绩更正不会改写旧快照，只能发布新榜单并在
``supersedes`` 中点名替代。旧榜单始终可按 list_id 取回，状态变为
``superseded``——“已公示榜单不能被静默覆盖”。

申诉期限按榜单钉住的公报版本计算（不同发布版本的期限可以不同），申诉
必须在期限内、针对当时榜单中的行提出；裁决与申诉决定共同构成可追溯的
申诉历史。
"""

import hashlib
import json

from .scoring import compute_awards, compute_rankings
from .timeutil import parse_iso


# 投影器/接口在快照事件之外附加的运行态元数据，不参与快照指纹
_PROJECTION_KEYS = ("event_id", "ts", "status", "superseded_by",
                    "fingerprint_verified")


def canonical_fingerprint(payload):
    """对榜单冻结内容计算稳定 SHA-256，供外部核对未被篡改。

    投影器附加的运行态元数据（激活/被替代状态、落库事件号等）不属于
    发布当时冻结的内容，计算指纹时剔除。
    """
    body = {k: v for k, v in payload.items()
            if k != "fingerprint" and k not in _PROJECTION_KEYS}
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_publication(state, *, list_id, kind, scope, distance, bulletin_version,
                      published_at, supersedes=None, note=None):
    """按指定公报版本物化榜单快照。

    用该版本公报在发布时刻重算排名与奖励，结论整体冻结进快照。
    """
    bulletin = state.bulletins.get(int(bulletin_version))
    if bulletin is None:
        raise ValueError(f"公报版本 {bulletin_version} 不存在")
    as_of = parse_iso(published_at).timestamp()
    rows = compute_rankings(state, bulletin, distance, as_of_ts=as_of)
    awards = compute_awards(state, bulletin, distance, as_of_ts=as_of)
    awards_by_bib = {}
    for item in awards:
        awards_by_bib.setdefault(item["bib"], []).append(item)

    entries = []
    for row in rows:
        basis = bulletin.get("rules", {}).get("ranking_basis", "gun")
        entries.append({
            "bib": row["bib"],
            "name": row["name"],
            "nationality": row["nationality"],
            "gender": row["gender"],
            "distance": row["distance"],
            "wave_id": row.get("wave_id_effective"),
            "overall_place": row.get("overall_place"),
            "domestic_place": row.get("domestic_place"),
            "official_net_seconds": row["official"].get("net_seconds"),
            "official_gun_seconds": row["official"].get("gun_seconds"),
            "irregularity_codes": sorted({i["code"] for i in row["irregularities"]}),
            "awards": [_strip_bib(a) for a in awards_by_bib.get(row["bib"], [])],
            "evidence": {
                "registration_event_id": row["evidence"][0] if row["evidence"] else None,
                "event_ids": list(row["evidence"]),
                "face_steps": [
                    {"context": s["context"],
                     "results": [a["result"] for a in s["attempts"]],
                     "template_ref": s["attempts"][0]["template_ref"]
                     if s["attempts"] else None}
                    for s in row["face_chain"]["steps"] if s["attempts"]],
                "ruling_event_ids": [r["event_id"] for r in row["rulings"]],
                "voided_ruling_ids": row.get("voided_ruling_ids", []),
            },
        })

    payload = {
        "list_id": list_id,
        "kind": kind,                       # results / awards
        "scope": scope,                     # provisional / final
        "distance": distance,
        "bulletin_version": int(bulletin_version),
        "supersedes": list(supersedes or []),
        "note": note,
        "published_at": published_at,
        "ranking_basis": bulletin.get("rules", {}).get("ranking_basis", "gun"),
        "appeal_window_hours": float(
            bulletin.get("rules", {}).get("appeal_window_hours", 24)),
        "entry_count": len(entries),
        "entries": entries,
    }
    payload["fingerprint"] = canonical_fingerprint(payload)
    return payload


def _strip_bib(award):
    return {k: v for k, v in award.items() if k not in ("bib", "distance")}


# --------------------------------------------------------------------------
# 申诉期限
# --------------------------------------------------------------------------

def appeal_deadline(state, list_id):
    snap = state.publications.get(list_id)
    if snap is None:
        return None
    hours = float(snap.get("appeal_window_hours", 24))
    return parse_iso(snap["published_at"]).timestamp() + hours * 3600


def appeal_history(state, bib):
    """汇总一名选手的申诉 -> 决定 -> 关联裁决，供完赛名单逐项追溯。"""
    out = []
    for rec in state.appeals.get(bib, []):
        filed = rec["filed"]
        snap = state.publications.get(filed.get("list_id"))
        deadline = appeal_deadline(state, filed.get("list_id")) if snap else None
        item = {
            "appeal_id": filed["appeal_id"],
            "list_id": filed.get("list_id"),
            "reason": filed.get("reason"),
            "filed_at": filed["occurred_at"],
            "within_window": (deadline is not None
                              and filed["ts"] <= deadline + 1e-6),
            "deadline_at": None if deadline is None else _iso(deadline),
            "bulletin_version": snap.get("bulletin_version") if snap else None,
            "status": "pending" if rec["decision"] is None
            else rec["decision"]["outcome"],
            "decision": None,
        }
        if rec["decision"] is not None:
            d = rec["decision"]
            item["decision"] = {
                "outcome": d["outcome"],
                "note": d.get("note"),
                "ruling_event_id": d.get("ruling_event_id"),
                "decided_at": d["occurred_at"],
            }
        out.append(item)
    return out


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()
