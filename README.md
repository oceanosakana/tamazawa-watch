# 霜ばしら 予約販売開始・再入荷 監視ボット（GitHub Actions + Discord）

九重本舗玉澤の「霜ばしら」（`https://tamazawa.thebase.in/items/33921076`）について、

1. 商品ページの在庫ステータスを監視し、
2. 「SOLD OUT → 購入可能」に変わった**その瞬間**を検知して
3. **Discord に通知**（購入ページをワンタップで開けるリンク付き）

を行うボットです。GitHub Actions 上で動くので、自分の PC を起動しておく必要がありません。

---

## ⚠️ 最初に読んでください（重要）

### 1. これは「自動購入ボット」ではありません

BASE の利用規約 **第18条 禁止行為 第33号** には

> クローリング（クローラ、ロボット又はスパイダー等のプログラム）、スクレイピング及びその他の類似の手段によって本サービスにアクセスし、又は本サービスに関する情報を取得する行為

と明記されています。つまり**機械的な自動アクセス全般が禁止**されており、このボットも厳密にはグレーです。自動での「カート投入→決済確定」まで行う完全自動購入は、規約違反の度合いが大きく、アカウント停止・注文キャンセルのリスクがあります。また BASE の決済は 3D セキュア認証が入るため、そもそも完全自動化は安定しません。

そこでこのボットは **「検知して人間に知らせる」までにとどめ、購入操作は自分で行う**設計にしています。これが「規約リスク・失敗リスク」のバランスが最も良い現実解です。

### 2. この商品の性質上、「cron 任せ」では足りません

霜ばしらは不定期な再入荷ではなく、**「○月○日○時に受付開始」と公式 X で告知される一斉開放型**の予約販売です（前回は 9/23 19:00 開始 → 数分で完売）。しかも **GitHub の `schedule`(cron) は最短 5 分間隔で、数分〜十数分の遅延が普通にあります**。

そのため本ボットは 2 段構えです。

| モード | 用途 | 動き |
|---|---|---|
| **常時監視（cron）** | 告知を見逃したときの保険 | 5 分ごとに起動し、約 5 分間だけ 10 秒間隔で監視 |
| **ホットウィンドウ監視（手動実行）** | **本命。ここで最速を狙う** | 告知された開始時刻の 10 分前から、**2 秒間隔**で監視。数時間ぶっ通しで回せる |

「最速で買いたい」なら、**X で告知を確認 → `START_AT` を入れて手動実行**が一番効きます。

### 3. GitHub Actions の利用枠に注意

- **プライベートリポジトリ**では実行時間が消費枠（無料枠 2,000 分/月）を食います。常時監視モードは 1 日で約 1,400 分を消費するので、**あっという間に枠が尽きます**。
- **パブリックリポジトリなら GitHub Actions は無料・無制限**です。Webhook URL は Secrets に入るので公開しても漏れません。**パブリックリポジトリを強く推奨します。**
- また GitHub は一定期間リポジトリに動きがないと `schedule` を無効化します。状態が変化したときだけ `state.json` が自動コミットされるので、活動ログとしても機能します。

---

## セットアップ

### 手順 1: リポジトリを作る

GitHub で **Public** リポジトリを新規作成し、この `tamazawa-watch` フォルダの中身を丸ごと push してください。

```
tamazawa-watch/
├── .github/workflows/watch.yml   # GitHub Actions の定義
├── monitor.py                    # 監視スクリプト本体（標準ライブラリのみ）
├── state.json                    # 監視状態（Actions が自動更新・コミット）
├── .gitignore
└── README.md
```

> `monitor.py` は `state.json` を探すので、**リポジトリのルートに `monitor.py` を置く**か、workflow 内の `working-directory` を合わせてください。このフォルダ構成のまま（`tamazawa-watch/` がルート）push するのが簡単です。

### 手順 2: Discord Webhook を取得

1. 通知したい Discord サーバー → チャンネルの ⚙️「チャンネルの編集」
2. 「連携サービス」→「ウェブフック」→「新しいウェブフック」
3. 名前を「霜ばしら監視ボット」などにして **ウェブフック URL をコピー**
   （`https://discord.com/api/webhooks/xxxx/yyyy` の形）

> スマホへのプッシュ通知を確実に受け取りたい場合、Discord モバイルアプリを入れて、そのサーバーの通知を「すべてのメッセージ」に設定しておいてください。

### 手順 3: GitHub に設定を登録

リポジトリの **Settings** で以下を設定します。

- **Secrets and variables → Actions → Secrets** に追加
  - `DISCORD_WEBHOOK_URL` = 手順 2 でコピーした URL
- **Secrets and variables → Actions → Variables**（任意）に追加
  - `DISCORD_MENTION` = 例 `<@あなたのユーザーID>` や `@here`（自分にメンションを飛ばしたい場合。未設定ならメンションなし）
- **Actions → General → Workflow permissions** を **「Read and write permissions」** に変更
  （`state.json` を自動コミットするため。忘れると push の手順だけ失敗します）

### 手順 4: 動作確認

**Actions** タブ → 左の「霜ばしら 監視」→ **Run workflow** を実行。

実行ログに次のように出れば成功です。

```
[2026-10-16 18:12:32 JST] 前回の状態: unpurchasable
[2026-10-16 18:12:32 JST] ステータス: unpurchasable （unknown から変化）
```

Discord にテスト通知を送って書式を確認したい場合は、ローカルで以下を実行してください（Discord には送信されず、送信内容が画面に表示されます）。

```powershell
cd tamazawa-watch
$env:DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/xxxx/yyyy"
$env:DRY_RUN = "1"
python -c "import monitor as m; m.notify_purchasable({'price': 4860})"
```

---

## 本命の使い方：告知された開始時刻を狙い撃つ

公式 X（[@TamazawaSendai](https://twitter.com/TamazawaSendai)）で「次回受付開始は ○日 19:00」のような告知が出たら、次のようにします。

**Actions → 霜ばしら 監視 → Run workflow** で以下を入力して実行します。

| 入力 | 例 | 意味 |
|---|---|---|
| `start_at` | `2026-11-20T19:00:00` | 受付開始予定時刻（**JST**、秒は省略可） |
| `lead_minutes` | `10` | 開始 10 分前から高速監視に入る |
| `fast_interval_seconds` | `2` | 高速監視中の間隔（2 秒） |
| `max_seconds` | `19800` | 監視を続ける秒数（19800 秒 = 5.5 時間） |
| `timeout_minutes` | `350` | ジョブのタイムアウト（`max_seconds` より大きく。**最大 360**） |

実行すると、開始 10 分前までは「1 回確認して終了」、開始 10 分前を過ぎると **2 秒間隔**で張り付きます。開始と同時に購入可能になれば、**その数秒以内**に Discord へ通知が飛びます。

> ジョブの上限は 6 時間です。それ以上先の時刻を指定した場合は、`start_at` を指定せず cron の常時監視に任せるか、時間をおいて手動実行し直してください。

> キュー待ちで手動実行がすぐ始まらないことがあります。確実を期すなら、開始時刻の **1〜2 時間前**には実行を始めておきましょう（`lead_minutes` を `120` にする）。

---

## state.json について

二重通知を防ぐために、直前の状態を `state.json` に保存し、Actions が変化時のみコミットします。

```json
{
  "status": "unpurchasable",
  "reason": "out_of_stock",
  "price": 4860,
  "changed_at": "2026-10-16 18:12:32 JST"
}
```

- `status`: `purchasable`（買える） / `unpurchasable`（買えない） / `unknown`（判定不能）
- `reason`: `out_of_stock`（在庫切れ）など
- 判定不能（メンテ画面・取得失敗）のときは**通知しません**し、状態も上書きしません。

**リセットしたい場合**は `state.json` の `"status"` を `"unknown"` にして push してください。

---

## 環境変数リファレンス

| 変数 | 既定 | 説明 |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | – | 必須。Discord Webhook URL（Secrets 推奨） |
| `DISCORD_MENTION` | – | 通知時のメンション（`<@ID>` / `@here` など） |
| `ITEM_URL` | 霜ばしら | 監視する商品 URL |
| `INTERVAL_SECONDS` | `10` | 通常時の監視間隔（秒） |
| `MAX_SECONDS` | `290` | 1 回の実行で監視し続ける秒数 |
| `START_AT` | – | 開始予定時刻（JST, ISO8601）。設定すると高速監視モードへ |
| `LEAD_MINUTES` | `10` | `START_AT` の何分前から高速化するか |
| `FAST_INTERVAL_SECONDS` | `2` | 高速監視中の間隔（秒） |
| `JITTER_SECONDS` | `1.0` | 待機時間のゆらぎ（秒）。周期的アクセスを避けるため |
| `REPEAT_NOTIFY` | `3` | 検知時に通知する回数（気づきやすさのため複数回） |
| `REPEAT_INTERVAL_SECONDS` | `20` | 再送の間隔（秒） |
| `NOTIFY_SOLD_OUT` | `0` | `1` にすると売り切れ復帰も通知 |
| `EXIT_ON_DETECT` | `0` | `1` にすると検知した時点で終了（Actions 時間の節約） |
| `ONCE` | `0` | `1` にすると 1 回だけ確認して終了 |
| `STATE_PATH` | `state.json` | 状態ファイルのパス |
| `DRY_RUN` | `0` | `1` は Discord に送信せず標準出力のみ |

---

## 監視の仕組み（何を見ているか）

BASE の商品ページ HTML には、テーマが埋め込む Google Tag Manager 用の `dataLayer` があり、そこに購入可否が入っています。

```js
window.dataLayer.push({
  'item_purchasability': "unpurchasable",
  'item_unpurchasable_reason': "out_of_stock",
  'itemPrice': 4860,
});
```

本スクリプトはこの `item_purchasability` を正規表現で抽出して判定します（`purchasable` になったら購入可能）。あわせて `SOLD OUT` 要素やメンテナンス文言も見て、誤検知を防いでいます。

キャッシュされた古い HTML をつかまないよう、`Cache-Control: no-cache` ヘッダとキャッシュバスター用クエリを付けて取得しています。

---

## さらに確実にしたい場合

1. **公式の「再入荷メール通知」も併用する**
   商品ページの SOLD OUT ボタン付近にある「再入荷通知」からメールアドレスを登録できます（BASE 純正機能）。本ボットと併用すると保険が二重になります。
2. **BASE アカウントを作っておく**
   ログイン済みだと入力の手間が減り、決済も速くなります。住所・カード情報をあらかじめ登録しておきましょう。
3. **開始直前は商品ページを開いたまま待機する**
   通知が来たらワンタップで購入へ。**カート投入ではなく「決済完了」が購入確定**です（店側の説明より）。決済まで一気に進めてください。
4. **複数端末で Discord を見る**
   スマホ（モバイル通信）と PC の両方で通知を受け取れるようにしておくと安心です。

---

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| Discord に通知が来ない | `DISCORD_WEBHOOK_URL` が Secrets に入っているか確認。ローカルで `DRY_RUN=1` を付けて書式を確認 |
| `state.json` のコミット手順で失敗する | Settings → Actions → General → Workflow permissions を「Read and write permissions」に |
| ステータスが `unknown` のまま | 商品ページの構造が変わった可能性。`monitor.py` の正規表現を実ページに合わせて修正 |
| Actions が動かなくなった | リポジトリが 60 日間動いていないと `schedule` が無効化される。Actions タブで再度 Enable |
| 実行がすぐに始まらない | 手動実行はキュー待ちになることがある。開始の 1〜2 時間前に起動しておく |
| 誤検知で通知が飛んだ | `state.json` の `status` を `unknown` に戻す。`REPEAT_NOTIFY` を減らす |

---

## 免責

本ボットの利用は自己責任でお願いします。BASE の利用規約（特に第18条）およびショップの定めるルールを確認のうえ、節度ある間隔で利用してください。規約違反・アクセス過多によるアカウント停止や注文キャンセルについて、作者は責任を負いません。
