"""
多 Agent 审议流程（3 轮）：
  Round 1: Proposer 生成初稿
  Round 2: N 个 Voter（不同 persona）并行给出批评意见
  Round 3: Proposer 根据批评修改，生成终稿

支持多 Provider（统一通过 claude -p CLI 调用）：
  - claude-*   → Anthropic 官方 API（消耗 Coding Plan 额度）
  - kimi-*     → Kimi Code 兼容 API（消耗 Kimi 额度）

模型名前缀决定底层 endpoint，同一脚本内可混合使用。

所有 claude -p session ID 统一收集，由调用方统一清理。
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CLAUDE_DIR = Path.home() / ".claude" / "projects"

# Kimi Code 兼容 Anthropic API 的 endpoint
KIMI_BASE_URL = "https://api.kimi.com/coding/"


# ---------------------------------------------------------------------------
# Session 管理
# ---------------------------------------------------------------------------

def cleanup_sessions(session_ids: list[str]) -> int:
    deleted = 0
    for sid in session_ids:
        if not sid:
            continue
        for f in glob.glob(str(CLAUDE_DIR / "*" / f"{sid}.jsonl")):
            try:
                Path(f).unlink()
                deleted += 1
            except OSError:
                pass
    return deleted


# ---------------------------------------------------------------------------
# Provider 路由
# ---------------------------------------------------------------------------

def _resolve_provider(model: str) -> str:
    """根据模型名前缀判断 provider。"""
    model_lower = model.lower()
    if model_lower.startswith("claude-"):
        return "claude"
    if model_lower.startswith("kimi-"):
        return "kimi"
    # 默认 fallback，保持向后兼容
    return "claude"


# ---------------------------------------------------------------------------
# 统一底层调用（通过 claude -p，根据 provider 切换 endpoint）
# ---------------------------------------------------------------------------

def _call_llm(prompt: str, model: str, timeout: int = 300) -> tuple[str, str]:
    """统一调用入口。根据模型名自动选择 endpoint，全部通过 claude -p 执行。

    返回 (text, session_id)。
    """
    provider = _resolve_provider(model)

    # 构造环境变量：根据 provider 切换 API endpoint
    env = os.environ.copy()
    if provider == "kimi":
        kimi_key = env.get("KIMI_API_KEY", "")
        if not kimi_key:
            raise RuntimeError(
                "调用 Kimi 模型需要设置环境变量 KIMI_API_KEY"
            )
        env["ANTHROPIC_BASE_URL"] = KIMI_BASE_URL
        env["ANTHROPIC_API_KEY"] = kimi_key
    else:
        # Claude：移除可能指向其他 provider 的 base_url 和 api_key，
        # 让 claude -p 使用 Claude Code 内部认证（coding plan）
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("ANTHROPIC_API_KEY", None)

    claude_bin = Path("/opt/homebrew/bin/claude")
    cmd = [
        str(claude_bin), "-p", "-",
        "--model", model,
        "--output-format", "json",
        "--dangerously-skip-permissions",
    ]
    result = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True,
        timeout=timeout, env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p 失败 (provider={provider}, model={model}, "
            f"rc={result.returncode}):\n"
            f"stderr: {result.stderr[:500]}\n"
            f"stdout: {result.stdout[:500]}"
        )

    session_id = ""
    try:
        resp = json.loads(result.stdout.strip())
        text = resp.get("result", "")
        session_id = resp.get("session_id", "")
    except json.JSONDecodeError:
        text = result.stdout.strip()

    return text.strip(), session_id


# ---------------------------------------------------------------------------
# Round 1：Proposer 初稿
# ---------------------------------------------------------------------------

_PROPOSER_DRAFT_PROMPT = """\
你是一位加密货币长期投资分析师。
你会收到一组来自 {source_name} 的最新文章，以及当前的持仓状态。
{history_context}
综合阅读这些文章，生成一份投资叙事分析初稿，格式如下：

## 本期核心叙事
（2-3 段，描述这批文章传递的主要市场观点和趋势）

## 被看多的资产
（列出文章中被正面讨论的资产，附理由）

## 被看空 / 需警惕的资产
（列出文章中被负面讨论或提示风险的资产，附理由）

## 叙事强度评估
（整体叙事的一致性和确信度，1-10 分，附说明）

## 对当前持仓的影响
（根据叙事，现有持仓是否需要调整？仅分析，不下指令。若无持仓信息则跳过此节）

## 值得关注的信号
（未来 2-4 周需要持续观察的指标或事件）

保持客观，区分"文章观点"和"你的判断"。

{positions}

以下是本期文章：
{articles}"""


def _format_positions(positions: dict) -> str:
    if not positions:
        return "当前持仓：无"
    lines = ["当前持仓（请在分析中重点关注这些资产的风险与机会）："]
    for asset, info in positions.get("holdings", {}).items():
        line = f"- {asset}: {info.get('type', '')}"
        if "liquidation_price" in info:
            line += f"，强平价 ${info['liquidation_price']}"
        if info.get("note"):
            line += f"，{info['note']}"
        lines.append(line)
    reserve = positions.get("usdt_reserve", {})
    if reserve:
        lines.append(
            f"- USDT 储备金: {reserve.get('amount', 0)} U（{reserve.get('note', '')}）"
        )
    return "\n".join(lines)


def run_proposer_draft(
    articles: list,
    current_positions: dict,
    proposer_model: str,
    source_name: str = "RSS",
    past_summaries: list[dict] | None = None,
) -> tuple[str, str]:
    articles_text = "".join(
        f"\n---\n### 文章 {i}: {a.title}\n链接: {a.link}\n发布: {a.published}\n\n"
        f"{a.content if a.content else a.summary}\n"
        for i, a in enumerate(articles, 1)
    )
    positions_text = _format_positions(current_positions)

    if past_summaries:
        lines = ["**历史叙事摘要（仅供参考，判断叙事延续还是转变）**："]
        for s in past_summaries:
            lines.append(f"\n[{s['date']}]\n{s['text']}")
        history_context = "\n".join(lines) + "\n\n"
    else:
        history_context = ""

    prompt = _PROPOSER_DRAFT_PROMPT.format(
        source_name=source_name,
        history_context=history_context,
        positions=positions_text,
        articles=articles_text,
    )
    return _call_llm(prompt, model=proposer_model, timeout=300)


# ---------------------------------------------------------------------------
# Round 2：Voter 批评
# ---------------------------------------------------------------------------

_VOTER_CRITIQUE_TASK = """
根据你查询到的最新数据和你的专业视角，审核上述分析初稿，给出具体的修改意见。

【事实核查纪律——必须遵守】
- 对于具体日期（如 FOMC 时间）、法律条款、统计数据、价格数据等可验证硬事实：
  → 如果不确定，标注"[待验证]"而非直接"纠正"
  → 如果初稿中的事实与你的记忆冲突，优先信任初稿（初稿可能基于更新鲜的数据）
  → 只有通过 web_search 找到明确反证后，才能提出事实性修正
- 严禁凭记忆"纠正"具体日期、人名、法律条款、金额等硬事实

格式：
**查询到的关键数据**:（最多 3 条，附来源）
**遗漏的关键分析**:（最多 3 条）
**需要修正的判断**:（最多 3 条；如涉及事实性修正，必须附 web_search 来源）
**建议补充的视角**:（最多 2 条）

直接给出具体意见，供分析师参考修改。不需要给出 approve/reject 判断。"""


@dataclass
class CritiqueResult:
    voter_id: int
    persona: str
    critique: str
    session_id: str = ""


def _run_single_critique(
    voter_id: int, persona_name: str, persona_desc: str,
    draft: str, articles_summary: str, voter_model: str,
) -> CritiqueResult:
    prompt = (
        f"{persona_desc}\n\n"
        f"以下是分析师的初稿：\n{draft}\n\n"
        f"文章摘要（供参考）：\n{articles_summary}\n\n"
        f"{_VOTER_CRITIQUE_TASK}"
    )
    text, sid = _call_llm(prompt, model=voter_model, timeout=600)
    return CritiqueResult(voter_id=voter_id, persona=persona_name,
                          critique=text, session_id=sid)


def run_voters_critique(
    draft: str,
    articles: list,
    voter_personas: list[tuple[str, str]],
    voter_model: str,
) -> list[CritiqueResult]:
    articles_summary = "\n".join(
        f"- [{a.title}]({a.link}): {a.summary[:200]}" for a in articles
    )
    results: list[CritiqueResult] = []
    with ThreadPoolExecutor(max_workers=len(voter_personas)) as executor:
        futures = {
            executor.submit(
                _run_single_critique,
                i + 1, name, desc, draft, articles_summary, voter_model,
            ): i
            for i, (name, desc) in enumerate(voter_personas)
        }
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda r: r.voter_id)
    return results


# ---------------------------------------------------------------------------
# Round 3：Proposer 修改终稿
# ---------------------------------------------------------------------------

_PROPOSER_REVISION_PROMPT = """\
你是一位加密货币长期投资分析师。

你之前写了一份市场分析初稿，现在收到了 {n_voters} 位不同视角的评审专家的批评意见。
请认真阅读所有批评，然后修改并完善你的报告。

修改要求：
- 吸收合理的批评，补充遗漏的分析
- 修正有偏差的判断
- 如果你认为某条批评不合理，可以在报告中注明并解释理由
- 保持原有结构，但内容应明显比初稿更全面、更严谨

---
【初稿】
{draft}

{critiques_block}

---
请输出修改后的完整报告（保持 Markdown 格式，包含所有章节）："""


def run_proposer_revision(
    draft: str,
    critiques: list[CritiqueResult],
    proposer_model: str,
) -> tuple[str, str]:
    critique_sections = "\n\n".join(
        f"---\n【{c.persona}专家的批评】\n{c.critique}"
        for c in critiques
    )
    prompt = _PROPOSER_REVISION_PROMPT.format(
        n_voters=len(critiques),
        draft=draft,
        critiques_block=critique_sections,
    )
    return _call_llm(prompt, model=proposer_model, timeout=300)


# ---------------------------------------------------------------------------
# 叙事摘要
# ---------------------------------------------------------------------------

_SUMMARIZE_PROMPT = """\
你是一位加密市场叙事分析师。请将下面这份多 Agent 审议后的市场报告，压缩成 **恰好三句话**，格式固定如下：

**核心结论**: （本期最重要的市场观点或趋势转变，一句话）
**关键风险**: （当前最需要警惕的尾部风险，一句话）
**待验证信号**: （未来 2-4 周需要持续观察以验证叙事的关键指标或事件，一句话）

不需要其他内容，直接输出三行。

---
{report}"""


def generate_summary(
    report: str,
    run_date: str,
    summary_model: str,
    summaries_dir: Path,
) -> dict:
    prompt = _SUMMARIZE_PROMPT.format(report=report[:6000])
    text, sid = _call_llm(prompt, model=summary_model, timeout=300)
    if sid:
        cleanup_sessions([sid])

    summary = {"date": run_date, "text": text.strip()}

    summaries_dir.mkdir(parents=True, exist_ok=True)
    date_key = run_date.replace("-", "")[:8]
    out = summaries_dir / f"{date_key}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def load_recent_summaries(summaries_dir: Path, n: int = 3) -> list[dict]:
    if not summaries_dir.exists():
        return []
    files = sorted(summaries_dir.glob("*.json"))[-n:]
    summaries = []
    for f in files:
        try:
            summaries.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return summaries
