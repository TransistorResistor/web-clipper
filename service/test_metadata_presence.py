import os, tempfile, io
os.environ['CLIPPER_DB'] = tempfile.mktemp(suffix='.db')
from db import init_db, connect
init_db()
import app, search

def test_edit_presence():
    c = app.app.test_client()
    a = c.post('/api/clip', json={'title':'Old', 'html':'<p>metadata regression</p>', 'tags':['sample'], 'meta':{'custom':'keep','report_number':'R1'}}).get_json()['id']
    assert c.patch(f'/api/clips/{a}', json={'title':'New', 'meta':{'report_number':''}}).status_code == 200
    d = c.get(f'/api/clips/{a}').get_json()
    assert d['meta']['title'] == d['title'] == 'New'
    assert d['meta']['custom'] == 'keep'
    assert a in [x['id'] for x in c.get('/api/clips?has=tags&missing=report_number').get_json()['clips']]
    assert a not in [x['id'] for x in c.get('/api/clips?missing=tags').get_json()['clips']]
    conn = connect()
    for q in ('has:tags AND missing:report_number','has:custom'):
        assert a in [x['doc'].clip_id for x in search.search(conn,q,search.SearchOptions())['results']]
    conn.close()

def test_pdf_no_remember(monkeypatch):
    raw = b'%PDF-test-correction-flow'
    monkeypatch.setattr(app.search_engine, 'pdf_to_text', lambda raw: 'PDF text')
    monkeypatch.setattr(app.ingest_mod, 'extract_pdf_metadata', lambda raw, text, *, filename: {})
    conn = connect()
    conn.execute("INSERT OR REPLACE INTO metadata_corrections(scope, field, value, updated_at) VALUES ('ext:pdf', 'title', 'Wrong remembered title', 0)")
    conn.commit()
    conn.close()
    c = app.app.test_client()
    r = c.post('/api/upload/preview',data={'file':(io.BytesIO(raw),'fresh.pdf')},content_type='multipart/form-data')
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d['remembered_fields'] == []
    assert c.post('/api/upload/commit',json={'token':d['token'],'fields':{'title':'Only this PDF'}}).status_code == 200
    conn = connect()
    assert conn.execute("SELECT count(*) FROM metadata_corrections WHERE value = 'Only this PDF'").fetchone()[0] == 0
    conn.close()


def test_custom_adjusted_marking_visible_searchable_filterable(monkeypatch):
    import marking_rules
    import normalize
    import backfill_norm
    monkeypatch.setattr(marking_rules, 'normalize_marking_text',
                        lambda value: 'Project Alpha' if value == 'RAW123' else value)
    c = app.app.test_client()
    a = c.post('/api/clip', json={'title':'Custom marking', 'html':'<p>new report</p>',
                                'meta':{'marking':'RAW123'}}).get_json()['id']
    detail = c.get(f'/api/clips/{a}').get_json()
    assert detail['marking_display'] == 'Project Alpha'
    assert detail['meta']['marking'] == 'RAW123'
    assert detail['meta']['marking_norm']['classification'] is None
    assert 'Project Alpha' in [x['name'] for x in c.get('/api/markings').get_json()['markings']]
    assert a in [x['id'] for x in c.get('/api/clips', query_string={'marking':'Project Alpha'}).get_json()['clips']]
    assert a in [x['id'] for x in c.get('/api/clips?marking=*').get_json()['clips']]
    conn = connect()
    try:
        for q in ('marking:"Project Alpha"', 'marking:RAW123'):
            result = search.search(conn, q, search.SearchOptions())
            assert a in [x['doc'].clip_id for x in result['results']]
        monkeypatch.setattr(marking_rules, 'normalize_marking_text',
                            lambda value: 'Project Beta' if value == 'RAW123' else value)
        backfill_norm.backfill(conn)
        assert c.get(f'/api/clips/{a}').get_json()['marking_display'] == 'Project Beta'
        monkeypatch.setattr(marking_rules, 'normalize_marking_text', lambda value: '')
        backfill_norm.backfill(conn)
        assert c.get(f'/api/clips/{a}').get_json()['marking_display'] == ''
        assert a not in [x['id'] for x in c.get('/api/clips?marking=*').get_json()['clips']]
    finally:
        conn.close()

def test_metadata_search_without_fulltext_hit():
    c = app.app.test_client()
    clip_id = c.post('/api/clip', json={'title':'Quiet library', 'html':'<p>ordinary words</p>',
        'url':'https://unique.example/abc', 'meta':{'marking':'Client C'}, 'force':True}).get_json()['id']
    conn = connect()
    try:
        for q in ('url:unique.example', 'title:uiet', 'zebraword OR marking:"Client C"',
                  'zebraword OR NOT tag:missing'):
            assert clip_id in [r['doc'].clip_id for r in search.search(conn, q, search.SearchOptions())['results']], q
    finally:
        conn.close()


def test_pdf_upload_marking_hook(monkeypatch):
    import sys
    from types import SimpleNamespace

    import pdf_marking_rules

    text_head = ("Mixed case PDF text. " * 100)[:1500]
    monkeypatch.setattr(app.search_engine, "pdf_to_text", lambda raw: text_head + "outside hook window")
    metadata = {"title": "Embedded report"}
    monkeypatch.setitem(sys.modules, "fitz", SimpleNamespace(open=lambda **kwargs: SimpleNamespace(
        metadata=metadata, page_count=1, close=lambda: None,
    )))
    calls = []

    def extract_marking(title, text, filename):
        calls.append((title, text, filename))
        return "project a|client C"

    monkeypatch.setattr(pdf_marking_rules, "extract_pdf_marking", extract_marking)
    client = app.app.test_client()
    preview = client.post("/api/upload/preview", data={
        "file": (io.BytesIO(b"%PDF-marking-preview"), r"C:\reports\preview.pdf"),
    }).get_json()
    assert preview["editable"]["marking"] == "project a|client C"
    committed = client.post("/api/upload/commit", json={
        "token": preview["token"], "fields": {"marking": "project b"},
    }).get_json()
    assert client.get(f"/api/clips/{committed['id']}").get_json()["marking_display"] == "Project B"

    metadata.clear()
    bulk = client.post("/api/upload/bulk", data={
        "file": (io.BytesIO(b"%PDF-marking-bulk"), "bulk.pdf"),
    }).get_json()
    saved = client.get(f"/api/clips/{bulk['id']}").get_json()
    assert saved["marking_display"] == "Project A|Client 3"
    assert calls == [("Embedded report", text_head, "preview.pdf"), ("bulk.pdf", text_head, "bulk.pdf")]
