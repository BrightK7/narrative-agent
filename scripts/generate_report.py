#!/usr/bin/env python3
"""
叙事报告生成器（多 Agent 审议）。

用法：
  python -m scripts.generate_report              # 只处理新文章
  python -m scripts.generate_report --reset      # 清空已读记录，重新抓取
  python -m scripts.generate_report --max 5      # 最多处理 5 篇文章
  python -m scripts.generate_report --config path/to/config.yaml
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from src.narrative.rss import fetch_new_articles, mark_articles_seen, reset_seen
from src.narrative.agents import (
    run_proposer_draft,
    run_voters_critique,
    run_proposer_revision,
    cleanup_sessions,
    generate_summary,
    load_recent_summaries,
)

DEFAULT_CONFIG = Path("config/config.yaml")


def _load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_personas(config_dir: Path) -> list[tuple[str, str]]:
    personas_file = config_dir / "personas.yaml"
    if not personas_file.exists():
        raise FileNotFoundError(f"找不到 personas 文件: {personas_file}")
    data = yaml.safe_load(personas_file.read_text(encoding="utf-8"))
    return [(p["name"], p["description"].strip()) for p in data]


def _load_positions(positions_file: str) -> dict:
    if not positions_file:
        return {}
    p = Path(positions_file)
    if p.exists():
        return json.loads(p.read_text())
    return {}


def _build_report(articles, revised: str, run_at: str, cfg: dict) -> str:
    title = cfg.get("report", {}).get("title", "叙事分析报告")
    disclaimer = cfg.get("report", {}).get("disclaimer", "")
    lines = [
        f"# {title}",
        f"**生成时间**: {run_at}",
        f"**本期文章数**: {len(articles)}",
        "",
        "---",
        "## 本期文章",
    ]
    for a in articles:
        lines.append(f"- [{a.title}]({a.link})  *{a.published}*")

    lines += ["", "---", "", revised, ""]

    if disclaimer:
        lines += ["---", f"*{disclaimer}*"]
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="叙事报告生成器")
    p.add_argument("--reset",  action="store_true", help="清空已读记录")
    p.add_argument("--max",    type=int, default=None, help="最多处理的文章数")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="config.yaml 路径")
    args = p.parse_args()

    cfg = _load_config(args.config)
    config_dir = args.config.parent

    rss_cfg    = cfg.get("rss", {})
    out_cfg    = cfg.get("output", {})
    models_cfg = cfg.get("models", {})

    feed_url      = rss_cfg.get("feed_url", "")
    max_articles  = args.max or rss_cfg.get("max_articles", 10)
    seen_path     = Path(out_cfg.get("seen_articles_path", "data/narrative/seen_articles.json"))
    reports_dir   = Path(out_cfg.get("reports_dir", "data/narrative/reports"))
    summaries_dir = Path(out_cfg.get("summaries_dir", "data/narrative/summaries"))
    positions_file = out_cfg.get("positions_file", "")

    proposer_model = models_cfg.get("proposer", "claude-opus-4-6")
    voter_model    = models_cfg.get("voter", "claude-sonnet-4-6")
    summary_model  = models_cfg.get("summary", "claude-haiku-4-5-20251001")

    personas = _load_personas(config_dir)

    # RSS source name for prompt (derived from feed URL domain)
    from urllib.parse import urlparse
    source_name = urlparse(feed_url).netloc.replace("www.", "") if feed_url else "RSS"

    if args.reset:
        print("清空已读记录...")
        reset_seen(seen_path)

    print(f"正在拉取 {source_name} RSS（最多 {max_articles} 篇新文章）...")
    articles, new_article_ids = fetch_new_articles(feed_url, seen_path, max_articles)

    if not articles:
        print("没有新文章，本次跳过。")
        return

    print(f"发现 {len(articles)} 篇新文章，开始 3 轮审议...\n")
    positions      = _load_positions(positions_file)
    past_summaries = load_recent_summaries(summaries_dir, n=3)
    all_sids: list[str] = []

    if past_summaries:
        print(f"已加载最近 {len(past_summaries)} 份历史叙事摘要作为 Proposer 上下文。\n")

    # --- Round 1 ---
    print("Round 1 / Proposer 正在生成初稿...")
    draft, sid = run_proposer_draft(
        articles, positions, proposer_model,
        source_name=source_name, past_summaries=past_summaries,
    )
    all_sids.append(sid)
    print("  初稿完成。\n")

    # --- Round 2 ---
    print(f"Round 2 / {len(personas)} 个 Voter 并行批评...")
    critiques = run_voters_critique(draft, articles, personas, voter_model)
    all_sids.extend(c.session_id for c in critiques)
    for c in critiques:
        print(f"  Voter {c.voter_id} [{c.persona}] 批评完成。")
    print()

    # --- Round 3 ---
    print("Round 3 / Proposer 根据批评修改终稿...")
    revised, sid = run_proposer_revision(draft, critiques, proposer_model)
    all_sids.append(sid)
    print("  终稿完成。\n")

    # --- 保存报告 ---
    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report = _build_report(articles, revised, run_at, cfg)

    reports_dir.mkdir(parents=True, exist_ok=True)
    fname    = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M") + "_report.md"
    out_path = reports_dir / fname
    out_path.write_text(report, encoding="utf-8")
    print(f"报告已保存: {out_path}")

    mark_articles_seen(new_article_ids, seen_path)

    # --- 生成摘要 ---
    print("正在生成叙事摘要...")
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        summary = generate_summary(revised, run_date, summary_model, summaries_dir)
        print(f"叙事摘要已保存: {summaries_dir}/{run_date.replace('-', '')}.json")
        print(f"\n本期摘要:\n{summary['text']}\n")
    except Exception as e:
        print(f"[警告] 叙事摘要生成失败，报告已保存: {e}")

    # --- 清理 sessions ---
    deleted = cleanup_sessions(all_sids)
    print(f"已清理 {deleted} 个临时 session（共 {len(all_sids)} 个 ID）。")

    print("\n" + "-" * 60)
    preview = "\n".join(revised.splitlines()[:30])
    print(preview)
    if len(revised.splitlines()) > 30:
        print(f"\n... [完整报告请查看 {out_path}]")


if __name__ == "__main__":
    main()
