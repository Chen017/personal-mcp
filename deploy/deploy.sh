#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Chen017/personal-mcp.git}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/personal-mcp}"
RUN_USER="${DEPLOY_USER:-${SUDO_USER:-$(id -un)}}"

if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
elif command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
else
    echo "[ERROR] Please run as root or install sudo first." >&2
    exit 1
fi

RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6 || true)"
if [ -z "$RUN_HOME" ]; then
    RUN_HOME="$(eval echo "~$RUN_USER")"
fi

run_as_user() {
    if [ "$(id -un)" = "$RUN_USER" ]; then
        "$@"
    else
        $SUDO -u "$RUN_USER" -H "$@"
    fi
}

echo "==> [1/6] Checking system prerequisites (git, curl, openssl)..."
MISSING_PKGS=()
for cmd in git curl openssl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        MISSING_PKGS+=("$cmd")
    fi
done
if [ "${#MISSING_PKGS[@]}" -gt 0 ]; then
    if command -v apt-get >/dev/null 2>&1; then
        $SUDO apt-get update -qq
        $SUDO apt-get install -y -qq "${MISSING_PKGS[@]}" ca-certificates
    else
        echo "[ERROR] Missing required tools: ${MISSING_PKGS[*]}" >&2
        exit 1
    fi
fi

echo "==> [2/6] Syncing repository into ${INSTALL_DIR} (user: ${RUN_USER})..."
$SUDO mkdir -p "$INSTALL_DIR"
$SUDO chown -R "$RUN_USER:$RUN_USER" "$INSTALL_DIR"

if [ -d "${INSTALL_DIR}/.git" ]; then
    run_as_user git -C "$INSTALL_DIR" fetch origin "$BRANCH"
    run_as_user git -C "$INSTALL_DIR" checkout "$BRANCH"
    run_as_user git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
else
    if [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null || true)" ]; then
        echo "[ERROR] ${INSTALL_DIR} is not empty and not a git repo." >&2
        exit 1
    fi
    run_as_user git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

echo "==> [3/6] Installing uv & Python dependencies..."
UV_BIN="$(command -v uv || true)"
if [ -z "$UV_BIN" ] && [ -x "${RUN_HOME}/.local/bin/uv" ]; then
    UV_BIN="${RUN_HOME}/.local/bin/uv"
fi
if [ -z "$UV_BIN" ]; then
    run_as_user bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
    UV_BIN="${RUN_HOME}/.local/bin/uv"
fi
if [ ! -x "$UV_BIN" ]; then
    echo "[ERROR] uv installation failed; expected at ${UV_BIN}" >&2
    exit 1
fi

run_as_user bash -c "cd $(printf '%q' "$INSTALL_DIR") && $(printf '%q' "$UV_BIN") sync --frozen || $(printf '%q' "$UV_BIN") sync"

echo "==> [4/6] Configuring .env..."
ENV_FILE="${INSTALL_DIR}/.env"
if [ ! -f "$ENV_FILE" ]; then
    run_as_user cp "${INSTALL_DIR}/.env.example" "$ENV_FILE"
    INTERNAL_TOKEN="$(openssl rand -hex 32)"
    SECRET_NOTES="$(openssl rand -hex 32)"
    SECRET_GITHUB="$(openssl rand -hex 32)"

    sed -i "s|replace_with_same_64_hex_as_php_config|${INTERNAL_TOKEN}|g" "$ENV_FILE"
    sed -i "s|replace_with_64_hex_chars_1|${SECRET_NOTES}|g" "$ENV_FILE"
    sed -i "s|replace_with_64_hex_chars_2|${SECRET_GITHUB}|g" "$ENV_FILE"

    if [ "${NON_INTERACTIVE:-0}" != "1" ] && [ -r /dev/tty ]; then
        printf "Enter your GitHub username (GITHUB_DEFAULT_OWNER, leave blank to fill later): " > /dev/tty
        IFS= read -r INPUT_OWNER < /dev/tty || true
        if [ -n "${INPUT_OWNER:-}" ]; then
            sed -i "s|^GITHUB_DEFAULT_OWNER=.*|GITHUB_DEFAULT_OWNER=${INPUT_OWNER}|" "$ENV_FILE"
            sed -i "s|^GITHUB_ALLOWED_WRITE_OWNERS=.*|GITHUB_ALLOWED_WRITE_OWNERS=${INPUT_OWNER}|" "$ENV_FILE"
        fi
        printf "Enter FIRECRAWL_API_KEY (leave blank to fill later): " > /dev/tty
        IFS= read -r INPUT_FC < /dev/tty || true
        if [ -n "${INPUT_FC:-}" ]; then
            sed -i "s|^FIRECRAWL_API_KEY=.*|FIRECRAWL_API_KEY=${INPUT_FC}|" "$ENV_FILE"
        fi
        printf "Enter GITHUB_PAT (leave blank to fill later): " > /dev/tty
        IFS= read -r INPUT_GH < /dev/tty || true
        if [ -n "${INPUT_GH:-}" ]; then
            sed -i "s|^GITHUB_PAT=.*|GITHUB_PAT=${INPUT_GH}|" "$ENV_FILE"
        fi
    fi
    chmod 600 "$ENV_FILE"
    $SUDO chown "$RUN_USER:$RUN_USER" "$ENV_FILE"
else
    echo "    Existing ${ENV_FILE} detected; preserving all secrets."
    chmod 600 "$ENV_FILE"
fi

echo "==> [5/6] Installing and starting systemd services..."
for svc in mcp-notes-web.service mcp-github.service; do
    SRC="${INSTALL_DIR}/deploy/systemd/${svc}"
    DST="/etc/systemd/system/${svc}"
    TMP_SVC="$(mktemp)"
    sed -e "s|YOUR_DEBIAN_USER|${RUN_USER}|g" -e "s|/opt/personal-mcp|${INSTALL_DIR}|g" "$SRC" > "$TMP_SVC"
    $SUDO install -m 644 "$TMP_SVC" "$DST"
    rm -f "$TMP_SVC"
done

$SUDO systemctl daemon-reload
$SUDO systemctl enable mcp-notes-web mcp-github
$SUDO systemctl restart mcp-notes-web mcp-github

echo "==> [6/6] Running stateless Streamable HTTP health checks..."
check_endpoint() {
    local name="$1"
    local port="$2"
    local svc="$3"
    local code="000"
    for _ in $(seq 1 15); do
        code="$(curl -s -o /dev/null -w "%{http_code}" \
            -X POST "http://127.0.0.1:${port}/mcp" \
            -H "Content-Type: application/json" \
            -H "Accept: application/json, text/event-stream" \
            -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' || true)"
        if [ "$code" = "200" ]; then
            echo "    [OK] ${name} listening on 127.0.0.1:${port}/mcp (HTTP 200)"
            return 0
        fi
        sleep 1
    done
    echo "[ERROR] ${name} health check failed on port ${port} (last HTTP=${code}). Recent logs:" >&2
    $SUDO journalctl -u "$svc" -n 30 --no-pager >&2 || true
    return 1
}

check_endpoint "Personal Notes Web" 8101 "mcp-notes-web"
check_endpoint "GitHub Personal" 8102 "mcp-github"

get_env_val() {
    local key="$1"
    grep -E "^${key}=" "$ENV_FILE" | head -n 1 | cut -d'=' -f2- || true
}

SECRET_NOTES_VAL="$(get_env_val SECRET_PATH_NOTES)"
SECRET_GITHUB_VAL="$(get_env_val SECRET_PATH_GITHUB)"
INTERNAL_TOKEN_VAL="$(get_env_val NOTES_INTERNAL_TOKEN)"
FC_VAL="$(get_env_val FIRECRAWL_API_KEY)"
GH_VAL="$(get_env_val GITHUB_PAT)"
OWNER_VAL="$(get_env_val GITHUB_DEFAULT_OWNER)"

echo ""
echo "======================================================================"
echo " Deployment succeeded! Summary & Next Steps:"
echo "======================================================================"
echo "1) Service status:"
echo "   sudo systemctl status mcp-notes-web mcp-github"
echo ""
if [[ "$FC_VAL" == *"xxxx"* || "$GH_VAL" == *"xxxx"* || "$OWNER_VAL" == "your-github-username" ]]; then
    echo "2) IMPORTANT - Fill remaining placeholders in ${ENV_FILE}, then restart:"
    echo "   nano ${ENV_FILE}"
    echo "   sudo systemctl restart mcp-notes-web mcp-github"
    echo ""
fi
echo "3) Optional SiYuan Share php-site/config.php (add inside return [...]; and restart container):"
echo "   'mcp_internal_token' => '${INTERNAL_TOKEN_VAL}',"
echo "   'mcp_internal_user_id' => 1,"
echo ""
echo "4) Gemini Custom Apps URLs (replace mcp.example.com with your domain):"
echo "   - Personal Notes Web: https://mcp.example.com/${SECRET_NOTES_VAL}/mcp"
echo "   - GitHub Personal:    https://mcp.example.com/${SECRET_GITHUB_VAL}/mcp"
echo ""
if command -v caddy >/dev/null 2>&1; then
    echo "5) Detected Caddy — ready-to-use Caddyfile snippet:"
    echo "----------------------------------------------------------------------"
    sed -e "s|<SECRET_PATH_NOTES>|${SECRET_NOTES_VAL}|g" \
        -e "s|<SECRET_PATH_GITHUB>|${SECRET_GITHUB_VAL}|g" \
        "${INSTALL_DIR}/deploy/caddy/Caddyfile.example"
elif command -v nginx >/dev/null 2>&1; then
    echo "5) Detected Nginx — ready-to-use server block:"
    echo "----------------------------------------------------------------------"
    sed -e "s|<SECRET_PATH_NOTES>|${SECRET_NOTES_VAL}|g" \
        -e "s|<SECRET_PATH_GITHUB>|${SECRET_GITHUB_VAL}|g" \
        "${INSTALL_DIR}/deploy/nginx/mcp.example.com.conf"
else
    echo "5) Reverse proxy templates are located under ${INSTALL_DIR}/deploy/{nginx,caddy}/"
fi
echo "======================================================================"
