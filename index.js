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
    // Step 1: Determine project root and database path
    const projectRoot = await findProjectRoot();
    const dbPath = path.join(projectRoot, '.project-memory', 'memory.db');
    
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
    fs.mkdirSync(dbDir, { recursive: true });
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
  
  // Create venv if it doesn't exist
  if (!fs.existsSync(venvPath)) {
    console.error('[project-memory] Creating Python virtual environment...');
    await runCommand(pythonCmd, ['-m', 'venv', 'venv'], { cwd: packageDir });
  }
  
  return {
    python: venvPath,
    packageDir,
    serverScript: path.join(packageDir, 'server.py')
  };
}

async function findPythonCommand() {
  const candidates = ['python3', 'python', 'python3.8', 'python3.9', 'python3.10', 'python3.11'];
  
  for (const cmd of candidates) {
    try {
      await runCommand(cmd, ['--version'], { stdio: 'pipe' });
      return cmd;
    } catch (error) {
      // Try next candidate
    }
  }
  
  return null;
}

async function installDependencies(pythonSetup) {
  const requirementsFile = path.join(pythonSetup.packageDir, 'requirements.txt');
  
  if (!fs.existsSync(requirementsFile)) {
    console.error('[project-memory] No requirements.txt found, skipping dependency installation');
    return;
  }
  
  // Check if dependencies are already installed (basic check)
  try {
    await runCommand(pythonSetup.python, ['-c', 'import mcp'], { stdio: 'pipe' });
    console.error('[project-memory] Dependencies already installed');
    return;
  } catch (error) {
    // Dependencies not installed, proceed with installation
  }
  
  console.error('[project-memory] Installing Python dependencies...');
  await runCommand(
    pythonSetup.python, 
    ['-m', 'pip', 'install', '-r', 'requirements.txt'],
    { cwd: pythonSetup.packageDir }
  );
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
  
  // Launch the Python MCP server
  const serverProcess = spawn(pythonSetup.python, [pythonSetup.serverScript], {
    env,
    stdio: 'inherit' // Pass through stdin/stdout/stderr for MCP communication
  });
  
  serverProcess.on('error', (error) => {
    console.error('[project-memory] Failed to start server process:', error);
    process.exit(1);
  });
  
  serverProcess.on('exit', (code, signal) => {
    console.error(`[project-memory] Server exited with code ${code}, signal ${signal}`);
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
