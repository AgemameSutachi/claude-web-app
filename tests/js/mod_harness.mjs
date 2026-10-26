// mod web-app-inbox（docs/web-app-inbox-mod/hooks/register.js）を偽の $ で動かす試験の台（tests/test_inbox_mod.py から呼ぶ）
// 本物の Claude Code は使わない。ファイルは記憶の中だけに置き、結果を JSON で標準出力へ書く。

import { pathToFileURL } from 'node:url'
import path from 'node:path'

const MOD = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), '../../docs/web-app-inbox-mod/hooks/register.js')
const HOME = 'C:\\Users\\tester'
const INBOX = 'C:/Users/tester/.claude-web-app/inbox'
const OLD = '11111111-2222-3333-4444-555555555555'
const NEW = '66666666-7777-8888-9999-aaaaaaaaaaaa'

// 試しごとに mod を読み直す（mod は最上位に状態を持つため）
let loadCount = 0
async function loadMod() {
  loadCount += 1
  return import(pathToFileURL(MOD).href + '?n=' + loadCount)
}

function makeWorld(opts = {}) {
  const w = {
    files: new Map(),
    sid: OLD,
    now: 1_000_000,
    calls: [],
    draft: opts.draft || '',
    timer: null,
    failSubmit: Boolean(opts.failSubmit),
    fillRefused: Boolean(opts.fillRefused),
  }
  w.$ = {
    env: { get: async (name) => (name === 'USERPROFILE' ? HOME : undefined) },
    fs: {
      read: async (p) => {
        if (!w.files.has(p)) throw new Error('ENOENT ' + p)
        return w.files.get(p)
      },
      write: async (p, text) => {
        w.files.set(p, text)
      },
      exists: async (p) => w.files.has(p),
    },
    clock: {
      now: async () => w.now,
      every: (ms, fn) => {
        w.timer = fn
        return () => {}
      },
    },
    session: { id: async () => w.sid },
    command: {
      run: async ({ command }) => {
        w.calls.push('command:' + command)
        if (command === 'clear') w.sid = NEW
        return { text: '' }
      },
    },
    prompt: {
      read: async () => ({ text: w.draft, cursor: w.draft.length }),
      fill: async ({ text }) => {
        w.calls.push('fill:' + text)
        if (w.fillRefused) return { isFilled: false, text: w.draft, cursor: 0 }
        w.draft = text
        return { isFilled: true, text, cursor: text.length }
      },
      submit: async ({ text, asUser }) => {
        if (w.failSubmit) throw new Error('submit failed')
        w.calls.push('submit:' + text + ':' + asUser)
        return { text }
      },
    },
  }
  return w
}

async function start(w, mod) {
  const hooks = {}
  mod.register((name, fn) => {
    hooks[name] = fn
  })
  const next = async (e) => e
  await hooks['session.start'](w.$, { isInteractive: true }, next)
  w.hooks = hooks
  w.next = next
}

async function tick(w, times = 1) {
  for (let i = 0; i < times; i++) {
    w.now += 2000
    await w.timer()
  }
}

function putReq(w, sid, req) {
  w.files.set(INBOX + '/' + sid + '.req.json', JSON.stringify({ id: 'r1', clear: false, text: '', created: w.now, cancelled: false, draft: 'keep', ...req }))
}

function readFile(w, sid, kind) {
  const t = w.files.get(INBOX + '/' + sid + '.' + kind + '.json')
  return t ? JSON.parse(t) : null
}

const scenarios = {
  async no_req_only_alive() {
    const w = makeWorld()
    await start(w, await loadMod())
    await tick(w, 3)
    return { calls: w.calls, alive: readFile(w, OLD, 'alive'), files: [...w.files.keys()] }
  },
  async clear_and_text_keep() {
    const w = makeWorld({ draft: 'kakikake' })
    await start(w, await loadMod())
    putReq(w, OLD, { clear: true, text: 'hello', draft: 'keep' })
    await tick(w)
    return { calls: w.calls, ack: readFile(w, OLD, 'ack'), aliveNew: readFile(w, NEW, 'alive'), draft: w.draft }
  },
  async refuse_with_draft() {
    const w = makeWorld({ draft: 'kakikake' })
    await start(w, await loadMod())
    putReq(w, OLD, { clear: true, text: 'hello', draft: 'refuse' })
    await tick(w)
    return { calls: w.calls, ack: readFile(w, OLD, 'ack') }
  },
  async refuse_without_draft() {
    const w = makeWorld({ draft: '   ' })
    await start(w, await loadMod())
    putReq(w, OLD, { text: 'hello', draft: 'refuse' })
    await tick(w)
    return { calls: w.calls, ack: readFile(w, OLD, 'ack') }
  },
  async discard_draft() {
    const w = makeWorld({ draft: 'kakikake' })
    await start(w, await loadMod())
    putReq(w, OLD, { text: 'hello', draft: 'discard' })
    await tick(w)
    return { calls: w.calls, ack: readFile(w, OLD, 'ack'), draft: w.draft }
  },
  async discard_under_dialog() {
    const w = makeWorld({ draft: 'kakikake', fillRefused: true })
    await start(w, await loadMod())
    putReq(w, OLD, { text: 'hello', draft: 'discard' })
    await tick(w)
    return { calls: w.calls, ack: readFile(w, OLD, 'ack') }
  },
  async busy_waits_and_subagent_does_not_clear_busy() {
    const w = makeWorld()
    await start(w, await loadMod())
    await w.hooks['turn.start'](w.$, { turnId: 't1' }, w.next)
    putReq(w, OLD, { text: 'hello' })
    w.files.delete(INBOX + '/' + OLD + '.alive.json')
    await tick(w)
    const aliveWhileBusy = readFile(w, OLD, 'alive')
    const callsWhileBusy = [...w.calls]
    // 下請けの turn.complete（agentId つき）では作業中の印を外さない
    await w.hooks['turn.complete'](w.$, { turnId: 's1', agentId: 'a1' }, w.next)
    await tick(w)
    const callsAfterSubagent = [...w.calls]
    await w.hooks['turn.complete'](w.$, { turnId: 't1' }, w.next)
    await tick(w)
    return { aliveWhileBusy: Boolean(aliveWhileBusy), callsWhileBusy, callsAfterSubagent, calls: w.calls }
  },
  async stale_and_cancelled_are_ignored() {
    const w = makeWorld()
    await start(w, await loadMod())
    putReq(w, OLD, { text: 'old', created: w.now - 120000 })
    await tick(w)
    const afterStale = [...w.calls]
    putReq(w, OLD, { id: 'r2', text: 'cancelled', cancelled: true })
    await tick(w)
    return { afterStale, calls: w.calls, ack: readFile(w, OLD, 'ack') }
  },
  async submit_error_after_clear_reports_new_sid() {
    const w = makeWorld({ failSubmit: true })
    await start(w, await loadMod())
    putReq(w, OLD, { clear: true, text: 'hello' })
    await tick(w)
    return { calls: w.calls, ack: readFile(w, OLD, 'ack') }
  },
  async not_reprocessed() {
    const w = makeWorld()
    await start(w, await loadMod())
    putReq(w, OLD, { text: 'once' })
    await tick(w, 3)
    return { calls: w.calls }
  },
  async non_interactive_does_nothing() {
    const w = makeWorld()
    const mod = await loadMod()
    mod.register((name, fn) => {
      if (name === 'session.start') w.start = fn
    })
    await w.start(w.$, { isInteractive: false }, async (e) => e)
    return { timer: Boolean(w.timer), files: [...w.files.keys()] }
  },
}

const name = process.argv[2]
const out = {}
for (const key of name ? [name] : Object.keys(scenarios)) {
  try {
    out[key] = await scenarios[key]()
  } catch (err) {
    out[key] = { error: String((err && err.stack) || err) }
  }
}
process.stdout.write(JSON.stringify(out))
