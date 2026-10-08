// Tests for the local changes (headless deny, hold timeout) and the core hold.
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
}

// Stubs every call the mod makes. `onSleep` is what the poll sleep does.
function stubs(
  on: any,
  probe: Probe,
  opts: { surfaces?: string[]; onSleep?: () => Promise<void>; onOpen?: () => void } = {},
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
  on('tool.call', () => ({ result: 'ran' }))
  on('session.start', () => ({ cwd: '/work' }))
}

// Yields to the event loop so the kit's 5 s limit can fire on a runaway poll loop.
const yieldTick = () => new Promise<void>((r) => setTimeout(r, 1))
const probe = (): Probe => ({ sleeps: 0, opens: 0, logs: [], nonSleep: [] })
const start = ($: any, isInteractive: boolean) =>
  $.session.start({ surface: isInteractive ? 'terminal' : null, isInteractive, cwd: '/work' })
const rm = { tool: 'Bash', command: 'rm -rf build' }

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

// The sleep does not move the clock: an unattended session where no one ever answers.
// Unpatched, this loops until the 5 s test limit.
test('headless: denies at once and never opens a pane or sleeps', async ($, on) => {
  const p = probe()
  mock.clock(on)
  stubs(on, p, { surfaces: [], onSleep: yieldTick })
  mock.env(on, {})
  await start($, false)
  const out: any = await $.tool.call(rm)
  expect(out.deny).toContain('unattended')
  expect(out.deny).toContain('did not run')
  expect(p.opens).toBe(0)
  expect(p.sleeps).toBe(0)
  expect(p.nonSleep.length > 0).toBe(true)
})

test('headless: no surface drawing also denies, even if session.start said interactive', async ($, on) => {
  const p = probe()
  mock.clock(on)
  stubs(on, p, { surfaces: [], onSleep: yieldTick })
  mock.env(on, {})
  await start($, true)
  const out: any = await $.tool.call(rm)
  expect(out.deny).toContain('unattended')
  expect(p.opens).toBe(0)
})

test('headless with BLAST_RADIUS_HEADLESS=allow passes through and logs once', async ($, on) => {
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
