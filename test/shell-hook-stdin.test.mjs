// Background reports must not restore cooked input over a prompt that has since switched its terminal to raw.
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {test} from 'node:test';
import {shellHook} from '../src/shell-hook.mjs';
import {socketDir} from './temp.mjs';

const has = command => spawnSync(command, ['--version']).status === 0;
const python = has('python3');
const probe = fileURLToPath(new URL('./fixtures/report-stdin.py', import.meta.url));
const preload = fileURLToPath(new URL('./fixtures/report-barrier.mjs', import.meta.url));
const bin = fileURLToPath(new URL('../bin/pair.mjs', import.meta.url));

for (const shell of ['zsh', 'bash', 'fish']) {
  test(`the ${shell} background reports leave a terminal raw after starting with cooked input`, {
    skip: !python ? 'python3 not installed' : !has(shell) ? `${shell} not installed` : false,
    timeout: 60_000,
  }, () => {
    const dir = socketDir('stdin-');
    writeFileSync(join(dir, 'hook'), shellHook(shell, bin, {socket: join(dir, 'daemon')}));
    const result = spawnSync('python3', [probe, shell, dir, preload], {encoding: 'utf8', timeout: 50_000});
    assert.equal(result.error, undefined, result.error?.message);
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), ['shell', 'command_start', 'command']);
  });
}
