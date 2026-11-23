"""End-to-end marking schema, metadata override and category filter coverage."""
import json
import os
import tempfile
os.environ['CLIPPER_DB'] = tempfile.mktemp(suffix='.db')
from db import connect, init_db
init_db()
import app
import backfill_norm
import marking_rules
import marking_schema as M
import normalize
import pytest


def test_categories_aliases_delimiters_and_unknown():
    meta = {'marking':' project a | CLIENT c ; 456 | mystery | Client 3 '}
    normalize.apply_normalization(meta)
    n = meta['marking_norm']
    assert n['categories'] == {'projects':['Project A'], 'clients':['Client 3'], 'cost_centres':['456']}
    assert n['unmapped'] == ['mystery']
    assert n['adjusted'] == 'Project A|Client 3|456|mystery'
    assert meta['marking'].startswith(' project a ')


def test_schema_validation_and_literal_multi_character_delimiters(tmp_path):
    original = M.CONFIG
    config = {'delimiters':['||','.'], 'categories':{'teams':{'label':'Teams','values':{'Alpha':['A']}}}}
    path = tmp_path/'schema.json'
    try:
        path.write_text(json.dumps(config))
        M.load(path)
        assert M.categorize('a||other.more')['tokens'] == ['Alpha','other','more']
        config['categories']['teams']['values']['Beta'] = ['a']
        path.write_text(json.dumps(config))
        with pytest.raises(ValueError, match='Ambiguous'):
            M.load(path)
        config['delimiters'] = ['']
        path.write_text(json.dumps(config))
        with pytest.raises(ValueError, match='delimiters'):
            M.load(path)
    finally:
        M.load()


def test_hook_and_override_preservation(monkeypatch):
    monkeypatch.setattr(marking_rules,'normalize_marking_text',lambda raw:'project b|client c')
    meta = {'marking':'source text'}
    normalize.apply_normalization(meta)
    assert meta['marking_norm']['adjusted'] == 'Project B|Client 3'
    meta['marking_adjusted'] = 'project c|789'
    normalize.apply_normalization(meta)
    assert meta['marking_norm']['categories'] == {'projects':['Project C'], 'cost_centres':['789']}
    assert meta['marking_norm']['manual']
    assert meta['marking_norm']['raw'] == 'source text'
    meta['marking_adjusted'] = ''
    normalize.apply_normalization(meta)
    assert meta['marking_norm']['tokens'] == []


def test_api_filters_overrides_and_backfill():
    c = app.app.test_client()
    def create(marking):
        response = c.post('/api/clip',json={'title':'report', 'html':'<p>schema filter test</p>', 'meta':{'marking':marking}, 'force':True})
        assert response.status_code == 200, response.get_json()
        return response.get_json()['id']
    a = create('project a|client c|456')
    b = create('project b|client 1')
    d = create('unmapped-token')
    def ids(filters, **other):
        response = c.get('/api/clips',query_string={'marking_filters':json.dumps(filters), **other})
        assert response.status_code == 200, response.get_json()
        return [x['id'] for x in response.get_json()['clips']]
    selected = {'projects':['Project A','Project B'], 'clients':['Client C']}
    assert a in ids(selected) and b not in ids(selected)
    assert b in ids({'cost_centres':['@missing']})
    assert a not in ids({'cost_centres':['@missing']})
    assert d in ids({'_unmapped':['@present']})
    assert a in [x['id'] for x in c.get('/api/clips?has=projects').get_json()['clips']]
    filtered = c.get('/api/search', query_string={'q':'schema','marking_filters':json.dumps(selected)}).get_json()
    assert filtered['count'] == 1, filtered
    assert 'Client 3' in c.get(f'/api/clips/{a}').get_json()['marking_display']
    assert c.patch(f'/api/clips/{a}',json={'meta':{'marking_adjusted':'project b|client 2'}}).status_code == 200
    assert a not in ids(selected)
    assert a in ids({'clients':['Client 2']})
    assert c.patch(f'/api/clips/{a}',json={'meta':{'marking':'project c|client c'}}).status_code == 200
    assert c.get(f'/api/clips/{a}').get_json()['marking_display'] == 'Project B|Client 2'
    conn = connect()
    try:
        backfill_norm.backfill(conn)
        assert c.get(f'/api/clips/{a}').get_json()['marking_display'] == 'Project B|Client 2'
    finally:
        conn.close()
    assert c.patch(f'/api/clips/{a}',json={'meta':{'marking_adjusted':None}}).status_code == 200
    assert c.get(f'/api/clips/{a}').get_json()['marking_display'] == 'Project C|Client 3'
    result = c.get('/api/search',query_string={'q':'marking:"Client C"'}).get_json()
    assert result['count'] >= 1
    assert c.get('/api/clips',query_string={'marking_filters':'{"bogus":["x"]}'}).status_code == 400
    assert c.get('/api/search',query_string={'q':'schema','marking_filters':'invalid'}).status_code == 400
    schema = c.get('/api/marking-schema').get_json()
    assert schema['delimiters'] == ['|',';']
    assert 'unmapped-token' in next(x for x in schema['categories'] if x['key']=='_unmapped')['values']
