import { expect, test } from 'claude-code/testing'

const MATCH = '{"matched":"why-did-you","phrase":"why did you build"}'
const NONE = '{"matched":null,"phrase":null}'
const CORRECTION_PROMPT = 'why did you build a new script, we already have a tool for it'

const band = (surface: 'terminal' | 'desktop', extra: Record<string, unknown> = {}) =>
  ({
    plugin: 'correction-band',
    component: 'AbovePrompt',
    requestId: 'correction-band',
    surface,
    viewport: { columns: 100, rows: 30 },
    props: { hasSurvey: false, isWorking: false, maxRows: 4, bodyColumns: 80, scroll: { offset: 0, bodyRows: 4 }, view: {}, ...extra },
  }) as const

const iso = (msAgo: number) => new Date(Date.now() - msAgo).toISOString().replace(/\.\d+Z$/, 'Z')

// Registers the stubs every test needs; returns counters and recorded calls.
function stubs(on: any, opts: { run?: () => unknown; heartbeat?: string | null; noCommand?: boolean; off?: boolean } = {}) {
  const seen = { runs: 0, argv: [] as string[], stdin: '', submitted: [] as string[], filled: [] as string[], commands: [] as unknown[], passed: [] as string[] }
  on('env.get', ($: any, e: any) => ({
    value: e.name === 'HOME' ? '/home/t' : e.name === 'CORRECTION_BAND_OFF' && opts.off ? '1' : undefined,
  }))
  on('process.run', ($: any, e: any) => {
    seen.runs++
    seen.argv = [...e.argv]
    seen.stdin = e.init?.stdin ?? ''
    return opts.run ? opts.run() : { value: { exitCode: 0, stdout: NONE, stderr: '' } }
  })
  on('fs.read', () =>
    opts.heartbeat == null ? { deny: 'no such file' } : { value: opts.heartbeat },
  )
  on('prompt.submit', ($: any, e: any) => {
    seen.submitted.push(e.text)
    return { text: e.text }
  })
  // With no stub the call rejects ("no implementation"), standing in for an unknown command.
  if (!opts.noCommand) {
    on('command.run', ($: any, e: any) => {
      seen.commands.push({ command: e.command, args: e.args })
      return { text: 'reviewing' }
    })
  }
  on('prompt.fill', ($: any, e: any) => {
    seen.filled.push(e.text)
    return { isFilled: true }
  })
  on('session.start', () => ({ cwd: '/work' }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['drawn by Claude Code'] }))
  return seen
}

const start = ($: any) => $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
const submit = ($: any, text: string) => $.prompt.submit({ text })

test('a correction-shaped prompt passes unchanged and draws the row with both buttons, in both apps', async ($, on) => {
  const seen = stubs(on, { run: () => ({ value: { exitCode: 0, stdout: MATCH, stderr: '' } }) })
  await start($)
  const out = await submit($, CORRECTION_PROMPT)
  expect(out.text).toBe(CORRECTION_PROMPT)
  expect(seen.argv).toEqual(['python3', '/home/t/.claude/hooks/correction-nudge.py', '--match'])
  expect(JSON.parse(seen.stdin)).toEqual({ prompt: CORRECTION_PROMPT })
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount(band(surface))
    expect(await ui.find({ type: 'Text', text: /Correction\?/ })).toBeDefined()
    expect(await ui.find({ key: 'log' })).toBeDefined()
    expect(await ui.find({ key: 'dismiss' })).toBeDefined()
    await ui.unmount()
  }
})

test('a neutral prompt draws no band', async ($, on) => {
  stubs(on)
  await start($)
  await submit($, 'run the tests please')
  const ui = await $.ui.mount(band('terminal'))
  expect(await ui.find({ text: /Correction\?/ })).toBeUndefined()
  expect(await ui.find({ text: 'drawn by Claude Code' })).toBeDefined()
  await ui.unmount()
})

test('a slash command never reaches the matcher', async ($, on) => {
  const seen = stubs(on)
  await start($)
  await submit($, '/why did you build this')
  expect(seen.runs).toBe(0)
})

test('a local-command-stdout prefix never reaches the matcher', async ($, on) => {
  const seen = stubs(on)
  await start($)
  await submit($, '<local-command-stdout>why did you build this</local-command-stdout>')
  expect(seen.runs).toBe(0)
})

test('a 1300 character prompt never reaches the matcher', async ($, on) => {
  const seen = stubs(on)
  await start($)
  const long = 'why did you build this '.repeat(60).slice(0, 1300)
  const out = await submit($, long)
  expect(seen.runs).toBe(0)
  expect(out.text).toBe(long)
})

test('a failing matcher process fails open', async ($, on) => {
  const seen = stubs(on, { run: () => ({ deny: 'boom' }) })
  await start($)
  const out = await submit($, CORRECTION_PROMPT)
  expect(out.text).toBe(CORRECTION_PROMPT)
  expect(seen.runs).toBe(1)
  const ui = await $.ui.mount(band('terminal'))
  expect(await ui.find({ text: /Correction\?/ })).toBeUndefined()
  await ui.unmount()
})

test('unparsable matcher output and a non-zero exit draw nothing', async ($, on) => {
  stubs(on, { run: () => ({ value: { exitCode: 1, stdout: MATCH, stderr: 'x' } }) })
  await start($)
  await submit($, CORRECTION_PROMPT)
  const ui = await $.ui.mount(band('terminal'))
  expect(await ui.find({ text: /Correction\?/ })).toBeUndefined()
  await ui.unmount()
})

test('Log it submits the correction-log instruction and clears the band', async ($, on) => {
  const seen = stubs(on, { run: () => ({ value: { exitCode: 0, stdout: MATCH, stderr: '' } }) })
  await start($)
  await submit($, CORRECTION_PROMPT)
  seen.submitted.length = 0
  const ui = await $.ui.mount(band('terminal'))
  await ui.press({ key: 'log' })
  expect(seen.submitted.length).toBe(1)
  expect(seen.submitted[0]).toContain('correction-log')
  expect(await ui.find({ text: /Correction\?/ })).toBeUndefined()
  await ui.unmount()
})

test('Not a correction clears the band and submits nothing', async ($, on) => {
  const seen = stubs(on, { run: () => ({ value: { exitCode: 0, stdout: MATCH, stderr: '' } }) })
  await start($)
  await submit($, CORRECTION_PROMPT)
  seen.submitted.length = 0
  const ui = await $.ui.mount(band('terminal'))
  await ui.press({ key: 'dismiss' })
  expect(seen.submitted.length).toBe(0)
  expect(await ui.find({ text: /Correction\?/ })).toBeUndefined()
  await ui.unmount()
})

test('a fresh heartbeat shows the count; Review runs /recall review through command.run', async ($, on) => {
  const seen = stubs(on, { heartbeat: JSON.stringify({ last_success: iso(2 * 60 * 1000), candidates: 33 }) })
  await start($)
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount(band(surface))
    expect(await ui.find({ type: 'Text', text: /33 memory candidates/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /stale/ })).toBeUndefined()
    await ui.unmount()
  }
  const ui = await $.ui.mount(band('terminal'))
  await ui.press({ key: 'review' })
  expect(seen.commands).toEqual([{ command: 'recall', args: 'review' }])
  expect(seen.submitted.length).toBe(0)
  expect(await ui.find({ text: /memory candidate/ })).toBeUndefined()
  await ui.unmount()
})

test('Review falls back to filling the prompt box when command.run rejects', async ($, on) => {
  const seen = stubs(on, {
    heartbeat: JSON.stringify({ last_success: iso(60 * 1000), candidates: 1 }),
    noCommand: true,
  })
  await start($)
  const ui = await $.ui.mount(band('terminal'))
  expect(await ui.find({ type: 'Text', text: /1 memory candidate pending/ })).toBeDefined()
  await ui.press({ key: 'review' })
  expect(seen.filled).toEqual(['/recall review'])
  expect(seen.submitted.length).toBe(0)
  await ui.unmount()
})

test('a 10 hour old heartbeat shows stale and still shows the count', async ($, on) => {
  stubs(on, { heartbeat: JSON.stringify({ last_success: iso(10 * 3600 * 1000), candidates: 5 }) })
  await start($)
  const ui = await $.ui.mount(band('terminal'))
  expect(await ui.find({ type: 'Text', text: /recall sync stale \(10h ago\)/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /5 memory candidates/ })).toBeDefined()
  await ui.unmount()
})

test('a stale heartbeat with no candidates shows the stale line and no Review button', async ($, on) => {
  stubs(on, { heartbeat: JSON.stringify({ last_success: iso(10 * 3600 * 1000), candidates: 0 }) })
  await start($)
  const ui = await $.ui.mount(band('terminal'))
  expect(await ui.find({ type: 'Text', text: /stale/ })).toBeDefined()
  expect(await ui.find({ key: 'review' })).toBeUndefined()
  await ui.unmount()
})

test('an unreadable heartbeat draws no band and does not throw', async ($, on) => {
  stubs(on, { heartbeat: null })
  await start($)
  const ui = await $.ui.mount(band('terminal'))
  expect(await ui.find({ text: 'drawn by Claude Code' })).toBeDefined()
  await ui.unmount()
})

test('a fresh heartbeat with zero candidates draws no band', async ($, on) => {
  stubs(on, { heartbeat: JSON.stringify({ last_success: iso(1000), candidates: 0 }) })
  await start($)
  const ui = await $.ui.mount(band('terminal'))
  expect(await ui.find({ text: 'drawn by Claude Code' })).toBeDefined()
  await ui.unmount()
})

test('both rows draw together, and maxRows 1 keeps only the correction row', async ($, on) => {
  stubs(on, {
    run: () => ({ value: { exitCode: 0, stdout: MATCH, stderr: '' } }),
    heartbeat: JSON.stringify({ last_success: iso(1000), candidates: 4 }),
  })
  await start($)
  await submit($, CORRECTION_PROMPT)
  const two = await $.ui.mount(band('terminal'))
  expect(await two.find({ text: /Correction\?/ })).toBeDefined()
  expect(await two.find({ text: /4 memory candidates/ })).toBeDefined()
  await two.unmount()
  const one = await $.ui.mount(band('terminal', { maxRows: 1 }))
  expect(await one.find({ text: /Correction\?/ })).toBeDefined()
  expect(await one.find({ text: /memory candidates/ })).toBeUndefined()
  await one.unmount()
})

test('a long phrase is cut with an ellipsis to fit a narrow band', async ($, on) => {
  const phrase = 'why did you build a new script when we already have a tool for exactly this job'
  stubs(on, { run: () => ({ value: { exitCode: 0, stdout: JSON.stringify({ matched: 'x', phrase }), stderr: '' } }) })
  await start($)
  await submit($, CORRECTION_PROMPT)
  const ui = await $.ui.mount(band('terminal', { bodyColumns: 60 }))
  expect(await ui.find({ type: 'Text', text: /Correction\? ".*…"/ })).toBeDefined()
  await ui.unmount()
})

test('CORRECTION_BAND_OFF=1 skips the matcher', async ($, on) => {
  const seen = stubs(on, { off: true })
  await start($)
  const out = await submit($, CORRECTION_PROMPT)
  expect(out.text).toBe(CORRECTION_PROMPT)
  expect(seen.runs).toBe(0)
})

test('a matching prompt followed by a neutral prompt leaves no band', async ($, on) => {
  let n = 0
  const seen = stubs(on, {
    run: () => ({ value: { exitCode: 0, stdout: n++ === 0 ? MATCH : NONE, stderr: '' } }),
  })
  await start($)
  await submit($, CORRECTION_PROMPT)
  await submit($, 'run the tests please')
  expect(seen.runs).toBe(2)
  const ui = await $.ui.mount(band('terminal'))
  expect(await ui.find({ text: /Correction\?/ })).toBeUndefined()
  await ui.unmount()
})

test('Log it submits a text containing the stored correction text', async ($, on) => {
  const seen = stubs(on, { run: () => ({ value: { exitCode: 0, stdout: MATCH, stderr: '' } }) })
  await start($)
  await submit($, CORRECTION_PROMPT)
  seen.submitted.length = 0
  const ui = await $.ui.mount(band('terminal'))
  await ui.press({ key: 'log' })
  expect(seen.submitted[0]).toContain(`"${CORRECTION_PROMPT}"`)
  await ui.unmount()
})
