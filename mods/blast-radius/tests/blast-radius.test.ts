// Tests for the local changes (question-dialog hold, nobody-to-ask deny, hold
// timeout, word splitting, scripts run by path) and the core hold.
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
  logs: string[] // $.ui.log to the transcript
  debug: string[] // $.ui.log to the debug sink
  nonSleep: string[][]
  cwds: string[] // the cwd of each non-sleep process.run, in order
  reads: string[] // paths handed to $.fs.read
  stats: string[] // paths handed to $.fs.stat
  asks: any[]
  ran: number
}

// A file the stubs know: its text, and where it lands when that differs from its path.
type Stubbed = { text: string; realPath?: string; size?: number; kind?: 'file' | 'dir' | 'other'; unreadable?: boolean }

// What `claude -p` does with $.ui.ask: the session has no AskUserQuestion tool.
const NO_DIALOG = () => ({ deny: 'no tool named "AskUserQuestion" in this session' })
// The answer shape $.ui.ask reads: the chosen label keyed by the question text.
const answer = (label: string) => (e: any) => ({ result: { answers: { [e.questions[0].question]: label } } })

// Stubs every call the mod makes. `onSleep` is what the poll sleep does; `ask`
// answers the engine's question dialog (AskUserQuestion), which $.ui.ask raises.
function stubs(
  on: any,
  probe: Probe,
  opts: {
    surfaces?: string[]
    onSleep?: () => Promise<void>
    onOpen?: () => void
    ask?: (e: any) => any
    files?: Record<string, Stubbed> // by the path $.fs.stat is asked about
    dirs?: string[] // folders beside /work that stat as directories
  } = {},
) {
  on('session.cwd', () => ({ value: '/work' }))
  on('session.id', () => ({ value: 'abc' })) // its scratchpad: /tmp/claude-<uid>/<project>/abc/scratchpad
  on('session.surfaces', () => ({ value: opts.surfaces ?? ['terminal'] }))
  on('process.run', async ($: any, e: any) => {
    if (e.argv[0] === 'sleep') {
      probe.sleeps += 1
      if (opts.onSleep) await opts.onSleep()
      return { value: { exitCode: 0, stdout: '', stderr: '' } }
    }
    probe.nonSleep.push(e.argv)
    probe.cwds.push(e.init?.cwd)
    if (e.argv[0] === 'bash' && e.argv[2].includes('pwd -P')) {
      // the cd resolver: every plain folder exists, a relative one under the folder it
      // ran in; one spelt with a command substitution does not resolve
      const d: string = e.argv[4]
      if (d.includes('$(')) return { value: { exitCode: 1, stdout: '', stderr: '' } }
      return { value: { exitCode: 0, stdout: `${d.startsWith('/') ? d : `${e.init?.cwd ?? '/work'}/${d}`}\n`, stderr: '' } }
    }
    return { value: { exitCode: 0, stdout: '', stderr: '' } }
  })
  on('fs.stat', ($: any, e: any) => {
    probe.stats.push(e.path)
    const f = opts.files?.[e.path]
    if (f) {
      return { value: { kind: f.kind ?? 'file', size: f.size ?? f.text.length, mtimeMs: 0, isLink: false, realPath: f.realPath ?? e.path } }
    }
    if (e.path === '/work' || opts.dirs?.includes(e.path)) {
      return { value: { kind: 'dir', size: 0, mtimeMs: 0, isLink: false, realPath: e.path } }
    }
    return { value: { kind: 'other', size: 0, mtimeMs: 0, isLink: false } } // leads nowhere: no realPath
  })
  on('fs.read', ($: any, e: any, next: any) => {
    probe.reads.push(e.path)
    const hit = Object.entries(opts.files ?? {}).find(([path, f]) => (f.realPath ?? path) === e.path)
    if (hit?.[1].unreadable) return next(e) // the bottom hook throws, so the read rejects
    return { value: hit ? hit[1].text : '' }
  })
  on('ui.open', () => {
    probe.opens += 1
    opts.onOpen?.()
    return { value: { isPlaced: true } }
  })
  on('ui.close', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.log', ($: any, e: any) => {
    ;(e.to === 'debug' ? probe.debug : probe.logs).push(e.text)
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
const probe = (): Probe => ({ sleeps: 0, opens: 0, logs: [], debug: [], nonSleep: [], cwds: [], reads: [], stats: [], asks: [], ran: 0 })
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

// ---- Scripts run by path ----

// The shape of the script that ran unheld on 2026-10-08: a header comment that
// mentions rm -rf, a folder built from $HOME, two deletes, and a positional.
const SCRIPT = [
  '#!/bin/bash',
  '# Removes the exported site. A comment saying rm -rf is not a command.',
  'set -u',
  'OUT="$HOME/Workspace/create/export/local-sites"',
  '[ -s "$OUT/site.zip" ] || { echo "missing"; exit 1; }',
  'rm -rf "$HOME/Local Sites/630-staging-launch-rehearsal"',
  'rm -rf "$OUT/tmp" "$1"',
  'echo done',
].join('\n')
const CLEAN = ['#!/bin/bash', '# rm -rf is only mentioned here', 'echo "nothing to see"', ''].join('\n')

// Runs `command` where nobody can be asked, with HOME set, over the stubbed files.
async function script($: any, on: any, command: string, files: Record<string, Stubbed>, extra: { dirs?: string[]; ask?: (e: any) => any; env?: Record<string, string> } = {}) {
  const p = probe()
  mock.clock(on)
  stubs(on, p, { surfaces: [], onSleep: yieldTick, files, dirs: ['/Users/me', ...(extra.dirs ?? [])], ask: extra.ask })
  mock.env(on, { HOME: '/Users/me', ...extra.env })
  await start($, false)
  const out: any = await $.tool.call({ tool: 'Bash', command })
  return { p, out, deny: String(out.deny ?? '') }
}

test('bash x.sh: the script is read and its rm -rf lines hold the command, named by line', async ($, on) => {
  const { p, deny } = await script($, on, 'bash local-delete.sh', { '/work/local-delete.sh': { text: SCRIPT } })
  expect(deny).toContain('did not run')
  expect(deny).toContain('run local-delete.sh, where line 6 (rm -rf) would delete nothing')
  expect(deny).toContain('line 7 (rm -rf) would')
  expect(deny).toContain('$1 is not a plain variable')
  expect(deny.includes('line 2')).toBe(false) // the comment is not a command
  expect(p.reads).toEqual(['/work/local-delete.sh'])
  // $HOME is filled in at the start of a word and of an assignment's value.
  expect(measured(p)).toEqual([['~/Local Sites/630-staging-launch-rehearsal'], ['~/Workspace/create/export/local-sites/tmp']])
  expect(p.ran).toBe(0)
})

test('the question names the script, its risky lines and where it was read from', async ($, on) => {
  const { p } = await script($, on, 'bash local-delete.sh', { '/work/local-delete.sh': { text: SCRIPT } }, { ask: answer('Cancel') })
  expect(p.asks.length).toBe(1)
  expect(p.asks[0].question).toContain('held `bash local-delete.sh`')
  expect(p.asks[0].question).toContain('line 6 (rm -rf)')
  expect(p.asks[0].question).toContain('Read from /work/local-delete.sh (8 lines); risky lines: 6, 7.')
  expect(p.ran).toBe(0)
})

test('a script with no risky line passes, read once, with a debug line and nothing in the transcript', async ($, on) => {
  const { p, out } = await script($, on, 'bash clean.sh', { '/work/clean.sh': { text: CLEAN } })
  expect(out).toEqual({ result: 'ran' })
  expect(p.reads).toEqual(['/work/clean.sh'])
  expect(p.logs).toEqual([])
  expect(p.debug.length).toBe(1)
  expect(p.debug[0]).toContain('nothing risky')
})

// One scenario per test: the kit registers a test's hooks before its first call on $.
for (const command of ['sh local-delete.sh', 'zsh -f -- local-delete.sh', 'source local-delete.sh', '. ./local-delete.sh', '/bin/bash -x local-delete.sh']) {
  test(`\`${command}\` reads the file`, async ($, on) => {
    const { p, deny } = await script($, on, command, { '/work/local-delete.sh': { text: SCRIPT } })
    expect(deny).toContain('line 6 (rm -rf)')
    expect(p.reads.length).toBe(1)
  })
}

for (const command of ['bash -euo pipefail local-delete.sh', 'bash +o posix local-delete.sh', 'sh -o errexit -- local-delete.sh']) {
  test(`\`${command}\`: an option value is not the file`, async ($, on) => {
    const { p, deny } = await script($, on, command, { '/work/local-delete.sh': { text: SCRIPT } })
    expect(deny).toContain('line 6 (rm -rf)')
    expect(p.stats[0]).toBe('/work/local-delete.sh')
  })
}

test('a program run by path outside the folders passes with only a debug line, not a transcript line', async ($, on) => {
  const { p, out } = await script($, on, '/usr/bin/git status', { '/usr/bin/git': { text: 'ELF', realPath: '/usr/bin/git' } })
  expect(out).toEqual({ result: 'ran' })
  expect(p.logs).toEqual([])
  expect(p.debug[0]).toContain('/usr/bin/git is outside')
})

test('a big binary run by path inside the session folder passes with only a debug line', async ($, on) => {
  const { p, out } = await script($, on, './target/release/app --help', { '/work/target/release/app': { text: 'ELF', size: 5 * 1024 * 1024 } })
  expect(out).toEqual({ result: 'ran' })
  expect(p.logs).toEqual([])
  expect(p.debug[0]).toContain('is over the 256 KiB limit')
})

test('a cd that needs a command to expand leaves a relative script unread, and says so', async ($, on) => {
  const { p, out } = await script($, on, 'cd "$(git rev-parse --show-toplevel)" && ./scripts/clean.sh', { '/work/scripts/clean.sh': { text: 'rm -rf dist\n' } })
  expect(out).toEqual({ result: 'ran' })
  expect(p.reads).toEqual([])
  expect(p.logs[0]).toContain('could not be expanded without running a command')
})

test('the same cd does not stop an absolute script path being read; its lines hold, unmeasured', async ($, on) => {
  const { p, deny } = await script($, on, 'cd "$(git rev-parse --show-toplevel)" && bash ~/bin/clean.sh', { '/Users/me/bin/clean.sh': { text: 'rm -rf "$HOME/.cache/x"\n' } })
  expect(p.reads).toEqual(['/Users/me/bin/clean.sh'])
  // where the script runs is unknown, so the hold says so instead of measuring
  expect(deny).toContain('It would have: rm -rf in clean.sh line 1 in $(git rev-parse --show-toplevel)')
  expect(measured(p)).toEqual([])
})

test('a bare path with a shell shebang is read', async ($, on) => {
  const { deny } = await script($, on, './local-delete', { '/work/local-delete': { text: SCRIPT } })
  expect(deny).toContain('line 6 (rm -rf)')
})

test('a bare path with another shebang is not a shell script: it passes with only a debug line', async ($, on) => {
  const { p, out } = await script($, on, './tool', { '/work/tool': { text: '#!/usr/bin/env node\nrequire("fs").rmSync("x", { recursive: true })\n' } })
  expect(out).toEqual({ result: 'ran' })
  expect(p.logs).toEqual([])
  expect(p.debug[0]).toContain('is not a shell')
})

test('~/bin/x is looked up under HOME and its env bash shebang counts', async ($, on) => {
  const { deny } = await script($, on, '~/bin/clean', { '/Users/me/bin/clean': { text: '#!/usr/bin/env bash\nrm -rf "$HOME/.cache"\n' } })
  expect(deny).toContain('run clean, where line 2 (rm -rf)')
})

test('a script outside the session folder, home and the Claude temp folder is not read, and the pass is said', async ($, on) => {
  const { p, out } = await script($, on, 'bash /opt/tool/x.sh', { '/opt/tool/x.sh': { text: SCRIPT } })
  expect(out).toEqual({ result: 'ran' })
  expect(p.reads).toEqual([])
  expect(p.logs.length).toBe(1)
  expect(p.logs[0]).toContain('/opt/tool/x.sh is outside the session folder')
  expect(p.logs[0]).toContain('`bash /opt/tool/x.sh` ran unchecked')
})

test('a link inside the session folder that lands outside is judged by where it lands', async ($, on) => {
  const { p, out } = await script($, on, 'bash x.sh', { '/work/x.sh': { text: SCRIPT, realPath: '/etc/x.sh' } })
  expect(out).toEqual({ result: 'ran' })
  expect(p.reads).toEqual([])
  expect(p.logs[0]).toContain('/etc/x.sh is outside')
})

test("this session's scratchpad is read", async ($, on) => {
  const path = '/tmp/claude-501/-Users-me/abc/scratchpad/local-delete.sh'
  const { p, deny } = await script($, on, `bash ${path}`, { [path]: { text: SCRIPT, realPath: `/private${path}`, } })
  expect(deny).toContain('line 6 (rm -rf)')
  expect(p.reads).toEqual([`/private${path}`])
})

test("another session's scratchpad is not read, and the pass is said", async ($, on) => {
  const path = '/tmp/claude-501/-Users-me/other/scratchpad/local-delete.sh'
  const { p, out } = await script($, on, `bash ${path}`, { [path]: { text: SCRIPT, realPath: `/private${path}` } })
  expect(out).toEqual({ result: 'ran' })
  expect(p.reads).toEqual([])
  expect(p.logs[0]).toContain("outside the session folder, your home folder and this session's scratchpad")
})

test('a script over 256 KiB is not read, and the pass is said with the size', async ($, on) => {
  const { p, out } = await script($, on, 'bash big.sh', { '/work/big.sh': { text: SCRIPT, size: 300 * 1024 } })
  expect(out).toEqual({ result: 'ran' })
  expect(p.reads).toEqual([])
  expect(p.logs[0]).toContain('300 KiB is over the 256 KiB limit')
})

test('a missing script passes with the reason', async ($, on) => {
  const { p, out } = await script($, on, 'bash nope.sh', {})
  expect(out).toEqual({ result: 'ran' })
  expect(p.logs[0]).toContain('no such file')
  expect(p.stats).toEqual(['/work/nope.sh']) // no PATH set: nothing else is looked up
})

// A name with no `/` is looked up as the shell does. Measured 2026-10-08 on macOS
// (bash 3.2, zsh 5.9): `source` and `.` try PATH first, except zsh's `source`,
// which tries the folder first; `bash x.sh` tries the folder, then PATH.
const ON_PATH = { PATH: '/usr/bin:/Users/me/bin' }

test('`. x.sh` reads the copy on PATH, which the shell runs before the one in the folder', async ($, on) => {
  const { p, deny } = await script($, on, '. x.sh', { '/work/x.sh': { text: CLEAN }, '/Users/me/bin/x.sh': { text: 'rm -rf "$HOME/data"\n' } }, { env: ON_PATH })
  expect(deny).toContain('run x.sh, where line 1 (rm -rf)')
  expect(p.reads).toEqual(['/Users/me/bin/x.sh'])
})

test('`source x.sh` also reads the copy in the folder, which zsh runs first', async ($, on) => {
  const { p, deny } = await script($, on, 'source x.sh', { '/work/x.sh': { text: 'rm -rf build\n' }, '/Users/me/bin/x.sh': { text: CLEAN } }, { env: ON_PATH })
  expect(deny).toContain('run x.sh, where line 1 (rm -rf)')
  expect(p.reads).toEqual(['/Users/me/bin/x.sh', '/work/x.sh'])
})

test('`bash x.sh` runs the copy in the folder when there is one, so PATH is not read', async ($, on) => {
  const { p, out } = await script($, on, 'bash x.sh', { '/work/x.sh': { text: CLEAN }, '/Users/me/bin/x.sh': { text: 'rm -rf build\n' } }, { env: ON_PATH })
  expect(out).toEqual({ result: 'ran' })
  expect(p.reads).toEqual(['/work/x.sh'])
})

test('`bash x.sh` with no copy in the folder reads the one on PATH', async ($, on) => {
  const { p, deny } = await script($, on, 'bash x.sh', { '/Users/me/bin/x.sh': { text: 'rm -rf build\n' } }, { env: ON_PATH })
  expect(deny).toContain('run x.sh, where line 1 (rm -rf)')
  expect(p.reads).toEqual(['/Users/me/bin/x.sh'])
})

test('a name found nowhere passes, said to be missing from the folder and PATH', async ($, on) => {
  const { p, out } = await script($, on, 'source nope.sh', {}, { env: ON_PATH })
  expect(out).toEqual({ result: 'ran' })
  expect(p.logs[0]).toContain('no such file in the folder it runs in or on PATH')
})

// Review finding 2026-10-08, not taken: separators inside quotes split the line, as
// upstream's splitter does for an inline rm too. The cut path is said, not hidden.
test('a ; inside a quoted script name cuts the path: the cut name is skipped and said', async ($, on) => {
  const { p, out } = await script($, on, 'bash "cleanup;prod.sh"', {})
  expect(out).toEqual({ result: 'ran' })
  expect(p.logs[0]).toContain('did not read cleanup (no such file')
})

test('a folder run by path passes with the reason', async ($, on) => {
  const { p, out } = await script($, on, './build', { '/work/build': { text: '', kind: 'dir' } })
  expect(out).toEqual({ result: 'ran' })
  expect(p.stats[0]).toBe('/work/build')
  expect(p.logs).toEqual([]) // no shell name: the debug sink, not the transcript
  expect(p.debug[0]).toContain('is not a regular file')
})

test('one level: a script the script runs is not read', async ($, on) => {
  const { p, out } = await script($, on, 'bash outer.sh', {
    '/work/outer.sh': { text: 'bash inner.sh\n./inner.sh\n' },
    '/work/inner.sh': { text: SCRIPT },
  })
  expect(out).toEqual({ result: 'ran' })
  expect(p.reads).toEqual(['/work/outer.sh'])
})

test('every script on the line is read in order, and the first risky one holds', async ($, on) => {
  const { p, deny } = await script($, on, 'bash clean.sh && bash local-delete.sh', {
    '/work/clean.sh': { text: CLEAN },
    '/work/local-delete.sh': { text: SCRIPT },
  })
  expect(deny).toContain('run local-delete.sh, where line 6 (rm -rf)')
  expect(p.reads).toEqual(['/work/clean.sh', '/work/local-delete.sh'])
})

test('a bare path that is not a shell script does not end the check: the next script is read', async ($, on) => {
  const { p, deny } = await script($, on, './node_modules/.bin/tsc && ./cleanup.sh', {
    '/work/node_modules/.bin/tsc': { text: '#!/usr/bin/env node\nconsole.log(1)\n' },
    '/work/cleanup.sh': { text: 'rm -rf dist\n' },
  })
  expect(deny).toContain('run cleanup.sh, where line 1 (rm -rf)')
  expect(p.reads).toEqual(['/work/node_modules/.bin/tsc', '/work/cleanup.sh'])
  expect(p.debug[0]).toContain('is not a shell')
  // it was read, so it is not named as unread
  expect(deny.includes('tsc')).toBe(false)
})

test('a skipped script does not end the check either, and each skip is said', async ($, on) => {
  const { p, deny } = await script($, on, 'bash /opt/x.sh; bash clean.sh; bash local-delete.sh', {
    '/opt/x.sh': { text: SCRIPT },
    '/work/clean.sh': { text: CLEAN },
    '/work/local-delete.sh': { text: SCRIPT },
  })
  expect(deny).toContain('run local-delete.sh, where line 6 (rm -rf)')
  expect(p.logs.length).toBe(1)
  expect(p.logs[0]).toContain('/opt/x.sh is outside')
  expect(p.reads).toEqual(['/work/clean.sh', '/work/local-delete.sh'])
  // the skipped one is named in the hold; the one read and found clean is not
  expect(deny).toContain('; Proceed also runs x.sh, which was not read')
  expect(deny.includes('clean.sh, which')).toBe(false)
})

test('a script skipped before the held one is named with the ones after it', async ($, on) => {
  const { p, deny } = await script($, on, '/opt/tools/wipe && ./build.sh && ./build2.sh', {
    '/opt/tools/wipe': { text: '#!/bin/bash\nrm -rf "$HOME/data"\n' },
    '/work/build.sh': { text: 'rm -rf dist\n' },
    '/work/build2.sh': { text: 'rm -rf out\n' },
  })
  expect(deny).toContain('run build.sh, where line 1 (rm -rf)')
  expect(deny).toContain('; Proceed also runs wipe, build2.sh, which were not read')
  expect(p.reads).toEqual(['/work/build.sh'])
  // a bare path with no shell name: its skip goes to the debug log, and only the hold names it
  expect(p.logs).toEqual([])
  expect(p.debug[0]).toContain('/opt/tools/wipe is outside')
})

test('a risk written on the line itself wins, and the script is not read but is named', async ($, on) => {
  const { p, deny } = await script($, on, 'bash local-delete.sh && rm -rf build', { '/work/local-delete.sh': { text: SCRIPT } })
  expect(deny).toContain('no file matches build; Proceed also runs local-delete.sh, which was not read')
  expect(deny.includes('local-delete.sh, where')).toBe(false)
  expect(p.reads).toEqual([])
})

test('two risky scripts: the hold is on the first, and the second is named as not read', async ($, on) => {
  const { p, deny } = await script($, on, 'bash a.sh && bash b.sh', { '/work/a.sh': { text: 'rm -rf x\n' }, '/work/b.sh': { text: 'rm -rf y\n' } })
  expect(deny).toContain('run a.sh, where line 1 (rm -rf)')
  expect(deny).toContain('; Proceed also runs b.sh, which was not read')
  expect(p.reads).toEqual(['/work/a.sh'])
})

test('a file that stats but cannot be read is skipped with the reason, never denied as a hook failure', async ($, on) => {
  const { p, out } = await script($, on, 'bash locked.sh', { '/work/locked.sh': { text: SCRIPT, unreadable: true } })
  expect(out).toEqual({ result: 'ran' })
  expect(p.logs[0]).toContain('it could not be read')
})

test('bash -c runs a string, not a file: out of scope as before', async ($, on) => {
  const { p, out } = await script($, on, 'bash -c "echo hi"', {})
  expect(out).toEqual({ result: 'ran' })
  expect(p.reads).toEqual([])
  expect(p.logs).toEqual([])
})

test('a script path from a variable set on the line is read', async ($, on) => {
  const { deny } = await script($, on, 'S=/work; bash "$S/local-delete.sh"', { '/work/local-delete.sh': { text: SCRIPT } })
  expect(deny).toContain('line 6 (rm -rf)')
})

test('a script path from an unset variable is not read, and the pass says why', async ($, on) => {
  const { p, out } = await script($, on, 'bash "$SCRATCH/local-delete.sh"', { '/work/local-delete.sh': { text: SCRIPT } })
  expect(out).toEqual({ result: 'ran' })
  expect(p.reads).toEqual([])
  expect(p.logs[0]).toContain('its path was not expanded: $SCRATCH is not set on this line')
})

test('cd on the line moves where the script is looked for; cd in the script moves where a line is measured', async ($, on) => {
  const { p, deny } = await script($, on, 'cd sub && bash x.sh', { '/work/sub/x.sh': { text: 'cd deeper\nrm -rf cache\n' } })
  expect(deny).toContain('run x.sh, where line 2 (rm -rf)')
  expect(measured(p)).toEqual([['cache']])
  // the rm measurement ran in /work/sub/deeper: the script's folder, moved by its own cd
  const i = p.nonSleep.findIndex((a) => a[0] === 'bash' && a[2].includes('compgen'))
  expect(p.cwds[i]).toBe('/work/sub/deeper')
})

test('a local assignment fills in, and more than five risky lines are named but not measured', async ($, on) => {
  const text = ['f() {', '  local D=/work/out', '  rm -rf "$D"', '}', 'rm -rf a', 'rm -rf b', 'rm -rf c', 'rm -rf d', 'rm -rf e', 'rm -rf f'].join('\n')
  const { p, deny } = await script($, on, 'bash many.sh', { '/work/many.sh': { text } })
  expect(measured(p)).toEqual([['/work/out'], ['a'], ['b'], ['c'], ['d']])
  expect(deny).toContain('2 more risky lines (9, 10) are not measured')
})

// ---- Comments ----

test('a comment after rm -rf leaves the rm held', async ($, on) => {
  // Comments are not removed: `#`, `old` and `output` are measured as targets too, as upstream.
  const { p, deny } = await denied($, on, 'rm -rf build # old output')
  expect(deny).toContain('did not run')
  expect(measured(p)[0][0]).toBe('build')
})

test('a # inside a word is part of the path', async ($, on) => {
  const { p } = await denied($, on, 'rm -rf "a#b" c#d')
  expect(measured(p)).toEqual([['a#b', 'c#d']])
})

// Four review rounds traced a comment stripper hiding a real rm -rf; it was removed.
// Each of these held before this change and must still hold.
for (const [name, command, target] of [
  ['an apostrophe in a heredoc body, then a quoted #', "cat <<EOF\nit's fine\nEOF\necho 'a #b'; rm -rf build", 'build'],
  ['a quoted # inside "$(...)" with quotes of its own', 'x="$(git log -1 --format="%h # %s")"; rm -rf build', 'build'],
  ["an ANSI-C quote with \\' before a quoted #", "echo $'it\\'s' 'a #b'; rm -rf build", 'build'],
  ['an escaped backslash at a line end, which is not a continuation', 'echo foo\\\\\nrm -rf x', 'x'],
] as const) {
  test(`still held: ${name}`, async ($, on) => {
    const { p, deny } = await denied($, on, command)
    expect(deny).toContain('did not run')
    expect(measured(p).flat()).toContain(target)
  })
}

test('in a script, a heredoc with an apostrophe and a comment line leave the real rm as the only risky line', async ($, on) => {
  const text = ["cat <<'EOF'", "It's a note", 'EOF', '# rm -rf is only a comment here', 'rm -rf build', ''].join('\n')
  const { p, deny } = await script($, on, 'bash notes.sh', { '/work/notes.sh': { text } })
  expect(deny).toContain('run notes.sh, where line 5 (rm -rf)')
  expect(deny.includes('line 4')).toBe(false)
  expect(measured(p)).toEqual([['build']])
})

// The shell removes a `\`-newline pair, so a word continued with no space is one word.
for (const [name, command, target] of [
  ['bare', 'rm -rf foo\\\nbar', 'foobar'],
  ['in double quotes', 'rm -rf "foo\\\nbar"', 'foobar'],
  ['in single quotes, where the pair is kept', "rm -rf 'foo\\\nbar'", 'foo\\\nbar'],
] as const) {
  test(`a word continued with no space is one path, ${name}`, async ($, on) => {
    const { p, deny } = await denied($, on, command)
    expect(deny).toContain('did not run')
    expect(measured(p)).toEqual([[target]])
  })
}

test('rm glued to its flag by a continuation runs `rm-rf`, which deletes nothing, so it passes', async ($, on) => {
  // bash and zsh both glue `echo\⏎hi` into `echohi: command not found` (2026-10-08).
  const { out } = await script($, on, 'rm\\\n-rf build', {})
  expect(out).toEqual({ result: 'ran' })
})

test('a continued line is one command, and the lines after it keep their numbers', async ($, on) => {
  const text = ['echo one \\', '  two', 'rm -rf \\', '  build', 'rm \\', '  -rf other', ''].join('\n')
  const { p, deny } = await script($, on, 'bash cont.sh', { '/work/cont.sh': { text } })
  expect(deny).toContain('line 3 (rm -rf)')
  expect(deny).toContain('line 5 (rm -rf)')
  expect(measured(p)).toEqual([['build'], ['other']])
})
