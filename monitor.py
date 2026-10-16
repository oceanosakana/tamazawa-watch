#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
霜ばしら（九重本舗玉澤 / BASE ショップ）予約販売開始・再入荷 監視スクリプト

やること:
  1. 商品ページを取得し、在庫ステータス（item_purchasability）を解析する
  2. 「買えない状態」->「買える状態」に変化した瞬間を検知する
  3. Discord Webhook に通知する（購入ページへのワンタップリンク付き）
  4. 状態を state.json に保存する（GitHub Actions がリポジトリにコミットして次回実行へ引き継ぐ）

依存ライブラリなし（Python 標準ライブラリのみ）。

主な環境変数:
  DISCORD_WEBHOOK_URL   必須。Discord の Webhook URL
  DISCORD_MENTION       任意。例: "<@1234567890>" / "@here" / "@everyone"
  ITEM_URL              監視対象 URL（既定: 霜ばしら）
  INTERVAL_SECONDS      通常の監視間隔 秒（既定 10）
  MAX_SECONDS           1 回のジョブで監視し続ける秒数（既定 290）
  START_AT              販売開始予定時刻 (JST, ISO8601) 例: 2026-10-20T19:00:00
  LEAD_MINUTES          START_AT の何分前から高速監視に入るか（既定 10）
  FAST_INTERVAL_SECONDS START_AT 前後で使う短い監視間隔（既定 2）
  JITTER_SECONDS        待機時間のゆらぎ 秒（既定 1.0）
  REPEAT_NOTIFY         検知時に通知を送る回数（既定 3）
  REPEAT_INTERVAL_SECONDS 再送の間隔 秒（既定 20）
  NOTIFY_SOLD_OUT       1 なら売り切れ復帰も通知（既定 0）
  EXIT_ON_DETECT        1 なら検知した時点で終了（既定 0）
  ONCE                  1 なら 1 回だけ確認して終了
  STATE_PATH            state.json のパス（既定 state.json）
  DRY_RUN               1 なら Discord に送信せず標準出力のみ
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

# --- 設定（環境変数から読み込み） ------------------------------------------

ITEM_URL = os.environ.get("ITEM_URL", "https://tamazawa.thebase.in/items/33921076").strip()

SHOP_MATCH = re.search(r"https?://([a-z0-9\-]+)\.thebase\.in/items/(\d+)", ITEM_URL)
SHOP_ID = SHOP_MATCH.group(1) if SHOP_MATCH else "tamazawa"
ITEM_ID = SHOP_MATCH.group(2) if SHOP_MATCH else "33921076"
CART_ADD_URL = f"https://{SHOP_ID}.thebase.in/cart/add/{SHOP_ID}"

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_MENTION = os.environ.get("DISCORD_MENTION", "").strip()

INTERVAL_SECONDS = float(os.environ.get("INTERVAL_SECONDS", "10"))
MAX_SECONDS = float(os.environ.get("MAX_SECONDS", "290"))
JITTER_SECONDS = float(os.environ.get("JITTER_SECONDS", "1.0"))
FAST_INTERVAL_SECONDS = float(os.environ.get("FAST_INTERVAL_SECONDS", "2"))
LEAD_MINUTES = float(os.environ.get("LEAD_MINUTES", "10"))
START_AT_RAW = os.environ.get("START_AT", "").strip()

REPEAT_NOTIFY = int(os.environ.get("REPEAT_NOTIFY", "3"))
REPEAT_INTERVAL_SECONDS = float(os.environ.get("REPEAT_INTERVAL_SECONDS", "20"))

NOTIFY_SOLD_OUT = os.environ.get("NOTIFY_SOLD_OUT", "0") == "1"
EXIT_ON_DETECT = os.environ.get("EXIT_ON_DETECT", "0") == "1"
ONCE = os.environ.get("ONCE", "0") == "1"
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

STATE_PATH = os.environ.get("STATE_PATH", "state.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# --- 商品ページの解析 -------------------------------------------------------

RE_PURCHASABILITY = re.compile(r"'item_purchasability'\s*:\s*\"([^\"]+)\"")
RE_UNPURCHASABLE_REASON = re.compile(r"'item_unpurchasable_reason'\s*:\s*\"([^\"]+)\"")
RE_ITEM_PRICE = re.compile(r"'itemPrice'\s*:\s*(\d+)")
RE_SOLD_OUT_ELEMENT = re.compile(r"purchaseButton__btn--(?:soldOut|noItem|outOfStock)")

# メンテナンス / エラーページを検知するためのキーワード
ERROR_MARKERS = ("メンテナンス", "Maintenance", "現在、ショップをご利用いただけません", "システムエラー")


def now_jst() -> datetime:
    return datetime.now(JST)


def ts_str(dt: datetime | None = None) -> str:
    dt = dt or now_jst()
    return dt.strftime("%Y-%m-%d %H:%M:%S JST")


def log(msg: str) -> None:
    print(f"[{ts_str()}] {msg}", flush=True)


def fetch_html(url: str, cache_bust: bool = True) -> str:
    """商品ページ HTML を取得する。no-cache ヘッダ + クエリで CDN キャッシュを避ける。"""
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
        raw = res.read()
    return raw.decode("utf-8", "replace")


def analyze(html: str) -> dict:
    """HTML から在庫ステータスを判定する。"""
    purch = RE_PURCHASABILITY.search(html)
    reason = RE_UNPURCHASABLE_REASON.search(html)
    price = RE_ITEM_PRICE.search(html)

    purchasability = purch.group(1) if purch else None
    unpurchasable_reason = reason.group(1) if reason else None

    sold_out = bool(RE_SOLD_OUT_ELEMENT.search(html)) or ("endOfSale" in html)

    if any(m in html for m in ERROR_MARKERS):
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
    }


# --- 状態の保存 / 読み込み --------------------------------------------------

def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # 壊れていても動くように
        log(f"state.json の読み込みに失敗したので無視します: {exc}")
        return {}


def save_state(state: dict) -> bool:
    """state.json を書き出す。内容が変わった場合のみ True を返す。"""
    old = load_state()
    if old == state:
        return False
    tmp = f"{STATE_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, STATE_PATH)
    return True


# --- Discord 通知 -----------------------------------------------------------

def post_discord(payload: dict) -> None:
    if DRY_RUN:
        log("DRY_RUN: Discord へ送信しません。送信内容は次の通り:")
        print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
        return
    if not DISCORD_WEBHOOK_URL:
        log("DISCORD_WEBHOOK_URL が未設定のため通知できません。")
        return

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            res.read()
        log("Discord へ通知しました。")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            parsed = json.loads(exc.read().decode("utf-8", "replace"))
            detail = str(parsed)
            # レートリミットは retry_after 秒待って 1 回だけ再送
            if exc.code == 429:
                wait = float(parsed.get("retry_after", 1.0)) + 0.5
                log(f"Discord レートリミット。{wait:.1f} 秒待って再送します。")
                time.sleep(wait)
                post_discord(payload)
                return
        except Exception:
            pass
        log(f"Discord への通知に失敗しました: HTTP {exc.code} {detail}")
    except Exception as exc:
        log(f"Discord への通知に失敗しました: {exc}")


def notify_purchasable(info: dict) -> None:
    price = info.get("price")
    price_text = f"¥{price:,}" if price else "¥4,860"

    purchase_link = f"[▶ 購入ページを開く（タップで商品ページ）]({ITEM_URL})"
    cart_link = f"[🛒 カートに追加（要ログイン）]({CART_ADD_URL})"

    base = {
        "username": "霜ばしら監視ボット",
        "allowed_mentions": {"parse": ["everyone", "roles", "users"]},
        "embeds": [
            {
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
                    {"name": "購入", "value": f"{purchase_link}\n{cart_link}", "inline": False},
                ],
                "footer": {"text": "ただし規約上、自動購入は推奨されません。自分で操作して購入してください。"},
            }
        ],
    }

    for i in range(max(1, REPEAT_NOTIFY)):
        payload = dict(base)
        if DISCORD_MENTION:
            payload["content"] = DISCORD_MENTION if i == 0 else f"{DISCORD_MENTION}（再送 {i + 1}/{REPEAT_NOTIFY}）"
        elif i > 0:
            payload["content"] = f"（再送 {i + 1}/{REPEAT_NOTIFY}）"
        post_discord(payload)
        if i < REPEAT_NOTIFY - 1:
            time.sleep(REPEAT_INTERVAL_SECONDS)


def notify_sold_out(info: dict) -> None:
    payload = {
        "username": "霜ばしら監視ボット",
        "embeds": [
            {
                "title": "😢 売り切れに戻りました（霜ばしら）",
                "description": f"ステータス: `{info.get('status')}` / 理由: `{info.get('reason')}`",
                "url": ITEM_URL,
                "color": 0x95A5A6,
                "fields": [{"name": "時刻", "value": ts_str(), "inline": False}],
            }
        ],
    }
    post_discord(payload)


# --- 監視ループ -------------------------------------------------------------

def parse_start_at() -> datetime | None:
    if not START_AT_RAW:
        return None
    text = START_AT_RAW.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        log(f"START_AT の書式が不正です（無視します）: {START_AT_RAW!r}")
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(JST)


def check_once() -> dict:
    """1 回取得して解析する。取得に失敗したら status='fetch_error'。"""
    try:
        html = fetch_html(ITEM_URL)
        info = analyze(html)
        info["http_ok"] = True
        return info
    except Exception as exc:
        log(f"取得に失敗しました: {exc}")
        return {"status": "fetch_error", "reason": str(exc), "sold_out": None, "price": None, "http_ok": False}


def main() -> int:
    start_at = parse_start_at()
    if start_at:
        log(f"販売開始予定: {ts_str(start_at)} ／ {LEAD_MINUTES} 分前から高速監視します。")

    state = load_state()
    log(f"前回の状態: {state.get('status', '(なし)')}")
    last_status = state.get("status")

    # 通知済み判定用: state 上の status が既に purchasable なら通知済みとみなす
    notified_purchasable = state.get("status") == "purchasable"

    deadline = time.monotonic() + MAX_SECONDS
    first = True
    detected = False

    while True:
        now = now_jst()

        # START_AT が設定されていて、まだ高速監視の時間帯に入っていない場合
        if start_at is not None and not ONCE:
            fast_from = start_at - timedelta(minutes=LEAD_MINUTES)
            if now < fast_from:
                log(f"高速監視の開始時刻（{ts_str(fast_from)}）前です。1 回だけ確認して終了します。")
                info = check_once()
                log(f"現在のステータス: {info['status']} (理由: {info.get('reason')})")
                update_state(state, info)
                return 0
            interval = FAST_INTERVAL_SECONDS
        else:
            interval = 1.0 if ONCE else INTERVAL_SECONDS

        if not first:
            sleep_for = interval + random.uniform(0, JITTER_SECONDS)
            # ジョブの残り時間を超えないように
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log("ジョブの監視時間が終了しました。")
                break
            time.sleep(min(sleep_for, max(0.0, remaining)))
            if time.monotonic() >= deadline:
                log("ジョブの監視時間が終了しました。")
                break
        first = False

        info = check_once()
        status = info["status"]

        if status in ("unknown", "error", "fetch_error"):
            log(f"ステータス判定不能のためスキップ: {status} ({info.get('reason')})")
            if ONCE:
                return 0
            continue

        changed = status != last_status
        log(f"ステータス: {status}" + (f" （{last_status} から変化）" if changed else ""))

        if status == "purchasable" and not notified_purchasable:
            log("★ 購入可能になりました！通知します。")
            notify_purchasable(info)
            notified_purchasable = True
            detected = True
        elif status == "unpurchasable" and last_status == "purchasable" and NOTIFY_SOLD_OUT:
            notify_sold_out(info)

        if changed:
            update_state(state, info)
            last_status = status

        if detected and EXIT_ON_DETECT:
            log("検知したので終了します。")
            break
        if ONCE:
            break

    return 0


def update_state(state: dict, info: dict) -> None:
    """state.json を更新する。status / reason に変化があったときだけ書き込む。

    ※ 毎回書き込むと GitHub Actions のコミットが毎回発生してしまうため、
      変化があったときのみ更新する。
    """
    status = info.get("status")
    if status in (None, "unknown", "error", "fetch_error"):
        return
    reason = info.get("reason")
    if state.get("status") == status and state.get("reason") == reason:
        return

    new_state = {
        "status": status,
        "reason": reason,
        "price": info.get("price"),
        "changed_at": ts_str(),
    }
    if save_state(new_state):
        log(f"state.json を更新しました: status={status} reason={reason}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
