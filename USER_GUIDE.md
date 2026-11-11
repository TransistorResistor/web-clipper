# Clipper — User Guide

A friendly, plain-English guide to using Clipper. No coding knowledge needed.
(If you're a developer and want the technical details, see [README.md](README.md)
instead.)

---

## What is Clipper?

Clipper is your own private scrapbook for things you read on the web and in PDFs.
When you find a page, article, or document worth keeping, you save ("clip") it
into Clipper with one click. Everything is stored **on your own computer** — there
is no cloud account, nothing is uploaded anywhere, and it all keeps working
without an internet connection.

Once something is clipped you can:

- **Find it again** by typing a few words — Clipper searches the full text, not
  just the title.
- **Tag it** so related things group together, with smart suggestions for which
  tags to use.
- **Add a note** in your own words about why you saved it.
- **Highlight and annotate** passages, just like marking up a printout.
- **Pull quotes out** of many documents at once into a single tidy report.

Think of it as a mix between a bookmark folder, a highlighter, and a search
engine — all kept neatly on your machine.

---

## How it fits together (the 30-second version)

There are two pieces, and they talk to each other on your own computer:

1. **The Clipper service** — a small program that runs quietly in the background
   and holds your library. You start it once and leave it running.
2. **The browser button** (called a "userscript") — a little 📎 paperclip button
   that appears on web pages. Clicking it sends the page to the service.

You also get a **library page** in your browser (your "home base") where you
browse, search, and read everything you've saved.

You don't need to understand the internals — just remember: *the service must be
running for the paperclip button and the library page to work.*

---

## First-time setup

This is the only part that's a bit technical. You do it **once**. After this,
day-to-day use is all clicking. If a tech-savvy friend set this up for you, you
can skip straight to ["Saving things"](#saving-things).

### Step 1 — Install the helper libraries

Clipper needs a few free building blocks (one of them, "Flask", is what runs the
service). Open a terminal/command prompt in the Clipper folder and run:

```
cd service
pip install -r requirements.txt
```

This downloads and installs everything in one go. You only do it once. (If you
see a message about "pip not found", Python isn't installed yet — install Python
first from python.org, then try again.)

### Step 2 — Start the service

In that same terminal, run:

```
python app.py
```

You'll see a line like `Running on http://127.0.0.1:5577`. **That means it
worked.** Leave this window open — closing it stops Clipper. (To start Clipper
again next time, you just repeat this one command.)

> **Tip:** `http://127.0.0.1:5577` is simply "this computer". Nothing is exposed
> to the internet; only you can reach it.

### Step 3 — Install the browser button

The paperclip button comes from a browser add-on called **Tampermonkey** (or
**Violentmonkey**) — both are free.

1. Install Tampermonkey from your browser's add-on/extension store.
2. Open the file `userscript/clipper.user.js` from the Clipper folder.
   Tampermonkey will offer to install it — click **Install**.

That's it. Visit any web page and you should see a small 📎 button.

---

## Saving things

### Saving a web page or article

1. Go to the page you want to keep.
2. Click the **📎 button**.
3. A small panel opens. Here you can (all optional):
   - **Add tags** — type a word and press Enter or comma. Suggested tags appear
     as clickable chips; just click the ones that fit.
   - **Add a note** — a sentence or two in your own words ("the chart on page 3
     is the useful bit", "background reading for the Smith project"). This is
     saved with the clip, helps Clipper suggest better tags, and shows up first
     when you export search results.
4. Click **Save**. A little confirmation appears, and you're done.

Clipper automatically grabs the main article text and strips away menus, ads, and
clutter, so you keep the content and not the noise.

### Saving a PDF you're looking at online

If you're reading an article that **links to a PDF** (for example a summary page
that links to the full report), right-click the PDF link. A small *"📎 Clip this
PDF with article context"* option appears. Click it and Clipper saves **both**
the article and the PDF together as one entry — and the PDF's text becomes
searchable.

### Saving a PDF or saved web page from your computer

Have a file already saved on your computer? Use the **Upload page**:

1. In the library, click the **Upload** link at the top.
2. **Drag a PDF or saved web page (.mht/.mhtml) onto the page**, or click to pick
   one.
3. Clipper reads it and shows you a **preview** — title, date, any tags it found,
   and a snippet — which you can edit before saving.
4. Add tags or a note if you like, then click to confirm.

You can drop **several files at once**; each gets its own preview card and its own
Save button, plus a handy "Save all" button.

> **No duplicates:** If you try to save something you've already got, Clipper
> notices and tells you which existing entry it matches, rather than making a
> copy. You can override and save it anyway if you really want to.

### Ingesting a whole folder of PDFs

Got a folder full of PDFs — subfolders and all? On the Upload page, click
**"📁 Or ingest a whole folder of PDFs"** and pick the top folder. Clipper
finds every PDF underneath it, tells you how many it found, and imports them
in one go with live progress — each one gets the same automatic metadata
extraction and keyword tags a normal upload does.

Before you start you can:

- add **tags of your own** that get applied to every imported PDF (handy for
  marking the batch, e.g. `archive-2024`), and
- tick **"Flag imports for tag review"** to confirm each PDF's tags one at a
  time afterwards on the review page — the same flow email imports use.

Files you've already saved are **skipped automatically** (each is listed with
a link to the existing entry), so you can re-run the same folder after adding
new files and only the new ones are imported.

---

## Your library: browsing and reading

Open your library by going to **http://127.0.0.1:5577/** in your browser
(bookmark it!). You'll see all your clips, newest first.

- **Click any clip** to open it and read the full thing. PDFs display right
  inside the page.
- **Tags** are listed in a sidebar — click one to see everything with that tag.
- Use the **checkboxes** on clips to select several at once, then tag or delete
  them in a batch.

### Inside a clip

When you open a single clip you can:

- **Edit its tags** — add or remove, with suggestions. Click **Save changes**.
- **Edit its note** — the comment box under the tags. It saves with **Save
  changes** too. (As you type, the tag suggestions update to match.)
- **Highlight and annotate** — select any text in the clip, click **Annotate
  selection**, and optionally add a comment. Your highlight stays put even if the
  page text changes slightly later.
- **Delete** the clip if you no longer want it.

---

## Finding things again

There are two ways to search, for two different jobs.

### Quick search — "where did I put that?"

The search box on the **library page** finds clips fast. Type a few words and it
matches across the title, the text, the tags, *and* your notes — with the
matching words highlighted. Great for "I know I saved something about X".

### Advanced search — "pull the relevant bits out of everything"

The **Search** page (linked at the top of the library) is the power tool. It digs
into the **full contents of attached PDFs and documents**, and it can **export a
report**: a single file containing just the relevant passages from every matching
document, with a bit of surrounding context around each hit.

You can be precise about what you want:

- **All of these words:** `solar AND battery`
- **Either word:** `solar OR wind`
- **Exclude a word:** `solar NOT residential`
- **An exact phrase:** `"renewable energy targets"`
- **Words near each other:** `budget %20 forecast` (within 20 words, any order)
- **By tag or date:** `tag:research`, `published:2024-01-01..2024-12-31`

You can mix these together. Then choose **Markdown** or **plain text** and
download the report. If a matched clip has one of your notes, that note appears
at the top of its section (labelled *"User comment:"*) — so your own framing leads
the extracted material.

---

## Smart tag suggestions (how they work, in plain terms)

When you add tags, Clipper proposes ones that are likely to fit. It does this
entirely on your computer — nothing is sent anywhere. It looks at:

- **What similar saved items were tagged** — "documents like this one usually got
  tag X".
- **Words that actually appear in the document** — if your library has a tag
  whose name shows up in the text, it's suggested.
- **Tags you often use together** — pick one, and its usual companions surface.
- **Where it came from** — tags you've used on other pages from the same website.

Each suggestion shows a tiny symbol hinting at *why* it was suggested. You're
never forced to use them — they're just one-click shortcuts. The more you tag,
the better the suggestions get.

> Your **note** matters here too: a one-line note describing what something is
> about steers the suggestions toward the angle you care about, because Clipper
> treats your note much like the document's title.

---

## Bringing in your old OneNote notes (optional)

If you have notebooks in **OneNote**, Clipper can import them so they're
searchable alongside everything else. The short version: in OneNote, export your
sections as Word documents (**File → Export → Word Document**), then a helper
script imports them. Folder names become tags, and images and attachments come
along. Re-running the import is safe — it skips anything already imported. See the
"OneNote migration" section of [README.md](README.md) for the exact commands, or
ask whoever set up Clipper to run it for you.

---

## Frequently asked questions

**Is any of this on the internet? Is my data private?**
Everything stays on your own computer. Clipper only listens to *this machine*
(that's what the `127.0.0.1` address means), there's no account, and nothing is
uploaded. Clips work offline.

**The 📎 button isn't showing up / saving fails.**
Almost always this means the service isn't running. Make sure the terminal window
from Step 2 is still open and showing `Running on http://127.0.0.1:5577`. If you
closed it, just run `python app.py` in the `service` folder again.

**A few websites have no 📎 button.**
That's on purpose. Clipper hides the button on sensitive pages — login screens,
payment/checkout pages, account settings — so you don't accidentally save private
forms.

**Where is everything actually stored?**
In a single file called `clipper.db` in the Clipper folder. That one file *is*
your whole library — back it up like any important document, and you've backed up
everything.

**Can I undo an edit?**
Edits to a clip's title or main text keep the previous version automatically, so
nothing is lost when you change something. Deleting a clip, however, is permanent.

**Do I lose my tags or notes if I edit them?**
No. Tags and notes save independently and don't create extra clutter. Edit them
as often as you like.

---

## The short cheat sheet

| I want to… | Do this |
|---|---|
| Start Clipper | Open a terminal in `service`, run `python app.py`, leave it open |
| Open my library | Go to `http://127.0.0.1:5577/` |
| Save a page | Click the 📎 button, add tags/note, Save |
| Save a PDF from a link | Right-click the link → "Clip this PDF with article context" |
| Save a file from my computer | Library → Upload → drag the file in |
| Import a folder of PDFs (recursive) | Library → Upload → 📁 folder button |
| Find a clip fast | Type in the library search box |
| Pull quotes from many docs | Library → Search → run a query → download report |
| Edit tags/notes/highlights | Open the clip, change it, click Save changes |

---

*Happy clipping! For the technical reference — schema, API, configuration,
search syntax, and how the tag ranker is tuned — see [README.md](README.md).*


Metadata editing and presence filters
-------------------------------------
Open a clip and expand Edit metadata to change its title, original URL,
marking, publication date or report number. Save metadata applies the edits.
The userscript capture window also lets you set title, original URL, report
number, publication date and marking before saving a page or PDF. Its post-save
window lets you edit the same fields with Save metadata. Saved values refresh
every three seconds while open; unsaved fields are preserved. Reinstall/update
the userscript (version 0.14.0) to use these controls. Native browser PDF viewers
may hide userscript windows; use the web UI to edit those clips.

In the library, choose Has field or Missing field and enter tags or a metadata
key (for example report_number, date_published, marking). This combines with
text, tag, PDF, marking and date filters. Advanced search accepts has:tags,
missing:report_number, has:marking AND NOT tag:draft, and arbitrary metadata
keys. Blank values count as absent. Use NOT tag:draft to exclude a specific tag.
PDF upload corrections apply only to that PDF and are never remembered for
later PDFs. Existing corrections and stored metadata remain in the database;
PDF ingestion ignores remembered corrections. No database format change is required.


Custom marking rules
--------------------
Edit `service/marking_rules.py`, function `normalize_marking_text(value)`.
It currently returns the input unchanged. Add your own text transformations
there; the result is then interpreted by the existing parser and
`service/markings.json` vocabulary. The hook applies to ingestion, metadata
edits from either UI and marking queries. Original markings remain preserved.
Restart the service after changing rules. Existing records are not rewritten;
re-save their metadata to recompute stored marking/filter columns.
No database format changes are required.
