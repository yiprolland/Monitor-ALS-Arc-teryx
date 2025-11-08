#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ALS.com Arc'teryx 监控（稳定键 = PDP slug / 单品单条通知 / 基线保护）
监控：
  1) 上新（新商品/新变体）
  2) 价格变化
  3) 仅提醒“缺货 → 到货”
  4) 库存数量增加（逐尺码对比；解析不到数量则 0/1 近似）

行为：
  - 只通知有变化的商品
  - 一个商品一条通知（同一商品的多种变化合并为一条）
  - 使用 PDP 路径中的 slug 作为稳定 key，避免标题/SKU/颜色抖动
  - 基线保护：当“上新占比 > 70%”时，跳过当次“上新”通知（可通过 BASELINE_PROTECT=0 关闭）

Env:
  DISCORD_WEBHOOK_URL   必填：Discord Webhook
  HEADLESS=0/1          可选：本地调试 0，CI 1（默认 1）
  KEYWORD_FILTER        可选：仅监控标题包含该关键词（不区分大小写）
  BASELINE_PROTECT=0/1  可选：默认 1，开启“上新占比异常时抑制上新通知”
  NOTIFY_INTERVAL_SEC   可选：单条通知间隔，默认 0.1 秒
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
# Utilities
# --------------------------

def jdump(obj: Any, path: Path) -> None:
    """Atomic write to avoid half-written or empty JSON."""
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
        # 打印几个示例 key，方便验证
        for i, k in enumerate(list(data.keys())[:5]):
            print(f"[snapshot] sample key {i+1}: {k}")
        return data
    except Exception as e:
        print(f"[snapshot] failed to parse {path}: {e}")
        return {}


def safe_sleep(a: float = 0.08, b: float = 0.22) -> None:
    time.sleep(random.uniform(a, b))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def slug_from_pdp_url(u: str) -> str:
    """
    从 PDP URL 取 slug：/arcteryx-xxx/p -> arcteryx-xxx
    """
    try:
        p = urlparse(u)
        path = (p.path or "").lower()
        m = re.search(r"/([^/]+)/p(?:$|/|\?|#)", path)
        return m.group(1) if m else path.strip("/").split("/")[-1]
    except Exception:
        u2 = (u or "").split("?")[0].split("#")[0].lower()
        return u2.strip("/").split("/")[-1]


def stable_key_from_url(u: str) -> str:
    """
    稳定 key 仅用 slug，避免标题/颜色/SKU 抖动导致重复“上新”。
    同一 PDP（即同一路由）→ 同一 key。
    """
    slug = slug_from_pdp_url(u)
    return slug or (u or "").lower()


# --------------------------
# Scraper
# --------------------------

def extract_collection_links(page) -> List[str]:
    """收集集合页上的 PDP 链接。"""
    anchors = page.locator("a[href*='/arcteryx-'][href*='/p']")
    hrefs = anchors.evaluate_all("els => els.map(e => e.href)")
    uniq: List[str] = []
    for h in hrefs:
        if "als.com" in h:
            h = h.split("#")[0]
            if h not in uniq:
                uniq.append(h)
    return uniq


def extract_sku(page) -> str:
    """解析 SKU / Style number。"""
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
    # 结构化数据兜底
    try:
        metas = page.locator("script[type='application/ld+json']")
        for i in range(min(8, metas.count())):
            raw = metas.nth(i).inner_text()
            if not raw.strip():
                continue
            obj = json.loads(raw)
            if isinstance(obj, dict):
                sku = obj.get("sku")
                if sku:
                    return str(sku).strip()
            elif isinstance(obj, list):
                for it in obj:
                    if isinstance(it, dict) and it.get("sku"):
                        return str(it["sku"]).strip()
    except Exception:
        pass
    return ""


def extract_color(page) -> str:
    """解析颜色。"""
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
        for i in range(min(selected.count(), 8)):
            t = norm_spaces(selected.nth(i).inner_text())
            if t and len(t) <= 40 and not re.search(r"(Add to cart|Add to bag)", t, re.I):
                return t
    except Exception:
        pass
    try:
        if page.locator("h1").count():
            title = page.locator("h1").first.inner_text()
            m = re.search(r"\(([^()]+)\)$", title)
            if m:
                return norm_spaces(m.group(1))
    except Exception:
        pass
    return ""


def money_from_text(txt: str) -> Tuple[str, float]:
    """
    抽取货币符号与金额，例如 '$ 360.00' 或 'CA$ 360'。
    返回 (currency_symbol, price_float)；若失败 price=nan, symbol=''
    """
    if not txt:
        return "", math.nan
    m = re.search(r"([A-Z]{2}\$|\$|C\$|CA\$|US\$|€|£|¥)\s*([0-9]+(?:\.[0-9]{2})?)", txt.replace(",", ""))
    if m:
        return m.group(1), float(m.group(2))
    m = re.search(r"([0-9]+(?:\.[0-9]{2})?)", txt.replace(",", ""))
    if m:
        return "", float(m.group(1))
    return "", math.nan


def extract_price(page) -> Tuple[str, float]:
    """解析货币与价格。"""
    for sel in [
        "[class*='price']",
        "[data-test*='price']",
        "div:has-text('$'), div:has-text('CA$'), div:has-text('US$'), div:has-text('€'), div:has-text('£'), div:has-text('¥')",
        "body",
    ]:
        try:
            if page.locator(sel).count():
                txt = page.locator(sel).first.inner_text()
                cur, pr = money_from_text(txt)
                if not math.isnan(pr):
                    return cur, pr
        except Exception:
            continue
    return "", math.nan


def extract_sizes_with_qty(page) -> Dict[str, int]:
    """
    返回 {size_text: qty_int}
    解析顺序：
      1) data-available-qty / data-inventory / data-qty / data-stock
      2) 页面脚本中的 "size":"XL","inventory_quantity":3
      3) 回退：按钮可点=1，不可点=0
    """
    sizes: Dict[str, int] = {}

    # 1) data-* 属性
    try:
        btns = page.locator("button, [role='option'], [data-size]")
        for i in range(min(btns.count(), 150)):
            el = btns.nth(i)
            label = norm_spaces(el.inner_text()).upper()
            if not label or len(label) > 10:
                continue
            if not re.fullmatch(r"(XXS|XS|S|M|L|XL|XXL|XXXL|[\d]{1,2})", label, re.I):
                continue
            qty_attr = None
            for attr in ("data-available-qty", "data-inventory", "data-qty", "data-stock", "data-quantity"):
                v = el.get_attribute(attr)
                if v and re.fullmatch(r"-?\d+", v.strip()):
                    qty_attr = int(v.strip())
                    break
            if qty_attr is not None:
                sizes[label] = max(0, qty_attr)
    except Exception:
        pass

    # 2) 脚本中的 JSON
    if not sizes:
        try:
            scripts = page.locator("script")
            for i in range(min(12, scripts.count())):
                raw = scripts.nth(i).inner_text()
                if not raw or ("variant" not in raw.lower() and "inventory" not in raw.lower()):
                    continue
                for m in re.finditer(r'"size"\s*:\s*"(?P<size>[^"]+?)"[^}]*?"inventory[^"]*?"\s*:\s*(?P<qty>-?\d+)', raw, re.I | re.S):
                    sizes[m.group("size").upper()] = max(0, int(m.group("qty")))
        except Exception:
            pass

    # 3) 回退：可点=1，不可点=0
    if not sizes:
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
                sizes[label] = 0 if (disabled is not None or aria in ("true", "disabled") or "disabled" in cls) else 1
        except Exception:
            pass

    return sizes


def parse_product_detail(page) -> Dict[str, Any]:
    """解析 PDP 所需字段。"""
    data = {
        "title": "",
        "sku": "",
        "color": "",
        "currency": "",
        "price": math.nan,
        "sizes": {},       # {size: qty_int}
        "in_stock": False, # 任一尺码 qty>0 即 True
    }

    try:
        if page.locator("h1").count():
            data["title"] = norm_spaces(page.locator("h1").first.inner_text())
        elif page.locator("title").count():
            data["title"] = norm_spaces(page.locator("title").first.inner_text())
    except Exception:
        pass

    try:
        data["sku"] = extract_sku(page)
    except Exception:
        pass

    try:
        data["color"] = extract_color(page)
    except Exception:
        pass

    try:
        cur, pr = extract_price(page)
        data["currency"] = cur
        data["price"] = pr
    except Exception:
        pass

    try:
        sizes = extract_sizes_with_qty(page)
        data["sizes"] = sizes
        data["in_stock"] = any(qty > 0 for qty in sizes.values()) if sizes else False
    except Exception:
        pass

    return data


def scrape_all_products(headless: bool = True, timeout_ms: int = 8000) -> Dict[str, Any]:
    """遍历集合页 → 逐个 PDP 解析 → 返回以 稳定 slug 为键 的 dict。"""
    result: Dict[str, Any] = {}
    keyword = os.environ.get("KEYWORD_FILTER", "").strip().lower()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, args=["--disable-http-cache"])
        ctx = browser.new_context(user_agent=USER_AGENT, locale="en-US")
        ctx.set_default_timeout(timeout_ms)

        # 拦截非必要资源以提速
        def _route(route):
            req = route.request
            if req.resource_type in ("image", "media", "font", "stylesheet"):
                return route.abort()
            return route.continue_()
        ctx.route("**/*", _route)

        page = ctx.new_page()

        page_idx = 1
        empty_hits = 0
        seen_urls: Set[str] = set()

        while True:
            url = COLLECTION_URL if page_idx == 1 else f"{COLLECTION_URL}?page={page_idx}"
            try:
                page.goto(url)
                page.wait_for_load_state("domcontentloaded")
            except PWTimeout:
                print(f"[page] timeout loading {url}")
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
                if href in seen_urls:
                    continue
                seen_urls.add(href)
                safe_sleep()

                ok = False
                final_url = href
                for attempt in range(2):  # 少量重试以提速
                    try:
                        page.goto(href)
                        page.wait_for_load_state("domcontentloaded")
                        safe_sleep()
                        final_url = page.url  # 取最终跳转后的 URL
                        pdata = parse_product_detail(page)
                        title = pdata.get("title", "")
                        if keyword and keyword not in (title or "").lower():
                            ok = True
                            break

                        # 稳定 key & 展示 URL
                        slug = slug_from_pdp_url(final_url)
                        key = stable_key_from_url(final_url)
                        display_url = f"https://www.als.com/{slug}/p" if slug else final_url.split("?")[0].split("#")[0]

                        if title:
                            pdata.update({"url": display_url, "last_seen": now_iso(), "key": key})
                            result[key] = pdata
                            ok = True
                            break
                    except Exception as e:
                        print(f"[detail] error {href}: {e}")
                        safe_sleep(0.2, 0.4)

                if not ok:
                    slug = slug_from_pdp_url(final_url or href)
                    key = stable_key_from_url(final_url or href)
                    display_url = f"https://www.als.com/{slug}/p" if slug else (final_url or href).split("?")[0].split("#")[0]
                    result[key] = {
                        "title": "",
                        "sku": "",
                        "color": "",
                        "currency": "",
                        "price": math.nan,
                        "sizes": {},
                        "in_stock": False,
                        "url": display_url,
                        "last_seen": now_iso(),
                        "key": key,
                        "note": "parse_failed",
                    }
            page_idx += 1

        ctx.close()
        browser.close()

    return result


# --------------------------
# Diff & Notification
# --------------------------

def compute_diff(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """
    返回：
      new_items:        [(k, n)]
      price_changes:    [(k, o, n)]
      restocks:         [(k, o, n)]
      stock_increases:  [(k, o, n, increased_sizes_dict)]
    """
    new_items: List[Tuple[str, Dict[str, Any]]] = []
    price_changes: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []
    restocks: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []
    stock_increases: List[Tuple[str, Dict[str, Any], Dict[str, Any], Dict[str, int]]] = []

    old_keys = set(old.keys())
    new_keys = set(new.keys())

    # 上新（含新变体）
    for k in sorted(new_keys - old_keys):
        new_items.append((k, new[k]))

    # 交集对比
    for k in sorted(new_keys & old_keys):
        o = old.get(k, {})
        n = new.get(k, {})

        # 价格变化
        op, np = o.get("price"), n.get("price")
        if (isinstance(op, (int, float)) and isinstance(np, (int, float))
                and not math.isnan(op) and not math.isnan(np) and abs(op - np) >= 0.01):
            price_changes.append((k, o, n))

        # 缺货→到货（仅提醒这一方向）
        if (not o.get("in_stock", False)) and n.get("in_stock", False):
            restocks.append((k, o, n))

        # 库存数量增加（逐尺码）
        increased: Dict[str, int] = {}
        osizes: Dict[str, int] = o.get("sizes") or {}
        nsizes: Dict[str, int] = n.get("sizes") or {}
        for size, nqty in nsizes.items():
            oqty = osizes.get(size, 0)
            try:
                if int(nqty) > int(oqty):
                    increased[size] = int(nqty)
            except Exception:
                if (nqty and not oqty):
                    increased[size] = 1
        if increased:
            stock_increases.append((k, o, n, increased))

    return {
        "new_items": new_items,
        "price_changes": price_changes,
        "restocks": restocks,
        "stock_increases": stock_increases,
    }


def _fmt_currency_price(currency: str, price: float) -> str:
    if isinstance(price, (int, float)) and not math.isnan(price):
        cur = (currency or "").strip()
        return f"{cur} {price:.2f}".strip() if cur else f"{price:.2f}"
    return "N/A"


def _fmt_sizes_line(sizes: Dict[str, int], only_keys: List[str] = None, limit: int = 8) -> str:
    items: List[str] = []
    if only_keys:
        for k in only_keys:
            if k in sizes:
                items.append(f"{k}:{sizes[k]}")
    else:
        for k, v in sizes.items():
            if v and v > 0:
                items.append(f"{k}:{v}")
                if len(items) >= limit:
                    break
    return "，".join(items) if items else "无"


def build_item_message(n: Dict[str, Any], reasons: List[str], increased_sizes: List[str] = None) -> Dict[str, Any]:
    """
    为单个商品构建 Discord payload（一个商品一条消息）。
    reasons: ["上新", "价格变化", "缺货→到货", "库存增加"]
    increased_sizes: 当包含“库存增加”时，仅展示增长的尺码（可选）。
    """
    nm = n.get("title") or "-"
    sku = n.get("sku") or "-"
    color = n.get("color") or "-"
    price = _fmt_currency_price(n.get("currency", ""), n.get("price"))
    sizes = n.get("sizes") or {}

    if increased_sizes:
        sizes_line = _fmt_sizes_line(sizes, only_keys=increased_sizes)
    else:
        sizes_line = _fmt_sizes_line(sizes)

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
            "footer": {"text": "als.com 价格/上新/库存监控"},
        }]
    }


def send_discord(payload: dict) -> None:
    """
    Discord Webhook 通知：仅必要请求头；单次发送（失败跳过）；轻微发送间隔避免 429。
    （不带 Origin/Referer，规避 50067 Invalid request origin）
    """
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
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0 Safari/537.36"
        ),
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

    # 发送间隔，避免频繁 429（可按需调大）
    time.sleep(float(os.environ.get("NOTIFY_INTERVAL_SEC", "0.1")))


# --------------------------
# Main
# --------------------------

def main() -> int:
    print(f"CWD={os.getcwd()}  SNAPSHOT_PATH={SNAPSHOT_PATH.resolve()}")
    headless = os.environ.get("HEADLESS", "1") != "0"
    baseline_protect = os.environ.get("BASELINE_PROTECT", "1") != "0"

    old = jload(SNAPSHOT_PATH)
    print(f"Loaded {len(old)} items from snapshot.")

    # 抓取
    new = scrape_all_products(headless=headless)
    print(f"Scraped {len(new)} items from website.")

    # 计算差异 → “一商品一条”聚合
    diffs = compute_diff(old, new)

    reasons_map: Dict[str, List[str]] = {}
    increased_sizes_map: Dict[str, List[str]] = {}

    for k, n in diffs["new_items"]:
        reasons_map.setdefault(k, []).append("上新")
    for k, o, n in diffs["price_changes"]:
        reasons_map.setdefault(k, []).append("价格变化")
    for k, o, n in diffs["restocks"]:
        reasons_map.setdefault(k, []).append("缺货→到货")
    for k, o, n, inc in diffs["stock_increases"]:
        reasons_map.setdefault(k, []).append("库存增加")
        increased_sizes_map[k] = list(inc.keys())

    changed_keys = sorted(set(reasons_map.keys()))
    print("Changed items:", len(changed_keys))

    # --- 基线保护：如果“上新占比异常高”，当次不上发“上新” ---
    if baseline_protect and changed_keys:
        total_new = len(diffs["new_items"])
        total_all = len(new) if new else 1
        ratio = total_new / total_all
        print(f"[baseline] new_ratio={ratio:.2%} (new={total_new}, all={total_all})")
        if ratio > 0.70 and total_all >= 20:  # 样本少时不触发保护
            print("[baseline] too many NEW items detected; suppress NEW notifications for this run.")
            # 移除“上新”原因，只保留价格/到货/库存增加
            for k, _n in list(diffs["new_items"]):
                if k in reasons_map:
                    reasons_map[k] = [r for r in reasons_map[k] if r != "上新"]
                    if not reasons_map[k]:
                        reasons_map.pop(k, None)
            changed_keys = sorted(set(reasons_map.keys()))
            print("Changed items after baseline protection:", len(changed_keys))

    # 写回快照（无论是否通知）
    jdump(new, SNAPSHOT_PATH)

    # 逐条通知（只对有变化的商品）
    if changed_keys:
        for k in changed_keys:
            n = new.get(k) or {}
            reasons = reasons_map.get(k, [])
            inc_sizes = increased_sizes_map.get(k)
            payload = build_item_message(n, reasons=reasons, increased_sizes=inc_sizes)
            send_discord(payload)
    else:
        print("No changes; no notifications.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
