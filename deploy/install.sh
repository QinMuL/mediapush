#!/usr/bin/env bash
# MediaPush systemd 直装脚本。
#
# 用法：
#   sudo bash deploy/install.sh [安装目录]        # 安装（默认 /opt/mediapush）
#   sudo bash deploy/install.sh --uninstall [安装目录]   # 卸载（保留 data/）
#
# 行为：创建系统用户 mediapush → 拷贝代码到安装目录 → 建 venv 装依赖 →
#       渲染并安装 systemd unit → enable + start。
#
# 直装场景代理地址在 Web 后台填 127.0.0.1:<port>（宿主机本机代理）。
set -euo pipefail

SERVICE_NAME="mediapush"
SERVICE_USER="mediapush"
DEFAULT_INSTALL_DIR="/opt/mediapush"
UNIT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/mediapush.service"
UNIT_DST="/etc/systemd/system/${SERVICE_NAME}.service"

# 仓库根（install.sh 位于 deploy/ 下）
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

err() { echo "❌ $*" >&2; exit 1; }
log() { echo "▶ $*"; }

require_root() {
  [ "$(id -u)" -eq 0 ] || err "请以 root 运行（sudo bash deploy/install.sh）"
}

ensure_user() {
  if ! id "$SERVICE_USER" &>/dev/null; then
    log "创建系统用户 $SERVICE_USER"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
}

ensure_python() {
  command -v python3 >/dev/null || err "未找到 python3，请先安装 Python 3.11+"
  local py_ver
  py_ver="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  local major minor
  major="${py_ver%%.*}"; minor="${py_ver#*.}"
  [ "$major" -ge 4 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; } \
    || err "Python 版本 $py_ver 过低，需要 3.11+"
}

copy_code() {
  local dest="$1"
  log "拷贝代码到 $dest"
  mkdir -p "$dest"
  # tar 管道拷贝，排除 venv/git/data/缓存；可重复执行覆盖
  tar --exclude='./.venv' --exclude='./.git' --exclude='./data' \
      --exclude='./__pycache__' --exclude='./.pytest_cache' --exclude='./.ruff_cache' \
      -cf - -C "$REPO_DIR" . | tar -xf - -C "$dest"
  mkdir -p "$dest/data"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$dest"
}

make_venv() {
  local dest="$1"
  log "创建 venv 并安装依赖"
  runuser -u "$SERVICE_USER" -- python3 -m venv "$dest/.venv"
  runuser -u "$SERVICE_USER" -- "$dest/.venv/bin/pip" install --upgrade pip
  runuser -u "$SERVICE_USER" -- "$dest/.venv/bin/pip" install -r "$dest/requirements.txt"
}

render_unit() {
  local install_dir="$1"
  log "渲染 systemd unit"
  local tmp
  tmp="$(mktemp)"
  # 用 | 作 sed 分隔符，避免路径中的 / 冲突
  sed -e "s|__INSTALL_DIR__|${install_dir}|g" \
      -e "s|__USER__|${SERVICE_USER}|g" \
      "$UNIT_SRC" > "$tmp"
  install -m 0644 "$tmp" "$UNIT_DST"
  rm -f "$tmp"
}

do_install() {
  local install_dir="${1:-$DEFAULT_INSTALL_DIR}"
  require_root
  ensure_python
  ensure_user
  copy_code "$install_dir"
  make_venv "$install_dir"
  render_unit "$install_dir"

  log "启用并启动 $SERVICE_NAME"
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
  systemctl restart "$SERVICE_NAME"

  echo
  echo "✅ 安装完成：$install_dir"
  echo "   状态：sudo systemctl status $SERVICE_NAME"
  echo "   日志：sudo journalctl -u $SERVICE_NAME -f  （或 $install_dir/data/mediapush.log）"
  echo "   首次管理员密码：sudo journalctl -u $SERVICE_NAME | grep '管理员密码'"
  echo "   Web 后台：http://<host>:8088/login"
}

do_uninstall() {
  local install_dir="${1:-$DEFAULT_INSTALL_DIR}"
  require_root
  log "停止并禁用 $SERVICE_NAME"
  systemctl stop "$SERVICE_NAME" 2>/dev/null || true
  systemctl disable "$SERVICE_NAME" 2>/dev/null || true
  rm -f "$UNIT_DST"
  systemctl daemon-reload
  log "移除安装目录 $install_dir（保留 data/ 备份）"
  if [ -d "$install_dir/data" ]; then
    mv "$install_dir/data" "${install_dir}.data.bak.$(date +%s)"
  fi
  rm -rf "$install_dir"
  # 删除用户（保留其可能拥有的其他资源：仅当无进程占用）
  if id "$SERVICE_USER" &>/dev/null; then
    userdel "$SERVICE_USER" 2>/dev/null || log "用户 $SERVICE_USER 保留（仍有资源占用）"
  fi
  echo "✅ 已卸载；data/ 备份于 ${install_dir}.data.bak.*"
}

main() {
  case "${1:-}" in
    --uninstall)
      do_uninstall "${2:-}"
      ;;
    -h|--help)
      sed -n '2,12p' "${BASH_SOURCE[0]}"
      ;;
    ""|/*|.*)
      do_install "${1:-}"
      ;;
    *)
      err "未知参数：$1（用法：sudo bash deploy/install.sh [安装目录|--uninstall]）"
      ;;
  esac
}

main "$@"
