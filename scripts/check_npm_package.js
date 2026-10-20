#!/usr/bin/env node

/**
 * Guard the published npm artifact, not just the source checkout.
 *
 * package.json's "files" allowlist controls what npm publishes. A missing
 * runtime helper can therefore pass tests in the repository and still break
 * every installed/npx invocation. npm pack --dry-run returns the actual pack
 * manifest without creating a tarball.
 */
'use strict';

const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const lib = require('../lib');

const ROOT = path.resolve(__dirname, '..');

function main() {
  const npmCommand = process.platform === 'win32' ? 'npm.cmd' : 'npm';
  const packed = spawnSync(npmCommand, ['pack', '--dry-run', '--json'], {
    cwd: ROOT,
    encoding: 'utf8',
    shell: process.platform === 'win32',
    maxBuffer: 4 * 1024 * 1024,
  });

  if (packed.error) {
    console.error(`Could not run npm pack: ${packed.error.message}`);
    process.exitCode = 1;
    return;
  }
  if (packed.status !== 0) {
    console.error('npm pack --dry-run failed.');
    if (packed.stderr) console.error(packed.stderr.trim());
    process.exitCode = 1;
    return;
  }

  let result;
  try {
    result = JSON.parse(packed.stdout);
  } catch (error) {
    console.error(`npm pack did not return valid JSON: ${error.message}`);
    if (packed.stdout) console.error(packed.stdout.slice(0, 2000));
    process.exitCode = 1;
    return;
  }

  const files = result?.[0]?.files;
  if (!Array.isArray(files)) {
    console.error('npm pack JSON did not include the packed file manifest.');
    process.exitCode = 1;
    return;
  }

  const packedPaths = new Set(files.map((file) => String(file.path)));

  // pyproject.toml is the source of truth for setuptools' top-level runtime
  // modules. Compare it independently with BOTH the wrapper's required-file
  // inventory and npm's actual packed manifest. Otherwise the same omission
  // can be repeated in package.json and lib.getRequiredFiles() and escape CI.
  let pyproject;
  try {
    pyproject = fs.readFileSync(path.join(ROOT, 'pyproject.toml'), 'utf8');
  } catch (error) {
    console.error(`Could not read pyproject.toml: ${error.message}`);
    process.exitCode = 1;
    return;
  }

  const moduleBlock = pyproject.match(/py-modules\\s*=\\s*\\[([\\s\\S]*?)\\]/m);
  if (!moduleBlock) {
    console.error('Could not find the py-modules list in pyproject.toml.');
    process.exitCode = 1;
    return;
  }

  const pythonModules = [...moduleBlock[1].matchAll(/["']([^"']+)["']/g)]
    .map((match) => `${match[1]}.py`);
  if (pythonModules.length === 0) {
    console.error('The py-modules list in pyproject.toml is empty or unparseable.');
    process.exitCode = 1;
    return;
  }

  const requiredFiles = new Set(lib.getRequiredFiles());
  const omittedFromRequired = pythonModules.filter((file) => !requiredFiles.has(file));
  if (omittedFromRequired.length > 0) {
    console.error('Python runtime modules are missing from lib.getRequiredFiles():');
    for (const file of omittedFromRequired) console.error(`  - ${file}`);
    process.exitCode = 1;
    return;
  }

  const omittedFromPackage = pythonModules.filter((file) => !packedPaths.has(file));
  if (omittedFromPackage.length > 0) {
    console.error('Python runtime modules declared in pyproject.toml are missing from the npm package:');
    for (const file of omittedFromPackage) console.error(`  - ${file}`);
    process.exitCode = 1;
    return;
  }

  const required = [
    'index.js',
    'lib.js',
    'postinstall.js',
    'LICENSE',
    ...lib.getRequiredFiles(),
  ];
  const missing = [...new Set(required)].filter((file) => !packedPaths.has(file));

  if (missing.length > 0) {
    console.error('npm package is missing required runtime files:');
    for (const file of missing) console.error(`  - ${file}`);
    process.exitCode = 1;
    return;
  }

  console.log(`npm package manifest passed: ${required.length} required runtime paths are included.`);
}

main();
