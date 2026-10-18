"""安装 Python 依赖。

只装 requirements-hub-core.txt 里的核心包（不含 torch），版本以原项目的 requirements.txt 为约束，
再安装 requirements-hub.txt。核心库是否齐全由 doctor.py 检查。

只用标准库，可以用任意 Python 3 运行：
    python3 scripts/install_deps.py --python main/xiaozhi-server/.venv/bin/python
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = ROOT / "main" / "xiaozhi-server"


def uv_install(python: str, req_file: Path, constraint: Path) -> subprocess.CompletedProcess:
    cmd = ["uv", "pip", "install", "--python", python, "-r", str(req_file), "-c", str(constraint)]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True, help="虚拟环境里的 python 路径")
    args = parser.parse_args()

    # 原项目的 requirements.txt 只作为版本约束：清单里没有的包不会被安装
    constraint = Path(tempfile.mkdtemp(prefix="xiaozhi-req-")) / "constraints.txt"
    constraint.write_text((SERVER_DIR / "requirements.txt").read_text(encoding="utf-8"), encoding="utf-8")

    print("==> 安装核心依赖 ...", flush=True)
    result = uv_install(args.python, SERVER_DIR / "requirements-hub-core.txt", constraint)
    if result.returncode != 0:
        print(result.stderr[-3000:], file=sys.stderr)
        print("核心依赖安装失败。如果是网络问题，可以设置 UV_INDEX_URL 使用国内镜像后重新运行。", file=sys.stderr)
        return 1

    # 新增依赖，用原项目的版本做约束，避免把已锁定的库（如 mcp）升级掉
    print("==> 安装 xiaozhi-hub 新增依赖 ...", flush=True)
    result = uv_install(args.python, SERVER_DIR / "requirements-hub.txt", constraint)
    if result.returncode != 0:
        print(result.stderr[-3000:], file=sys.stderr)
        print("新增依赖安装失败。", file=sys.stderr)
        return 1

    print("==> Python 依赖安装完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
