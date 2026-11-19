// YouTube Studio Shorts uploader — the exact flow verified by hand on 2026-11-18.
import { ACCOUNTS, log, setLocalFiles } from './lib.mjs';

// The channel id is optional in the brand pack: Studio redirects to the signed-in channel, and the
// handle on the page must match the pack's.
let CHANNEL = ACCOUNTS.youtube.channelId;
let STUDIO = CHANNEL ? `https://studio.youtube.com/channel/${CHANNEL}` : 'https://studio.youtube.com';

export async function checkYouTube(page) {
  await page.goto(STUDIO, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(3000);
  const url = page.url();
  if (url.includes('accounts.google.com')) return { ok: false, why: 'not signed in to Google' };
  const m = url.match(/studio\.youtube\.com\/channel\/(UC[\w-]+)/);
  if (!m) return { ok: false, why: `not a YouTube Studio channel page (${url})` };
  if (CHANNEL && m[1] !== CHANNEL) return { ok: false, why: `wrong channel (${m[1]}, brand pack says ${CHANNEL})` };
  const handle = ACCOUNTS.youtube.handle;
  const seen = handle ? await page.locator(`text=${handle}`).count().catch(() => 0) : 0;
  if (!CHANNEL) {
    if (!seen) return { ok: false, why: `signed in, but ${handle || 'no handle in brand/brand.json'} not shown — wrong channel?` };
    CHANNEL = m[1]; STUDIO = `https://studio.youtube.com/channel/${CHANNEL}`;
  }
  return { ok: true, channelId: CHANNEL, handleSeen: seen > 0 };
}

async function setText(page, box, text) {
  await box.click();
  await page.keyboard.press('ControlOrMeta+A');   // Cmd on a Mac, Ctrl elsewhere
  await page.keyboard.press('Backspace');
  await box.pressSequentially(text, { delay: 5 });
  const got = (await box.innerText()).trim();
  if (got !== text.trim()) throw new Error(`text mismatch: wanted ${JSON.stringify(text)} got ${JSON.stringify(got)}`);
}

export async function postYouTube(page, item, { dry = false } = {}) {
  const chk = await checkYouTube(page);
  if (!chk.ok) throw new Error(`YouTube check failed: ${chk.why}`);
  // Never re-upload: if a Short with this title already exists on the channel, record that one.
  await page.goto(`${STUDIO}/videos/short`, { waitUntil: 'domcontentloaded' });
  await page.locator('ytcp-video-row').first().waitFor({ timeout: 30000 }).catch(() => {});
  const rows = await page.$$eval('ytcp-video-row', rs => rs.map(r => ({ title: r.querySelector('#video-title')?.innerText || '',
    id: ((([...r.querySelectorAll('a')].map(a => a.href).find(h => /video\/[^/]+\/edit/.test(h)) || '').match(/video\/([^/]+)/)) || [])[1] })));
  const dup = rows.find(r => r.title.startsWith(item.title.slice(0, 30)) && r.id);
  if (dup) return { url: `https://youtube.com/shorts/${dup.id}`, existing: true };
  await page.goto(STUDIO, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2000);

  await page.locator('#upload-icon, ytcp-button#create-icon').first().click();
  const menuUpload = page.locator('tp-yt-paper-item#text-item-0, #text-item-0');
  if (await menuUpload.isVisible().catch(() => false)) await menuUpload.click();
  const dialog = page.locator('ytcp-uploads-dialog');
  await setLocalFiles(page, dialog.locator('input[type=file]'), item.file);
  log('youtube', item.slug, 'file attached');

  const boxes = dialog.locator('#textbox');
  await boxes.first().waitFor({ timeout: 60000 });
  await page.waitForTimeout(1500);
  await setText(page, boxes.nth(0), item.title);
  await setText(page, boxes.nth(1), item.description);

  const notKids = dialog.locator('tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]');
  await notKids.scrollIntoViewIfNeeded();
  await notKids.click();
  if ((await notKids.getAttribute('aria-checked')) !== 'true') throw new Error('could not set "not made for kids"');

  for (let i = 0; i < 3; i++) { await dialog.locator('#next-button').click(); await page.waitForTimeout(1200); }
  const pub = dialog.locator('tp-yt-paper-radio-button[name="PUBLIC"]');
  await pub.click();
  if ((await pub.getAttribute('aria-checked')) !== 'true') throw new Error('could not select Public');

  // Wait for upload + checks (up to 10 min).
  const progress = dialog.locator('ytcp-video-upload-progress');
  const t0 = Date.now();
  while (Date.now() - t0 < 600000) {
    const s = (await progress.innerText().catch(() => '')) || '';
    if (/Checks complete|Upload complete|Processing/i.test(s) && !/Uploading/i.test(s)) break;
    await page.waitForTimeout(2000);
  }
  const status = await progress.innerText().catch(() => '');
  log('youtube', item.slug, 'status:', status.replace(/\s+/g, ' '));
  if (/copyright|issues found(?!.*no issues)/i.test(status) && !/No issues found/i.test(status)) throw new Error(`YouTube checks flagged: ${status}`);

  if (dry) { log('youtube', item.slug, 'DRY RUN: stopped before Publish (video stays a private draft)'); return { dry: true }; }

  await dialog.locator('#done-button').click();
  const share = page.locator('ytcp-video-share-dialog, tp-yt-paper-dialog:has-text("Video published")');
  await share.first().waitFor({ timeout: 120000 });
  const href = await share.first().locator('a[href*="youtube.com/shorts"], a[href*="youtu.be"]').first().getAttribute('href');
  const url = href.replace(/\?.*$/, '');
  await page.keyboard.press('Escape').catch(() => {});

  // Verify: the Shorts list shows it Public.
  await page.goto(`${STUDIO}/videos/short`, { waitUntil: 'domcontentloaded' });
  const row = page.locator('ytcp-video-row', { hasText: item.title.slice(0, 40) }).first();
  await row.waitFor({ timeout: 60000 });
  const rowText = await row.innerText();
  if (!/Public/.test(rowText)) throw new Error(`published but row not Public: ${rowText.replace(/\s+/g, ' ')}`);
  return { url };
}
