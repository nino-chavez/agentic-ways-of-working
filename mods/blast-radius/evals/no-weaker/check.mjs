// Checks that a change to the classifier never stops holding a command the base held.
// usage: node check.mjs <base blast-radius.mjs> <changed blast-radius.mjs> <tests.ts>
// Reads every string literal in the test file plus the extras below, and exits 1
// naming each one the base holds and the change does not. Nothing is run or read
// from disk beyond the three files: only the classifiers are called.
import { readFileSync } from "node:fs";
const [basePath, changedPath, testsPath] = process.argv.slice(2);
const load = (path) => {
  const src = readFileSync(path, "utf8").replace(/export function register/, "function register");
  // An older copy has only classify; a newer one has classifyCommand too.
  return new Function(`${src}\nreturn typeof classifyCommand === "function" ? classifyCommand : classify;`)();
};
const base = load(basePath);
const changed = load(changedPath);
const strings = new Set();
for (const m of readFileSync(testsPath, "utf8").matchAll(/'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"|`(?:[^`\\$]|\\.)*`/g)) {
  try {
    strings.add(new Function(`return ${m[0]}`)());
  } catch {
    // not a plain literal
  }
}
// Shapes four review rounds traced by hand on 2026-10-08, and a few neighbours.
const EXTRA = [
  'x="$(git log -1 --format="%h # %s")"; rm -rf build',
  "echo $'it\\'s' 'a #b'; rm -rf build",
  "cat <<EOF\nit's fine\nEOF\necho 'a #b'; rm -rf build",
  "echo foo\\\\\nrm -rf x",
  "rm -rf \\\n build",
  "rm \\\n -rf build",
  "git commit -m 'x; rm -rf y'",
  "(cd a && rm -rf b)",
  'D=/w; rm -rf "$D"/*',
  "bash x.sh && rm -rf b",
  "rm -rf a \\\n&& git reset --hard",
  "git push -f origin main # c",
  "python manage.py migrate # x",
];
const held = (r) => r !== null && r.kind !== "scripts";
let baseHeld = 0;
const weaker = [];
for (const c of [...strings, ...EXTRA]) {
  if (typeof c !== "string" || c === "" || !held(base(c))) continue;
  baseHeld += 1;
  if (!held(changed(c))) weaker.push(c);
}
console.log(`held by the base: ${baseHeld}; no longer held: ${weaker.length}`);
for (const c of weaker) console.log(`  no longer held: ${JSON.stringify(c)}`);
process.exit(weaker.length === 0 ? 0 : 1);
