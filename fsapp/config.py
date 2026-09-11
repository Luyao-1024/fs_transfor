"""配置持久化: 服务器列表 / 会话设置 / ~/.ssh/config 解析."""
from __future__ import annotations

import copy
import json
import os
import uuid
from pathlib import Path

CONFIG_DIR = Path(
    os.environ.get("FSTRANSFOR_CONFIG_HOME")
    or os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "fs_transfor"
SERVERS_FILE = CONFIG_DIR / "servers.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "auto_connect": True,
    "show_hidden": False,
    "tabs": [],          # [{"left": {...}, "right": {...}}, ...]
    "active_tab": 0,
}

# 服务器配置字段(密码/口令永不落盘)
SERVER_FIELDS = ("id", "name", "host", "port", "username", "auth_method", "key_path")


def _load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(default)


def _save_json(path: Path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass  # 配置写失败不致命: 忽略


def load_servers() -> list[dict]:
    data = _load_json(SERVERS_FILE, [])
    out = []
    for s in data if isinstance(data, list) else []:
        if isinstance(s, dict) and s.get("host"):
            out.append({k: s.get(k) for k in SERVER_FIELDS})
    return out


def save_servers(servers: list[dict]):
    _save_json(SERVERS_FILE, [{k: s.get(k) for k in SERVER_FIELDS} for s in servers])


def upsert_server(cfg: dict) -> dict:
    servers = load_servers()
    cfg = {k: cfg.get(k) for k in SERVER_FIELDS}
    if not cfg.get("id"):
        cfg["id"] = uuid.uuid4().hex[:8]
        servers.append(cfg)
    else:
        for i, s in enumerate(servers):
            if s.get("id") == cfg["id"]:
                servers[i] = cfg
                break
        else:
            servers.append(cfg)
    save_servers(servers)
    return cfg


def delete_server(server_id: str):
    save_servers([s for s in load_servers() if s.get("id") != server_id])


def find_server(server_id: str | None):
    if not server_id:
        return None
    for s in load_servers():
        if s.get("id") == server_id:
            return s
    return None


def load_settings() -> dict:
    data = _load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    if not isinstance(data, dict):
        return copy.deepcopy(DEFAULT_SETTINGS)
    merged = copy.deepcopy(DEFAULT_SETTINGS)
    for k in DEFAULT_SETTINGS:
        if k in data:
            merged[k] = data[k]
    if not merged.get("tabs"):
        # 兼容旧单标签格式(left/right 顶层字段)
        left, right = data.get("left"), data.get("right")
        if left or right:
            merged["tabs"] = [{"left": left or {"type": "local", "path": None},
                               "right": right or {"type": "local", "path": None}}]
        else:
            merged["tabs"] = []
    return merged


def save_settings(settings: dict):
    _save_json(SETTINGS_FILE, settings)


def load_ssh_config_hosts() -> list[dict]:
    """解析 ~/.ssh/config 中的主机条目(跳过通配模式), 用于一键填充连接表单."""
    path = Path.home() / ".ssh" / "config"
    if not path.exists():
        return []
    import paramiko

    cfg = paramiko.SSHConfig()
    try:
        with open(path, encoding="utf-8") as f:
            cfg.parse(f)
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    seen = set()
    for host in sorted(cfg.get_hostnames()):
        if not host or "*" in host or "!" in host or host in seen:
            continue
        seen.add(host)
        c = cfg.lookup(host)
        try:
            port = int(c.get("port", 22) or 22)
        except (TypeError, ValueError):
            port = 22
        key = None
        ids = c.get("identityfile") or []
        if ids:
            key = ids[0] if isinstance(ids[0], str) else (ids[0][0] if ids[0] else None)
        out.append({
            "name": host,
            "host": c.get("hostname", host),
            "port": port,
            "username": c.get("user") or os.environ.get("USER", ""),
            "key_path": key,
        })
    return out
