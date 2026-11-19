// Shared helpers: paths, ledger, locking, hashing, notifications.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';

// The repo checkout this engine lives in (engine/..), unless CLIPENGINE_ROOT says otherwise.
// It is wherever the repo was cloned.
export const ROOT = process.env.CLIPENGINE_ROOT || path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
export const OUTBOX = path.join(ROOT, 'outbox');
export const POSTED = path.join(ROOT, 'posted');
export const FAILED = path.join(ROOT, 'failed');
export const LEDGER = path.join(ROOT, 'ledger.jsonl');
export const PROFILE = path.join(ROOT, 'chrome-profile');
export const LOGS = path.join(ROOT, 'logs');

// Who this engine posts as: the channel's brand pack (brand/brand.json, or CLIPENGINE_BRAND).
const BRAND_FILE = process.env.CLIPENGINE_BRAND || path.join(ROOT, 'brand', 'brand.json');
export const BRAND = fs.existsSync(BRAND_FILE) ? JSON.parse(fs.readFileSync(BRAND_FILE, 'utf8')) : {};
const acc = BRAND.accounts || {};
export const ACCOUNTS = {
  youtube: { channelId: acc.youtube?.channelId || '', handle: acc.youtube?.handle || '' },
  instagram: { username: acc.instagram?.username || '' },
  tiktok: { username: acc.tiktok?.username || '' },
};
// commits the engine makes (ledger, stats) are signed with a neutral identity
export const GIT_AUTHOR = ['-c', 'user.name=clipengine', '-c', 'user.email=clipengine@users.noreply.github.com'];

for (const d of [OUTBOX, POSTED, FAILED, LOGS]) fs.mkdirSync(d, { recursive: true });

export function log(...a) {
  const line = `[${new Date().toISOString()}] ${a.join(' ')}`;
  console.log(line);
  fs.appendFileSync(path.join(LOGS, 'engine.log'), line + '\n');
}

export function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

export function readLedger() {
  if (!fs.existsSync(LEDGER)) return [];
  return fs.readFileSync(LEDGER, 'utf8').split('\n').filter(Boolean).map(l => JSON.parse(l));
}

export function appendLedger(rec) {
  fs.appendFileSync(LEDGER, JSON.stringify({ ...rec, at: new Date().toISOString() }) + '\n');
}

// A clip counts as posted on a platform if the ledger has a "live" record for its slug OR its file hash.
export function alreadyLive(platform, slug, hash) {
  return readLedger().find(r => r.platform === platform && r.status === 'live' && (r.slug === slug || (hash && r.sha256 === hash)));
}

export function notify(title, msg) {
  if (process.platform !== 'darwin') return;   // Mac pop-ups only; everywhere else the log line is the record
  try {
    execFileSync('osascript', ['-e', `display notification ${JSON.stringify(msg)} with title ${JSON.stringify(title)}`]);
  } catch {}
}

export function acquireLock() {
  const lock = path.join(ROOT, '.engine.lock');
  try {
    const fd = fs.openSync(lock, 'wx');
    fs.writeSync(fd, String(process.pid));
    fs.closeSync(fd);
  } catch {
    const pid = Number(fs.readFileSync(lock, 'utf8'));
    let alive = false;
    try { process.kill(pid, 0); alive = true; } catch {}
    if (alive) return null;
    fs.writeFileSync(lock, String(process.pid));
  }
  return () => { try { fs.unlinkSync(lock); } catch {} };
}

// ffmpeg/ffprobe: Homebrew's on the Mac, otherwise whatever is on PATH (Linux and other machines).
export function bin(name) {
  for (const p of [`/opt/homebrew/bin/${name}`, `/usr/local/bin/${name}`, `/usr/bin/${name}`]) if (fs.existsSync(p)) return p;
  return name;
}

export function extractFrame(mp4, seconds, out) {
  execFileSync(bin('ffmpeg'), ['-v', 'error', '-y', '-ss', String(seconds), '-i', mp4, '-frames:v', '1', '-q:v', '2', out]);
  return out;
}

export function probe(mp4) {
  const j = JSON.parse(execFileSync(bin('ffprobe'), ['-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', mp4]).toString());
  const v = j.streams.find(s => s.codec_type === 'video');
  return { duration: Number(j.format.duration), width: v?.width, height: v?.height };
}

// Attach a local file to a file input by path. Chrome runs on this Mac, so it reads the file itself:
// no transfer, no size cap (Playwright refuses >50 MB to an attached browser).
export async function setLocalFiles(page, locator, files) {
  const el = await locator.elementHandle({ timeout: 60000 });
  const cdp = await page.context().newCDPSession(page);
  try {
    await el.evaluate(e => { window.__csFileInput = e; });
    const ref = await cdp.send('Runtime.evaluate', { expression: 'window.__csFileInput' });
    await cdp.send('DOM.getDocument', { depth: 0 });
    await cdp.send('DOM.setFileInputFiles', { files: [].concat(files), objectId: ref.result.objectId });
    await el.evaluate(e => { delete window.__csFileInput; });
  } finally { await cdp.detach().catch(() => {}); }
}
