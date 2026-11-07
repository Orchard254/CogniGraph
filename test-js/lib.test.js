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
    assert.ok(cmds.includes('python3.14'));
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
    for (const expected of ['server.py', 'corpus_hygiene.py', 'pm_backup.py', 'requirements.txt', 'hooks/git_hook.py', '__init__.py']) {
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

describe('resolveDefaultDbPath (.cognigraph vs legacy .project-memory)', () => {
  function makeRoot() {
    return fs.mkdtempSync(path.join(os.tmpdir(), 'cg-resolve-'));
  }

  it('prefers .cognigraph when neither dir exists (fresh project)', () => {
    const root = makeRoot();
    try {
      // Pure resolution: must not create directories as a side effect.
      assert.strictEqual(
        lib.resolveDefaultDbPath(root),
        path.join(root, '.cognigraph', 'memory.db')
      );
      assert.ok(!fs.existsSync(path.join(root, '.cognigraph')));
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it('passes the legacy path through when only .project-memory exists', () => {
    const root = makeRoot();
    try {
      fs.mkdirSync(path.join(root, '.project-memory'), { recursive: true });
      assert.strictEqual(
        lib.resolveDefaultDbPath(root),
        path.join(root, '.project-memory', 'memory.db')
      );
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it('prefers .cognigraph when both dirs exist', () => {
    const root = makeRoot();
    try {
      fs.mkdirSync(path.join(root, '.cognigraph'), { recursive: true });
      fs.mkdirSync(path.join(root, '.project-memory'), { recursive: true });
      assert.strictEqual(
        lib.resolveDefaultDbPath(root),
        path.join(root, '.cognigraph', 'memory.db')
      );
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
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

describe('launchMCPServer exit status propagation', () => {
  async function launchWithFakeChild(exitFn) {
    const handlers = {};
    const child = {
      handlers,
      on(event, fn) { handlers[event] = fn; },
      kill() {},
    };
    const origError = console.error;
    console.error = () => {};
    try {
      await lib.launchMCPServer(
        { python: '/fake/python', serverScript: '/fake/server.py' },
        '/data/memory.db',
        {
          spawnFn: () => child,
          runCommandFn: async () => ({ stdout: '', stderr: '' }),
          onSignal: () => {},
          exitFn,
        }
      );
    } finally {
      console.error = origError;
    }
    return child;
  }

  function emitExit(child, code, signal) {
    const origError = console.error;
    console.error = () => {};
    try {
      child.handlers.exit(code, signal);
    } finally {
      console.error = origError;
    }
  }

  it('preserves a clean server exit instead of converting zero to failure', async () => {
    const exitCodes = [];
    const child = await launchWithFakeChild((code) => exitCodes.push(code));
    emitExit(child, 0, null);
    assert.deepStrictEqual(exitCodes, [0]);
  });

  it('preserves a non-zero server exit code', async () => {
    const exitCodes = [];
    const child = await launchWithFakeChild((code) => exitCodes.push(code));
    emitExit(child, 7, null);
    assert.deepStrictEqual(exitCodes, [7]);
  });

  it('maps forwarded SIGINT and SIGTERM termination to conventional exit statuses', async () => {
    const exitCodes = [];
    const child = await launchWithFakeChild((code) => exitCodes.push(code));
    emitExit(child, null, 'SIGINT');
    emitExit(child, null, 'SIGTERM');
    assert.deepStrictEqual(exitCodes, [130, 143]);
  });

  it('fails closed when a child exit has neither a code nor a recognized signal', async () => {
    const exitCodes = [];
    const child = await launchWithFakeChild((code) => exitCodes.push(code));
    emitExit(child, null, null);
    assert.deepStrictEqual(exitCodes, [1]);
  });
});

describe('findPythonCommand (python3.14 alias)', () => {
  it('selects python3.14 when only that alias reports a supported version', async () => {
    const fakeRun = async (cmd) => {
      if (cmd === 'python3.14') return { stdout: 'Python 3.14.0', stderr: '' };
      throw new Error(`not found: ${cmd}`);
    };
    assert.strictEqual(await lib.findPythonCommand(fakeRun), 'python3.14');
  });
});

describe('manifest/version parity (single-source checker)', () => {
  const root = path.join(__dirname, '..');
  const pkgJson = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf-8'));
  const lockJson = JSON.parse(fs.readFileSync(path.join(root, 'package-lock.json'), 'utf-8'));
  const pyproject = fs.readFileSync(path.join(root, 'pyproject.toml'), 'utf-8');
  const setupPy = fs.readFileSync(path.join(root, 'setup.py'), 'utf-8');

  const pyprojectVersion = (/^version\s*=\s*"([^"]+)"/m.exec(pyproject) || [])[1];
  const pyprojectRequiresPython = (/^requires-python\s*=\s*"([^"]+)"/m.exec(pyproject) || [])[1];
  const pyModulesMatch = /py-modules\s*=\s*\[([^\]]+)\]/s.exec(pyproject);
  const pyModQuoted = pyModulesMatch ? [...pyModulesMatch[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]) : [];
  const schemaMatch = /_SCHEMA_FILES\s*=\s*\(([^)]+)\)/s.exec(setupPy);
  const schemaFiles = schemaMatch ? [...schemaMatch[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]) : [];

  it('exposes a semver-like package version from lib', () => {
    assert.match(lib.PACKAGE_VERSION, /^\d+\.\d+\.\d+$/);
  });

  it('keeps lib PACKAGE_VERSION in sync with package.json, package-lock.json and pyproject.toml', () => {
    assert.strictEqual(lib.PACKAGE_VERSION, pkgJson.version, 'lib PACKAGE_VERSION must equal package.json version');
    assert.strictEqual(pkgJson.version, lockJson.version, 'package.json version must equal package-lock.json top-level version');
    assert.strictEqual(lockJson.version, lockJson.packages[''].version, 'package-lock.json top-level version must equal packages[""].version');
    assert.ok(pyprojectVersion, 'pyproject.toml [project] version must be present');
    assert.strictEqual(pkgJson.version, pyprojectVersion, 'package.json version must equal pyproject.toml version');
  });

  it('keeps PYTHON_MIN_VERSION in sync with pyproject.toml requires-python', () => {
    assert.ok(pyprojectRequiresPython, 'pyproject.toml requires-python must be present');
    assert.ok(
      pyprojectRequiresPython.includes(lib.PYTHON_MIN_VERSION),
      `requires-python "${pyprojectRequiresPython}" must include PYTHON_MIN_VERSION "${lib.PYTHON_MIN_VERSION}"`
    );
  });

  it('keeps every REQUIRED_FILES entry in package.json "files"', () => {
    assert.ok(Array.isArray(pkgJson.files) && pkgJson.files.length > 0);
    for (const file of lib.getRequiredFiles()) {
      assert.ok(pkgJson.files.includes(file), `REQUIRED_FILES entry missing from package.json "files": ${file}`);
    }
  });

  it('ships the JS runtime entry points in package.json "files"', () => {
    for (const file of ['index.js', 'postinstall.js', 'lib.js']) {
      assert.ok(pkgJson.files.includes(file), `package.json "files" missing JS runtime entry: ${file}`);
    }
  });

  it('keeps every setup.py _SCHEMA_FILES entry in REQUIRED_FILES and package.json "files"', () => {
    assert.ok(schemaFiles.length > 0, 'setup.py _SCHEMA_FILES must be non-empty and parseable');
    const required = lib.getRequiredFiles();
    for (const file of schemaFiles) {
      assert.ok(required.includes(file), `setup.py _SCHEMA_FILES entry missing from REQUIRED_FILES: ${file}`);
      assert.ok(pkgJson.files.includes(file), `setup.py _SCHEMA_FILES entry missing from package.json "files": ${file}`);
    }
  });

  it('keeps every pyproject.toml py-module in REQUIRED_FILES', () => {
    assert.ok(pyModQuoted.length > 0, 'pyproject.toml py-modules must be non-empty and parseable');
    const required = lib.getRequiredFiles();
    for (const mod of pyModQuoted) {
      assert.ok(required.includes(`${mod}.py`), `pyproject py-module missing from REQUIRED_FILES: ${mod}.py`);
      assert.ok(pkgJson.files.includes(`${mod}.py`), `pyproject py-module missing from package.json "files": ${mod}.py`);
    }
  });

  it('has every REQUIRED_FILES entry on disk', () => {
    for (const file of lib.getRequiredFiles()) {
      assert.ok(fs.existsSync(path.join(root, file)), `REQUIRED_FILES entry missing on disk: ${file}`);
    }
  });

  it('has every package.json "files" entry on disk', () => {
    for (const file of pkgJson.files) {
      assert.ok(fs.existsSync(path.join(root, file)), `package.json "files" entry missing on disk: ${file}`);
    }
  });
});

describe('deriveProjectRootFromDbPath (MEMORY_DB_PATH regression)', () => {
  it('recovers the root for the standard .cognigraph layout', () => {
    const root = path.join(os.tmpdir(), 'cg-root');
    assert.strictEqual(
      lib.deriveProjectRootFromDbPath(path.join(root, '.cognigraph', 'memory.db')),
      root
    );
  });

  it('recovers the root for the legacy .project-memory layout', () => {
    const root = path.join(os.tmpdir(), 'cg-root');
    assert.strictEqual(
      lib.deriveProjectRootFromDbPath(path.join(root, '.project-memory', 'memory.db')),
      root
    );
  });

  it('returns null for a custom absolute path instead of a bogus dirname(dirname) root', () => {
    // Regression: old dirname(dirname("/data/memory.db")) gave "/" and
    // dirname(dirname("/custom/dir/my.db")) gave "/custom" — both misleading.
    assert.strictEqual(lib.deriveProjectRootFromDbPath('/data/memory.db'), null);
    assert.strictEqual(lib.deriveProjectRootFromDbPath('/custom/dir/my.db'), null);
    assert.strictEqual(lib.deriveProjectRootFromDbPath(path.join(os.tmpdir(), 'my.db')), null);
  });

  it('returns null for nested or non-standard layouts', () => {
    const root = path.join(os.tmpdir(), 'cg-root');
    assert.strictEqual(
      lib.deriveProjectRootFromDbPath(path.join(root, '.cognigraph', 'subdir', 'memory.db')),
      null
    );
    assert.strictEqual(
      lib.deriveProjectRootFromDbPath(path.join(root, 'custom', 'memory.db')),
      null
    );
  });

  it('returns null for missing input', () => {
    assert.strictEqual(lib.deriveProjectRootFromDbPath(''), null);
    assert.strictEqual(lib.deriveProjectRootFromDbPath(null), null);
    assert.strictEqual(lib.deriveProjectRootFromDbPath(undefined), null);
  });
});

