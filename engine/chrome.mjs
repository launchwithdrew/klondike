// The poster's own Chrome, shared by post.mjs and stats.mjs.
import fs from 'node:fs';
import { spawn } from 'node:child_process';
import { chromium } from 'playwright-core';
import path from 'node:path';
import { PROFILE, LOGS, ROOT } from './lib.mjs';

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
// CLIPENGINE_CDP_URL: attach to a browser that's already running and signed in (e.g. an AI agent's own
// browser on a VM, started with --remote-debugging-port) instead of starting the engine's Chrome.
// Saved once in config/ops.json ("browser_cdp_url") so the poster, stats and doctor all use it.
function savedCdpUrl() {
  try { return JSON.parse(fs.readFileSync(path.join(ROOT, 'config', 'ops.json'), 'utf8')).browser_cdp_url || ''; } catch { return ''; }
}
const ATTACH_URL = process.env.CLIPENGINE_CDP_URL || savedCdpUrl();
export const CDP_URL = ATTACH_URL || `http://127.0.0.1:${CDP_PORT}`;
export const ATTACHED = !!ATTACH_URL;
async function cdpUp() {
  try { const r = await fetch(`${CDP_URL}/json/version`); return r.ok; } catch { return false; }
}
function chromeFlags() {
  const f = [`--user-data-dir=${PROFILE}`, `--remote-debugging-port=${CDP_PORT}`, '--remote-debugging-address=127.0.0.1',
    '--no-first-run', '--no-default-browser-check'];
  // Chrome refuses to run as root (Docker, cloud VMs) without this
  if (typeof process.getuid === 'function' && process.getuid() === 0) f.push('--no-sandbox');
  // a Linux box with no screen can't show a window: run it headless (signing in then needs a screen
  // or CLIPENGINE_CDP_URL pointing at a browser that's already signed in)
  if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY) f.push('--headless=new');
  return f;
}
export const HEADLESS = chromeFlags().includes('--headless=new');
export async function startChrome(urls = []) {
  if (await cdpUp()) return;
  if (ATTACHED) throw new Error(`no browser answering at ${CDP_URL} (browser_cdp_url) — make sure your browser is running with remote debugging on that port, or remove browser_cdp_url from config/ops.json to use the engine's own Chrome`);
  fs.mkdirSync(LOGS, { recursive: true });
  const logPath = path.join(LOGS, 'chrome.log');
  const log = fs.openSync(logPath, 'a');
  let exited = null;
  const child = spawn(CHROME, [...chromeFlags(), ...urls], { detached: true, stdio: ['ignore', log, log] });
  child.on('error', e => { exited = `could not run ${CHROME}: ${e.message}`; });
  child.on('exit', code => { exited = exited || `Chrome exited (code ${code})`; });
  child.unref();
  for (let i = 0; i < 40 && !exited && !(await cdpUp()); i++) await new Promise(r => setTimeout(r, 500));
  if (!(await cdpUp())) {
    let tail = '';
    try { tail = fs.readFileSync(logPath, 'utf8').trim().split('\n').slice(-6).join('\n'); } catch {}
    throw new Error(`engine Chrome did not start${exited ? ` — ${exited}` : ''} (${CHROME})` +
      (tail ? `\nChrome said:\n${tail}` : '') + '\nSee docs/troubleshooting.md ("engine Chrome did not start").');
  }
}
export async function launch() {
  await startChrome();
  const browser = await chromium.connectOverCDP(CDP_URL);
  const ctx = browser.contexts()[0];
  ctx.close = () => browser.close(); // detach only; Chrome keeps running with its sessions
  return ctx;
}
