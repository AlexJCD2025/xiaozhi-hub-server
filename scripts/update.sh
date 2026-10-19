#!/usr/bin/env bash
# xiaozhi-hub-server 更新到最新版本（macOS / Linux）
# 用法：bash scripts/update.sh
#   1. 从 GitHub 拉取最新代码（配置 data/.config.yaml、模型、虚拟环境都不在 Git 里，不受影响）
#   2. 重新运行 install.sh 补装新增的依赖和模型（已有的会跳过）
#   3. 开了开机自启时重启后台服务；手动启动的服务需要自己重启
set -euo pipefail

# 整个脚本放在函数里：git 更新会改写本文件，bash 边读边执行会读到新旧混杂的内容。
# 拉取完代码后用 exec 换成新版本的本脚本继续后面的步骤
main() {
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  cd "$ROOT"
  if [[ "${1:-}" == "--after-pull" ]]; then after_pull "${2:-}"; return; fi

  git rev-parse --git-dir >/dev/null 2>&1 || fail "${ROOT} 不是用 git 下载的，无法自动更新。请重新运行一键安装命令，或手动下载新版本"
  local branch old new
  branch="$(git symbolic-ref --short -q HEAD)" || fail "当前不在任何分支上，无法自动更新"

  # 只看受 Git 管理的文件；配置、模型、虚拟环境都被 .gitignore 忽略，不算改动
  if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    git status --short --untracked-files=no
    fail "上面这些文件被改过，更新会和它们冲突。请先撤销（git checkout -- <文件>）或备份后再更新"
  fi

  step "检查新版本"
  old="$(git rev-parse HEAD)"
  local shallow
  shallow="$(git rev-parse --is-shallow-repository)"
  if [[ "$shallow" == "true" ]]; then
    # 一键安装下载的是只含最新一次提交的浅克隆，更新时也只取最新一次提交
    git fetch --depth 1 origin "$branch"
  else
    git fetch origin "$branch"
  fi
  new="$(git rev-parse FETCH_HEAD)"
  if [[ "$old" == "$new" ]]; then
    info "已经是最新版本（$(git log -1 --format='%h，%cd' --date=short)）"
    info "如果上次更新中途失败了，可以运行 bash scripts/install.sh 补装"
    return
  fi

  step "下载新版本"
  if [[ "$shallow" == "true" ]]; then
    # 浅克隆没有新旧版本之间的历史，不能快进合并，直接切到新版本（上面已确认没有本地改动）
    git reset --keep FETCH_HEAD
  else
    git merge --ff-only FETCH_HEAD || fail "本地有自己的提交，无法直接更新。请手动合并：git merge origin/${branch}"
  fi
  exec bash "$ROOT/scripts/update.sh" --after-pull "$old"
}

after_pull() {
  local old="$1"
  # 只检出运行需要的目录；和 bootstrap.sh 里的列表保持一致。以后新增目录时，更新会把它检出来
  local SPARSE_PATHS=(main/xiaozhi-server scripts hub-docs)
  if [[ "$(git config --bool core.sparseCheckout || true)" == "true" ]]; then
    git sparse-checkout set "${SPARSE_PATHS[@]}"
  fi

  if [[ -n "$old" ]] && git cat-file -e "${old}^{commit}" 2>/dev/null && git merge-base --is-ancestor "$old" HEAD 2>/dev/null; then
    info "更新内容："
    git log --format='      %h %s' "$old..HEAD" | head -20
  else
    # 浅克隆只有最新一次提交，列不出中间的改动，给出 GitHub 上的对比页
    info "已更新到：$(git log -1 --format='%h %s（%cd）' --date=short)"
    local url
    url="$(git remote get-url origin 2>/dev/null | sed -E 's#^git@github.com:#https://github.com/#; s#\.git$##')"
    [[ -n "$old" && "$url" == https://github.com/* ]] && info "全部改动：$url/compare/${old:0:12}...$(git rev-parse --short=12 HEAD)"
  fi

  step "补装依赖和模型"
  HUB_UPDATING=1 bash "$ROOT/scripts/install.sh" || fail "安装步骤失败。按上面的提示处理后，运行 bash scripts/install.sh 补装"

  if bash "$ROOT/scripts/autostart.sh" status >/dev/null 2>&1; then
    step "重启后台服务"
    bash "$ROOT/scripts/autostart.sh" restart
    printf "\n更新完成，后台服务已重启。\n"
  else
    printf "\n更新完成。如果服务正在运行，请在它的终端按 Ctrl+C 停止，再运行 bash \"%s\" 启动新版本。\n" "$ROOT/scripts/start.sh"
  fi
}

step() { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
info() { printf "    %s\n" "$*"; }
fail() { printf "\n\033[31m✗ %s\033[0m\n" "$*" >&2; exit 1; }

main "$@"
