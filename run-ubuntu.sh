#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="${GPU_HUNTER_REPO_URL:-https://github.com/haohaoi34/GPU-Hunter.git}"
APP_DIR="${GPU_HUNTER_DIR:-$HOME/GPU-Hunter}"
BRANCH="${GPU_HUNTER_BRANCH:-main}"

say() {
  printf "\n[GPU Hunter] %s\n" "$1"
}

run_as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    say "需要 root 权限，请先安装 sudo 或使用 root 用户运行。"
    exit 1
  fi
}

install_base_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    say "安装基础环境：git / python3 / ca-certificates / curl"
    run_as_root apt-get update
    run_as_root DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 ca-certificates curl
    return
  fi

  say "未检测到 apt-get。请确认这是 Ubuntu/Debian 系统，或手动安装 git 与 python3。"
  exit 1
}

clone_or_update_repo() {
  if [ -d "$APP_DIR/.git" ]; then
    say "更新仓库：$APP_DIR"
    git -C "$APP_DIR" fetch --all --prune
    git -C "$APP_DIR" checkout "$BRANCH"
    git -C "$APP_DIR" pull --ff-only
    return
  fi

  if [ -e "$APP_DIR" ]; then
    say "$APP_DIR 已存在但不是 Git 仓库，请删除或设置 GPU_HUNTER_DIR 指向其他目录。"
    exit 1
  fi

  say "克隆仓库到：$APP_DIR"
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
}

run_app() {
  cd "$APP_DIR"
  if [ ! -f "gpu_hunter_server.py" ]; then
    say "没有找到 gpu_hunter_server.py，请确认仓库内容完整。"
    exit 1
  fi

  chmod +x gpu_hunter_server.py
  say "启动 GPU Hunter。按提示填写 API Key、价格、可选 Telegram 和代理。"
  exec python3 gpu_hunter_server.py "$@"
}

install_base_packages
clone_or_update_repo
run_app "$@"
