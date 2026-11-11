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
    monkeypatch.setattr(app.ingest_mod, 'extract_pdf_metadata', lambda raw, text: {})
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
