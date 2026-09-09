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
  const markIncomplete = (reason) => {
    // npm postinstall must stay best-effort (exit 0), but a silent success
    // hides broken installs. Leave a marker index.js picks up on startup.
    try {
      fs.writeFileSync(path.join(__dirname, '.postinstall-incomplete'), reason + '\n');
    } catch (e) { /* ignore */ }
    console.warn(`⚠️  POSTINSTALL INCOMPLETE (${reason}) — recorded in .postinstall-incomplete; index.js will re-verify on startup.`);
  };

  try {
    // Step 1: Check Python availability
    console.log('📋 Checking Python availability...');
    const pythonCmd = await findPythonCommand();
    if (!pythonCmd) {
      console.warn('⚠️  Python 3.8+ not found. The system will attempt to auto-install Python dependencies on first use.');
      console.log('💡 For best performance, install Python 3.8+ from python.org');
      markIncomplete('python-3.8+-not-found');
      return;
    }
    console.log(`✅ Found Python: ${pythonCmd}`);

    // Step 2: Get package directory
    const packageDir = __dirname;
    console.log(`📁 Package directory: ${packageDir}`);

    // Step 3: Check for required Python files (must mirror package.json "files")
    const requiredFiles = [
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

    // Step 6: Verify installation (hard dep fails the gate; soft lanes warn)
    console.log('🔍 Verifying installation...');
    const hardImports = ['mcp'];
    const softLanes = {
      'sqlite_vec': 'vec KNN lane (falls back to brute-force scan)',
      'tree_sitter': 'structural lane',
      'tree_sitter_python': 'structural lane (Python)',
      'tree_sitter_javascript': 'structural lane (JavaScript)',
      'tree_sitter_typescript': 'structural lane (TypeScript)',
      'fastembed': 'semantic embeddings lane (falls back to tokenized matching)',
      'numpy': 'semantic embeddings lane',
    };
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
      try { fs.unlinkSync(path.join(packageDir, '.postinstall-incomplete')); } catch (e) { /* ignore */ }
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
      const { stdout } = await runCommand(cmd, ['--version'], { stdio: 'pipe' });
      const m = /Python\s+(\d+)\.(\d+)/.exec(stdout || '');
      if (m && (parseInt(m[1], 10) > 3 || (parseInt(m[1], 10) === 3 && parseInt(m[2], 10) >= 8))) {
        return cmd;
      }
      // Version unparseable or < 3.8: try next candidate
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
