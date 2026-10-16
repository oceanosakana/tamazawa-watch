#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
霜ばしら（九重本舗玉澤）予約販売開始・再入荷 監視スクリプト v2

強化ポイント:
  1. 3 系統の情報源を監視する
     - A) BASE 商品ページ  : 在庫ステータス（買える/買えない）※最優先・最新
     - B) 公式サイトお知らせ: https://www.tamazawa.jp/news/ の新着記事
     - C) 公式 X          : 投稿数の増加で「新着投稿あり」を検知（本文は取得しない）
  2. 「受付開始」の告知を検知し、本文から開始日時を推定して Discord へ通知
  3. 推定した開始日時を state.json に保存して **自動でアーミング**（次回以降、開始前から高速監視）
  4. 商品ページの商品説明文の変化も検知（告知はまず説明文が書き換わる）

依存ライブラリなし（Python 標準ライブラリのみ）。

環境変数は README を参照。
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------
# 設定（環境変数）
# ---------------------------------------------------------------------------

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _envf(key: str, default: float) -> float:
    try:
        return float(_env(key) or default)
    except ValueError:
        return default


def _envi(key: str, default: int) -> int:
    try:
        return int(float(_env(key) or default))
    except ValueError:
        return default


def _envb(key: str, default: bool = False) -> bool:
    return _env(key, "1" if default else "0") == "1"


ITEM_URL = _env("ITEM_URL", "https://tamazawa.thebase.in/items/33921076")
NEWS_URL = _env("NEWS_URL", "https://www.tamazawa.jp/news/")
X_SCREEN_NAME = _env("X_SCREEN_NAME", "TamazawaSendai")

SHOP_MATCH = re.search(r"https?://([a-z0-9\-]+)\.thebase\.in/items/(\d+)", ITEM_URL)
SHOP_ID = SHOP_MATCH.group(1) if SHOP_MATCH else "tamazawa"
CART_ADD_URL = f"https://{SHOP_ID}.thebase.in/cart/add/{SHOP_ID}"

DISCORD_WEBHOOK_URL = _env("DISCORD_WEBHOOK_URL")
DISCORD_MENTION = _env("DISCORD_MENTION")

ENABLE_ITEM = _envb("ENABLE_ITEM", True)
ENABLE_NEWS = _envb("ENABLE_NEWS", True)
ENABLE_X = _envb("ENABLE_X", True)

MODE = (_env("MODE", "continuous") or "continuous").lower()  # continuous / window
INTERVAL_SECONDS = _envf("INTERVAL_SECONDS", 20)      # 通常時の商品ページ監視間隔
FAST_INTERVAL_SECONDS = _envf("FAST_INTERVAL_SECONDS", 2)  # 受付開始ウィンドウ中の間隔
ANNOUNCE_INTERVAL_SECONDS = _envf("ANNOUNCE_INTERVAL_SECONDS", 120)  # お知らせ/説明文の確認間隔
X_INTERVAL_SECONDS = _envf("X_INTERVAL_SECONDS", 900)  # X の確認間隔（外部サービスなので控えめに）
MAX_SECONDS = _envf("MAX_SECONDS", 420)
JITTER_SECONDS = _envf("JITTER_SECONDS", 1.0)

START_AT_RAW = _env("START_AT")
LEAD_MINUTES = _envf("LEAD_MINUTES", 10)
AUTO_ARM = _envb("AUTO_ARM", True)   # 告知から推定した開始時刻を自動で使う
ARM_MAX_DAYS = _envf("ARM_MAX_DAYS", 45)  # これより先の日時は誤検知とみなして無視

REPEAT_NOTIFY = _envi("REPEAT_NOTIFY", 3)
REPEAT_INTERVAL_SECONDS = _envf("REPEAT_INTERVAL_SECONDS", 20)

NOTIFY_SOLD_OUT = _envb("NOTIFY_SOLD_OUT", False)
NOTIFY_ANNOUNCEMENT = _envb("NOTIFY_ANNOUNCEMENT", True)
NOTIFY_TEXT_CHANGE = _envb("NOTIFY_TEXT_CHANGE", False)
EXIT_ON_DETECT = _envb("EXIT_ON_DETECT", False)

# 起動通知: 既定では GitHub Actions の手動実行 (workflow_dispatch) のときだけ送る。
# cron 実行ごとに送ると約 5 分おきに大量通知になるため、明示的に NOTIFY_START を
# 指定しない限り定時実行では送らない。
GITHUB_EVENT_NAME = _env("GITHUB_EVENT_NAME")
GITHUB_RUN_ID = _env("GITHUB_RUN_ID")
GITHUB_REPOSITORY = _env("GITHUB_REPOSITORY")
GITHUB_SERVER_URL = _env("GITHUB_SERVER_URL", "https://github.com")
NOTIFY_START = _envb("NOTIFY_START", GITHUB_EVENT_NAME == "workflow_dispatch")
ONCE = _envb("ONCE", False)
DRY_RUN = _envb("DRY_RUN", False)
STATE_PATH = _env("STATE_PATH", "state.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

SHIMOBASHIRA_HINT = "霜ばしら"

# 「受付開始」告知とみなすキーワード
ANNOUNCE_KEYWORDS = (
    "受付開始", "受付を開始", "受付致します", "受付いたします",
    "予約受付", "オンライン予約", "販売開始", "より受付", "受付を行",
)


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def now_jst() -> datetime:
    return datetime.now(JST)


def ts_str(dt: datetime | None = None) -> str:
    return (dt or now_jst()).strftime("%Y-%m-%d %H:%M:%S JST")


def log(msg: str) -> None:
    print(f"[{ts_str()}] {msg}", flush=True)


def fetch(url: str, cache_bust: bool = True) -> str:
    target = url
    if cache_bust:
        sep = "&" if "?" in url else "?"
        target = f"{url}{sep}_={int(time.time() * 1000)}"
    req = urllib.request.Request(
        target,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as res:
        return res.read().decode("utf-8", "replace")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def excerpt(text: str, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------------------
# A) BASE 商品ページの解析
# ---------------------------------------------------------------------------

RE_PURCHASABILITY = re.compile(r"'item_purchasability'\s*:\s*\"([^\"]+)\"")
RE_UNPURCHASABLE_REASON = re.compile(r"'item_unpurchasable_reason'\s*:\s*\"([^\"]+)\"")
RE_ITEM_PRICE = re.compile(r"'itemPrice'\s*:\s*(\d+)")
RE_SOLD_OUT_ELEMENT = re.compile(r"purchaseButton__btn--(?:soldOut|noItem|outOfStock)")
RE_META_DESC = re.compile(r'<meta name="description" content="([\s\S]*?)"\s*/>')
RE_SALES_PERIOD = re.compile(r"<[^>]*class=\"[^\"]*salesPeriod--(willStart|willEnd|finish|available)[^\"]*\"[^>]*>([\s\S]{0,160}?)</")

ERROR_MARKERS = ("メンテナンス", "Maintenance", "システムエラー", "現在、カートがご利用")


def analyze_item(page_html: str) -> dict:
    """商品ページ HTML から在庫ステータスと説明文を取り出す。"""
    purch = RE_PURCHASABILITY.search(page_html)
    reason = RE_UNPURCHASABLE_REASON.search(page_html)
    price = RE_ITEM_PRICE.search(page_html)
    desc = RE_META_DESC.search(page_html)
    period = RE_SALES_PERIOD.search(page_html)

    description = html_lib.unescape(desc.group(1)) if desc else ""

    purchasability = purch.group(1) if purch else None
    unpurchasable_reason = reason.group(1) if reason else None
    sold_out = bool(RE_SOLD_OUT_ELEMENT.search(page_html)) or ("endOfSale" in page_html)

    if any(m in page_html for m in ERROR_MARKERS):
        status = "error"
    elif purchasability == "purchasable":
        status = "purchasable"
    elif purchasability == "unpurchasable":
        status = "unpurchasable"
    elif sold_out:
        status = "unpurchasable"
    else:
        status = "unknown"

    return {
        "status": status,
        "purchasability": purchasability,
        "reason": unpurchasable_reason,
        "sold_out": sold_out,
        "price": int(price.group(1)) if price else None,
        "description": description,
        "description_hash": digest(description) if description else None,
        "sales_period": re.sub(r"\s+", " ", period.group(2)).strip() if period else None,
        "sales_period_state": period.group(1) if period else None,
    }


def check_item() -> dict:
    try:
        info = analyze_item(fetch(ITEM_URL))
        info["ok"] = True
        return info
    except Exception as exc:
        log(f"商品ページの取得に失敗: {exc}")
        return {"status": "fetch_error", "reason": str(exc), "ok": False,
                "description_hash": None, "price": None}


# ---------------------------------------------------------------------------
# B) 公式サイト「お知らせ」の解析
# ---------------------------------------------------------------------------

RE_NEWS_ROW = re.compile(
    r'<th scope="row">\s*(\d{4})年(\d{2})月(\d{2})日\s*</th>\s*'
    r'<td>\s*<a href="(detail\.php\?CN=\d+)">([\s\S]*?)</a>',
    re.S,
)


def check_news() -> dict:
    """お知らせ一覧から最新の記事を取得する（初回は基準として記録するだけ）。"""
    try:
        page = fetch(NEWS_URL)
    except Exception as exc:
        log(f"お知らせの取得に失敗: {exc}")
        return {"ok": False}

    rows = RE_NEWS_ROW.findall(page)
    if not rows:
        return {"ok": False}

    year, month, day, rel, title = rows[0]
    title = html_lib.unescape(re.sub(r"\s+", " ", title)).strip()
    return {
        "ok": True,
        "id": rel,
        "date": f"{year}-{month}-{day}",
        "title": title,
        "url": urllib.parse.urljoin(NEWS_URL, rel),
    }


def fetch_news_detail(url: str) -> str:
    """記事本文を取得する（日時の抽出に使う）。"""
    try:
        page = fetch(url, cache_bust=False)
    except Exception as exc:
        log(f"お知らせ本文の取得に失敗: {exc}")
        return ""
    m = re.search(r"<main[\s\S]*?</main>", page)
    body = m.group(0) if m else page
    body = re.sub(r"<script[\s\S]*?</script>", " ", body)
    body = re.sub(r"<[^>]+>", " ", body)
    return html_lib.unescape(re.sub(r"[ \t]+", " ", body)).strip()


# ---------------------------------------------------------------------------
# C) 公式 X の新着チェック
# ---------------------------------------------------------------------------

def check_x() -> dict:
    """X に新しい投稿があったかを投稿数で検知する。

    ※ X のタイムライン本文は公式 API なしでは安定して取得できない
      （syndication エンドポイントは古いキャッシュを返し、レート制限もある）ため、
      ここでは「投稿数が増えた = 何か投稿された」ことだけを検知して知らせる。
      告知の本文と日時は商品説明文／公式サイトのお知らせから拾うのが本命。
    """
    if not X_SCREEN_NAME:
        return {"ok": False}
    url = f"https://api.fxtwitter.com/{X_SCREEN_NAME}"
    try:
        data = json.loads(fetch(url, cache_bust=False))
    except Exception as exc:
        log(f"X の取得に失敗: {exc}")
        return {"ok": False}
    if data.get("code") != 200:
        return {"ok": False}
    user = data.get("user") or {}
    count = user.get("tweets")
    if not isinstance(count, int):
        return {"ok": False}
    return {
        "ok": True,
        "tweets": count,
        "followers": user.get("followers"),
        "url": f"https://x.com/{X_SCREEN_NAME}",
    }


# ---------------------------------------------------------------------------
# 告知文からの開始日時の推定
# ---------------------------------------------------------------------------

RE_DATE = re.compile(r"(?:(\d{4})\s*[年/\-])?\s*(\d{1,2})\s*[月/\-]\s*(\d{1,2})\s*日?")
RE_TIME = re.compile(r"(\d{1,2})\s*[:時]\s*(\d{2})?")


def looks_like_announcement(text: str) -> bool:
    if not text:
        return False
    if any(k in text for k in ANNOUNCE_KEYWORDS):
        return True
    return False


def parse_start_datetime(text: str, now: datetime | None = None) -> datetime | None:
    """「10/24（月）19:00頃より」「9月23日（水）19時」等から開始日時を推定する。"""
    if not text:
        return None
    now = now or now_jst()
    for m in RE_DATE.finditer(text):
        year = int(m.group(1)) if m.group(1) else now.year
        month, day = int(m.group(2)), int(m.group(3))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue

        # 日付の直後に時刻（19:00 / 19時 / 19時30分）があるかを見る
        tail = text[m.end(): m.end() + 20]
        tm = RE_TIME.search(tail)
        if tm is None:
            # 「19時頃より10/24」のような並びも一応拾う
            head = text[max(0, m.start() - 14): m.start()]
            tm = RE_TIME.search(head)
            if tm is None:
                continue

        hour = int(tm.group(1))
        minute = int(tm.group(2) or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            continue

        try:
            dt = datetime(year, month, day, hour, minute, tzinfo=JST)
        except ValueError:
            continue

        # 年が書かれていない場合、過去すぎるなら来年と解釈する
        if m.group(1) is None and dt < now - timedelta(days=30):
            try:
                dt = dt.replace(year=year + 1)
            except ValueError:
                continue

        if dt < now - timedelta(hours=6):
            continue  # 明らかに過去の記述（前回の告知など）
        if dt > now + timedelta(days=ARM_MAX_DAYS):
            continue  # 遠すぎる日時は誤検知とみなす
        return dt
    return None


# ---------------------------------------------------------------------------
# state.json
# ---------------------------------------------------------------------------

def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log(f"state.json の読み込みに失敗（無視します）: {exc}")
        return {}


def save_state(state: dict) -> bool:
    tmp = f"{STATE_PATH}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, STATE_PATH)
        return True
    except Exception as exc:
        log(f"state.json の保存に失敗: {exc}")
        return False


# ---------------------------------------------------------------------------
# Discord 通知
# ---------------------------------------------------------------------------

def post_discord(payload: dict) -> None:
    if DRY_RUN:
        log("DRY_RUN: Discord へ送信しません。送信内容:")
        print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
        return
    if not DISCORD_WEBHOOK_URL:
        log("DISCORD_WEBHOOK_URL が未設定のため通知できません。")
        return

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            res.read()
        log("Discord へ通知しました。")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            try:
                wait = float(json.loads(exc.read().decode("utf-8", "replace")).get("retry_after", 1.0)) + 0.5
            except Exception:
                wait = 2.0
            log(f"Discord レートリミット。{wait:.1f} 秒待って再送します。")
            time.sleep(wait)
            post_discord(payload)
            return
        log(f"Discord 通知に失敗: HTTP {exc.code}")
    except Exception as exc:
        log(f"Discord 通知に失敗: {exc}")


def notify_purchasable(info: dict) -> None:
    price = info.get("price")
    price_text = f"¥{price:,}" if price else "¥4,860"
    embed = {
        "title": "🎉 販売開始 / 再入荷を検知しました！（霜ばしら）",
        "description": (
            "**今すぐ下のリンクから購入してください。**\n"
            "1 個限りの商品です。決済完了までが購入確定です。"
        ),
        "url": ITEM_URL,
        "color": 0xE74C3C,
        "fields": [
            {"name": "商品", "value": "霜ばしら（九重本舗玉澤）", "inline": True},
            {"name": "価格", "value": price_text, "inline": True},
            {"name": "検知時刻", "value": ts_str(), "inline": False},
            {"name": "購入", "value": f"[▶ 購入ページを開く]({ITEM_URL})\n[🛒 カートに追加（要ログイン）]({CART_ADD_URL})", "inline": False},
        ],
        "footer": {"text": "規約上、自動購入は推奨されません。自分で操作して購入してください。"},
    }
    _notify_repeated(embed, alert=True)


def notify_announcement(source: str, text: str, start_at: datetime | None, url: str | None = None) -> None:
    fields = [{"name": "情報源", "value": source, "inline": True}]
    if start_at:
        fields.append({"name": "推定開始日時", "value": f"**{ts_str(start_at)}**", "inline": True})
        fields.append({
            "name": "自動アーミング",
            "value": f"開始 {LEAD_MINUTES:g} 分前から高速監視に切り替わります"
                     if AUTO_ARM else "AUTO_ARM=0 のため手動で START_AT を設定してください",
            "inline": False,
        })
    else:
        fields.append({"name": "推定開始日時", "value": "日時を特定できませんでした（本文を確認してください）", "inline": False})
    fields.append({"name": "本文", "value": excerpt(text, 400) or "(空)", "inline": False})

    embed = {
        "title": "📢 「霜ばしら」受付開始の告知を検出しました",
        "url": url or ITEM_URL,
        "color": 0x3498DB,
        "fields": fields,
        "footer": {"text": "告知を検出しました。日時は本文から自動推定したものです。"},
    }
    _notify_repeated(embed, alert=False)


def notify_text_change(old_excerpt: str, new_excerpt: str) -> None:
    embed = {
        "title": "📝 商品説明文が更新されました（霜ばしら）",
        "url": ITEM_URL,
        "color": 0xF1C40F,
        "fields": [
            {"name": "更新後", "value": new_excerpt or "(空)", "inline": False},
            {"name": "更新前", "value": old_excerpt or "(空)", "inline": False},
        ],
        "footer": {"text": "在庫ステータスはまだ変わっていません。"},
    }
    post_discord({"username": "霜ばしら監視ボット", "embeds": [embed]})


def notify_sold_out(info: dict) -> None:
    embed = {
        "title": "😢 売り切れに戻りました（霜ばしら）",
        "description": f"ステータス: `{info.get('status')}` / 理由: `{info.get('reason')}`",
        "url": ITEM_URL,
        "color": 0x95A5A6,
        "fields": [{"name": "時刻", "value": ts_str(), "inline": False}],
    }
    post_discord({"username": "霜ばしら監視ボット", "embeds": [embed]})


def _run_url() -> str | None:
    """GitHub Actions の実行ログ URL を組み立てる（ローカル実行時は None）。"""
    if GITHUB_RUN_ID and GITHUB_REPOSITORY:
        return f"{GITHUB_SERVER_URL}/{GITHUB_REPOSITORY}/actions/runs/{GITHUB_RUN_ID}"
    return None


def notify_start(state: dict, start_at: datetime | None, start_src: str) -> None:
    """ボット起動を Discord へ通知する。"""
    if GITHUB_EVENT_NAME == "workflow_dispatch":
        trigger = "手動実行"
    elif GITHUB_EVENT_NAME:
        trigger = f"{GITHUB_EVENT_NAME}（定時実行）"
    else:
        trigger = "ローカル実行"

    if start_at:
        start_value = (
            f"**{ts_str(start_at)}**\n"
            f"（{start_src} / 開始 {LEAD_MINUTES:g} 分前から高速監視）"
        )
    else:
        start_value = "開始予定の告知はまだ検出していません（通常間隔で監視します）"

    embed = {
        "title": "🚀 霜ばしら監視ボットを起動しました",
        "description": f"監視を開始します（この実行は最大 {MAX_SECONDS:g} 秒間）。",
        "color": 0x2ECC71,
        "fields": [
            {"name": "起動方法", "value": trigger, "inline": True},
            {"name": "前回の状態", "value": f"`{state.get('status', '(なし)')}`", "inline": True},
            {"name": "監視モード", "value":
                f"`{MODE}` / 通常 {INTERVAL_SECONDS:g} 秒・高速 {FAST_INTERVAL_SECONDS:g} 秒",
             "inline": False},
            {"name": "開始予定", "value": start_value, "inline": False},
        ],
    }
    url = _run_url()
    if url:
        embed["url"] = url
        embed["footer"] = {"text": "実行ログを開く"}
    post_discord({"username": "霜ばしら監視ボット", "embeds": [embed]})


def _notify_repeated(embed: dict, alert: bool) -> None:
    times = max(1, REPEAT_NOTIFY) if alert else 1
    for i in range(times):
        payload = {
            "username": "霜ばしら監視ボット",
            "allowed_mentions": {"parse": ["everyone", "roles", "users"]},
            "embeds": [embed],
        }
        if DISCORD_MENTION:
            payload["content"] = DISCORD_MENTION if i == 0 else f"{DISCORD_MENTION}（再送 {i + 1}/{times}）"
        elif i > 0:
            payload["content"] = f"（再送 {i + 1}/{times}）"
        post_discord(payload)
        if i < times - 1:
            time.sleep(REPEAT_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# 監視本体
# ---------------------------------------------------------------------------

def resolve_start_at(state: dict) -> tuple[datetime | None, str]:
    """env START_AT を優先し、無ければ state の announced_start_at（自動アーミング）を使う。"""
    if START_AT_RAW:
        dt = _parse_iso(START_AT_RAW)
        if dt:
            return dt, "env START_AT"
    if AUTO_ARM:
        raw = state.get("announced_start_at")
        if raw:
            dt = _parse_iso(raw)
            if dt:
                return dt, f"自動アーミング ({state.get('announced_source', '?')})"
    return None, ""


def _parse_iso(text: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(JST)


def handle_announcement_source(state: dict, source: str, text: str, url: str | None,
                               is_new: bool) -> datetime | None:
    """告知らしき文面を評価し、必要なら通知して開始日時を state に記録する。"""
    start_at = parse_start_datetime(text)
    if not is_new:
        return start_at

    if looks_like_announcement(text) or start_at:
        log(f"★ 告知を検出 ({source}): {excerpt(text, 120)}")
        if NOTIFY_ANNOUNCEMENT:
            notify_announcement(source, text, start_at, url)
        if start_at:
            state["announced_start_at"] = start_at.isoformat()
            state["announced_source"] = source
            state["announced_at"] = ts_str()
            log(f"自動アーミング: {ts_str(start_at)} を開始予定として記録しました。")
    return start_at


def check_news_source(state: dict, baseline: bool) -> None:
    """公式サイト「お知らせ」の新着を検知する。"""
    if not ENABLE_NEWS:
        return
    news = check_news()
    if not news.get("ok"):
        return

    prev_id = state.get("news_latest_id")
    first = prev_id is None
    is_new = prev_id is not None and news["id"] != prev_id

    state["news_latest_id"] = news["id"]
    state["news_latest_title"] = news["title"]
    state["news_latest_date"] = news["date"]

    if first or baseline:
        log(f"お知らせ最新: [{news['date']}] {news['title']} （基準として記録）")
        return
    if not is_new:
        log(f"お知らせ最新: [{news['date']}] {news['title']} （変化なし）")
        return

    log(f"★ お知らせに新着: [{news['date']}] {news['title']}")
    body = fetch_news_detail(news["url"])
    handle_announcement_source(state, "公式サイト お知らせ", f"{news['title']} {body}", news["url"], True)


def check_x_source(state: dict, baseline: bool) -> None:
    """X の投稿数が増えたら「新着投稿あり」として知らせる。"""
    if not ENABLE_X:
        return
    x = check_x()
    if not x.get("ok"):
        return

    prev = state.get("x_tweet_count")
    state["x_tweet_count"] = x["tweets"]

    if prev is None or baseline:
        log(f"X 投稿数: {x['tweets']} （基準として記録）")
        return
    if x["tweets"] == prev:
        log(f"X 投稿数: {x['tweets']} （変化なし）")
        return

    log(f"★ X に新しい投稿があります（{prev} → {x['tweets']} 件）")
    if NOTIFY_ANNOUNCEMENT:
        post_discord({
            "username": "霜ばしら監視ボット",
            "allowed_mentions": {"parse": ["everyone", "roles", "users"]},
            "content": DISCORD_MENTION or None,
            "embeds": [{
                "title": "🐦 公式Xに新しい投稿がありました",
                "description": (
                    f"投稿数が {prev} → **{x['tweets']}** に増えました。\n"
                    "受付開始の告知の可能性があります。内容を確認してください。\n"
                    "（X の本文は自動取得できないため、日時は自分で確認して "
                    "`START_AT` に設定すると確実です）"
                ),
                "url": x["url"],
                "color": 0x1DA1F2,
                "fields": [{"name": "確認時刻", "value": ts_str(), "inline": False}],
            }],
        })


def main() -> int:
    state = load_state()
    baseline = not state  # state が空 = 初回。初回は基準記録のみで通知しない

    start_at, start_src = resolve_start_at(state)
    if start_at:
        log(f"開始予定: {ts_str(start_at)} （{start_src}）／ 開始 {LEAD_MINUTES:g} 分前から高速監視")
    else:
        log("開始予定の告知はまだ検出していません。通常間隔で監視します。")

    log(f"前回の状態: status={state.get('status', '(なし)')}")

    if NOTIFY_START:
        notify_start(state, start_at, start_src)

    last_status = state.get("status")
    notified_purchasable = state.get("status") == "purchasable"
    last_desc_hash = state.get("item_description_hash")
    last_desc_excerpt = state.get("item_description_excerpt", "")

    deadline = time.monotonic() + MAX_SECONDS
    first = True
    detected = False
    last_news_check = 0.0
    last_x_check = 0.0

    while True:
        now = now_jst()
        in_window = bool(start_at) and now >= start_at - timedelta(minutes=LEAD_MINUTES)

        # window モードで、まだ開始前なら 1 周だけ確認して終了（Actions 時間の節約）
        if MODE == "window" and start_at and not in_window and not ONCE:
            log("開始ウィンドウ前のため、1 周だけ確認して終了します。")
            _item_round(state, last_status, notified_purchasable, last_desc_hash, last_desc_excerpt,
                        baseline, notify_item=True)
            check_news_source(state, baseline)
            check_x_source(state, baseline)
            save_state(state)
            return 0

        if not first:
            interval = FAST_INTERVAL_SECONDS if in_window else INTERVAL_SECONDS
            wait = interval + random.uniform(0, JITTER_SECONDS)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log("この実行の監視時間が終了しました。")
                break
            time.sleep(min(wait, max(0.0, remaining)))
            if time.monotonic() >= deadline:
                log("この実行の監視時間が終了しました。")
                break
        first = False

        result = _item_round(state, last_status, notified_purchasable, last_desc_hash,
                             last_desc_excerpt, baseline, notify_item=True)
        last_status, notified_purchasable, last_desc_hash, last_desc_excerpt, item_detected = result
        detected = detected or item_detected

        # 告知系はそれぞれの間隔でだけ確認する
        if time.monotonic() - last_news_check >= ANNOUNCE_INTERVAL_SECONDS:
            check_news_source(state, baseline)
            last_news_check = time.monotonic()
        if ENABLE_X and time.monotonic() - last_x_check >= X_INTERVAL_SECONDS:
            check_x_source(state, baseline)
            last_x_check = time.monotonic()

        new_start, _ = resolve_start_at(state)
        if new_start and (start_at is None or new_start != start_at):
            start_at = new_start
            log(f"開始予定を更新: {ts_str(start_at)}")

        baseline = False

        if detected and EXIT_ON_DETECT:
            log("検知したため終了します。")
            break
        if ONCE:
            break

    save_state(state)
    return 0


def _item_round(state: dict, last_status, notified_purchasable: bool,
                last_desc_hash, last_desc_excerpt, baseline: bool,
                notify_item: bool) -> tuple:
    """商品ページを 1 回確認して state を更新する。"""
    if not ENABLE_ITEM:
        return last_status, notified_purchasable, last_desc_hash, last_desc_excerpt, False

    info = check_item()
    status = info["status"]
    detected = False

    if status in ("unknown", "error", "fetch_error"):
        log(f"ステータス判定不能のためスキップ: {status} ({info.get('reason')})")
        return last_status, notified_purchasable, last_desc_hash, last_desc_excerpt, False

    changed = status != last_status
    log(f"[商品] status={status}" + (f" ({last_status} → {status})" if changed else ""))

    # --- 在庫ステータスの変化 ---
    if status == "purchasable" and not notified_purchasable and notify_item:
        log("★ 購入可能になりました！通知します。")
        notify_purchasable(info)
        notified_purchasable = True
        detected = True
    elif status == "unpurchasable" and last_status == "purchasable" and NOTIFY_SOLD_OUT:
        notify_sold_out(info)

    if changed:
        state["status"] = status
        state["reason"] = info.get("reason")
        state["price"] = info.get("price")
        state["changed_at"] = ts_str()
        last_status = status

    # --- 商品説明文の変化（告知の前触れ） ---
    desc_hash = info.get("description_hash")
    if desc_hash:
        new_excerpt = excerpt(info.get("description", ""), 400)
        if last_desc_hash is None:
            log("商品説明文を基準として記録しました。")
        elif desc_hash != last_desc_hash:
            log("★ 商品説明文が更新されました。")
            text = info.get("description", "")
            if NOTIFY_TEXT_CHANGE and not baseline:
                notify_text_change(last_desc_excerpt, new_excerpt)
            handle_announcement_source(state, "商品説明文", text, ITEM_URL, is_new=True)
        state["item_description_hash"] = desc_hash
        state["item_description_excerpt"] = new_excerpt
        last_desc_hash = desc_hash
        last_desc_excerpt = new_excerpt

    save_state(state)
    return last_status, notified_purchasable, last_desc_hash, last_desc_excerpt, detected


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
