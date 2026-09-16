#!/usr/bin/env python3
"""Monitor Artale news and notify a Discord webhook about new posts."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


NEWS_URL = "https://artale.live/tw/news"
STATE_FILE = Path(__file__).with_name("seen.json")
START_DATE = date(2026, 9, 15)
ARTICLE_PATH_RE = re.compile(r"^/tw/news/([A-Za-z0-9_-]+)$")
DATE_RE = re.compile(r"\b20\d{2}\.\d{2}\.\d{2}(?:\s+\d{2}:\d{2})?\b")
MAX_SEEN = 500


def http_get(url: str, attempts: int = 3) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 Artale-News-Monitor/1.0",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        },
    )

    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError):
            if attempt == attempts:
                raise
            time.sleep(attempt * 2)

    raise RuntimeError("HTTP request failed unexpectedly")


class NewsListParser(HTMLParser):
    """Extract news rows from the server-rendered Artale news page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.current: dict[str, Any] | None = None
        self.stack: list[str] = []
        self.capture: tuple[int, str, list[str]] | None = None
        self.items: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)

        if tag == "a" and self.current is None:
            href = attributes.get("href")
            if not href:
                return

            full_url = urljoin(NEWS_URL, href)
            match = ARTICLE_PATH_RE.fullmatch(urlparse(full_url).path.rstrip("/"))
            if not match:
                return

            self.current = {
                "id": match.group(1),
                "url": full_url,
                "title": "",
                "category": "",
                "published": "",
            }
            self.stack = []
            self.capture = None
            return

        if self.current is None:
            return

        self.stack.append(tag)
        if tag != "span":
            return

        classes = set((attributes.get("class") or "").split())
        field = ""
        if "title" in classes:
            field = "title"
        elif "date" in classes:
            field = "published"
        elif "tag" in classes:
            field = "category"

        if field:
            self.capture = (len(self.stack), field, [])

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if self.current is None:
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.capture is not None:
            self.capture[2].append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return

        if tag == "a" and not self.stack:
            if self.current["title"]:
                self.items.append(self.current)
            self.current = None
            self.capture = None
            return

        if not self.stack:
            return

        if self.capture is not None and self.capture[0] == len(self.stack):
            _, field, parts = self.capture
            self.current[field] = re.sub(r"\s+", " ", "".join(parts)).strip()
            self.capture = None

        self.stack.pop()


class DetailParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_h1 = False
        self.current_h1: list[str] = []
        self.h1s: list[str] = []
        self.all_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "h1":
            self.in_h1 = True
            self.current_h1 = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        self.all_text.append(text)
        if self.in_h1:
            self.current_h1.append(text)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self.in_h1:
            title = " ".join(self.current_h1).strip()
            if title:
                self.h1s.append(title)
            self.in_h1 = False


def get_news_list() -> list[dict[str, str]]:
    parser = NewsListParser()
    parser.feed(http_get(NEWS_URL))

    unique_items: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for item in parser.items:
        if item["id"] in seen_ids:
            continue
        seen_ids.add(item["id"])
        unique_items.append(item)
    return unique_items


def enrich_news_detail(item: dict[str, str]) -> dict[str, str]:
    parser = DetailParser()
    parser.feed(http_get(item["url"]))

    for heading in reversed(parser.h1s):
        if heading != "公告":
            item["title"] = heading
            break

    date_match = DATE_RE.search(" ".join(parser.all_text))
    if date_match:
        item["published"] = date_match.group(0)
    return item


def parse_published_date(value: str) -> date | None:
    match = DATE_RE.search(value)
    if not match:
        return None
    try:
        year, month, day = match.group(0)[:10].split(".")
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def filter_from_start_date(news: list[dict[str, str]]) -> list[dict[str, str]]:
    eligible: list[dict[str, str]] = []
    for original_item in news:
        item = original_item
        published_date = parse_published_date(item.get("published", ""))
        if published_date is None:
            try:
                item = enrich_news_detail(item.copy())
            except Exception as error:
                raise RuntimeError(
                    f"無法確認公告日期：{item['url']} - {error}"
                ) from error
            published_date = parse_published_date(item.get("published", ""))

        if published_date is None:
            raise RuntimeError(f"公告沒有可辨識的發布日期：{item['url']}")

        if published_date >= START_DATE:
            eligible.append(item)

    return eligible


def load_seen() -> list[str]:
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(f"無法讀取 {STATE_FILE.name}: {error}") from error

    seen = data.get("seen", [])
    if not isinstance(seen, list) or not all(isinstance(value, str) for value in seen):
        raise RuntimeError(f"{STATE_FILE.name} 格式錯誤")
    return seen


def save_seen(seen: list[str]) -> None:
    unique_seen = list(dict.fromkeys(seen))[:MAX_SEEN]
    STATE_FILE.write_text(
        json.dumps({"seen": unique_seen}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def send_discord(item: dict[str, str]) -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise RuntimeError("找不到 GitHub Secret：DISCORD_WEBHOOK_URL")

    details = []
    if item.get("category"):
        details.append(f"**分類：** {item['category']}")
    if item.get("published"):
        details.append(f"**發布時間：** {item['published']}")

    payload = {
        "username": "Artale 公告通知",
        "content": "📢 **Artale 官方新公告**",
        "embeds": [
            {
                "title": item["title"][:256],
                "url": item["url"],
                "description": "\n".join(details),
                "color": 0xE67E22,
            }
        ],
        "allowed_mentions": {"parse": []},
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    for attempt in range(1, 4):
        request = Request(
            webhook_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Artale-News-Monitor/1.0",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                response.read()
            return
        except HTTPError as error:
            if error.code != 429 or attempt == 3:
                raise
            try:
                retry_after = float(json.loads(error.read()).get("retry_after", 1))
            except (ValueError, json.JSONDecodeError):
                retry_after = 1
            time.sleep(max(retry_after, 1))


def main() -> None:
    news = get_news_list()
    if not news:
        raise RuntimeError("沒有找到任何 Artale 公告，為避免遺失狀態已停止執行。")

    eligible_news = filter_from_start_date(news)
    old_seen = load_seen()
    seen_set = set(old_seen)
    new_items = [item for item in eligible_news if item["id"] not in seen_set]

    if not new_items:
        print(
            f"目前沒有新公告；監控範圍為 {START_DATE.isoformat()}（含）以後，"
            f"目前符合條件 {len(eligible_news)} 筆。"
        )
        return

    detailed_items: list[dict[str, str]] = []
    for item in new_items:
        try:
            detailed_items.append(enrich_news_detail(item.copy()))
        except Exception as error:
            print(f"讀取詳細資料失敗，改用列表資料：{item['url']} - {error}")
            detailed_items.append(item)

    detailed_items.sort(key=lambda value: value.get("published", ""))
    successful_ids: list[str] = []
    failures: list[str] = []

    for item in detailed_items:
        try:
            send_discord(item)
            successful_ids.append(item["id"])
            print(f"已通知：{item['title']}")
        except Exception as error:
            failures.append(item["id"])
            print(f"通知失敗：{item['url']} - {error}")

    if successful_ids:
        save_seen(successful_ids + old_seen)

    if failures:
        raise RuntimeError(f"有 {len(failures)} 筆公告通知失敗，稍後會自動重試。")


if __name__ == "__main__":
    main()
