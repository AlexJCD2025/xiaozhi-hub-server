"""读写 data/.config.yaml。

写入使用 ruamel.yaml 往返模式，保留用户文件里的注释和格式；
读取合并后的配置时绕过 load_config 的缓存，保证拿到磁盘上的最新值。
"""

import os
import tempfile
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from config.config_loader import get_project_dir, merge_configs, read_config

# 管理后台允许修改的 LLM 字段
EDITABLE_LLM_FIELDS = ("api_key", "base_url", "url", "model_name")


def default_config_path() -> Path:
    return Path(get_project_dir()) / "config.yaml"


def custom_config_path() -> Path:
    return Path(get_project_dir()) / "data" / ".config.yaml"


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    return y


def load_custom(path: Path = None) -> CommentedMap:
    path = path or custom_config_path()
    if not path.exists():
        return CommentedMap()
    with open(path, "r", encoding="utf-8") as f:
        data = _yaml().load(f)
    return data if isinstance(data, CommentedMap) else CommentedMap()


def save_custom(data: CommentedMap, path: Path = None) -> None:
    """原子写入：先写临时文件再替换，避免写一半时服务读到损坏的配置。"""
    path = path or custom_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _yaml().dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_merged(default_path: Path = None, custom_path: Path = None) -> dict:
    default = read_config(str(default_path or default_config_path())) or {}
    custom = read_config(str(custom_path or custom_config_path())) or {}
    return merge_configs(default, custom)


def is_placeholder(value) -> bool:
    """原项目用含"你"的占位文本表示未填写，例如"你的api_key"。"""
    return value is None or str(value).strip() == "" or "你" in str(value)


def mask_key(value) -> str:
    if is_placeholder(value):
        return ""
    value = str(value)
    if len(value) <= 12:
        return "已设置"
    return f"{value[:4]}…{value[-4:]}"


def list_llms(merged: dict) -> list:
    items = []
    for name, entry in (merged.get("LLM") or {}).items():
        if not isinstance(entry, dict):
            continue
        item = {"name": name, "type": entry.get("type", name), "fields": {}}
        for field in ("model_name", "base_url", "url"):
            if field in entry:
                item["fields"][field] = "" if is_placeholder(entry[field]) else str(entry[field])
        if "api_key" in entry:
            item["api_key_set"] = not is_placeholder(entry["api_key"])
            item["api_key_hint"] = mask_key(entry["api_key"])
        items.append(item)
    return items


def update_llm(name: str, fields: dict, merged: dict, path: Path = None) -> None:
    """把字段写入 data/.config.yaml 的 LLM.<name>。

    只接受该模型在合并配置里本来就有的字段；api_key 传空表示不修改。
    """
    entry = (merged.get("LLM") or {}).get(name)
    if not isinstance(entry, dict):
        raise KeyError(name)
    changes = {}
    for field, value in fields.items():
        if field not in EDITABLE_LLM_FIELDS or field not in entry:
            continue
        value = "" if value is None else str(value).strip()
        if field == "api_key" and value == "":
            continue
        changes[field] = value
    if not changes:
        return
    data = load_custom(path)
    llm = data.setdefault("LLM", CommentedMap())
    target = llm.setdefault(name, CommentedMap())
    for field, value in changes.items():
        target[field] = value
    save_custom(data, path)


def select_llm(name: str, merged: dict, path: Path = None) -> None:
    if name not in (merged.get("LLM") or {}):
        raise KeyError(name)
    data = load_custom(path)
    data.setdefault("selected_module", CommentedMap())["LLM"] = name
    save_custom(data, path)


def get_password_hash(path: Path = None):
    admin = load_custom(path).get("admin") or {}
    return admin.get("password_hash")


def set_password_hash(password_hash: str, path: Path = None) -> None:
    data = load_custom(path)
    data.setdefault("admin", CommentedMap())["password_hash"] = password_hash
    save_custom(data, path)
