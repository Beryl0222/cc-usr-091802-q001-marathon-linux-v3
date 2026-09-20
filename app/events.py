"""只追加事件日志。

所有赛务事实（报名、领物、人脸核验、检录、计时、完赛、裁决、公示、
申诉、医疗求助与转运）都以事件形式追加，不做原地更新。判定结果全部可由
事件日志重放重建，因此：

- 设备断网补传：迟到事件按其业务时间 ``occurred_at`` 归位，不影响确定性；
- 同一事件重放：``event_id`` 相同，或同一 ``(device_id, device_seq)`` 重复上报，
  只会被记录一次；
- 乱序到达：归约顺序固定为 ``(occurred_at, event_id)``，与到达先后无关。
"""

import json
import os
import threading
import uuid

from .timeutil import parse_iso


def new_id(prefix="evt"):
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class ValidationError(ValueError):
    """事件内容不合法（HTTP 层映射为 400）。"""


class EventStore:
    """线程安全的只追加 JSONL 事件日志。"""

    def __init__(self, path=None):
        self._path = path
        self._lock = threading.RLock()
        self._events = []
        self._ids = set()
        # (device_id, device_seq) -> 首次入库的 event_id
        self._device_index = {}
        self._listeners = []
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        self._ingest(json.loads(line), persist=False)

    # ---- 写入 -----------------------------------------------------------

    def append(self, event_type, payload, occurred_at, event_id=None,
               device_id=None, device_seq=None, ingested_at=None):
        """校验并追加一个事件，返回 ``(event, duplicated)``。

        duplicated 为 True 时表示该事件此前已入库（重放/补传去重），
        返回的是首次入库的那条事件。
        """
        from .timeutil import utc_iso
        from datetime import datetime, timezone

        if not isinstance(event_type, str) or not event_type:
            raise ValidationError("缺少 event_type")
        if not isinstance(payload, dict):
            raise ValidationError("payload 必须是对象")
        try:
            occurred_dt = parse_iso(occurred_at)
        except (ValueError, TypeError) as exc:
            raise ValidationError(str(exc)) from exc

        event_id = event_id or new_id()
        dup_key = (device_id, device_seq) if device_id is not None and device_seq is not None else None
        with self._lock:
            if event_id in self._ids:
                return self._find(event_id), True
            if dup_key is not None and dup_key in self._device_index:
                return self._find(self._device_index[dup_key]), True

            event = {
                "event_id": event_id,
                "event_type": event_type,
                "occurred_at": occurred_at.strip(),
                "occurred_at_utc": occurred_dt.isoformat(),
                "ingested_at": ingested_at
                or datetime.now(timezone.utc).isoformat(),
                "payload": payload,
            }
            if device_id is not None:
                event["device_id"] = device_id
            if device_seq is not None:
                event["device_seq"] = device_seq
            self._ingest(event, persist=True)
            for listener in self._listeners:
                listener(event)
            return event, False

    def _ingest(self, event, persist):
        """内部：放入索引；可选落盘。不做重复检查（重放时直接走这里）。"""
        self._ids.add(event["event_id"])
        device_id = event.get("device_id")
        device_seq = event.get("device_seq")
        if device_id is not None and device_seq is not None:
            self._device_index.setdefault((device_id, device_seq), event["event_id"])
        self._events.append(event)
        if persist and self._path:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())

    def _find(self, event_id):
        for event in self._events:
            if event["event_id"] == event_id:
                return event
        raise KeyError(event_id)

    # ---- 读取 -----------------------------------------------------------

    def canonical_events(self):
        """按 ``(occurred_at_utc, event_id)`` 排序的事件列表——确定性重放顺序。

        原始时区字符串原样保留在 ``occurred_at``，排序一律用换算后的 UTC。
        """
        return sorted(self._events,
                      key=lambda e: (e.get("occurred_at_utc", e["occurred_at"]),
                                     e["event_id"]))

    def all_events(self):
        return list(self._events)

    def count(self):
        return len(self._events)

    def lock(self):
        return self._lock
