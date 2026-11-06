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
  const req = JSON.parse(input);
  const phase = req.refresh ? 'R' : ({orient: 'O', inspect: 'I', verify: 'V'}[req.phase] ?? '?');
  fs.appendFileSync(process.env.COGNIGRAPH_TEST_CALLS, phase);
  process.stdout.write(JSON.stringify({
    context_markdown: req.file_paths?.length
      ? 'focused ' + req.phase + ' files: ' + req.file_paths.join(',')
      : 'context for ' + req.query + (req.refresh ? ' refreshed' : ''),
    retrieval_id: req.refresh || req.file_paths?.length ? null : 41,
  }));
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
      session: { async hook(name, callback) { hooks.set(name, callback); return { async dispose() {} }; } },
      tool: { async hook(name, callback) { hooks.set(name, callback); return { async dispose() {} }; } }
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
    // Each new model request needs the cached context; the same mutable
    // request should not acquire another copy when its hook retries.
    assert.equal(second.system[0].text, first.system[0].text);
    second.system.push({type:'text', text:'Independent system instruction'});
    await hooks.get('context')(second);
    assert.equal(second.system.length, 2);
    assert.equal(second.system[1].text, 'Independent system instruction');
    assert.equal(fs.readFileSync(countFile, 'utf8').length, 1);

    hooks.get('prompt')({sessionID: 's1', prompt: {text: 'add cache'}});
    const third = req(); // transcript may be stale until new prompt is admitted
    await hooks.get('context')(third);
    assert.match(third.system[0].text, /context for add cache/);
    assert.equal(fs.readFileSync(countFile, 'utf8').length, 2);

    const toolHook = hooks.get('execute.after');
    // A completed read changes the focus and refreshes just the file context.
    toolHook({sessionID:'s1', status:'completed', tool:'read',
      input:{filePath:'src/auth.ts'}});
    const focused = req();
    await hooks.get('context')(focused);
    assert.match(focused.system[0].text, /focused inspect files: src\/auth.ts/);
    assert.equal(fs.readFileSync(countFile, 'utf8').length, 3);
    await hooks.get('context')(focused);
    assert.equal(focused.system.length, 1, 'same assembled request has one memory block');
    assert.equal(fs.readFileSync(countFile, 'utf8').length, 3);

    // The same read should not trigger another retrieval.
    toolHook({sessionID:'s1', status:'completed', tool:'read',
      input:{filePath:'src/auth.ts'}});
    await hooks.get('context')(req());
    assert.equal(fs.readFileSync(countFile, 'utf8').length, 3);

    // Edits always refresh the active file briefing, even for the same path.
    toolHook({sessionID:'s1', status:'completed', tool:'edit',
      input:{filePath:'src/auth.ts'}});
    // Reassembling a prior request after an edit must replace its old
    // file-focus block, not append another.
    await hooks.get('context')(focused);
    assert.equal(focused.system.length, 1);
    assert.match(focused.system[0].text, /focused verify files/);
    assert.match(focused.system[0].text, /focused verify files/);
    assert.match(focused.system[0].text, /context for add cache refreshed/);
    assert.doesNotMatch(focused.system[0].text, /CogniGraph retrieval ID: 41/);
    assert.equal(fs.readFileSync(countFile, 'utf8'), 'OOIRV');

    // Failed tools and commands without a structured filename cannot focus.
    toolHook({sessionID:'s1', status:'error', tool:'read',
      input:{filePath:'src/passwords.ts'}});
    toolHook({sessionID:'s1', status:'completed', tool:'bash',
      input:{command:'cat src/passwords.ts'}});
    await hooks.get('context')(req());
    assert.equal(fs.readFileSync(countFile, 'utf8'), 'OOIRV');

    // Reading again returns to INSPECT even if it is the same focused file;
    // the request must not retain an obsolete post-edit VERIFY briefing.
    toolHook({sessionID:'s1', status:'completed', tool:'read',
      input:{filePath:'src/auth.ts'}});
    await hooks.get('context')(req());
    assert.equal(fs.readFileSync(countFile, 'utf8'), 'OOIRVI');

    hooks.get('prompt')({sessionID: 's2', prompt: {text: 'fix login'}});
    const other = {sessionID: 's2', messages: [], system:[]};
    await hooks.get('context')(other);
    assert.match(other.system[0].text, /context for fix login/);
    assert.equal(fs.readFileSync(countFile, 'utf8'), 'OOIRVIO');
    assert.doesNotMatch(other.system[0].text, /focused .* files/);
  } finally {
    if (originalCountPath === undefined) delete process.env.COGNIGRAPH_TEST_CALLS;
    else process.env.COGNIGRAPH_TEST_CALLS = originalCountPath;
    fs.rmSync(root, {recursive: true, force: true});
  }
});
