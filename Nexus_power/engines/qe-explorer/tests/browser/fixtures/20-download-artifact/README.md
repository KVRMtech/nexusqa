# Fixture 20 — download artifact (M1.5 / T-ND-03)

## Purpose

Prove that a download-triggering action leaves a **real file on disk**, recorded
as an auditable artifact — not a log line saying a download started.

The distinction matters because the two are trivially confused. "A download
event fired" is a claim about the browser; "a 685-byte file whose first five
bytes are `%PDF-` exists at `artifacts/001_sales-packet.pdf`" is evidence. The
milestone asks for the second, so this fixture ships a genuinely valid PDF
(hand-built with a real xref table and `%%EOF`, not a text file with a `.pdf`
suffix) and the test reads the captured bytes back.

Three download shapes, because they fail differently:

1. **`Download Sales Packet`** — a server-side file behind `<a download>`. The
   href is visible to capture, so this is the easy case.
2. **`Export Policy Schedule`** — a client-generated `Blob` handed to a synthetic
   anchor. There is no href and no file on the server. Capture sees a plain
   `<button>` and *cannot tell* a download is about to happen. This is how every
   "Export to CSV" in an enterprise application works, and it is the case that
   makes a browser-level download listener non-optional.
3. **`Export Audit Log`** — the same mechanism with a **hostile suggested
   filename** (`../../../etc/audit-log.txt`). A download's suggested filename is
   application-controlled text; used unreduced as a path component it writes
   outside the evidence directory.

`Claims` is an ordinary link, present so the fixture also shows a navigation is
still a navigation.

## Expected controls

Four captured controls: two links (`Download Sales Packet` with an absolute href
ending `/sales-packet.pdf`, and `Claims`) and two buttons with empty hrefs
(`Export Policy Schedule`, `Export Audit Log`). The two buttons carrying no href
is the point, not an omission — it is the evidence that capture alone cannot
classify a download.

## Expected manifest

The capture golden (`golden/manifest_20-download-artifact.json`) is recorded
under the standard characterization crawl (`max_states=1`, `observe_only=True`),
so it holds the inventory of this page and nothing else.

The artifact expectations are asserted by
`tests/browser/test_page_lifecycle_execution.py`, which drives the production
port, clicks each control, and then checks on disk:

* the file exists under the crawl directory's `artifacts/` subdirectory;
* it is non-empty, and the PDF's bytes start with `%PDF-`;
* the emitted `browser_event` carries `event="download"` plus `filename`,
  `bytes`, `content_type`, `page_url`, `artifact_path` and `trigger_label`;
* the hostile name landed inside `artifacts/` with its separators reduced.

## Targeted defect

`BUG-M15-DOWNLOAD-NO-ARTIFACT`. The context was created with no stated
`accept_downloads` and no `page.on("download")` listener existed anywhere, so a
click that downloaded a file produced no artifact, no filename, no content type
and no record at all. The crawl could not distinguish "this button exports the
policy schedule" from "this button does nothing".

## Running this fixture alone

```bash
cd Nexus_power/engines/qe-explorer
python -m pytest tests/browser -k 20-download-artifact
python -m pytest tests/browser/test_page_lifecycle_execution.py -k download
```
