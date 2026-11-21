// Exercise launcher selection only: no processes are spawned and no packages installed.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const script = fs.readFileSync('scripts/dev.js', 'utf8');
let checks = 0;
for (const platform of ['win32', 'linux']) for (const hasVenv of [true, false]) {
  const calls = [];
  const context = {__dirname: path.resolve('scripts'), console,
    process: {platform, argv: ['node', 'dev.js', '--port', '8765'], env: {}},
    require(name) {
      if (name === 'node:fs') return {existsSync: () => hasVenv};
      if (name === 'node:child_process') return {spawn: (...args) => {calls.push(args); return new EventEmitter();}};
      return require(name);
    }};
  vm.runInNewContext(script, context);
  const [command, args, options] = calls[0];
  assert.equal(command, hasVenv ? path.join(path.resolve('.'), '.venv', platform === 'win32' ? 'Scripts/python.exe' : 'bin/python') : platform === 'win32' ? 'py' : 'python3');
  assert.equal(args.at(-1), '8765');
  assert.equal(options.windowsHide, true);
  checks++;
}
console.log(`PASS ${checks} launcher configurations: project venv preferred, system fallback preserved`);
