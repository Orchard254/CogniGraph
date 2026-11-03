const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

function eventBus() {
  const queued = [];
  let waiting = null, closed = false;
  return {
    emit(event) {
      if (waiting) { const next = waiting; waiting = null; next({ value: event, done: false }); }
      else queued.push(event);
    },
    subscribe({ signal }) {
      signal.addEventListener('abort', () => {
        closed = true;
        if (waiting) { const next = waiting; waiting = null; next({ done: true }); }
      }, { once: true });
      return {
        [Symbol.asyncIterator]() { return this; },
        next() {
          if (queued.length) return Promise.resolve({ value: queued.shift(), done: false });
          if (closed) return Promise.resolve({ done: true });
          return new Promise((resolve) => { waiting = resolve; });
        },
      };
    },
  };
}
async function waitFor(predicate) {
  for (let i = 0; i < 200; i++) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error('Timed out waiting for task outcome');
}

test('OpenCode auto-reports edits at session idle with no self-ratings or cross-project paths', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'cg-opencode-outcomes-'));
  const recordFile = path.join(root, 'records.jsonl');
  const prior = process.env.COGNIGRAPH_TEST_OUTCOME_RECORDS;
  process.env.COGNIGRAPH_TEST_OUTCOME_RECORDS = recordFile;
  let cleanup;
  try {
    const fakeBridge = path.join(root, 'fake-bridge.cjs');
    fs.writeFileSync(fakeBridge, [
      "const fs = require('node:fs');",
      "let input = '';",
      "process.stdin.on('data', c => input += c);",
      "process.stdin.on('end', () => {",
      "  const req = JSON.parse(input);",
      "  if (process.argv.includes('--mode') && process.argv.includes('outcome')) {",
      "    fs.appendFileSync(process.env.COGNIGRAPH_TEST_OUTCOME_RECORDS, JSON.stringify(req) + '\\n');",
      "    process.stdout.write(JSON.stringify({ status: 'recorded', ranking_updated: false }));",
      "  } else {",
      "    process.stdout.write(JSON.stringify({ status:'ok', context_markdown:'project facts', retrieval_id:314 }));",
      "  }",
      "});",
    ].join('\n'));
    const template = path.join(__dirname, '..', 'integrations', 'opencode-v2', 'index.ts.template');
    const code = fs.readFileSync(template, 'utf8')
      .replace('import { Plugin } from "@opencode/plugin"',
               'const Plugin = { define: (definition) => definition }')
      .replace('__COGNIGRAPH_PYTHON_JSON__', JSON.stringify(process.execPath))
      .replace('__COGNIGRAPH_BRIDGE_JSON__', JSON.stringify(fakeBridge));
    const pluginFile = path.join(root, 'plugin.mjs');
    fs.writeFileSync(pluginFile, code);
    const plugin = (await import(pathToFileURL(pluginFile).href)).default;
    const hooks = new Map(), bus = eventBus();
    const hook = async (name, callback) => {
      hooks.set(name, callback);
      return { async dispose() {} };
    };
    cleanup = await plugin.setup({
      location: { directory: root },
      session: { hook }, tool: { hook },
      event: { subscribe: (options) => bus.subscribe(options) },
    });
    hooks.get('prompt')({ sessionID:'s1', prompt:{ text:'fix login' } });
    const msg = { sessionID:'s1', messages:[], system:[] };
    await hooks.get('context')(msg);
    assert.match(msg.system[0].text, /CogniGraph retrieval ID: 314/);
    const tool = hooks.get('execute.after');
    tool({ sessionID:'s1', status:'completed', tool:'read',
      input:{ filePath:'src/read-only.ts' } });
    tool({ sessionID:'s1', status:'completed', tool:'edit',
      input:{ filePath:'src/login.ts' }, result:{ output:'untrusted output' } });
    tool({ sessionID:'s1', status:'completed', tool:'write',
      input:{ filePath:path.join(root, 'src/session.ts') } });
    tool({ sessionID:'s1', status:'error', tool:'edit',
      input:{ filePath:'src/fail.ts' } });
    tool({ sessionID:'s1', status:'completed', tool:'bash',
      input:{ command:'cat src/secrets.ts' } });
    tool({ sessionID:'s1', status:'completed', tool:'edit',
      input:{ filePath:path.resolve(root, '..', 'outside.ts') } });
    tool({ sessionID:'s1', status:'completed', tool:'edit',
      input:{ filePath:'.env' } });
    bus.emit({ type:'session.idle', data:{ sessionID:'other' } });
    bus.emit({ type:'session.idle', data:{ sessionID:'s1' } });
    const records = () => fs.existsSync(recordFile)
      ? fs.readFileSync(recordFile, 'utf8').trim().split('\n').map(JSON.parse) : [];
    await waitFor(() => records().length === 1);
    assert.equal(records()[0].retrieval_id, 314);
    assert.deepEqual(records()[0].changed_files, ['src/login.ts', 'src/session.ts']);
    assert.doesNotMatch(JSON.stringify(records()), /untrusted output|read-only|secrets.ts/);
    bus.emit({ type:'session.idle', data:{ sessionID:'s1' } });
    await new Promise((resolve) => setTimeout(resolve, 150));
    assert.equal(records().length, 1, 'repeated idle is idempotent');
    hooks.get('prompt')({ sessionID:'s2', prompt:{ text:'add cache' } });
    await hooks.get('context')({ sessionID:'s2', messages:[], system:[] });
    bus.emit({ type:'session.idle', data:{ sessionID:'s2' } });
    await new Promise((resolve) => setTimeout(resolve, 80));
    assert.equal(records().length, 1, 'read-only second session does not report');
    tool({ sessionID:'s2', status:'completed', tool:'edit',
      input:{ filePath:'src/cache.ts' } });
    bus.emit({ type:'session.idle', data:{ sessionID:'s2' } });
    await waitFor(() => records().length === 2);
    assert.deepEqual(records()[1].changed_files, ['src/cache.ts']);
  } finally {
    if (cleanup) await cleanup();
    if (prior === undefined) delete process.env.COGNIGRAPH_TEST_OUTCOME_RECORDS;
    else process.env.COGNIGRAPH_TEST_OUTCOME_RECORDS = prior;
    fs.rmSync(root, { recursive:true, force:true });
  }
});
