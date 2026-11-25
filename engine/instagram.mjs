// Instagram Reel poster — the exact flow verified by hand on 2026-11-18.
import path from 'node:path';
import { ACCOUNTS, checkPostingRights, log, extractFrame, setLocalFiles } from './lib.mjs';

const PROFILE_URL = `https://www.instagram.com/${ACCOUNTS.instagram.username}/`;

export async function checkInstagram(page) {
  await page.goto(PROFILE_URL, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(3000);
  const loginWall = /accounts\/login/.test(page.url()) || await page.locator('input[name="username"], a[href*="/accounts/login"]').count();
  if (loginWall) return { ok: false, why: 'not logged in to Instagram' };
  const own = await page.locator('text=Edit profile').count();
  if (!own) return { ok: false, why: `logged in, but not as ${ACCOUNTS.instagram.username}` };
  return { ok: true };
}

async function newestReels(page) {
  return page.$$eval('main a[href*="/reel/"]', as => as.map(a => ({ href: a.getAttribute('href'), alt: a.querySelector('img')?.alt || '' })));
}

const dlgBtn = (page, name) => page.locator('div[role=dialog] div[role=button], div[role=dialog] button').filter({ hasText: new RegExp(`^${name}$`) }).first();

export async function postInstagram(page, item, { dry = false } = {}) {
  checkPostingRights(item);
  const chk = await checkInstagram(page);
  if (!chk.ok) throw new Error(`Instagram check failed: ${chk.why}`);
  const before = await newestReels(page);
  const existing = before.find(r => r.alt.startsWith(item.title.slice(0, 30)));
  if (existing) return { url: `https://www.instagram.com${existing.href}`, existing: true };

  await page.locator('svg[aria-label="New post"]').first().click();
  const postItem = page.locator('a, div[role=button], span').filter({ hasText: /^Post$/ }).first();
  if (await postItem.isVisible({ timeout: 3000 }).catch(() => false)) await postItem.click();
  await setLocalFiles(page, page.locator('div[role=dialog] input[type=file]').first(), item.file);
  log('instagram', item.slug, 'file attached');

  const ok = page.locator('div[role=dialog] button').filter({ hasText: /^OK$/ }).first();
  if (await ok.isVisible({ timeout: 6000 }).catch(() => false)) await ok.click();

  // Crop → Original (keep the full 9:16 frame).
  await page.locator('svg[aria-label="Select crop"]').first().click({ timeout: 30000 });
  await page.locator('div[role=dialog] span, div[role=dialog] div').filter({ hasText: /^Original$/ }).first().click();
  await dlgBtn(page, 'Next').click();

  // Edit → cover from a frame at cover_s.
  const cover = extractFrame(item.file, item.cover_s ?? 1.2, path.join(path.dirname(item.file), 'cover.jpg'));
  const coverInput = page.locator('div[role=dialog] input[type=file][accept*="image"]').first();
  if (await coverInput.count()) { await setLocalFiles(page, coverInput, cover); await page.waitForTimeout(1500); }
  else log('instagram', item.slug, 'cover input not found; Instagram default cover used');
  await dlgBtn(page, 'Next').click();

  // Caption: title, newline, description.
  const cap = page.locator('div[role=dialog] div[contenteditable=true][role=textbox]').first();
  await cap.click();
  await cap.pressSequentially(item.title, { delay: 5 });
  await page.keyboard.press('Shift+Enter');
  await cap.pressSequentially(item.description, { delay: 5 });
  const got = (await cap.innerText()).trim();
  const want = `${item.title}\n${item.description}`.trim();
  if (got !== want) throw new Error(`caption mismatch: got ${JSON.stringify(got)}`);

  if (dry) { log('instagram', item.slug, 'DRY RUN: stopped before Share'); return { dry: true }; }

  checkPostingRights(item);
  await dlgBtn(page, 'Share').click();
  await page.locator('text=Your reel has been shared').waitFor({ timeout: 600000 });

  // Verify on the grid: a new reel whose alt text starts with the title.
  await page.goto(PROFILE_URL, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
  const after = await newestReels(page);
  const known = new Set(before.map(r => r.href));
  const hit = after.find(r => !known.has(r.href) && r.alt.startsWith(item.title.slice(0, 30)));
  if (!hit) throw new Error('shared, but the reel is not visible on the grid yet');
  return { url: `https://www.instagram.com${hit.href}` };
}
