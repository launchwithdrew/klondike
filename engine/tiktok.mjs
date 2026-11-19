// TikTok poster — TikTok Studio web upload. Handle is learned from the signed-in profile.
import { ACCOUNTS, log, setLocalFiles } from './lib.mjs';

const UPLOAD = 'https://www.tiktok.com/tiktokstudio/upload?from=webapp';
const EXPECTED = (ACCOUNTS.tiktok.username || '').replace(/^@/, ''); // the signed-in handle must be exactly this (brand pack)

async function signedInHandle(page) {
  // /profile redirects to the signed-in user's own @handle (Studio pages carry other accounts' ids).
  await page.goto('https://www.tiktok.com/profile', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
  if (/\/login/.test(page.url())) return null;
  const m = page.url().match(/tiktok\.com\/@([A-Za-z0-9._]+)/);
  return m ? m[1] : null;
}

export async function checkTikTok(page) {
  const handle = await signedInHandle(page);
  if (!handle) return { ok: false, why: 'not logged in to TikTok' };
  if (!EXPECTED) return { ok: false, why: 'no TikTok username in brand/brand.json — run clipengine init' };
  if (handle.toLowerCase() !== EXPECTED.toLowerCase()) return { ok: false, why: `logged in as @${handle}, not @${EXPECTED}` };
  return { ok: true, handle };
}

async function latestVideos(page, handle) {
  await page.goto(`https://www.tiktok.com/@${handle}`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(5000);
  return page.$$eval('[data-e2e="user-post-item"]', els => els.map(el => ({
    url: (el.querySelector('a[href*="/video/"]')?.href || '').split('?')[0], alt: el.querySelector('img')?.alt || '' })).filter(v => v.url));
}

export async function postTikTok(page, item, { dry = false } = {}) {
  const chk = await checkTikTok(page);
  if (!chk.ok) throw new Error(`TikTok check failed: ${chk.why}`);
  const beforeList = await latestVideos(page, chk.handle);
  const existing = beforeList.find(v => v.alt.startsWith(item.title.slice(0, 30)));
  if (existing) return { url: existing.url, existing: true };
  const before = new Set(beforeList.map(v => v.url));

  await page.goto(UPLOAD, { waitUntil: 'domcontentloaded' });
  await setLocalFiles(page, page.locator('input[type=file][accept*="video"]').first(), item.file);
  log('tiktok', item.slug, 'file attached');

  // Caption editor (Draft.js): clear the filename TikTok pre-fills, then title, newline, description.
  const cap = page.locator('div[contenteditable=true]').first();
  await cap.waitFor({ timeout: 120000 });
  await page.waitForTimeout(2000);
  // First-time popups and feature tours cover the editor. Dismiss them without changing any account setting.
  for (let i = 0; i < 8; i++) {
    const btn = page.locator('.__floater button, .react-joyride__tooltip button, div[role=dialog] button, .TUXModal button')
      .filter({ hasText: /^(Got it|Cancel|Not now|Close|Skip|Last)$/ }).first();
    const overlay = page.locator('.react-joyride__overlay, div[role=dialog]:visible').first();
    if (await btn.isVisible().catch(() => false)) { await btn.click().catch(() => {}); await page.waitForTimeout(800); continue; }
    if (await overlay.isVisible().catch(() => false)) { await page.keyboard.press('Escape'); await page.waitForTimeout(800); continue; }
    break;
  }

  await cap.click();
  await page.keyboard.press('ControlOrMeta+A');   // Cmd on a Mac, Ctrl elsewhere
  await page.keyboard.press('Backspace');
  await cap.pressSequentially(item.title, { delay: 8 });
  await page.keyboard.press('Shift+Enter');
  // Hashtags open a suggestion popup; type each word, then Space closes it without picking a suggestion.
  for (const word of item.description.split(' ')) {
    await cap.pressSequentially(word, { delay: 8 });
    if (word.startsWith('#')) await page.waitForTimeout(600);
    await page.keyboard.press('Space');
  }
  await page.keyboard.press('Escape').catch(() => {});
  const got = (await cap.innerText()).replace(/\s+/g, ' ').trim();
  const want = `${item.title} ${item.description}`.replace(/\s+/g, ' ').trim();
  if (got !== want) throw new Error(`caption mismatch: got ${JSON.stringify(got)}`);

  // Wait for upload to finish (Post button enabled).
  const postBtn = page.locator('button[data-e2e="post_video_button"], button:has-text("Post")').last();
  const t0 = Date.now();
  while (Date.now() - t0 < 600000) {
    if (await postBtn.isEnabled().catch(() => false) && !(await page.locator('text=/Uploading|Processing/').count())) break;
    await page.waitForTimeout(2000);
  }
  if (dry) { log('tiktok', item.slug, 'DRY RUN: stopped before Post'); return { dry: true }; }

  await postBtn.click();
  const postNow = page.locator('button:has-text("Post now")');
  if (await postNow.isVisible({ timeout: 4000 }).catch(() => false)) await postNow.click();
  await page.waitForURL(/tiktokstudio\/content|manage/, { timeout: 300000 }).catch(() => {});

  // Verify: a new video appears on the public profile.
  for (let i = 0; i < 12; i++) {
    const after = await latestVideos(page, chk.handle);
    const fresh = after.map(v => v.url).find(u => !before.has(u));
    if (fresh) {
      // The video page must actually play: TikTok drops duplicates and failed checks as "unavailable".
      await page.goto(fresh, { waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(4000);
      const body = await page.locator('body').innerText();
      if (/currently unavailable|isn't available|not available/i.test(body)) throw new Error(`TikTok accepted the upload but the video is unavailable (likely a duplicate): ${fresh}`);
      return { url: fresh };
    }
    await page.waitForTimeout(10000);
  }
  throw new Error('posted, but no new video visible on the profile yet');
}
