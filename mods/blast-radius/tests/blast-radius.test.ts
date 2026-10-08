// Tests for the local changes (question-dialog hold, nobody-to-ask deny, hold
// timeout, word splitting) and the core hold.
// Run: claude plugin test mods/blast-radius
import { expect, mock, test } from 'claude-code/testing'

const PANE = {
  plugin: 'blast-radius',
  component: 'Pane',
  requestId: 'blast-radius',
  surface: 'terminal',
  viewport: { columns: 100, rows: 30 },
  props: {
    title: 'Blast Radius',
    isFocused: true,
    bodyColumns: 80,
    placement: 'inline',
    scroll: { offset: 0, bodyRows: 10 },
    view: {},
  },
} as const

type Probe = {
  sleeps: number
  opens: number
  logs: string[]
  nonSleep: string[][]
  asks: any[]
  ran: number
}

// What `claude -p` does with $.ui.ask: the session has no AskUserQuestion tool.
const NO_DIALOG = () => ({ deny: 'no tool named "AskUserQuestion" in this session' })
// The answer shape $.ui.ask reads: the chosen label keyed by the question text.
const answer = (label: string) => (e: any) => ({ result: { answers: { [e.questions[0].question]: label } } })

// Stubs every call the mod makes. `onSleep` is what the poll sleep does; `ask`
// answers the engine's question dialog (AskUserQuestion), which $.ui.ask raises.
function stubs(
  on: any,
  probe: Probe,
  opts: { surfaces?: string[]; onSleep?: () => Promise<void>; onOpen?: () => void; ask?: (e: any) => any } = {},
) {
  on('session.cwd', () => ({ value: '/work' }))
  on('session.surfaces', () => ({ value: opts.surfaces ?? ['terminal'] }))
  on('process.run', async ($: any, e: any) => {
    if (e.argv[0] === 'sleep') {
      probe.sleeps += 1
      if (opts.onSleep) await opts.onSleep()
      return { value: { exitCode: 0, stdout: '', stderr: '' } }
    }
    probe.nonSleep.push(e.argv)
    return { value: { exitCode: 0, stdout: '', stderr: '' } }
  })
  on('ui.open', () => {
    probe.opens += 1
    opts.onOpen?.()
    return { value: { isPlaced: true } }
  })
  on('ui.close', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.log', ($: any, e: any) => {
    probe.logs.push(e.text)
    return { value: undefined }
  })
  on('tool.call', ($: any, e: any) => {
    if (e.tool === 'AskUserQuestion') {
      probe.asks.push(e.questions[0])
      return (opts.ask ?? NO_DIALOG)(e)
    }
    probe.ran += 1
    return { result: 'ran' }
  })
  on('session.start', () => ({ cwd: '/work' }))
}

// Yields to the event loop so the kit's 5 s limit can fire on a runaway poll loop.
const yieldTick = () => new Promise<void>((r) => setTimeout(r, 1))
const probe = (): Probe => ({ sleeps: 0, opens: 0, logs: [], nonSleep: [], asks: [], ran: 0 })
const start = ($: any, isInteractive: boolean) =>
  $.session.start({ surface: isInteractive ? 'terminal' : null, isInteractive, cwd: '/work' })
const rm = { tool: 'Bash', command: 'rm -rf build' }
// The paths the rm measurement was handed (its bash script lists them after $0).
const measured = (p: Probe) => p.nonSleep.filter((a) => a[0] === 'bash' && a[2].includes('compgen')).map((a) => a.slice(4))

test('a command that is not risky passes straight through', async ($, on) => {
  const p = probe()
  stubs(on, p)
  await start($, true)
  const out = await $.tool.call({ tool: 'Bash', command: 'ls' })
  expect(out).toEqual({ result: 'ran' })
  expect(p.opens).toBe(0)
})

// The hold polls with sleep; the first poll is when a person would press a button.
function pressOnFirstSleep($: any, on: any, key: string) {
  const p = probe()
  const clock = mock.clock(on)
  mock.env(on, {})
  let pressed = false
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['host'] }))
  stubs(on, p, {
    onSleep: async () => {
      if (!pressed) {
        pressed = true
        const ui = await $.ui.mount(PANE)
        await ui.press({ key })
      }
      await clock.advance(250)
    },
  })
  return p
}

test('Proceed runs the held command', async ($, on) => {
  pressOnFirstSleep($, on, 'proceed')
  await start($, true)
  expect(await $.tool.call(rm)).toEqual({ result: 'ran' })
})

test('Cancel refuses the held command', async ($, on) => {
  pressOnFirstSleep($, on, 'cancel')
  await start($, true)
  const out: any = await $.tool.call(rm)
  expect(out.deny).toContain('did not run')
  expect(out.deny).toContain('pressed Cancel')
})

// ---- No surface draws the pane: the engine's question dialog holds the call ----

// The desktop app's Code tab, measured 2026-10-08: an SDK host reports
// isInteractive=false and no surface, yet puts AskUserQuestion to the person.
test('desktop shape (isInteractive=false, no surface): the question dialog holds it, Proceed runs it', async ($, on) => {
  const p = probe()
  mock.clock(on)
  stubs(on, p, { surfaces: [], onSleep: yieldTick, ask: answer('Proceed') })
  mock.env(on, {})
  await start($, false)
  expect(await $.tool.call(rm)).toEqual({ result: 'ran' })
  expect(p.opens).toBe(0)
  expect(p.asks.length).toBe(1)
  expect(p.asks[0].header).toBe('Blast Radius')
  expect(p.asks[0].options.map((o: any) => o.label)).toEqual(['Proceed', 'Cancel'])
  expect(p.asks[0].question).toContain('rm -rf build')
  expect(p.asks[0].question).toContain('delete nothing')
  expect(p.ran).toBe(1)
})

test('interactive with no surface asks in the dialog instead of denying', async ($, on) => {
  const p = probe()
  mock.clock(on)
  stubs(on, p, { surfaces: [], onSleep: yieldTick, ask: answer('Proceed') })
  mock.env(on, {})
  await start($, true)
  expect(await $.tool.call(rm)).toEqual({ result: 'ran' })
  expect(p.asks.length).toBe(1)
})

test('Cancel in the question dialog refuses', async ($, on) => {
  const p = probe()
  mock.clock(on)
  stubs(on, p, { surfaces: [], onSleep: yieldTick, ask: answer('Cancel') })
  mock.env(on, {})
  await start($, false)
  const out: any = await $.tool.call(rm)
  expect(out.deny).toContain('the user chose Cancel')
  expect(p.ran).toBe(0)
})

test('a typed answer in the question dialog refuses and quotes it', async ($, on) => {
  const p = probe()
  mock.clock(on)
  stubs(on, p, { surfaces: [], onSleep: yieldTick, ask: answer('move it to the trash instead') })
  mock.env(on, {})
  await start($, false)
  const out: any = await $.tool.call(rm)
  expect(out.deny).toContain('"move it to the trash instead"')
  expect(out.deny).toContain('act on what they said')
  expect(p.ran).toBe(0)
})

// A person takes longer than one poll to dismiss a dialog; a session with no one
// to ask refuses at once. The first is a cancel, not "nobody could be asked".
test('dismissing the question after a wait refuses as dismissed, not as unattended', async ($, on) => {
  const p = probe()
  mock.clock(on)
  let release = () => {}
  const dismissed = new Promise<void>((r) => (release = r))
  stubs(on, p, {
    surfaces: [],
    onSleep: async () => {
      if (p.sleeps === 2) release()
      await yieldTick()
    },
    ask: async () => {
      await dismissed
      return { deny: 'The user dismissed the question.' }
    },
  })
  mock.env(on, {})
  await start($, false)
  const out: any = await $.tool.call(rm)
  expect(out.deny).toContain('dismissed the Blast Radius question')
  expect(out.deny.includes('no one could be asked')).toBe(false)
  expect(p.ran).toBe(0)
})

test('a question nobody answers is refused at the hold limit and said to be still open', async ($, on) => {
  const p = probe()
  const clock = mock.clock(on)
  let release = () => {}
  const later = new Promise<void>((r) => (release = r))
  stubs(on, p, {
    surfaces: [],
    onSleep: () => clock.advance(100_000),
    ask: async (e: any) => {
      await later
      return answer('Proceed')(e)
    },
  })
  mock.env(on, {})
  await start($, false)
  const out: any = await $.tool.call(rm)
  expect(out.deny).toContain('nobody answered the question')
  expect(out.deny).toContain('may still be open')
  expect(out.deny).toContain('600 s')
  release()
  await yieldTick()
  expect(p.ran).toBe(0)
})

// ---- Nobody can be asked (claude -p): deny at once, naming the signals ----

test('nobody to ask (claude -p): denies within one poll, opens no pane, names the signals', async ($, on) => {
  const p = probe()
  mock.clock(on)
  stubs(on, p, { surfaces: [], onSleep: yieldTick })
  mock.env(on, {})
  await start($, false)
  const out: any = await $.tool.call(rm)
  expect(out.deny).toContain('did not run')
  expect(out.deny).toContain('no one could be asked')
  expect(out.deny).toContain('isInteractive=false')
  expect(out.deny).toContain('drawing surfaces: none')
  expect(out.deny).toContain('no tool named')
  expect(out.deny).toContain("Claude Code's own environment")
  expect(p.opens).toBe(0)
  expect(p.sleeps <= 1).toBe(true)
  expect(p.nonSleep.length > 0).toBe(true)
  expect(p.ran).toBe(0)
})

test('nobody to ask with BLAST_RADIUS_HEADLESS=allow passes through and logs once', async ($, on) => {
  const p = probe()
  mock.clock(on)
  stubs(on, p, { surfaces: [], onSleep: yieldTick })
  mock.env(on, { BLAST_RADIUS_HEADLESS: 'allow' })
  await start($, false)
  const out = await $.tool.call(rm)
  expect(out).toEqual({ result: 'ran' })
  expect(p.logs.length).toBe(1)
  expect(p.opens).toBe(0)
})

// The opt-in is for runs with no one to ask. It must not skip the question in
// the desktop app, where someone can answer it.
test('BLAST_RADIUS_HEADLESS=allow still asks when someone can answer', async ($, on) => {
  const p = probe()
  mock.clock(on)
  stubs(on, p, { surfaces: [], onSleep: yieldTick, ask: answer('Cancel') })
  mock.env(on, { BLAST_RADIUS_HEADLESS: 'allow' })
  await start($, false)
  const out: any = await $.tool.call(rm)
  expect(out.deny).toContain('the user chose Cancel')
  expect(p.logs.length).toBe(0)
  expect(p.ran).toBe(0)
})

// A question refused at once may be a person's quick Esc or a host that refuses
// questions with someone present. Only "no question dialog at all" is unattended.
test('BLAST_RADIUS_HEADLESS=allow never runs a command whose question was refused at once', async ($, on) => {
  const p = probe()
  mock.clock(on)
  stubs(on, p, { surfaces: [], onSleep: yieldTick, ask: () => ({ deny: 'The user dismissed the question.' }) })
  mock.env(on, { BLAST_RADIUS_HEADLESS: 'allow' })
  await start($, false)
  const out: any = await $.tool.call(rm)
  expect(out.deny).toContain('refused at once')
  expect(out.deny).toContain('isInteractive=false')
  expect(p.logs.length).toBe(0)
  expect(p.ran).toBe(0)
})

// ---- Hold limit ----

test('timeout: nobody answers within 600 s', async ($, on) => {
  const p = probe()
  const clock = mock.clock(on)
  stubs(on, p, { onSleep: () => clock.advance(100_000) })
  mock.env(on, {})
  await start($, true)
  const out: any = await $.tool.call(rm)
  expect(out.deny).toContain('nobody answered')
  expect(out.deny).toContain('600 s')
  expect(p.opens).toBe(1)
})

test('BLAST_RADIUS_HOLD_SECONDS=5 denies after 5 s', async ($, on) => {
  const p = probe()
  const clock = mock.clock(on)
  stubs(on, p, { onSleep: () => clock.advance(2_000) })
  mock.env(on, { BLAST_RADIUS_HOLD_SECONDS: '5' })
  await start($, true)
  const out: any = await $.tool.call(rm)
  expect(out.deny).toContain('nobody answered')
  expect(out.deny).toContain('5 s')
  // 2 s per sleep: the limit is passed on the third check, not the first
  expect(p.sleeps).toBe(3)
})

// ---- Word splitting and variables in rm targets ----

// Runs `command` where nobody can be asked, so the deny carries the summary.
async function denied($: any, on: any, command: string) {
  const p = probe()
  mock.clock(on)
  stubs(on, p, { surfaces: [], onSleep: yieldTick })
  mock.env(on, {})
  await start($, false)
  const out: any = await $.tool.call({ tool: 'Bash', command })
  return { p, deny: String(out.deny) }
}

test('a quoted variable joined to a glob is one target, expanded from the line', async ($, on) => {
  const { p } = await denied($, on, 'D=/work/out; rm -rf "$D"/*')
  // Upstream measured "$D" and "/*", the filesystem root, as two targets.
  expect(measured(p)).toEqual([['/work/out/*']])
})

test('an unset variable is not measured and is not reported as "delete nothing"', async ($, on) => {
  const { p, deny } = await denied($, on, 'rm -rf "$DIR"/*')
  expect(measured(p)).toEqual([])
  expect(deny).toContain('not measured')
  expect(deny).toContain('$DIR is not set on this line')
  expect(deny.includes('delete nothing')).toBe(false)
})

test('a variable set by a command is not run, not measured, and said so', async ($, on) => {
  const { p, deny } = await denied($, on, 'X=$(getconf DARWIN_USER_TEMP_DIR); rm -rf "$X"/code_sign_clone.*')
  expect(measured(p)).toEqual([])
  expect(deny).toContain('$X is set on this line from a command')
  expect(deny.includes('delete nothing')).toBe(false)
  expect(p.nonSleep.some((a) => a.includes('getconf'))).toBe(false)
})

// The shell splits an unquoted value with spaces into several paths; measuring it
// as one path that doesn't exist would report "delete nothing".
test('an unquoted variable holding spaces is not measured as one path', async ($, on) => {
  const { p, deny } = await denied($, on, 'X="a b"; rm -rf $X')
  expect(measured(p)).toEqual([])
  expect(deny).toContain('holds spaces')
})

test('a command substitution stays one unexpanded word and its argument is not measured', async ($, on) => {
  const { p, deny } = await denied($, on, 'rm -rf $(cat list) keep')
  expect(measured(p)).toEqual([['keep']])
  expect(deny).toContain('runs a command')
})

test('an assignment inside a subshell or a pipe does not outlive it', async ($, on) => {
  const sub = await denied($, on, '(X=/tmp/a); rm -rf $X')
  expect(measured(sub.p)).toEqual([])
})

test('an assignment that feeds a pipe is not used to expand', async ($, on) => {
  const piped = await denied($, on, 'X=/tmp/a | cat; rm -rf $X')
  expect(measured(piped.p)).toEqual([])
})

test('a single-quoted $ is a literal path and is measured as written', async ($, on) => {
  const { p } = await denied($, on, "X=/tmp/b; rm -rf '$X'")
  expect(measured(p)).toEqual([['$X']])
})

test('$HOME and a quoted folder with a space each stay one target', async ($, on) => {
  const { p } = await denied($, on, 'rm -rf "$HOME/.cache/x" "My Dir"/sub')
  expect(measured(p)).toEqual([['~/.cache/x', 'My Dir/sub']])
})

test('a measured target and an unexpanded one: the summary names both', async ($, on) => {
  const { p, deny } = await denied($, on, 'rm -rf build "$TMPDIR"/cache')
  expect(measured(p)).toEqual([['build']])
  expect(deny).toContain('$TMPDIR/cache matches, not measured')
})
