// Run with: node tests/test_console_turn_power.cjs
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
function derive(on, holonomic, speed = 80, maxDuty = 1400) {
  return vm.runInNewContext(source + '\nJSON.stringify(derive())', {
    intents: () => new Set(on), cfg: { holonomic, maxDuty },
    robotName: 'picar_freenove',
    els: { speed: { value: speed } },
    clamp: (v, lo, hi) => Math.max(lo, Math.min(hi, v)),
  });
}
function check(on, holo, expected, speed, cap) {
  assert.deepEqual(JSON.parse(derive(on, holo, speed, cap)), expected);
}
for (const holo of [false, true]) {
  const turn = holo ? 720 : 1120;
  check(['spinL'], holo, {vx: 0, vy: 0, omega: turn});
  check(['spinR'], holo, {vx: 0, vy: 0, omega: -turn});
  check(['fwd'], holo, {vx: 960, vy: 0, omega: 0});
  check(['back'], holo, {vx: -960, vy: 0, omega: 0});
  check([], holo, {vx: 0, vy: 0, omega: 0});
  check(['strafeL'], holo, {vx: 0, vy: holo ? 1120 : 0, omega: 0});
}
check(['spinL'], false, {vx: 0, vy: 0, omega: 0}, 0);
check(['spinL'], false, {vx: 0, vy: 0, omega: 1000}, 100, 1000);
console.log('PASS: script syntax and 14 control cases; no hardware used.');
