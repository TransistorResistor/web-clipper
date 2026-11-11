// ==UserScript==
// @name         Local Clipper
// @namespace    https://local.tim.clipper
// @version      0.14.0
// @description  Clip web pages and PDFs into a local SQLite-backed knowledge base.
// @match        *://*/*
// @match        file:///*.pdf
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_registerMenuCommand
// @grant        GM_openInTab
// @grant        GM_notification
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

(function () {
    'use strict';

    const SERVICE = 'http://127.0.0.1:5577';
    const LS_LAST_TAGS = 'clipper:lastTags';

    // ===================================================================
    // BLOCKLIST
    // ===================================================================
    // Pages matching any block rule get no clipper button and can't be
    // clipped (the menu command and panel refuse too). This keeps login
    // screens, paywalls, account pages, checkout flows, etc. out of the
    // knowledge base.
    //
    // A rule is an object of CONDITIONS. ALL conditions in a rule must hold
    // for that rule to fire (logical AND within a rule). The page is blocked
    // if ANY rule fires (logical OR across rules).
    //
    // Supported condition keys:
    //   text:        string | string[] | RegExp - matched against the page's
    //                HTML (lowercased for plain strings). An array means every
    //                entry must be present (AND).
    //   anyText:     string[] - at least ONE entry must be present (OR).
    //   selector:    CSS selector | string[] - every selector must match at
    //                least one element. Covers id (#user-login), class
    //                (.login-form), tag (form), attribute ([name="password"]),
    //                or any compound CSS.
    //   anySelector: string[] - at least ONE selector must match.
    //   urlIncludes: string | string[] - every substring must be in the URL.
    //   urlMatches:  RegExp - tested against location.href.
    //   host:        string | RegExp - exact host string or regex over hostname.
    //   title:       string | RegExp - matched against document.title.
    //   not:         a nested condition-object that must NOT hold (negation).
    //   label:       optional human name, shown in the console when it fires.
    //
    // Examples are included below - edit freely.

    const BLOCK_RULES = [
        {
            label: 'Login page (text + form id)',
            // Fires only if BOTH the phrase appears AND a login form exists.
            text: 'login page',
            anySelector: ['form#user-login', '#user-login', 'form#login'],
        },
        {
            label: 'Dedicated auth page',
            // A password field alone is NOT enough - many news sites and
            // forums keep a sign-in box in the header of every article, and
            // matching on that blocked perfectly clippable pages. Require
            // the page itself to be about signing in (title says so) in
            // addition to the password field. Auth-ish URLs are covered by
            // the separate URL rule below.
            selector: ['input[type="password"]'],
            title: /sign.?in|log.?in|log.?on|authenticate|create.{0,3}account|password reset/i,
        },
        {
            label: 'URL looks like auth/account',
            urlMatches: /\/(login|signin|sign-in|logout|account|password|auth)(\/|\?|$)/i,
        },
        {
            label: 'Checkout / payment',
            anySelector: ['form#checkout', '[name="cardnumber"]', '#payment-form',
                          'input[autocomplete="cc-number"]'],
        },
        {
            label: 'Example: block a specific host entirely',
            host: 'mail.google.com',
        },
    ];

    function condText(needle) {
        const html = (document.body ? document.body.innerHTML : document.documentElement.innerHTML);
        const hay = html.toLowerCase();
        if (needle instanceof RegExp) return needle.test(html);
        const list = Array.isArray(needle) ? needle : [needle];
        return list.every(s => hay.includes(String(s).toLowerCase()));
    }
    function condAnyText(list) {
        if (!list || !list.length) return true; // vacuously satisfied
        const hay = (document.body ? document.body.innerHTML : '').toLowerCase();
        return list.some(s => hay.includes(String(s).toLowerCase()));
    }
    function condSelector(sel) {
        const list = Array.isArray(sel) ? sel : [sel];
        return list.every(s => { try { return !!document.querySelector(s); } catch (_) { return false; } });
    }
    function condAnySelector(list) {
        if (!list || !list.length) return true;
        return list.some(s => { try { return !!document.querySelector(s); } catch (_) { return false; } });
    }
    function condUrlIncludes(val) {
        const list = Array.isArray(val) ? val : [val];
        const url = location.href.toLowerCase();
        return list.every(s => url.includes(String(s).toLowerCase()));
    }
    function condHost(val) {
        if (val instanceof RegExp) return val.test(location.hostname);
        return location.hostname === val;
    }
    function condTitle(val) {
        if (val instanceof RegExp) return val.test(document.title);
        return document.title.toLowerCase().includes(String(val).toLowerCase());
    }

    // Evaluate one condition-object: every present key must be satisfied.
    function ruleHolds(rule) {
        if ('text' in rule && !condText(rule.text)) return false;
        if ('anyText' in rule && !condAnyText(rule.anyText)) return false;
        if ('selector' in rule && !condSelector(rule.selector)) return false;
        if ('anySelector' in rule && !condAnySelector(rule.anySelector)) return false;
        if ('urlIncludes' in rule && !condUrlIncludes(rule.urlIncludes)) return false;
        if ('urlMatches' in rule && !(rule.urlMatches instanceof RegExp && rule.urlMatches.test(location.href))) return false;
        if ('host' in rule && !condHost(rule.host)) return false;
        if ('title' in rule && !condTitle(rule.title)) return false;
        if ('not' in rule && ruleHolds(rule.not)) return false; // negation
        // A rule with no recognized condition keys never fires (avoids
        // accidentally blocking everything).
        const keys = Object.keys(rule).filter(k => k !== 'label');
        if (!keys.length) return false;
        return true;
    }

    function pageIsBlocked() {
        for (const rule of BLOCK_RULES) {
            try {
                if (ruleHolds(rule)) {
                    console.info('[clipper] blocked by rule:', rule.label || rule);
                    return rule;
                }
            } catch (e) {
                console.warn('[clipper] block rule error', rule, e);
            }
        }
        return null;
    }

    // -- helpers ---------------------------------------------------------

    // Cross-manager GM bridge. Tampermonkey and Violentmonkey both expose the
    // classic GM_* functions (given the @grants above) on Chrome and Firefox;
    // some setups only expose the promise-style GM.* namespace. Resolve each
    // lazily so we run under either. The typeof-guards avoid a ReferenceError
    // when a grant is missing.
    const _GMx =
        (typeof GM_xmlhttpRequest === 'function') ? GM_xmlhttpRequest :
        (typeof GM !== 'undefined' && GM && typeof GM.xmlHttpRequest === 'function')
            ? GM.xmlHttpRequest.bind(GM) : null;

    function gmOpenInTab(url) {
        try {
            if (typeof GM_openInTab === 'function') return GM_openInTab(url, { active: true });
            if (typeof GM !== 'undefined' && GM && typeof GM.openInTab === 'function') return GM.openInTab(url);
        } catch (_) { /* fall through */ }
        try { window.open(url, '_blank'); } catch (_) { /* nothing else to try */ }
    }

    function gmFetch(opts) {
        return new Promise((resolve, reject) => {
            if (!_GMx) { reject(new Error('GM_xmlhttpRequest unavailable (check @grant / manager)')); return; }
            // Some managers' request bridge can throw synchronously (observed on
            // Chrome's built-in PDF viewer). Catch it so the promise rejects and
            // callers degrade gracefully instead of an uncaught error aborting us.
            try {
                _GMx({
                    ...opts,
                    onload: r => resolve(r),
                    onerror: e => reject(e),
                    ontimeout: () => reject(new Error('timeout')),
                });
            } catch (e) {
                reject(e);
            }
        });
    }

    function el(tag, attrs = {}, ...children) {
        const n = document.createElement(tag);
        for (const [k, v] of Object.entries(attrs)) {
            if (k === 'style' && typeof v === 'object') Object.assign(n.style, v);
            else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
            else n.setAttribute(k, v);
        }
        for (const c of children) {
            if (c == null) continue;
            n.append(c.nodeType ? c : document.createTextNode(c));
        }
        return n;
    }

    function toast(msg, kind = 'info') {
        // On pages where our HTML overlay can't be seen — Chrome/Edge paint the
        // native PDF viewer on top of page content, hiding anything we add — use
        // a manager notification so status is still visible. Everywhere else
        // (incl. Firefox's HTML pdf.js viewer) the lightweight in-page toast.
        if (looksLikePdf() && typeof GM_notification === 'function') {
            try {
                GM_notification({
                    title: 'Local Clipper', text: String(msg), timeout: 5000,
                    onclick: () => gmOpenInTab(SERVICE + '/'),
                });
                return;
            } catch (_) { /* fall back to DOM toast */ }
        }
        const t = el('div', {
            class: 'clipper-ui',
            style: {
                position: 'fixed', bottom: '20px', right: '20px',
                background: kind === 'error' ? '#c0392b' : '#27ae60',
                color: 'white', padding: '10px 14px', borderRadius: '6px',
                font: '13px sans-serif', zIndex: '2147483647',
                boxShadow: '0 4px 12px rgba(0,0,0,0.2)', cursor: 'pointer',
            },
            onclick: () => gmOpenInTab(SERVICE + '/'),
        }, msg);
        document.body.appendChild(t);
        setTimeout(() => t.remove(), 4000);
    }

    // ===================================================================
    // TARGETED EXTRACTION CONFIG
    // ===================================================================
    // Article extraction runs a cascade, stopping at the first step that
    // yields content:
    //   1. SITE-SPECIFIC extractor (matched by hostname).
    //   2. TIER-1 selectors - high-confidence content containers.
    //   3. TIER-2 selectors - broader / fuzzier markers (only if no tier-1).
    //   4. GENERIC text-density heuristic - final fallback.
    //
    // Selectors can target any marking: tag ('article'), id ('#content'),
    // class ('.post-body'), ARIA role ('[role="main"]'), or attribute
    // ('[itemprop="articleBody"]', '[data-testid="article-body"]').

    const TIER1_SELECTORS = [
        'article',                          // tag
        '[role="article"]',                 // ARIA role
        'main article',                     // descendant combinator
        '[itemprop="articleBody"]',         // schema.org microdata attribute
        '.article__body',                   // BEM-style class
        '.article-body',                    // class
        '#article-body',                    // id
        '.post-content',                    // class
        '.entry-content',                   // class (WordPress)
        '[data-component="article-body"]',  // data-attribute
    ];

    const TIER2_SELECTORS = [
        'main',                             // tag
        '[role="main"]',                    // ARIA role
        '#content',                         // id
        '#main',                            // id
        '.content',                         // class
        '.post',                            // class
        '.entry',                           // class
        '.prose',                           // class (Tailwind typography)
        '[data-testid="story"]',            // data-attribute (test hooks)
        '.markdown-body',                   // class (GitHub-rendered markdown)
    ];

    const SITE_EXTRACTORS = [
        {
            name: 'Wikipedia',
            match: h => /\bwikipedia\.org$/.test(h) || /\.wikipedia\.org$/.test(h),
            pick: () => document.querySelector('.mw-parser-output')
                     || document.querySelector('#mw-content-text'),
            titleSel: () => document.querySelector('#firstHeading'),
            clean: root => root.querySelectorAll(
                '.mw-editsection, .navbox, .vertical-navbox, .infobox, ' +
                '.reference, .reflist, .hatnote, .metadata, .mw-empty-elt, ' +
                'table.ambox, #toc, .toc'
            ).forEach(n => n.remove()),
        },
        {
            name: 'GitHub README',
            match: h => h === 'github.com' || h.endsWith('.github.com'),
            pick: () => document.querySelector('article.markdown-body')
                     || document.querySelector('.markdown-body'),
            titleSel: () => document.querySelector('strong[itemprop="name"] a, h1'),
        },
        {
            name: 'Stack Overflow',
            match: h => /(^|\.)stackoverflow\.com$/.test(h) || /(^|\.)stackexchange\.com$/.test(h),
            pick: () => {
                const wrap = document.createElement('div');
                const q = document.querySelector('.question .s-prose, .question .post-text');
                if (q) {
                    const h = document.createElement('h2'); h.textContent = 'Question';
                    wrap.append(h, q.cloneNode(true));
                }
                document.querySelectorAll('.answer .s-prose, .answer .post-text').forEach((a, i) => {
                    if (i > 2) return;
                    const h = document.createElement('h2'); h.textContent = 'Answer ' + (i + 1);
                    wrap.append(h, a.cloneNode(true));
                });
                return wrap.childNodes.length ? wrap : null;
            },
            titleSel: () => document.querySelector('#question-header h1, h1[itemprop="name"]'),
        },
        {
            name: 'Medium',
            match: h => h === 'medium.com' || h.endsWith('.medium.com'),
            pick: () => document.querySelector('article section'),
            titleSel: () => document.querySelector('h1'),
        },
        {
            name: 'MDN',
            match: h => h === 'developer.mozilla.org',
            pick: () => document.querySelector('.main-page-content article, article.main-page-content'),
            titleSel: () => document.querySelector('h1'),
        },
    ];

    // ===================================================================
    // METADATA EXTRACTION
    // ===================================================================
    // Independent of content extraction. Returns a flat object of fields
    // that gets merged into `meta` on clip submission. Searchable later via
    // meta_json on the server.
    //
    // Per-site extractors are matched by hostname (same shape as
    // SITE_EXTRACTORS). Each extractor returns a partial object; missing
    // fields are then filled in by the generic extractor. The generic
    // extractor also runs alone if no site extractor matches.
    //
    // Standard fields the rest of the system understands:
    //   title             - page title (separate from clip title, may differ)
    //   url               - canonical URL if available, else location.href
    //   report_number     - report / document ID where applicable
    //   date_published    - ISO-8601 if possible
    //   marking           - classification / handling marker (see selectMarking)
    //
    // Extractors may also add arbitrary extra fields; they all flow through
    // to meta_json. The viewer detail panel shows everything.

    // ---- Marking selection hook ---------------------------------------
    // CUSTOMISE THIS to match your existing script's logic. The function
    // receives an object of CANDIDATES gathered from various sources and
    // returns a single marking string (or null). The default below picks
    // the first non-empty candidate in source-priority order - replace
    // with your real logic.
    function selectMarking(candidates) {
        // candidates is e.g.
        //   { metaSecurity: 'OFFICIAL', firstHeading: null, classBanner: 'OFFICIAL: Sensitive', ... }
        // Default behaviour: first non-empty wins in this order.
        const priority = [
            'classBanner', 'metaSecurity', 'metaClassification',
            'firstHeading', 'pageFooter', 'urlPath', 'fallback',
        ];
        for (const key of priority) {
            const v = candidates[key];
            if (v && String(v).trim()) return String(v).trim();
        }
        return null;
    }

    // ---- Helpers used by metadata extractors --------------------------
    function metaContent(name) {
        // Match <meta name="..."> OR <meta property="...">, case-insensitive.
        const el = document.querySelector(
            `meta[name="${name}" i], meta[property="${name}" i]`
        );
        return el ? (el.getAttribute('content') || '').trim() : '';
    }
    function firstText(selectors) {
        for (const s of (Array.isArray(selectors) ? selectors : [selectors])) {
            try {
                const el = document.querySelector(s);
                if (el) {
                    const t = (el.innerText || el.textContent || '').trim();
                    if (t) return t;
                }
            } catch (_) { /* bad selector, skip */ }
        }
        return '';
    }
    function canonicalUrl() {
        const link = document.querySelector('link[rel="canonical"]');
        if (link && link.href) return link.href;
        const og = metaContent('og:url');
        return og || location.href;
    }
    function pageTitle() {
        return (metaContent('og:title') || document.title || '').trim();
    }
    function publishedDate() {
        // Try the usual suspects in order: schema.org, OG, Dublin Core, <time>.
        return (
            metaContent('article:published_time') ||
            metaContent('datePublished') ||
            metaContent('og:published_time') ||
            metaContent('dc.date') ||
            metaContent('dc.date.issued') ||
            (document.querySelector('time[datetime]')
                ? document.querySelector('time[datetime]').getAttribute('datetime')
                : '')
        ).trim();
    }

    function escapeRegExp(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

    // Find a labelled value inside a scoped metadata container — the "keyword,
    // then the next word/element" pattern. Copes with the layouts sites
    // actually use for labelled fields:
    //   inline:   <div class="report-metadata">Report number: RPT-123</div>
    //   sibling:  <span class="k">Report number</span><span class="v">RPT-123</span>
    //   dl:       <dt>Report number</dt><dd>RPT-123</dd>
    //   table:    <th>Report number</th><td>RPT-123</td>
    //
    //   label  - string or RegExp. Strings are matched case-insensitively and
    //            tolerate a trailing colon/dash on both the label argument and
    //            the page text.
    //   within - CSS selector for the container to search (e.g. '.report-metadata',
    //            '#metadata'). Omit/falsey to search the whole body.
    //   opts.word - return only the first token after the label, not the rest
    //               (use for single-token values like an ID).
    // Returns '' if the label isn't found.
    function labeledValue(label, within, opts = {}) {
        const scope = within ? document.querySelector(within) : document.body;
        if (!scope) return '';

        const re = label instanceof RegExp ? label : new RegExp(
            '^\\s*' + escapeRegExp(String(label).replace(/[:#\-\s]+$/, '')) +
            '\\s*[:#\\-–]?\\s*', 'i');

        // Tightest match: the element with the shortest text that still starts
        // with the label, so a big #metadata wrapper doesn't swallow the field
        // that follows the one we want. Include `scope` itself, not just its
        // descendants, so an inline label that lives directly on the scoped
        // element (e.g. within:'#byline' on <div id="byline">Author: …</div>)
        // is still found.
        let best = null, bestLen = Infinity;
        [scope, ...scope.querySelectorAll('*')].forEach(el => {
            const txt = (el.textContent || '').replace(/\s+/g, ' ').trim();
            if (re.test(txt) && txt.length < bestLen) { best = el; bestLen = txt.length; }
        });
        if (!best) return '';

        const txt = (best.textContent || '').replace(/\s+/g, ' ').trim();
        let value = txt.replace(re, '').trim();          // inline: strip the label
        if (!value && best.nextElementSibling) {          // dt/dd, th/td, span/span
            value = (best.nextElementSibling.textContent || '').replace(/\s+/g, ' ').trim();
        }
        return opts.word ? (value.split(/\s+/)[0] || '') : value;
    }

    // ---- Declarative label config -------------------------------------
    // Sugar over labeledValue() so a site can be a config block instead of
    // hand-written `||` chains. A "labelSpec" describes how to find one field:
    //   'Report number'                       - a single label
    //   /document\s*no/i                       - a RegExp label
    //   ['Report number', /doc.*no/i]          - try each in order, first wins
    //   { any:[...], within:'#byline', word:true } - full control; `within`
    //         overrides the spec-level container, `word` keeps only the first
    //         token of the value.
    function resolveLabel(labelSpec, defaultWithin) {
        let any, within = defaultWithin || '', word = false;
        if (Array.isArray(labelSpec)) {
            any = labelSpec;
        } else if (typeof labelSpec === 'string' || labelSpec instanceof RegExp) {
            any = [labelSpec];
        } else if (labelSpec && typeof labelSpec === 'object') {
            any = Array.isArray(labelSpec.any) ? labelSpec.any
                : (labelSpec.label != null ? [labelSpec.label] : []);
            if (labelSpec.within) within = labelSpec.within;
            if (labelSpec.word) word = true;
        } else {
            return '';
        }
        for (const lab of any) {
            const v = labeledValue(lab, within, { word });
            if (v) return v;
        }
        return '';
    }

    // Run a declarative spec -> partial meta object. `spec.container` is the
    // default scope for every label; `spec.labels` maps a meta field to a
    // labelSpec. Empty results are omitted so the generic extractor still fills
    // them in (see the merge in extractMetadata).
    function resolveLabels(spec) {
        const out = {};
        const within = spec.container || '';
        for (const [field, labelSpec] of Object.entries(spec.labels || {})) {
            const v = resolveLabel(labelSpec, within);
            if (v) out[field] = v;
        }
        return out;
    }

    // Wrap a declarative spec as a normal SITE_METADATA_EXTRACTORS entry.
    function labelExtractor(spec) {
        return { name: spec.name, match: spec.match, extract: () => resolveLabels(spec) };
    }

    // ---- Per-site metadata extractors ---------------------------------
    //
    // Each entry: { match(host), extract() -> partial meta object }
    // Add your real site logic here. The examples below are illustrative -
    // adapt selectors / regex / candidate sources to match what your
    // existing script does for each site.

    const SITE_METADATA_EXTRACTORS = [
        // EXAMPLE 1: arXiv. Pulls arXiv ID as report_number from URL/citation
        // and authors from the dl/dt/dd block.
        {
            name: 'arXiv',
            match: h => h === 'arxiv.org' || h.endsWith('.arxiv.org'),
            extract: () => {
                const m = location.pathname.match(/\/abs\/([\w.\-/]+)/);
                const arxivId = m ? m[1] : '';
                return {
                    report_number: arxivId ? 'arXiv:' + arxivId : '',
                    date_published: firstText('.dateline').replace(/^.*?:\s*/, '')
                                    || publishedDate(),
                    marking_candidates: {
                        // arXiv preprints are public; no classification.
                        // But if your existing logic stamps something specific
                        // for arXiv, plug it in here.
                        fallback: 'PUBLIC',
                    },
                };
            },
        },

        // EXAMPLE 2: a generic "gov report" site. Look for a doc ID in the
        // breadcrumb / report-id span, a classification banner, etc.
        // Replace `match` with your real host check.
        {
            name: 'Example gov report site',
            match: h => h === 'reports.example.gov',
            extract: () => ({
                // Prefer a dedicated element if the site has one, then fall back
                // to scanning a metadata block for a "Report number:" label and
                // taking the value that follows it (works inline, in a sibling
                // span, or in a dl/table row). `word: true` since an ID is a
                // single token.
                report_number: firstText([
                    '.report-id', '#report-number', '[data-report-id]',
                ]) || labeledValue('Report number', '.report-metadata', { word: true })
                   || labeledValue(/^\s*(report|document|publication)\s*(no\.?|number|id)/i,
                                   '#metadata', { word: true }),
                date_published: firstText('.publish-date')
                    || labeledValue('Published', '.report-metadata')
                    || publishedDate(),
                marking_candidates: {
                    classBanner: firstText(['.classification-banner', '.handling-marker'])
                        || labeledValue('Classification', '.report-metadata'),
                    metaSecurity: metaContent('security-classification'),
                    pageFooter: firstText('.page-footer .classification'),
                },
            }),
        },

        // EXAMPLE 3: Wikipedia - illustrate pulling distinct fields.
        {
            name: 'Wikipedia',
            match: h => /\bwikipedia\.org$/.test(h),
            extract: () => ({
                title: firstText('#firstHeading') || pageTitle(),
                marking_candidates: { fallback: 'PUBLIC' },
            }),
        },

        // EXAMPLE 4: declarative label-driven extractor. No JS per field — a
        // container plus a map of meta-field -> label(s). Equivalent to the
        // hand-written labeledValue() chains in EXAMPLE 2, but as config.
        // Fields it doesn't find are left unset, so the generic extractor and
        // publishedDate() still fill them in. Targets markup like:
        //
        //   <section class="report-metadata">
        //     <dl>
        //       <dt>Report number</dt><dd>RPT-2026-0412</dd>
        //       <dt>Published</dt><dd>2026-03-14</dd>
        //     </dl>
        //   </section>
        //   <div id="byline">Prepared by: A. Researcher</div>
        //
        labelExtractor({
            name: 'Example label-driven report site',
            match: h => h === 'docs.example.org',
            container: '.report-metadata',           // default scope for every label
            labels: {
                report_number: { any: ['Report number', /document\s*no/i], word: true },
                date_published: 'Published',          // string shorthand
                author: { any: ['Author', 'Prepared by'], within: '#byline' }, // per-field scope
            },
        }),
    ];

    // ---- Generic metadata fallback ------------------------------------
    function genericMetadata() {
        const candidates = {
            metaClassification: metaContent('classification'),
            metaSecurity: metaContent('security-classification') || metaContent('security'),
            // Common visible banners.
            classBanner: firstText([
                '.classification-banner', '.handling-marker', '.security-banner',
                '#classification', '[data-classification]',
            ]),
            firstHeading: '',  // see below
            pageFooter: firstText(['.page-footer .classification', 'footer .classification']),
            urlPath: '',
            fallback: '',
        };
        // First heading: if it looks like "OFFICIAL: ..." or contains an
        // ALL-CAPS handling marker, use it.
        const h1 = firstText('h1');
        if (h1 && /^[A-Z][A-Z\s:\-/]{3,}/.test(h1.split('\n')[0])) {
            candidates.firstHeading = h1.split('\n')[0].trim();
        }
        // URL-path marker: e.g. /unclassified/, /official/, /pub/.
        const pathMatch = location.pathname.match(/\/(unclassified|official|public|pub|internal|confidential)(\/|$)/i);
        if (pathMatch) candidates.urlPath = pathMatch[1].toUpperCase();

        // Try to detect a report number from typical patterns in the title
        // or visible "Report No." / "Document No." labels on the page.
        let reportNumber = '';
        const labelEl = Array.from(document.querySelectorAll('*'))
            .find(el => /^(report|document|publication)\s*(no\.?|number|id)\s*[:#-]?\s*/i.test(
                ((el.childNodes[0] && el.childNodes[0].nodeValue) || '').trim()
            ));
        if (labelEl) {
            const text = (labelEl.innerText || '').replace(/\s+/g, ' ').trim();
            const m = text.match(/^(report|document|publication)\s*(no\.?|number|id)\s*[:#-]?\s*([\w./\-]+)/i);
            if (m) reportNumber = m[3];
        }
        if (!reportNumber) {
            // Some sites tag it as schema.org / citation_*.
            reportNumber = metaContent('citation_technical_report_number')
                || metaContent('citation_report_id')
                || metaContent('dc.identifier')
                || '';
        }

        return {
            title: pageTitle(),
            url: canonicalUrl(),
            report_number: reportNumber,
            date_published: publishedDate(),
            marking_candidates: candidates,
        };
    }

    // ---- Top-level entry point ----------------------------------------
    function extractMetadata() {
        const host = location.hostname;
        let siteMeta = {};
        let siteName = null;
        const site = SITE_METADATA_EXTRACTORS.find(s => {
            try { return s.match(host); } catch (_) { return false; }
        });
        if (site) {
            try {
                siteMeta = site.extract() || {};
                siteName = site.name;
            } catch (e) {
                console.warn('clipper: metadata extractor', site.name, 'failed', e);
            }
        }
        // Fill in anything the site extractor didn't supply from the generic.
        const genericMeta = genericMetadata();
        const merged = { ...genericMeta, ...siteMeta };
        // Marking candidates: site-specific candidates take priority, then
        // any unset ones fall back to the generic detector's findings.
        merged.marking_candidates = {
            ...(genericMeta.marking_candidates || {}),
            ...(siteMeta.marking_candidates || {}),
        };
        merged.marking = selectMarking(merged.marking_candidates);
        merged.metadata_extractor = siteName ? `site:${siteName}` : 'generic';
        return merged;
    }

    function nodeHasContent(node, minChars = 200) {
        return node && (node.innerText || '').trim().length >= minChars;
    }

    function findTargeted(selectors) {
        for (const sel of selectors) {
            let node;
            try { node = document.querySelector(sel); } catch (_) { continue; }
            if (nodeHasContent(node)) return { node, selector: sel };
        }
        return null;
    }

    function genericBestNode() {
        const candidates = Array.from(document.querySelectorAll(
            'article, main, [role="main"], .post, .entry, .content, #content'
        ));
        const pool = candidates.length ? candidates : Array.from(document.querySelectorAll('div, section'));
        let best = null, bestScore = 0;
        for (const node of pool) {
            const text = node.innerText || '';
            const ps = node.querySelectorAll('p').length;
            const score = text.length * (1 + Math.log(ps + 1));
            if (score > bestScore) { bestScore = score; best = node; }
        }
        return best;
    }

    // -- capture modes ---------------------------------------------------

    function captureFull() {
        const clone = document.body.cloneNode(true);
        // Remove the clipper's own injected UI (launcher, modal, chip, toast)
        // so it doesn't end up in the captured page.
        clone.querySelectorAll('.clipper-ui, #clipper-launcher').forEach(n => n.remove());
        clone.querySelectorAll('script, style, iframe, noscript, link[rel="stylesheet"]').forEach(n => n.remove());
        return { title: document.title, html: clone.outerHTML, mode: 'full' };
    }

    function captureSelection() {
        const sel = window.getSelection();
        if (!sel || sel.isCollapsed) return null;
        const container = document.createElement('div');
        for (let i = 0; i < sel.rangeCount; i++) {
            container.appendChild(sel.getRangeAt(i).cloneContents());
        }
        return { title: document.title + ' (selection)', html: container.innerHTML, mode: 'selection' };
    }

    // Structural + conventional "page chrome" we don't want in either the
    // stored article OR the text we feed the tag-suggester: site nav, asides,
    // headers/footers, and the usual related / recommended / "more from" /
    // carousel / share / comment / newsletter widgets. The semantic elements
    // (nav/aside/header/footer/roles) are always safe to drop; the class/
    // aria/id patterns are best-effort, matched case-insensitively, and tuned
    // to the common chrome that was leaking unrelated tags into suggestions
    // (e.g. an ABC program page's "More from this program" carousel).
    const BOILERPLATE_SELECTORS = [
        'nav', 'aside', 'header', 'footer', 'form', 'button',
        '[role="navigation"]', '[role="complementary"]', '[role="banner"]',
        '[role="contentinfo"]', '[role="search"]',
        '[class*="related" i]', '[class*="recommend" i]',
        '[class*="more-from" i]', '[class*="morefrom" i]', '[class*="up-next" i]',
        '[class*="carousel" i]', '[class*="promo" i]', '[class*="newsletter" i]',
        '[class*="subscribe" i]', '[class*="share" i]', '[class*="social" i]',
        '[class*="comment" i]', '[class*="sidebar" i]', '[class*="breadcrumb" i]',
        '[class*="advert" i]', '[id*="related" i]', '[id*="comment" i]',
        '[aria-label*="related" i]', '[aria-label*="recommend" i]',
        '[data-component*="related" i]',
    ];

    function stripBoilerplate(root) {
        for (const sel of BOILERPLATE_SELECTORS) {
            let nodes;
            try { nodes = root.querySelectorAll(sel); }
            catch (_) { continue; }  // skip any selector the engine rejects
            nodes.forEach(n => n.remove());
        }
        return root;
    }

    // Run the extraction cascade and return the chosen content node plus
    // provenance, WITHOUT cloning - callers decide whether to clone it (for
    // storage) or read its text (for suggestions). Returns
    // { node, title, extractor, siteClean }; node may be null.
    function pickContentNode() {
        const host = location.hostname;
        let node = null, title = null, extractor = null, siteClean = null;

        // 1. Site-specific extractor.
        const site = SITE_EXTRACTORS.find(s => {
            try { return s.match(host); } catch (_) { return false; }
        });
        if (site) {
            try {
                const picked = site.pick();
                if (nodeHasContent(picked, 50)) {
                    node = picked;
                    extractor = 'site:' + site.name;
                    siteClean = site.clean || null;
                    if (site.titleSel) {
                        const tEl = site.titleSel();
                        if (tEl) title = tEl.innerText.trim();
                    }
                }
            } catch (e) {
                console.warn('clipper: site extractor', site.name, 'failed', e);
            }
        }
        // 2. Tier-1 selectors.
        if (!node) {
            const hit = findTargeted(TIER1_SELECTORS);
            if (hit) { node = hit.node; extractor = 'tier1:' + hit.selector; }
        }
        // 3. Tier-2 selectors (only if tier-1 found nothing).
        if (!node) {
            const hit = findTargeted(TIER2_SELECTORS);
            if (hit) { node = hit.node; extractor = 'tier2:' + hit.selector; }
        }
        // 4. Generic heuristic fallback.
        if (!node) {
            node = genericBestNode();
            extractor = node ? 'generic' : null;
        }
        return { node, title, extractor, siteClean };
    }

    function captureArticle() {
        const { node, title: pickedTitle, extractor, siteClean } = pickContentNode();
        if (!node) return captureFull();

        const clone = node.cloneNode(true);
        clone.querySelectorAll('.clipper-ui, #clipper-launcher').forEach(n => n.remove());
        clone.querySelectorAll('script, style, iframe, noscript').forEach(n => n.remove());
        stripBoilerplate(clone);
        if (siteClean) { try { siteClean(clone); } catch (_) { /* best effort */ } }

        let title = pickedTitle;
        if (!title) {
            const h = clone.querySelector('h1, h2');
            title = (h && h.innerText.trim()) || document.title;
        }

        return {
            title: title || document.title,
            html: clone.outerHTML,
            mode: 'article',
            extractor,
        };
    }

    // Clean article context for the tag-suggester: { title, text }. Uses the
    // SAME extraction + boilerplate stripping as a real clip, so suggestions
    // stop keying off page chrome (nav, carousels, "more from", related
    // links) that used to drag in unrelated tags. The title is the EXTRACTED
    // article title (the server boosts keyword suggestions that overlap it),
    // not document.title which often carries a " - Site Name" suffix. Falls
    // back to raw body text / document.title if no content node is found.
    // textContent (not innerText) so it works on the detached clone.
    function articleSuggestContext(maxChars = 4000) {
        const { node, title } = pickContentNode();
        if (!node) {
            return {
                title: document.title || '',
                text: (document.body ? (document.body.innerText || '') : '').slice(0, maxChars),
            };
        }
        const clone = node.cloneNode(true);
        clone.querySelectorAll('.clipper-ui, #clipper-launcher, script, style, noscript')
            .forEach(n => n.remove());
        stripBoilerplate(clone);
        const text = (clone.textContent || '').replace(/\s+/g, ' ').trim().slice(0, maxChars);
        let t = title;
        if (!t) {
            const h = clone.querySelector('h1, h2');
            t = (h && h.innerText.trim()) || document.title || '';
        }
        return { title: t, text };
    }

    // -- link stripping + image inlining ---------------------------------

    // Unwrap every <a>: keep its inner content (text/images/formatting) but
    // remove the link, so no hyperlinks survive into stored HTML. Also drop
    // stray href / xlink:href attributes from anything left over.
    function stripLinks(root) {
        let removed = 0;
        root.querySelectorAll('a').forEach(a => {
            const parent = a.parentNode;
            if (!parent) return;
            while (a.firstChild) parent.insertBefore(a.firstChild, a);
            parent.removeChild(a);
            removed++;
        });
        root.querySelectorAll('[href], [xlink\\:href]').forEach(el2 => {
            el2.removeAttribute('href');
            el2.removeAttribute('xlink:href');
        });
        return removed;
    }

    async function inlineImages(htmlString, baseUrl) {
        const doc = new DOMParser().parseFromString(htmlString, 'text/html');

        // Strip hyperlinks first so link text remains but no <a> survives.
        const linksRemoved = stripLinks(doc.body);

        // Normalise lazy-loaded images and kill responsive-image attributes:
        // - promote data-src/data-original/data-lazy-src to src so the real
        //   image gets inlined instead of a placeholder;
        // - drop srcset/sizes/loading (and <picture> <source> variants),
        //   which would otherwise keep remote URLs in the stored clip and
        //   leak requests to the original site when viewed later.
        doc.querySelectorAll('img').forEach(img => {
            if (!img.getAttribute('src') || /^data:image\/(gif|svg)/.test(img.getAttribute('src') || '')) {
                const lazy = img.getAttribute('data-src') || img.getAttribute('data-original')
                          || img.getAttribute('data-lazy-src');
                if (lazy) img.setAttribute('src', lazy);
            }
            ['srcset', 'sizes', 'loading', 'data-src', 'data-original', 'data-lazy-src']
                .forEach(a => img.removeAttribute(a));
        });
        doc.querySelectorAll('picture source').forEach(s => s.remove());

        const imgs = Array.from(doc.querySelectorAll('img'));
        let inlined = 0;
        for (const img of imgs) {
            const src = img.getAttribute('src');
            if (!src || src.startsWith('data:')) continue;
            try {
                const abs = new URL(src, baseUrl).href;
                const resp = await gmFetch({
                    method: 'GET', url: abs, responseType: 'arraybuffer', timeout: 15000,
                });
                if (resp.status >= 200 && resp.status < 300) {
                    const bytes = new Uint8Array(resp.response);
                    const b64 = arrayBufferToBase64(bytes);
                    const mime = guessMime(abs, resp);
                    img.setAttribute('src', `data:${mime};base64,${b64}`);
                    inlined++;
                }
            } catch (e) {
                console.warn('clipper: image inline failed for', src, e);
            }
        }
        return { html: doc.body.innerHTML, inlined, total: imgs.length, linksRemoved };
    }

    function arrayBufferToBase64(bytes) {
        let bin = '';
        const chunk = 0x8000;
        for (let i = 0; i < bytes.length; i += chunk) {
            bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
        }
        return btoa(bin);
    }

    function guessMime(url, resp) {
        const headers = (resp.responseHeaders || '').toLowerCase();
        const m = headers.match(/content-type:\s*(image\/[a-z+.-]+)/);
        if (m) return m[1];
        const ext = url.split('?')[0].split('.').pop().toLowerCase();
        return ({
            png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg',
            gif: 'image/gif', webp: 'image/webp', svg: 'image/svg+xml',
        })[ext] || 'image/png';
    }

    // -- PDF -------------------------------------------------------------

    function looksLikePdf() {
        if (/\.pdf($|\?|#)/i.test(location.pathname + location.search)) return true;
        if (document.contentType === 'application/pdf') return true;
        if (document.querySelector('embed[type="application/pdf"]')) return true;
        return false;
    }

    async function fetchPdfBytes() {
        const resp = await gmFetch({
            method: 'GET', url: location.href, responseType: 'arraybuffer', timeout: 60000,
        });
        if (resp.status < 200 || resp.status >= 300) throw new Error('PDF fetch failed: ' + resp.status);
        return new Uint8Array(resp.response);
    }

    // -- Linked PDF detection + stub-article-with-PDF flow ----------------
    //
    // Right-clicking an anchor that points to a PDF should offer to clip
    // the current page's article AND attach the PDF as one combined clip.
    // We add a small floating chip near the cursor on contextmenu, not a
    // real OS context-menu entry (which would require a browser extension).

    // Returns the anchor element to use as the PDF source, or null. Walks
    // up from the event target so users can right-click an icon/img inside
    // an <a>.
    function findPdfAnchor(target) {
        let el = target;
        while (el && el.nodeType === 1) {
            if (el.tagName === 'A' && el.href) {
                if (anchorLooksLikePdf(el)) return el;
                return null;
            }
            el = el.parentElement;
        }
        return null;
    }

    function anchorLooksLikePdf(a) {
        const href = (a.getAttribute('href') || '').trim();
        if (!href) return false;
        // Drop the fragment + query for extension testing.
        const path = href.split('#')[0].split('?')[0].toLowerCase();
        if (path.endsWith('.pdf')) return true;
        // Some sites use ?type=pdf, ?format=pdf, or set type/role attrs.
        if ((a.type || '').toLowerCase() === 'application/pdf') return true;
        if (/(?:^|[?&])(?:format|type|filetype)=pdf(?:&|$)/i.test(href)) return true;
        // Aria/data hints sometimes mark download buttons.
        const aria = (a.getAttribute('aria-label') || '').toLowerCase();
        if (/\.pdf\b|download pdf|view pdf|full report \(pdf\)/.test(aria)) return true;
        return false;
    }

    async function fetchArbitraryUrlAsBytes(url, timeoutMs = 120000) {
        const resp = await gmFetch({
            method: 'GET', url, responseType: 'arraybuffer', timeout: timeoutMs,
        });
        if (resp.status < 200 || resp.status >= 300) {
            throw new Error('fetch ' + url + ' -> HTTP ' + resp.status);
        }
        return {
            bytes: new Uint8Array(resp.response),
            // Pull a reasonable filename from Content-Disposition or the URL path.
            filename: filenameFromResponse(resp, url),
            mime: contentTypeFromResponse(resp) || 'application/pdf',
        };
    }

    function filenameFromResponse(resp, url) {
        const headers = (resp.responseHeaders || '');
        // Try filename= and filename*= in Content-Disposition.
        const m1 = headers.match(/filename\*\s*=\s*[^']*''([^;\r\n]+)/i)
                || headers.match(/filename\s*=\s*"([^"]+)"/i)
                || headers.match(/filename\s*=\s*([^;\r\n]+)/i);
        if (m1) {
            try { return decodeURIComponent(m1[1].trim()); }
            catch (_) { return m1[1].trim(); }
        }
        // Fall back to the URL's last path segment.
        try {
            const u = new URL(url);
            const seg = u.pathname.split('/').filter(Boolean).pop() || 'document.pdf';
            return seg.endsWith('.pdf') ? seg : (seg + '.pdf');
        } catch (_) {
            return 'document.pdf';
        }
    }

    function contentTypeFromResponse(resp) {
        const m = (resp.responseHeaders || '').match(/content-type:\s*([^;\r\n]+)/i);
        return m ? m[1].trim().toLowerCase() : '';
    }

    function bytesToBase64(bytes) { return arrayBufferToBase64(bytes); }

    // Floating chip shown when right-clicking a PDF link.
    // We don't preventDefault on the contextmenu event - the normal browser
    // menu still appears. The chip is an extra affordance, not a hijack.
    //
    // Positioning matters: the native context menu opens DOWN-AND-RIGHT from
    // the click point and, being browser chrome, paints on top of the page.
    // A chip sitting at the cursor is therefore hidden behind the menu, and
    // the first click (to dismiss the menu) also dismisses the chip - so it
    // can never be clicked. We place the chip ABOVE-AND-LEFT of the cursor,
    // which clears the menu in the common case and keeps either vertical or
    // horizontal separation in most edge-flips (only a cursor jammed into the
    // top-left viewport corner can still overlap, which is rare).
    let chipEl = null;
    function showLinkChip(anchor, ev) {
        hideLinkChip();
        chipEl = el('div', {
            class: 'clipper-ui',
            style: {
                position: 'fixed',
                // Off-screen until measured below, so it doesn't flash at 0,0.
                left: '-9999px', top: '-9999px',
                background: '#06c', color: 'white',
                padding: '8px 12px', borderRadius: '4px',
                font: '13px -apple-system, BlinkMacSystemFont, sans-serif',
                boxShadow: '0 4px 12px rgba(0,0,0,0.25)',
                zIndex: '2147483647', cursor: 'pointer', userSelect: 'none',
                maxWidth: '260px',
            },
            onclick: () => {
                hideLinkChip();
                doStubClip(anchor.href);
            },
        }, '📎 Clip this PDF with article context');
        document.body.appendChild(chipEl);

        // Measure the real size, then anchor above-and-left of the cursor and
        // clamp to the viewport. Fall back to the other side of an axis only
        // when the cursor is too close to the top/left edge to fit there.
        const GAP = 10, PAD = 4;
        const rect = chipEl.getBoundingClientRect();
        let left = ev.clientX - rect.width - GAP;
        let top  = ev.clientY - rect.height - GAP;
        if (left < PAD) left = Math.min(ev.clientX + GAP, window.innerWidth  - rect.width  - PAD);
        if (top  < PAD) top  = Math.min(ev.clientY + GAP, window.innerHeight - rect.height - PAD);
        chipEl.style.left = Math.max(PAD, left) + 'px';
        chipEl.style.top  = Math.max(PAD, top)  + 'px';

        // Auto-hide if the user clicks elsewhere or scrolls.
        setTimeout(() => {
            document.addEventListener('click', hideLinkChip, { once: true, capture: true });
            document.addEventListener('scroll', hideLinkChip, { once: true, passive: true });
        }, 0);
        // Time-out after a few seconds so it doesn't linger if ignored.
        setTimeout(hideLinkChip, 6000);
    }
    function hideLinkChip() {
        if (chipEl && chipEl.parentNode) chipEl.parentNode.removeChild(chipEl);
        chipEl = null;
    }

    // After a no-dialog PDF save (stub-clip, or quick-clip) the clip lands with
    // no tags - those flows have no tag widget. Surface the server's suggestions
    // as a small floating row of clickable chips so they can be added in one
    // click each, right where the "Saved" toast appears. Suggestions come from
    // /api/tags/suggest with exclude_clip_id: the server assembles the document
    // context itself - including the attached PDF's extracted text - so a PDF
    // clip (whose own content_text is just a title) still gets real suggestions.
    // A chip click PATCHes the cumulative tag list onto the clip. Reuses the
    // showLinkChip overlay idiom; on Firefox's HTML pdf.js viewer the DOM is
    // visible, so this shows for both flows.
    // Shared by the capture form and the saved-clip editor. Never persisted
    // locally: defaults always come from this page or this saved clip.
    function createMetadataEditor(container, initial = {}) {
        const inputs = {};
        const dirty = new Set();
        for (const [key, label] of Object.entries({ title: 'Title', url: 'Original URL',
            report_number: 'Report number', date_published: 'Published', marking: 'Marking' })) {
            const input = el('input', { type: 'text', 'aria-label': label,
                style: { width: '100%', boxSizing: 'border-box', padding: '5px', margin: '3px 0 7px',
                    border: '1px solid #ccc', borderRadius: '4px', fontSize: '12px' },
                oninput: () => dirty.add(key) });
            inputs[key] = input;
            container.appendChild(el('label', { style: { display: 'block', fontSize: '11px', color: '#666' } }, label, input));
        }
        function update(values) {
            for (const key of Object.keys(inputs)) {
                if (!dirty.has(key)) inputs[key].value = values[key] || '';
            }
        }
        update(initial);
        return { update, clearDirty: () => dirty.clear(),
            values: () => Object.fromEntries(Object.entries(inputs).map(([k, input]) => [k, input.value.trim()])),
            changes: () => Object.fromEntries([...dirty].map(k => [k, inputs[k].value.trim()])) };
    }

    let suggestChipEl = null;
    async function showSuggestionChips(clipId) {
        hideSuggestionChips();
        let suggestions = [];
        try {
            suggestions = await fetchSuggestions({
                exclude_clip_id: clipId, chosen: [], limit: 12,
            });
        } catch (_) { /* service down / offline: nothing to show */ }


        const applied = [];   // initial saved tags are loaded below
        const row = el('div', {
            style: { display: 'flex', flexWrap: 'wrap', gap: '6px', alignItems: 'center' },
        });
        const box = el('div', {
            class: 'clipper-ui',
            style: {
                position: 'fixed', bottom: '74px', right: '20px', width: '360px', maxWidth: '90vw', maxHeight: '80vh', overflowY: 'auto',
                background: 'white', color: '#222', padding: '10px 12px',
                borderRadius: '8px', boxShadow: '0 6px 20px rgba(0,0,0,0.25)',
                font: '13px -apple-system, BlinkMacSystemFont, sans-serif',
                zIndex: '2147483646',
            },
        });
        suggestChipEl = box;

        // Keep saved metadata visible until explicitly dismissed.

        const header = el('div', {
            style: {
                display: 'flex', justifyContent: 'space-between',
                alignItems: 'center', marginBottom: '8px',
            },
        },
            el('span', { style: { fontSize: '11px', color: '#666', fontWeight: '600' } },
               `Edit clip #${clipId}`),
            el('span', {
                title: 'Dismiss',
                style: { cursor: 'pointer', color: '#999', fontSize: '15px', lineHeight: '1', padding: '0 2px' },
                onclick: hideSuggestionChips,
            }, '×'));
        const editorHost = el('div', { style: { marginTop: '10px' } });
        const editor = createMetadataEditor(editorHost);
        const saveStatus = el('div', { role: 'status' });
        const metadata = el('pre', { style: { whiteSpace: 'pre-wrap', maxHeight: '220px', overflow: 'auto', fontSize: '12px' } });
        async function refreshMetadata() {
            try {
                const r = await gmFetch({ method: 'GET', url: `${SERVICE}/api/clips/${clipId}`, timeout: 8000 });
                if (r.status !== 200) throw new Error('Unable to load saved metadata');
                const saved = JSON.parse(r.responseText);
                editor.update({ ...saved.meta, title: saved.title, url: saved.url });
                applied.splice(0, applied.length, ...(saved.tags || []));
                metadata.textContent = JSON.stringify({ ...saved.meta, title: saved.title, url: saved.url, tags: saved.tags }, null, 2);
            } catch (e) { metadata.textContent = e.message; }
        }
        const saveMetadata = el('button', { onclick: async () => {
            const changed = editor.changes();
            if (!Object.keys(changed).length) return;
            saveMetadata.disabled = true;
            saveStatus.textContent = 'Saving…';
            try {
                const { title, ...meta } = changed;
                const payload = { meta };
                if (title !== undefined) payload.title = title || 'Untitled';
                const r = await gmFetch({ method: 'PATCH', url: `${SERVICE}/api/clips/${clipId}`,
                    headers: { 'Content-Type': 'application/json' }, data: JSON.stringify(payload), timeout: 8000 });
                if (r.status !== 200) throw new Error('Save failed. Your edits are retained; retry.');
                editor.clearDirty();
                await refreshMetadata();
                saveStatus.textContent = 'Metadata saved';
            } catch (e) { saveStatus.textContent = e.message; }
            finally { saveMetadata.disabled = false; }
        } }, 'Save metadata');
        box.append(header, editorHost, saveMetadata, saveStatus, el('a', { href: `${SERVICE}/clip/${clipId}`, target: '_blank' }, 'Edit saved metadata'),
            el('button', { onclick: refreshMetadata }, 'Refresh metadata'), metadata, row);
        await refreshMetadata();
        const refreshTimer = setInterval(() => {
            if (!box.isConnected) { clearInterval(refreshTimer); return; }
            refreshMetadata();
        }, 3000);

        for (const s of suggestions) {
            const isBoth = s.why === 'both';
            const chip = el('span', {
                title: s.why || '',
                style: {
                    fontSize: '12px', padding: '3px 9px', borderRadius: '12px',
                    background: isBoth ? '#e7f6ec' : '#eef',
                    color: isBoth ? '#1b5e2a' : '#335',
                    border: '1px solid ' + (isBoth ? '#b6e0c4' : '#ccd'),
                    cursor: 'pointer', userSelect: 'none',
                },
                onclick: async () => {
                    if (chip.dataset.added) return;
                    applied.push(s.tag);
                    try {
                        await patchClipTags(clipId, applied.slice());
                        chip.dataset.added = '1';
                        chip.textContent = '✓ ' + s.tag;
                        chip.style.background = '#cdebd6';
                        chip.style.color = '#0f4d20';
                        chip.style.border = '1px solid #9fd3b0';
                        chip.style.cursor = 'default';
                    } catch (e) {
                        applied.pop();   // roll back so a retry re-sends it
                        console.warn('clipper: add tag failed', e);
                    }
                },
            }, s.tag + ' ', el('span', { style: { color: '#88a', fontSize: '10px' } },
                               WHY_MARK[s.why] || '≈'));
            row.appendChild(chip);
        }

        document.body.appendChild(box);
    }
    function hideSuggestionChips() {
        if (suggestChipEl && suggestChipEl.parentNode) suggestChipEl.parentNode.removeChild(suggestChipEl);
        suggestChipEl = null;
    }

    // The stub-clip flow: extract the current page's article, fetch the PDF
    // from the link, post both as a single combined clip.
    async function doStubClip(pdfUrl) {
        // Don't run on blocked pages.
        const blocked = pageIsBlocked();
        if (blocked) {
            toast('Clipper is disabled on this page (' + (blocked.label || 'blocked') + ')', 'error');
            return;
        }
        toast('Fetching PDF…');
        try {
            // 1. Article extraction on the live page (the "stub").
            const cap = captureArticle();
            if (!cap) throw new Error('No article content found');

            // 2. Strip links + inline images on the article HTML (same as
            //    the normal clip flow).
            const { html, inlined, total, linksRemoved } =
                await inlineImages(cap.html, location.href);

            // 3. Fetch the linked PDF. GM_xmlhttpRequest carries cookies
            //    for that origin, so authenticated docs work.
            const pdf = await fetchArbitraryUrlAsBytes(pdfUrl);
            if (!pdf.mime.includes('pdf') && !pdf.filename.toLowerCase().endsWith('.pdf')) {
                // Soft-warn but proceed - some servers send octet-stream
                // for PDFs and we still want to store them.
                console.warn('clipper: link does not look like a PDF, mime=' + pdf.mime);
            }

            // 4. Metadata - run against the live article page, not the PDF.
            let extractedMeta = {};
            try { extractedMeta = extractMetadata(); }
            catch (e) { console.warn('clipper: metadata extraction failed', e); }

            // 5. Combine into one clip with the PDF as an attachment.
            // No auto-tags: this flow has no tag UI, and silently attaching
            // the previous clip's tags put wrong tags on unrelated documents.
            // Tag it afterwards in the viewer (where suggestions can help).
            const tags = [];
            const r = await postClipDedup({
                html,
                title: cap.title,
                url: location.href,
                source: 'userscript-article+pdf',
                tags,
                attachments: [{
                    filename: pdf.filename,
                    mime: 'application/pdf',
                    b64: bytesToBase64(pdf.bytes),
                }],
                meta: {
                    capture_mode: 'article+pdf',
                    host: location.host,
                    extractor: cap.extractor || null,
                    links_removed: linksRemoved,
                    images_inlined: inlined,
                    pdf_source_url: pdfUrl,
                    pdf_filename: pdf.filename,
                    pdf_bytes: pdf.bytes.length,
                    ...extractedMeta,
                },
            });
            if (!r) { toast('Skipped (duplicate)'); return; }
            const markStr = extractedMeta.marking ? ` [${extractedMeta.marking}]` : '';
            toast(`Saved clip #${r.id}${markStr} with PDF (${(pdf.bytes.length/1024).toFixed(0)} KB)`);
            // No tag widget in this flow - offer suggestions to add post-save.
            if (r.id) showSuggestionChips(r.id);
        } catch (e) {
            console.error('clipper: stub-clip failed', e);
            toast('Failed: ' + e.message, 'error');
        }
    }

    function installLinkContextMenu() {
        // Capture-phase so we run before page handlers that might
        // stopPropagation on the contextmenu event.
        document.addEventListener('contextmenu', ev => {
            try {
                const anchor = findPdfAnchor(ev.target);
                if (!anchor) { hideLinkChip(); return; }
                showLinkChip(anchor, ev);
            } catch (e) {
                console.warn('clipper: contextmenu handler error', e);
            }
        }, true);
    }

    // -- service calls ---------------------------------------------------

    async function postClip(payload) {
        const resp = await gmFetch({
            method: 'POST', url: `${SERVICE}/api/clip`,
            headers: { 'Content-Type': 'application/json' },
            data: JSON.stringify(payload), timeout: 120000,
        });
        if (resp.status !== 200) throw new Error('clip: ' + resp.status + ' ' + resp.responseText);
        return JSON.parse(resp.responseText);
    }

    // Add tags to an already-saved clip. Used by the post-save suggestion chips
    // (the no-dialog PDF flows have no tag widget to collect them up front). The
    // server's upsert is additive, but we always send the cumulative chosen list
    // so a click is idempotent and order-independent.
    async function patchClipTags(clipId, tags) {
        const resp = await gmFetch({
            method: 'PATCH', url: `${SERVICE}/api/clips/${clipId}`,
            headers: { 'Content-Type': 'application/json' },
            data: JSON.stringify({ tags }), timeout: 15000,
        });
        if (resp.status !== 200) throw new Error('patch tags: ' + resp.status);
        return JSON.parse(resp.responseText);
    }

    // Shared "you already have this" prompt. Returns true if the user wants to
    // clip the duplicate anyway (force), false to skip.
    function confirmDuplicate(d) {
        const when = d.captured_at
            ? new Date(d.captured_at * 1000).toLocaleDateString() : 'previously';
        const matched = d.matched_on ? `, matched on ${d.matched_on}` : '';
        return window.confirm(
            `You already have this — "${d.title}" (clip #${d.id}, saved ${when}${matched}).` +
            `\n\nClip it again anyway?`);
    }

    // Post a clip, and if the server reports a duplicate, ask the user whether
    // to clip anyway (force). Returns the final response object, or null if
    // the user declined. `r.ok === true` means a clip was created.
    async function postClipDedup(payload) {
        let r = await postClip(payload);
        if (r && r.ok === false && r.duplicate) {
            if (!confirmDuplicate(r.duplicate)) return null;
            r = await postClip({ ...payload, force: true });
        }
        return r;
    }

    async function postPdf(pdfBytes, filename, tagsCsv, url, metaJson, force, note) {
        const boundary = '----clip' + Math.random().toString(16).slice(2);
        const enc = new TextEncoder();
        const parts = [];
        const push = (name, value) =>
            parts.push(enc.encode(`--${boundary}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n${value}\r\n`));
        const pdfMeta = metaJson ? JSON.parse(metaJson) : {};
        push('title', pdfMeta.title || filename);
        push('url', url);
        push('tags', tagsCsv);
        if (metaJson) push('meta', metaJson);
        if (note) push('note', note);
        if (force) push('force', '1');
        parts.push(enc.encode(
            `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${filename}"\r\nContent-Type: application/pdf\r\n\r\n`
        ));
        parts.push(pdfBytes);
        parts.push(enc.encode(`\r\n--${boundary}--\r\n`));
        const total = parts.reduce((s, p) => s + p.byteLength, 0);
        const body = new Uint8Array(total);
        let off = 0;
        for (const p of parts) { body.set(p, off); off += p.byteLength; }
        const resp = await gmFetch({
            method: 'POST', url: `${SERVICE}/api/attach-pdf`,
            headers: { 'Content-Type': `multipart/form-data; boundary=${boundary}` },
            data: body.buffer, binary: true, timeout: 300000,
        });
        if (resp.status !== 200) throw new Error('pdf: ' + resp.status + ' ' + resp.responseText);
        return JSON.parse(resp.responseText);
    }

    // Upload a PDF, and if the server reports a duplicate, confirm before
    // forcing. Returns the response, or null if the user declined. Mirrors
    // postClipDedup for the multipart PDF endpoint.
    async function postPdfDedup(bytes, filename, tagsCsv, url, metaJson, note) {
        let r = await postPdf(bytes, filename, tagsCsv, url, metaJson, false, note);
        if (r && r.ok === false && r.duplicate) {
            if (!confirmDuplicate(r.duplicate)) return null;
            r = await postPdf(bytes, filename, tagsCsv, url, metaJson, true, note);
        }
        return r;
    }

    // -- tag input widget (chips + autocomplete + suggestions) ------------
    //
    // Mirrors service/static/tags.js (the viewer pages' shared widget);
    // kept as its own copy because a userscript can't safely load scripts
    // from the service into arbitrary pages. Styles are applied via JS so
    // page CSP / CSS can't break them.

    let _allTagsCache = null;
    async function fetchAllTags() {
        if (_allTagsCache) return _allTagsCache;
        try {
            const resp = await gmFetch({ method: 'GET', url: `${SERVICE}/api/tags`, timeout: 8000 });
            if (resp.status === 200) {
                const j = JSON.parse(resp.responseText);
                _allTagsCache = (j.tags || []).map(t => ({ name: t.name, n: t.n }));
            } else _allTagsCache = [];
        } catch (_) { _allTagsCache = []; }
        return _allTagsCache;
    }

    async function fetchSuggestions(payload) {
        try {
            const resp = await gmFetch({
                method: 'POST', url: `${SERVICE}/api/tags/suggest`,
                headers: { 'Content-Type': 'application/json' },
                data: JSON.stringify(payload),
                timeout: 8000,
            });
            if (resp.status !== 200) return [];
            return (JSON.parse(resp.responseText).suggestions || []);
        } catch (_) { return []; }
    }

    // Ban a keyword so the server stops proposing it (and its case/punctuation
    // variants) as a tag suggestion. Fire-and-forget; the chip is removed
    // locally on click regardless of the round-trip.
    async function blockSuggestion(keyword) {
        try {
            await gmFetch({
                method: 'POST', url: `${SERVICE}/api/suggest/blocklist`,
                headers: { 'Content-Type': 'application/json' },
                data: JSON.stringify({ keyword }),
                timeout: 8000,
            });
        } catch (_) { /* offline / service down: nothing to do */ }
    }

    const WHY_MARK = { 'similar': '≈', 'predicted': '◎', 'co-occurring': '↔',
                       'mention': '¶', 'both': '★',
                       'keyword': '＋', 'code': '#' };
    // Legend shown under the suggestion chips so the glyph on each chip is
    // self-explanatory (where the suggestion came from).
    const WHY_LEGEND = '≈ similar   ◎ predicted   ↔ co-occurs   '
                     + '¶ in text   # designation   ★ multiple   ＋ new';

    // Prefix matches first, then usage count, then name.
    function rankTagMatches(all, frag, exclude) {
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

    // ctx: { title, text, host }. Returns { getTags, addTag, focus }.
    function createTagWidget(container, ctx) {
        const suggestRow = el('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '6px', margin: '0 0 6px', alignItems: 'center' } });
        const wrap = el('div', { style: { position: 'relative' } });
        const box = el('div', {
            style: {
                display: 'flex', flexWrap: 'wrap', gap: '4px', padding: '5px 6px',
                border: '1px solid #ccc', borderRadius: '4px', background: 'white',
                cursor: 'text', alignItems: 'center',
            },
            onclick: () => input.focus(),
        });
        const input = el('input', {
            type: 'text', autocomplete: 'off', placeholder: 'add tag…',
            style: {
                flex: '1', minWidth: '90px', border: 'none', outline: 'none',
                font: 'inherit', fontSize: '12px', padding: '3px 2px', background: 'transparent',
            },
        });
        const acList = el('div', {
            style: {
                position: 'absolute', left: '0', right: '0', top: '100%', zIndex: '2',
                background: 'white', border: '1px solid #ccc', borderTop: 'none',
                borderRadius: '0 0 4px 4px', maxHeight: '160px', overflowY: 'auto',
                display: 'none', boxShadow: '0 4px 10px rgba(0,0,0,0.08)',
            },
        });
        box.appendChild(input);
        wrap.append(box, acList);
        // Legend (hidden until there are suggestions) decoding the chip glyphs.
        const legend = el('div', {
            style: {
                fontSize: '10px', color: '#999', margin: '0 0 6px',
                display: 'none', lineHeight: '1.4',
            },
        }, WHY_LEGEND);
        container.append(suggestRow, legend, wrap);

        let tags = [];
        let allTags = [];
        let activeIdx = -1;
        let lastKey = null;
        let timer = null;
        const lcSet = () => new Set(tags.map(t => t.toLowerCase()));

        function renderChips() {
            box.querySelectorAll('.clipper-tag-chip').forEach(n => n.remove());
            for (const t of tags) {
                const x = el('span', {
                    style: { cursor: 'pointer', color: '#6b8cba', fontWeight: '600', padding: '0 4px', borderRadius: '8px' },
                    onclick: e => { e.stopPropagation(); removeTag(t); },
                }, '×');
                const chip = el('span', {
                    class: 'clipper-tag-chip',
                    style: {
                        display: 'inline-flex', alignItems: 'center', gap: '4px',
                        fontSize: '12px', background: '#e8f0fe', color: '#1a4480',
                        border: '1px solid #c4d7f5', borderRadius: '10px',
                        padding: '2px 4px 2px 9px',
                    },
                }, t, x);
                box.insertBefore(chip, input);
            }
        }
        function addTag(name) {
            name = (name || '').trim().replace(/,+$/, '').trim();
            if (!name || lcSet().has(name.toLowerCase())) return false;
            tags.push(name);
            renderChips(); scheduleSuggest();
            return true;
        }
        function removeTag(name) {
            tags = tags.filter(t => t !== name);
            renderChips(); scheduleSuggest();
            input.focus();
        }
        function commitInput() {
            for (const p of input.value.split(',')) addTag(p);
            input.value = '';
            closeAc();
        }

        function closeAc() { acList.style.display = 'none'; acList.innerHTML = ''; activeIdx = -1; }
        function renderAc(items) {
            if (!items.length) { closeAc(); return; }
            acList.innerHTML = '';
            items.forEach((t, i) => {
                const row = el('div', {
                    style: {
                        padding: '6px 10px', fontSize: '12px', cursor: 'pointer',
                        display: 'flex', justifyContent: 'space-between',
                        background: i === activeIdx ? '#eef6ff' : 'white',
                    },
                    onmousedown: e => {  // mousedown fires before blur
                        e.preventDefault();
                        addTag(t.name); input.value = ''; closeAc(); input.focus();
                    },
                },
                    el('span', {}, t.name),
                    el('span', { style: { color: '#999', fontSize: '11px' } }, String(t.n)));
                acList.appendChild(row);
            });
            acList.style.display = 'block';
        }
        function updateAc() {
            const frag = input.value.trim();
            if (!frag) { closeAc(); return; }
            activeIdx = -1;
            renderAc(rankTagMatches(allTags, frag, lcSet()));
        }
        function acceptActive() {
            const rows = acList.children;
            const idx = activeIdx >= 0 ? activeIdx : 0;
            if (!rows.length || idx >= rows.length) return false;
            addTag(rows[idx].firstChild.textContent);
            input.value = ''; closeAc();
            return true;
        }

        input.addEventListener('input', updateAc);
        input.addEventListener('keydown', e => {
            const rows = acList.children;
            const open = acList.style.display === 'block' && rows.length;
            const paint = () => { for (let i = 0; i < rows.length; i++) rows[i].style.background = i === activeIdx ? '#eef6ff' : 'white'; };
            if (e.key === 'ArrowDown' && open) { e.preventDefault(); activeIdx = (activeIdx + 1) % rows.length; paint(); }
            else if (e.key === 'ArrowUp' && open) { e.preventDefault(); activeIdx = (activeIdx - 1 + rows.length) % rows.length; paint(); }
            else if (e.key === 'Tab' && open && input.value.trim()) { e.preventDefault(); acceptActive(); }
            else if (e.key === 'Enter') {
                e.preventDefault();
                if (open && activeIdx >= 0) acceptActive(); else commitInput();
            }
            else if (e.key === ',') { e.preventDefault(); commitInput(); }
            else if (e.key === 'Escape') { closeAc(); }
            else if (e.key === 'Backspace' && !input.value && tags.length) { removeTag(tags[tags.length - 1]); }
        });
        // Synchronous: blur runs before the Save button's click handler, so
        // half-typed text becomes a chip instead of being silently dropped.
        input.addEventListener('blur', commitInput);

        function suggestKey() {
            // Include the note so editing it re-fetches (it feeds suggestions
            // like the title does).
            return JSON.stringify([tags.map(t => t.toLowerCase()).sort(),
                                   ctx.host || '', ctx.getNote ? ctx.getNote() : '']);
        }
        function scheduleSuggest() {
            clearTimeout(timer);
            timer = setTimeout(refreshSuggestions, 300);
        }
        async function refreshSuggestions() {
            const key = suggestKey();
            if (key === lastKey) return;
            lastKey = key;
            const sug = await fetchSuggestions({
                text: ctx.text || '', title: ctx.title || '', host: ctx.host || '',
                note: ctx.getNote ? ctx.getNote() : '',
                chosen: tags.slice(), limit: 14,
            });
            const chosen = lcSet();
            const filtered = sug.filter(s => !chosen.has(s.tag.toLowerCase()));
            suggestRow.innerHTML = '';
            legend.style.display = filtered.length ? 'block' : 'none';
            if (!filtered.length) return;
            suggestRow.appendChild(el('span', {
                style: { fontSize: '11px', color: '#888', marginRight: '4px' }
            }, 'Suggested:'));
            for (const s of filtered) {
                const isBoth = s.why === 'both';
                // Small ✕ that bans the keyword instead of adding it: posts to
                // the blocklist and drops the chip so it won't be offered again.
                const ban = el('span', {
                    title: 'Never suggest “' + s.tag + '” again',
                    style: {
                        marginLeft: '5px', color: '#b66', fontWeight: '600',
                        cursor: 'pointer', fontSize: '11px',
                    },
                    onclick: e => {
                        e.stopPropagation();
                        blockSuggestion(s.tag);
                        chip.remove();
                    },
                }, '✕');
                const chip = el('span', {
                    title: s.why,
                    style: {
                        fontSize: '12px', padding: '3px 9px', borderRadius: '12px',
                        background: isBoth ? '#e7f6ec' : '#eef',
                        color: isBoth ? '#1b5e2a' : '#335',
                        border: '1px solid ' + (isBoth ? '#b6e0c4' : '#ccd'),
                        cursor: 'pointer', userSelect: 'none',
                    },
                    onclick: () => { addTag(s.tag); input.focus(); },
                }, s.tag + ' ', el('span', { style: { color: '#88a', fontSize: '10px' } },
                                   WHY_MARK[s.why] || '≈'), ban);
                suggestRow.appendChild(chip);
            }
        }

        // Skip the auto-fetch on PDF pages: suggestions aren't meaningful for a
        // binary PDF, and on Chrome's native viewer the request can trip the
        // manager bridge. Manual tag entry still works.
        if (!looksLikePdf()) {
            fetchAllTags().then(a => { allTags = a; });
            refreshSuggestions();
        }

        return {
            getTags: () => tags.slice(),
            addTag,
            focus: () => input.focus(),
            refresh: scheduleSuggest,   // re-fetch after external context (note) changes
        };
    }

    // -- UI --------------------------------------------------------------

    // Make `panel` draggable by its `handle`. On first grab it switches the
    // panel from its centered transform to absolute left/top, then follows the
    // cursor (clamped to the viewport). After a real drag it swallows the click
    // the browser synthesizes, so the overlay's click-to-close doesn't fire.
    function makeDraggable(panel, handle) {
        handle.addEventListener('mousedown', e => {
            e.preventDefault();
            const rect = panel.getBoundingClientRect();
            panel.style.transform = 'none';
            panel.style.left = rect.left + 'px';
            panel.style.top = rect.top + 'px';
            const offX = e.clientX - rect.left;
            const offY = e.clientY - rect.top;
            let moved = false;
            const onMove = ev => {
                moved = true;
                const x = Math.max(0, Math.min(ev.clientX - offX, window.innerWidth - panel.offsetWidth));
                const y = Math.max(0, Math.min(ev.clientY - offY, window.innerHeight - panel.offsetHeight));
                panel.style.left = x + 'px';
                panel.style.top = y + 'px';
            };
            const onUp = () => {
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
                if (moved) {
                    const swallow = ev => { ev.stopPropagation(); document.removeEventListener('click', swallow, true); };
                    document.addEventListener('click', swallow, true);
                }
            };
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        });
    }

    let panelOpen = false;
    function openPanel() {
        if (panelOpen) return;
        // Re-check at open time in case the page mutated since load.
        const blocked = pageIsBlocked();
        if (blocked) {
            toast('Clipper is disabled on this page (' + (blocked.label || 'blocked') + ')', 'error');
            return;
        }
        panelOpen = true;

        // Snapshot the page selection NOW, before the modal steals focus.
        // Building the panel and focusing the tag input both collapse the
        // page selection, so reading it lazily at Save time always came back
        // empty - selection mode could never work through the modal. Capture
        // it once here and reuse the snapshot when the user saves.
        const selectionSnapshot = captureSelection();

        const overlay = el('div', {
            class: 'clipper-ui',
            style: {
                position: 'fixed', top: '0', left: '0', right: '0', bottom: '0',
                background: 'rgba(0,0,0,0.4)', zIndex: '2147483646',
                font: '13px -apple-system, BlinkMacSystemFont, sans-serif',
            },
        });
        const panel = el('div', {
            style: {
                position: 'fixed', top: '50%', left: '50%', transform: 'translate(-50%,-50%)',
                width: '400px', background: 'white', color: '#222',
                borderRadius: '8px', padding: '20px', maxHeight: '85vh', overflowY: 'auto',
                boxShadow: '0 12px 48px rgba(0,0,0,0.3)', zIndex: '2147483647',
            },
        });

        const isPdf = looksLikePdf();
        // Title doubles as the drag handle for moving the modal.
        const titleBar = el('div', {
            title: 'Drag to move',
            style: {
                fontWeight: '600', fontSize: '14px', marginBottom: '12px',
                cursor: 'move', userSelect: 'none',
            },
        }, isPdf ? 'Clip PDF' : 'Clip page');
        panel.appendChild(titleBar);
        makeDraggable(panel, titleBar);

        // Small field label; centralizes the shared style + font size.
        const fieldLabel = (text, extra) => el('label', {
            style: { display: 'block', fontSize: '11px', color: '#666', ...(extra || {}) },
        }, text);

        let modeSel;
        if (!isPdf) {
            panel.appendChild(fieldLabel('Mode'));
            // Only offer "Current selection" when there actually is one (the
            // snapshot above is non-null). If text was selected, default to
            // it - that's almost always why the user selected before clipping.
            const options = [
                el('option', { value: 'article' }, 'Article (extract main content)'),
                el('option', { value: 'full' }, 'Full page'),
            ];
            if (selectionSnapshot) {
                options.unshift(el('option', { value: 'selection' }, 'Current selection'));
            }
            modeSel = el('select', {
                style: { width: '100%', padding: '6px', marginBottom: '10px', fontSize: '12px' },
            }, ...options);
            panel.appendChild(modeSel);
        }

        let initialMeta = {};
        try { initialMeta = extractMetadata(); } catch (_) { /* editable manual fallback */ }
        const metadataHost = el('div');
        const captureMetadata = createMetadataEditor(metadataHost, {
            ...initialMeta, title: initialMeta.title || document.title || '', url: initialMeta.url || location.href,
        });
        panel.appendChild(metadataHost);
        panel.appendChild(fieldLabel('Tags'));

        // Chip-style tag widget (suggestion chips + chip input + autocomplete).
        // The input starts EMPTY - the previous behaviour of pre-filling the
        // last clip's tags silently carried them onto unrelated pages.
        // Instead, the last-used tags are offered as one-click "Recent" chips.
        const tagHost = el('div', {});
        panel.appendChild(tagHost);

        // Text context for suggestions. When the user has a selection (the
        // default mode in that case), key suggestions off the selected
        // passage - that's what's being clipped. Otherwise use the EXTRACTED
        // article + title (same cascade + boilerplate stripping as a real
        // clip), not the raw page, so nav/carousel/"more from" chrome can't
        // drag in unrelated tags. Cloning one content node is cheap; it falls
        // back to body text if extraction finds nothing.
        let suggestText, suggestTitle;
        if (selectionSnapshot) {
            const tmp = el('div');
            tmp.innerHTML = selectionSnapshot.html;
            suggestText = (tmp.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 4000);
            suggestTitle = document.title || '';
        } else {
            const ctx = articleSuggestContext(4000);
            suggestText = ctx.text;
            suggestTitle = ctx.title;
        }
        // User note: a free-form comment stored with the clip. It feeds tag
        // suggestions (treated like the title) and leads the clip in search
        // exports. Created before the widget so the widget can read it live.
        const noteInput = el('textarea', {
            rows: '2', placeholder: 'Optional note — saved with the clip, '
                + 'used for tag suggestions, and shown atop search exports',
            style: {
                width: '100%', boxSizing: 'border-box', marginTop: '4px',
                padding: '6px', font: 'inherit', fontSize: '12px', resize: 'vertical',
                border: '1px solid #ccc', borderRadius: '4px',
            },
        });

        const tagWidget = createTagWidget(tagHost, {
            title: suggestTitle, text: suggestText, host: location.hostname,
            getNote: () => noteInput.value.trim(),
        });
        // Re-suggest as the note is typed (debounced inside the widget).
        noteInput.addEventListener('input', () => tagWidget.refresh());

        const recentTags = (GM_getValue(LS_LAST_TAGS, '') || '')
            .split(',').map(s => s.trim()).filter(Boolean);
        if (recentTags.length) {
            const recentRow = el('div', {
                style: { display: 'flex', flexWrap: 'wrap', gap: '6px', margin: '6px 0 0', alignItems: 'center' },
            }, el('span', { style: { fontSize: '11px', color: '#888', marginRight: '4px' } }, 'Recent:'));
            for (const t of recentTags) {
                recentRow.appendChild(el('span', {
                    style: {
                        fontSize: '12px', padding: '3px 9px', borderRadius: '12px',
                        background: '#f4f4f8', color: '#555', border: '1px dashed #bbb',
                        cursor: 'pointer', userSelect: 'none',
                    },
                    onclick: () => tagWidget.addTag(t),
                }, t));
            }
            panel.appendChild(recentRow);
        }

        panel.appendChild(fieldLabel('Note', { marginTop: '10px' }));
        panel.appendChild(noteInput);

        const status = el('div', {
            style: { marginTop: '12px', fontSize: '11px', color: '#666', minHeight: '16px' }
        });
        panel.appendChild(status);

        const btnRow = el('div', {
            style: { marginTop: '14px', display: 'flex', gap: '8px', justifyContent: 'flex-end' }
        });
        const cancelBtn = el('button', {
            style: {
                padding: '7px 12px', fontSize: '12px', background: '#eee', border: 'none',
                borderRadius: '4px', cursor: 'pointer'
            },
            onclick: closePanel,
        }, 'Cancel');
        const clipBtn = el('button', {
            style: {
                padding: '7px 12px', fontSize: '12px', background: '#06c', color: 'white',
                border: 'none', borderRadius: '4px', cursor: 'pointer', fontWeight: '600'
            },
            onclick: doClip,
        }, isPdf ? 'Save PDF' : 'Save clip');
        btnRow.append(cancelBtn, clipBtn);
        panel.appendChild(btnRow);

        overlay.appendChild(panel);
        overlay.addEventListener('click', e => { if (e.target === overlay) closePanel(); });
        document.body.appendChild(overlay);
        tagWidget.focus();

        function closePanel() { overlay.remove(); panelOpen = false; }

        async function doClip() {
            clipBtn.disabled = true; cancelBtn.disabled = true;
            status.style.color = '#666';
            const tags = tagWidget.getTags();
            // Remember for the "Recent" chips - but only when tags were
            // actually used, so an untagged save doesn't wipe the list.
            if (tags.length) GM_setValue(LS_LAST_TAGS, tags.join(', '));
            try {
                if (isPdf) {
                    status.textContent = 'Fetching PDF…';
                    const bytes = await fetchPdfBytes();
                    status.textContent = `Uploading ${(bytes.length / 1024).toFixed(0)} KB…`;
                    const filename = location.pathname.split('/').pop() || 'document.pdf';
                    let pdfMeta = {};
                    try { pdfMeta = extractMetadata(); }
                    catch (e) { console.warn('clipper: metadata extraction failed (PDF)', e); }
                    pdfMeta = { ...pdfMeta, ...captureMetadata.values() };
                    const r = await postPdfDedup(bytes, filename, tags.join(','), pdfMeta.url,
                                                 JSON.stringify(pdfMeta), noteInput.value.trim());
                    if (!r) { status.textContent = ''; closePanel(); toast('Skipped (duplicate)'); return; }
                    closePanel();
                    const markStr = pdfMeta.marking ? ` [${pdfMeta.marking}]` : '';
                    toast(`Saved as clip #${r.id}${markStr} (click to open library)`);
                } else {
                    const mode = modeSel.value;
                    status.textContent = 'Capturing…';
                    let cap;
                    if (mode === 'full') cap = captureFull();
                    else if (mode === 'selection') cap = selectionSnapshot;
                    else cap = captureArticle();
                    if (!cap) throw new Error('Nothing to clip - is anything selected?');

                    status.textContent = 'Inlining images…';
                    const { html, inlined, total, linksRemoved } = await inlineImages(cap.html, location.href);
                    status.textContent =
                        `Inlined ${inlined}/${total} images, stripped ${linksRemoved} links. Sending…`;

                    // Run metadata extraction against the live page (not the
                    // cloned content node) so it sees the whole document.
                    let extractedMeta = {};
                    try { extractedMeta = extractMetadata(); }
                    catch (e) { console.warn('clipper: metadata extraction failed', e); }

                    const r = await postClipDedup({
                        html, title: captureMetadata.values().title || cap.title, url: captureMetadata.values().url,
                        source: 'userscript-' + mode, tags,
                        note: noteInput.value.trim(),
                        meta: {
                            capture_mode: mode,
                            host: location.host,
                            extractor: cap.extractor || null,
                            links_removed: linksRemoved,
                            images_inlined: inlined,
                            ...extractedMeta,
                            ...captureMetadata.values(),
                        },
                    });
                    if (!r) { status.textContent = ''; closePanel(); toast('Skipped (duplicate)'); return; }
                    closePanel();
                    const markStr = extractedMeta.marking ? ` [${extractedMeta.marking}]` : '';
                    toast(`Saved as clip #${r.id}${markStr}` +
                          (cap.extractor ? ` via ${cap.extractor}` : ''));
                }
            } catch (e) {
                status.textContent = String(e);
                status.style.color = '#c0392b';
                clipBtn.disabled = false; cancelBtn.disabled = false;
            }
        }
    }

    function injectButton() {
        const btn = el('div', {
            id: 'clipper-launcher', class: 'clipper-ui', title: 'Clip this page',
            style: {
                position: 'fixed', bottom: '20px', right: '20px',
                width: '44px', height: '44px', borderRadius: '50%',
                background: '#06c', color: 'white',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                fontWeight: '600', cursor: 'pointer', userSelect: 'none',
                boxShadow: '0 4px 12px rgba(0,0,0,0.2)', zIndex: '2147483645',
                font: '20px sans-serif',
            },
            // preventDefault on mousedown so clicking the launcher doesn't
            // collapse the page's text selection - selection mode depends on
            // it still being present when openPanel() snapshots it.
            onmousedown: e => e.preventDefault(),
            onclick: openPanel,
        }, '📎');
        document.body.appendChild(btn);
    }

    // One-shot PDF clip with no dialog, for environments where the panel can't
    // be shown — Chrome/Edge paint the native PDF viewer over page content, so
    // the launcher and modal are invisible there. Fetches the PDF via the
    // manager (carrying this origin's cookies, so authed docs work) and posts
    // it. No tags here; add them later in the viewer where suggestions help.
    async function quickClipPdf() {
        const blocked = pageIsBlocked();
        if (blocked) {
            toast('Clipper is disabled on this page (' + (blocked.label || 'blocked') + ')', 'error');
            return;
        }
        toast('Fetching PDF…');
        try {
            const bytes = await fetchPdfBytes();
            const filename = location.pathname.split('/').pop() || 'document.pdf';
            let pdfMeta = {};
            try { pdfMeta = extractMetadata(); }
            catch (e) { console.warn('clipper: metadata extraction failed (PDF)', e); }
            const r = await postPdfDedup(bytes, filename, '', location.href, JSON.stringify(pdfMeta), '');
            if (!r) { toast('Skipped (duplicate)'); return; }
            const markStr = pdfMeta.marking ? ` [${pdfMeta.marking}]` : '';
            toast(`Saved as clip #${r.id}${markStr}`);
            // No tag widget in this flow - offer suggestions to add post-save.
            // (A no-op where the DOM is hidden, e.g. a native PDF viewer.)
            if (r.id) showSuggestionChips(r.id);
        } catch (e) {
            console.error('clipper: quick PDF clip failed', e);
            toast('Failed: ' + e.message, 'error');
        }
    }

    // Entry point. If the page is blocked, inject nothing and register a
    // menu command that just explains why.
    function boot() {
        const blocked = pageIsBlocked();
        if (blocked) {
            GM_registerMenuCommand('Clipper disabled here (' + (blocked.label || 'blocked') + ')',
                () => toast('Clipper is disabled on this page by a block rule.', 'error'));
            return;
        }
        injectButton();
        installLinkContextMenu();
        GM_registerMenuCommand('Clip this page', openPanel);
        // PDF pages need a path that doesn't depend on the (possibly invisible)
        // overlay UI, so always offer a no-dialog clip from the manager menu.
        if (looksLikePdf()) {
            GM_registerMenuCommand('Clip this PDF now (no dialog)', quickClipPdf);
        }
        GM_registerMenuCommand('Open library', () => gmOpenInTab(SERVICE + '/'));
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
})();
