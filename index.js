#!/usr/bin/env node

/**
 * Universal MCP Server Entry Point for project-memory
 * 
 * This Node.js wrapper provides universal IDE compatibility by:
 * 1. Auto-detecting or creating Python virtual environment
 * 2. Installing Python dependencies if needed
 * 3. Launching the Python MCP server with proper environment
 * 4. Auto-creating project database on first use
 * 
 * Usage: npx -y @project-memory/mcp-server
 */

const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');

// Configuration
const PACKAGE_VERSION = '1.0.0';
const PYTHON_MIN_VERSION = '3.8';

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
    dbPath = path.join(projectRoot, '.project-memory', 'memory.db');
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
    console.error('Failed to start project-memory MCP server:', error.message);
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
    console.error(`[project-memory] Created database directory: ${dbDir}`);
  }
}

async function setupPythonEnvironment() {
  // Check if Python is available
  const pythonCmd = await findPythonCommand();
  if (!pythonCmd) {
    throw new Error('Python 3.8+ is required but not found. Please install Python.');
  }
  
  // Get package directory (where this script is installed)
  const packageDir = __dirname;
  
  // Check for virtual environment
  let venvPath;
  const platform = os.platform();
  if (platform === 'win32') {
    venvPath = path.join(packageDir, 'venv', 'Scripts', 'python.exe');
  } else {
    venvPath = path.join(packageDir, 'venv', 'bin', 'python');
  }
  
  // Create venv if it doesn't exist (race-safe: two starters may race here)
  if (!fs.existsSync(venvPath)) {
    console.error('[project-memory] Creating Python virtual environment...');
    try {
      await runCommand(pythonCmd, ['-m', 'venv', 'venv'], { cwd: packageDir });
    } catch (error) {
      if (fs.existsSync(venvPath)) {
        console.error('[project-memory] venv appeared concurrently, continuing');
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
  const m = /Python\s+(\d+)\.(\d+)/.exec(output || '');
  if (!m) return null;
  return { major: parseInt(m[1], 10), minor: parseInt(m[2], 10) };
}

async function findPythonCommand() {
  const candidates = ['python3', 'python', 'python3.8', 'python3.9', 'python3.10', 'python3.11', 'python3.12'];

  for (const cmd of candidates) {
    try {
      const { stdout } = await runCommand(cmd, ['--version'], { stdio: 'pipe' });
      const v = parsePythonVersion(stdout);
      if (v && (v.major > 3 || (v.major === 3 && v.minor >= 8))) {
        return cmd;
      }
      // Version below PYTHON_MIN_VERSION (3.8) or unparseable: keep looking
    } catch (error) {
      // Try next candidate
    }
  }

  return null;
}

async function installDependencies(pythonSetup) {
  const requirementsFile = path.join(pythonSetup.packageDir, 'requirements.txt');
  const incompleteMarker = path.join(pythonSetup.packageDir, '.postinstall-incomplete');

  if (fs.existsSync(incompleteMarker)) {
    let reason = '';
    try { reason = fs.readFileSync(incompleteMarker, 'utf-8').trim(); } catch (e) { /* ignore */ }
    console.error(`[project-memory] WARNING: npm postinstall did not complete${reason ? ` (${reason})` : ''}. Verifying install now.`);
  }

  const markComplete = () => {
    try { if (fs.existsSync(incompleteMarker)) fs.unlinkSync(incompleteMarker); } catch (e) { /* ignore */ }
  };

  if (!fs.existsSync(requirementsFile)) {
    console.error('[project-memory] No requirements.txt found, skipping dependency installation');
    return;
  }

  // Check if dependencies are already installed (every hard runtime dep)
  const requiredImports = ['mcp', 'sqlite_vec', 'tree_sitter'];
  let missing = [];
  for (const mod of requiredImports) {
    try {
      await runCommand(pythonSetup.python, ['-c', `import ${mod}`], { stdio: 'pipe' });
    } catch (error) {
      missing.push(mod);
    }
  }
  if (missing.length === 0) {
    console.error('[project-memory] Dependencies already installed');
    markComplete();
    return;
  }
  console.error(`[project-memory] Missing dependencies: ${missing.join(', ')}`);

  console.error('[project-memory] Installing Python dependencies...');
  await runCommand(
    pythonSetup.python,
    ['-m', 'pip', 'install', '-r', 'requirements.txt'],
    { cwd: pythonSetup.packageDir }
  );
  markComplete();
}

async function launchMCPServer(pythonSetup, dbPath) {
  // Set environment variable for database path
  const env = {
    ...process.env,
    MEMORY_DB_PATH: dbPath
  };
  
  console.error(`[project-memory] Starting MCP server with database: ${dbPath}`);
  console.error(`[project-memory] Python: ${pythonSetup.python}`);
  console.error(`[project-memory] Server script: ${pythonSetup.serverScript}`);

  // Pre-spawn gate: a corrupt/incomplete install must fail here with a clear
  // message, not hang or crash-loop after spawn.
  try {
    await runCommand(pythonSetup.python, ['-m', 'py_compile', pythonSetup.serverScript], { stdio: 'pipe' });
  } catch (error) {
    console.error(`[project-memory] Server script failed to compile: ${error.message}`);
    process.exit(1);
  }

  // Launch the Python MCP server
  let serverProcess;
  try {
    serverProcess = spawn(pythonSetup.python, [pythonSetup.serverScript], {
      env,
      stdio: 'inherit' // Pass through stdin/stdout/stderr for MCP communication
    });
  } catch (error) {
    console.error('[project-memory] Failed to spawn server process:', error.message);
    process.exit(1);
  }

  // Startup watchdog: exit within the grace window means crash-loop, not ready.
  const STARTUP_GRACE_MS = 5000;
  const startTime = Date.now();
  let running = true;
  
  serverProcess.on('error', (error) => {
    console.error('[project-memory] Failed to start server process:', error);
    process.exit(1);
  });
  
  serverProcess.on('exit', (code, signal) => {
    running = false;
    if (Date.now() - startTime < STARTUP_GRACE_MS) {
      console.error(
        `[project-memory] Server died during startup (code ${code}, signal ${signal}). ` +
        `Likely a broken Python install — rerun setup or check requirements.`
      );
    } else {
      console.error(`[project-memory] Server exited with code ${code}, signal ${signal}`);
    }
    process.exit(code || 1);
  });
  
  // Forward signals to the server process
  process.on('SIGINT', () => serverProcess.kill('SIGINT'));
  process.on('SIGTERM', () => serverProcess.kill('SIGTERM'));
}

function runCommand(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      ...options,
      stdio: options.stdio || 'pipe'
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

// Start the server
if (require.main === module) {
  main();
}

module.exports = { main };
