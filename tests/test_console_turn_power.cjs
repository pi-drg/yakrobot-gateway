// Run with: node tests/test_console_turn_power.cjs
//
// The console's derive() is the whole motor-power policy for a human driver:
// intents (key or touch) become one {vx, vy, omega} frame. These cases pin the
// two properties the floor taught us — a pivot gets the robot's whole duty
// allowance, while the speed slider still scales driving and still slows a
// pivot an operator has deliberately dialled down.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../src/core/static/console.html'), 'utf8');
for (const match of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
const start = html.indexOf('  function derive() {');
assert.ok(start >= 0);
const end = html.indexOf('\n  }', start) + 4;
const source = html.slice(start, end);
function derive(on, holonomic, speed = 80, maxDuty = 1400, robot = 'picar_freenove') {
  return vm.runInNewContext(source + '\nJSON.stringify(derive())', {
    intents: () => new Set(on), cfg: { holonomic, maxDuty },
    robotName: robot,
    els: { speed: { value: speed } },
    clamp: (v, lo, hi) => Math.max(lo, Math.min(hi, v)),
  });
}
let cases = 0;
function check(on, holo, expected, speed, cap) {
  cases++;
  const got = JSON.parse(derive(on, holo, speed, cap));
  assert.deepEqual(got, expected, `${on.join('+') || 'idle'} holo=${holo} ` +
    `speed=${speed} cap=${cap} → ${JSON.stringify(got)}`);
}

// --- both wheel types at the default 80% slider ---------------------------
for (const holo of [false, true]) {
  check(['spinL'], holo, {vx: 0, vy: 0, omega: holo ? 720 : 1400});
  check(['spinR'], holo, {vx: 0, vy: 0, omega: holo ? -720 : -1400});
  check(['fwd'], holo, {vx: 960, vy: 0, omega: 0});
  check(['back'], holo, {vx: -960, vy: 0, omega: 0});
  check([], holo, {vx: 0, vy: 0, omega: 0});
  check(['strafeL'], holo, {vx: 0, vy: holo ? 1120 : 0, omega: 0});
}

// --- ordinary wheels: pivots take the whole allowance at 80% and up -------
check(['spinL'], false, {vx: 0, vy: 0, omega: 1400}, 100);
check(['spinL'], false, {vx: 0, vy: 0, omega: 1400}, 95);

// Below the default the slider still slows the pivot rather than starving it.
for (const [speed, duty] of [[60, 1050], [40, 700], [20, 350], [10, 175]]) {
  check(['spinL'], false, {vx: 0, vy: 0, omega: duty}, speed);
}
check(['spinL'], false, {vx: 0, vy: 0, omega: 0}, 0);

// --- ordinary wheels: driving power is unchanged --------------------------
check(['fwd'], false, {vx: 1200, vy: 0, omega: 0}, 100);
check(['fwd'], false, {vx: 600, vy: 0, omega: 0}, 50);
check(['diagFL'], false, {vx: 800, vy: 0, omega: 0}, 80);

// --- the robot's cap stays the ceiling, at both ends of the useful range --
// A car whose per-unit config raises control_max_duty gets a stronger pivot…
check(['spinL'], false, {vx: 0, vy: 0, omega: 2000}, 80, 2000);
// …while a car reporting a near-maximum cap still gets a bounded pivot.
check(['spinL'], false, {vx: 0, vy: 0, omega: 2400}, 100, 4095);
check(['spinL'], false, {vx: 0, vy: 0, omega: 1000}, 100, 1000);
// Forward is capped by the same number, and never asked for more than 1200.
check(['fwd'], false, {vx: 1000, vy: 0, omega: 0}, 100, 1000);
check(['fwd'], false, {vx: 1200, vy: 0, omega: 0}, 100, 1400);

// --- mecanum pivots are untouched: 900, slider-linear ---------------------
check(['spinL'], true, {vx: 0, vy: 0, omega: 900}, 100);
check(['spinL'], true, {vx: 0, vy: 0, omega: 360}, 40);
check(['spinL'], true, {vx: 0, vy: 0, omega: 720}, 80, 800);

// The newer Adeept robot keeps its stronger carpet profile after the Freenove
// tuning is applied to the shared console.
assert.deepEqual(JSON.parse(derive(['fwd'], false, 80, 4095, 'picar_adeept')),
  {vx: 3276, vy: 0, omega: 0});
assert.deepEqual(JSON.parse(derive(['spinL'], false, 80, 4095, 'picar_adeept')),
  {vx: 0, vy: 0, omega: 2400});
assert.deepEqual(JSON.parse(derive(['spinL'], false, 100, 4095, 'picar_adeept')),
  {vx: 0, vy: 0, omega: 3000});

console.log(`PASS: script syntax, ${cases} Freenove cases and 3 Adeept cases; no hardware used.`);
