// correction-band: a band above the prompt for corrections and pending recall reviews.
//
// prompt.submit: asks ~/.claude/hooks/correction-nudge.py --match whether the
//   prompt has a correction shape. The matcher is owned by that script; this
//   file holds no pattern. A match is kept in $.state and drawn as a row with
//   Log it / Not a correction buttons. The prompt itself always passes on
//   unchanged: this module never drops or rewrites a prompt.
// session.start: reads ~/.claude/recall-context-heartbeat.json and keeps the
//   pending candidate count (stale after 6 h, the rule recall-review-nudge.py uses).
// ui.render (AbovePrompt): up to two rows, one per pending item, each a Text
//   line plus buttons. Nothing pending means next(e): the band is not drawn.
//
// Reads: the heartbeat file. Runs: python3 correction-nudge.py --match with the
// prompt on stdin. Submits: the log-it instruction, as the mod, never as the user.
// Review runs /recall review through $.command.run, else fills the prompt box with it.
// Fail open: any error, timeout, non-zero exit, or unparsable output leaves the
// prompt flowing and the band absent. CORRECTION_BAND_OFF=1 turns the mod off.
//
// The host reads on(...) and $.noun.method(...) from source, so they are
// spelled literally, and helpers that take $ are top-level functions.
import { atom, read, update } from 'claude-code'

const CORRECTION = atom({ plugin: 'correction-band', key: 'correction' }, null)
const RECALL = atom({ plugin: 'correction-band', key: 'recall' }, null)

const NAME = 'correction-band'
const MAX_PROMPT_CHARS = 1200
const STALE_MS = 6 * 3600 * 1000
// Prompts that are not the operator's own words; the matcher is never asked.
const SKIP_PREFIXES = ['/', '<', '[Request interrupted', 'This session is being continued', 'Caveat: The messages below']

function logItText(text) {
  return `Record this correction I gave with correction-log: "${text}". Run ~/.local/bin/correction-log --scope <this repo or *> --job … --not … --use … --why … --source …, point --use at the rule's owner and write the owner first if there is none. Then continue the task.`
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    const result = await next(e)
    await loadRecall($)
    return result
  })

  on('prompt.submit', async ($, e, next) => {
    try {
      if ((await $.env.get('CORRECTION_BAND_OFF')) === '1') return next(e)
      if (next.origin?.plugin === NAME) return next(e)
      if (!isOperatorText(e.text)) return next(e)
      // The row never outlives the prompt that matched.
      await update($, CORRECTION, () => null)
      await checkPrompt($, e.text)
    } catch {
      // Fail open: the prompt goes on as typed.
    }
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const correction = await read($, CORRECTION)
    const recall = await read($, RECALL)
    const rows = []
    if (correction) rows.push(correctionRow($, e, correction))
    const recallRow = recall ? recallLine($, e, recall) : null
    if (recallRow) rows.push(recallRow)
    const room = Math.min(2, e.props?.maxRows ?? 2)
    if (rows.length === 0 || room < 1) return next(e)
    const { Box } = $.ui.resolve(e)
    return Box({ flexDirection: 'column', paddingX: 1, children: rows.slice(0, room) })
  })
}

function isOperatorText(text) {
  if (typeof text !== 'string' || text.length === 0 || text.length > MAX_PROMPT_CHARS) return false
  return !SKIP_PREFIXES.some((p) => text.startsWith(p))
}

async function checkPrompt($, text) {
  const home = await $.env.get('HOME')
  if (!home) return
  const r = await $.process.run(['python3', `${home}/.claude/hooks/correction-nudge.py`, '--match'], {
    timeoutMs: 1500,
    stdin: JSON.stringify({ prompt: text }),
  })
  if (r.exitCode !== 0) return
  const out = JSON.parse(r.stdout.trim().split('\n').pop() ?? '')
  if (!out || typeof out.matched !== 'string' || !out.matched) return
  await update($, CORRECTION, () => ({
    text: text.slice(0, 160),
    pattern: out.matched,
    phrase: String(out.phrase ?? ''),
    at: Date.now(),
  }))
  $.ui.invalidate('ui.render')
}

async function loadRecall($) {
  try {
    const home = await $.env.get('HOME')
    const raw = await $.fs.read(`${home}/.claude/recall-context-heartbeat.json`)
    const hb = JSON.parse(raw)
    const at = typeof hb.last_success === 'string' ? Date.parse(hb.last_success) : NaN
    const stale = Number.isNaN(at) || Date.now() - at > STALE_MS
    const candidates = Number.isFinite(hb.candidates) ? Number(hb.candidates) : 0
    await update($, RECALL, () => ({
      candidates,
      lastSuccess: Number.isNaN(at) ? null : String(hb.last_success),
      stale,
      error: typeof hb.error === 'string' ? hb.error : null,
      reviewedAt: null,
    }))
  } catch {
    await update($, RECALL, () => null)
  }
  $.ui.invalidate('ui.render')
}

function ageText(lastSuccess) {
  const at = lastSuccess ? Date.parse(lastSuccess) : NaN
  if (Number.isNaN(at)) return 'never'
  const hours = (Date.now() - at) / 3600000
  return hours < 48 ? `${Math.round(hours)}h ago` : `${Math.round(hours / 24)}d ago`
}

// Cut text to fit `room` cells, ending in an ellipsis; single-width characters only.
function fit(text, room) {
  const flat = text.replace(/\s+/g, ' ').trim()
  if (room < 4) return ''
  return flat.length <= room ? flat : `${flat.slice(0, room - 1)}…`
}

function correctionRow($, e, correction) {
  const { Box, Text, Button } = $.ui.resolve(e)
  const columns = e.props?.bodyColumns ?? 80
  const buttons = 20 + 10 // "[ Not a correction ]" + "[ Log it ]" with spacing
  const phrase = fit(correction.phrase, columns - buttons - 16)
  return Box({
    flexDirection: 'row',
    children: [
      Text({ children: `Correction? "${phrase}"  ` }),
      Button({
        key: 'log',
        label: 'Log it',
        hotkey: '1',
        onPress: async () => {
          await $.prompt.submit({ text: logItText(correction.text) })
          await update($, CORRECTION, () => null)
          $.ui.invalidate('ui.render')
        },
      }),
      Button({
        key: 'dismiss',
        label: 'Not a correction',
        hotkey: '2',
        onPress: async () => {
          await update($, CORRECTION, () => null)
          $.ui.invalidate('ui.render')
        },
      }),
    ],
  })
}

function recallLine($, e, recall) {
  if (recall.reviewedAt) return null
  const showCount = recall.candidates > 0
  if (!showCount && !recall.stale) return null
  const { Box, Text, Button } = $.ui.resolve(e)
  const children = []
  if (recall.stale) {
    children.push(Text({ dimColor: true, children: `recall sync stale (${ageText(recall.lastSuccess)})  ` }))
  }
  if (showCount) {
    const n = recall.candidates
    children.push(Text({ children: `Recall: ${n} memory candidate${n === 1 ? '' : 's'} pending review  ` }))
    children.push(
      Button({
        key: 'review',
        label: 'Review',
        hotkey: '3',
        onPress: async () => {
          try {
            await $.command.run({ command: 'recall', args: 'review' })
          } catch {
            // The host refuses a $.prompt.submit text that begins with /; fill the box for the person to send.
            await $.prompt.fill({ text: '/recall review', mode: 'replace' })
          }
          await update($, RECALL, (r) => (r ? { ...r, reviewedAt: Date.now() } : r))
          $.ui.invalidate('ui.render')
        },
      }),
    )
  }
  return Box({ flexDirection: 'row', children })
}
