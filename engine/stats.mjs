#!/usr/bin/env node
// Views for every live post, read from each account's own profile grid in the poster's signed-in Chrome.
//   node engine/stats.mjs            snapshot every post live in the last 7 days -> data/performance.jsonl
//   node engine/stats.mjs --dry      print what it read, write nothing   (--no-push: write, don't push)
// One page per platform (TikTok profile, Instagram reels grid, YouTube Shorts tab); each tile is matched
// to the ledger by its video id. Run it hourly (a scheduled job, or `clipengine cycle`):
// `clipengine learn` uses the snapshot nearest 24 h after posting.
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { ROOT, ACCOUNTS, GIT_AUTHOR, log, readLedger } from './lib.mjs';

const HANDLES = { tiktok: ACCOUNTS.tiktok.username.replace(/^@/, ''), instagram: ACCOUNTS.instagram.username,
                  youtube: ACCOUNTS.youtube.handle.replace(/^@/, '') };
const PAGES = {
  tiktok: `https://www.tiktok.com/@${HANDLES.tiktok}`,
  instagram: `https://www.instagram.com/${HANDLES.instagram}/reels/`,
  youtube: `https://www.youtube.com/@${HANDLES.youtube}/shorts`,
};
const LINK = { tiktok: 'a[href*="/video/"]', instagram: 'a[href*="/reel/"]', youtube: 'a[href*="/shorts/"]' };
const PERF = path.join(ROOT, 'data', 'performance.jsonl');

// "1,234" "1.2K" "3.4M" "12 views" -> number
export function parseCount(text) {
  const m = String(text || '').replace(/,/g, '').match(/(\d+(?:\.\d+)?)\s*([KMB])?/i);
  if (!m) return null;
  const mult = { k: 1e3, m: 1e6, b: 1e9 }[(m[2] || '').toLowerCase()] || 1;
  return Math.round(parseFloat(m[1]) * mult);
}

// the id that identifies a post on each platform, from any of its URLs
export function postId(platform, url) {
  const re = { tiktok: /\/video\/(\d+)/, instagram: /\/(?:reel|p)\/([A-Za-z0-9_-]+)/, youtube: /\/shorts\/([A-Za-z0-9_-]+)/ }[platform];
  const m = String(url || '').match(re);
  return m ? m[1] : null;
}

// From a tile's visible text, the view count: the number next to "views", else the last number shown.
export function viewsFromTile(text) {
  const t = String(text || '');
  const near = t.match(/([\d.,]+\s*[KMB]?)\s*views?/i);
  if (near) return parseCount(near[1]);
  const all = t.match(/[\d.,]+\s*[KMB]?/gi) || [];
  return all.length ? parseCount(all[all.length - 1]) : null;
}

async function readGrid(page, platform) {
  await page.goto(PAGES[platform], { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(6000);
  for (let i = 0; i < 4; i++) { await page.mouse.wheel(0, 2500); await page.waitForTimeout(1500); }
  return page.$$eval(LINK[platform], (as, platform) => as.map(a => {
    // the tile: the link itself, or its nearest ancestor that also holds the count
    let el = a, text = a.innerText || '';
    for (let i = 0; i < 3 && !/\d/.test(text) && el.parentElement; i++) { el = el.parentElement; text = el.innerText || ''; }
    const tt = platform === 'tiktok' ? (a.querySelector('[data-e2e="video-views"]')?.innerText || '') : '';
    return { href: a.href, text: tt || text };
  }), platform);
}

async function main() {
  const dry = process.argv.includes('--dry');
  const week = Date.now() - 7 * 86400e3;
  const live = readLedger().filter(r => r.status === 'live' && r.url && Date.parse(r.at || 0) > week);
  if (!live.length) { log('stats: no live posts in the last 7 days'); return; }
  const { launch } = await import('./chrome.mjs');
  const ctx = await launch();
  const now = new Date().toISOString();
  const rows = [];
  try {
    for (const platform of ['tiktok', 'instagram', 'youtube']) {
      const mine = live.filter(r => r.platform === platform);
      if (!mine.length) continue;
      const page = await ctx.newPage();
      try {
        const tiles = await readGrid(page, platform);
        const byId = new Map(tiles.map(t => [postId(platform, t.href), t]));
        for (const r of mine) {
          const tile = byId.get(postId(platform, r.url));
          if (!tile) { log('stats', platform, r.slug, 'not found on the profile grid'); continue; }
          const views = viewsFromTile(tile.text);
          rows.push({ slug: r.slug, platform, url: r.url, checked_at: now, views,
                      age_h: Math.round((Date.now() - Date.parse(r.at)) / 36e5 * 10) / 10, source: 'stats.mjs' });
        }
      } catch (e) { log('stats', platform, 'failed:', e.message.split('\n')[0]); }
      finally { await page.close().catch(() => {}); }
    }
  } finally { await ctx.close().catch(() => {}); }
  for (const r of rows) log('stats', r.platform, r.slug, r.views, `views at ${r.age_h} h`);
  if (dry || !rows.length) return;
  fs.mkdirSync(path.dirname(PERF), { recursive: true });
  fs.appendFileSync(PERF, rows.map(r => JSON.stringify(r)).join('\n') + '\n');
  log(`stats: ${rows.length} snapshots -> data/performance.jsonl`);
  if (process.argv.includes('--no-push')) return;
  // share the numbers with every machine (the agent learns from them); a failed push is retried next hour
  const git = (...a) => execFileSync('git', ['-C', ROOT, ...a], { stdio: ['ignore', 'pipe', 'pipe'] }).toString();
  try {
    git('pull', '--rebase', '--autostash', '-q');
    git('add', 'data/performance.jsonl');
    git(...GIT_AUTHOR,
        'commit', '-qm', `stats: ${rows.length} view snapshots`);
    git('push', '-q');
  } catch (e) { log('stats: push failed, will retry next run:', e.message.split('\n')[0]); }
}

if (import.meta.url === `file://${process.argv[1]}`) main().catch(e => { log('stats fatal:', e.message); process.exit(1); });
