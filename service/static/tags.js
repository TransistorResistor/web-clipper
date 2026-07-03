/* Shared tag-input widget for the Clipper viewer pages.
 *
 * One implementation of: chip-style tag entry, autocomplete over existing
 * tags (prefix matches first, then by usage), and debounced suggestion
 * chips from /api/tags/suggest. Used by detail.html and upload.html so the
 * two stop drifting apart. (The userscript carries its own copy of the
 * same structure - it can't load same-origin scripts from arbitrary pages.)
 *
 * Usage:
 *   const widget = ClipperTags.create(containerEl, {
 *     initial: ['a', 'b'],
 *     context: { title, text, host, excludeClipId,      // suggestion context
 *                getNote },   // optional () => string, fed like the title
 *     onChange: tags => {...},                          // optional
 *   });
 *   widget.getTags()  -> ['a', 'b', ...]
 *   widget.setTags([...])
 */
window.ClipperTags = (function () {
  'use strict';

  const CSS = `
  .ct-suggest { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 6px; align-items: center; }
  .ct-suggest .ct-label { font-size: 11px; color: #888; }
  .ct-chip { font-size: 12px; padding: 3px 9px; border-radius: 12px; background: #eef;
             color: #335; border: 1px solid #ccd; cursor: pointer; user-select: none; }
  .ct-chip:hover { background: #dde; }
  .ct-chip.ct-both { background: #e7f6ec; border-color: #b6e0c4; color: #1b5e2a; }
  .ct-chip .ct-why { color: #88a; font-size: 10px; margin-left: 4px; }
  .ct-chip .ct-ban { color: #b66; font-weight: 600; font-size: 11px; margin-left: 5px; }
  .ct-chip .ct-ban:hover { color: #c0392b; }
  .ct-box { display: flex; flex-wrap: wrap; gap: 4px; padding: 5px 6px; border: 1px solid #ccc;
            border-radius: 4px; background: white; cursor: text; align-items: center; }
  .ct-box:focus-within { border-color: #06c; }
  .ct-tag { display: inline-flex; align-items: center; gap: 4px; font-size: 12px;
            background: #e8f0fe; color: #1a4480; border: 1px solid #c4d7f5;
            border-radius: 10px; padding: 2px 4px 2px 9px; }
  .ct-tag .ct-x { cursor: pointer; color: #6b8cba; font-weight: 600; padding: 0 4px;
                  border-radius: 8px; }
  .ct-tag .ct-x:hover { background: #c4d7f5; color: #1a4480; }
  .ct-in { flex: 1; min-width: 90px; border: none; outline: none; font: inherit;
           font-size: 13px; padding: 3px 2px; background: transparent; }
  .ct-wrap { position: relative; }
  .ct-ac { position: absolute; left: 0; right: 0; top: 100%; z-index: 30; background: white;
           border: 1px solid #ccc; border-top: none; border-radius: 0 0 4px 4px;
           max-height: 180px; overflow-y: auto; display: none;
           box-shadow: 0 4px 10px rgba(0,0,0,0.08); }
  .ct-ac.ct-open { display: block; }
  .ct-ac-item { padding: 6px 10px; font-size: 13px; cursor: pointer; display: flex;
                justify-content: space-between; }
  .ct-ac-item.ct-active, .ct-ac-item:hover { background: #eef6ff; }
  .ct-ac-item .ct-n { color: #999; font-size: 11px; }
  `;

  let cssInjected = false;
  function injectCss() {
    if (cssInjected) return;
    const s = document.createElement('style');
    s.textContent = CSS;
    document.head.appendChild(s);
    cssInjected = true;
  }

  // why -> chip marker. 'both' = more than one signal agreed; 'keyword' =
  // a proposed new tag extracted from the document.
  const WHY_MARK = { 'similar': '≈', 'predicted': '◎', 'co-occurring': '↔',
                     'mention': '¶', 'both': '★',
                     'keyword': '＋', 'code': '#' };

  let _allTagsPromise = null;
  function fetchAllTags() {
    if (!_allTagsPromise) {
      _allTagsPromise = fetch('/api/tags')
        .then(r => r.json())
        .then(j => (j.tags || []).map(t => ({ name: t.name, n: t.n })))
        .catch(() => []);
    }
    return _allTagsPromise;
  }
  // Call after committing uploads etc. so new tags show up in autocomplete.
  function invalidateTagCache() { _allTagsPromise = null; }

  async function fetchSuggestions(payload) {
    try {
      const r = await fetch('/api/tags/suggest', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!r.ok) return [];
      return (await r.json()).suggestions || [];
    } catch (_) { return []; }
  }

  // Ban a keyword so the suggester stops proposing it (and its case/punctuation
  // variants). Fire-and-forget; callers drop the chip locally on click.
  async function blockKeyword(keyword) {
    try {
      await fetch('/api/suggest/blocklist', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ keyword }),
      });
    } catch (_) { /* service unreachable: nothing to do */ }
  }

  // Prefix matches first, then usage count, then name.
  function rankMatches(all, frag, exclude) {
    const f = frag.toLowerCase();
    return all
      .filter(t => t.name.toLowerCase().includes(f) && !exclude.has(t.name.toLowerCase()))
      .sort((a, b) => {
        const ap = a.name.toLowerCase().startsWith(f) ? 0 : 1;
        const bp = b.name.toLowerCase().startsWith(f) ? 0 : 1;
        return ap - bp || b.n - a.n || a.name.localeCompare(b.name);
      })
      .slice(0, 8);
  }

  function create(container, opts) {
    injectCss();
    opts = opts || {};
    const ctx = opts.context || {};
    container.innerHTML = '';

    const suggestRow = document.createElement('div');
    suggestRow.className = 'ct-suggest';
    const wrap = document.createElement('div');
    wrap.className = 'ct-wrap';
    const box = document.createElement('div');
    box.className = 'ct-box';
    const input = document.createElement('input');
    input.className = 'ct-in';
    input.autocomplete = 'off';
    input.placeholder = opts.placeholder || 'add tag…';
    const ac = document.createElement('div');
    ac.className = 'ct-ac';
    box.appendChild(input);
    wrap.append(box, ac);
    container.append(suggestRow, wrap);

    let tags = [];                 // current chips, in order
    let allTags = [];
    let activeIdx = -1;
    let lastSuggestKey = null;
    let suggestTimer = null;

    const lcSet = () => new Set(tags.map(t => t.toLowerCase()));

    function notifyChange() { if (opts.onChange) opts.onChange(getTags()); }

    function renderChips() {
      box.querySelectorAll('.ct-tag').forEach(n => n.remove());
      for (const t of tags) {
        const chip = document.createElement('span');
        chip.className = 'ct-tag';
        const label = document.createElement('span');
        label.textContent = t;
        const x = document.createElement('span');
        x.className = 'ct-x';
        x.textContent = '×';
        x.title = 'Remove ' + t;
        x.addEventListener('click', e => { e.stopPropagation(); removeTag(t); });
        chip.append(label, x);
        box.insertBefore(chip, input);
      }
    }

    function addTag(name) {
      name = (name || '').trim().replace(/,+$/, '').trim();
      if (!name) return false;
      if (lcSet().has(name.toLowerCase())) return false;
      tags.push(name);
      renderChips(); notifyChange(); scheduleSuggest();
      return true;
    }
    function removeTag(name) {
      tags = tags.filter(t => t !== name);
      renderChips(); notifyChange(); scheduleSuggest();
      input.focus();
    }
    function commitInput() {
      // Commas allow pasting "a, b, c" in one go.
      const parts = input.value.split(',');
      let added = false;
      for (const p of parts) added = addTag(p) || added;
      input.value = '';
      closeAc();
      return added;
    }

    // ---- autocomplete ----
    function closeAc() { ac.classList.remove('ct-open'); ac.innerHTML = ''; activeIdx = -1; }
    function renderAc(items) {
      if (!items.length) { closeAc(); return; }
      ac.innerHTML = '';
      items.forEach((t, i) => {
        const row = document.createElement('div');
        row.className = 'ct-ac-item' + (i === activeIdx ? ' ct-active' : '');
        row.dataset.name = t.name;
        const name = document.createElement('span');
        name.textContent = t.name;
        const n = document.createElement('span');
        n.className = 'ct-n';
        n.textContent = String(t.n);
        row.append(name, n);
        row.addEventListener('mousedown', e => {   // mousedown beats blur
          e.preventDefault();
          addTag(t.name); input.value = ''; closeAc(); input.focus();
        });
        ac.appendChild(row);
      });
      ac.classList.add('ct-open');
    }
    function updateAc() {
      // Re-resolve the module cache: if another widget invalidated it
      // (e.g. a committed upload created new tags), pick the fresh list
      // up for the next keystroke.
      fetchAllTags().then(a => { allTags = a; });
      const frag = input.value.trim();
      if (!frag) { closeAc(); return; }
      activeIdx = -1;
      renderAc(rankMatches(allTags, frag, lcSet()));
    }
    function acceptActive() {
      const items = ac.querySelectorAll('.ct-ac-item');
      const idx = activeIdx >= 0 ? activeIdx : 0;
      if (!items.length || idx >= items.length) return false;
      addTag(items[idx].dataset.name);
      input.value = ''; closeAc();
      return true;
    }

    input.addEventListener('input', updateAc);
    input.addEventListener('keydown', e => {
      const items = ac.querySelectorAll('.ct-ac-item');
      const open = ac.classList.contains('ct-open') && items.length;
      if (e.key === 'ArrowDown' && open) {
        e.preventDefault(); activeIdx = (activeIdx + 1) % items.length;
        items.forEach((it, i) => it.classList.toggle('ct-active', i === activeIdx));
      } else if (e.key === 'ArrowUp' && open) {
        e.preventDefault(); activeIdx = (activeIdx - 1 + items.length) % items.length;
        items.forEach((it, i) => it.classList.toggle('ct-active', i === activeIdx));
      } else if (e.key === 'Tab' && open && input.value.trim()) {
        e.preventDefault(); acceptActive();
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (open && activeIdx >= 0) acceptActive();
        else commitInput();
      } else if (e.key === ',') {
        e.preventDefault(); commitInput();
      } else if (e.key === 'Escape') {
        closeAc();
      } else if (e.key === 'Backspace' && !input.value && tags.length) {
        removeTag(tags[tags.length - 1]);
      }
    });
    // Synchronous: blur runs before a click on Save/Ingest is handled, so
    // half-typed text becomes a chip instead of being silently dropped.
    // (Autocomplete rows preventDefault on mousedown, so they never blur.)
    input.addEventListener('blur', commitInput);
    box.addEventListener('click', () => input.focus());

    // ---- suggestions (debounced; refetch only when the key changes) ----
    const ctxNote = () => (ctx.getNote ? ctx.getNote() : (ctx.note || ''));
    function suggestKey() {
      return JSON.stringify([tags.map(t => t.toLowerCase()).sort(),
                             ctx.excludeClipId || null, ctx.host || '',
                             (ctx.title || '').slice(0, 80),
                             ctxNote().slice(0, 200)]);
    }
    function scheduleSuggest() {
      clearTimeout(suggestTimer);
      suggestTimer = setTimeout(refreshSuggestions, 300);
    }
    async function refreshSuggestions() {
      const key = suggestKey();
      if (key === lastSuggestKey) return;
      lastSuggestKey = key;
      const sug = await fetchSuggestions({
        text: ctx.text || '', title: ctx.title || '', host: ctx.host || '',
        note: ctxNote(),
        chosen: getTags(), exclude_clip_id: ctx.excludeClipId, limit: 12,
      });
      const chosen = lcSet();
      const filtered = sug.filter(s => !chosen.has(s.tag.toLowerCase()));
      suggestRow.innerHTML = '';
      if (!filtered.length) return;
      const label = document.createElement('span');
      label.className = 'ct-label';
      label.textContent = 'Suggested:';
      suggestRow.appendChild(label);
      for (const s of filtered) {
        const chip = document.createElement('span');
        chip.className = 'ct-chip' + (s.why === 'both' ? ' ct-both' : '');
        chip.textContent = s.tag + ' ';
        const why = document.createElement('span');
        why.className = 'ct-why';
        why.textContent = WHY_MARK[s.why] || '≈';
        why.title = s.why;
        chip.appendChild(why);
        // ✕ bans the keyword (server-side) and drops the chip; clicking the
        // chip body still adds the tag as before.
        const ban = document.createElement('span');
        ban.className = 'ct-ban';
        ban.textContent = '✕';
        ban.title = 'Never suggest “' + s.tag + '” again';
        ban.addEventListener('click', e => {
          e.stopPropagation();
          blockKeyword(s.tag);
          chip.remove();
        });
        chip.appendChild(ban);
        chip.addEventListener('click', () => { addTag(s.tag); input.focus(); });
        suggestRow.appendChild(chip);
      }
    }

    function getTags() { return tags.slice(); }
    function setTags(list) {
      tags = [];
      const seen = new Set();
      for (const raw of (list || [])) {
        const t = (raw || '').trim();
        if (t && !seen.has(t.toLowerCase())) { seen.add(t.toLowerCase()); tags.push(t); }
      }
      renderChips(); scheduleSuggest();
    }

    setTags(opts.initial || []);
    fetchAllTags().then(a => { allTags = a; });
    refreshSuggestions();   // initial suggestions from page context

    return { getTags, setTags, focus: () => input.focus(),
             refreshSuggestions: scheduleSuggest };
  }

  // Standalone existing-tag autocomplete, attachable to any plain <input>
  // (e.g. the library page's merge-target and bulk add/remove fields). Reuses
  // rankMatches + the .ct-ac styles; a fixed-position dropdown anchored to the
  // input's rect avoids restructuring the host page's layout. `opts.multi`
  // completes the current comma-separated token instead of the whole value.
  // Returns { close, isOpen }.
  function attachAutocomplete(input, opts) {
    injectCss();
    opts = opts || {};
    const multi = !!opts.multi;
    let allTags = [];
    let activeIdx = -1;
    fetchAllTags().then(a => { allTags = a; });

    const ac = document.createElement('div');
    ac.className = 'ct-ac';
    ac.style.position = 'fixed';
    document.body.appendChild(ac);

    const token = () => multi
      ? input.value.slice(input.value.lastIndexOf(',') + 1).trim()
      : input.value.trim();
    const others = () => multi
      ? new Set(input.value.split(',').slice(0, -1)
          .map(s => s.trim().toLowerCase()).filter(Boolean))
      : new Set();
    function fill(name) {
      if (!multi) { input.value = name; return; }
      const i = input.value.lastIndexOf(',');
      input.value = (i >= 0 ? input.value.slice(0, i + 1) + ' ' : '') + name + ', ';
    }
    const isOpen = () => ac.classList.contains('ct-open');
    function close() { ac.classList.remove('ct-open'); ac.innerHTML = ''; activeIdx = -1; }
    function place() {
      const r = input.getBoundingClientRect();
      ac.style.left = r.left + 'px';
      ac.style.top = r.bottom + 'px';
      ac.style.width = r.width + 'px';
    }
    function accept(name) { fill(name); close(); input.focus(); }
    function render(items) {
      if (!items.length) { close(); return; }
      ac.innerHTML = '';
      items.forEach((t, i) => {
        const row = document.createElement('div');
        row.className = 'ct-ac-item' + (i === activeIdx ? ' ct-active' : '');
        row.dataset.name = t.name;
        const nm = document.createElement('span'); nm.textContent = t.name;
        const n = document.createElement('span'); n.className = 'ct-n'; n.textContent = String(t.n);
        row.append(nm, n);
        row.addEventListener('mousedown', e => { e.preventDefault(); accept(t.name); });
        ac.appendChild(row);
      });
      place();
      ac.classList.add('ct-open');
    }
    function update() {
      fetchAllTags().then(a => { allTags = a; });   // pick up cache invalidations
      const frag = token();
      if (!frag) { close(); return; }
      activeIdx = -1;
      render(rankMatches(allTags, frag, others()));
    }

    input.addEventListener('input', update);
    input.addEventListener('keydown', e => {
      const items = ac.querySelectorAll('.ct-ac-item');
      const open = isOpen() && items.length;
      if (!open) return;
      if (e.key === 'ArrowDown') {
        e.preventDefault(); activeIdx = (activeIdx + 1) % items.length;
        items.forEach((it, i) => it.classList.toggle('ct-active', i === activeIdx));
      } else if (e.key === 'ArrowUp') {
        e.preventDefault(); activeIdx = (activeIdx - 1 + items.length) % items.length;
        items.forEach((it, i) => it.classList.toggle('ct-active', i === activeIdx));
      } else if (e.key === 'Tab') {
        e.preventDefault(); accept(items[activeIdx >= 0 ? activeIdx : 0].dataset.name);
      } else if (e.key === 'Enter' && activeIdx >= 0) {
        // Accept the highlighted tag and suppress the host's Enter handler
        // (e.g. merge-on-Enter). This listener is registered first, so
        // stopImmediatePropagation keeps the host's from firing.
        e.preventDefault(); e.stopImmediatePropagation();
        accept(items[activeIdx].dataset.name);
      } else if (e.key === 'Enter') {
        close();   // nothing highlighted: let Enter fall through to the host
      } else if (e.key === 'Escape') {
        e.preventDefault(); close();
      }
    });
    input.addEventListener('blur', () => setTimeout(close, 120));
    window.addEventListener('scroll', () => { if (isOpen()) close(); }, true);

    return { close, isOpen };
  }

  return { create, invalidateTagCache, attachAutocomplete };
})();
