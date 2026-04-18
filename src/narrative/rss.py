from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import feedparser
import requests
from bs4 import BeautifulSoup

REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; narrative-agent/1.0)"}


@dataclass
class Article:
    id: str
    title: str
    link: str
    published: str
    summary: str
    content: str


def _load_seen(seen_path: Path) -> set[str]:
    if seen_path.exists():
        return set(json.loads(seen_path.read_text()))
    return set()


def _save_seen(seen: set[str], seen_path: Path) -> None:
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    seen_path.write_text(json.dumps(sorted(seen), indent=2))


def _fetch_full_text(url: str, timeout: int = 15) -> str:
    try:
        r = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        body = soup.find("article") or soup.find("main") or soup.body
        if body:
            for tag in body.find_all(["nav", "footer", "script", "style", "aside"]):
                tag.decompose()
            return body.get_text(separator="\n", strip=True)[:8000]
    except Exception:
        pass
    return ""


def fetch_new_articles(
    feed_url: str,
    seen_path: Path,
    max_articles: int = 20,
) -> tuple[list[Article], set[str]]:
    """
    拉取 RSS，返回 (新文章列表, 本次新增的 uid 集合)。
    调用方在报告成功生成后再调用 mark_articles_seen()，避免失败时消耗文章。
    """
    seen = _load_seen(seen_path)
    feed = feedparser.parse(feed_url)
    articles: list[Article] = []
    new_ids: set[str] = set()

    for entry in feed.entries[:max_articles]:
        uid = entry.get("id") or entry.get("link", "")
        if uid in seen:
            continue

        title   = entry.get("title", "")
        link    = entry.get("link", "")
        pub     = entry.get("published", entry.get("updated", ""))
        summary = entry.get("summary", "")[:500]

        print(f"  抓取全文: {title[:60]}...")
        content = _fetch_full_text(link)
        time.sleep(0.5)

        articles.append(Article(
            id=uid, title=title, link=link,
            published=pub, summary=summary, content=content,
        ))
        new_ids.add(uid)

    return articles, new_ids


def mark_articles_seen(new_ids: set[str], seen_path: Path) -> None:
    seen = _load_seen(seen_path)
    seen.update(new_ids)
    _save_seen(seen, seen_path)


def reset_seen(seen_path: Path) -> None:
    _save_seen(set(), seen_path)
