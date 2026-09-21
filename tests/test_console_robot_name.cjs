// Run with: node tests/test_console_robot_name.cjs
//
// The console is one shared file, so no robot's name may be baked into it — and
// the name it shows has to come from the robot's own descriptor at run time.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../src/core/static/console.html'), 'utf8');

// 1. No reference-robot name survives anywhere in the shared page.
assert.ok(!/finland/i.test(html), 'console.html still names the reference car');

// 2. Every inline script still parses.
for (const match of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);

// 3. applyRobotName() puts the descriptor's name on the page.
const start = html.indexOf('  async function applyRobotName() {');
assert.ok(start >= 0, 'applyRobotName() is missing');
const source = html.slice(start, html.indexOf('\n  }\n', start) + 4);

function paint(robotName, descriptor) {
  const els = { unit: { textContent: 'this robot' }, help: { textContent: 'Driving this robot' } };
  const ctx = {
    robotName,
    document: {
      title: 'Robot driving console',
      getElementById: id => (id === 'unit' ? els.unit : null),
      querySelector: sel => (sel === '#help h2' ? els.help : null),
    },
    fetch: async () => ({ ok: descriptor !== null, json: async () => descriptor }),
  };
  ctx.$ = ctx.document.getElementById;
  return vm.runInNewContext(source + '\napplyRobotName();', ctx)
    .then(() => ({ title: ctx.document.title, unit: els.unit.textContent, help: els.help.textContent }));
}

const name = "PiCar-Seattle-01";
const cases = [
  // Served by the gateway: the descriptor names the robot.
  paint("picar_freenove", { name }).then(painted => {
    assert.deepEqual(painted, {
      title: `Drive ${name}`, unit: name, help: `Driving ${name}`,
    });
  }),
  // Served by a robot directly: nothing to fetch, labels stay generic.
  paint("", { name }).then(painted => {
    assert.deepEqual(painted, {
      title: 'Robot driving console', unit: 'this robot', help: 'Driving this robot',
    });
  }),
  // Descriptor unreachable: still generic, never wrong.
  paint("picar_freenove", null).then(painted => {
    assert.equal(painted.unit, 'this robot');
  }),
];

Promise.all(cases).then(() => console.log('PASS: shared console names no robot and paints the one it reads.'));
