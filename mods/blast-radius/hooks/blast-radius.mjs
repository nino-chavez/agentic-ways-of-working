// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0
//
// Blast Radius: holds a risky Bash command and shows what it would change.
//
// tool.call (Bash): if the command is risky, work out its blast radius, open a
// pane with Proceed and Cancel, and hold the call until one is pressed.
// ui.render (Pane): draws the report. If the surface won't place the pane (a
// narrow terminal), the same report is drawn in the AbovePrompt band instead.
//
// Holding: a hook has 10 s of its own time, but time spent inside a `$` call is
// free. So the hold loop waits on a short `$.process.run(["sleep", ...])` until
// a button's onPress sets the decision.
//
// The host reads `on(...)` and `$.noun.method(...)` from source, so they are
// spelled literally, and helpers that take `$` are top-level functions.

const PANE_ID = "blast-radius";
const POLL_SECONDS = "0.25";
const HOLD_SECONDS_DEFAULT = 600;
const HOLD_SECONDS_MIN = 5;
const LIST_MAX = 10;
// Local change: the engine's question dialog, the hold where no surface draws the pane.
const PROCEED = "Proceed";
const CANCEL = "Cancel";
const QUESTION_LIST_MAX = 3;

// The call being held, or null. One at a time: Bash calls in a turn run in order.
let held = null;

// Local change: whether a person is at the prompt, from session.start. null until
// that event is seen (a mod loaded mid-session). Reported in a deny, not used to
// decide: the desktop app's Code tab reports false with a person present.
let sessionInteractive = null;

export function register(on) {
  // Local change: record whether a person is present, for the hold below.
  on("session.start", async ($, e, next) => {
    sessionInteractive = e.isInteractive === true;
    return next(e);
  });

  // Local change: `.catch` refuses the command when the hook itself fails (a
  // throw outside the hold's own try, or an overrun budget). Upstream failed open.
  on("tool.call", { tool: "Bash" }, async ($, e, next) => {
    const risk = classify(String(e.command ?? ""));
    if (risk === null) {
      return next(e);
    }
    // One hold at a time. If another risky call is already held (a subagent's,
    // say), wait until it is answered. `held` is claimed with no await between
    // the check and the claim, so two waiting calls can't both get through.
    while (held !== null) {
      if (next.signal.aborted) {
        return { deny: "Blast Radius held this command and did not run it: the turn was interrupted. Do not retry it unless the user asks you to." };
      }
      await $.process.run(["sleep", POLL_SECONDS], { timeoutMs: 5000 });
    }
    const mine = { command: String(e.command), risk, report: null, decision: null, where: "pane", polls: 0, answer: null, askError: null, askRejectedAtPoll: null };
    held = mine;

    let paneOpened = false;
    let decision;
    let holdSeconds = HOLD_SECONDS_DEFAULT;
    let summary = risk.label;
    let surfaces = [];
    try {
      // Measure where the command will run: the session folder, moved by any
      // `cd dir &&` or `git -C dir` earlier in the same command line.
      const sessionCwd = await $.session.cwd();
      const cwd = risk.dir ? await resolveDir($, sessionCwd, risk.dir) : sessionCwd;
      mine.report = cwd === null
        ? { summary: `${risk.label} in ${risk.dir}`, lines: [], note: `Couldn't find the folder ${risk.dir}, so I couldn't measure what this would change.` }
        : await measure($, risk, cwd);
      summary = mine.report.summary;

      // Local change: the limit is BLAST_RADIUS_HOLD_SECONDS (default 600, minimum 5);
      // upstream hard-coded 10 minutes.
      const asked = Number(await $.env.get("BLAST_RADIUS_HOLD_SECONDS"));
      if (Number.isFinite(asked) && asked > 0) {
        holdSeconds = Math.max(HOLD_SECONDS_MIN, asked);
      }

      // Local change: where the person answers. A surface that draws gets the pane,
      // as upstream. With none, the engine's own question dialog holds the call.
      // Measured 2026-10-08 in a stream-json SDK host shaped like the desktop app's
      // Code tab (--permission-prompt-tool stdio): isInteractive was false and
      // surfaces() empty, yet $.ui.ask reached the host as a can_use_tool request
      // for AskUserQuestion. Under `claude -p` the ask rejects at once ("no tool
      // named AskUserQuestion"), which is what marks a session with no one to ask.
      // In the real desktop (a probe mod, same day) surfaces() listed `desktop` at
      // load, was empty when a Bash call arrived, and listed `desktop` again after;
      // the desktop drew a mod pane the person saw and answered the question.
      surfaces = await $.session.surfaces();
      if (surfaces.length > 0) {
        const opened = await $.ui.open({ id: PANE_ID, title: "Blast Radius", focus: true, rows: paneRows(mine.report) });
        paneOpened = true;
        if (opened.isPlaced) {
          mine.where = "pane";
        } else if (surfaces.includes("terminal")) {
          mine.where = "band"; // a narrow terminal: the report is drawn above the prompt
        } else {
          mine.where = "ask"; // a remote surface that places no panes
        }
      } else {
        mine.where = "ask";
      }
      if (mine.where === "ask") {
        // Not awaited: the poll loop below keeps the interrupt and the hold limit.
        $.ui.ask(question(mine), { header: "Blast Radius", options: [PROCEED, CANCEL] }).then(
          (answer) => {
            if (mine.decision === null) {
              mine.answer = String(answer);
              mine.decision = mine.answer === PROCEED ? "proceed" : "answered";
            }
          },
          (error) => {
            if (mine.decision === null) {
              mine.askError = String(error?.message ?? error).slice(0, 200);
              mine.askRejectedAtPoll = mine.polls;
              mine.decision = "ask-rejected";
            }
          },
        );
      }
      $.ui.invalidate("ui.render");

      const startedAt = await $.clock.now();
      while (mine.decision === null) {
        if (next.signal.aborted) {
          mine.decision = "interrupted";
          break;
        }
        if ((await $.clock.now()) - startedAt > holdSeconds * 1000) {
          mine.decision = "timeout";
          break;
        }
        await $.process.run(["sleep", POLL_SECONDS], { timeoutMs: 5000 });
        mine.polls += 1;
      }
    } catch {
      mine.decision = "error"; // anything unexpected refuses the command
    } finally {
      decision = mine.decision;
      // Close this call's pane before releasing the hold, so the next call's
      // pane can't be the one that gets closed. Local change: an opened pane that
      // waits unplaced is closed too, so it is not seated later with nothing held.
      try {
        if (paneOpened) {
          await $.ui.close({ id: PANE_ID });
        }
      } catch {
        // the pane is already gone
      }
      if (held === mine) {
        held = null;
      }
      $.ui.invalidate("ui.render");
    }

    if (decision === "proceed") {
      $.ui.toast("Blast Radius: running it");
      return next(e);
    }
    // Local change: what a rejected question means. Only a session with no question
    // dialog at all (`claude -p`: "no tool named AskUserQuestion") is one nobody can
    // be asked in, and only that one honours BLAST_RADIUS_HEADLESS=allow. A rejection
    // before the first poll ended is a dismissal straight away, or a host answering
    // without a person (one that refuses questions, or approves every request with no
    // answer); a person may be there, so it is refused, never run. Later, it was dismissed.
    if (decision === "ask-rejected") {
      if (/no tool named/i.test(mine.askError ?? "")) {
        decision = "nobody";
      } else {
        decision = mine.askRejectedAtPoll === 0 ? "refused" : "dismissed";
      }
    }
    // Local change: the signals that decided, so Claude can report them accurately.
    const signals = `isInteractive=${sessionInteractive ?? "not seen"}, drawing surfaces: ${surfaces.length > 0 ? surfaces.join(", ") : "none"}, question rejected at once: ${mine.askError ?? "no reason given"}`;
    if (decision === "nobody") {
      if ((await $.env.get("BLAST_RADIUS_HEADLESS")) === "allow") {
        $.ui.log(`Blast Radius: no one could be asked, BLAST_RADIUS_HEADLESS=allow, running: ${summary}`);
        return next(e);
      }
      return {
        deny: `Blast Radius held this command and did not run it: no one could be asked to approve it (${signals}). It would have: ${summary}. Do not retry it. Ask the user to run it themselves, or to start Claude Code with BLAST_RADIUS_HEADLESS=allow in Claude Code's own environment when an unattended run may delete; the mod reads it from that environment, so a VAR=value prefix on the Bash command does not reach it.`,
      };
    }
    if (decision === "refused") {
      return {
        deny: `Blast Radius held this command and did not run it: its question was refused at once (${signals}). Either the user dismissed it straight away, or this host answers Claude Code's questions without a person. It would have: ${summary}. Do not retry it unless the user asks you to.`,
      };
    }
    if (decision === "timeout") {
      const where = mine.where === "ask" ? "the question; it may still be open, and answering it now does nothing" : "the pane";
      return {
        deny: `Blast Radius held this command for ${holdSeconds} s and nobody answered ${where}. It did not run. It would have: ${summary}. Do not retry it unless the user asks you to.`,
      };
    }
    if (decision === "answered") {
      const said = mine.answer === CANCEL ? "the user chose Cancel" : `the user answered "${mine.answer.slice(0, 300)}" instead of choosing Proceed`;
      return {
        deny: `Blast Radius held this command and did not run it: ${said}. It would have: ${summary}. Do not retry it unless the user asks you to; act on what they said.`,
      };
    }
    const why = {
      cancel: "the user pressed Cancel",
      dismissed: "the user dismissed the Blast Radius question",
      interrupted: "the turn was interrupted",
      error: "Blast Radius hit an error while holding it",
    }[decision] ?? "no answer was recorded";
    return {
      deny: `Blast Radius held this command and did not run it: ${why}. It would have: ${summary}. Do not retry it unless the user asks you to.`,
    };
  }).catch(($, e, next) => (next.called ? next(e) : { deny: "Blast Radius failed while checking this command, so it did not run. Do not retry it unless the user asks you to." }));

  on("ui.render", { component: "Pane" }, ($, e, next) => {
    if (e.requestId !== PANE_ID || held === null || held.report === null) {
      return next(e);
    }
    return draw($.ui.resolve(e), held);
  });

  on("ui.render", { component: "AbovePrompt" }, ($, e, next) => {
    if (held === null || held.report === null || held.where !== "band") {
      return next(e);
    }
    return draw($.ui.resolve(e), held);
  });
}

// ---- What counts as risky -------------------------------------------------

// sudo options that take a value, so the value isn't read as the command.
const SUDO_VALUE_OPTIONS = new Set(["-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-T", "-U"]);
// Commands that only read, so a bare word "migrate" in them isn't a migration.
const READ_ONLY = new Set(["ls", "cat", "echo", "printf", "grep", "rg", "find", "less", "head", "tail", "cd", "git"]);

/** A folder a later `cd arg` moves to, given the folder so far (null = the session folder). */
function joinDir(dir, arg) {
  if (arg === undefined || arg === "~" || arg.startsWith("/") || arg.startsWith("~/")) {
    return arg ?? "~";
  }
  return dir ? `${dir}/${arg}` : arg;
}

/** The first risky segment of a shell command, or null. */
function classify(command) {
  let dir = null; // where a `cd` earlier on the line moved to; null means the session folder
  const scopes = []; // dir to restore when a ( subshell ) closes
  const pushed = []; // pushd stack, for popd
  // Local change: NAME -> a value set earlier on the line, or null when it can't be
  // known without running something. Saved and restored around ( subshells ).
  const vars = new Map();
  const varScopes = [];
  // Local change: the separators are kept, so an assignment can be told apart by
  // what runs it. Only one that follows `;` or a line start and feeds no pipe
  // surely ran in this shell.
  const parts = command.split(/(&&|\|\||;|\||\n)/);
  for (let p = 0; p < parts.length; p += 2) {
    const raw = parts[p];
    const certain = (p === 0 || parts[p - 1] === ";" || parts[p - 1] === "\n") && parts[p + 1] !== "|";
    const opens = (raw.match(/^\s*\(+/)?.[0].trim().length) ?? 0;
    // Trailing redirects and & don't hide a closing ) : `(cd sub && make) > log`.
    const tail = raw.replace(/(?:\s*(?:\d*>>?|&>>?|<)\s*\S+|\s*&)+\s*$/, "");
    const closes = (tail.match(/\)+\s*$/)?.[0].trim().length) ?? 0;
    for (let k = 0; k < opens; k += 1) {
      scopes.push(dir);
      varScopes.push(new Map(vars));
    }
    const risk = classifySegment(raw, dir, pushed, vars, certain);
    if (risk !== null && risk.cd === undefined) {
      return risk;
    }
    if (risk !== null) {
      dir = risk.cd; // a cd, pushd or popd moved the folder
    }
    for (let k = 0; k < closes && scopes.length > 0; k += 1) {
      dir = scopes.pop(); // a cd inside ( ... ) doesn't outlive it
      const outer = varScopes.pop(); // nor does an assignment
      vars.clear();
      outer.forEach((value, name) => vars.set(name, value));
    }
  }
  return null;
}

// Words that can come before the real command without changing what it does.
const PREFIXES = new Set(["command", "exec", "env", "nohup", "time", "then", "do", "else", "!"]);

/** One segment: a risk, { cd } for a folder change, or null. */
function classifySegment(segment, dir, pushed, vars, certain) {
  {
    const notes = new Map(); // word -> what it left unexpanded (Local change)
    const words = tokenize(segment.trim().replace(/^[({]+\s*/, "").replace(/\s*[)}]+$/, ""), vars, notes);
    // Local change: a segment of plain assignments (`X=/path`, `export X=...`) sets
    // a value later words expand. One that needs a command or another unknown
    // variable is recorded as unknowable (null), never run.
    const assigns = words[0] === "export" ? words.slice(1) : words;
    if (assigns.length > 0 && assigns.every((w) => /^[A-Za-z_][A-Za-z0-9_]*=/.test(w))) {
      for (const w of assigns) {
        const eq = w.indexOf("=");
        vars.set(w.slice(0, eq), certain && !notes.has(w) ? w.slice(eq + 1) : null);
      }
      return null;
    }
    while (words.length > 0 && /^[A-Za-z_][A-Za-z0-9_]*=/.test(words[0])) {
      words.shift(); // leading VAR=value
    }
    if (words[0] === "sudo") {
      words.shift();
      while (words.length > 0 && words[0].startsWith("-")) {
        const option = words.shift();
        if (SUDO_VALUE_OPTIONS.has(option)) {
          words.shift();
        }
      }
    }
    while (words.length > 0 && (PREFIXES.has(words[0]) || /^[A-Za-z_][A-Za-z0-9_]*=/.test(words[0]))) {
      words.shift();
    }
    if (words[0] === "nice") {
      words.shift();
      if (words[0] === "-n") {
        words.splice(0, 2);
      } else if (/^-\d+$/.test(words[0] ?? "")) {
        words.shift();
      }
    }
    const [first, ...args] = words;
    if (first === undefined) {
      return null;
    }
    const cmd = first.replace(/^\\/, ""); // \rm skips aliases; it's still rm
    if (cmd === "cd") {
      return { cd: args[0] === "-" ? "-" : joinDir(dir, args[0]) };
    }
    if (cmd === "pushd") {
      pushed.push(dir);
      return { cd: joinDir(dir, args[0]) };
    }
    if (cmd === "popd") {
      return { cd: pushed.length > 0 ? pushed.pop() : "-" };
    }
    if (cmd === "rm" || cmd.endsWith("/rm")) {
      const flags = args.filter((a) => a.startsWith("-"));
      const recursive = flags.some((f) => f === "--recursive" || (/^-[^-]/.test(f) && /[rR]/.test(f)));
      const force = flags.some((f) => f === "--force" || (/^-[^-]/.test(f) && f.includes("f")));
      if (recursive || force) {
        const targets = args.filter((a) => !a.startsWith("-") || a === "-");
        // Local change: targets the tokenizer could not expand are named, with why.
        return { kind: "rm", label: `rm ${flags.join(" ")}`.trim(), targets, dir, unexpanded: unexpandedWhy(targets, notes) };
      }
    }
    if (cmd === "git") {
      // Git's own options come before the subcommand; -C moves where it runs.
      let gitDir = dir;
      let i = 0;
      while (i < args.length && args[i].startsWith("-")) {
        if (args[i] === "-C" && i + 1 < args.length) {
          gitDir = joinDir(gitDir, args[i + 1]);
          i += 2;
        } else if (args[i] === "-c" && i + 1 < args.length) {
          i += 2;
        } else {
          i += 1;
        }
      }
      const sub = args[i];
      const rest = args.slice(i + 1);
      if (sub === "reset" && rest.includes("--hard")) {
        return { kind: "git-reset", label: "git reset --hard", args: rest, dir: gitDir };
      }
      if (sub === "clean") {
        return { kind: "git-clean", label: "git clean", args: rest, dir: gitDir };
      }
      if (sub === "push" && rest.some((a) => a === "--force" || a === "-f" || a.startsWith("--force-with-lease") || /^\+/.test(a))) {
        return { kind: "git-push-force", label: "git push --force", args: rest, dir: gitDir };
      }
      const stagedOnly = sub === "restore" && rest.includes("--staged") && !rest.includes("--worktree") && !rest.includes("-W");
      if ((sub === "checkout" || sub === "restore") && rest.includes(".") && !stagedOnly) {
        return { kind: "git-checkout", label: `git ${sub} -- .`, args: rest, dir: gitDir };
      }
    }
    const joined = words.join(" ");
    if (/\balembic\s+upgrade\b/.test(joined)) {
      return { kind: "migrate", tool: "alembic", label: "alembic upgrade", dir };
    }
    if (/\bdb:migrate(?!:status\b)/.test(joined)) {
      return { kind: "migrate", tool: "rails", label: "db:migrate", dir };
    }
    if (/\bprisma\s+migrate\b/.test(joined)) {
      return { kind: "migrate", tool: "prisma", label: "prisma migrate", dir };
    }
    if (/\bmanage\.py\s+migrate\b/.test(joined)) {
      return { kind: "migrate", tool: "django", label: "manage.py migrate", dir };
    }
    if (!READ_ONLY.has(cmd) && args.includes("migrate")) {
      return { kind: "migrate", tool: "unknown", label: "migrate", dir };
    }
  }
  return null;
}

// Resolves a `cd` target to an absolute folder, or null if it doesn't exist.
// The target is passed as an argument, never as source.
const CD_SCRIPT = `unset CDPATH; d="$1"; case "$d" in "~") d="$HOME";; "~/"*) d="$HOME/\${d#\\~/}";; esac; cd -- "$d" 2>/dev/null && pwd -P`;

async function resolveDir($, sessionCwd, dir) {
  if (dir === "-") {
    return null; // `cd -` depends on the shell's history
  }
  const run = await $.process.run(["bash", "-c", CD_SCRIPT, "blast-radius", dir], { cwd: sessionCwd, timeoutMs: 5000 });
  const out = run.stdout.trim();
  return run.exitCode === 0 && out !== "" ? out : null;
}

/**
 * Splits one segment into words the way the shell does, honouring quotes and
 * backslashes. Good enough to read flags and paths.
 *
 * Local change. Upstream split at every quote, so `rm -rf "$DIR"/*` read as the
 * two targets `$DIR` and `/*`, the second one the filesystem root. Here the pieces
 * of one word stay together. Outside single quotes, `$NAME` and `${NAME}` take a
 * value set earlier on the line (`vars`), and `$HOME` at the start of a word
 * becomes `~`. Every other expansion stays as written and is recorded in `notes`
 * (word -> what it left, and why), so the caller can say it was not measured:
 * an unset variable, one whose value is unknown, an unquoted value with spaces
 * (the shell would split it), `$1` or `${X:-y}`, and `$(...)` or backticks, which
 * are kept whole and never run. A `$` from single quotes or `\$` is a plain `$`.
 */
function tokenize(text, vars = new Map(), notes = new Map()) {
  const words = [];
  let word = null; // null between words
  let left = []; // what this word left unexpanded
  let i = 0;
  const push = () => {
    if (left.length > 0) {
      notes.set(word, left);
    }
    words.push(word);
    word = null;
    left = [];
  };
  // Reads the expansion at text[i] (a `$` or a backtick): its value, or the text as written.
  const expansion = (quoted) => {
    if (text[i] === "`") {
      const end = text.indexOf("`", i + 1);
      const raw = end === -1 ? text.slice(i) : text.slice(i, end + 1);
      i += raw.length;
      left.push({ kind: "command", raw });
      return raw;
    }
    if (text[i + 1] === "(") {
      const raw = text.slice(i, closing(text, i + 1));
      i += raw.length;
      left.push({ kind: "command", raw });
      return raw;
    }
    const m = /^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))/.exec(text.slice(i));
    if (m === null) {
      const special = /^\$(?:[0-9?@*#$!-]|\{[^}]*\}?)/.exec(text.slice(i));
      if (special === null) {
        i += 1;
        return "$"; // a lone $, as the shell leaves it
      }
      i += special[0].length;
      left.push({ kind: "special", raw: special[0] });
      return special[0];
    }
    i += m[0].length;
    const name = m[1] ?? m[2];
    if (!vars.has(name)) {
      if (name === "HOME" && word === "") {
        return "~";
      }
      left.push({ kind: "unset", name });
      return m[0];
    }
    const value = vars.get(name);
    if (value === null) {
      left.push({ kind: "unknown", name });
      return m[0];
    }
    if (!quoted && /\s/.test(value)) {
      left.push({ kind: "split", name });
      return m[0];
    }
    return value;
  };
  while (i < text.length) {
    const c = text[i];
    if (/\s/.test(c)) {
      if (word !== null) {
        push();
      }
      i += 1;
      continue;
    }
    if (word === null) {
      word = "";
    }
    if (c === "'") {
      const end = text.indexOf("'", i + 1);
      word += end === -1 ? text.slice(i + 1) : text.slice(i + 1, end);
      i = end === -1 ? text.length : end + 1;
    } else if (c === '"') {
      i += 1;
      while (i < text.length && text[i] !== '"') {
        if (text[i] === "\\" && i + 1 < text.length && '"\\$`'.includes(text[i + 1])) {
          word += text[i + 1];
          i += 2;
        } else if (text[i] === "$" || text[i] === "`") {
          word += expansion(true);
        } else {
          word += text[i];
          i += 1;
        }
      }
      i += 1;
    } else if (c === "\\") {
      word += text[i + 1] ?? "";
      i += 2;
    } else if (c === "$" || c === "`") {
      word += expansion(false);
    } else {
      word += c;
      i += 1;
    }
  }
  if (word !== null) {
    push();
  }
  return words;
}

/** Local change: the index just past the `)` that closes the `(` at `open`, or the end. */
function closing(text, open) {
  let depth = 0;
  for (let k = open; k < text.length; k += 1) {
    const c = text[k];
    if (c === "\\") {
      k += 1;
    } else if (c === "'") {
      const end = text.indexOf("'", k + 1);
      if (end === -1) {
        return text.length;
      }
      k = end;
    } else if (c === "(") {
      depth += 1;
    } else if (c === ")") {
      depth -= 1;
      if (depth === 0) {
        return k + 1;
      }
    }
  }
  return text.length;
}

/**
 * Local change: the rm targets the tokenizer could not expand, and why, or null
 * when every target was expanded.
 */
function unexpandedWhy(targets, notes) {
  const left = targets.filter((t) => notes.has(t));
  if (left.length === 0) {
    return null;
  }
  const reasons = new Set();
  for (const t of left) {
    for (const n of notes.get(t)) {
      reasons.add({
        command: () => `${n.raw} runs a command, which Blast Radius does not run`,
        special: () => `${n.raw} is not a plain variable`,
        unset: () => `$${n.name} is not set on this line, and Blast Radius does not read the shell's variables`,
        unknown: () => `$${n.name} is set on this line from a command or another variable, or where it may not have run`,
        split: () => `$${n.name} is unquoted and holds spaces, so the shell splits it into several paths`,
      }[n.kind]());
    }
  }
  return { targets: left, why: [...reasons].join("; ") };
}

// ---- Measuring the blast radius -------------------------------------------

/** { summary, lines, note } for the pane. Never throws: a failed read is said, not hidden. */
async function measure($, risk, cwd) {
  try {
    if (risk.kind === "rm") {
      return await measureRm($, risk, cwd);
    }
    if (risk.kind === "migrate") {
      return await measureMigrations($, risk, cwd);
    }
    return await measureGit($, risk, cwd);
  } catch (error) {
    return { summary: `${risk.label} (could not measure it)`, lines: [], note: `Could not measure: ${String(error?.message ?? error).slice(0, 200)}` };
  }
}

// The paths are passed to bash as arguments, never as source, so nothing in
// them runs. compgen -G expands a glob without command substitution.
const RM_SCRIPT = `
shopt -s nullglob dotglob
paths=()
for p in "$@"; do
  case "$p" in "~"|"~/"*) p="$HOME\${p#\\~}";; esac
  if [[ "$p" == *[*?[]* ]]; then
    while IFS= read -r m; do paths+=("$m"); done < <(compgen -G "$p")
  elif [[ -e "$p" || -L "$p" ]]; then
    paths+=("$p")
  fi
done
if (( \${#paths[@]} == 0 )); then echo "0 0 0"; exit 0; fi
# A relative path gets ./ in front, so find never reads a name like -delete as an action.
for i in "\${!paths[@]}"; do case "\${paths[$i]}" in /*) ;; *) paths[$i]="./\${paths[$i]}";; esac; done
files=$(find "\${paths[@]}" \\( -type f -o -type l \\) 2>/dev/null | wc -l | tr -d ' ')
kb=$(du -skc "\${paths[@]}" 2>/dev/null | tail -n1 | cut -f1)
echo "$files $(( \${kb:-0} * 1024 )) \${#paths[@]}"
find "\${paths[@]}" \\( -type f -o -type l \\) 2>/dev/null | head -n ${LIST_MAX}
`;


async function measureRm($, risk, cwd) {
  if (risk.targets.length === 0) {
    return { summary: "rm with no paths", lines: [], note: "No paths to expand." };
  }
  // Local change: targets Blast Radius could not expand are not measured, and the
  // summary says so instead of "delete nothing". Upstream measured the literal `$X`.
  const skipped = risk.unexpanded;
  const measurable = skipped === null ? risk.targets : risk.targets.filter((t) => !skipped.targets.includes(t));
  if (measurable.length === 0) {
    return {
      summary: `delete what ${skipped.targets.join(" ")} matches: not measured, because ${skipped.why}`,
      lines: [],
      note: "Blast Radius does not run commands or read the shell's variables to expand a path, so it could not count what this would delete.",
    };
  }
  const report = await measureRmPaths($, { ...risk, targets: measurable }, cwd);
  if (skipped !== null) {
    report.summary += `; and what ${skipped.targets.join(" ")} matches, not measured, because ${skipped.why}`;
  }
  return report;
}

async function measureRmPaths($, risk, cwd) {
  const run = await $.process.run(["bash", "-c", RM_SCRIPT, "blast-radius", ...risk.targets], { cwd, timeoutMs: 15000 });
  const [head, ...rest] = run.stdout.split("\n").filter((l) => l !== "");
  const [files, bytes, found] = (head ?? "0 0 0").split(" ").map(Number);
  if (!found) {
    return { summary: `delete nothing: no file matches ${risk.targets.join(" ")}`, lines: [], note: "The paths don't exist, so rm has nothing to remove." };
  }
  if (!files) {
    return { summary: `delete ${found} ${found === 1 ? "path" : "paths"} with no files in ${found === 1 ? "it" : "them"}`, lines: [], note: `Paths: ${risk.targets.join(" ")}` };
  }
  return {
    summary: `delete ${files} ${files === 1 ? "file" : "files"} (about ${size(bytes)})`,
    lines: rest.map((l) => l.replace(/^\.\//, "")),
    more: Math.max(0, files - rest.length),
    note: `Paths: ${risk.targets.join(" ")}`,
  };
}

async function measureGit($, risk, cwd) {
  if (risk.kind === "git-push-force") {
    return await measurePush($, risk, cwd);
  }
  if (risk.kind === "git-clean") {
    const flags = [];
    const paths = [];
    for (let i = 0; i < risk.args.length; i += 1) {
      const a = risk.args[i];
      if (a === "--") {
        paths.push(...risk.args.slice(i + 1));
        break;
      }
      if (a === "-e" || a === "--exclude") {
        flags.push(a, risk.args[i + 1] ?? "");
        i += 1;
      } else if (a.startsWith("--exclude=") || /^-e./.test(a)) {
        flags.push(a);
      } else if (/^-[a-zA-Z]+$/.test(a)) {
        const kept = a.replace(/[finq]/g, ""); // -n is added below; -f, -i and -q would change the dry run
        if (kept !== "-") {
          flags.push(kept);
        }
      } else if (!a.startsWith("-")) {
        paths.push(a);
      }
    }
    const run = await $.process.run(["git", "clean", "-n", ...flags, "--", ...paths], { cwd, timeoutMs: 15000 });
    if (run.exitCode !== 0) {
      return { summary: "git clean (could not dry-run it)", lines: [], note: run.stderr.trim().slice(0, 200) };
    }
    const gone = run.stdout.split("\n").filter((l) => l.startsWith("Would remove ")).map((l) => l.slice(13));
    return {
      summary: gone.length === 0 ? "remove nothing: no untracked files match" : `remove ${gone.length} untracked ${gone.length === 1 ? "path" : "paths"}`,
      lines: gone.slice(0, LIST_MAX),
      more: Math.max(0, gone.length - LIST_MAX),
      note: "From git clean -n. Untracked files are not in git, so they can't be recovered.",
    };
  }
  const status = await $.process.run(["git", "status", "--porcelain"], { cwd, timeoutMs: 15000 });
  if (status.exitCode !== 0) {
    return { summary: `${risk.label} (not a git repo here?)`, lines: [], note: status.stderr.trim().slice(0, 200) };
  }
  const rows = status.stdout.split("\n").filter((l) => l.length > 3 && !l.startsWith("??"));
  // reset --hard drops staged and unstaged changes; checkout -- . drops unstaged ones.
  const lost = risk.kind === "git-reset" ? rows : rows.filter((l) => l[1] !== " ");
  const stat = await $.process.run(["git", "diff", "--shortstat", risk.kind === "git-reset" ? "HEAD" : "--"], { cwd, timeoutMs: 15000 });
  return {
    summary: lost.length === 0 ? "discard nothing: no uncommitted changes" : `discard uncommitted changes in ${lost.length} ${lost.length === 1 ? "file" : "files"}`,
    lines: lost.slice(0, LIST_MAX).map((l) => `${l.slice(0, 2)} ${l.slice(3)}`),
    more: Math.max(0, lost.length - LIST_MAX),
    note: stat.stdout.trim() !== "" ? `${stat.stdout.trim()}. Uncommitted changes can't be recovered.` : "From git status --porcelain.",
  };
}

async function measurePush($, risk, cwd) {
  const positional = risk.args.filter((a) => !a.startsWith("-"));
  const remote = positional[0] ?? "origin";
  // A refspec is src:dst. With no colon, the local branch of the same name is pushed.
  const spec = (positional[1] ?? "").replace(/^\+/, "");
  let [source, branch] = spec.includes(":") ? spec.split(":") : [spec, spec];
  branch = (branch ?? "").replace(/^refs\/heads\//, "");
  if (!branch) {
    const head = await $.process.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], { cwd, timeoutMs: 10000 });
    branch = head.stdout.trim();
    source = "HEAD";
  } else if (branch === "HEAD") {
    // `git push origin HEAD` pushes the current branch to its namesake.
    const head = await $.process.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], { cwd, timeoutMs: 10000 });
    branch = head.stdout.trim();
    source = "HEAD";
  }
  source = source || "HEAD";
  const ref = `${remote}/${branch}`;
  const known = await $.process.run(["git", "rev-parse", "--verify", "--quiet", ref], { cwd, timeoutMs: 10000 });
  if (known.exitCode !== 0) {
    return { summary: `force-push to ${ref}`, lines: [], note: `No local copy of ${ref}, so I can't tell which commits the push would drop. Run git fetch first.` };
  }
  const log = await $.process.run(["git", "log", "--oneline", "--no-decorate", `${source}..${ref}`], { cwd, timeoutMs: 15000 });
  const dropped = log.stdout.split("\n").filter((l) => l !== "");
  return {
    summary: dropped.length === 0 ? `force-push to ${ref}: drops no commits` : `force-push to ${ref}: drops ${dropped.length} ${dropped.length === 1 ? "commit" : "commits"}`,
    lines: dropped.slice(0, LIST_MAX),
    more: Math.max(0, dropped.length - LIST_MAX),
    note: `Commits on ${ref} that ${source} doesn't have, as of the last fetch.`,
  };
}

const MIGRATION_LISTERS = {
  django: { argv: ["python3", "manage.py", "showmigrations", "--plan"], pending: (l) => l.startsWith("[ ]"), strip: (l) => l.slice(4) },
  alembic: { argv: ["alembic", "history", "-r", "current:head"], pending: (l) => l.includes("->"), strip: (l) => l },
  rails: { argv: ["bin/rails", "db:migrate:status"], pending: (l) => /^\s*down\b/.test(l), strip: (l) => l.trim() },
  prisma: { argv: ["npx", "--no-install", "prisma", "migrate", "status"], pending: (l) => /^\s{2}\S/.test(l), strip: (l) => l.trim() },
};

async function measureMigrations($, risk, cwd) {
  const lister = MIGRATION_LISTERS[risk.tool];
  if (lister === undefined) {
    return { summary: "run migrations", lines: [], note: "I can't list the pending migrations for this tool, so the list is not shown." };
  }
  let run;
  try {
    run = await $.process.run(lister.argv, { cwd, timeoutMs: 20000 });
  } catch (error) {
    run = { exitCode: -1, stdout: "", stderr: String(error?.message ?? error) };
  }
  if (run.exitCode !== 0) {
    return { summary: `run ${risk.label}`, lines: [], note: `Couldn't list pending migrations (${lister.argv.join(" ")} failed).` };
  }
  const pending = run.stdout.split("\n").filter(lister.pending).map(lister.strip);
  return {
    summary: pending.length === 0 ? `run ${risk.label}: nothing pending` : `apply ${pending.length} pending ${pending.length === 1 ? "migration" : "migrations"}`,
    lines: pending.slice(0, LIST_MAX),
    more: Math.max(0, pending.length - LIST_MAX),
    note: `From ${lister.argv.join(" ")}.`,
  };
}

function size(bytes) {
  if (!Number.isFinite(bytes) || bytes < 1024) {
    return `${bytes || 0} B`;
  }
  const units = ["KB", "MB", "GB", "TB"];
  let n = bytes;
  let i = -1;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n.toFixed(n < 10 ? 1 : 0)} ${units[i]}`;
}

// ---- Drawing --------------------------------------------------------------

/**
 * Local change: the text of the engine's question dialog, for a session where no
 * surface draws the pane. The command, what it would do, and the first few paths.
 */
function question(state) {
  const { report } = state;
  const command = state.command.length > 300 ? `${state.command.slice(0, 300)}…` : state.command;
  const shown = report.lines.slice(0, QUESTION_LIST_MAX);
  const rest = report.lines.length - shown.length + (report.more ?? 0);
  const list = shown.length > 0 ? ` That includes ${shown.join(", ")}${rest > 0 ? ` and ${rest} more` : ""}.` : "";
  const note = report.note ? ` ${/[.?!]$/.test(report.note) ? report.note : `${report.note}.`}` : "";
  return `Blast Radius held \`${command}\`. It would ${report.summary}.${list}${note} Run it?`;
}

function paneRows(report) {
  return Math.min(24, 9 + report.lines.length + (report.more ? 1 : 0));
}

function draw(t, state) {
  const { Box, Text, Button } = t;
  const { report } = state;
  const list = report.lines.map((line, i) => Text({ key: `l${i}`, children: `  ${line}`, wrap: "truncate-end" }));
  if (report.more) {
    list.push(Text({ key: "more", dimColor: true, children: `  + ${report.more} more` }));
  }
  // The buttons answer the call this pane was drawn for, never whichever one is held now.
  const decide = (choice) => () => {
    if (state.decision === null) {
      state.decision = choice;
    }
  };
  return Box({
    flexDirection: "column",
    borderStyle: "round",
    borderColor: "yellow",
    paddingX: 1,
    children: [
      Text({ key: "title", bold: true, color: "yellow", children: `⚠ Blast Radius · ${state.risk.label}` }),
      Text({ key: "cmd", children: [Text({ dimColor: true, children: "Command  " }), Text({ bold: true, children: state.command })], wrap: "truncate-end" }),
      Text({ key: "sum", children: [Text({ dimColor: true, children: "Would    " }), Text({ color: "red", bold: true, children: report.summary })] }),
      Box({ key: "list", flexDirection: "column", marginTop: 1, children: list }),
      report.note ? Text({ key: "note", dimColor: true, italic: true, children: report.note, wrap: "wrap" }) : null,
      Box({
        key: "buttons",
        marginTop: 1,
        gap: 2,
        children: [
          Button({ key: "proceed", label: "Proceed", hotkey: "1", plain: true, onPress: decide("proceed") }),
          Button({ key: "cancel", label: "Cancel", hotkey: "2", plain: true, autoFocus: true, onPress: decide("cancel") }),
          Text({ key: "hint", dimColor: true, children: "Claude is waiting on your answer" }),
        ],
      }),
    ],
  });
}
