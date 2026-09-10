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

const path = require('path');
const fs = require('fs');
const lib = require('./lib');

async function main() {
  // In CI the Node test job must not provision a Python venv; the Python
  // suite is set up separately in the Python job. Exit before any venv work.
  if (process.env.CI) {
    console.log('CI environment detected — skipping Python environment setup.');
    return;
  }

  console.log('🚀 Setting up project-memory MCP server...');

  const markIncomplete = (reason) => {
    // npm postinstall must stay best-effort (exit 0), but a silent success
    // hides broken installs. Leave a marker index.js picks up on startup.
    lib.writeIncompleteMarker(__dirname, reason);
    console.warn(`⚠️  POSTINSTALL INCOMPLETE (${reason}) — recorded in .postinstall-incomplete; index.js will re-verify on startup.`);
  };

  try {
    // Step 1: Check Python availability
    console.log('📋 Checking Python availability...');
    const pythonCmd = await findPythonCommand();
    if (!pythonCmd) {
      console.warn('⚠️  Python 3.12+ not found. The system will attempt to auto-install Python dependencies on first use.');
      console.log('💡 For best performance, install Python 3.12+ from python.org');
      markIncomplete('python-3.12+-not-found');
      return;
    }
    console.log(`✅ Found Python: ${pythonCmd}`);

    // Step 2: Get package directory
    const packageDir = __dirname;
    console.log(`📁 Package directory: ${packageDir}`);

    // Step 3: Check for required Python files (must mirror package.json "files")
    const requiredFiles = lib.getRequiredFiles();
    for (const file of requiredFiles) {
      if (!fs.existsSync(path.join(packageDir, file))) {
        console.warn(`⚠️  Required file missing: ${file}`);
        console.log('💡 The npm package may not have been built correctly');
        markIncomplete(`required-file-missing:${file}`);
        return;
      }
    }
    console.log('✅ All required Python files present');

    // Step 4: Set up virtual environment
    const venvPath = lib.getVenvPythonPath(packageDir);

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

    // Step 6: Verify installation (hard dep fails the gate; soft lanes warn)
    console.log('🔍 Verifying installation...');
    const hardImports = lib.HARD_REQUIRED_DEPS;
    const softLanes = lib.SOFT_DEP_LANES;
    let verifyOk = true;
    for (const mod of hardImports) {
      try {
        await runCommand(venvPath, ['-c', `import ${mod}`], { stdio: 'pipe' });
        console.log(`✅ Verified dependency: ${mod}`);
      } catch (error) {
        console.warn(`⚠️  Missing hard dependency '${mod}': install failed or incomplete`);
        verifyOk = false;
      }
    }
    for (const [mod, lane] of Object.entries(softLanes)) {
      try {
        await runCommand(venvPath, ['-c', `import ${mod}`], { stdio: 'pipe' });
        console.log(`✅ Verified optional dependency: ${mod}`);
      } catch (error) {
        console.warn(`⚠️  Optional dependency '${mod}' missing — ${lane} degraded`);
      }
    }
    if (!verifyOk) {
      console.warn('⚠️  Installation verification found gaps, but system should work on first use');
      markIncomplete('dependency-verification-gaps');
    } else {
      console.log('✅ Installation verified successfully');
      lib.clearIncompleteMarker(packageDir);
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
  return lib.findPythonCommand(runCommand);
}

function runCommand(command, args, options = {}) {
  // postinstall.js historically defaults to stdio 'inherit' (verbose setup
  // output); lib defaults to 'pipe', so keep the inherit default here.
  return lib.runCommand(command, args, { stdio: 'inherit', ...options });
}

// Run the post-install script
if (require.main === module) {
  main().catch(error => {
    console.error('Fatal error in post-install:', error);
    process.exit(1);
  });
}

module.exports = { main };
