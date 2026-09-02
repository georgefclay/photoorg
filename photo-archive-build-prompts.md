# Family Photo Archive — Build Prompts

Each numbered section is a self-contained prompt. Paste **Section 0 (Shared Context)** at the top of every one, then the prompt body. Build in the order given — later prompts assume earlier ones exist.

---

## 0. Shared Context (paste into every prompt)

```
PROJECT: A private family photo archive for ~5,000 images — a mix of phone/camera
files with EXIF and scanned prints with no metadata. Roughly 3,000 are scans.
Most scans are 300 PPI.

THREE TIERS:
1. Windows desktop app (Python + PySide6) — ingest, dedupe review, scan cleanup,
   face review, sync. This is where all the human review work happens.
2. Mac mini M6, 32GB, on the LAN — runs a local inference HTTP service
   (vision model + face embeddings). The Windows app calls it over the LAN.
3. Web app (Node.js + Express + Postgres, React frontend) — private, family and
   friends only. Viewing, commenting, tagging, suggesting dates, downloads.
   Exposes a REST API that the desktop app is a client of.

INVIOLABLE RULE: originals are never modified, moved, or deleted by any tool.
Masters live in a read-only archive directory, offline, never on the web server.
Every tool reads masters and writes derived working copies. The entire working
set must be regenerable from the masters at any time.

OTHER GROUND RULES:
- Deletes are never real deletes. Move to quarantine, or soft-delete with a flag.
- The database is the source of truth for organization. Albums are virtual.
  All working files sit in one flat directory on disk — no folder hierarchy.
- A known fact and a guess are different things. AI output and family
  suggestions go into a suggestions queue; only an admin promotes them to
  confirmed values.
- Every state change is written to an audit log table with the previous value,
  so any edit can be traced and undone. Audit history lives in the database
  only — never in image metadata.
```

---

## 1. Database Schema

```
Build the Postgres schema and migrations for the archive described above.

TABLES:

photos — id, master_path, working_path, sha256, phash, width, height, mime,
  is_scan (bool), has_no_people (bool), capture_date (date, nullable),
  capture_date_precision (enum: exact/month/year/decade/unknown),
  capture_date_confirmed (bool), completeness_score (int 0-100),
  synced_at, created_at, updated_at

photo_backs — id, photo_id FK, working_path, transcribed_text,
  transcription_confidence. A scanned reverse side attaches to its front photo;
  it is not a photo row of its own and is excluded from downloads.

people — id, given_name, middle_name, surname, maiden_name, nickname,
  birth_year (nullable), death_year (nullable), notes

person_name_variants — id, person_id FK, variant, kind (nickname/misspelling/
  alternate_spelling). Populated both from a seeded nickname list and by hand.

faces — id, photo_id FK, person_id FK nullable, bbox (jsonb), embedding
  (vector or float[]), confidence, source (ai/human), is_disputed (bool),
  disputed_by, created_by

relationships — id, person_a_id, person_b_id, type (parent/spouse/sibling),
  confirmed (bool), created_by. Derive grandparent/cousin/etc. by traversal.

places — id, name, latitude nullable, longitude nullable, notes

photo_places — photo_id, place_id

albums — id, name, description, created_by
album_photos — album_id, photo_id  (a photo may be in many albums)

comments — id, photo_id, user_id, body, is_hidden (bool), created_at

suggestions — id, photo_id, user_id, kind (date/person/place/relationship),
  payload (jsonb), status (pending/accepted/rejected), resolved_by,
  resolved_at, source (human/ai)

likes — user_id, photo_id, created_at  (unique on the pair)

users — id, email, display_name, role (admin/contributor),
  status (active/suspended), created_at, last_login_at

access_requests — id, email, message, status (pending/approved/denied),
  token, token_expires_at, decided_by, decided_at

audit_log — id, user_id, action, entity_type, entity_id, previous_value
  (jsonb), new_value (jsonb), created_at

REQUIREMENTS:
- Migrations, not a single dump. Use node-pg-migrate or Knex.
- Indexes for: phash lookups, full-text search across comments and AI
  descriptions, person name variants, capture_date ranges.
- A view or function computing completeness_score from whether the photo has a
  confirmed date, at least one non-disputed face (or has_no_people set), and a
  place.
- Seed script for the nickname variant table using a standard English nickname
  list (Richard/Rick/Dick/Richie, Margaret/Peggy/Maggie, etc.).
```

---

## 2. Mac Mini Inference Service

```
Build a local HTTP inference service in Python (FastAPI) to run on a Mac mini
M6 with 32GB unified memory. It is called over the LAN by the Windows desktop
app. No authentication beyond a shared token in a header — it is LAN-only.

Use MLX for Apple Silicon acceleration where possible. Model choice should be
configurable, defaulting to Qwen3-VL 8B for vision work.

ENDPOINTS — each is a separate job with its own prompt, so any one can be
re-run independently when a model is upgraded:

POST /transcribe-back
  Input: image of the reverse of a print.
  Returns: transcribed handwritten text, plus any date or names it can parse
  out of it, plus a confidence score. This is the highest-value job — run it
  first and treat its output as strong evidence.

POST /describe
  Input: photo.
  Returns: a short factual description ("two children on a porch with a dog")
  for full-text indexing. Do not speculate about identities.

POST /estimate-date
  Input: photo.
  Returns: an estimated year range, a confidence score, and the visual
  reasoning (clothing, hairstyles, car models, film grain, print border style,
  paper finish). Always a RANGE, never a single year. This is explicitly a
  low-confidence signal that a human confirms.

POST /detect-faces
  Input: photo.
  Returns: bounding boxes plus an embedding vector per face. Use a dedicated
  face embedding model, not the VLM.

POST /match-faces
  Input: an embedding plus a set of labelled reference embeddings.
  Returns: ranked candidate matches with distances.
  IMPORTANT: the caller will exclude disputed tags from the reference set;
  the service must not assume all references are trustworthy.

ALSO:
- Batch endpoints that accept a list and stream results, since 3,000 photos
  will run overnight.
- A /health endpoint reporting loaded model and free memory.
- Structured JSON logging of every request with timing, so throughput can be
  measured.
- Expect 5-15 seconds per image; design for a long-running batch that can be
  interrupted and resumed.
```

---

## 3. Desktop App — Shell and Ingest

```
Build the skeleton of a Windows desktop application in Python + PySide6. It is
a single app with several modes, selectable from a sidebar: Ingest, Dedupe,
Cleanup, Faces, Sync. This prompt covers the shell and the Ingest mode only —
leave the other modes as empty placeholder panels.

CONFIG: masters directory (read-only), working directory, quarantine directory,
manual-fix directory, inference service URL and token, web API URL and token.
Store in a config file, editable in a Settings screen.

INGEST MODE:
- Walk the masters directory. For each file, compute sha256, copy to the
  working directory under a generated stable filename, and insert a photos row.
  Never write to, move, or rename anything under masters. Open masters
  read-only and fail loudly if the directory is writable-and-not-marked-safe.
- Extract EXIF from the working copy: capture date, dimensions, GPS,
  camera make/model. When a real EXIF timestamp exists, set capture_date with
  precision 'exact' and capture_date_confirmed = true.
- Flag scans (no EXIF capture date) as is_scan.
- Handle video files too — phones shoot clips and they should not fall through.
  Store them as photos rows with the appropriate mime type, skip vision jobs.
- Compute a perceptual hash for every image and store it.

FRONT/BACK PAIRING:
Scanned prints were scanned front then back, consecutively. Auto-pair by scan
order: odd = front, even = back, within a scan batch. Create a photo_backs row
for each back and do NOT create a photos row for it. Present the auto-paired
set in a review grid so mis-pairings can be corrected by hand before commit.

Ingest must be resumable — re-running skips files already ingested by sha256.
Progress bar, running count, and a log pane.
```

---

## 4. Desktop App — Dedupe Review

```
Add the Dedupe mode to the desktop app.

DETECTION:
Scanned duplicates of the same print differ in dust, skew, and color, so exact
hashing will not catch them. Use perceptual hashing (implement pHash and dHash,
make the algorithm and Hamming distance threshold configurable) to find
candidate pairs and groups. Also catch the case where the same moment exists as
both a phone file and a scan of the printed copy — visually near-identical,
completely different files.

REVIEW UI:
- Side-by-side comparison at full resolution with synchronized zoom and pan.
- Show for each: filename, dimensions, file size, whether it has EXIF, capture
  date if known, is_scan flag.
- PRE-SELECT the likely keeper rather than making the user decide cold:
  prefer the file with real EXIF (the digital original beats a scan of a print
  of it), then higher resolution, then larger file size. The user confirms or
  overrides with one click.
- Keyboard-driven: arrow keys to move, a key to accept the suggestion, a key
  to skip, a key to mark "not duplicates" so the pair is never shown again.
- Handle groups of 3+, not just pairs.

DELETION:
"Delete" moves the working copy to the quarantine directory and sets a
soft-delete flag on the photos row. It never unlinks a file and never touches
the master. A quarantine browser lets anything be restored.

Track review progress so a session of several thousand pairs can be paused and
resumed.
```

---

## 5. Desktop App — Scan Cleanup

```
Add the Cleanup mode to the desktop app. Automatic batch processing with a
human review step — no manual sliders as the primary interface, since there are
thousands of images.

GEOMETRIC:
- Deskew against the print edge.
- Auto-crop to the print boundary, removing scanner bed background.
- Detect and split flatbed scans containing multiple prints into separate
  images, each becoming its own photos row.

TONAL:
- Color cast removal for prints that have shifted orange or magenta with age.
- Contrast and level correction.
- Fading correction.

REVIEW QUEUE:
Before and after, side by side, at full resolution. Three actions:
- Accept — the cleaned version becomes the working copy.
- Reject — moves to the manual-fix directory as a separate queue to work
  through later, so it does not block the main flow.
- Send to remote enhance — see below.

Cleanup always writes a NEW derived file. It never overwrites the previous
working copy in place, and never touches the master.

PLUGGABLE REMOTE ENHANCE:
Define a provider interface (submit image, poll or await, return enhanced
image) with the provider selected by config, not code. Ship a Claid.ai
implementation and a null/stub implementation. Claid is credit-based
pay-as-you-go, roughly $0.03 per operation at volume, and is tuned for
e-commerce product photos rather than family portraits — so build in a way that
lets a different provider be swapped in later without touching calling code.
Include a cost counter showing spend for the session.
```

---

## 6. Desktop App — Face Review and Inference Client

```
Add the Faces mode to the desktop app, plus the client library for the Mac mini
inference service.

INFERENCE CLIENT:
An abstract interface with a local LAN implementation pointing at the Mac mini,
mirroring the pluggable pattern used for remote enhance. Retries, timeouts, and
graceful handling of the service being down. Batch jobs run in a background
thread with progress reporting and must be resumable — an overnight run of
3,000 images that dies at image 1,800 resumes at 1,800.

BATCH RUNNER:
Separate, independently re-runnable jobs: transcribe-backs, describe,
estimate-date, detect-faces. Each records per-photo status so any single job
can be re-run for the whole archive when a model is upgraded, without
reprocessing the others.

FACE REVIEW UI:
- Cluster unlabelled faces by embedding similarity and present clusters for
  bulk labelling: "these 40 faces look like the same person — who is it?"
- Assign a cluster to an existing person or create a new one.
- When building reference embeddings for matching, EXCLUDE any face tagged as
  disputed. One wrong identification must not quietly poison every future match
  for that person.
- Show the AI's suggested match and confidence, but require a human confirm.

AI OUTPUT HANDLING:
Everything the models produce — dates, descriptions, back transcriptions, face
matches — is written as a suggestion with source = 'ai', never directly as a
confirmed value. Handwritten dates read off the back of a print should be
flagged as high-confidence suggestions, since they are far more reliable than
visual dating.
```

---

## 7. Web API — Auth

```
Build the authentication system for the Express API.

There is no open signup. The flow is:

1. REQUEST ACCESS: A public page with a short form — email address and an
   optional message. Submitting it creates an access_requests row with status
   pending and sends a notification to the admin only. No email is ever sent to
   the requester at this stage. This is the critical property: a bot filling
   the form cannot cause mail to be sent to any third party, so the sending
   domain's reputation cannot be burned by spam blasted at real people.

2. ADMIN DECISION: The notification email to the admin contains Approve and
   Deny buttons. Each is a signed, single-use token link.
   - The link MUST land on a confirmation page requiring a click, not act on
     GET. Mail clients pre-fetch links and would otherwise auto-approve
     everything.
   - Tokens expire after 72 hours.
   - Decisions also work from an admin screen listing pending requests, so
     nothing depends on finding the email.

3. APPROVAL creates a users row with role contributor and status active, and
   sends the new user their first magic link.

4. MAGIC LINKS thereafter: single-use, short-expiry, sent only to addresses
   already in the users table. Session cookie after redemption, HTTP-only,
   with a long-lived session since this is a low-security family archive and
   convenience matters.

ROLES: admin can promote suggestions, confirm dates, resolve disputes, hide
comments, and manage users. Contributor can view, comment, like, tag faces,
dispute tags, and suggest dates, people, places, and relationships.

SUSPENSION: setting a user to suspended immediately invalidates their sessions
and stops magic links working. Their comments, tags, and contributions REMAIN —
they are part of the archive's history now.

A separate service-account token authenticates the desktop app against the API.
It is not a human user and does not appear in user lists.
```

---

## 8. Web API — Core Resources

```
Build the REST API for the archive. Assume the schema and auth from previous
prompts.

PHOTOS: list with filtering and pagination, get one with all related data
(faces, people, comments, place, likes, completeness), update (admin only for
confirmed fields).

SUGGESTIONS: any contributor can create one for a date, person, place, or
relationship. Admin lists pending, accepts, or rejects. Accepting writes the
value to the canonical field, marks it confirmed, and writes an audit_log entry
with the previous value.

Never let a suggestion overwrite a confirmed fact directly. A photo has exactly
one authoritative date; disagreement about what it should be lives in the
comment thread, not in competing date fields.

FACES: create a tag, dispute someone else's tag (sets is_disputed, records who,
and queues it for admin), admin resolves.

PEOPLE: CRUD, including maiden name and nickname. Contributors can create
people and add relationships, but relationships go through the suggestion queue
— family trees get genuinely contested and need a single arbiter.

COMMENTS: create, list, admin hide (soft, never hard delete).

LIKES: toggle. Expose a most-liked view.

ALBUMS: virtual only. Adding a photo to an album never moves a file.

AUDIT LOG: written on every state change, with the previous value so any edit
can be undone. Admin-only endpoint to browse it.

REPORTING: a monthly activity summary endpoint — who logged in, who tagged, who
commented, who confirmed dates, counts per person per month. Built entirely
from the audit log.

SYNC ENDPOINTS for the desktop app (service token):
- Push photos, embeddings, descriptions, transcriptions, and AI suggestions.
- Pull confirmed labels, dates, people, and places for metadata writing.
- Resumable: a synced flag per photo means a run that dies at 1,800 of 2,000
  picks up where it left off rather than starting over. Sync is triggered
  manually by a button in the desktop app, never on a schedule.
```

---

## 9. Search

```
Build search for the API and frontend. Search is a first-class feature, not an
afterthought — with 5,000 photos in one flat pool it is the primary way anyone
finds anything, especially before dates are confirmed.

MATCH ACROSS:
- Person names — given, middle, surname, AND maiden name. A search for either
  the married or the maiden surname must return the same person. Relatives will
  search by whichever name they knew her by.
- Nicknames, via the person_name_variants table. "Rick" finds Richard. "Peggy"
  finds Margaret — which is exactly why this needs a lookup table and not fuzzy
  string matching, since Peggy and Margaret are not textually similar at all.
- Misspellings, via phonetic matching (Soundex or Metaphone, the genealogy
  standard) so Schmidt matches Schmitt and Katherine matches Kathryn.
- Full text across comments, AI-generated descriptions, and transcribed text
  from the backs of prints. This lets people find "the porch photo" even when
  nobody ever tagged it.
- Date ranges, tolerant of imprecision — a photo known only to the decade
  should still surface in a 1960s search.
- Places.

Combine the layers so a search for "Katherine" returns Kathryn, Cathy, Kate,
and Katherine, ranked by match strength, without returning nonsense.

Expose filters for: has no confirmed date, has untagged faces, low completeness
score — so contributors can be pointed at the photos that most need attention.
```

---

## 10. Web Frontend

```
Build the React frontend for the family archive. The audience is relatives of
mixed technical ability, many on phones. Prioritize obviousness over density.

VIEWS:
- Browse: one big grid to start with, since the archive begins as a flat
  undated pool. Sort by recently added, most liked, or least complete.
- Photo detail: large image, people tagged, date with a visible indicator of
  whether it is confirmed or a guess, place, comment thread, like button,
  completeness indicator, and the scanned back with its transcription if one
  exists.
- Search, exposing the full matching described in the search prompt.
- Person page: everything that person appears in, their names including maiden
  name, and their relationships.
- Albums (virtual).
- Admin: pending suggestions, disputed tags, access requests, monthly activity
  report.

CONTRIBUTION UX — the point of the whole site:
- Tagging a face should be two taps. Draw or accept a box, type a name with
  autocomplete against existing people.
- Suggesting a date should be one field that accepts "1962", "March 1962", or
  "sometime in the 60s".
- The like button is the lowest-friction entry point — someone who would never
  write a comment will happily tap a heart. Make it prominent.
- Surface a "photos needing attention" feed driven by completeness score, so
  contributors always have an obvious next thing to do.
- A photo with many likes and no date is a high-priority target — weight the
  feed accordingly.
```

---

## 11. Download and Export

```
Build the bulk download system. Any user with access can download any number of
photos, up to the entire archive. The principle is that everyone holds a copy,
so the archive survives the server dying.

RESOLUTION: full only. No size options. Scans are 300 PPI and the whole set is
roughly 50-100GB zipped, so the entire-archive download must be a background
job that emails a link when ready, never a synchronous request.

FOLDER STRUCTURE IN THE ZIP:
The archive is one flat blob on disk, but the download must contain a real
folder tree, rendered at zip time from the database:
- Year/Month for photos with a known month.
- Year only, at the top of that year's folder, when the month is unknown —
  never invent a January.
- A catch-all folder ("Unsorted") for photos with no date at all.
- Optionally a parallel People/ tree, where a photo appears under each person
  tagged in it. Duplication inside the zip is fine.

EXCLUDE scanned backs. The value is in the transcription, not in an image of
someone's biro.

METADATA: every file in the zip has confirmed values already embedded — see the
metadata writer prompt.

Also build an admin full-export producing images plus a database dump in an
open format, so the entire archive can leave this system intact. Family
archives outlive the software they are built in.
```

---

## 12. Metadata Writer

```
Build the component that writes confirmed data into image files. It runs in the
desktop app after a sync pull.

WRITES ONLY TO WORKING COPIES. Never opens a master for writing. This is
non-negotiable — assert it in code, not just in comments.

WRITE:
- Capture date to EXIF DateTimeOriginal, only when confirmed. Never write a
  guess into a date field.
- People into IPTC/XMP. Put the person's FULL set of known names (given,
  middle, surname, maiden) in the description field, and just the common name
  in the keywords field — some viewers render a long multi-name string badly.
- Place name and, where known, GPS coordinates.
- The AI description into the image description field, clearly marked as
  auto-generated.
- Transcribed text from the back, if any.

DO NOT WRITE audit history, contributor identities, comment threads, likes, or
suggestion status into the files. Metadata holds the settled truth about the
photo. The record of how that truth was arrived at stays in the database.

Write to a temp file and atomically replace, so a crash mid-write cannot
corrupt a working copy. Since the entire working set is regenerable from
masters, a corrupted working copy is recoverable — but do not rely on that.

Include a dry-run mode that reports what would be written per file.
```

---

## Build Order

1 → 2 → 3 → 4 → 7 → 8 → 5 → 6 → 9 → 10 → 11 → 12

Schema first, then the inference service so it can be tested standalone, then
ingest and dedupe to get a clean working set. Auth and core API next so sync has
somewhere to push. Cleanup and faces after that, then search, frontend,
downloads, and finally the metadata writer once there are confirmed values worth
writing.
