// The poster's own Chrome, shared by post.mjs and stats.mjs.
import fs from 'node:fs';
import { spawn } from 'node:child_process';
import { chromium } from 'playwright-core';
import { PROFILE } from './lib.mjs';

// The engine never lets Playwright launch Chrome: Playwright's launch uses a mock keychain (so it can't read
// cookies a real sign-in saved, and Chrome discards them) and flags automation (Google refuses sign-in).
// Instead it starts a plain Chrome on the engine profile with a local debugging port and attaches to it.
export const CDP_PORT = 9333;
// Chrome: CLIPENGINE_CHROME if set, else the usual install path for this OS.
export const CHROME = process.env.CLIPENGINE_CHROME || [
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
].find(p => fs.existsSync(p)) || 'google-chrome';
async function cdpUp() {
  try { const r = await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`); return r.ok; } catch { return false; }
}
export async function startChrome(urls = []) {
  if (!(await cdpUp())) {
    const child = spawn(CHROME, [`--user-data-dir=${PROFILE}`, `--remote-debugging-port=${CDP_PORT}`, '--remote-debugging-address=127.0.0.1',
      '--no-first-run', '--no-default-browser-check', ...urls], { detached: true, stdio: 'ignore' });
    child.unref();
    for (let i = 0; i < 40 && !(await cdpUp()); i++) await new Promise(r => setTimeout(r, 500));
    if (!(await cdpUp())) throw new Error('engine Chrome did not start');
  }
}
export async function launch() {
  await startChrome();
  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${CDP_PORT}`);
  const ctx = browser.contexts()[0];
  ctx.close = () => browser.close(); // detach only; Chrome keeps running with its sessions
  return ctx;
}
