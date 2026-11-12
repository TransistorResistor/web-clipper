"""Local working databases for network paths. Run this module as a watchdog.

Network copies are standalone rollback-mode snapshots; SQLite only opens local
files. A separate worker owns the cross-machine lock and outlives the service.
"""
from __future__ import annotations

import atexit
import ctypes
import hashlib
import json
import logging
import ntpath
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

log = logging.getLogger('clipper.network_db')


def is_network_path(path: str) -> bool:
    if path.startswith(('\\\\', '//')):
        return True
    if os.name == 'nt':
        drive = ntpath.splitdrive(os.path.abspath(path))[0]
        return bool(drive and ctypes.windll.kernel32.GetDriveTypeW(drive + '\\') == 4)
    return False


class FileLock:
    """OS-backed lock; the sidecar stays, but locks release on worker death."""
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.file = open(self.path, 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0)
                if not self.file.read(1):
                    self.file.write(b'0')
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self.file.close()
            self.file = None
            raise RuntimeError(f'Library already in use, or lock unavailable: {self.path}') from e
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()
            self.file = None


def write_json(path, value):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps({**value, "worker_pid": os.getpid()}), encoding='utf-8')
    os.replace(temp, path)


def fingerprint(path):
    try:
        digest = hashlib.sha256()
        with open(path, 'rb') as f:
            for block in iter(lambda: f.read(1024 * 1024), b''):
                digest.update(block)
        return digest.hexdigest()
    except FileNotFoundError:
        return None


def check_standalone(remote):
    for suffix in ('-wal', '-journal'):
        sidecar = Path(str(remote) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise RuntimeError('Network DB has a live WAL/journal. Stop all users and prepare a standalone local SQLite backup before using network caching.')


def validate_db(path):
    conn = sqlite3.connect(path)
    try:
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('Database integrity check failed')
        # Never import a WAL-mode file as a network snapshot.
        conn.execute('PRAGMA journal_mode = DELETE')
    finally:
        conn.close()


def publish_snapshot(local, remote, expected):
    """Backup locally, copy to a unique network temp, then replace destination.

    Keep the prior network snapshot untouched on copy/validation failure.
    Detect edits by tools that do not honour our lock before replacing it.
    """
    local, remote = Path(local), Path(remote)
    snapshot = local.parent / 'snapshot.db'
    temp = remote.with_name(remote.name + '.upload-' + uuid.uuid4().hex)
    try:
        if snapshot.exists():
            snapshot.unlink()
        src = sqlite3.connect(local, timeout=10)
        dst = sqlite3.connect(snapshot)
        try:
            src.backup(dst, pages=256, sleep=0.05)
            dst.execute('PRAGMA journal_mode = DELETE')
        finally:
            dst.close()
            src.close()
        validate_db(snapshot)
        check_standalone(remote)
        if fingerprint(remote) != expected:
            raise RuntimeError('Network DB changed outside this app; refusing to overwrite it')
        with open(snapshot, 'rb') as source, open(temp, 'xb') as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        if fingerprint(temp) != fingerprint(snapshot):
            raise RuntimeError('Network snapshot verification failed')
        check_standalone(remote)
        if fingerprint(remote) != expected:
            raise RuntimeError('Network DB changed during sync; refusing to overwrite it')
        os.replace(temp, remote)
        return fingerprint(snapshot)
    finally:
        snapshot.unlink(missing_ok=True)
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            log.exception('Could not remove incomplete network upload %s', temp)


class ParentProcess:
    def __init__(self, pid):
        self.pid = pid
        self.handle = None
        if os.name == 'nt':
            from ctypes import wintypes
            self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.kernel.OpenProcess.restype = wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.kernel.WaitForSingleObject.restype = wintypes.DWORD
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.handle = self.kernel.OpenProcess(0x100000, False, pid)
            if not self.handle:
                raise RuntimeError('Cannot monitor service process')

    def alive(self):
        if self.handle:
            return self.kernel.WaitForSingleObject(self.handle, 0) == 258
        try:
            os.kill(self.pid, 0)
            return True
        except ProcessLookupError:
            return False

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)


def worker(remote, cache, pid, delay=60, max_delay=120, poll=1):
    remote, cache = Path(remote), Path(cache).absolute()
    if cache == cache.parent or cache.is_symlink():
        raise RuntimeError('Refusing unsafe cache directory')
    local = cache / 'library.db'
    parent = ParentProcess(pid)
    cache.parent.mkdir(parents=True, exist_ok=True)
    # Local lock prevents a second worker from deleting an active local cache.
    try:
        with FileLock(str(cache) + '.lock'):
            # Explicit ephemeral policy: even unsynced leftovers are discarded.
            if cache.exists():
                shutil.rmtree(cache)
            cache.mkdir()
            monitor = None
            ready = False
            network_lock = FileLock(str(remote) + ".clipper-lock")
            locked = False
            try:
                network_lock.__enter__()
                locked = True
                if not remote.parent.is_dir():
                    raise RuntimeError('Network DB directory is unavailable')
                check_standalone(remote)
                expected = fingerprint(remote)
                if expected is not None:
                    shutil.copyfile(remote, local)
                    validate_db(local)
                    if fingerprint(remote) != expected:
                        raise RuntimeError('Network DB changed while loading')
                else:
                    sqlite3.connect(local).close()
                monitor = sqlite3.connect(local, timeout=10)
                version = monitor.execute('PRAGMA data_version').fetchone()[0]
                write_json(cache / 'status.json', {'state':'ready', 'last_sync':None, 'error':None})
                ready = True
                dirty_since = last_change = None
                retry_at = 0
                last_sync = None
                last_error = None
                while parent.alive() and not (cache / 'stop').exists():
                    now = time.monotonic()
                    current = monitor.execute('PRAGMA data_version').fetchone()[0]
                    if current != version:
                        version = current
                        dirty_since = dirty_since if dirty_since is not None else now
                        last_change = now
                        write_json(cache / 'status.json', {'state':'error' if last_error else 'pending', 'last_sync':last_sync, 'error':last_error})
                    if dirty_since is not None and now >= retry_at and (
                            now - last_change >= delay or now - dirty_since >= max_delay):
                        try:
                            expected = publish_snapshot(local, remote, expected)
                            # New commits during backup are observed next iteration.
                            dirty_since = last_change = None
                            last_sync = time.time()
                            last_error = None
                            write_json(cache / 'status.json', {'state':'synced', 'last_sync':last_sync, 'error':None})
                        except Exception as e:
                            log.exception('Network DB sync failed; retrying in 60 seconds')
                            retry_at = time.monotonic() + 60
                            last_error = str(e)
                            write_json(cache / 'status.json', {'state':'error', 'last_sync':last_sync, 'error':str(e)})
                    time.sleep(poll)
            finally:
                if monitor:
                    monitor.close()
                if ready:
                    try:
                        publish_snapshot(local, remote, expected)
                    except Exception:
                        log.exception('FINAL SYNC FAILED. Deleting local data as requested; unsynced edits will be lost.')
                # Close every local connection before deletion (required on Windows).
                try:
                    shutil.rmtree(cache)
                finally:
                    if locked:
                        network_lock.__exit__()
    finally:
        parent.close()


class NetworkDatabase:
    def __init__(self, remote):
        self.remote = os.path.abspath(remote)
        appdata = os.environ.get('LOCALAPPDATA')
        if not appdata:
            appdata = str(Path.home() / '.local' / 'share')
        key = hashlib.sha256(os.path.normcase(self.remote).encode()).hexdigest()[:24]
        self.cache = Path(appdata) / 'WebClipper' / 'network-db' / key
        self.local = str(self.cache / 'library.db')
        self.process = None

    def start(self):
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        # Log remains outside the data directory so shutdown failures are visible.
        logfile = open(str(self.cache) + '.log', 'a', encoding='utf-8')
        flags = 0
        if os.name == 'nt':
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        # Existing status must not make a fresh worker look ready. The worker
        # creates its directory under a lock; identify readiness by this run's PID.
        args = [sys.executable, str(Path(__file__).resolve()), '--worker',
                self.remote, str(self.cache), str(os.getpid())]
        try:
            self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                stdout=logfile, stderr=logfile, creationflags=flags,
                start_new_session=(os.name != 'nt'))
        finally:
            logfile.close()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f'Network DB worker failed. See {self.cache}.log')
            status = self.status()
            if status.get('worker_pid') == self.process.pid:
                atexit.register(self.stop)
                return self.local
            time.sleep(0.1)
        self.stop()
        raise RuntimeError(f'Network DB startup timed out. See {self.cache}.log')

    def status(self):
        try:
            result = json.loads((self.cache / 'status.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            result = {'state':'starting'}
        return {**result, 'remote_path':self.remote, 'local_path':self.local,
                'worker_alive':self.process is not None and self.process.poll() is None}

    def stop(self):
        if self.process and self.process.poll() is None:
            try:
                (self.cache / 'stop').touch()
            except FileNotFoundError:
                return
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                log.warning('Network DB worker is still finishing final sync/cleanup; see %s.log', self.cache)


_manager = None
_manager_lock = threading.Lock()


def working_path(configured):
    global _manager
    if not (is_network_path(configured) or os.environ.get('CLIPPER_NETWORK_CACHE') == '1'):
        return configured
    with _manager_lock:
        if _manager is None:
            candidate = NetworkDatabase(configured)
            candidate.start()
            _manager = candidate
        if os.path.normcase(os.path.abspath(configured)) != os.path.normcase(_manager.remote):
            raise RuntimeError('Restart the app to change the network database location')
        if not _manager.status()['worker_alive']:
            raise RuntimeError('Network DB worker stopped; stop and restart Clipper')
        return _manager.local


def status():
    return _manager.status() if _manager else {'state':'local', 'worker_alive':False}


def shutdown():
    if _manager:
        _manager.stop()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        if len(sys.argv) != 5 or sys.argv[1] != '--worker':
            raise SystemExit('Usage: network_db.py --worker REMOTE CACHE PARENT_PID')
        worker(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    except Exception:
        log.exception('Network DB worker stopped')
        raise
