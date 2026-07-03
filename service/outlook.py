"""
Outlook (classic desktop) integration - optional, Windows-only, fully isolated.

This is the COM-facing half of the Outlook feeder: it reads mail items by
EntryID from a running classic Outlook via pywin32 and returns plain Python
dicts. The app's /api/outlook/* endpoints turn those dicts into clips using the
ordinary ingestion helpers.

ISOLATION CONTRACT (so a broken/absent Outlook never disturbs the rest of the
app):
  * Nothing heavy is imported at module top level - `win32com` is imported
    lazily inside the functions that need it. So `import outlook` is safe on any
    platform, even where pywin32 isn't installed.
  * Every public function is total: it catches ImportError/COM errors and
    returns a structured result ({available:...} / (items, errors)) instead of
    raising. Callers never need a try/except around them, and a failure here
    degrades to "no Outlook items" rather than a 500.

Only classic desktop Outlook exposes this COM object model; "new Outlook"
(Monarch) and Outlook on the web do not, and would need an Office.js add-in
instead.
"""
from __future__ import annotations

import contextlib
import logging
import mimetypes
import os
import re
import tempfile

log = logging.getLogger("clipper.outlook")

# PR_ATTACH_CONTENT_ID - the MIME Content-ID of an inline attachment, used to
# map `cid:` <img> references in the body back to their bytes.
_PR_ATTACH_CONTENT_ID = "http://schemas.microsoft.com/mapi/proptag/0x3712001F"
_CID_IMG_RE = re.compile(r'src\s*=\s*(["\'])cid:([^"\']+)\1', re.IGNORECASE)

# Outlook OlAttachmentType: 1 = olByValue (a real file), 5 = olEmbeddedItem,
# 6 = olOLE. We treat anything carrying a Content-ID as inline regardless.
_OL_BY_VALUE = 1


@contextlib.contextmanager
def _com_apartment():
    """Initialize COM for the current thread for the duration of the block.

    The web server handles each request on a worker thread from its pool, and
    COM must be initialized *per thread* (CoInitialize) before any Dispatch /
    GetActiveObject call. Without this, the first COM call on a fresh worker
    thread raises 'CoInitialize has not been called' (CO_E_NOTINITIALIZED,
    0x800401F0) - the canonical pywin32-in-a-web-app failure. We pair each Init
    with an Uninit so a pooled thread doesn't leak apartment state between
    requests. `pythoncom` is imported lazily to keep `import outlook` safe on
    platforms without pywin32 (see the ISOLATION CONTRACT above)."""
    import pythoncom
    pythoncom.CoInitialize()
    try:
        yield
    finally:
        pythoncom.CoUninitialize()


def _fmt_com_error(e: Exception) -> str:
    """Render a COM/pywin32 exception readably for the macro's MsgBox.

    pywintypes.com_error carries a (hresult, message, excepinfo, argerror)
    tuple, so a bare str(e) dumps that whole tuple. Pull out the human message
    and the hex HRESULT (the greppable part), preferring the richer description
    the COM server may tuck into excepinfo[2]. Falls back to str(e) for
    non-COM exceptions."""
    args = getattr(e, "args", ()) or ()
    hr = args[0] if args and isinstance(args[0], int) else None
    msg = args[1] if len(args) > 1 and isinstance(args[1], str) else str(e)
    excepinfo = args[2] if len(args) > 2 else None
    if isinstance(excepinfo, tuple) and len(excepinfo) > 2 and excepinfo[2]:
        msg = str(excepinfo[2]).strip() or msg
    if hr is not None:
        return f"{msg} (HRESULT 0x{hr & 0xFFFFFFFF:08X})"
    return msg


def available() -> dict:
    """Report whether the Outlook bridge can run here. Never raises.

    {'available': bool, 'reason': str}. `available` is True when pywin32 imports
    (the import endpoint can then Dispatch Outlook on demand); `reason` notes
    whether an Outlook instance is currently running."""
    try:
        import win32com.client  # noqa: F401
    except Exception as e:  # pragma: no cover - platform dependent
        return {"available": False,
                "reason": f"pywin32 not available: {e.__class__.__name__}: {e}"}
    try:
        import win32com.client
        with _com_apartment():
            win32com.client.GetActiveObject("Outlook.Application")
        return {"available": True, "reason": "Outlook is running"}
    except Exception:
        # pywin32 is here but Outlook isn't running right now; Dispatch() will
        # start/attach to it when an import is actually requested.
        return {"available": True, "reason": "pywin32 present; Outlook not currently running"}


def _attachment_bytes(att) -> bytes:
    """Read an attachment's bytes via a temp file (COM has no direct bytes
    accessor for olByValue attachments). Caller guards exceptions."""
    fd, path = tempfile.mkstemp(suffix="_" + (att.FileName or "att"))
    os.close(fd)
    try:
        att.SaveAsFile(path)
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _content_id(att) -> str:
    try:
        cid = att.PropertyAccessor.GetProperty(_PR_ATTACH_CONTENT_ID)
        return (cid or "").strip("<>").strip()
    except Exception:
        return ""


def _guess_mime(filename: str, raw: bytes) -> str:
    if raw[:5] == b"%PDF-" or (filename or "").lower().endswith(".pdf"):
        return "application/pdf"
    mime, _ = mimetypes.guess_type(filename or "")
    return mime or "application/octet-stream"


def _to_data_uri(mime: str, raw: bytes) -> str:
    import base64
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _read_item(item, entry_id: str) -> dict:
    """Map one Outlook MailItem to a plain dict. Each attachment is read
    independently so one unreadable attachment can't sink the whole item; any
    `cid:` inline images that resolve are rewritten into the HTML as data URIs
    so the app's existing data-URI extractor pulls them into assets."""
    subject = (getattr(item, "Subject", "") or "").strip()
    html = getattr(item, "HTMLBody", "") or ""
    if not html:
        # Plain-text-only mail: wrap the body so it renders and indexes.
        from html import escape as _esc
        body = getattr(item, "Body", "") or ""
        html = "<pre>" + _esc(body) + "</pre>" if body else ""

    received_ts, received_iso = None, ""
    try:
        rt = item.ReceivedTime  # pywintypes datetime
        received_ts = int(rt.timestamp())
        received_iso = rt.isoformat()
    except Exception:
        pass

    sender_name = (getattr(item, "SenderName", "") or "").strip()
    sender_email = ""
    try:
        sender_email = (item.SenderEmailAddress or "").strip()
    except Exception:
        pass

    attachments = []
    cid_map: dict[str, str] = {}
    try:
        n = item.Attachments.Count
    except Exception:
        n = 0
    for i in range(1, n + 1):  # COM collections are 1-indexed
        try:
            att = item.Attachments.Item(i)
            filename = att.FileName or f"attachment-{i}"
            raw = _attachment_bytes(att)
            if not raw:
                continue
            mime = _guess_mime(filename, raw)
            cid = _content_id(att)
            is_inline = bool(cid) or getattr(att, "Type", _OL_BY_VALUE) != _OL_BY_VALUE
            if cid and mime.startswith("image/"):
                cid_map[cid] = _to_data_uri(mime, raw)
            attachments.append({"filename": filename, "mime": mime,
                                "bytes": raw, "inline": is_inline})
        except Exception as e:
            log.warning("outlook: skipping unreadable attachment %d on %s: %s",
                        i, entry_id, e)

    if cid_map and html:
        def _swap(m):
            uri = cid_map.get(m.group(2).strip("<>").strip())
            return f"src={m.group(1)}{uri}{m.group(1)}" if uri else m.group(0)
        html = _CID_IMG_RE.sub(_swap, html)

    return {
        "entry_id": entry_id,
        "subject": subject,
        "html": html,
        "received_ts": received_ts,
        "received_iso": received_iso,
        "sender_name": sender_name,
        "sender_email": sender_email,
        "attachments": attachments,
    }


def fetch_items(entry_ids: list[str]) -> tuple[list[dict], list[dict]]:
    """Read the given mail items by EntryID. Returns (items, errors); never
    raises. `errors` is a list of {entry_id, error} for ids that couldn't be
    read (including a single entry with entry_id=None if Outlook itself is
    unreachable), so a partial failure still imports the rest."""
    try:
        import win32com.client
    except Exception as e:
        return [], [{"entry_id": None,
                     "error": f"pywin32 not available: {e.__class__.__name__}: {e}"}]

    # All COM work runs inside a per-thread apartment (see _com_apartment); the
    # Dispatch below would otherwise raise CO_E_NOTINITIALIZED on the server's
    # worker threads. Returning from within the `with` still runs CoUninitialize.
    with _com_apartment():
        try:
            outlook = win32com.client.Dispatch("Outlook.Application")
            ns = outlook.GetNamespace("MAPI")
        except Exception as e:
            return [], [{"entry_id": None,
                         "error": f"cannot reach Outlook: {_fmt_com_error(e)}"}]

        items, errors = [], []
        for eid in entry_ids:
            try:
                item = ns.GetItemFromID(eid)
                items.append(_read_item(item, eid))
            except Exception as e:
                log.warning("outlook: could not read item %s: %s", eid, e)
                errors.append({"entry_id": eid, "error": _fmt_com_error(e)})
        return items, errors
