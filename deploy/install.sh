#!/usr/bin/env bash
# One-shot install for a Linux server with systemd.
#   sudo bash -c "curl -fsSL https://raw.githubusercontent.com/hrf100/pm5m/main/deploy/install.sh | bash"
#   sudo bash deploy/install.sh
#   sudo bash deploy/install.sh --domain pm.example.com
set -euo pipefail
umask 022

DEST=/opt/pm5m
ENV_FILE=/etc/pm5m.env
REPO_URL="${PM5M_REPO:-https://github.com/hrf100/pm5m.git}"
REPO_REF="${PM5M_REF:-main}"
INSTALL_URL="https://raw.githubusercontent.com/hrf100/pm5m/main/deploy/install.sh"
DOMAIN=""
CLEAN_SRC=""

SELF=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SELF=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")
fi

if [[ "$(id -u)" -ne 0 ]]; then
  if [[ -n "$SELF" ]] && command -v sudo >/dev/null 2>&1; then
    exec sudo bash "$SELF" "$@"
  fi
  echo "需要 root。在服务器上执行:" >&2
  echo "  sudo bash -c \"curl -fsSL ${INSTALL_URL} | bash\"" >&2
  exit 1
fi

usage() {
  cat <<EOF
用法:
  sudo bash -c "curl -fsSL ${INSTALL_URL} | bash"
  sudo bash -c "curl -fsSL ${INSTALL_URL} | bash -s -- --domain pm.example.com"
  sudo bash deploy/install.sh
  sudo bash deploy/install.sh --domain pm.example.com

不带 --domain：应用只监听 127.0.0.1:8080，用 SSH 隧道访问。
带 --domain：额外安装 Caddy，公网 HTTPS 反代到本机 8080。域名 A 记录需已指向这台机器。
重复执行会从 ${REPO_URL} 的 ${REPO_REF} 升级代码和依赖，不会覆盖已有 /etc/pm5m.env 里的密码和私钥。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain)
      [[ $# -ge 2 ]] || { echo "缺少域名" >&2; exit 1; }
      DOMAIN=$2
      shift 2
      ;;
    --domain=*)
      DOMAIN=${1#*=}
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -n "$DOMAIN" && ! "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$ ]]; then
  echo "域名不合法: $DOMAIN" >&2
  exit 1
fi

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "只支持 Linux 服务器。" >&2
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1 || [[ ! -d /run/systemd/system ]]; then
  echo "需要 systemd 作为 1 号进程（常规 VPS）。没有 systemd 的容器不能用这个脚本。" >&2
  exit 1
fi

SRC=""
if [[ -n "$SELF" ]]; then
  SRC=$(cd "$(dirname "$SELF")/.." && pwd -P)
fi

install_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y python3 python3-venv python3-pip ca-certificates curl git
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 python3-pip ca-certificates curl git
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 python3-pip ca-certificates curl git
  else
    echo "需要 apt-get、dnf 或 yum。" >&2
    exit 1
  fi
}

pick_python() {
  local c
  for c in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

echo "==> 安装系统包"
install_packages
if command -v timedatectl >/dev/null 2>&1; then
  timedatectl set-ntp true || true
fi

if [[ -z "$SRC" || ! -f "$SRC/requirements.txt" || ! -d "$SRC/pm5m" ]]; then
  echo "==> 从 ${REPO_URL} 克隆 (${REPO_REF})"
  if ! command -v git >/dev/null 2>&1; then
    echo "没找到 git，系统包安装后仍不可用。" >&2
    exit 1
  fi
  CLEAN_SRC=$(mktemp -d)
  git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$CLEAN_SRC"
  SRC=$CLEAN_SRC
fi

PY=$(pick_python) || {
  echo "需要 Python >= 3.9。当前 python3 太旧，请先安装 python3.11 再重跑。" >&2
  exit 1
}
echo "==> 使用 $($PY -V)"

echo "==> 系统用户 pm5m"
if ! getent group pm5m >/dev/null 2>&1; then
  groupadd --system pm5m
fi
if ! id pm5m >/dev/null 2>&1; then
  useradd --system --gid pm5m --home-dir /var/lib/pm5m --shell /usr/sbin/nologin pm5m
fi
install -d -o pm5m -g pm5m -m 700 /var/lib/pm5m

echo "==> 安装代码到 $DEST"
if [[ "$SRC" != "$DEST" ]]; then
  install -d -m 755 "$DEST"
  tar -C "$SRC" \
    --exclude .venv \
    --exclude data \
    --exclude __pycache__ \
    --exclude .git \
    --exclude tests \
    -cf - . | tar -C "$DEST" -xf -
fi

echo "==> Python 虚拟环境与依赖"
if [[ ! -x "$DEST/.venv/bin/python" ]]; then
  "$PY" -m venv "$DEST/.venv"
fi
if [[ ! -x "$DEST/.venv/bin/pip" ]]; then
  "$DEST/.venv/bin/python" -m ensurepip --upgrade
fi
"$DEST/.venv/bin/pip" install --disable-pip-version-check --upgrade pip
"$DEST/.venv/bin/pip" install --disable-pip-version-check -r "$DEST/requirements.txt"
chown -R root:root "$DEST"
chmod -R a+rX "$DEST"

echo "==> 导入自检"
IMPORT_CMD="cd '$DEST' && env PYTHONDONTWRITEBYTECODE=1 HOME=/var/lib/pm5m '$DEST/.venv/bin/python' -c 'import pm5m, aiohttp'"
if command -v runuser >/dev/null 2>&1; then
  runuser -u pm5m -- bash -c "$IMPORT_CMD"
else
  su -s /bin/bash pm5m -c "$IMPORT_CMD"
fi

CREATED_ENV=0
TRUST=0
if [[ -n "$DOMAIN" ]]; then
  TRUST=1
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "==> 写入 $ENV_FILE"
  PASS=$("$DEST/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(18))')
  ( umask 077; cat > "$ENV_FILE" <<EOF
# generated by deploy/install.sh — chmod 600, root:root
PM5M_PASSWORD=${PASS}
PM5M_HOST=127.0.0.1
PM5M_PORT=8080
PM5M_TRUST_PROXY=${TRUST}
PM5M_ALLOW_WEB_KEY=0
PM5M_IP_ALLOWLIST=
PM5M_DATA=/var/lib/pm5m
PM5M_SIG_TYPE=2
# PM5M_PRIVATE_KEY=
# PM5M_FUNDER=
# PM5M_SECRET_PASSPHRASE=
EOF
  )
  chown root:root "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  CREATED_ENV=1
else
  echo "==> 保留已有 $ENV_FILE"
  if [[ -n "$DOMAIN" ]]; then
    if grep -q '^PM5M_TRUST_PROXY=' "$ENV_FILE"; then
      sed -i 's/^PM5M_TRUST_PROXY=.*/PM5M_TRUST_PROXY=1/' "$ENV_FILE"
    else
      echo 'PM5M_TRUST_PROXY=1' >> "$ENV_FILE"
    fi
  fi
fi

PORT=$(sed -n 's/^PM5M_PORT=//p' "$ENV_FILE" | tail -n 1)
PORT=${PORT:-8080}

if [[ -n "$DOMAIN" ]]; then
  echo "==> Caddy ($DOMAIN)"
  if ! command -v caddy >/dev/null 2>&1; then
    if ! command -v apt-get >/dev/null 2>&1; then
      echo "未安装 Caddy，且系统不是 apt。请先装好 Caddy 再重跑。" >&2
      exit 1
    fi
    apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
    install -d -m 755 /usr/share/keyrings
    keytmp=$(mktemp)
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor > "$keytmp"
    mv "$keytmp" /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
      > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update
    apt-get install -y caddy
  fi
  CADDY_FILE=/etc/caddy/Caddyfile
  WRITE_CADDY=0
  if [[ ! -f "$CADDY_FILE" ]]; then
    WRITE_CADDY=1
  elif grep -q 'managed by pm5m' "$CADDY_FILE"; then
    WRITE_CADDY=1
  elif grep -q 'file_server' "$CADDY_FILE" && grep -q ':80' "$CADDY_FILE"; then
    WRITE_CADDY=1
  fi
  if [[ "$WRITE_CADDY" -eq 1 ]]; then
    cat > "$CADDY_FILE" <<EOF
# managed by pm5m deploy/install.sh
${DOMAIN} {
    encode gzip
    reverse_proxy 127.0.0.1:${PORT} {
        header_up X-Forwarded-Proto https
    }
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options nosniff
        Referrer-Policy no-referrer
        X-Frame-Options DENY
        -Server
    }
    @other not path / /api/*
    respond @other 404
}
EOF
    systemctl enable caddy
    systemctl reload caddy 2>/dev/null || systemctl restart caddy
  else
    echo "已有自定义 $CADDY_FILE，未覆盖。请手工把 ${DOMAIN} 反代到 127.0.0.1:${PORT}。" >&2
  fi
fi

echo "==> systemd"
install -m 644 "$SRC/deploy/pm5m.service" /etc/systemd/system/pm5m.service
systemctl daemon-reload
systemctl enable pm5m
systemctl restart pm5m

echo "==> 等待监听 127.0.0.1:${PORT}"
CODE=""
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:${PORT}/" || true)
  if [[ "$CODE" == "401" || "$CODE" == "200" ]]; then
    break
  fi
  sleep 1
done
if [[ "$CODE" != "401" && "$CODE" != "200" ]]; then
  echo "服务没有在 127.0.0.1:${PORT} 返回 401/200（实际: ${CODE:-无响应}）。" >&2
  journalctl -u pm5m -n 60 --no-pager >&2 || true
  exit 1
fi

echo
echo "pm5m 已启动。应用只监听 127.0.0.1:${PORT}，用户名 admin。"
if [[ "$CREATED_ENV" -eq 1 ]]; then
  echo "网页密码: ${PASS}"
  echo "已写入 ${ENV_FILE} 的 PM5M_PASSWORD（权限 600）。"
else
  echo "密码仍是 ${ENV_FILE} 里的 PM5M_PASSWORD。"
fi
echo "私钥: 编辑 ${ENV_FILE} 填入 PM5M_PRIVATE_KEY 和 PM5M_FUNDER，然后 sudo systemctl restart pm5m"
echo "日志: journalctl -u pm5m -f"
if [[ -n "$DOMAIN" ]]; then
  echo "公网: https://${DOMAIN}  （确认 DNS 已指向本机，防火墙放行 22/80/443，不要开放 ${PORT}）"
else
  echo "访问: ssh -L ${PORT}:127.0.0.1:${PORT} <user>@<server> ，浏览器打开 http://127.0.0.1:${PORT}"
fi
if [[ -n "$CLEAN_SRC" ]]; then
  rm -rf "$CLEAN_SRC"
fi
