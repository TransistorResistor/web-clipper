"""Configurable marking categories. No database schema changes required."""
import json
import os
from pathlib import Path
import re


def key(value):
    return ' '.join(value.split()).casefold()


def load(path=None):
    global CONFIG, LOOKUP, SPLITTER
    path = Path(path or os.environ.get('CLIPPER_MARKINGS', Path(__file__).with_name('markings.json')))
    data = json.loads(path.read_text(encoding='utf-8'))
    delimiters = data.get('delimiters')
    if not isinstance(delimiters, list) or not delimiters or any(not isinstance(x, str) or not x for x in delimiters):
        raise ValueError('markings.json delimiters must be a nonempty array of nonempty literal strings')
    categories = data.get('categories')
    if not isinstance(categories, dict):
        raise ValueError('markings.json categories must be an object')
    lookup = {}
    for name, spec in categories.items():
        if not re.fullmatch(r'[a-z][a-z0-9_]*', name) or name in ('marking', 'title', 'url', 'tags', 'tag', 'published', 'report', 'date_published', 'report_number', 'marking_norm', 'marking_adjusted', 'source', 'note'):
            raise ValueError(f'Invalid or reserved category key: {name}')
        if not isinstance(spec, dict) or not isinstance(spec.get('label', name), str) or not isinstance(spec.get('values'), dict):
            raise ValueError(f'Category {name} requires a values object and a text label')
        for canonical, aliases in spec['values'].items():
            if not canonical.strip() or canonical.startswith('@') or any(d in canonical for d in delimiters):
                raise ValueError(f'Invalid canonical value in {name}: {canonical}')
            if not isinstance(aliases, list) or any(not isinstance(a, str) or not a.strip() or any(d in a for d in delimiters) for a in aliases):
                raise ValueError(f'Aliases for {canonical} must be strings without delimiters')
            for spelling in [canonical, *aliases]:
                token = key(spelling)
                if token in lookup and lookup[token] != (name, canonical):
                    raise ValueError(f'Ambiguous marking alias: {spelling}')
                lookup[token] = (name, canonical)
    CONFIG, LOOKUP = data, lookup
    SPLITTER = re.compile('|'.join(re.escape(x) for x in sorted(set(delimiters), key=len, reverse=True)))


def categorize(text):
    categories, unmapped, tokens = {}, [], []
    seen = set()
    for token in SPLITTER.split(text):
        token = token.strip()
        if not token:
            continue
        match = LOOKUP.get(key(token))
        name, canonical = match if match else ('_unmapped', token)
        identity = (name, key(canonical))
        if identity in seen:
            continue
        seen.add(identity)
        tokens.append(canonical)
        if match:
            categories.setdefault(name, []).append(canonical)
        else:
            unmapped.append(canonical)
    return {'adjusted': CONFIG['delimiters'][0].join(tokens),
            'categories': categories, 'unmapped': unmapped, 'tokens': tokens}


def values(meta, category):
    norm = meta.get('marking_norm') or {}
    return norm.get('unmapped', []) if category == '_unmapped' else norm.get('categories', {}).get(category, [])


def filters(raw):
    data = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
    if not isinstance(data, dict):
        raise ValueError('marking_filters must be an object of category: [values]')
    for category, selected in data.items():
        if category not in CONFIG['categories'] and category != '_unmapped':
            raise ValueError(f'Unknown marking category: {category}')
        if not isinstance(selected, list) or any(not isinstance(v, str) for v in selected):
            raise ValueError('Each marking category filter must be an array of strings')
        if any(v.startswith('@') and v not in ('@present', '@missing') for v in selected):
            raise ValueError('Unknown marking presence filter')
    return data


def sql_filters(selected, alias='c'):
    """OR within categories, AND across categories; bound JSON paths and values."""
    clauses, params = [], []
    for category, choices in selected.items():
        if not choices:
            continue
        path = '$.marking_norm.unmapped' if category == '_unmapped' else '$.marking_norm.categories.' + category
        meta = f"CASE WHEN json_valid({alias}.meta_json) THEN {alias}.meta_json ELSE '{{}}' END"
        options = []
        for choice in choices:
            expression = f'EXISTS (SELECT 1 FROM json_each({meta}, ?) mv'
            params.append(path)
            if choice in ('@present', '@missing'):
                expression += ')'
                if choice == '@missing':
                    expression = 'NOT ' + expression
            else:
                canonical = LOOKUP.get(key(choice), ('', choice))[1]
                expression += ' WHERE mv.value = ? COLLATE NOCASE)'
                params.append(canonical)
            options.append(expression)
        clauses.append('(' + ' OR '.join(options) + ')')
    return ''.join(' AND ' + clause for clause in clauses), params


load()
