#!/usr/bin/env node

/**
 * Node test runner: discovers test-js/*.test.js via fs.readdirSync and
 * spawns `node --test <explicit paths>`. Explicit file paths are the only
 * portable form: glob arguments require Node >= 22, and the directory form
 * (`node --test test-js/`) fails on Windows with MODULE_NOT_FOUND.
 */

'use strict';

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const TEST_DIR = path.join(__dirname, '..', 'test-js');

function discoverTestFiles() {
  return fs
    .readdirSync(TEST_DIR)
    .filter((name) => name.endsWith('.test.js'))
    .sort()
    .map((name) => path.join(TEST_DIR, name));
}

function main() {
  const testFiles = discoverTestFiles();
  if (testFiles.length === 0) {
    console.error(`No *.test.js files found in ${TEST_DIR}`);
    process.exit(1);
  }

  console.log(`Running ${testFiles.length} test file(s):`);
  for (const file of testFiles) {
    console.log(`  ${path.relative(process.cwd(), file)}`);
  }

  // No shell: spawnSync on the exact interpreter binary keeps this
  // independent of PATH/PATHEXT quirks across platforms.
  const result = spawnSync(process.execPath, ['--test', ...testFiles], {
    stdio: 'inherit',
  });

  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  process.exit(result.status === null ? 1 : result.status);
}

main();