/* CloudTransfer front end. Vanilla JS, no build step. */
'use strict';

// ---------------------------------------------------------------- helpers

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function bytes(n) {
  if (n === null || n === undefined) return '—';
  if (n === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
  const value = n / Math.pow(1024, i);
  return `${value >= 100 || i === 0 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text === null || text === undefined ? '' : String(text);
  return div.innerHTML;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  let payload = null;
  const text = await response.text();
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = { detail: text }; }
  }
  if (!response.ok) {
    const detail = (payload && (payload.detail || payload.message)) || response.statusText;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return payload;
}

const banner = {
  node: $('#banner'),
  show(message, kind = 'ok', autoHide = kind === 'ok') {
    this.node.className = `banner ${kind}`;
    this.node.textContent = message;
    this.node.hidden = false;
    clearTimeout(this._timer);
    if (autoHide) this._timer = setTimeout(() => this.hide(), 6000);
  },
  hide() { this.node.hidden = true; },
};

// ---------------------------------------------------------------- modal

const modal = {
  open({ title, placeholder, okLabel }) {
    $('#modal-title').textContent = title;
    $('#modal-input').value = '';
    $('#modal-input').placeholder = placeholder || '';
    $('#modal-ok').textContent = okLabel || 'Create';
    $('#modal-error').hidden = true;
    $('#modal').hidden = false;
    $('#modal-input').focus();
    return new Promise((resolve) => { this._resolve = resolve; });
  },
  error(message) {
    $('#modal-error').textContent = message;
    $('#modal-error').hidden = false;
  },
  close(value) {
    $('#modal').hidden = true;
    if (this._resolve) { this._resolve(value); this._resolve = null; }
  },
};

$('#modal-cancel').addEventListener('click', () => modal.close(null));
$('#modal-ok').addEventListener('click', () => modal.close($('#modal-input').value.trim() || null));
$('#modal-input').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') modal.close($('#modal-input').value.trim() || null);
  if (event.key === 'Escape') modal.close(null);
});

// ---------------------------------------------------------------- picker

class Picker {
  constructor(rootId, { endpoint, rootLabel, side }) {
    this.root = $(`#${rootId}`);
    this.endpoint = endpoint;
    this.side = side;
    this.stack = [{ id: 'root', name: rootLabel }];
    this.selected = null;
    this.ready = false;

    this.listing = $('[data-role="listing"]', this.root);
    this.crumbs = $('[data-role="crumbs"]', this.root);
    this.pickedBox = $('[data-role="picked"]', this.root);

    $('[data-role="up"]', this.root).addEventListener('click', () => this.up());
    const newFolder = $('[data-role="newfolder"]', this.root);
    if (newFolder) newFolder.addEventListener('click', () => this.createFolder());

    // Until the matching account is connected there is nothing to list, so say
    // so rather than showing an empty box.
    this.listing.innerHTML =
      `<li class="empty-row">Connect ${escapeHtml(rootLabel === 'My Drive' ? 'Google Drive' : 'OneDrive')} to browse your folders.</li>`;
  }

  get current() { return this.stack[this.stack.length - 1]; }

  enable() {
    if (this.ready) return;
    this.ready = true;
    this.load();
  }

  async load() {
    this.listing.innerHTML = '<li class="loading">Loading…</li>';
    this.renderCrumbs();
    try {
      const data = await api(`${this.endpoint}?parent=${encodeURIComponent(this.current.id)}`);
      // The server resolves 'root' to a real ID; keep it so jobs get the real one.
      this.current.id = data.parent.id;
      this.current.name = data.parent.name || this.current.name;
      this.select(this.current.id, this.current.name);
      this.renderCrumbs();
      this.renderListing(data);
    } catch (error) {
      this.listing.innerHTML = `<li class="empty-row">${escapeHtml(error.message)}</li>`;
    }
  }

  renderListing(data) {
    const rows = [];
    for (const folder of data.folders) {
      rows.push(
        `<li class="folder" data-id="${escapeHtml(folder.id)}" data-name="${escapeHtml(folder.name)}">` +
        `<span class="icon">\u{1F4C1}</span>` +
        `<span class="label">${escapeHtml(folder.name)}</span>` +
        `</li>`
      );
    }
    for (const file of data.files) {
      const tag = file.needs_conversion
        ? '<span class="tag">converts to Office</span>'
        : (file.is_shortcut ? '<span class="tag">shortcut</span>' : '');
      rows.push(
        `<li class="file">` +
        `<span class="icon">\u{1F4C4}</span>` +
        `<span class="label">${escapeHtml(file.name)}</span>` +
        `${tag}<span class="size">${file.size === null ? '' : bytes(file.size)}</span>` +
        `</li>`
      );
    }
    if (!rows.length) rows.push('<li class="empty-row">This folder is empty.</li>');
    this.listing.innerHTML = rows.join('');

    $$('li.folder', this.listing).forEach((row) => {
      row.addEventListener('click', () => {
        this.stack.push({ id: row.dataset.id, name: row.dataset.name });
        this.load();
      });
    });
  }

  renderCrumbs() {
    const parts = this.stack.map((entry, index) => {
      const last = index === this.stack.length - 1;
      if (last) return `<span class="here">${escapeHtml(entry.name)}</span>`;
      return `<button data-depth="${index}">${escapeHtml(entry.name)}</button><span class="sep">/</span>`;
    });
    this.crumbs.innerHTML = parts.join('');
    $$('button', this.crumbs).forEach((button) => {
      button.addEventListener('click', () => {
        this.stack = this.stack.slice(0, Number(button.dataset.depth) + 1);
        this.load();
      });
    });
  }

  up() {
    if (this.stack.length < 2) return;
    this.stack.pop();
    this.load();
  }

  select(id, name) {
    this.selected = { id, name, path: this.stack.map((e) => e.name).join('/') };
    this.pickedBox.classList.add('set');
    this.pickedBox.innerHTML = `Selected: <strong>${escapeHtml(this.selected.path)}</strong>`;
    refreshStartButton();
  }

  async createFolder() {
    const name = await modal.open({
      title: 'New OneDrive folder',
      placeholder: 'Folder name',
      okLabel: 'Create',
    });
    if (!name) return;
    try {
      const result = await api(
        `${this.endpoint}/folder?parent=${encodeURIComponent(this.current.id)}` +
        `&name=${encodeURIComponent(name)}`,
        { method: 'POST' }
      );
      if (result.renamed_because && result.renamed_because.length) {
        banner.show(
          `Created as "${result.folder.name}" — ${result.renamed_because.join('; ')}.`,
          'warn'
        );
      }
      this.load();
    } catch (error) {
      banner.show(`Could not create the folder: ${error.message}`, 'error', false);
    }
  }
}

const sourcePicker = new Picker('picker-source', {
  endpoint: '/api/browse/drive',
  rootLabel: 'My Drive',
  side: 'drive',
});
const destPicker = new Picker('picker-dest', {
  endpoint: '/api/browse/onedrive',
  rootLabel: 'OneDrive',
  side: 'onedrive',
});

// ---------------------------------------------------------------- accounts

let connected = { google: false, microsoft: false };

function renderAccount(provider, info, configured) {
  const card = $(`#acct-${provider}`);
  const state = $('[data-role="state"]', card);
  const who = $('[data-role="who"]', card);
  const connect = $('[data-role="connect"]', card);
  const disconnect = $('[data-role="disconnect"]', card);

  if (info) {
    state.className = 'pill pill-on';
    state.textContent = 'connected';
    who.textContent = info.label || '';
    connect.hidden = true;
    disconnect.hidden = false;
  } else {
    state.className = configured ? 'pill pill-off' : 'pill pill-idle';
    state.textContent = configured ? 'not connected' : 'not configured';
    who.textContent = '';
    connect.hidden = !configured;
    disconnect.hidden = true;
  }
}

async function loadStatus() {
  try {
    const status = await api('/api/status');
    connected = { google: !!status.google, microsoft: !!status.microsoft };
    renderAccount('google', status.google, status.configured.google);
    renderAccount('microsoft', status.microsoft, status.configured.microsoft);
    if (connected.google) sourcePicker.enable();
    if (connected.microsoft) destPicker.enable();
    refreshStartButton();
  } catch (error) {
    banner.show(`Could not read status: ${error.message}`, 'error', false);
  }
}

$$('[data-role="disconnect"]').forEach((button) => {
  button.addEventListener('click', async () => {
    const provider = button.closest('.account').id.replace('acct-', '');
    if (!confirm(`Disconnect ${provider}? Stored tokens for it are deleted.`)) return;
    try {
      await api(`/api/accounts/${provider}/disconnect`, { method: 'POST' });
      banner.show(`${provider} disconnected.`);
      loadStatus();
    } catch (error) {
      banner.show(error.message, 'error', false);
    }
  });
});

// ---------------------------------------------------------------- start

function refreshStartButton() {
  const button = $('#start');
  const hint = $('#start-hint');
  const reasons = [];
  if (!connected.google) reasons.push('connect Google Drive');
  if (!connected.microsoft) reasons.push('connect OneDrive');
  if (!sourcePicker.selected) reasons.push('pick a source folder');
  if (!destPicker.selected) reasons.push('pick a destination folder');

  if (reasons.length) {
    button.disabled = true;
    hint.textContent = `To start: ${reasons.join(', ')}.`;
  } else {
    button.disabled = false;
    hint.textContent =
      `${sourcePicker.selected.path} → ${destPicker.selected.path}. ` +
      `Files are counted first, then moved.`;
  }
}

$('#start').addEventListener('click', async () => {
  const button = $('#start');
  button.disabled = true;
  try {
    const job = await api('/api/jobs', {
      method: 'POST',
      body: JSON.stringify({
        source_folder_id: sourcePicker.selected.id,
        source_folder_name: sourcePicker.selected.path,
        dest_folder_id: destPicker.selected.id,
        dest_folder_path: destPicker.selected.path,
        conflict_policy: $('#conflict').value,
        verify: $('#verify').checked,
        include_trashed: $('#trashed').checked,
        start_immediately: true,
      }),
    });
    banner.show(`Transfer #${job.id} started. Counting files…`);
    expanded.add(job.id);
    await refreshJobs();
  } catch (error) {
    banner.show(`Could not start: ${error.message}`, 'error', false);
  } finally {
    refreshStartButton();
  }
});

// ---------------------------------------------------------------- jobs

const expanded = new Set();
const taskCache = new Map();

function statusLabel(status) {
  return status.replace(/_/g, ' ');
}

function jobCard(job) {
  const pct = job.percent || 0;
  const doneish = ['completed', 'completed_with_errors'].includes(job.status);
  const active = ['running', 'pending', 'planning'].includes(job.status);

  const stats = [
    `${job.done_files}/${job.total_files} files`,
    `${bytes(job.done_bytes)} of ${bytes(job.total_bytes)}`,
  ];
  if (job.rate_bytes_per_second) stats.push(`${bytes(job.rate_bytes_per_second)}/s`);
  if (job.eta_seconds !== null && job.eta_seconds !== undefined && active) {
    stats.push(`~${duration(job.eta_seconds)} left`);
  }
  if (job.failed_files) stats.push(`<span class="bad">${job.failed_files} failed</span>`);
  if (job.skipped_files) stats.push(`<span class="warn">${job.skipped_files} skipped</span>`);

  const activeRows = (job.active_files || []).map((file) => {
    const filePct = file.total ? Math.round((file.bytes_done / file.total) * 100) : 0;
    return `<div><span class="nm">${escapeHtml(file.label)}</span>` +
           `<span>${bytes(file.bytes_done)} / ${bytes(file.total)}</span>` +
           `<span>${filePct}%</span></div>`;
  }).join('');

  const actions = [];
  if (job.running) {
    actions.push('<button class="btn btn-small" data-act="pause">Pause</button>');
    actions.push('<button class="btn btn-small btn-danger" data-act="cancel">Cancel</button>');
  } else if (['paused', 'pending', 'failed', 'cancelled'].includes(job.status)) {
    actions.push('<button class="btn btn-small btn-primary" data-act="start">Resume</button>');
  }
  if (job.failed_files && !job.running) {
    actions.push('<button class="btn btn-small" data-act="retry">Retry failed</button>');
  }
  if (job.total_files) {
    actions.push(`<button class="btn btn-small" data-act="toggle">${
      expanded.has(job.id) ? 'Hide files' : 'Show files'}</button>`);
    actions.push(
      `<a class="btn btn-small" href="/api/jobs/${job.id}/report?fmt=csv">Download report</a>`
    );
  }
  if (!job.running) {
    actions.push('<button class="btn btn-small btn-quiet" data-act="delete">Delete</button>');
  }

  return `
    <div class="job ${doneish ? 'done' : ''}" data-job="${job.id}">
      <div class="job-top">
        <div class="job-route">
          #${job.id} ${escapeHtml(job.source_folder_name || job.source_folder_id)}
          <span class="arrow">→</span>${escapeHtml(job.dest_folder_path || 'OneDrive')}
        </div>
        <span class="job-status s-${job.status}">${escapeHtml(statusLabel(job.status))}</span>
      </div>
      <div class="bar"><i style="width:${pct}%"></i></div>
      <div class="job-stats">${stats.join('<span></span>')}</div>
      ${activeRows ? `<div class="job-active">${activeRows}</div>` : ''}
      ${job.error ? `<p class="job-error">${escapeHtml(job.error)}</p>` : ''}
      <div class="job-actions">${actions.join('')}</div>
      <div class="filelist" data-role="files" ${expanded.has(job.id) ? '' : 'hidden'}></div>
    </div>`;
}

function renderTasks(container, payload) {
  const rows = payload.tasks.map((task) => {
    let verified = '';
    if (task.verified === true) verified = '<span class="vmark" title="hashes matched">✓</span>';
    else if (task.verified === false) verified = '<span class="vbad" title="hash mismatch">✗</span>';

    const notes = [];
    if (task.renamed) notes.push(`renamed from "${task.source_name}"`);
    if (task.converted_to) notes.push('converted to an Office format');
    if (task.error) notes.push(task.error);
    else if (task.hash_note && task.verified === false) notes.push(task.hash_note);

    return `<tr>
      <td class="st t-${task.status}">${escapeHtml(task.status)}</td>
      <td>${escapeHtml(task.dest_path)}${
        notes.length ? `<div class="why">${escapeHtml(notes.join(' · '))}</div>` : ''}</td>
      <td class="st">${task.size_is_estimate && !task.bytes_done ? '~' : ''}${bytes(task.source_size)}</td>
      <td class="st">${task.status === 'running' ? `${task.percent}%` : verified}</td>
    </tr>`;
  }).join('');

  const shown = payload.tasks.length;
  container.innerHTML = `
    <table>
      <thead><tr><th>Status</th><th>Destination</th><th>Size</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="4">No files.</td></tr>'}</tbody>
    </table>
    ${shown < payload.total
      ? `<p class="why">Showing ${shown} of ${payload.total}. Download the report for the full list.</p>`
      : ''}`;
}

async function loadTasks(jobId, container) {
  container.innerHTML = '<p class="why">Loading…</p>';
  try {
    const payload = await api(`/api/jobs/${jobId}/tasks?limit=200`);
    taskCache.set(jobId, payload);
    renderTasks(container, payload);
  } catch (error) {
    container.innerHTML = `<p class="why">${escapeHtml(error.message)}</p>`;
  }
}

function wireJobCard(card) {
  const jobId = Number(card.dataset.job);
  $$('[data-act]', card).forEach((button) => {
    button.addEventListener('click', async () => {
      const act = button.dataset.act;
      try {
        if (act === 'toggle') {
          const box = $('[data-role="files"]', card);
          if (expanded.has(jobId)) {
            expanded.delete(jobId);
            box.hidden = true;
          } else {
            expanded.add(jobId);
            box.hidden = false;
            await loadTasks(jobId, box);
          }
          await refreshJobs();
          return;
        }
        if (act === 'delete') {
          if (!confirm(`Delete transfer #${jobId}? Files already in OneDrive stay there.`)) return;
          await api(`/api/jobs/${jobId}`, { method: 'DELETE' });
          expanded.delete(jobId);
        } else if (act === 'retry') {
          const result = await api(`/api/jobs/${jobId}/retry-failed`, { method: 'POST' });
          banner.show(`Re-queued ${result.requeued} failed file(s).`);
        } else {
          await api(`/api/jobs/${jobId}/${act}`, { method: 'POST' });
        }
        await refreshJobs();
      } catch (error) {
        banner.show(error.message, 'error', false);
      }
    });
  });
}

let pollTimer = null;

async function refreshJobs() {
  const box = $('#jobs');
  try {
    const { jobs } = await api('/api/jobs?limit=25');
    if (!jobs.length) {
      box.innerHTML = '<p class="empty">No transfers yet.</p>';
      return;
    }

    // Jobs in flight need their live counters, which the list does not carry.
    const detailed = await Promise.all(
      jobs.map(async (job) => {
        if (['running', 'pending', 'planning'].includes(job.status) || job.running) {
          try { return await api(`/api/jobs/${job.id}`); } catch { return job; }
        }
        return job;
      })
    );

    box.innerHTML = detailed.map(jobCard).join('');
    $$('.job', box).forEach(wireJobCard);

    for (const job of detailed) {
      if (!expanded.has(job.id)) continue;
      const card = $(`.job[data-job="${job.id}"]`, box);
      if (card) await loadTasks(job.id, $('[data-role="files"]', card));
    }

    const anyActive = detailed.some(
      (job) => job.running || ['running', 'pending', 'planning'].includes(job.status)
    );
    clearTimeout(pollTimer);
    if (anyActive) pollTimer = setTimeout(refreshJobs, 1500);
  } catch (error) {
    box.innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  }
}

// ---------------------------------------------------------------- boot

(function boot() {
  const params = new URLSearchParams(location.search);
  if (params.get('connected')) {
    banner.show(`${params.get('connected')} connected.`);
    history.replaceState({}, '', location.pathname);
  } else if (params.get('error')) {
    banner.show(params.get('error'), 'error', false);
    history.replaceState({}, '', location.pathname);
  }
  loadStatus();
  refreshJobs();
})();
