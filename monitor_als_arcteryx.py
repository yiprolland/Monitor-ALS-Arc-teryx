#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ALS.com Arc'teryx 监控（精简稳定版 / 无任何“上新过多”抑制逻辑）
仅监控：
  1) 上新（新商品/新变体）
  2) 价格变化
  3) 仅提醒“缺货 → 到货”

保证：
  - 只通知有变化的商品（绝不推送无变化）
  - 一个商品一条通知（同一商品多种变化合并）
  - 稳定 key = PDP 规范化 URL 的 slug（/xxx/p ⇒ xxx）
  - 快照原子写入

Env:
  DISCORD_WEBHOOK_URL   必填：Discord Webhook
  HEADLESS=0/1          可选：本地 0，CI 1（默认 1）
  KEYWORD_FILTER        可选：只监控标题包含该关键词（不区分大小写）
  NOTIFY_INTERVAL_SEC   可选：每条通知间隔，默认 0.1 秒
"""

import json
import os
import re
import sys
import time
import math
import random
import shutil
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Dict, Any, List, Tuple, Set
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

COLLECTION_URL = "https://www.als.com/arc-teryx"
SNAPSHOT_PATH = Path("snapshot.json")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# --------------------------
# 基础工具
# --------------------------

def jdump(obj: Any, path: Path) -> None:
    """原子写文件，避免半写入导致快照损坏。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile('w', delete=False, encoding='utf-8', dir=str(path.parent)) as tmp:
        json.dump(obj, tmp, ensure_ascii=False, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name
    try:
        shutil.move(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except Exception:
            pass


def jload(path: Path) -> Dict[str, Any]:
    if not path.exists():
        print(f"[snapshot] {path} not found.")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        print(f"[snapshot] loaded {len(data)} items from {path}.")
        return data
    except Exception as e:
        print(f"[snapshot] failed to parse {path}: {e}")
        return {}


def safe_sleep(a: float = 0.06, b: float = 0.18) -> None:
    time.sleep(random.uniform(a, b))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def slug_from_pdp_url(u: str) -> str:
    """从 PDP URL 取 slug：/arcteryx-xxx/p -> arcteryx-xxx"""
    try:
        p = urlparse(u)
        path = (p.path or "").lower()
        m = re.search(r"/([^/]+)/p(?:$|/|\?|#)", path)
        return m.group(1) if m else path.strip("/").split("/")[-1]
    except Exception:
        u2 = (u or "").split("?")[0].split("#")[0].lower()
        return u2.strip("/").split("/")[-1]


def stable_key_from_url(u: str) -> str:
    """稳定 key：仅用 PDP slug，避免标题/SKU/颜色抖动。"""
    slug = slug_from_pdp_url(u)
    return slug or (u or "").lower()

# --------------------------
# 抓取解析
# --------------------------

def extract_collection_links(page) -> List[str]:
    """集合页抓取到 PDP 链接列表。"""
    anchors = page.locator("a[href*='/arcteryx-'][href*='/p']")
    hrefs = anchors.evaluate_all("els => els.map(e => e.href)")
    uniq: List[str] = []
    for h in hrefs:
        if "als.com" in h:
            h = h.split("#")[0]
            if h not in uniq:
                uniq.append(h)
    return uniq


def extract_price(page) -> Tuple[str, float]:
    """货币与价格（尽量简单稳健）。"""
    for sel in [
        "[class*='price']",
        "[data-test*='price']",
        "div:has-text('$'), div:has-text('CA$'), div:has-text('US$'), div:has-text('€'), div:has-text('£'), div:has-text('¥')",
        "body",
    ]:
        try:
            if page.locator(sel).count():
                txt = page.locator(sel).first.inner_text()
                txt = txt.replace(",", "")
                m = re.search(r"([A-Z]{2}\$|\$|CA\$|US\$|€|£|¥)\s*([0-9]+(?:\.[0-9]{2})?)", txt)
                if m:
                    return m.group(1), float(m.group(2))
                m = re.search(r"([0-9]+(?:\.[0-9]{2})?)", txt)
                if m:
                    return "", float(m.group(1))
        except Exception:
            continue
    return "", math.nan


def extract_title(page) -> str:
    try:
        if page.locator("h1").count():
            return norm_spaces(page.locator("h1").first.inner_text())
        if page.locator("title").count():
            return norm_spaces(page.locator("title").first.inner_text())
    except Exception:
        pass
    return ""


def extract_sku(page) -> str:
    """SKU（简化：匹配 X 开头样式号；退路找 SKU/Style/Model 标记）。"""
    try:
        txt = page.locator("body").inner_text()
        m = re.search(r"(X\d{9,12})", txt)
        if m:
            return m.group(1).strip()
        m = re.search(r"(?:SKU|Style|Model)\s*[:#]\s*([A-Za-z0-9\-]+)", txt, re.I)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return ""


def extract_color(page) -> str:
    """颜色（简版：尝试 Color: 行、aria-selected 按钮、标题括号）。"""
    try:
        matches = page.locator("text=/Color\\s*:/i")
        if matches.count():
            line = matches.first.evaluate("el => el.parentElement ? el.parentElement.innerText : el.innerText")
            if line:
                m = re.search(r"Color\s*:\s*(.+)", line, re.I)
                if m:
                    return norm_spaces(m.group(1))
    except Exception:
        pass
    try:
        selected = page.locator("[aria-pressed='true'], [aria-selected='true']")
        for i in range(min(selected.count(), 6)):
            t = norm_spaces(selected.nth(i).inner_text())
            if t and len(t) <= 40 and not re.search(r"(Add to cart|Add to bag)", t, re.I):
                return t
    except Exception:
        pass
    try:
        title = extract_title(page)
        m = re.search(r"\(([^()]+)\)$", title)
        if m:
            return norm_spaces(m.group(1))
    except Exception:
        pass
    return ""


def extract_sizes_available(page) -> List[str]:
    """返回可购尺码列表（只判断可点/不可点，不取数量，避免误报）。"""
    sizes: List[str] = []
    try:
        candidates = page.locator(
            "button:has-text('XXS'), button:has-text('XS'), button:has-text('S'), "
            "button:has-text('M'), button:has-text('L'), button:has-text('XL'), "
            "button:has-text('XXL'), button:has-text('XXXL')"
        )
        for i in range(candidates.count()):
            el = candidates.nth(i)
            label = norm_spaces(el.inner_text()).upper()
            if not label:
                continue
            disabled = el.get_attribute("disabled")
            aria = el.get_attribute("aria-disabled")
            cls = (el.get_attribute("class") or "")
            if not (disabled is not None or aria in ("true", "disabled") or "disabled" in cls):
                sizes.append(label)
    except Exception:
        pass
    return sorted(list(dict.fromkeys(sizes)))


def parse_product_detail(page) -> Dict[str, Any]:
    """PDP 解析（简化字段，仅保留必要）。"""
    title = extract_title(page)
    sku = extract_sku(page)
    color = extract_color(page)
    currency, price = extract_price(page)
    sizes_avail = extract_sizes_available(page)
    return {
        "title": title,
        "sku": sku,
        "color": color,
        "currency": currency,
        "price": price,
        "sizes_avail": sizes_avail,          # 可购尺码列表
        "in_stock": bool(sizes_avail),       # 任一尺码可买即 True
    }


def scrape_all_products(headless: bool = True, timeout_ms: int = 8000) -> Dict[str, Any]:
    """
    集合页翻页直到连续2页无链接；逐个 PDP 解析。
    仅保留必要逻辑：拦截静态资源以提速；每个 PDP 尝试 1 次。
    key = 稳定 slug。
    """
    result: Dict[str, Any] = {}
    keyword = os.environ.get("KEYWORD_FILTER", "").strip().lower()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, args=["--disable-http-cache"])
        ctx = browser.new_context(user_agent=USER_AGENT, locale="en-US")
        ctx.set_default_timeout(timeout_ms)

        # 拦截非必要资源
        def _route(route):
            if route.request.resource_type in ("image", "media", "font", "stylesheet"):
                return route.abort()
            return route.continue_()
        ctx.route("**/*", _route)

        page = ctx.new_page()
        page_idx, empty_hits = 1, 0
        seen: Set[str] = set()

        while True:
            url = COLLECTION_URL if page_idx == 1 else f"{COLLECTION_URL}?page={page_idx}"
            try:
                page.goto(url)
                page.wait_for_load_state("domcontentloaded")
            except PWTimeout:
                print(f"[list] timeout {url}")
                empty_hits += 1
                if empty_hits >= 2:
                    break
                page_idx += 1
                continue

            links = extract_collection_links(page)
            print(f"[collection] page {page_idx} links: {len(links)}")

            if not links:
                empty_hits += 1
                if empty_hits >= 2:
                    break
                page_idx += 1
                continue

            empty_hits = 0
            for href in links:
                if href in seen:
                    continue
                seen.add(href)
                safe_sleep()

                try:
                    page.goto(href)
                    page.wait_for_load_state("domcontentloaded")
                    safe_sleep()
                    final_url = page.url
                    pdata = parse_product_detail(page)

                    if keyword and keyword not in (pdata.get("title") or "").lower():
                        continue

                    slug = slug_from_pdp_url(final_url)
                    key = stable_key_from_url(final_url)
                    display_url = f"https://www.als.com/{slug}/p" if slug else final_url.split("?")[0].split("#")[0]

                    pdata.update({"url": display_url, "last_seen": now_iso(), "key": key})
                    # 只有有标题才算有效商品，避免空噪声
                    if pdata["title"]:
                        result[key] = pdata
                except Exception as e:
                    print(f"[detail] error {href}: {e}")
            page_idx += 1

        ctx.close()
        browser.close()

    return result

# --------------------------
# 差异与通知
# --------------------------

def compute_diff(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """
    返回：
      new_items:     [(k, n)]
      price_changes: [(k, o, n)]
      restocks:      [(k, o, n)]
    """
    new_items: List[Tuple[str, Dict[str, Any]]] = []
    price_changes: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []
    restocks: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []

    old_keys, new_keys = set(old.keys()), set(new.keys())

    # 上新（新商品/新变体）
    for k in sorted(new_keys - old_keys):
        new_items.append((k, new[k]))

    # 交集对比
    for k in sorted(new_keys & old_keys):
        o, n = old.get(k, {}), new.get(k, {})

        # 价格变化（双边都是数字且差值>=0.01）
        op, np = o.get("price"), n.get("price")
        if (isinstance(op, (int, float)) and not math.isnan(op)
            and isinstance(np, (int, float)) and not math.isnan(np)
            and abs(op - np) >= 0.01):
            price_changes.append((k, o, n))

        # 仅提醒 缺货 → 到货
        if (not o.get("in_stock", False)) and n.get("in_stock", False):
            restocks.append((k, o, n))

    return {
        "new_items": new_items,
        "price_changes": price_changes,
        "restocks": restocks,
    }


def _fmt_currency_price(currency: str, price: float) -> str:
    if isinstance(price, (int, float)) and not math.isnan(price):
        cur = (currency or "").strip()
        return f"{cur} {price:.2f}".strip() if cur else f"{price:.2f}"
    return "N/A"


def _fmt_sizes_line(sizes_avail: List[str]) -> str:
    return "、".join(sizes_avail) if sizes_avail else "无"


def build_item_message(n: Dict[str, Any], reasons: List[str]) -> Dict[str, Any]:
    """单品单条通知（合并原因）。"""
    nm = n.get("title") or "-"
    sku = n.get("sku") or "-"
    color = n.get("color") or "-"
    price = _fmt_currency_price(n.get("currency", ""), n.get("price"))
    sizes_line = _fmt_sizes_line(n.get("sizes_avail") or [])
    header = "、".join(reasons)

    content = "\n".join([
        f"**{header}**",
        f"• 名称：{nm}",
        f"• 货号：{sku}",
        f"• 颜色：{color}",
        f"• 价格：{price}",
        f"🧾 库存信息：{sizes_line}",
        f"{n.get('url')}",
    ])

    return {
        "content": None,
        "embeds": [{
            "title": "Al's | Arc'teryx 监控",
            "description": content[:4000],
            "timestamp": datetime.utcnow().isoformat(),
            "color": 0x00AAFF,
            "footer": {"text": "als.com 上新 / 价格 / 到货"},
        }]
    }


def send_discord(payload: dict) -> None:
    """简化 webhook：单次尝试，7s 超时；不带 Origin/Referer。"""
    import urllib.request
    import urllib.error

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        print("WARN: DISCORD_WEBHOOK_URL 未配置，跳过通知。")
        return

    webhook = webhook.replace("discordapp.com", "discord.com")
    if "?" not in webhook:
        webhook = webhook + "?wait=true"

    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
    }

    try:
        req = urllib.request.Request(webhook, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=7) as resp:
            body = resp.read().decode("utf-8", "ignore")
            print(f"Discord OK: {resp.status} {body[:120]}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        print(f"Discord HTTPError: {e.code} {body[:200]}")
    except Exception as ex:
        print(f"Discord error: {repr(ex)}")

    time.sleep(float(os.environ.get("NOTIFY_INTERVAL_SEC", "0.1")))

# --------------------------
# 主流程
# --------------------------

def main() -> int:
    print(f"CWD={os.getcwd()}  SNAPSHOT_PATH={SNAPSHOT_PATH.resolve()}")
    headless = os.environ.get("HEADLESS", "1") != "0"

    old = jload(SNAPSHOT_PATH)
    print(f"Loaded {len(old)} items from snapshot.")

    new = scrape_all_products(headless=headless)
    print(f"Scraped {len(new)} items from website.")

    diffs = compute_diff(old, new)

    # 合并为“每商品一条”的原因列表
    reasons_map: Dict[str, List[str]] = {}
    for k, _n in diffs["new_items"]:
        reasons_map.setdefault(k, []).append("上新")
    for k, _o, _n in diffs["price_changes"]:
        reasons_map.setdefault(k, []).append("价格变化")
    for k, _o, _n in diffs["restocks"]:
        reasons_map.setdefault(k, []).append("缺货→到货")

    changed_keys = sorted(set(reasons_map.keys()))
    print("Changed items:", len(changed_keys))

    # 先写回快照（确保下次对比有基线）
    jdump(new, SNAPSHOT_PATH)

    # 只给有变化的商品发通知（一个商品一条）
    if changed_keys:
        for k in changed_keys:
            n = new.get(k) or {}
            payload = build_item_message(n, reasons=reasons_map.get(k, []))
            send_discord(payload)
    else:
        print("No changes; no notifications.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
