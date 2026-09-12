# Tabsmith

Split a song into fixed-length parts, run each part through **Songsterr's AI
transcription**, download the Guitar Pro files, and merge them back into one score.

Requires an existing, signed-in Songsterr account. Tabsmith never sees your
password — you sign in yourself, once, in a real browser window.

```bash
~/Tabsmith/tabsmith.sh login                       # once, by hand
~/Tabsmith/tabsmith.sh run "~/Music/Hum.mp3"       # split -> transcribe -> merge
```

Handles mp3, m4a, wav, opus, flac, aac, ogg, aiff — anything ffmpeg can decode.

## Read this before your first real run

Songsterr Plus includes **50 AI transcriptions per month, and one upload can be a
whole song.** Each 20-second chunk spends one of those 50. So a 4-minute song
costs **12 transcriptions chunked, or 1 whole**:

```bash
~/Tabsmith/tabsmith.sh run song.mp3            # 20s chunks, as designed
~/Tabsmith/tabsmith.sh run song.mp3 --whole    # one upload, one credit
```

Chunking also costs quality. Each chunk is transcribed with no knowledge of its
neighbours, so each gets its own tempo and key guess, and notes sustained across
a boundary are cut in half. Pinning the grid helps a great deal:

```bash
~/Tabsmith/tabsmith.sh run song.mp3 --tempo 92 --time-signature 4/4
```

Chunking is worth it when a song is long, when a whole-song upload fails, or when
you want to re-do one rough section without re-transcribing everything.

## Commands

| | |
|---|---|
| `login` | Sign in once; the session is reused afterwards |
| `quota` | Transcriptions left this month |
| `split FILE` | Only split the audio — no upload, no account needed |
| `run FILE` | The whole pipeline |
| `merge DIR\|FILES` | Only merge Guitar Pro files |

Useful `run` flags: `--whole`, `--seconds 30`, `--tempo`, `--time-signature`,
`--format wav`, `--min-tail 5` (fold a stubby last part into the one before it,
so you don't spend a credit on 1.4s of audio), `--keep-going`, `-y`, `--headless`.

## Output

```
Hum [tabsmith]/
  parts/     Hum - Murtaza Qizilbash Part 1.mp3 …   + manifest.json
  tabs/      Hum - Murtaza Qizilbash Part 1.gp5 …
  state.json
  Hum - Murtaza Qizilbash.gp5     <- the merged score
```

**Runs resume.** Every finished part is recorded in `state.json`; re-running the
same command picks up where it stopped and never pays twice for a part you
already have. If a part fails, fix it and re-run — only the missing parts upload.

Ordering comes from `manifest.json`, not filename sort — `Part 10` sorts before
`Part 2` alphabetically, and the naming you asked for has no zero padding.

## How the merge works

Each chunk arrives as its own little song. Merging means:

1. concatenating the song-level measure headers and re-deriving every bar number
   and absolute tick position (a bar is `numerator × 3840/denominator` ticks,
   and bar 1 starts at 960, not 0);
2. matching tracks *across* chunks by tuning, percussion flag and name, padding
   with full-bar rests wherever a chunk lacks a track another chunk has — so a
   bass that drops out for one chunk stays bar-aligned instead of sliding early;
3. writing each chunk's detected tempo in as a mix-table change on its first
   beat, so a chunk heard at 96 BPM still plays at 96 in the merged file
   (`--flatten-tempo` turns this off).

## Verified vs. not

Tested here, end to end:

- exact 20s non-overlapping cuts (65s source → 20+20+20+5, no drift or overlap)
- naming: `Hum - Murtaza Qizilbash Part 1`
- part ordering past 9 parts (1→12, not 1,10,11,12,2,…)
- merging across differing tempos, differing track sets, and a mid-song 4/4→3/4
  change, with contiguous ticks and correct bar numbering
- browser launch, signed-out detection, and every Songsterr selector below

**Not verified: the signed-in upload and download itself.** That needs your
account, and I was not going to spend your transcriptions to test it. The first
real run is the real test — start with one short file.

## If Songsterr changes their page

Tabsmith drives the real UI, keyed on element **ids**, which are stable;
Songsterr's CSS class names are content-hashed and change every deploy, so
nothing here depends on them. The private API is documented in
`docs/songsterr-notes.md` but deliberately not called directly.

Selectors relied on: `#control-export-gp` (Guitar Pro download), `#download_modal`,
`#control-more` / `#control-export`, `#first-bar-tempo-input`,
`#first-bar-numerator-input`, `#first-bar-denominator-input`, and the text
`upload an audio file` / `Transcribe tab with AI`. Note the submit control is an
`<a>` carrying `role="dialog"`, so role-based lookups miss it — `_clickable()` in
`tabsmith/songsterr.py` works by visible text across any tag.

If a run fails with "could not find …", that file is where to look.

## Troubleshooting

**"A Tabsmith browser window is already open (pid N)"** — Chromium allows one
process per profile. Close that window, or `kill N`. Locks left behind by a
crashed run are detected as stale and cleared automatically.

**Sign-in never completes.** Fixed in v1.0.1. The login poll used to re-navigate
the page every couple of seconds, which threw you off the form mid-typing. The
check now uses `GET /api/preference` (401 signed out, 200 signed in) through
`page.request`, which reuses cookies without touching the page.

Do not probe `/api/user/current` for this — it answers 200 to anyone, reading
"current" as a profile name and returning a stub person called "Current".

**Starting over.** `rm -rf ~/.tabsmith/browser-profile` signs out completely.

## Known limits

- **GP7/GP8 (`.gp`) files cannot be merged.** The merger handles GP3/GP4/GP5.
  `.gp` is a zip of GPIF XML with id-indexed note pools that need full remapping.
  If Songsterr hands back `.gp`, Tabsmith says so plainly and leaves the
  individual tabs in `tabs/` rather than producing a wrong file.
- Guitar Pro export is gated on most accounts — Plus, a legacy account, or a
  per-song unlock.
- Merged tabs are a **draft**. Chunk boundaries are where the seams will show.

## Layout

```
tabsmith/naming.py     title/artist detection, "Part N" naming
tabsmith/split.py      ffmpeg chunking + manifest
tabsmith/songsterr.py  Playwright automation
tabsmith/merge.py      Guitar Pro merging
tabsmith/cli.py        commands
docs/songsterr-notes.md
```

Browser profile: `~/.tabsmith/browser-profile`. Delete it to sign out.
