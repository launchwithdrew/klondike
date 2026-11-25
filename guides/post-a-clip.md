# Post a clip

**When:** `next` says "post …", or `clipengine to-post` lists a clip.

1. `clipengine to-post` (or `--json`) shows each due clip: the file, the exact title or caption for each platform,
   and the account to be signed in as.
2. **Claim it before uploading:** `clipengine claim --slug SLUG --platform PLATFORM`. This puts the attempt on
   record and gives you a 30-minute lease.
3. Check the browser is signed in as the named account. Upload the file with exactly that text.
4. Record the live link: `clipengine posted --slug SLUG --platform PLATFORM --url https://…`.
5. If the upload failed, run `clipengine attempt --slug SLUG --platform PLATFORM --failed "why"`. If you changed
   your mind, run `--release`.

If a lease runs out with no outcome, the post is `uncertain`. Check the account first, then record `posted` or
`--failed`. Never upload it again blind.

Only the platforms the clip lists, and only your owner's accounts.
