const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const template = path.join(__dirname, '..', 'integrations', 'opencode-v2', 'index.ts.template');

test('OpenCode V2 hook injects per-request context while reusing retrieval per task', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'cognigraph-opencode-'));
  const originalCountPath = process.env.COGNIGRAPH_TEST_CALLS;
  const countFile = path.join(root, 'calls');
  process.env.COGNIGRAPH_TEST_CALLS = countFile;
  try {
    const fakeBridge = path.join(root, 'fake-bridge.js');
    fs.writeFileSync(fakeBridge, `
const fs = require('node:fs');
let input = '';
process.stdin.on('data', c => input += c);
process.stdin.on('end', () => {
  fs.appendFileSync(process.env.COGNIGRAPH_TEST_CALLS, '1');
  process.stdout.write(JSON.stringify({context_markdown: 'context for ' + JSON.parse(input).query}));
});
`);
    let code = fs.readFileSync(template, 'utf8')
      .replace('import { Plugin } from "@opencode/plugin"', 'const Plugin = { define: (definition) => definition }')
      .replace('__COGNIGRAPH_PYTHON_JSON__', JSON.stringify(process.execPath))
      .replace('__COGNIGRAPH_BRIDGE_JSON__', JSON.stringify(fakeBridge));
    const pluginFile = path.join(root, 'cognigraph.mjs');
    fs.writeFileSync(pluginFile, code);
    const plugin = (await import(pathToFileURL(pluginFile).href)).default;
    const hooks = new Map();
    const ctx = {
      location: { directory: root },
      session: { async hook(name, callback) { hooks.set(name, callback); return { async dispose() {} }; } }
    };
    await plugin.setup(ctx);
    hooks.get('prompt')({sessionID: 's1', prompt: {text: 'fix login'}});
    const req = () => ({sessionID: 's1', messages: [{role:'user', content:'fix login'}], system:[]});
    const first = req();
    await hooks.get('context')(first);
    assert.match(first.system[0].text, /context for fix login/);
    const second = req();
    await hooks.get('context')(second);
    assert.equal(second.system.length, 1);
    assert.equal(fs.readFileSync(countFile, 'utf8').length, 1);

    hooks.get('prompt')({sessionID: 's1', prompt: {text: 'add cache'}});
    const third = req(); // transcript may be stale until new prompt is admitted
    await hooks.get('context')(third);
    assert.match(third.system[0].text, /context for add cache/);
    assert.equal(fs.readFileSync(countFile, 'utf8').length, 2);

    hooks.get('prompt')({sessionID: 's2', prompt: {text: 'fix login'}});
    const other = {sessionID: 's2', messages: [], system:[]};
    await hooks.get('context')(other);
    assert.match(other.system[0].text, /context for fix login/);
    assert.equal(fs.readFileSync(countFile, 'utf8').length, 3);
  } finally {
    if (originalCountPath === undefined) delete process.env.COGNIGRAPH_TEST_CALLS;
    else process.env.COGNIGRAPH_TEST_CALLS = originalCountPath;
    fs.rmSync(root, {recursive: true, force: true});
  }
});
