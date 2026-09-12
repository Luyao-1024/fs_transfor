"""有限的传输结果历史；只保存白名单字段，不保存连接凭据。"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile

from .backend.base import BackendError
from .backend.local import LocalBackend

MAX_HISTORY = 200
STATES = {"done", "partial", "cancelled", "error"}
ITEM_STATES = {"committed", "moved", "skipped", "error", "cancelled",
               "source_delete_failed", "pending"}


def endpoint(backend):
    if backend.is_local:
        return {"type": "local"}
    return {"type": "ssh", "host": getattr(backend, "host", ""),
            "port": getattr(backend, "port", 22),
            "username": getattr(backend, "username", "") or ""}


def snapshot(task):
    return {"uid": task.uid, "src": endpoint(task.src), "dst": endpoint(task.dst),
            "paths": list(task.src_paths), "dst_dir": task.dst_dir, "move": task.move,
            "status": task.status, "phase": task.phase, "error": task.error,
            "note": task.note, "bytes": task.done_bytes, "total": task.total_bytes,
            "files": task.file_count, "done_files": task.done_files,
            "known": task.total_known, "created_at": task.created_at,
            "ended_at": task.ended_at, "items": dict(task.item_status),
            "item_errors": dict(task.item_errors)}


def endpoint_label(value):
    if value["type"] == "local":
        return "本地"
    return f'{value["username"]}@{value["host"]}:{value["port"]}'


def clean_record(record):
    """损坏或非预期字段不进入 UI；历史永远不会自行重放。"""
    if not isinstance(record, dict) or record.get("status") not in STATES:
        return None
    for key in ("uid", "dst_dir", "error", "note"):
        if not isinstance(record.get(key), str):
            return None
    paths = record.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        return None
    out = {k: record[k] for k in ("uid", "dst_dir", "error", "note", "status", "paths")}
    for side in ("src", "dst"):
        value = record.get(side)
        if not isinstance(value, dict):
            return None
        if value.get("type") == "local":
            out[side] = {"type": "local"}
        elif (value.get("type") == "ssh" and isinstance(value.get("host"), str)
              and isinstance(value.get("username"), str)
              and type(value.get("port")) is int and 1 <= value["port"] <= 65535):
            out[side] = {k: value[k] for k in ("type", "host", "port", "username")}
        else:
            return None
    out["move"] = record.get("move") is True
    out["known"] = record.get("known") is True
    out["phase"] = "finished"
    for key in ("bytes", "total", "files", "done_files", "created_at", "ended_at"):
        value = record.get(key)
        out[key] = value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else 0
    for key in ("items", "item_errors"):
        value = record.get(key, {})
        out[key] = {k: v for k, v in value.items()
                    if isinstance(k, str) and isinstance(v, str)
                    and (key != "items" or v in ITEM_STATES)} if isinstance(value, dict) else {}
    return out


class TaskHistory:
    def __init__(self, path, on_error=None):
        self.path = Path(path)
        self.on_error = on_error or (lambda message: None)
        self.records = []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                self.records = [r for item in raw[-MAX_HISTORY:]
                                if (r := clean_record(item)) is not None]
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.on_error("任务历史无法读取，原文件保留")

    def save(self):
        temp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                             dir=self.path.parent, delete=False) as stream:
                temp = stream.name
                json.dump(self.records, stream, ensure_ascii=False, allow_nan=False)
            os.replace(temp, self.path)
        except (OSError, ValueError) as error:
            self.on_error(f"任务历史保存失败: {error}")
        finally:
            if temp is not None and os.path.exists(temp):
                try:
                    os.unlink(temp)
                except OSError:
                    self.on_error(f"历史临时文件清理失败: {temp}")

    def record(self, task):
        value = clean_record(snapshot(task))
        if value is None:
            return
        self.records = [r for r in self.records if r["uid"] != task.uid]
        self.records.append(value)
        self.records = self.records[-MAX_HISTORY:]
        self.save()

    def clear(self):
        self.records = []
        self.save()

    def remove(self, uid):
        self.records = [r for r in self.records if r["uid"] != uid]
        self.save()


def retry_record(manager, record, names=None, failed_only=False):
    """重试所选顶层条目；目录重新扫描，重新确认冲突，不自动续传。"""
    def resolve(value):
        if value["type"] == "local":
            return LocalBackend()
        hub = manager.connection_hub
        if hub is not None:
            entry = hub._live.get(hub.key(value))
            if entry is not None and not entry.backend.dead:
                return entry.backend
        raise BackendError("请先在面板中重新连接对应服务器，再重试任务")

    src, dst = resolve(record["src"]), resolve(record["dst"])
    paths = []
    for path in record["paths"]:
        name = src.basename(path)
        state = record["items"].get(name, "error")
        if names is not None and name not in names:
            continue
        if failed_only and state not in ("error", "cancelled", "pending"):
            continue
        if record["move"] and state in ("moved", "committed", "source_delete_failed"):
            continue  # 已复制源的清理需人工核对，不重新执行移动或覆盖。
        paths.append(path)
    if not paths:
        raise BackendError("没有可重试的项目；已复制的移动项目请核对目标后手动处理源")
    return manager.enqueue(src, paths, dst, record["dst_dir"], move=record["move"])
