'use strict'

// Exercises the real Python bridge, SQLite and generated OpenCode adapter.
// Only the host's event callbacks are simulated; this is NOT a live host test.
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const {spawnSync} = require('node:child_process')
const {pathToFileURL} = require('node:url')

const REPO = path.resolve(__dirname, '..')
const TEMPLATE = path.join(REPO, 'integrations', 'opencode-v2', 'index.ts.template')
const BRIDGE = path.join(REPO, 'opencode_context.py')
const FIXTURE = path.join(__dirname, 'support', 'real_bridge_fixture.py')
const PYTHON = process.platform === 'win32'
  ? path.join(REPO, 'venv', 'Scripts', 'python.exe')
  : path.join(REPO, 'venv', 'bin', 'python')

function fixture(project, mode) {
  const run = spawnSync(PYTHON, [FIXTURE, project, mode], {
    encoding: 'utf8', timeout: 30000, cwd: REPO, env: process.env,
  })
  assert.equal(run.status, 0, (run.stderr || '') + (run.stdout || ''))
  return JSON.parse(run.stdout.trim())
}

async function hostFixture(project) {
  const code = fs.readFileSync(TEMPLATE, 'utf8')
    .replace('import { Plugin } from "@opencode/plugin"',
             'const Plugin = { define: (definition) => definition }')
    .replace('__COGNIGRAPH_PYTHON_JSON__', JSON.stringify(PYTHON))
    .replace('__COGNIGRAPH_BRIDGE_JSON__', JSON.stringify(BRIDGE))
  const file = path.join(project, 'plugin.mjs')
  fs.writeFileSync(file, code)
  const plugin = (await import(pathToFileURL(file).href)).default
  const callbacks = new Map()
  const hook = async (name, cb) => {
    callbacks.set(name, cb)
    return {async dispose() {}}
  }
  const dispose = await plugin.setup({
    location: {directory: project}, session: {hook}, tool: {hook},
  })
  return {
    prompt(text, id='s1') {
      callbacks.get('prompt')({sessionID: id, prompt: {text}})
    },
    tool(name, file, status='completed', id='s1') {
      callbacks.get('execute.after')({
        sessionID: id, tool: name, status, input: {filePath: file},
      })
    },
    context(id='s1') {
      const event = {
        sessionID: id, messages: [{role:'user', content:'restore_session'}],
        system: [],
      }
      return callbacks.get('context')(event).then(() => event)
    },
    dispose,
  }
}

test('actual Python bridge delivers source freshness and refreshes after observed edits',
  {timeout: 90000}, async (t) => {
    if (!fs.existsSync(PYTHON)) {
      assert.ok(!process.env.CI, 'CI must provide the tested virtualenv interpreter')
      return t.skip('local installed Python virtualenv unavailable')
    }
    const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'cg-real-bridge-'))
    const previous = process.env.PYTHONPATH
    let host
    try {
      const shim = path.join(scratch, 'offline')
      const project = path.join(scratch, 'fact-project')
      fs.mkdirSync(shim)
      fs.mkdirSync(project)
      // Avoid network/model downloads in test subprocesses. SQLite, ranking,
      // the bridge and rendered source-freshness logic remain real.
      fs.writeFileSync(path.join(shim, 'sitecustomize.py'),
        'import pm_common\npm_common._SEMANTIC_AVAILABLE = False\n')
      process.env.PYTHONPATH = [shim, REPO, previous].filter(Boolean).join(path.delimiter)
      fixture(project, 'seed-fact')
      host = await hostFixture(project)
      host.prompt('repair restore_session after reload')
      const initial = await host.context()
      assert.equal(initial.system.length, 1)
      assert.match(initial.system[0].text, /CogniGraph retrieval ID: [0-9]+/)
      assert.match(initial.system[0].text, /source_freshness=current/)
      assert.match(initial.system[0].text, /restore_session/)
      assert.equal(fixture(project, 'stats').usage_count, 1)

      host.tool('read', 'src/session.py')
      const inspected = await host.context()
      assert.match(inspected.system[0].text, /Current file focus/)
      assert.match(inspected.system[0].text, /src\/session.py/)
      assert.equal(fixture(project, 'stats').usage_count, 1,
        'read-only focus should not add a task receipt')

      fs.writeFileSync(path.join(project, 'src', 'session.py'),
        'def restore_session():\n    return False\n')
      host.tool('edit', 'src/session.py')
      const verified = await host.context()
      assert.equal(verified.system.length, 1)
      assert.match(verified.system[0].text, /source_freshness=possibly_stale/)
      assert.doesNotMatch(verified.system[0].text, /source_freshness=current/)
      assert.doesNotMatch(verified.system[0].text, /CogniGraph retrieval ID:/,
        'recheck must not present a new attribution receipt')
      assert.equal(fixture(project, 'stats').usage_count, 1,
        'refresh and focused context must not fabricate retrieval receipts')

      // A repeated context assembly must replace the original context block.
      await host.context()
      assert.equal(fixture(project, 'stats').usage_count, 1)
    } finally {
      if (host) await host.dispose()
      if (previous === undefined) delete process.env.PYTHONPATH
      else process.env.PYTHONPATH = previous
      fs.rmSync(scratch, {recursive: true, force: true})
    }
  })

test('actual bridge delivers unknown-first source leads with zero stored facts',
  {timeout: 60000}, async (t) => {
    if (!fs.existsSync(PYTHON)) {
      assert.ok(!process.env.CI, 'CI must provide the tested virtualenv interpreter')
      return t.skip('local installed Python virtualenv unavailable')
    }
    const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'cg-unknown-bridge-'))
    const previous = process.env.PYTHONPATH
    let host
    try {
      const shim = path.join(scratch, 'offline')
      const project = path.join(scratch, 'unknown-project')
      fs.mkdirSync(shim)
      fs.mkdirSync(project)
      fs.writeFileSync(path.join(shim, 'sitecustomize.py'),
        'import pm_common\npm_common._SEMANTIC_AVAILABLE = False\n')
      process.env.PYTHONPATH = [shim, REPO, previous].filter(Boolean).join(path.delimiter)
      fixture(project, 'seed-unknown')
      host = await hostFixture(project)
      host.prompt('repair restore_session')
      const response = await host.context()
      assert.equal(response.system.length, 1)
      assert.match(response.system[0].text, /structural index \(unverified\)/)
      assert.match(response.system[0].text, /src\/session.py/)
      assert.match(response.system[0].text, /No new relevant fact was delivered/)
      assert.doesNotMatch(response.system[0].text, /CogniGraph retrieval ID:/)
      assert.equal(fixture(project, 'stats').usage_count, 0)
    } finally {
      if (host) await host.dispose()
      if (previous === undefined) delete process.env.PYTHONPATH
      else process.env.PYTHONPATH = previous
      fs.rmSync(scratch, {recursive: true, force: true})
    }
  })
