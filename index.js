#!/usr/bin/env node

/**
 * Universal MCP Server Entry Point for CogniGraph
 * 
 * This Node.js wrapper provides universal IDE compatibility by:
 * 1. Auto-detecting or creating Python virtual environment
 * 2. Installing Python dependencies if needed
 * 3. Launching the Python MCP server with proper environment
 * 4. Auto-creating project database on first use
 * 
 * Usage: npx -y cognigraph
 */

const path = require('path');
const fs = require('fs');
const lib = require('./lib');

// Configuration
const PACKAGE_VERSION = lib.PACKAGE_VERSION;
const PYTHON_MIN_VERSION = lib.PYTHON_MIN_VERSION;

async function main() {
  try {
    // Step 1: Determine project root and database path.
  // MEMORY_DB_PATH is the single source of truth when set (e.g. by the
  // caller or tests); otherwise fall back to CWD-based project detection.
  let projectRoot;
  let dbPath;
  if (process.env.MEMORY_DB_PATH) {
    dbPath = process.env.MEMORY_DB_PATH;
    projectRoot = path.dirname(path.dirname(dbPath));
  } else {
    projectRoot = await findProjectRoot();
    // .cognigraph when present, legacy .project-memory passed through for the
    // Python server's once-at-startup migration (lib.resolveDefaultDbPath).
    dbPath = lib.resolveDefaultDbPath(projectRoot);
  }
    
    // Step 2: Ensure database directory exists
    await ensureDatabaseDirectory(dbPath);
    
    // Step 3: Set up Python environment
    const pythonSetup = await setupPythonEnvironment();
    
    // Step 4: Install dependencies if needed
    await installDependencies(pythonSetup);
    
    // Step 5: Launch the Python MCP server
    await launchMCPServer(pythonSetup, dbPath);
    
  } catch (error) {
    console.error('Failed to start CogniGraph MCP server:', error.message);
    process.exit(1);
  }
}

async function findProjectRoot() {
  // Start from current working directory
  let currentDir = process.cwd();
  
  // Look for project indicators (package.json, pyproject.toml, .git, etc.)
  const projectIndicators = [
    'package.json',
    'pyproject.toml', 
    'requirements.txt',
    'setup.py',
    '.git',
    'Cargo.toml',
    'go.mod'
  ];
  
  // Search upward for project root
  while (currentDir !== path.parse(currentDir).root) {
    for (const indicator of projectIndicators) {
      if (fs.existsSync(path.join(currentDir, indicator))) {
        return currentDir;
      }
    }
    currentDir = path.dirname(currentDir);
  }
  
  // Fallback to current directory if no project indicators found
  return process.cwd();
}

async function ensureDatabaseDirectory(dbPath) {
  const dbDir = path.dirname(dbPath);
  if (!fs.existsSync(dbDir)) {
    try {
      fs.mkdirSync(dbDir, { recursive: true });
    } catch (error) {
      throw new Error(`Cannot create database directory ${dbDir}: ${error.message}`);
    }
    console.error(`[cognigraph] Created database directory: ${dbDir}`);
  }
}

async function setupPythonEnvironment() {
  // Check if Python is available
  const pythonCmd = await findPythonCommand();
  if (!pythonCmd) {
    throw new Error(`Python ${PYTHON_MIN_VERSION}+ is required but not found. Please install Python.`);
  }
  
  // Get package directory (where this script is installed)
  const packageDir = __dirname;
  
  // Check for virtual environment
  const venvPath = lib.getVenvPythonPath(packageDir);
  
  // Create venv if it doesn't exist (race-safe: two starters may race here)
  if (!fs.existsSync(venvPath)) {
    console.error('[cognigraph] Creating Python virtual environment...');
    try {
      await runCommand(pythonCmd, ['-m', 'venv', 'venv'], { cwd: packageDir });
    } catch (error) {
      if (fs.existsSync(venvPath)) {
        console.error('[cognigraph] venv appeared concurrently, continuing');
      } else {
        throw new Error(
          `Failed to create virtual environment: ${error.message}. ` +
          `Check disk space and write permission for ${packageDir}.`
        );
      }
    }
  }

  const serverScript = path.join(packageDir, 'server.py');
  if (!fs.existsSync(serverScript)) {
    throw new Error(`Server script missing: ${serverScript}. The npm package install is incomplete.`);
  }

  return {
    python: venvPath,
    packageDir,
    serverScript
  };
}

function parsePythonVersion(output) {
  return lib.parsePythonVersion(output);
}

async function findPythonCommand() {
  return lib.findPythonCommand(runCommand);
}

async function installDependencies(pythonSetup) {
  const requirementsFile = path.join(pythonSetup.packageDir, 'requirements.txt');
  const incompleteMarker = path.join(pythonSetup.packageDir, '.postinstall-incomplete');

  if (fs.existsSync(incompleteMarker)) {
    let reason = '';
    try { reason = fs.readFileSync(incompleteMarker, 'utf-8').trim(); } catch (e) { /* ignore */ }
    console.error(`[cognigraph] WARNING: npm postinstall did not complete${reason ? ` (${reason})` : ''}. Verifying install now.`);
  }

  const markComplete = () => {
    try { if (fs.existsSync(incompleteMarker)) fs.unlinkSync(incompleteMarker); } catch (e) { /* ignore */ }
  };

  if (!fs.existsSync(requirementsFile)) {
    console.error('[cognigraph] No requirements.txt found, skipping dependency installation');
    return;
  }

  // Check if dependencies are already installed.
  // Only 'mcp' is hard-required (the server cannot run without it); every
  // other lane degrades gracefully, so those are warnings, not failures.
  const hardRequired = lib.HARD_REQUIRED_DEPS;
  const softLanes = lib.SOFT_DEP_LANES;
  let missing = [];
  for (const mod of hardRequired) {
    try {
      await runCommand(pythonSetup.python, ['-c', `import ${mod}`], { stdio: 'pipe' });
    } catch (error) {
      missing.push(mod);
    }
  }
  for (const [mod, lane] of Object.entries(softLanes)) {
    try {
      await runCommand(pythonSetup.python, ['-c', `import ${mod}`], { stdio: 'pipe' });
    } catch (error) {
      console.error(`[cognigraph] Optional dependency '${mod}' missing — ${lane} degraded`);
    }
  }
  if (missing.length === 0) {
    console.error('[cognigraph] Dependencies already installed');
    markComplete();
    return;
  }
  console.error(`[cognigraph] Missing dependencies: ${missing.join(', ')}`);

  console.error('[cognigraph] Installing Python dependencies...');
  await runCommand(
    pythonSetup.python,
    ['-m', 'pip', 'install', '-r', 'requirements.txt'],
    { cwd: pythonSetup.packageDir }
  );
  markComplete();
}

async function launchMCPServer(pythonSetup, dbPath) {
  return lib.launchMCPServer(pythonSetup, dbPath);
}

function runCommand(command, args, options = {}) {
  return lib.runCommand(command, args, options);
}

// Start the server
if (require.main === module) {
  main();
}

module.exports = { main };
