// desktop-probe: on a Bash command containing BR_DESKTOP_PROBE, records what this
// host gives a mod, then refuses the command with the findings as its reason.
let started = null

export function register(on) {
  on('session.start', async ($, e, next) => {
    started = { isInteractive: e.isInteractive, surface: e.surface, surfacesAtStart: await $.session.surfaces() }
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: 'desktop-probe' }, ($, e, next) => {
    const { Box, Text } = $.ui.resolve(e)
    return Box({ flexDirection: 'column', children: [
      Text({ bold: true, children: 'Desktop probe pane' }),
      Text({ children: `Drawn on surface: ${e.surface}. If you can see this, answer "Yes, I see it".` }),
    ] })
  })

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    if (!String(e.command ?? '').includes('BR_DESKTOP_PROBE')) return next(e)
    const found = { started, surfacesNow: await $.session.surfaces() }
    try {
      found.open = await $.ui.open({ id: 'desktop-probe', title: 'Desktop probe', focus: true, rows: 6 })
    } catch (err) {
      found.open = `threw: ${String(err?.message ?? err).slice(0, 200)}`
    }
    $.ui.invalidate('ui.render')
    const box = { answer: null, error: null }
    $.ui.ask('Blast Radius desktop probe: is a pane titled "Desktop probe" showing in this session right now?', {
      header: 'Probe', options: ['Yes, I see it', 'No pane'],
    }).then((a) => { box.answer = String(a) }, (err) => { box.error = String(err?.message ?? err).slice(0, 200) })
    const t0 = await $.clock.now()
    let polls = 0
    while (box.answer === null && box.error === null && (await $.clock.now()) - t0 < 180000) {
      if (next.signal.aborted) { box.error = 'interrupted'; break }
      await $.process.run(['sleep', '0.5'], { timeoutMs: 5000 })
      polls += 1
    }
    found.ask = { ...box, polls, waitedMs: (await $.clock.now()) - t0 }
    found.surfacesAfter = await $.session.surfaces()
    try { await $.ui.close({ id: 'desktop-probe' }) } catch {}
    return { deny: `DESKTOP_PROBE ${JSON.stringify(found)}` }
  })
}
