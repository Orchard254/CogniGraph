#!/usr/bin/env node

/**
 * Post-installation script for @project-memory/mcp-server
 * 
 * This script runs automatically after npm install to:
 * 1. Verify Python availability
 * 2. Set up Python virtual environment if needed
 * 3. Install Python dependencies
 * 4. Verify the installation
 */

const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');

console.log('🚀 Setting up project-memory MCP server...');

async function main() {
  try {
    // Step 1: Check Python availability
    console.log('📋 Checking Python availability...');
    const pythonCmd = await findPythonCommand();
    if (!pythonCmd) {
      console.warn('⚠️  Python 3.8+ not found. The system will attempt to auto-install Python dependencies on first use.');
      console.log('💡 For best performance, install Python 3.8+ from python.org');
      return;
    }
    console.log(`✅ Found Python: ${pythonCmd}`);

    // Step 2: Get package directory
    const packageDir = __dirname;
    console.log(`📁 Package directory: ${packageDir}`);

    // Step 3: Check for required Python files
    const requiredFiles = ['server.py', 'memory_store.py', 'bootstrap.py', 'requirements.txt'];
    for (const file of requiredFiles) {
      if (!fs.existsSync(path.join(packageDir, file))) {
        console.warn(`⚠️  Required file missing: ${file}`);
        console.log('💡 The npm package may not have been built correctly');
        return;
      }
    }
    console.log('✅ All required Python files present');

    // Step 4: Set up virtual environment
    const platform = os.platform();
    let venvPath;
    if (platform === 'win32') {
      venvPath = path.join(packageDir, 'venv', 'Scripts', 'python.exe');
    } else {
      venvPath = path.join(packageDir, 'venv', 'bin', 'python');
    }

    if (!fs.existsSync(venvPath)) {
      console.log('🔧 Creating Python virtual environment...');
      await runCommand(pythonCmd, ['-m', 'venv', 'venv'], { cwd: packageDir });
      console.log('✅ Virtual environment created');
    } else {
      console.log('✅ Virtual environment already exists');
    }

    // Step 5: Install dependencies
    console.log('📦 Installing Python dependencies...');
    try {
      await runCommand(
        venvPath,
        ['-m', 'pip', 'install', '--upgrade', 'pip'],
        { cwd: packageDir }
      );
      
      await runCommand(
        venvPath,
        ['-m', 'pip', 'install', '-r', 'requirements.txt'],
        { cwd: packageDir }
      );
      console.log('✅ Python dependencies installed');
    } catch (error) {
      console.warn('⚠️  Failed to install Python dependencies:', error.message);
      console.log('💡 Dependencies will be installed on first use');
    }

    // Step 6: Verify installation
    console.log('🔍 Verifying installation...');
    try {
      await runCommand(venvPath, ['-c', 'import mcp'], { stdio: 'pipe' });
      console.log('✅ Installation verified successfully');
    } catch (error) {
      console.warn('⚠️  Installation verification failed, but system should work on first use');
    }

    console.log('🎉 Setup complete! The project-memory MCP server is ready to use.');
    console.log('💡 Add it to your IDE\'s MCP configuration:');
    console.log('   {');
    console.log('     "mcpServers": {');
    console.log('       "project-memory": {');
    console.log('         "command": "npx",');
    console.log('         "args": ["-y", "@project-memory/mcp-server"]');
    console.log('       }');
    console.log('     }');
    console.log('   }');

  } catch (error) {
    console.error('❌ Setup failed:', error.message);
    console.log('💡 The system will attempt to set up automatically on first use');
  }
}

async function findPythonCommand() {
  const candidates = ['python3', 'python', 'python3.8', 'python3.9', 'python3.10', 'python3.11', 'python3.12'];
  
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

function runCommand(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      ...options,
      stdio: options.stdio || 'inherit'
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

// Run the post-install script
if (require.main === module) {
  main().catch(error => {
    console.error('Fatal error in post-install:', error);
    process.exit(1);
  });
}

module.exports = { main };
