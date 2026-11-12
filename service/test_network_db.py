"""Network DB snapshots, locking, worker sync and crash cleanup regressions."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest
import network_db as N


def make_db(path, text='initial'):
    c = sqlite3.connect(path)
    c.execute('CREATE TABLE IF NOT EXISTS items(value TEXT)')
    c.execute('INSERT INTO items VALUES (?)', (text,))
    c.commit()
    c.close()


def values(path):
    c = sqlite3.connect(path)
    try:
        return [r[0] for r in c.execute('SELECT value FROM items')]
    finally:
        c.close()


def wait_for(predicate, timeout=15):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('Timed out')


def test_network_detection():
    assert N.is_network_path(r'\\server\share\clipper.db')
    assert N.is_network_path('//server/share/clipper.db')
    assert not N.is_network_path(str(Path.cwd() / 'test-local.db'))


def test_consistent_snapshot_with_wal(tmp_path):
    local, remote = tmp_path/'local.db', tmp_path/'remote.db'
    make_db(local)
    c = sqlite3.connect(local)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute("INSERT INTO items VALUES ('committed in WAL')")
    c.commit()
    digest = N.publish_snapshot(local, remote, None)
    assert digest == N.fingerprint(remote)
    assert values(remote) == ['initial', 'committed in WAL']
    check = sqlite3.connect(remote)
    assert check.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
    check.close()
    c.close()


def test_external_change_not_overwritten(tmp_path):
    local, remote = tmp_path/'local.db', tmp_path/'remote.db'
    make_db(local)
    old = N.publish_snapshot(local, remote, None)
    c = sqlite3.connect(remote)
    c.execute("INSERT INTO items VALUES ('external')")
    c.commit()
    c.close()
    with pytest.raises(RuntimeError, match='changed outside'):
        N.publish_snapshot(local, remote, old)
    assert values(remote) == ['initial', 'external']
    assert not list(tmp_path.glob('*.upload-*'))


def test_copy_failure_keeps_last_snapshot(tmp_path, monkeypatch):
    local, remote = tmp_path/'local.db', tmp_path/'remote.db'
    make_db(local)
    old = N.publish_snapshot(local, remote, None)
    def fail(*args, **kwargs):
        raise OSError('Disconnected')
    monkeypatch.setattr(N.shutil, 'copyfileobj', fail)
    with pytest.raises(OSError):
        N.publish_snapshot(local, remote, old)
    assert N.fingerprint(remote) == old
    assert not list(tmp_path.glob('*.upload-*'))


def test_lock_excludes_second_writer(tmp_path):
    with N.FileLock(tmp_path/'test.lock'):
        with pytest.raises(RuntimeError):
            with N.FileLock(tmp_path/'test.lock'):
                pass


def test_reject_live_remote_wal(tmp_path):
    remote = tmp_path/'remote.db'
    make_db(remote)
    c = sqlite3.connect(remote)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute("INSERT INTO items VALUES ('wal')")
    c.commit()
    with pytest.raises(RuntimeError, match='WAL/journal'):
        N.check_standalone(remote)
    c.close()


def test_worker_batches_and_cleans_on_parent_death(tmp_path):
    remote, cache = tmp_path/'remote.db', tmp_path/'cache'
    make_db(remote)
    cache.mkdir()
    (cache/'old-sensitive.db').write_bytes(b'stale')
    parent = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    script = 'import network_db as n; n.worker(*__import__("sys").argv[1:3], int(__import__("sys").argv[3]), delay=.3, max_delay=.6, poll=.05)'
    proc = subprocess.Popen([sys.executable, '-c', script, str(remote), str(cache), str(parent.pid)], cwd=Path(N.__file__).parent)
    try:
        wait_for(lambda: (cache/'status.json').exists())
        assert not (cache/'old-sensitive.db').exists()
        c = sqlite3.connect(cache/'library.db')
        c.execute("INSERT INTO items VALUES ('batched')")
        c.commit()
        c.close()
        wait_for(lambda: values(remote) == ['initial', 'batched'])
        c = sqlite3.connect(cache/'library.db')
        c.execute("INSERT INTO items VALUES ('final')")
        c.commit()
        c.close()
        parent.kill()
        parent.wait(timeout=5)
        proc.wait(timeout=15)
        assert proc.returncode == 0
        assert values(remote) == ['initial', 'batched', 'final']
        assert not cache.exists()
    finally:
        for child in (parent, proc):
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_manager_graceful_shutdown(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'appdata'))
    remote = tmp_path/'remote.db'
    make_db(remote)
    manager = N.NetworkDatabase(str(remote))
    try:
        local = manager.start()
        assert manager.status()['worker_alive']
        c = sqlite3.connect(local)
        c.execute("INSERT INTO items VALUES ('shutdown')")
        c.commit()
        c.close()
    finally:
        manager.stop()
    assert values(remote) == ['initial', 'shutdown']
    assert not manager.cache.exists()


def test_mapped_drive_detection(monkeypatch):
    if os.name != 'nt':
        pytest.skip('Windows mapped-drive API')
    monkeypatch.setattr(N.ctypes.windll.kernel32, 'GetDriveTypeW', lambda root: 4)
    assert N.is_network_path(r'E:\library.db')


def test_final_sync_failure_still_deletes_cache(tmp_path, monkeypatch):
    remote, cache = tmp_path/'remote.db', tmp_path/'cache'
    make_db(remote)
    class DeadParent:
        def __init__(self, pid): pass
        def alive(self): return False
        def close(self): pass
    def fail(*args):
        raise OSError('Network disconnected')
    monkeypatch.setattr(N, 'ParentProcess', DeadParent)
    monkeypatch.setattr(N, 'publish_snapshot', fail)
    N.worker(remote, cache, os.getpid())
    assert not cache.exists()
    assert values(remote) == ['initial']


def test_unavailable_share_discards_stale_cache(tmp_path):
    cache = tmp_path/'cache'
    cache.mkdir()
    (cache/'library.db').write_bytes(b'unsynced leftover')
    with pytest.raises(OSError):
        N.worker(tmp_path/'disconnected'/'remote.db', cache, os.getpid())
    assert not cache.exists()


def test_second_manager_cannot_delete_active_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'appdata'))
    remote = tmp_path/'remote.db'
    make_db(remote)
    first = N.NetworkDatabase(str(remote))
    second = N.NetworkDatabase(str(remote))
    try:
        local = first.start()
        with pytest.raises(RuntimeError, match='worker failed'):
            second.start()
        assert Path(local).exists()
        assert values(local) == ['initial']
    finally:
        first.stop()
        second.stop()
