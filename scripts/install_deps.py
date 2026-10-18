"""安装 Python 依赖：按原项目方式安装 requirements.txt 的全部依赖，再安装 requirements-hub.txt。

不修改原项目文件，也不维护跳过列表：安装失败时，从 uv 的报错里找出当前平台装不上的库，
在临时清单里去掉它重新安装（最多重试几次）。核心库是否齐全由 doctor.py 检查。

只用标准库，可以用任意 Python 3 运行：
    python3 scripts/install_deps.py --python main/xiaozhi-server/.venv/bin/python
"""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = ROOT / "main" / "xiaozhi-server"
MAX_RETRIES = 6


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def req_name(line: str):
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith("-"):
        return None
    m = re.match(r"([A-Za-z0-9_.\-]+)", line)
    return norm(m.group(1)) if m else None


def find_culprit(stderr: str, names: set):
    """从 uv 的报错里找出导致无法安装的那个库（必须是 requirements.txt 里直接列出的）。"""
    # 网络问题（下载失败、超时）不是"平台装不上"，不能跳过，否则可能把 torch 这类核心库跳掉
    if re.search(r"Failed to (?:download|fetch)|timed? ?out|Connection (?:reset|refused)|network", stderr, re.I):
        return None
    patterns = [
        r"Because (?:only )?([A-Za-z0-9_.\-]+)[=<>!~ ]",          # Because vosk==0.3.45 has no wheels ...
        r"you require ([A-Za-z0-9_.\-]+)[=<>!~ ]",
        r"Failed to build `?([A-Za-z0-9_.\-]+)",                 # 当前平台没有安装包，只能编译且编译失败
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, stderr, re.S):
            name = norm(m.group(1))
            if name in names:
                return name
    return None


def uv_install(python: str, req_file: Path, constraint: Path = None) -> subprocess.CompletedProcess:
    cmd = ["uv", "pip", "install", "--python", python, "-r", str(req_file)]
    if constraint:
        cmd += ["-c", str(constraint)]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True, help="虚拟环境里的 python 路径")
    args = parser.parse_args()

    lines = (SERVER_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
    names = {n for n in (req_name(l) for l in lines) if n}
    skipped = []
    tmp_dir = Path(tempfile.mkdtemp(prefix="xiaozhi-req-"))
    req_tmp = tmp_dir / "requirements.txt"

    for attempt in range(MAX_RETRIES + 1):
        req_tmp.write_text("\n".join(l for l in lines if req_name(l) not in skipped) + "\n", encoding="utf-8")
        note = "（已跳过：" + ", ".join(skipped) + "）" if skipped else ""
        print(f"==> 安装原项目依赖{note} ...", flush=True)
        result = uv_install(args.python, req_tmp)
        if result.returncode == 0:
            break
        culprit = find_culprit(result.stderr, names - set(skipped))
        if not culprit or attempt == MAX_RETRIES:
            print(result.stderr[-3000:], file=sys.stderr)
            print("依赖安装失败，且无法自动判断是哪个库导致的，请查看上面的报错。"
                  "如果是网络问题，可以设置 UV_INDEX_URL 使用国内镜像后重新运行。", file=sys.stderr)
            return 1
        reason = next((l.strip() for l in result.stderr.splitlines() if culprit.split("-")[0] in l.lower()), "")
        print(f"    当前平台装不上 {culprit}，跳过后重试。原因：{reason[:160]}", flush=True)
        skipped.append(culprit)

    # 新增依赖，用原项目的版本做约束，避免把已锁定的库（如 mcp）升级掉
    constraint = tmp_dir / "constraints.txt"
    constraint.write_text(req_tmp.read_text(encoding="utf-8"), encoding="utf-8")
    print("==> 安装 xiaozhi-hub 新增依赖 ...", flush=True)
    result = uv_install(args.python, SERVER_DIR / "requirements-hub.txt", constraint)
    if result.returncode != 0:
        print(result.stderr[-3000:], file=sys.stderr)
        print("新增依赖安装失败。", file=sys.stderr)
        return 1

    if skipped:
        print(f"==> 已跳过当前平台装不上的库：{', '.join(skipped)}。它们只用于可选方案，是否影响使用以自检结果为准。")
    print("==> Python 依赖安装完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
