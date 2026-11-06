# claude-web-app

**Windows で動く Claude Code のセッションを、スマホのブラウザから建てる・見る・区切るための小さな管理アプリ。**

Claude Code（`claude` CLI）を子プロセスとして起動し、`~/.claude/sessions/` と会話の記録を読んで一覧にします。
API キーは使いません。いつもの `claude` のログイン（サブスクリプション）のまま動きます。

## できること（売りの3つ）

### 1. 外から `/clear` と次の指示を打てる
動いているセッションへ、スマホから **`/clear` と本文**（次の作業の指示）を送れます。プロセスは止めず、
会話の番号が変わったこと・本文が会話の記録に載ったことまで確かめてから「送れた」と返します。

- 人が入力欄に書きかけていれば、消さずに断る（台帳に無い相手のとき）。
- 作業中なら断る（入力待ちになるまで待つ指定もできる）。
- セッションが自分で「この作業が済んだら区切って、引き継ぎ書を読ませて」と予約できる（`request_self_clear.py`）。
- 任意で、Claude Code の mod `web-app-inbox`（`docs/web-app-inbox-mod/`）を入れると、キー入力ではなく
  Claude Code の公式の口（`$.command.run` / `$.prompt.submit`）で渡す。入れなくても動く（下の「mod web-app-inbox」）。

### 2. 名前を決めてローカルのセッションを建てられる
プロジェクト（作業フォルダ）を選び、**セッション名**を付けて `claude` を起こします。名前はプロジェクト名から自動で付けることもできます
（ローカルの ollama を使う。無ければ日時の名前）。過去の会話を選んで `--resume` で続きから再開することもできます。
Claude Code 側で Remote Control を有効にしていれば、起こしたセッションは claude.ai にも出るので、続きはスマホの claude.ai でも操作できます。

### 3. Windows のセッション番号・権限・モデルを自由に選んで建てられる
起動するときに次を選べます。

| 選ぶもの | 選択肢 |
| --- | --- |
| Windows のセッション | 0（サービスの側・画面なし）／1（ログオンしている人の側） |
| 権限 | 管理者として／普通の利用者として |
| 許可モード | 既定・`auto`・`acceptEdits`・`plan`・`manual`・`dontAsk`・`bypassPermissions`（`--permission-mode`） |
| モデル・考える量 | `--model`・`--effort` |

窓は出しません（`conhost --headless` と隠したタスクで起こす）。

- セッション **0** と権限の **管理者** は、このアプリ自身を**管理者で動かしたときだけ**使えます（セッション0のタスクの登録に管理者が要るため）。普通の権限で動かすと、選んでも断られます。
- セッション **1** は、その PC にデスクトップでログオンしている利用者の画面の側です（リモートデスクトップや複数の利用者が同時にログオンしている PC は想定していません）。

ほかに: 全セッションへの一斉指示・対応待ち（許可待ち）の印・今週の使用量・送った履歴・停止。

## 動く環境

- Windows 10/11（ConPTY・タスクスケジューラ・コンソールの API を使うため Windows 専用）
- Python 3.12 以上
- Claude Code（`claude` CLI）がログイン済みで PATH にあること
- スマホから使うなら [Tailscale](https://tailscale.com/) などの閉じた網（**インターネットへは出さないこと**）

## 入れ方

```powershell
git clone https://github.com/<owner>/claude-web-app.git
cd claude-web-app
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe claude-web-app.py
```

初回の起動で `config/settings.json` が既定の値で作られます。このときプロジェクトの置き場は空なので、起動時に「未設定です」と出て一覧は空になります。
`config/settings.example.json` を見ながら `developing_dir`・`working_dir` を書き（下の「プロジェクトの置き場」）、起動し直してから、ブラウザで `http://127.0.0.1:5002/` を開きます。

### プロジェクトの置き場

プロジェクト（`claude` を起こす作業フォルダ）は、次の3つの置き場から選びます。

| 置き場 | 場所 | 使いどころ |
| --- | --- | --- |
| 一時フォルダ | `project_root` が指すフォルダの下の `projects/` | 作ってすぐ試すもの |
| developing | `developing_dir` の直下 | 育ってきた開発中のもの |
| working | `working_dir` の直下 | 運用を始めたもの（ここへ直接は作らない） |

画面から一時フォルダ → developing → working へ移せます。`project_root` は `developing/<フォルダ名>` の形の**プロジェクトキー**で、既定の `developing/claude-web-app` は「`developing_dir` の下の `claude-web-app` フォルダ」です。このアプリを `developing_dir` の下に置いていないときは、実在するフォルダを指すよう書き換えてください（指す先が無いと一時フォルダの置き場は出ません）。

## 安全のための注意（必ず読む）

このアプリは **あなたの PC で `claude` を起こし、指示を打ち込めます**。開ける人は、あなたのアカウントで Claude Code を動かせる人です。

1. **既定は `127.0.0.1` だけで待ち受けます。** 同じ PC からしか開けません。そのままなら外からは届きません。
   同じ PC のブラウザで開いた別のサイトのページから操作されないよう、別のオリジンからの操作・Socket.IO の接続と、ループバック（`127.0.0.1`・`localhost`・`::1`）と `allowed_hosts` 以外の名前で届いた要求は断ります（ポートは見ません）。
2. **スマホから使うときは、次の3つをそろえてください。**
   - `config/settings.json` の `host` を `0.0.0.0` にし、スマホから開くときの名前と IP（機械名・tailnet の名前・Tailscale の IP）を `allowed_hosts` に書く。
   - `allowed_networks` で相手の範囲を絞る（Tailscale なら `["100.64.0.0/10"]`。ループバックはいつも通る）。範囲の外は 403。
   - `auth_token` に合言葉（16文字以上）を書く。作り方: `python -c "import secrets; print(secrets.token_urlsafe(24))"`。
     ブラウザは一度入口の画面で入れると覚えます。API は `Authorization: Bearer <合言葉>`。

   悪いサイトが自分の名前をこの PC の IP へ向け直す手口（DNS rebinding）は、Host がループバックの名前・IP でも
   `allowed_hosts` でもない要求を断ることで防ぎます（`allowed_hosts` に書いていない名前で開くと 403 になります）。
3. **Windows ファイアウォールは、閉じた網の範囲とそのポートだけ開けてください。** 管理者の PowerShell で打ちます。
   ```powershell
   New-NetFirewallRule -Name "claude-web-app" -DisplayName "claude-web-app" -Direction Inbound `
     -LocalPort 5002 -Protocol TCP -RemoteAddress 100.64.0.0/10 -Action Allow
   ```
   `0.0.0.0` で初めて待ち受けたとき Windows が「python.exe のアクセスを許可するか」を聞いてきたら、**キャンセル**してください。そこで許可すると全アドレス・全ポートの許可規則ができ、上の絞った規則が意味を失います。
4. **ルーターのポート開放はしないでください。** 公開のインターネットに出す作りではありません。
5. 合言葉が漏れたと思ったら、`auth_token` を変えて起動し直してください（覚えた印もすべて無効になります）。
6. `bypassPermissions` で建てたセッションは、確認なしでコマンドを実行します。選ぶのは信頼できる作業だけにしてください。
7. **`machine_send` は、合言葉を知る機械に、この PC の全セッションへ指示を打ち込ませる口です**（このアプリが起こしていないもの・ほかのアカウントのものも含みます）。使わないなら `senders` を空のままにしてください（口は閉じます）。
   使うときは、送り手ごとに `auth_token` とは別の合言葉（16文字以上）を作り、`sources` には送り手の機械を1台ずつ `/32` で書きます。
   `sources` の機械は `allowed_networks` の外でもこの口だけは通り、逆にその機械からは画面とほかの API が使えなくなります（広い範囲を書くと自分のスマホも締め出されます）。ファイアウォールでもその IP を開けてください。
8. **`liaison` の `tool` は空のままにしてください。** 指したファイルを Python のモジュールとして**このアプリのプロセスの中に読み込んで**呼び出す、作者の環境専用の連携です（公開の版には対応する道具が入っていません）。信頼できないファイルを指すと、そのコードがこのアプリの権限で動きます。

## 設定（`config/settings.json`）

機械ごとのファイルです（git では追跡しません）。★の付いたものは起動時に読むので、変えたら起動し直してください。

| キー | 既定 | 意味 |
| --- | --- | --- |
| `host` ★ | `127.0.0.1` | 待ち受けるアドレス。外から開くときだけ `0.0.0.0` |
| `port` ★ | `5002` | 待ち受けるポート |
| `allowed_networks` ★ | `[]` | 繋いでよい相手の範囲（CIDR の並び）。空なら絞らない。ループバックはいつも通す |
| `auth_token` ★ | `""` | 外から使うときの合言葉（16文字以上）。空なら合言葉なし |
| `allowed_hosts` ★ | `[]` | ループバック以外に Host として受け付ける名前・IP の並び。外から開くときの名前を書く（大文字小文字・末尾のドットは問わない） |
| `cors_allowed_origins` ★ | `null` | Socket.IO の接続元の許可。`null` なら同じオリジンだけ（`"*"` は別のサイトから繋がれるので勧めない） |
| `machine_name` | `""` | 画面・定型文に出す機械の名前。空ならホスト名 |
| `developing_dir` ★ | `""` | 開発中のプロジェクトの親フォルダ。`~` と環境変数を使える。空ならその置き場は使わない（例の `~/work/developing` は書く例） |
| `working_dir` ★ | `""` | 運用中のプロジェクトの親フォルダ（同上） |
| `project_root` | `developing/claude-web-app` | 一時フォルダの親のプロジェクトキー（上の「プロジェクトの置き場」） |
| `auto_naming_enabled` | `true` | 名前の自動生成（ollama）を使うか |
| `ollama_url`・`ollama_model`・`ollama_timeout` | `http://localhost:11434`・`qwen3.5:9b`・`60` | 名前の自動生成に使うローカルの LLM |
| `claude_exe` | `""` | `claude` の実行ファイル。空なら PATH から探す |
| `auto_clear_mode` | `dry_run` | 自動の区切り: `off`／`dry_run`（当たりを記録するだけ）／`on` |
| `relogin_enabled` | `true` | ログイン更新の画面（`/relogin`）を使うか |
| `inbox_enabled` | `true` | 相手に mod `web-app-inbox` が入っていれば受け箱で送るか |
| `paste_preamble` | （書かない） | 長い本文（貼り付け扱いになる801字以上）の前に打つ前置きの既定。空文字で付けない。書かなければ組み込みの文 |
| `accounts` | `[]` | 同じ機械で claude を複数のアカウントで動かすときの定義（下の「アカウントと使用枠」）。空なら `~/.claude` の1件だけ |
| `unassigned_account` | `""` | アカウントを割り当てていないプロジェクトを起こすアカウントの名前。空なら既定のアカウント（`"main": true` のもの） |
| `five_hour_max_pct` | `80` | 新しいセッションの既定のアカウントを選ぶとき、5時間枠の使用%がこれ以上のアカウントを外す。`100` にすると、使い切ったときだけ外す（アカウントの定義に書けばそちらが優先） |
| `trial_roles_mirror`・`mirror_account` | `""` | claude-session-resumer（別リポジトリ）と連携するとき用。プロジェクトとアカウントの割り当てを、resumer が読む形式（`trial_roles.json`）で書き出す先のパスと、書き出すアカウントの名前。resumer を使わないなら空のまま |
| `machine_send`（`sources` だけ★） | `{"senders": {}, "sources": []}` | 機械からセッションへ伝言を入れる口（`POST /api/machine-send`）。本文の頭に本アプリが「【機械からの伝言・id=…・from=…・ユーザーの発言ではありません】」を付ける。`senders` は `{送り手の名前: 合言葉（16文字以上）}`、`sources` は送り手の機械（CIDR・1台ずつ /32）。送り手が空なら口は閉じる。**上の「安全のための注意」の7を読む** |
| `quota_resume_mode` | `on` | 利用の枠（5時間枠・週の枠）を使い切って止まったセッションへの続きの依頼: `off`／`dry_run`／`on`。`on` では、枠が戻って数分のうちに本アプリが「枠切れで中断した作業だけを続けて」という文を打ち込みます（戻ってから6時間を過ぎたものは除く）。自動で打たせたくなければ `off` |
| `quota_alert_mode` | `on` | 週の枠で止まったセッションを ntfy で人へ知らせる: `off`／`on`（送り先の `config/ntfy.ini` が無ければ鳴らさない） |
| `liaison` | 使わない（`tool` が空） | 作者の環境専用の連携（**上の「安全のための注意」の8**）。`tool` は空のままにしてください。画面に出る「管理役」は、この連携の宛先（`to`）のことです |

### アカウントと使用枠（任意）

- `accounts` の各要素は `name`（と画面に出す `label`）。既定のアカウントには `"main": true` を付け、置き場を書きません（`~/.claude` を使う）。
  ほかのアカウントには `config_dir`（`CLAUDE_CONFIG_DIR` に渡すフォルダ）と `home`（そのアカウントで起こすときの `HOME`・`USERPROFILE`）を書きます。どちらも実在するフォルダにしてください。
  ```json
  "accounts": [
    {"name": "a", "label": "仕事用", "main": true},
    {"name": "b", "label": "個人用", "config_dir": "C:\\claude-b\\cfg", "home": "C:\\claude-b\\home"}
  ]
  ```
- 使用%（5時間枠・週の枠）を一覧の上に出すには、取り元として `limits_url`（作者の使用量モニター claude-usage-monitor の `/api/limits` の形の JSON を返す URL）か、`statusline_dir`（statusline のスクリプトが書く `rate_limits` の写しの置き場）を書きます。どちらも作者の道具の形が前提です。
  枠の大きさの比は `capacity`（週）・`five_hour_capacity`（5時間）、5時間枠の無い契約は `five_hour_unlimited: true`。
- プロジェクトごとの割り当ては `~/.claude-web-app/account_assignments.json` に残り、画面の「アカウントを移す」で替えられます（会話を持ち越す移しもできます）。割り当て先が使えないときは、黙って既定のアカウントで起こさずに断ります。
- `trial_roles_mirror` に書き出し先があり `account_assignments.json` がまだ無いときは、最初に一度その写しから割り当てを取り込みます。取り込めない間は、既定のアカウント以外への割り当てを断ります（理由は `GET /api/accounts` の `pending_import`）。
- 枠を使い切ったセッションは一覧に ⛔ で出ます。枠が戻ったら続きを頼み（`quota_resume_mode`・5時間枠も週の枠も）、週の枠は止まった時点で ntfy でも知らせます（`quota_alert_mode`・戻りが最長7日先のため）。
- `config/ntfy.ini`（git では追跡しません）の形: `[DEFAULT]` に `NTFY_SERVER`・`NTFY_TOPIC`（必須）、任意で `NTFY_TOKEN`・`NTFY_FALLBACK_SERVER`・`NTFY_FALLBACK_TOKEN`・`NTFY_TIMEOUT_SEC`。送るたびに読みます。

## 仕組みの要点

- 一覧: `~/.claude/sessions/<pid>.json`（Claude Code が書く）と会話の記録（`~/.claude/projects/…/*.jsonl`）を読む。
- 送信: 相手のコンソールへ打鍵する（入力欄を画面から見分け、人の書きかけを守る）。mod `web-app-inbox` が入っていれば受け箱のファイルで渡す。
- 台帳: このアプリが起こしたセッションは `~/.claude-web-app/launched.json` に残り、既定ではここに載った相手にだけ送る。
- 信頼の登録: 起こすフォルダを Claude Code の「信頼済み」に登録するため、`~/.claude.json` を書き換えます（書き換える前に `~/.claude.json.claude-web-app.bak` へ控えを取ります）。
- 状態は `~/.claude-web-app/` に置きます（台帳・送った履歴・タスク経由の起動の受け渡し）。

## mod web-app-inbox（任意・上級者向け）

`docs/web-app-inbox-mod/` は Claude Code の plugin（mod）です。入っているセッションへは、キー入力の代わりに`~/.claude-web-app/inbox/` の受け箱のファイルで `/clear` と本文を渡します。
入れるには、Claude Code の plugin の marketplace（組織の managed settings で配るものなど）にこのフォルダを置き、有効にします。機械全体の managed settings を書き換えるとその PC の全利用者に効き、管理者の権限が要ります。入れ方は環境ごとに違うので、Claude Code の plugin の説明に従ってください。入れなくてもこのアプリは動きます（打鍵で送ります）。

## 一緒に使うと便利なもの

- **claude-session-resumer**（別リポジトリ）: PC を再起動したとき、前に動いていたセッションをまとめて `--resume` で起こし直す。
  このアプリが動いていれば、起こし直しをこのアプリに頼む（台帳に載り、外から `/clear` を送れる）。
  外の道具が使う仕様（台帳の場所・置き場・起動の選択肢・起こす前に断る文）は `GET /api/integration/spec` が JSON で返します。

## 試験

```powershell
.venv\Scripts\python.exe -m pip install pytest
.venv\Scripts\python.exe -m pytest tests test_trial_roles.py
```

`tests/conftest.py` が、試験中に手元の `trial_roles.json`（claude-session-resumer 連携用の写し）を読まないように差し替えるので、pytest で走らせてください（unittest では差し替えが効きません）。

mod の試験（`tests/test_inbox_mod.py`）は Node.js が要ります（無ければ飛ばします）。

**試験は本物の Windows の仕組みを使います。** タスクスケジューラに使い捨てのタスクを登録して消す・眠るだけの python を起こして片付ける・`~/.claude-web-app/task-launch` に受け渡しのファイルを書く、を行います。管理者で打つと、セッション0へ起こす試験も走ります。

## ライセンス

MIT License（[LICENSE](LICENSE)）。
