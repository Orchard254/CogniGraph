'use strict';

// Tests for lib.js (shared helpers extracted from index.js / postinstall.js).
// node:test + node:assert only. No npm dependencies.

const { describe, it } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const lib = require('../lib');

describe('parsePythonVersion', () => {
  const cases = [
    ['Python 3.8.10', { major: 3, minor: 8 }],
    ['Python 3.11.2', { major: 3, minor: 11 }],
    ['Python 3.12.1', { major: 3, minor: 12 }],
    ['Python 2.7.18', { major: 2, minor: 7 }],
    ['Python 3.8', { major: 3, minor: 8 }],
    ['', null],
    [null, null],
    [undefined, null],
    ['not-a-version', null],
    ['Python', null],
  ];
  for (const [input, expected] of cases) {
    it(`parses ${JSON.stringify(input)}`, () => {
      assert.deepStrictEqual(lib.parsePythonVersion(input), expected);
    });
  }
});

describe('isPythonVersionSupported (<3.12 rejection)', () => {
  it('rejects Python below 3.12', () => {
    assert.strictEqual(lib.isPythonVersionSupported({ major: 3, minor: 11 }), false);
    assert.strictEqual(lib.isPythonVersionSupported({ major: 3, minor: 7 }), false);
    assert.strictEqual(lib.isPythonVersionSupported({ major: 2, minor: 7 }), false);
  });

  it('rejects null / unparseable versions', () => {
    assert.strictEqual(lib.isPythonVersionSupported(null), false);
    assert.strictEqual(lib.isPythonVersionSupported(undefined), false);
    assert.strictEqual(lib.isPythonVersionSupported(lib.parsePythonVersion('garbage')), false);
  });

  it('accepts 3.12+ and future majors', () => {
    assert.strictEqual(lib.isPythonVersionSupported({ major: 3, minor: 12 }), true);
    assert.strictEqual(lib.isPythonVersionSupported({ major: 3, minor: 13 }), true);
    assert.strictEqual(lib.isPythonVersionSupported({ major: 4, minor: 0 }), true);
  });
});

describe('getCandidatePythonCommands', () => {
  it('returns a non-empty list including python3 and python', () => {
    const cmds = lib.getCandidatePythonCommands();
    assert.ok(Array.isArray(cmds) && cmds.length > 0);
    assert.ok(cmds.includes('python3'));
    assert.ok(cmds.includes('python'));
  });
});

describe('findPythonCommand (injected runCommand)', () => {
  it('skips <3.12 and picks the first supported candidate', async () => {
    const versions = { python3: 'Python 3.11.2', python: 'Python 3.12.1' };
    const fakeRun = async (cmd) => {
      if (versions[cmd]) return { stdout: versions[cmd], stderr: '' };
      throw new Error(`not found: ${cmd}`);
    };
    assert.strictEqual(await lib.findPythonCommand(fakeRun), 'python');
  });

  it('returns null when every candidate is missing or <3.12', async () => {
    const alwaysOld = async () => ({ stdout: 'Python 3.11.0', stderr: '' });
    assert.strictEqual(await lib.findPythonCommand(alwaysOld), null);

    const alwaysMissing = async () => { throw new Error('ENOENT'); };
    assert.strictEqual(await lib.findPythonCommand(alwaysMissing), null);
  });
});

describe('getVenvPythonPath', () => {
  it('uses Scripts\\python.exe on win32', () => {
    assert.strictEqual(
      lib.getVenvPythonPath('/pkg', 'win32'),
      path.join('/pkg', 'venv', 'Scripts', 'python.exe')
    );
  });

  it('uses bin/python on posix platforms', () => {
    for (const platform of ['linux', 'darwin', 'freebsd']) {
      assert.strictEqual(
        lib.getVenvPythonPath('/pkg', platform),
        path.join('/pkg', 'venv', 'bin', 'python')
      );
    }
  });

  it('defaults to the current platform', () => {
    assert.strictEqual(
      lib.getVenvPythonPath('/pkg'),
      lib.getVenvPythonPath('/pkg', os.platform())
    );
  });
});

describe('getRequiredFiles', () => {
  it('is a non-empty list with the key entries', () => {
    const files = lib.getRequiredFiles();
    assert.ok(Array.isArray(files) && files.length > 0);
    for (const expected of ['server.py', 'requirements.txt', 'hooks/git_hook.py', '__init__.py']) {
      assert.ok(files.includes(expected), `missing ${expected}`);
    }
  });

  it('returns a copy (mutating it does not affect later calls)', () => {
    const a = lib.getRequiredFiles();
    a.push('evil.js');
    assert.ok(!lib.getRequiredFiles().includes('evil.js'));
  });

  it('reports no missing file for the real package dir', () => {
    assert.strictEqual(lib.getMissingRequiredFile(path.join(__dirname, '..')), null);
  });

  it('reports the first missing file for an empty dir', () => {
    const empty = fs.mkdtempSync(path.join(os.tmpdir(), 'pm-empty-'));
    try {
      assert.strictEqual(
        lib.getMissingRequiredFile(empty),
        lib.getRequiredFiles()[0]
      );
    } finally {
      fs.rmdirSync(empty);
    }
  });
});

describe('incomplete marker write/read round-trip', () => {
  it('round-trips through a tmp dir and clears cleanly', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pm-marker-'));
    try {
      assert.strictEqual(lib.readIncompleteMarker(dir), '');
      lib.writeIncompleteMarker(dir, 'test-reason');
      assert.strictEqual(lib.readIncompleteMarker(dir), 'test-reason');
      assert.ok(fs.existsSync(lib.getIncompleteMarkerPath(dir)));
      lib.clearIncompleteMarker(dir);
      assert.strictEqual(lib.readIncompleteMarker(dir), '');
      assert.ok(!fs.existsSync(lib.getIncompleteMarkerPath(dir)));
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe('launchMCPServer spawn options (injected fake spawn)', () => {
  function makeFakeChild() {
    const handlers = {};
    return {
      handlers,
      on(event, fn) { handlers[event] = fn; },
      kill() {},
    };
  }

  it('spawns with stdio inherit and MEMORY_DB_PATH passthrough', async () => {
    const spawnCalls = [];
    const runCalls = [];
    const fakeChild = makeFakeChild();
    const fakeSpawn = (cmd, args, opts) => {
      spawnCalls.push({ cmd, args, opts });
      return fakeChild;
    };
    const fakeRun = async (cmd, args, opts) => {
      runCalls.push({ cmd, args, opts });
      return { stdout: '', stderr: '' };
    };

    const pythonSetup = {
      python: '/fake/python',
      packageDir: '/fake',
      serverScript: '/fake/server.py',
    };
    const dbPath = '/data/memory.db';

    // Silence the informational console.error lines for a clean test log.
    const origError = console.error;
    console.error = () => {};
    let proc;
    try {
      proc = await lib.launchMCPServer(pythonSetup, dbPath, {
        spawnFn: fakeSpawn,
        runCommandFn: fakeRun,
        onSignal: () => {},
      });
    } finally {
      console.error = origError;
    }

    assert.strictEqual(proc, fakeChild);
    assert.strictEqual(spawnCalls.length, 1);
    const [{ cmd, args, opts }] = spawnCalls;
    assert.strictEqual(cmd, '/fake/python');
    assert.deepStrictEqual(args, ['/fake/server.py']);
    assert.strictEqual(opts.stdio, 'inherit');
    assert.strictEqual(opts.env.MEMORY_DB_PATH, dbPath);

    // Pre-spawn py_compile gate ran first.
    assert.ok(runCalls.length >= 1);
    assert.deepStrictEqual(
      runCalls[0].args,
      ['-m', 'py_compile', '/fake/server.py']
    );
  });

  it('buildMCPServerSpawnOptions passes MEMORY_DB_PATH through with stdio inherit', () => {
    const opts = lib.buildMCPServerSpawnOptions('/data/memory.db');
    assert.strictEqual(opts.stdio, 'inherit');
    assert.strictEqual(opts.env.MEMORY_DB_PATH, '/data/memory.db');
  });
});
