/* Shared status banner for local-working-copy network databases. */
(() => {
  const banner = document.createElement('div');
  banner.setAttribute('role', 'status');
  Object.assign(banner.style, {display:'none', padding:'8px 20px', font:'13px sans-serif',
    background:'#e8f0fe', color:'#15365f', borderBottom:'1px solid #ccd'});
  document.body.prepend(banner);
  async function refresh() {
    try {
      const response = await fetch('/health', {cache:'no-store'});
      if (!response.ok) throw new Error('Unable to check sync');
      const {storage} = await response.json();
      if (!storage || storage.state === 'local') { banner.style.display = 'none'; return; }
      banner.style.display = 'block';
      const failed = storage.state === 'error' || !storage.worker_alive;
      banner.style.background = failed ? '#fee2e2' : '#e8f0fe';
      banner.style.color = failed ? '#991b1b' : '#15365f';
      const when = storage.last_sync ? new Date(storage.last_sync * 1000).toLocaleTimeString() : '';
      banner.textContent = failed
        ? 'Network sync failed: ' + (storage.error || 'Sync worker stopped; restart Clipper.') +
          ' Unsynced edits will be deleted when the local cache is cleaned up.'
        : storage.state === 'pending'
          ? 'Network library: local changes pending; sync after 60 seconds idle (at most 2 minutes before an attempt).'
          : 'Network library: ' + (when ? 'last synced ' + when : 'local working copy ready') +
            '. Local copy is deleted on exit.';
    } catch (_) {
      if (banner.style.display !== 'none') banner.textContent = 'Cannot check network sync status; the service may be unavailable.';
    }
  }
  refresh();
  setInterval(refresh, 5000);
})();
