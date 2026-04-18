#!/bin/bash
# 运行报告生成，并将 sentinel_input.json 推送到 GitHub（供 Sentinel 直接读取）。
#
# 可选环境变量：
#   NARRATIVE_CONFIG  - config.yaml 路径（默认 config/config.yaml）
#   NARRATIVE_MAX     - 最多处理文章数

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"

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

"$PYTHON" -m scripts.generate_report --config "$CONFIG" $MAX_ARGS

# 更新 sentinel_input.json，推送到 GitHub 供 Sentinel 读取
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
  "updated": "$(date +%Y-%m-%d)"
}
EOF
fi

# 推送报告 + sentinel_input.json 到 GitHub
git add data/narrative/reports/ data/sentinel_input.json 2>/dev/null || true
git diff --cached --quiet || git commit -m "chore: narrative report $(date +%Y%m%d)"
git push origin main
