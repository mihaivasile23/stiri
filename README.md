# Știri

A personal headlines page: <https://stiri.social-data.ro>

Every 15 minutes GitHub fetches the news feeds listed in `settings.yml`,
sorts them into lanes, and republishes the site. Nothing runs on your own
computer, and it costs nothing.

## What's in here

| File | What it does | Do you edit it? |
|---|---|---|
| `settings.yml` | Pages, lanes, sources and search terms | **Yes, this is the control panel** |
| `build.py` | Fetches feeds and builds the site | No |
| `web/` | The page itself (layout, colours, app icon) | Only to change the look |
| `.github/workflows/update.yml` | The 15-minute schedule | No |

## Editing the lanes

1. Open `settings.yml` on GitHub and click the pencil icon (top right of the file).
2. Make your change. The top of the file explains every option with examples.
3. Click **Commit changes**. The site updates within about two minutes.

Common edits:

- **Add a source:** copy an existing `name / feed / site` block, paste it under the lane, change the values. If you don't know the feed address, leave out the `feed` line and keep only `site`.
- **Remove a source:** delete its lines.
- **New lane:** copy a whole lane block, from `- name:` down to its last source.
- **Tune a search:** edit the text in quotes under `searches:`.

Finding a site's feed address: try adding `/feed` or `/rss` to the site's
address, or look for an RSS icon in the site's footer. WordPress sites
almost always use `/feed`.

If you break the file (usually indentation), the site keeps showing the
last good version, and GitHub emails you that the run failed. Open the
**Actions** tab to see the error, or undo your change from the file's
**History**.

## Checking sources

At the bottom of the site, **Starea surselor** lists any source whose feed
isn't working. Those still appear through the Google News backup; fix the
`feed` address when you have time.

## Good to know

- The 15-minute schedule is best-effort: GitHub sometimes runs it a few minutes late.
- "Read" headlines are remembered per device and per browser.
- The site shows a warning at the top if headlines haven't updated for more than two hours. That usually means the Actions tab has a failed run.
