#!/usr/bin/env node
// One-command drop for any agent or person. No dependencies beyond Node + git (+ ffprobe if present).
//
//   node tools/drop.mjs --file path/to/final.mp4 --slug kai_vape \
//     --title "Streamer tried VAPING in-game and regretted it 😭💨" \
//     --description "One true sentence about the clip. #game #streamer" \
//     --moment-at 2026-11-18T12:41:00-05:00 --brand-safe
//
// Options: --platforms youtube,instagram,tiktok (default all) · --schedule tiktok=ISO,... · --grade JSON · --cover 1.2 · --no-push (stage only) · --check (validate only)
// It refuses: bad slug, slug or title already live (ledger), missing brand-safe flag, non-vertical or >3 min video.
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const argv = process.argv.slice(2);
const opt = (k, d) => { const i = argv.indexOf(`--${k}`); return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : d; };
const flag = k => argv.includes(`--${k}`);
const die = m => { console.error(`DROP REFUSED: ${m}`); process.exit(1); };

const file = opt('file'), slug = opt('slug'), title = opt('title'), description = opt('description');
if (!file || !slug || !title || !description) die('need --file --slug --title --description');
if (!fs.existsSync(file)) die(`no such file: ${file}`);
if (!/^[a-z0-9_]+$/.test(slug)) die('slug must be lowercase a-z, 0-9, _');
if (title.length > 100) die(`title is ${title.length} chars (max 100)`);
if (!flag('brand-safe')) die('pass --brand-safe only after checking: no sexualised framing, nudity, gambling, explicit lyrics, hate');
const platforms = opt('platforms', 'youtube,instagram,tiktok').split(',').map(s => s.trim()).filter(Boolean);
for (const p of platforms) if (!['youtube', 'instagram', 'tiktok'].includes(p)) die(`unknown platform ${p}`);

// Never repeat a live post (slug or title) — the ledger is the record of everything live.
const ledgerPath = path.join(ROOT, 'ledger.jsonl');
const ledger = fs.existsSync(ledgerPath) ? fs.readFileSync(ledgerPath, 'utf8').split('\n').filter(Boolean).map(l => JSON.parse(l)) : [];
const live = ledger.filter(r => r.status === 'live');
if (live.some(r => r.slug === slug)) die(`slug "${slug}" is already live`);
if (live.some(r => r.title && r.title.trim().toLowerCase() === title.trim().toLowerCase())) die('a live post already has this exact title');
if (fs.existsSync(path.join(ROOT, 'outbox', slug))) die(`outbox/${slug} already exists (drop in progress)`);

// Video sanity, if ffprobe is available.
try {
  const j = JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', file]).toString());
  const v = j.streams.find(s => s.codec_type === 'video');
  if (!v) die('no video stream');
  if (v.height <= v.width) die(`not vertical (${v.width}x${v.height})`);
  if (Number(j.format.duration) > 180) die(`too long (${Math.round(j.format.duration)} s, max 180)`);
} catch (e) { if (e.message?.startsWith('DROP REFUSED')) throw e; console.warn('note: ffprobe not available; skipped video checks'); }

if (flag('check')) { console.log(`OK: ${slug} is valid and not yet live`); process.exit(0); }

const dir = path.join(ROOT, 'outbox', slug);
fs.mkdirSync(dir, { recursive: true });
fs.copyFileSync(file, path.join(dir, 'clip.mp4'));
// --schedule tiktok=2026-11-18T23:40:00Z,instagram=... : the distribution gate's slot per platform (poster waits for it)
const schedule = {};
for (const pair of (opt('schedule', '') || '').split(',').map(s => s.trim()).filter(Boolean)) {
  const [p, when] = pair.split('=');
  if (!platforms.includes(p) || isNaN(Date.parse(when))) die(`bad --schedule entry "${pair}"`);
  schedule[p] = new Date(when).toISOString();
}
const post = { slug, title, description, platforms, cover_s: Number(opt('cover', '1.2')), moment_at: opt('moment-at', null), brand_safe: true, dropped_at: new Date().toISOString() };
if (Object.keys(schedule).length) post.schedule = schedule;
const momentWindow = opt('moment-window', null);
if (momentWindow) {
  const { login, moment_start_at, moment_end_at } = JSON.parse(momentWindow);
  Object.assign(post, { login, moment_start_at, moment_end_at });
}
const grade = opt('grade', null);                 // banger score JSON from publish, kept with the post for learning
if (grade) { try { post.grade = JSON.parse(grade); } catch { die('--grade must be JSON'); } }
fs.writeFileSync(path.join(dir, 'post.json'), JSON.stringify(post, null, 2) + '\n'); // written last
console.log(`staged outbox/${slug}`);

// a public copy (.public-build) is one machine: the poster reads outbox/ directly, nothing is committed
if (flag('no-push') || fs.existsSync(path.join(ROOT, '.public-build')) || process.env.CLIPENGINE_NO_SYNC === '1') process.exit(0);
const git = (...a) => execFileSync('git', ['-C', ROOT, ...a], { stdio: ['ignore', 'pipe', 'inherit'] }).toString();
git('pull', '--rebase', '--autostash', '-q');
git('add', '-f', `outbox/${slug}/clip.mp4`, `outbox/${slug}/post.json`);
git('commit', '-qm', `drop: ${slug}`);
git('push', '-q');
console.log(`pushed drop: ${slug} — the poster picks it up within ~30 s and posts to ${platforms.join(', ')}`);
