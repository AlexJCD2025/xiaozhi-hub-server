"""检查某个模块（provider）需要的 Python 包是否已安装。

精简安装只装了默认组合需要的包，其他模块（如 FunASR、阿里云 TTS）的依赖可能没装。
这里不维护"模块 → 包"的对照表，而是直接读 provider 源文件顶层的 import，
所以原项目新增或修改 provider 后也能自动适配。

只看模块加载时就会执行的 import：
- 跳过 try 块里的（通常是可选依赖，缺了也能加载）和函数内部的（用到时才导入）；
- 递归进入项目自身的模块（core、config、plugins_func），找出它们间接依赖的包。
"""

import ast
import importlib.machinery
import importlib.util
import sys
from functools import lru_cache
from pathlib import Path

from config.config_loader import get_project_dir

PROJECT_PACKAGES = {"core", "config", "plugins_func"}


def provider_file(section: str, type_: str) -> Path:
    base = Path(get_project_dir()) / "core" / "providers"
    if section in ("LLM", "Intent"):
        return base / section.lower() / type_ / f"{type_}.py"
    return base / section.lower() / f"{type_}.py"


def _project_file(module: str):
    path = Path(get_project_dir()).joinpath(*module.split("."))
    for candidate in (path.with_suffix(".py"), path / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _module_level_imports(tree: ast.Module):
    """模块加载时会执行的 import 语句（含 if 分支，不含 try 和函数、类内部）。"""
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif isinstance(node, ast.If):
            stack.extend(node.body + node.orelse)


def _is_namespace(spec) -> bool:
    return spec.origin in (None, "namespace") and spec.submodule_search_locations is not None


@lru_cache(maxsize=None)
def _find_top(top: str):
    loaded = sys.modules.get(top)
    if loaded is not None:
        # 已经加载过的模块一定可用；有些模块的 __spec__ 是 None（例如测试里的桩模块），find_spec 会报错
        return getattr(loaded, "__spec__", None) or importlib.machinery.ModuleSpec(top, None, origin="loaded")
    try:
        return importlib.util.find_spec(top)
    except (ImportError, ValueError):
        return None


@lru_cache(maxsize=None)
def _submodule_exists(name: str) -> bool:
    if name in sys.modules:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _external_imports(path: Path, seen: set) -> set:
    if path in seen:
        return set()
    seen.add(path)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()
    names = set()
    for node in _module_level_imports(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif node.level == 0 and node.module:
            # from google import genai：google 是命名空间包时，真正的依赖是 google.genai
            modules = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names if alias.name != "*"]
        else:
            continue  # 相对导入，属于项目自身
        for module in modules:
            if module.split(".")[0] in PROJECT_PACKAGES:
                sub = _project_file(module)
                if sub:
                    names |= _external_imports(sub, seen)
            else:
                names.add(module)
    return names


@lru_cache(maxsize=None)
def missing_packages(section: str, type_: str) -> tuple:
    """返回该模块缺少的包（按导入名），为空表示依赖齐全。找不到源文件时也返回空，交给原项目报错。"""
    path = provider_file(section, type_)
    if not path.is_file():
        return ()
    missing = set()
    for module in _external_imports(path, set()):
        parts = module.split(".")
        spec = _find_top(parts[0])
        if spec is None:
            missing.add(parts[0])
        elif len(parts) > 1 and _is_namespace(spec):
            # 命名空间包（如装了 protobuf 就会有 google）要看到第二层：google 在，不代表 google.genai 在。
            # 普通包只看顶层，避免为了检查而执行它的 __init__
            name = ".".join(parts[:2])
            if not _submodule_exists(name):
                missing.add(name)
    return tuple(sorted(missing))
