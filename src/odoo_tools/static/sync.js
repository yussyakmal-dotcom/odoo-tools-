/* Workspace snapshot monitor. Polling is only active while a job is running. */
let selectedSyncWorkspace = null;
let syncPollHandle = null;
let syncBusy = false;

function syncElement(tag, value, className) {
  const el = document.createElement(tag);
  el.textContent = value;
  if (className) el.className = className;
  return el;
}
function syncCanRun(workspace) {
  return currentUser && (currentUser.is_admin || workspace.role === 'workspace_admin');
}
async function refreshSyncPanel(workspaces) {
  const panel = $('sync-panel');
  panel.hidden = !workspaces.length || currentUser.must_change_password;
  stopSyncPoll();
  if (panel.hidden) {
    selectedSyncWorkspace = null;
    return;
  }
  const select = $('sync-workspace');
  if (!workspaces.some(w => String(w.id) === String(selectedSyncWorkspace))) {
    selectedSyncWorkspace = String(workspaces[0].id);
  }
  select.replaceChildren();
  for (const w of workspaces) {
    const option = document.createElement('option');
    option.value = String(w.id);
    option.textContent = w.name + ' (' + w.slug + ')';
    select.append(option);
  }
  select.value = selectedSyncWorkspace;
  await loadSyncMonitor();
}
function stopSyncPoll() {
  if (syncPollHandle) window.clearInterval(syncPollHandle);
  syncPollHandle = null;
}
function syncTable(data) {
  const holder = $('sync-models');
  holder.replaceChildren();
  if (!data.length) return;
  const table = document.createElement('table');
  const head = document.createElement('thead');
  const tr = document.createElement('tr');
  for (const title of ['Model', 'Downloaded', 'Server count', 'Status']) {
    tr.append(syncElement('th', title));
  }
  head.append(tr);
  table.append(head);
  const body = document.createElement('tbody');
  for (const item of data) {
    const row = document.createElement('tr');
    for (const value of [
      item.model, String(item.downloaded),
      item.expected_count === null ? '-' : String(item.expected_count),
      item.status + (item.note ? ' · ' + item.note : '')
    ]) row.append(syncElement('td', value));
    body.append(row);
  }
  table.append(body);
  holder.append(table);
}
async function loadSyncMonitor() {
  const wid = selectedSyncWorkspace;
  if (!wid || syncBusy || !currentUser) return;
  syncBusy = true;
  try {
    const [history, snapshot] = await Promise.all([
      api('/workspaces/' + wid + '/sync/runs'),
      api('/workspaces/' + wid + '/sync/snapshots/current')
    ]);
    if (wid !== selectedSyncWorkspace || !currentUser) return;
    const workspaces = await api('/workspaces');
    if (wid !== selectedSyncWorkspace || !currentUser) return;
    const workspace = workspaces.find(w => String(w.id) === wid);
    $('sync-start').hidden = !workspace || !syncCanRun(workspace);
    const runs = history.runs || [];
    const latest = runs[0];
    const activeJob = runs.some(r => ['queued', 'running'].includes(r.state));
    $('sync-start').disabled = activeJob;

    if (!latest) {
      $('sync-summary').textContent = 'Belum ada sinkronisasi. Gunakan Download All BOM untuk memulai.';
      $('sync-progress').hidden = true;
      $('sync-models').replaceChildren();
    } else {
      const labels = {
        queued: 'Menunggu worker', running: 'Sedang mengunduh',
        completed: 'Selesai — snapshot aktif', incomplete: 'Tidak lengkap — snapshot lama tetap aman',
        failed: 'Gagal — snapshot lama tetap aman'
      };
      $('sync-summary').textContent = 'Run #' + latest.id + ' · ' +
        (labels[latest.state] || latest.state) +
        '\n' + (latest.message || '') +
        (latest.error_code ? '\nKode: ' + latest.error_code : '') +
        '\nDownloaded: ' + latest.downloaded + ' record';
      $('sync-progress').hidden = !['queued', 'running'].includes(latest.state);
      const percent = latest.estimated_total > 0
        ? Math.min(99, Math.round(100 * latest.downloaded / latest.estimated_total)) : 0;
      $('sync-progress-bar').style.width = percent + '%';
      $('sync-progress-label').textContent =
        (latest.current_model || 'Menunggu') + ' · ' + latest.downloaded +
        ' / ' + (latest.estimated_total || '?') + ' sementara (' + percent + '%)';
      syncTable(latest.models || []);
    }
    $('sync-history').replaceChildren();
    if (!runs.length) $('sync-history').textContent = 'Belum ada pekerjaan.';
    for (const r of runs.slice(0, 10)) {
      const row = syncElement('div',
        '#' + r.id + ' · ' + r.state + ' · ' + r.created_at +
        (r.finished_at ? ' · selesai ' + r.finished_at : ''),
        'workspace-item');
      $('sync-history').append(row);
    }
    $('sync-snapshot').replaceChildren();
    if (!snapshot.available) $('sync-snapshot').textContent = 'Belum ada snapshot lokal yang lolos validasi.';
    else {
      $('sync-snapshot').append(syncElement('strong', 'Snapshot #' + snapshot.snapshot_id +
        ' · ' + snapshot.total_records + ' records · ' + snapshot.activated_at));
      for (const model of snapshot.models) {
        $('sync-snapshot').append(syncElement('div', model.model + ': ' + model.count));
      }
    }
    if (activeJob && !syncPollHandle) {
      syncPollHandle = window.setInterval(() => loadSyncMonitor().catch(() => {}), 2000);
    } else if (!activeJob) stopSyncPoll();
  } catch (error) {
    if (wid === selectedSyncWorkspace) {
      $('sync-summary').textContent = 'Gagal membaca status sinkronisasi: ' + error.message;
      stopSyncPoll();
    }
  } finally {
    syncBusy = false;
  }
}

$('sync-workspace').addEventListener('change', async () => {
  selectedSyncWorkspace = $('sync-workspace').value;
  stopSyncPoll();
  await loadSyncMonitor();
});
$('sync-refresh').addEventListener('click', async () => {
  await loadSyncMonitor();
});
$('sync-start').addEventListener('click', async () => {
  const wid = selectedSyncWorkspace;
  if (!wid || !window.confirm('Download seluruh BOM yang dapat diakses dari Odoo ke workspace ini? Data Odoo tidak akan diubah.')) return;
  $('sync-start').disabled = true;
  try {
    const job = await api('/workspaces/' + wid + '/sync/runs', {method: 'POST'});
    if (wid === selectedSyncWorkspace) {
      showMessage('Sync run #' + job.run_id + ' masuk antrean worker.', true);
      await loadSyncMonitor();
    }
  } catch (error) {
    showMessage('Gagal memulai download: ' + error.message);
    $('sync-start').disabled = false;
  }
});
