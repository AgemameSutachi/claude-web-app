// 本アプリ（claude-web-app）からの送信を受け箱で受け取り、mod の公式の口で渡す（方式 A）
//
// 【何をするか】対話のセッションごとに2秒おきに受け箱 <家>/.claude-web-app/inbox/ を見る。
//   - <sessionId>.alive.json: mod が入っている印（版・最後に見回った時刻）。10秒ごとと /clear の後に書く。
//     本アプリはこれが新しい相手にだけ受け箱で送り、無ければ今の打鍵で送る（併存）。
//   - <sessionId>.req.json（本アプリが書く）: { id, clear, text, created, cancelled, draft }
//   - <sessionId>.ack.json（mod が書く）: { id, stage: queued|cleared|submitted|refused, reason, newSessionId }
//     ack は送信を受けた（/clear の前の）id の名前で書き続ける（本アプリが探す場所を1つにする）。
// 【書きかけ（draft）】refuse: 入力欄に文字があれば断る / keep: 触らずに送る（submit は書きかけに触れない・
//   2026-10-22 の確かめ）/ discard: $.prompt.fill で空にしてから送る。
// 【線】登録するのは session.start（見回りを始める）・turn.start / turn.complete（作業中の印）だけ。
//   道具・許可・UI・コマンドの登録はしない。$.fs.write は受け箱の下だけ。消す口は無いので、消すのは本アプリ。
// 【取り消し】submit と command.run は呼ぶと待ちの列に入り取り消せない。作業中は呼ばず、最初に呼ぶ直前に req を読み直し、
//   無い・cancelled・別の id なら打たない。呼ぶ前に ack(queued) を書く。queued の後は取り消さない
//   （/clear の後の本文も、本アプリが req を消していても送る。本アプリは queued を見たら打鍵へ戻らない）。
// 【静的解析の決まり】$ は最上位の関数にだけ渡す・$ の名前空間を変数に入れない・on のイベント名は文字列で書く。

const VERSION = '1.0.0'
// 見回りの間隔（ミリ秒）
const TICK_MS = 2000
// alive を書き直す周の間隔（5周＝10秒。本アプリは30秒より古い alive を「無い」とみなす）
const ALIVE_EVERY_TICKS = 5
// これより古い req は打たない（本アプリが落ちて残ったもの。本アプリの受け取りの待ちは約12.5秒）
const REQ_MAX_AGE_MS = 60 * 1000

// 受け箱の場所（session.start で家のフォルダから決める）
let inbox = null
// model のターンの途中か（turn.start〜turn.complete）。途中は呼ばない
let busy = false
// 1件を処理している最中か（同じ req を二重に処理しない）
let handling = false
let ticks = 0
// 最後に alive を書いた id（/clear で替わったらすぐ書き直す）
let aliveSid = null

async function resolveInbox($) {
  const home = (await $.env.get('USERPROFILE')) || (await $.env.get('HOME'))
  if (!home) return null
  return home.replace(/\\/g, '/').replace(/\/+$/, '') + '/.claude-web-app/inbox'
}

async function readJson($, path) {
  try {
    return JSON.parse(await $.fs.read(path))
  } catch (err) {
    // 無い・書きかけで読めないときは次の周で読み直す
    return null
  }
}

async function writeAlive($, sid) {
  const at = await $.clock.now()
  await $.fs.write(inbox + '/' + sid + '.alive.json', JSON.stringify({ sid, version: VERSION, at }))
  aliveSid = sid
}

async function ack($, sid, obj) {
  const at = await $.clock.now()
  await $.fs.write(inbox + '/' + sid + '.ack.json', JSON.stringify({ ...obj, at }))
}

// 呼ぶ直前に req を読み直す（本アプリが時間切れで消した・取り消したなら打たない）
async function stillWanted($, sid, req) {
  const again = await readJson($, inbox + '/' + sid + '.req.json')
  return Boolean(again && !again.cancelled && again.id === req.id)
}

// 書きかけの扱い。進めてよければ true、断ったら ack を書いて false
async function handleDraft($, sid, req, draft, extra) {
  if (draft === 'keep') return true
  if (draft === 'discard') {
    const r = await $.prompt.fill({ text: '', mode: 'replace' })
    if (r && r.isFilled === false) {
      // ダイアログの下・入力欄が無いときは消せない
      await ack($, sid, { id: req.id, stage: 'refused', reason: 'no_input_area', ...extra })
      return false
    }
    return true
  }
  const box = await $.prompt.read()
  if (box && box.text && box.text.trim()) {
    await ack($, sid, { id: req.id, stage: 'refused', reason: 'draft_present', ...extra })
    return false
  }
  return true
}

async function handle($, sid, req) {
  // /clear が済んだ後で失敗しても「/clear は済んだ」と本アプリへ伝えられるよう、進み具合を外に持つ
  const progress = { newSid: null }
  try {
    await handleInner($, sid, req, progress)
  } catch (err) {
    // queued のまま残すと、待っているのか失敗したのか本アプリから見分けられない
    const extra = progress.newSid ? { newSessionId: progress.newSid } : {}
    await ack($, sid, {
      id: req.id,
      stage: 'refused',
      reason: 'error: ' + String(err).slice(0, 300),
      version: VERSION,
      ...extra,
    })
  }
}

async function handleInner($, sid, req, progress) {
  const draft = ['refuse', 'keep', 'discard'].includes(req.draft) ? req.draft : 'refuse'
  const text = typeof req.text === 'string' ? req.text : ''
  if (!req.clear && !text) {
    await ack($, sid, { id: req.id, stage: 'refused', reason: 'empty' })
    return
  }
  // 断る判定（refuse）は読むだけなので先に、消す（discard）は req がまだ有効と確かめた後に
  // （本アプリが下げた直後に、書きかけだけ消えて何も送られない形を避ける）
  if (draft === 'refuse' && !(await handleDraft($, sid, req, draft, {}))) return
  if (!(await stillWanted($, sid, req))) return
  if (draft === 'discard' && !(await handleDraft($, sid, req, draft, {}))) return
  await ack($, sid, { id: req.id, stage: 'queued' })

  let newSid = sid
  if (req.clear) {
    await $.command.run({ command: 'clear' })
    // session.end の sessionId は終わる側の id なので、返った後に新しい id を読む
    newSid = await $.session.id()
    progress.newSid = newSid
    await writeAlive($, newSid)
    await ack($, sid, { id: req.id, stage: 'cleared', newSessionId: newSid })
    if (!text) return
    // /clear の数秒に人が打ち始めたかもしれないので、本文の前にもう一度確かめる
    if (draft === 'refuse' && !(await handleDraft($, sid, req, draft, { newSessionId: newSid }))) return
  }
  await $.prompt.submit({ text, asUser: true })
  await ack($, sid, { id: req.id, stage: 'submitted', newSessionId: newSid })
}

async function tick($) {
  ticks += 1
  if (!inbox) return
  let sid
  try {
    sid = await $.session.id()
    // alive は「mod が居る」の印なので、作業中も1件を処理している最中も書く
    // （長いターンの直後に本アプリが打鍵へ落ちないように）
    if (sid !== aliveSid || ticks % ALIVE_EVERY_TICKS === 1) await writeAlive($, sid)
  } catch (err) {
    return
  }
  if (busy || handling) return
  handling = true
  try {
    const reqPath = inbox + '/' + sid + '.req.json'
    if (!(await $.fs.exists(reqPath))) return
    const req = await readJson($, reqPath)
    if (!req || !req.id || req.cancelled) return
    const now = await $.clock.now()
    if (typeof req.created !== 'number' || now - req.created > REQ_MAX_AGE_MS) return
    const done = await readJson($, inbox + '/' + sid + '.ack.json')
    if (done && done.id === req.id) return
    await handle($, sid, req)
  } catch (err) {
    // 見回りは止めない（次の周でやり直す）
  } finally {
    handling = false
  }
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    const r = await next(e)
    // 人の居ない実行（-p・SDK）では見回らない（本アプリが送る相手は対話のセッションだけ）
    if (!e.isInteractive) return r
    inbox = await resolveInbox($)
    if (inbox) $.clock.every(TICK_MS, () => tick($))
    return r
  })
  on('turn.start', async ($, e, next) => {
    busy = true
    return next(e)
  })
  on('turn.complete', async ($, e, next) => {
    // 下請け（subagent）のターンは turn.start を出さず turn.complete だけを出す（agentId つき）。
    // 主のターンの途中で下請けが終わっても、作業中の印は外さない
    if (!e.agentId) busy = false
    return next(e)
  })
}
