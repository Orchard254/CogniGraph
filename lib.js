/**
 * Shared pure helpers for the CogniGraph npm wrapper.
 *
 * Extracted verbatim-semantics from index.js and postinstall.js so the
 * helpers can be unit-tested with node:test. Behaviour is intentionally
 * IDENTICAL to the originals — this file is refactor-only, no behaviour
 * change.
 *
 * Zero npm dependencies (node builtins only).
 */

const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');

// ---------------------------------------------------------------------------
// Constants (single source of truth; values copied from index.js/postinstall.js)
// ---------------------------------------------------------------------------

const PACKAGE_VERSION = '1.0.0';
const PYTHON_MIN_VERSION = '3.12';

const PYTHON_CANDIDATES = [
  'python3',
  'python',
  'python3.12',
  'python3.13',
];

// Must mirror the package.json "files" list (python sources, schemas, hooks).
const REQUIRED_FILES = [
  'server.py', 'memory_store.py', 'pm_common.py', 'pm_search.py',
  'pm_context.py', 'pm_lifecycle.py', 'pm_structural.py', 'bootstrap.py',
  'structural_extract.py', 'branch_promotion.py', 'genesis_migrate.py',
  'watcher.py', 'wire.py', 'memory_cli.py', '__init__.py',
  'requirements.txt',
  'phase0_schema.sql', 'phase1_episodic.sql',
  'phase1_episodic_embeddings.sql', 'phase2_branch_admin.sql',
  'episodic_fts.sql', 'legacy_provenance.sql',
  'hooks/git_hook.py',
];

// Only 'mcp' is hard-required (the server cannot run without it); every
// other lane degrades gracefully, so those are warnings, not failures.
const HARD_REQUIRED_DEPS = ['mcp'];

const SOFT_DEP_LANES = {
  sqlite_vec: 'vec KNN lane (falls back to brute-force scan)',
  tree_sitter: 'structural lane',
  tree_sitter_python: 'structural lane (Python)',
  tree_sitter_javascript: 'structural lane (JavaScript)',
  tree_sitter_typescript: 'structural lane (TypeScript)',
  fastembed: 'semantic embeddings lane (falls back to tokenized matching)',
  numpy: 'semantic embeddings lane',
};

const INCOMPLETE_MARKER_FILENAME = '.postinstall-incomplete';

// ---------------------------------------------------------------------------
// Data-directory resolution (CogniGraph rebrand, Phase 1.9).
// The live DB lives at <project>/.cognigraph/memory.db; legacy checkouts
// still carry <project>/.project-memory/memory.db until first-use migration
// renames it. The Node wrapper never migrates by itself: when only the legacy
// dir exists it passes the legacy path through and the Python server migrates
// once at startup (before any store is opened). Single migration owner:
// pm_common.maybe_migrate_legacy_data_dir.
// ---------------------------------------------------------------------------

const DATA_DIR_NAME = '.cognigraph';
const LEGACY_DATA_DIR_NAME = '.project-memory';

/** Default DB path for a project root (pure except for fs injection). */
function resolveDefaultDbPath(projectRoot, fsMod = fs) {
  const current = path.join(projectRoot, DATA_DIR_NAME);
  const legacy = path.join(projectRoot, LEGACY_DATA_DIR_NAME);
  let hasCurrent = false;
  let hasLegacy = false;
  try { hasCurrent = fsMod.existsSync(current); } catch (e) { /* ignore */ }
  try { hasLegacy = fsMod.existsSync(legacy); } catch (e) { /* ignore */ }
  if (hasCurrent) return path.join(current, 'memory.db');
  if (hasLegacy) return path.join(legacy, 'memory.db');
  return path.join(current, 'memory.db');
}

// ---------------------------------------------------------------------------
// Version parsing / comparison (pure)
// ---------------------------------------------------------------------------

function parsePythonVersion(output) {
  const m = /Python\s+(\d+)\.(\d+)/.exec(output || '');
  if (!m) return null;
  return { major: parseInt(m[1], 10), minor: parseInt(m[2], 10) };
}

function parseMinVersion(minVersion = PYTHON_MIN_VERSION) {
  const m = /^(\d+)\.(\d+)$/.exec(minVersion || '');
  if (!m) return null;
  return { major: parseInt(m[1], 10), minor: parseInt(m[2], 10) };
}

function isPythonVersionSupported(version, minVersion = PYTHON_MIN_VERSION) {
  if (!version) return false;
  const min = parseMinVersion(minVersion);
  if (!min) return false;
  if (version.major > min.major) return true;
  if (version.major < min.major) return false;
  return version.minor >= min.minor;
}

function getCandidatePythonCommands() {
  return [...PYTHON_CANDIDATES];
}

// ---------------------------------------------------------------------------
// Paths / file lists (pure except for optional fs injection)
// ---------------------------------------------------------------------------

function getVenvPythonPath(packageDir, platform = os.platform()) {
  if (platform === 'win32') {
    return path.join(packageDir, 'venv', 'Scripts', 'python.exe');
  }
  return path.join(packageDir, 'venv', 'bin', 'python');
}

function getRequiredFiles() {
  return [...REQUIRED_FILES];
}

/** First missing required file under packageDir, or null when complete. */
function getMissingRequiredFile(packageDir, fsMod = fs) {
  for (const file of REQUIRED_FILES) {
    if (!fsMod.existsSync(path.join(packageDir, file))) {
      return file;
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// postinstall-incomplete marker read/write (fs-injectable for tests)
// ---------------------------------------------------------------------------

function getIncompleteMarkerPath(packageDir) {
  return path.join(packageDir, INCOMPLETE_MARKER_FILENAME);
}

function writeIncompleteMarker(packageDir, reason, fsMod = fs) {
  try {
    fsMod.writeFileSync(getIncompleteMarkerPath(packageDir), reason + '\n');
  } catch (e) { /* ignore — marker is best-effort */ }
}

function readIncompleteMarker(packageDir, fsMod = fs) {
  try {
    return fsMod.readFileSync(getIncompleteMarkerPath(packageDir), 'utf-8').trim();
  } catch (e) { /* ignore */ }
  return '';
}

function clearIncompleteMarker(packageDir, fsMod = fs) {
  try {
    if (fsMod.existsSync(getIncompleteMarkerPath(packageDir))) {
      fsMod.unlinkSync(getIncompleteMarkerPath(packageDir));
    }
  } catch (e) { /* ignore */ }
}

// ---------------------------------------------------------------------------
// Process helpers (spawn/runCommand injectable for tests)
// ---------------------------------------------------------------------------

function runCommand(command, args, options = {}, spawnFn = spawn) {
  return new Promise((resolve, reject) => {
    const child = spawnFn(command, args, {
      ...options,
      stdio: options.stdio || 'pipe',
    });

    let stdout = '';
    let stderr = '';

    if (child.stdout) {
      child.stdout.on('data', (data) => {
        stdout += data.toString();
      });
    }

    if (child.stderr) {
      child.stderr.on('data', (data) => {
        stderr += data.toString();
      });
    }

    child.on('close', (code) => {
      if (code === 0) {
        resolve({ stdout, stderr });
      } else {
        reject(new Error(`Command failed with code ${code}: ${stderr}`));
      }
    });

    child.on('error', reject);
  });
}

async function findPythonCommand(runCommandFn = runCommand) {
  for (const cmd of getCandidatePythonCommands()) {
    try {
      const { stdout } = await runCommandFn(cmd, ['--version'], { stdio: 'pipe' });
      if (isPythonVersionSupported(parsePythonVersion(stdout))) {
        return cmd;
      }
      // Version below PYTHON_MIN_VERSION or unparseable: keep looking
    } catch (error) {
      // Try next candidate
    }
  }

  return null;
}

// ---------------------------------------------------------------------------
// MCP server launch (spawn/runCommand/signal-handler injectable for tests)
// ---------------------------------------------------------------------------

/** argv for the python server process (pure). */
function buildMCPServerSpawnArgs(pythonSetup) {
  return [pythonSetup.serverScript];
}

/**
 * spawn() options for the MCP server (pure except process.env read).
 * stdio MUST stay 'inherit' — the MCP stdio channel rides on it.
 */
function buildMCPServerSpawnOptions(dbPath, extraEnv = {}) {
  return {
    env: {
      ...process.env,
      ...extraEnv,
      MEMORY_DB_PATH: dbPath,
    },
    stdio: 'inherit', // Pass through stdin/stdout/stderr for MCP communication
  };
}

async function launchMCPServer(pythonSetup, dbPath, deps = {}) {
  const spawnFn = deps.spawnFn || spawn;
  const runCommandFn = deps.runCommandFn || runCommand;
  const onSignal = deps.onSignal || ((event, handler) => process.on(event, handler));

  const env = {
    ...process.env,
    MEMORY_DB_PATH: dbPath,
  };

  console.error(`[cognigraph] Starting MCP server with database: ${dbPath}`);
  console.error(`[cognigraph] Python: ${pythonSetup.python}`);
  console.error(`[cognigraph] Server script: ${pythonSetup.serverScript}`);

  // Pre-spawn gate: a corrupt/incomplete install must fail here with a clear
  // message, not hang or crash-loop after spawn.
  try {
    await runCommandFn(pythonSetup.python, ['-m', 'py_compile', pythonSetup.serverScript], { stdio: 'pipe' });
  } catch (error) {
    console.error(`[cognigraph] Server script failed to compile: ${error.message}`);
    process.exit(1);
  }

  // Launch the Python MCP server
  let serverProcess;
  try {
    serverProcess = spawnFn(pythonSetup.python, buildMCPServerSpawnArgs(pythonSetup), {
      env,
      stdio: 'inherit', // Pass through stdin/stdout/stderr for MCP communication
    });
  } catch (error) {
    console.error('[cognigraph] Failed to spawn server process:', error.message);
    process.exit(1);
  }

  // Startup watchdog: exit within the grace window means crash-loop, not ready.
  const STARTUP_GRACE_MS = 5000;
  const startTime = Date.now();

  serverProcess.on('error', (error) => {
    console.error('[cognigraph] Failed to start server process:', error);
    process.exit(1);
  });

  serverProcess.on('exit', (code, signal) => {
    if (Date.now() - startTime < STARTUP_GRACE_MS) {
      console.error(
        `[cognigraph] Server died during startup (code ${code}, signal ${signal}). ` +
        `Likely a broken Python install — rerun setup or check requirements.`
      );
    } else {
      console.error(`[cognigraph] Server exited with code ${code}, signal ${signal}`);
    }
    process.exit(code || 1);
  });

  // Forward signals to the server process
  onSignal('SIGINT', () => serverProcess.kill('SIGINT'));
  onSignal('SIGTERM', () => serverProcess.kill('SIGTERM'));

  return serverProcess;
}

module.exports = {
  PACKAGE_VERSION,
  PYTHON_MIN_VERSION,
  PYTHON_CANDIDATES,
  REQUIRED_FILES,
  HARD_REQUIRED_DEPS,
  SOFT_DEP_LANES,
  INCOMPLETE_MARKER_FILENAME,
  DATA_DIR_NAME,
  LEGACY_DATA_DIR_NAME,
  resolveDefaultDbPath,
  parsePythonVersion,
  parseMinVersion,
  isPythonVersionSupported,
  getCandidatePythonCommands,
  getVenvPythonPath,
  getRequiredFiles,
  getMissingRequiredFile,
  getIncompleteMarkerPath,
  writeIncompleteMarker,
  readIncompleteMarker,
  clearIncompleteMarker,
  runCommand,
  findPythonCommand,
  buildMCPServerSpawnArgs,
  buildMCPServerSpawnOptions,
  launchMCPServer,
};
