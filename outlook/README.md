# Outlook → Clipper (classic desktop)

Send emails — primarily their **PDF attachments**, optionally their **bodies** —
from classic desktop Outlook into your local Clipper library with a ribbon
button.

## How it works

The button runs a tiny VBA macro that does only one thing: collect the
`EntryID`s of the selected emails and POST them to the running Clipper service
(`http://127.0.0.1:5577/api/outlook/import`). Clipper then reads those mail
items back over COM (via `pywin32`) and imports them using its normal pipeline:

- **Each PDF attachment → its own clip**, with PDF metadata extraction and the
  PDF's authored *Keywords* applied as tags (deduped by the file's hash, so
  re-sending the same mail won't double-import).
- **Email body (optional) → an HTML clip** carrying any non-PDF attachments;
  inline images are folded in. Set `INCLUDE_BODY = True` in the macro to enable.

All the heavy lifting is server-side in Python — the macro carries no base64 or
HTML logic, which is what keeps it robust.

### Tag review window

By default, after an import the button opens a small **tag-review window** (a
compact Edge app-mode window, not a browser tab) listing the just-imported
clips. Each shows the suggestion-backed tag widget — click the suggested chips
to tag it (the `✕` on a chip bans that keyword from future suggestions), then
**Save & done**. **Skip** defers a clip — it stays in the queue and reappears
the next time the review window opens. The clips are already saved; the window
is only about tags, so closing it loses nothing.

Turn this off in Clipper's **Tags → manage → Outlook import** panel ("Open a
tagging review window after importing emails"). With it off, imports are tagged
silently (PDF keywords only) and the button just shows a confirmation toast. The
setting is server-side; the macro follows whatever the server reports, and a
per-call `review` override can also be sent in the POST body.

> The bridge is **fully isolated**: if `pywin32` is missing, Outlook isn't
> running, or an EntryID is stale, only `/api/outlook/*` is affected — the rest
> of Clipper (clipping, search, suggestions) keeps working, and the import
> endpoint returns a structured per-item error rather than failing.

## Requirements

- **Classic desktop Outlook** (the Win32 app). "New Outlook" / Outlook on the
  web don't expose this COM model and won't work with this approach.
- The Clipper service running locally (`python app.py`).
- `pywin32` available to the Python running Clipper. It ships with Anaconda; if
  needed: `pip install pywin32`.

Check the bridge is live: `GET http://127.0.0.1:5577/api/outlook/status`
→ `{"available": true, ...}`.

## Install the button

1. Outlook → **Alt+F11** to open the VBA editor.
2. **File → Import File…** and choose [`ClipperSendToClipper.bas`](ClipperSendToClipper.bas).
   (Or paste its two `Sub`s into `ThisOutlookSession`.)
3. Optionally edit the top of the module: `INCLUDE_BODY = True` to also clip
   bodies; change `SERVICE_URL` if you run Clipper on a non-default port.
4. Add the button: **File → Options → Customize Ribbon** (or the Quick Access
   Toolbar) → *Choose commands from:* **Macros** → add
   `ClipperSendToClipper.SendToClipper`.

### Macro security (so the button isn't blocked)

Outlook blocks unsigned macros by default. The clean fix is to **self-sign** the
project:

1. Run `SelfCert.exe` (ships with Office; e.g.
   `C:\Program Files\Microsoft Office\root\OfficeXX\SelfCert.exe`) and create a
   certificate, e.g. *"Clipper Macros"*.
2. In the VBA editor: **Tools → Digital Signature… → Choose…** → pick that
   certificate → OK, then save.
3. **File → Options → Trust Center → Trust Center Settings… → Macro Settings** →
   *"Notifications for digitally signed macros, all other macros disabled"*. On
   first run, choose **Trust all documents from this publisher**.

## Use it

Select one or more emails in Outlook, click **Send to Clipper**. A toast reports
how many clips were imported. Open Clipper to see them — PDF clips carry their
keyword tags; body clips (if enabled) record the sender and received date in
their metadata.

## Notes & limits

- The sender's name/address is stored in clip metadata for reference but is
  **deliberately excluded** from the tag-suggestion context (author names make
  poor topical tags).
- Emails have no web URL, so the clip's URL is left blank.
- `.msg` files dragged to disk aren't a supported upload type; use this button
  for emails instead.
- Want unattended capture instead of a button? The same
  `/api/outlook/import` endpoint can be driven by a folder-watcher — ask and it
  can be added alongside the button.
