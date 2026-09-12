# Songsterr internals (reverse-engineered 2026-08-29)

Discovered by reading the production JS bundles. **Everything here is private/undocumented
and can change without notice** — that is why Tabsmith drives the real UI with Playwright
instead of replaying these endpoints directly.

## Routes
- `/new`  — "Transcribe tabs instantly with AI" page (`/ai` 302-redirects here)

## Stable DOM ids (survive deploys; CSS classes are hashed and DO NOT)
| id | what |
|---|---|
| `yt-link-input` | YouTube link field (`name=ytLink`) |
| `first-bar-tempo-input` | pin tempo (name=`firstBarTempo`) |
| `first-bar-numerator-input` / `first-bar-denominator-input` | pin time signature |
| `anacrusis-numerator-input` / `anacrusis-denominator-input` | pickup bar |
| `download_modal` | download format dialog |
| `control-export-gp` | **Download as Guitar Pro (all tracks)** |
| `control-export-midi` / `-mp3` / `-wav` / `-mscz` | other formats |
| `control-more`, `control-export` | toolbar buttons that open the dialog |

Button text `upload an audio file` reveals the file input (it is created on demand —
there is no `input[type=file]` in the DOM until you click it).

## API (observed in `transcriptions/transcribeTab:start`)
```
POST /api/song/transcribe-file-song
  { uploadTicket, artist, title, maxDuration,
    instruments,            // JSON
    firstBarTempo,          // <-- pin BPM
    firstBarSignature,      // JSON, <-- pin time signature
    anacrusisSignature,     // JSON
    startTime, endTime,     // <-- transcribe a RANGE of one upload
    tripletFeel, fingerstyle }

POST /api/song/transcribe-song            # YouTube variant (videoId)
POST /api/song/transcription/process      # { transcriptionId, revisionId? }
GET  /api/contributions/available-transcriptions-count   # remaining quota
GET  /api/meta/{songId}/{revisionId}
```
The file itself is uploaded first via an internal helper
(`purpose: "transcription-audio"`) that returns an `uploadTicket`.

## Gating (from DownloadPopup)
GP export is locked unless: Plus subscriber, OR a legacy account created before a
cutoff date, OR per-song bonus purchase, OR `revision.isAllowDownload`.
`controls/download` early-returns unless `user.isLoggedIn` — hence "must be signed in".

## Quota
Songsterr Plus = **50 AI transcriptions/month**, and one upload can be a whole song.
Each 20-second chunk spends one transcription.

## Free-tier facts (measured 2026-08-29, signed in)

`GET /api/contributions/available-transcriptions-count` on a free account:

```json
{"availableTranscriptionsCount": 0, "limit": 0, "maxDuration": 20}
```

- `maxDuration: 20` — the 20-second cap on **file uploads**. This is the reason
  for chunking.
- `limit: 0` / `availableTranscriptionsCount: 0` — no AI transcriptions
  available. Submitting anyway does **not** error visibly: the POST is rejected,
  the page stays on `/new`, and the header flips to "UPGRADE TO PLUS".
  Poll this endpoint *before* uploading.
- A whole-song transcription via **YouTube link** is not bound by `maxDuration`;
  a 4.6-minute song came back as a single 149-bar score.

## Guitar Pro export on a free account: WORKS

`#control-export-gp` downloaded successfully with no Plus subscription.
**But the file is `.gp` (GP 8.1.3)** — a zip of `Content/score.gpif`, NOT `.gp5`.
pyguitarpro cannot read or merge it.

GPIF is id-pooled and deduped: `<Bars>`, `<Voices>`, `<Beats>`, `<Notes>`,
`<Rhythms>` are separate pools referenced by id, and identical beats/notes are
shared. `<MasterBar><Bars>0 149 298 447</Bars>` lists one bar id per track, in
track order. Merging therefore needs every pool id remapped by an offset per
file, plus per-track bar padding — not a concatenation.
