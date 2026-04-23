#!/bin/bash
# 运行报告生成，并将 sentinel_input.json 推送到 GitHub（供 Sentinel 直接读取）。
#
# 可选环境变量：
#   NARRATIVE_CONFIG  - config.yaml 路径（默认 config/config.yaml）
#   NARRATIVE_MAX     - 最多处理文章数

set -eo pipefail
# 先关闭 nounset，避免 source .bash_profile 时 iTerm2 等集成脚本报错
set +u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"

# ---------------------------------------------------------------------------
# 1. 加载用户环境变量（launchd 不会自动加载 ~/.bash_profile）
# ---------------------------------------------------------------------------
if [ -f "$HOME/.bash_profile" ]; then
    source "$HOME/.bash_profile"
fi
if [ -f "$HOME/.zshrc" ]; then
    source "$HOME/.zshrc" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 2. 频率控制：每 ~40 小时跑一次（近似每两三天）
# ---------------------------------------------------------------------------
LAST_RUN_FILE="data/.last_run"
CATCH_UP="false"
INTERVAL_HOURS=40
INTERVAL_SECS=$((INTERVAL_HOURS * 3600))

if [ -f "$LAST_RUN_FILE" ]; then
    LAST_RUN=$(cat "$LAST_RUN_FILE")
    NOW=$(date +%s)
    ELAPSED=$((NOW - LAST_RUN))
    if [ "$ELAPSED" -lt "$INTERVAL_SECS" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M')] 距离上次运行仅 $((ELAPSED / 3600)) 小时（目标间隔 ${INTERVAL_HOURS} 小时），本次跳过。"
        exit 0
    fi
    CATCH_UP="true"
    echo "[$(date '+%Y-%m-%d %H:%M')] 距离上次运行 $((ELAPSED / 3600)) 小时，超过 ${INTERVAL_HOURS} 小时阈值，开始执行。"
else
    echo "[$(date '+%Y-%m-%d %H:%M')] 首次运行，无历史记录。"
fi

# ---------------------------------------------------------------------------
# 3. 运行报告生成
# ---------------------------------------------------------------------------
CONFIG="${NARRATIVE_CONFIG:-config/config.yaml}"
MAX_ARGS=""
if [ -n "${NARRATIVE_MAX:-}" ]; then
    MAX_ARGS="--max ${NARRATIVE_MAX}"
fi

if [ -f "venv/bin/python3" ]; then
    PYTHON="venv/bin/python3"
else
    PYTHON="python3"
fi

echo "[$(date '+%Y-%m-%d %H:%M')] 开始生成叙事报告..."
"$PYTHON" -m scripts.generate_report --config "$CONFIG" $MAX_ARGS

# ---------------------------------------------------------------------------
# 4. 更新 sentinel_input.json
# ---------------------------------------------------------------------------
LATEST_REPORT=$(ls -t data/narrative/reports/*.md 2>/dev/null | head -1)
if [ -n "$LATEST_REPORT" ]; then
    REMOTE_URL=$(git remote get-url origin 2>/dev/null || echo "")
    if [ -n "$REMOTE_URL" ]; then
        REPO_PATH=$(echo "$REMOTE_URL" \
            | sed 's|git@github.com:||' \
            | sed 's|https://github.com/||' \
            | sed 's|\.git$||')
        RAW_BASE="https://raw.githubusercontent.com/${REPO_PATH}/main"
    else
        echo "警告：无法从 git remote 推断 repo URL，请手动更新 sentinel_input.json"
        RAW_BASE="https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_REPO/main"
    fi

    cat > data/sentinel_input.json << EOF
{
  "latest_report_url": "${RAW_BASE}/${LATEST_REPORT}",
  "updated": "$(date +%Y-%m-%d)",
  "catch_up": ${CATCH_UP}
}
EOF
fi

# ---------------------------------------------------------------------------
# 5. 推送报告到 GitHub
# ---------------------------------------------------------------------------
git add data/narrative/reports/ data/narrative/summaries/ data/sentinel_input.json 2>/dev/null || true
git diff --cached --quiet || git commit -m "chore: narrative report $(date +%Y%m%d)"
git push origin main

# ---------------------------------------------------------------------------
# 6. 记录成功运行时间戳
# ---------------------------------------------------------------------------
date +%s > "$LAST_RUN_FILE"
echo "[$(date '+%Y-%m-%d %H:%M')] 完成。"
