#!/usr/bin/env node
// Clip engine poster.
//   node post.mjs --login     open the engine's Chrome so the channel owner signs in once
//   node post.mjs --check     confirm both accounts are signed in and correct
//   node post.mjs --once      post everything ready in outbox/, then exit
//   node post.mjs --watch     keep running; post each clip the moment its post.json lands
//   add --dry                 do everything except the final Publish/Share click
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { ROOT, SYNC, OUTBOX, POSTED, FAILED, GIT_AUTHOR, log, sha256, alreadyLive, appendLedger, notify, acquireLock, probe } from './lib.mjs';
import { postYouTube, checkYouTube } from './youtube.mjs';
import { postInstagram, checkInstagram } from './instagram.mjs';
import { postTikTok, checkTikTok } from './tiktok.mjs';

const args = new Set(process.argv.slice(2));
const DRY = args.has('--dry');
const POSTERS = { youtube: postYouTube, instagram: postInstagram, tiktok: postTikTok };
const MAX_TRIES = 3;

import { startChrome, launch, HEADLESS, ATTACHED, CDP_URL } from './chrome.mjs';

// A platform is due when it isn't live yet and its scheduled time (post.json "schedule", set by the
// distribution gate) has come. An item with nothing due is left alone — Chrome isn't even started.
function duePlatforms(meta, now = Date.now()) {
  const sched = meta.schedule || {};
  return (meta.platforms || ['youtube', 'instagram', 'tiktok'])
    .filter(p => !alreadyLive(p, meta.slug, null))
    .filter(p => !sched[p] || Date.parse(sched[p]) <= now);
}

function readyItems() {
  return fs.readdirSync(OUTBOX, { withFileTypes: true }).filter(d => d.isDirectory())
    .map(d => path.join(OUTBOX, d.name))
    .filter(dir => fs.existsSync(path.join(dir, 'post.json')) && fs.existsSync(path.join(dir, 'clip.mp4')))
    .filter(dir => {
      try { return duePlatforms(JSON.parse(fs.readFileSync(path.join(dir, 'post.json'), 'utf8'))).length > 0; }
      catch { return true; }                      // unreadable post.json: let processItem hold it with a reason
    });
}

function loadItem(dir) {
  const meta = JSON.parse(fs.readFileSync(path.join(dir, 'post.json'), 'utf8'));
  const file = path.join(dir, 'clip.mp4');
  const problems = [];
  if (!/^[a-z0-9_]+$/.test(meta.slug || '')) problems.push('slug must be a-z0-9_');
  if (!meta.title || meta.title.length > 100) problems.push('title missing or > 100 chars');
  if (!meta.description) problems.push('description missing');
  if (meta.brand_safe !== true) problems.push('brand_safe is not true');
  const p = probe(file);
  if (p.height <= p.width) problems.push(`not vertical (${p.width}x${p.height})`);
  if (p.duration > 180) problems.push(`too long (${p.duration}s)`);
  return { ...meta, dir, file, platforms: meta.platforms || ['youtube', 'instagram', 'tiktok'], sha256: sha256(file), problems };
}

function move(dir, dest) {
  const target = path.join(dest, path.basename(dir));
  fs.rmSync(target, { recursive: true, force: true });
  fs.renameSync(dir, target);
}

async function processItem(ctx, dir) {
  const item = loadItem(dir);
  if (item.problems.length) {
    log(item.slug, 'HELD:', item.problems.join('; '));
    notify('clipengine: clip held', `${item.slug}: ${item.problems.join('; ')}`);
    if (!DRY) move(dir, FAILED);
    return;
  }
  const results = {};
  for (const platform of item.platforms) {
    if (!POSTERS[platform]) { results[platform] = 'unsupported'; continue; }
    const at = (item.schedule || {})[platform];
    if (at && Date.parse(at) > Date.now() && !alreadyLive(platform, item.slug, item.sha256)) {
      log(item.slug, platform, 'scheduled for', at); results[platform] = 'scheduled'; continue;
    }
    const live = alreadyLive(platform, item.slug, item.sha256);
    if (live) { log(item.slug, platform, 'already live, skipping:', live.url); results[platform] = 'live'; continue; }
    let lastErr;
    for (let attempt = 1; attempt <= MAX_TRIES; attempt++) {
      const page = await ctx.newPage();
      try {
        const t0 = Date.now();
        const r = await POSTERS[platform](page, item, { dry: DRY });
        if (r.dry) { results[platform] = 'dry-ok'; break; }
        const latency = item.moment_at ? Math.round((Date.now() - Date.parse(item.moment_at)) / 60000) : null;
        appendLedger({ platform, slug: item.slug, sha256: item.sha256, title: item.title, url: r.url, status: 'live', found_existing: !!r.existing, post_seconds: Math.round((Date.now() - t0) / 1000), moment_to_live_min: latency });
        log(item.slug, platform, 'LIVE', r.url, latency != null ? `(${latency} min after the moment)` : '');
        results[platform] = 'live';
        break;
      } catch (e) {
        lastErr = e;
        log(item.slug, platform, `attempt ${attempt} failed:`, e.message.split('\n')[0]);
        await page.screenshot({ path: path.join(dir, `${platform}-fail-${attempt}.png`) }).catch(() => {});
      } finally { await page.close().catch(() => {}); }
    }
    if (!results[platform]) {
      results[platform] = 'failed';
      appendLedger({ platform, slug: item.slug, sha256: item.sha256, status: 'failed', error: lastErr?.message?.split('\n')[0] });
    }
  }
  const allLive = item.platforms.every(p => results[p] === 'live');
  log(item.slug, 'result', JSON.stringify(results));
  if (DRY) return;
  if (allLive) { move(dir, POSTED); notify('clipengine: live', `${item.title.slice(0, 60)} — ${item.platforms.join(' + ')}`); }
  else if (Object.values(results).every(r => r === 'live' || r === 'scheduled')) log(item.slug, 'waiting for its scheduled slot(s)');
  else notify('clipengine: needs a look', `${item.slug}: ${JSON.stringify(results)} (stays in outbox, retried next run)`);
}

// Repo sync: pull new drops from the cloud clipping session; push ledger + moves back.
const GIT_ENV = { ...process.env, GIT_SSH_COMMAND: 'ssh -o StrictHostKeyChecking=accept-new' };
function git(...a) { return execFileSync('git', ['-C', ROOT, ...a], { env: GIT_ENV, stdio: ['ignore', 'pipe', 'pipe'] }).toString().trim(); }
function pull() {
  if (!SYNC) return;               // a public copy gets engine releases only through `clipengine update`
  try { git('pull', '--rebase', '--autostash', '-q'); } catch (e) { log('git pull failed:', String(e.stderr || e.message).split('\n')[0]); }
}
function pushResults(msg) {
  if (DRY || !SYNC) return;
  try {
    // Commit only the ledger and the removal of drops that were pulled from the repo — never new media.
    git('add', 'ledger.jsonl');
    git('add', '-u', 'outbox');
    if (!git('diff', '--cached', '--name-only')) return;
    git(...GIT_AUTHOR, 'commit', '-qm', msg);
    git('push', '-q');
  } catch (e) { log('git push failed:', String(e.stderr || e.message).split('\n')[0]); }
}

async function runOnce(ctx) {
  pull();
  const items = readyItems();
  for (const dir of items) await processItem(ctx, dir);
  if (items.length) pushResults(`engine: processed ${items.map(d => path.basename(d)).join(', ')}`);
}

async function main() {
  if (args.has('--login')) {
    if (ATTACHED) {
      console.log(`Using your own browser at ${CDP_URL} — its saved sign-ins are used. If an account isn't signed in there yet, sign in there, then run: node post.mjs --check`);
      return;
    }
    if (HEADLESS) {
      console.log('This computer has no screen, so a sign-in window would be invisible. Either sign in on a computer with a ' +
        'screen, or start a browser you can see and sign in to (e.g. your agent\'s own browser) with ' +
        '--remote-debugging-port=9222 and add "browser_cdp_url": "http://127.0.0.1:9222" to config/ops.json — the engine then posts from it. ' +
        'See docs/troubleshooting.md.');
      return;
    }
    // Sign-in happens in a plain (non-automated) Chrome on the engine profile — Google blocks automated sign-in.
    await startChrome(['https://accounts.google.com/ServiceLogin?continue=https://studio.youtube.com', 'https://www.instagram.com/accounts/login/', 'https://www.tiktok.com/login']);
    console.log('Sign in to YouTube, Instagram and TikTok in the engine Chrome window. Leave it open; the engine uses it.');
    return;
  }
  const release = acquireLock();
  if (!release) { log('another engine run is active; exiting'); return; }
  if (args.has('--watch')) {
    // Idle cheaply: pull every 30 s; only start/attach Chrome when there is something to post.
    log('watching', OUTBOX, DRY ? '(dry)' : '');
    let busy = false, again = false;
    const tick = async () => {
      if (busy) { again = true; return; }
      busy = true;
      try {
        do {
          again = false;
          pull();
          const items = readyItems();
          if (!items.length) break;
          const ctx = await launch();
          try { for (const dir of items) await processItem(ctx, dir); } finally { await ctx.close().catch(() => {}); }
          pushResults(`engine: processed ${items.map(d => path.basename(d)).join(', ')}`);
        } while (again);
      } catch (e) { log('watch error:', e.message); notify('clipengine: engine error', e.message.slice(0, 120)); }
      busy = false;
    };
    fs.watch(OUTBOX, { recursive: true }, (_e, f) => { if (f && f.endsWith('post.json')) setTimeout(tick, 1500); });
    setInterval(tick, 30000);
    await tick();
    await new Promise(() => {});
  }
  const ctx = await launch();
  try {
    if (args.has('--check')) {
      const yt = await checkYouTube(await ctx.newPage());
      const ig = await checkInstagram(await ctx.newPage());
      const tt = await checkTikTok(await ctx.newPage());
      console.log(JSON.stringify({ youtube: yt, instagram: ig, tiktok: tt }));
      return;
    }
    await runOnce(ctx);
  } finally {
    await ctx.close().catch(() => {}); release();
  }
}

main().catch(e => { log('fatal:', e.stack || e.message); process.exit(1); });
