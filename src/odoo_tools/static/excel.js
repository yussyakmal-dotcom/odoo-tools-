/* Excel BOM workflow. All edits are local SQLite only; no Odoo writes. */
let selectedExcelWorkspace = null;
let excelPreviewToken = null;
let excelLoading = false;

function excelLine(tag, textContent) {
  const element = document.createElement(tag);
  element.textContent = String(textContent);
  return element;
}
function excelCanEdit(workspace) {
  return currentUser && (currentUser.is_admin ||
    workspace.role === 'workspace_admin' || workspace.role === 'editor');
}
function excelResetPreview() {
  excelPreviewToken = null;
  $('excel-preview-section').hidden = true;
  $('excel-change-list').replaceChildren();
  $('excel-warning-list').replaceChildren();
  $('excel-apply').disabled = true;
}
async function refreshExcelPanel(workspaces) {
  const panel = $('excel-panel');
  panel.hidden = !workspaces.length || currentUser.must_change_password;
  if (panel.hidden) {
    selectedExcelWorkspace = null;
    excelResetPreview();
    return;
  }
  const picker = $('excel-workspace');
  const preferred = selectedExcelWorkspace || selectedSyncWorkspace || String(workspaces[0].id);
  selectedExcelWorkspace = String(workspaces.some(w => String(w.id) === String(preferred))
    ? preferred : workspaces[0].id);
  picker.replaceChildren();
  for (const workspace of workspaces) {
    const option = document.createElement('option');
    option.value = String(workspace.id);
    option.textContent = workspace.name + ' (' + workspace.slug + ')';
    picker.append(option);
  }
  picker.value = selectedExcelWorkspace;
  const w = workspaces.find(x => String(x.id) === selectedExcelWorkspace);
  $('excel-editor-area').hidden = !excelCanEdit(w);
  excelResetPreview();
  await excelRefreshStatus();
}
async function excelRefreshStatus() {
  const wid = selectedExcelWorkspace;
  if (!wid || excelLoading) return;
  excelLoading = true;
  try {
    const result = await api('/workspaces/' + wid + '/excel/working-summary');
    if (wid !== selectedExcelWorkspace || !currentUser) return;
    const applied = result.recent_applies || [];
    const lines = [
      'Snapshot #' + result.snapshot_id + ' — perubahan versi kerja: ' +
      result.working_overrides + ' BOM Lines.',
      'Snapshot asli: tidak berubah. Odoo: tidak diubah.',
      applied.length
        ? 'Penerapan terakhir: batch #' + applied[0].id + ' · ' +
          applied[0].changed_count + ' baris · ' + applied[0].applied_at
        : 'Belum ada perubahan lokal melalui Excel.',
    ];
    $('excel-summary').textContent = lines.join('\n');
  } catch (error) {
    if (wid === selectedExcelWorkspace) {
      $('excel-summary').textContent = 'Status lokal belum tersedia: ' + error.message;
    }
  } finally {
    excelLoading = false;
  }
}
$('excel-workspace').addEventListener('change', async () => {
  selectedExcelWorkspace = $('excel-workspace').value;
  excelResetPreview();
  $('excel-file').value = '';
  await excelRefreshStatus();
});
$('excel-refresh').addEventListener('click', () => excelRefreshStatus());

$('excel-export').addEventListener('click', async () => {
  const wid = selectedExcelWorkspace;
  if (!wid) return;
  const button = $('excel-export');
  button.disabled = true;
  clearMessage();
  try {
    const response = await fetch('/api/v1/workspaces/' + wid + '/excel/export', {
      method: 'GET', credentials: 'same-origin', cache: 'no-store',
    });
    if (!response.ok) {
      const result = await response.json().catch(() => ({}));
      throw new Error(typeof result.detail === 'string' ? result.detail : 'Export Excel gagal');
    }
    const file = await response.blob();
    const objectUrl = URL.createObjectURL(file);
    const link = document.createElement('a');
    link.href = objectUrl;
    const disposition = response.headers.get('Content-Disposition') || '';
    const nameMatch = disposition.match(/filename="([^"]+)"/);
    link.download = nameMatch ? nameMatch[1] : 'BOM_Local.xlsx';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    showMessage('Excel lokal berhasil diunduh. Edit kolom F dan H lalu upload kembali.', true);
  } catch (error) {
    showMessage(error.message);
  } finally {
    button.disabled = false;
  }
});
function excelRenderPreview(result) {
  const section = $('excel-preview-section');
  section.hidden = false;
  const counts = result.total_lines + ' BOM Lines diperiksa · ' +
    result.changed_count + ' berubah · ' + result.unchanged_count + ' tidak berubah';
  $('excel-preview-headline').textContent = counts;
  const warnings = $('excel-warning-list');
  warnings.replaceChildren();
  if (result.warning_count) {
    warnings.append(excelLine('strong', result.warning_count + ' peringatan (periksa sebelum Apply):'));
    for (const warning of result.warnings || []) warnings.append(excelLine('div', '⚠ ' + warning));
  } else {
    warnings.append(excelLine('div', 'Tidak ada peringatan validasi.'));
  }
  const holder = $('excel-change-list');
  holder.replaceChildren();
  if (result.changed_count) {
    const table = document.createElement('table');
    const thead = document.createElement('thead');
    const header = document.createElement('tr');
    for (const label of ['BOM Line ID', 'BOM ID', 'Component: Lama → Baru', 'Qty: Lama → Baru']) {
      header.append(excelLine('th', label));
    }
    thead.append(header);
    table.append(thead);
    const tbody = document.createElement('tbody');
    for (const change of (result.changes_sample || []).slice(0, 100)) {
      const row = document.createElement('tr');
      for (const value of [
        change.bom_line_id, change.bom_id,
        change.old_product_id + ' → ' + change.new_product_id,
        change.old_qty + ' → ' + change.new_qty,
      ]) row.append(excelLine('td', value));
      tbody.append(row);
    }
    table.append(tbody);
    holder.append(table);
    if (result.changed_count > 100) {
      holder.append(excelLine('p', 'Menampilkan 100 perubahan pertama dari ' +
        result.changed_count + '. Semua baris tetap divalidasi.'));
    }
  } else {
    holder.append(excelLine('p', 'Tidak ada perubahan. Tidak ada data lokal yang perlu diterapkan.'));
  }
  excelPreviewToken = result.preview_token || null;
  $('excel-apply').disabled = !excelPreviewToken;
}
$('excel-upload-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const wid = selectedExcelWorkspace;
  const file = $('excel-file').files[0];
  if (!wid || !file) return;
  if (file.size > 12 * 1024 * 1024) {
    showMessage('Excel terlalu besar (maksimal 12 MB).');
    return;
  }
  clearMessage();
  excelResetPreview();
  const button = $('excel-preview-button');
  button.disabled = true;
  $('excel-summary').textContent = 'Memvalidasi Excel dan membandingkan nilai lokal...';
  try {
    const data = new FormData();
    data.append('file', file, file.name);
    const response = await fetch('/api/v1/workspaces/' + wid + '/excel/preview', {
      method: 'POST',
      headers: {'X-CSRF-Token': csrf},
      credentials: 'same-origin',
      body: data,
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = result.detail;
      if (detail && typeof detail === 'object') {
        throw new Error((detail.message || 'Excel tidak valid') + '\n' +
          (detail.errors || []).slice(0, 15).join('\n'));
      }
      throw new Error(typeof detail === 'string' ? detail : 'Excel upload gagal');
    }
    if (selectedExcelWorkspace !== wid) return;
    excelRenderPreview(result);
    $('excel-summary').textContent = 'Upload selesai: ' +
      result.changed_count + ' perubahan lokal siap diperiksa.';
  } catch (error) {
    showMessage(error.message);
    await excelRefreshStatus();
  } finally {
    button.disabled = false;
  }
});
$('excel-apply').addEventListener('click', async () => {
  if (!excelPreviewToken || !selectedExcelWorkspace) return;
  const wid = selectedExcelWorkspace;
  const token = excelPreviewToken;
  if (!window.confirm('Terapkan perubahan Excel ke working BOM SQLite lokal? ' +
    'Snapshot asli dan data Odoo tidak berubah.')) return;
  const button = $('excel-apply');
  button.disabled = true;
  clearMessage();
  try {
    const result = await api('/workspaces/' + wid + '/excel/preview/' +
      encodeURIComponent(token) + '/apply', {method: 'POST'});
    if (wid !== selectedExcelWorkspace) return;
    excelResetPreview();
    $('excel-file').value = '';
    await excelRefreshStatus();
    showMessage('Berhasil: ' + result.changed_count +
      ' BOM Lines diperbarui di data kerja lokal (batch #' + result.batch_id +
      '). Odoo tidak berubah.', true);
  } catch (error) {
    showMessage('Apply dibatalkan: ' + error.message);
    button.disabled = false;
  }
});
$('excel-cancel').addEventListener('click', () => excelResetPreview());
