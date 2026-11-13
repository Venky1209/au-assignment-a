# AI Usage Disclosure

## Tools used
Claude (Claude Code) in the terminal.

## What I used them for
- Reading both assignments and the data generator with me, and listing the ambiguous spots in the
  spec before any code was written.
- Researching the PII approach: blind index (keyed HMAC) for matching phones, AES-GCM for values we
  need back, separate keys, and why a plain SHA-256 of a phone number is unsafe.
- Writing most of the code in `solution/ledger/` and the tests, module by module, after we agreed the
  design.
- Running the pipeline, the large-scale timing, the test suite and a clean-clone check.

## What I wrote or decided myself
- Picked Assignment A over B. I started leaning to B, then switched because ingestion feeding
  dashboard numbers is what I worked on at JettyAI, so it is the work I can defend and extend live.
- Kept it a terminal tool instead of a web UI, since the output is graded by diffing files.
- Insisted that names are encrypted at rest, not just left out of the reports (see below).
- Asked for the PII approach to be checked against outside sources before building on it.
- Reviewed the spec-vs-implementation gaps and pushed to close the "quarantine what can't be linked"
  gap with a visible `ledger quarantine` command (every quarantined event with a reason).
- Asked for `ledger status`, the welcome screen and `ledger shell`, so the database state and the
  commands are visible without reading code.
- Went through and agreed the ambiguity calls listed in `SUBMISSION.md`.

## Where the AI was wrong or unhelpful
- **PII for names:** when we designed PII storage, the AI said no report needs the customer name and
  offered "encrypt it, or don't store it at all" as equal options. I did not accept that: the spec says
  names are PII that "must be protected at rest", and a voice agent needs the name to greet the
  customer, so dropping it is not realistic and storing it plainly is not allowed. We encrypt it
  (AES-GCM) and a test greps the raw DB file to prove no name is stored in plaintext.
- It first pushed toward a full terminal UI and toward Assignment B; both were dropped for time and fit.
- The first version of the "no plaintext PII in the DB" test assumed SQLite's WAL file always exists
  after closing, and failed; it was fixed to check whatever DB files exist.
- The ingest summary table was unreadable in a narrow terminal (every column truncated). I noticed it
  while testing manually and it was reworked.

## How I verified AI-generated code
- **Manual testing:** ran the full flow myself from an empty folder: ingest drop 1, `status`, ingest
  drop 2, `status` again (orphans fall from 1123 to 462 as late leads arrive), re-ingest (every file
  skipped), `report`, `quarantine`, `explain` on specific leads, the shell, and tried to delete from
  the ledger (refused).
- **Checked the edge cases against the spec by hand:** an override that wins over the original
  disposition while the original stays in the ledger; an unknown disposition code (`call_back`) being
  ignored; rejected rows and their reasons; PII columns in the database showing only hashes and
  ciphertext.
- **Cross-checks between outputs:** quarantine reasons add up to `orphan_events` in
  `data_quality.json` (306 + 152 + 4 = 462); the first `lead_report.csv` row and the `0.5625`
  connectivity rate match the examples in the assignment README.
- **Automated (run by the AI and by me):** `pytest solution`, 64 tests, including drop-by-drop ==
  all-at-once == re-ingest compared byte-for-byte on an unseen seed, and a clean-clone run following
  the commands in `SUBMISSION.md`.
