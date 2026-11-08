/* Workspace Odoo connection UI: no API key value is ever returned from the backend. */
let selectedConnectionWorkspace = null;

// Store ONLY the selected workspace ID. Never store an Odoo API key or a login token.
const CONNECTION_WORKSPACE_KEY = 'bom-tools-connection-workspace';
function rememberedConnectionWorkspace() {
  try { return window.localStorage.getItem(CONNECTION_WORKSPACE_KEY); }
  catch { return null; }
}
function rememberConnectionWorkspace(id) {
  try { window.localStorage.setItem(CONNECTION_WORKSPACE_KEY, String(id)); }
  catch { /* Storage may be disabled; connection data still lives on the server. */ }
}

function connectionStatus(message) {
  $('odoo-connection-status').textContent = message;
}

async function refreshConnectionPanel(workspaces) {
  const authorized = workspaces.filter(w => currentUser.is_admin || w.role === 'workspace_admin');
  const panel = $('connection-panel');
  panel.hidden = !authorized.length || currentUser.must_change_password;
  if (panel.hidden) {
    selectedConnectionWorkspace = null;
    $('odoo-key').value = '';
    return;
  }
  const dropdown = $('connection-workspace');
  const preferred = selectedConnectionWorkspace || rememberedConnectionWorkspace();
  if (authorized.some(w => String(w.id) === String(preferred))) {
    selectedConnectionWorkspace = String(preferred);
  } else {
    selectedConnectionWorkspace = String(authorized[0].id);
  }
  rememberConnectionWorkspace(selectedConnectionWorkspace);
  dropdown.replaceChildren();
  for (const w of authorized) {
    const option = document.createElement('option');
    option.value = String(w.id);
    option.textContent = w.name + ' (' + w.slug + ')';
    dropdown.append(option);
  }
  dropdown.value = selectedConnectionWorkspace;
  await loadOdooConnection();
}

async function loadOdooConnection() {
  const wid = selectedConnectionWorkspace;
  if (!wid) return;
  $('odoo-key').value = '';
  try {
    const c = await api('/workspaces/' + wid + '/odoo/connection');
    if (wid !== selectedConnectionWorkspace) return;
    $('odoo-url').value = c.base_url || '';
    $('odoo-database').value = c.database || '';
    $('odoo-key').required = !c.api_key_configured;
    $('odoo-key-status').textContent = c.api_key_configured
      ? '✓ API Key tersimpan di server (disembunyikan untuk keamanan)'
      : '⚠ API Key belum tersimpan untuk workspace ini';
    $('odoo-key-status').className = c.api_key_configured ? 'key-status saved' : 'key-status missing';
    $('odoo-key').placeholder = c.api_key_configured ? 'Kosongkan untuk memakai API Key tersimpan' : 'Masukkan API Key Odoo';
    $('odoo-key-hint').textContent = c.api_key_configured
      ? 'API Key telah disimpan terenkripsi. Untuk menggantinya, masukkan API Key baru; nilai lama tidak ditampilkan.'
      : 'Belum ada API Key. Masukkan dan simpan untuk workspace ini.';
    connectionStatus(c.configured
      ? 'Konfigurasi tersimpan. Status pengujian terakhir: ' + (c.last_test_status || 'belum diuji') +
        (c.last_tested_at ? '\nDiuji pada: ' + c.last_tested_at : '')
      : 'Belum ada konfigurasi Odoo pada workspace ini. Pastikan workspace di atas benar.');
  } catch (error) {
    $('odoo-key-status').textContent = 'Tidak dapat memastikan status API Key';
    $('odoo-key-status').className = 'key-status missing';
    connectionStatus('Tidak dapat membaca konfigurasi: ' + error.message);
  }
}

$('connection-workspace').addEventListener('change', async () => {
  selectedConnectionWorkspace = $('connection-workspace').value;
  rememberConnectionWorkspace(selectedConnectionWorkspace);
  await loadOdooConnection();
});

$('odoo-connection-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  clearMessage();
  const wid = selectedConnectionWorkspace;
  if (!wid) return;
  const button = $('odoo-save');
  button.disabled = true;
  connectionStatus('Menyimpan konfigurasi...');
  try {
    const payload = {
      base_url: $('odoo-url').value.trim(),
      database: $('odoo-database').value.trim() || null
    };
    const key = $('odoo-key').value.trim();
    if (key) payload.api_key = key;
    await api('/workspaces/' + wid + '/odoo/connection', {method: 'PUT', body: payload});
    $('odoo-key').value = '';
    if (wid === selectedConnectionWorkspace) {
      await loadOdooConnection();
      showMessage('Konfigurasi Odoo berhasil disimpan. Silakan jalankan Test Connection.', true);
    }
  } catch (error) {
    connectionStatus('Gagal menyimpan konfigurasi: ' + error.message);
  } finally {
    button.disabled = false;
  }
});

$('odoo-test').addEventListener('click', async () => {
  clearMessage();
  const wid = selectedConnectionWorkspace;
  if (!wid) return;
  const button = $('odoo-test');
  button.disabled = true;
  connectionStatus('Menguji akses JSON-2 API Odoo (read-only)...');
  try {
    const result = await api('/workspaces/' + wid + '/odoo/connection/test', {method: 'POST'});
    if (wid !== selectedConnectionWorkspace) return;
    const label = result.status === 'connected' ? 'BERHASIL'
      : result.status === 'partial' ? 'TERHUBUNG SEBAGIAN' : 'GAGAL';
    const parts = ['Status koneksi: ' + label];
    if (result.error) parts.push('Alasan: ' + result.error);
    for (const item of result.models || []) {
      parts.push((item.status === 'readable' ? '✓ ' : '⚠ ') + item.model + ': ' + item.status +
        (item.reason ? ' (' + item.reason + ')' : ''));
    }
    parts.push('Tes hanya membaca data. Tidak ada BOM yang diubah atau diunduh.');
    connectionStatus(parts.join('\n'));
  } catch (error) {
    connectionStatus('Tidak dapat melakukan tes: ' + error.message);
  } finally {
    button.disabled = false;
  }
});
