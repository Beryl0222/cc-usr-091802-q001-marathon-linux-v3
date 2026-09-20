"""追加式事件日志：断网补传、同一事件重放或乱序到达都不改变最终判定。

所有赛务事实（报名、人脸核验结论、领物、检录、计时、裁决、申诉、公示、
救治调度）都以事件形式追加。日志按 event_id 幂等去重，投影按事件发生时
间排序，因此到达顺序不影响名次。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading

from .timeutil import parse_instant  # noqa: F401  (校验时区用)

EVENT_TYPES = {
    "registration", "face_verification", "bib_pickup", "corral_checkin",
    "wave_change_request", "timing", "ruling", "appeal", "appeal_decision",
    "publication", "medical_case_opened", "medical_resource_locked",
    "medical_resource_released", "medical_transport", "medical_case_closed",
}

# 人脸比对只保留模板引用与结论，生物特征原文禁止进入日志。
FORBIDDEN_KEYS = {
    "image", "photo", "face_image", "face_photo", "template",
    "template_data", "embedding", "embeddings", "biometric", "biometrics",
}

REQUIRED_FIELDS = {
    "registration": ("athlete",),
    "face_verification": ("athlete_id", "phase", "decision"),
    "bib_pickup": ("athlete_id", "bib", "wave"),
    "corral_checkin": ("athlete_id", "wave"),
    "wave_change_request": ("athlete_id", "from_wave", "to_wave"),
    "timing": ("bib", "point"),
    "ruling": ("ruling_id", "action"),
    "appeal": ("appeal_id", "athlete_id"),
    "appeal_decision": ("appeal_id", "decision"),
    "publication": ("publication_id", "distance", "gender", "snapshot"),
    "medical_case_opened": ("case_id", "location_km", "category"),
    "medical_resource_locked": ("case_id", "resource_id"),
    "medical_resource_released": ("case_id", "resource_id"),
    "medical_transport": ("case_id", "hospital_id"),
    "medical_case_closed": ("case_id",),
}


def _scan_forbidden(node, path="$"):
    """递归检查事件中是否夹带生物特征原文字段。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() in FORBIDDEN_KEYS:
                return f"{path}.{key}"
            found = _scan_forbidden(value, f"{path}.{key}")
            if found:
                return found
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found = _scan_forbidden(item, f"{path}[{index}]")
            if found:
                return found
    return None


def validate_event(raw):
    """校验事件信封；不合法时抛出 ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError("事件必须是对象")
    event_id = raw.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("事件缺少 event_id")
    etype = raw.get("type")
    if etype not in EVENT_TYPES:
        raise ValueError(f"未知事件类型: {etype!r}")
    occurred = raw.get("occurred_at")
    if not isinstance(occurred, str):
        raise ValueError("事件缺少 occurred_at")
    parse_instant(occurred)
    for field_name in REQUIRED_FIELDS[etype]:
        if field_name not in raw:
            raise ValueError(f"事件缺少字段: {field_name}")
    bad = _scan_forbidden(raw)
    if bad:
        raise ValueError(f"事件包含禁止的生物特征字段: {bad}")
    if etype == "face_verification" and raw.get("decision") not in ("match", "no_match"):
        raise ValueError("face_verification.decision 只能是 match/no_match")
    return True


class Journal:
    """线程安全的追加式事件日志，可选 JSONL 文件持久化。"""

    def __init__(self, path=None):
        self._events = []
        self._ids = {}
        self._lock = threading.Lock()
        self._path = path
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        self._store(json.loads(line))

    def _store(self, event):
        seq = len(self._events)
        self._events.append(event)
        self._ids[event["event_id"]] = seq
        return seq

    def append(self, events):
        """批量追加，返回逐条回执：accepted / duplicate / rejected。"""
        receipts = []
        with self._lock:
            for raw in events:
                receipts.append(self._append_one(raw))
        return receipts

    def _append_one(self, raw):
        event_id = raw.get("event_id") if isinstance(raw, dict) else None
        try:
            validate_event(raw)
        except ValueError as exc:
            return {"event_id": event_id, "status": "rejected", "error": str(exc)}
        if raw["event_id"] in self._ids:
            return {"event_id": raw["event_id"], "status": "duplicate",
                    "seq": self._ids[raw["event_id"]]}
        seq = self._store(raw)
        if self._path:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(raw, ensure_ascii=False, sort_keys=True) + "\n")
        return {"event_id": raw["event_id"], "status": "accepted", "seq": seq}

    def __len__(self):
        return len(self._events)

    def __iter__(self):
        return iter(enumerate(self._events))

    def get(self, event_id):
        seq = self._ids.get(event_id)
        return self._events[seq] if seq is not None else None

    def digest(self):
        """全部事件 ID 的摘要，用于公示快照与日志互相印证。"""
        hasher = hashlib.sha256()
        for event_id in sorted(self._ids):
            hasher.update(event_id.encode("utf-8"))
            hasher.update(b"\n")
        return hasher.hexdigest()
