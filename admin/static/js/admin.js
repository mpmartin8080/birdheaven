// Bird Heaven admin console. No framework, no build step -- same as the site.

import { supported as passkeysSupported, registerPasskey } from './webauthn.js';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = { clips: [], status: null, settings: null, jobsTimer: null, editing: null };

// ---------------------------------------------------------------- helpers

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? {} : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401) { location.replace('/login.html'); throw new Error('Signed out.'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (HTTP ${res.status}).`);
  return data;
}

let toastTimer;
function toast(msg, isError = false) {
  const el = $('[data-toast]');
  el.textContent = msg;
  el.classList.toggle('error', isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 7000 : 3500);
}

function fmtDuration(sec) {
  if (sec == null) return '';
  const s = Math.round(sec);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function fmtDate(iso) {
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

function fmtBytes(n) {
  if (n == null) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

// ISO (any offset) <-> <input type="datetime-local"> value in the browser's zone.
function toLocalInput(isoOrDate) {
  const d = isoOrDate instanceof Date ? isoOrDate : new Date(isoOrDate);
  if (isNaN(d)) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fromLocalInput(value) {
  if (!value) return null;
  const d = new Date(value);
  return isNaN(d) ? null : d.toISOString();
}

function setBusy(btn, busy, label) {
  if (busy) { btn.dataset.label = btn.textContent; btn.textContent = label; btn.disabled = true; }
  else { btn.textContent = btn.dataset.label || btn.textContent; btn.disabled = false; }
}

// ---------------------------------------------------------------- tabs

function showTab(name) {
  $$('[data-tab]').forEach((t) => t.setAttribute('aria-selected', String(t.dataset.tab === name)));
  $$('[data-panel]').forEach((p) => { p.hidden = p.dataset.panel !== name; });
  history.replaceState(null, '', `#${name}`);
  if (name === 'settings') loadSettings();
  if (name === 'passkeys') loadPasskeys();
  if (name === 'upload') loadJobs();
}
$$('[data-tab]').forEach((t) => t.addEventListener('click', () => showTab(t.dataset.tab)));
$$('[data-goto]').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.goto)));

// ---------------------------------------------------------------- status

async function loadStatus() {
  state.status = await api('GET', '/api/auth/status');
  const s = state.status;
  const banner = $('[data-password-banner]');
  banner.hidden = s.method !== 'password';
  if (s.method === 'password') {
    $('[data-password-banner-text]').textContent = s.passkeys === 0
      ? 'Register a passkey on the Passkeys tab, then set ADMIN_PASSWORD_LOGIN=false in deploy/.env.prod and run deploy-droplet up.'
      : `You have ${s.passkeys} passkey${s.passkeys === 1 ? '' : 's'}. Set ADMIN_PASSWORD_LOGIN=false in deploy/.env.prod and run deploy-droplet up to switch password sign-in off.`;
  }
}

$('[data-signout]').addEventListener('click', async () => {
  await fetch('/api/session', { method: 'DELETE' });
  location.replace('/login.html');
});

// ---------------------------------------------------------------- clips

async function loadClips() {
  const data = await api('GET', '/api/clips');
  state.clips = data.videos;
  $('[data-storage-note]').textContent = `${data.videos.length} clip${data.videos.length === 1 ? '' : 's'} · stored in ${data.storage}`;
  $('[data-clip-count]').textContent = data.videos.length ? ` ${data.videos.length}` : '';
  if (data.publicSite) $('[data-public-link]').href = data.publicSite;
  $('[data-species]').replaceChildren(...data.species.map((s) => {
    const o = document.createElement('option');
    o.value = s;
    return o;
  }));
  renderClips();
}

function renderClips() {
  const q = $('[data-search]').value.trim().toLowerCase();
  const list = q
    ? state.clips.filter((c) => c.title.toLowerCase().includes(q)
        || (c.species || []).some((s) => s.toLowerCase().includes(q)))
    : state.clips;
  const ul = $('[data-clip-list]');
  ul.replaceChildren(...list.map(clipRow));
  $('[data-clip-empty]').hidden = state.clips.length > 0;
}
$('[data-search]').addEventListener('input', renderClips);

function clipRow(c) {
  const li = document.createElement('li');
  li.className = 'clip';

  const thumb = document.createElement('div');
  thumb.className = 'clip-thumb';
  if (c.posterUrl) {
    const img = document.createElement('img');
    img.src = c.posterUrl;
    img.alt = '';
    img.loading = 'lazy';
    img.onerror = () => img.remove();
    thumb.append(img);
  }
  if (c.duration) {
    const d = document.createElement('span');
    d.className = 'dur';
    d.textContent = fmtDuration(c.duration);
    thumb.append(d);
  }

  const main = document.createElement('div');
  main.className = 'clip-main';
  const title = document.createElement('div');
  title.className = 'clip-title';
  title.textContent = c.title;
  const meta = document.createElement('div');
  meta.className = 'clip-meta';
  meta.textContent = [fmtDate(c.recorded), fmtBytes(c.bytes), c.width && c.height ? `${c.width}×${c.height}` : ''].filter(Boolean).join(' · ');
  const tags = document.createElement('ul');
  tags.className = 'tags';
  tags.replaceChildren(...(c.species || []).map((s) => {
    const t = document.createElement('li');
    t.textContent = s;
    return t;
  }));
  main.append(title, meta, tags);

  const actions = document.createElement('div');
  actions.className = 'clip-actions';
  const star = document.createElement('button');
  star.className = 'star';
  star.type = 'button';
  star.setAttribute('aria-pressed', String(!!c.featured));
  star.setAttribute('aria-label', c.featured ? 'Unfeature' : 'Feature');
  star.title = c.featured ? 'Featured — click to unfeature' : 'Feature this clip';
  star.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.5l2.9 6 6.6.9-4.8 4.6 1.2 6.5L12 17.4 6.1 20.5l1.2-6.5L2.5 9.4l6.6-.9z"/></svg>';
  star.addEventListener('click', async () => {
    try {
      const updated = await api('PATCH', `/api/clips/${encodeURIComponent(c.slug)}`, { featured: !c.featured });
      Object.assign(c, updated);
      renderClips();
    } catch (err) { toast(err.message, true); }
  });
  const edit = document.createElement('button');
  edit.className = 'btn btn-sm';
  edit.type = 'button';
  edit.textContent = 'Edit';
  edit.addEventListener('click', () => openEdit(c));
  const del = document.createElement('button');
  del.className = 'btn btn-sm btn-danger';
  del.type = 'button';
  del.textContent = 'Delete';
  del.addEventListener('click', () => confirmDelete(c));
  actions.append(star, edit, del);

  li.append(thumb, main, actions);
  return li;
}

// ---- edit dialog

const editDialog = $('[data-edit-dialog]');
const editForm = $('[data-edit-form]');
const editVideo = $('[data-edit-video]');

function openEdit(c) {
  state.editing = c;
  editForm.title.value = c.title;
  editForm.species.value = (c.species || []).join(', ');
  editForm.recorded.value = toLocalInput(c.recorded);
  editForm.description.value = c.description || '';
  editForm.featured.checked = !!c.featured;
  editForm.posterAt.value = '';
  $('[data-poster-status]').textContent = '';
  editVideo.poster = c.posterUrl || '';
  editVideo.src = c.videoUrl || '';
  editDialog.showModal();
}

function closeEdit() {
  editVideo.pause();
  editVideo.removeAttribute('src');
  editVideo.load();
  editDialog.close();
}
$$('[data-close]', editDialog).forEach((b) => b.addEventListener('click', closeEdit));
editDialog.addEventListener('cancel', (e) => { e.preventDefault(); closeEdit(); });

$('[data-use-frame]').addEventListener('click', () => {
  editForm.posterAt.value = editVideo.currentTime.toFixed(1);
});

$('[data-regen-poster]').addEventListener('click', async (e) => {
  const at = parseFloat(editForm.posterAt.value);
  if (!(at >= 0)) { toast('Pick a time first — pause the video and press "Use current frame".', true); return; }
  const btn = e.currentTarget;
  setBusy(btn, true, 'Working…');
  $('[data-poster-status]').textContent = '';
  try {
    const updated = await api('POST', `/api/clips/${encodeURIComponent(state.editing.slug)}/poster`, { at });
    Object.assign(state.editing, updated);
    editVideo.poster = updated.posterUrl;
    $('[data-poster-status]').textContent = 'Poster updated.';
    renderClips();
  } catch (err) {
    toast(err.message, true);
  } finally {
    setBusy(btn, false);
  }
});

editForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const c = state.editing;
  const btn = editForm.querySelector('button[type="submit"]');
  setBusy(btn, true, 'Saving…');
  try {
    const updated = await api('PATCH', `/api/clips/${encodeURIComponent(c.slug)}`, {
      title: editForm.title.value,
      species: editForm.species.value,
      recorded: fromLocalInput(editForm.recorded.value),
      description: editForm.description.value,
      featured: editForm.featured.checked,
    });
    Object.assign(c, updated);
    renderClips();
    closeEdit();
    toast('Saved.');
  } catch (err) {
    toast(err.message, true);
  } finally {
    setBusy(btn, false);
  }
});

$('[data-delete-from-edit]').addEventListener('click', () => {
  const c = state.editing;
  closeEdit();
  confirmDelete(c);
});

// ---- delete dialog

const deleteDialog = $('[data-delete-dialog]');
function confirmDelete(c) {
  $('[data-delete-name]').textContent = c.title;
  deleteDialog.querySelector('[name="deleteFiles"]').checked = true;
  deleteDialog.returnValue = '';
  deleteDialog.onclose = async () => {
    if (deleteDialog.returnValue !== 'delete') return;
    const files = deleteDialog.querySelector('[name="deleteFiles"]').checked;
    try {
      await api('DELETE', `/api/clips/${encodeURIComponent(c.slug)}?deleteFiles=${files}`);
      state.clips = state.clips.filter((x) => x.slug !== c.slug);
      renderClips();
      $('[data-clip-count]').textContent = state.clips.length ? ` ${state.clips.length}` : '';
      toast('Clip deleted.');
    } catch (err) { toast(err.message, true); }
  };
  deleteDialog.showModal();
}

// ---------------------------------------------------------------- upload

const uploadForm = $('[data-upload-form]');
const fileInput = $('[data-file]');
const drop = $('[data-drop]');
let chosenFile = null;

function chooseFile(file) {
  chosenFile = file || null;
  drop.hidden = !!file;
  $('[data-chosen]').hidden = !file;
  $('[data-upload-btn]').disabled = !file;
  if (!file) return;
  $('[data-chosen-name]').textContent = file.name;
  $('[data-chosen-size]').textContent = fmtBytes(file.size);
  if (!uploadForm.recorded.value && file.lastModified) {
    uploadForm.recorded.value = toLocalInput(new Date(file.lastModified));
  }
  if (!uploadForm.title.value) uploadForm.title.focus();
}

fileInput.addEventListener('change', () => chooseFile(fileInput.files[0]));
$('[data-clear-file]').addEventListener('click', () => { fileInput.value = ''; chooseFile(null); });
['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => {
  e.preventDefault(); drop.classList.add('over');
}));
['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => {
  e.preventDefault(); drop.classList.remove('over');
}));
drop.addEventListener('drop', (e) => {
  const f = e.dataTransfer.files[0];
  if (f) chooseFile(f);
});

// Live progress of the in-flight browser -> server transfer, keyed by job id.
const sending = new Map();

function putFile(jobId, file) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', `/api/uploads/${jobId}`);
    xhr.setRequestHeader('content-type', 'application/octet-stream');
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) { sending.set(jobId, e.loaded / e.total); renderJobs(); }
    };
    xhr.onload = () => {
      sending.delete(jobId);
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else {
        let msg = `Upload failed (HTTP ${xhr.status}).`;
        try { msg = JSON.parse(xhr.responseText).error || msg; } catch { /* keep default */ }
        reject(new Error(msg));
      }
    };
    xhr.onerror = () => { sending.delete(jobId); reject(new Error('The connection dropped during the upload.')); };
    xhr.send(file);
  });
}

uploadForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  if (!chosenFile) return;
  const btn = $('[data-upload-btn]');
  setBusy(btn, true, 'Starting…');
  const file = chosenFile;
  try {
    const job = await api('POST', '/api/uploads', {
      filename: file.name,
      size: file.size,
      title: uploadForm.title.value,
      species: uploadForm.species.value,
      description: uploadForm.description.value,
      recorded: fromLocalInput(uploadForm.recorded.value),
      featured: uploadForm.featured.checked,
    });
    // The form is free for the next clip as soon as the job exists.
    uploadForm.reset();
    fileInput.value = '';
    chooseFile(null);
    sending.set(job.id, 0);
    state.jobs = [job, ...(state.jobs || [])];
    renderJobs();
    toast('Uploading — you can keep working while it finishes.');
    putFile(job.id, file).then(loadJobs).catch((err) => { toast(err.message, true); loadJobs(); });
    pollJobs();
  } catch (err) {
    toast(err.message, true);
  } finally {
    setBusy(btn, false);
    btn.disabled = !chosenFile;
  }
});

const STATE_LABEL = { receiving: 'Uploading', queued: 'Queued', processing: 'Processing', done: 'Published', error: 'Failed' };

function renderJobs() {
  const jobs = state.jobs || [];
  $('[data-jobs-empty]').hidden = jobs.length > 0;
  $('[data-jobs]').replaceChildren(...jobs.map((j) => {
    const li = document.createElement('li');
    li.className = 'job';
    const head = document.createElement('div');
    head.className = 'job-head';
    const title = document.createElement('span');
    title.className = 'job-title';
    title.textContent = j.title;
    const badge = document.createElement('span');
    badge.className = `job-state ${j.state}`;
    badge.textContent = STATE_LABEL[j.state] || j.state;
    head.append(title, badge);
    const step = document.createElement('div');
    step.className = 'muted small';
    step.textContent = j.state === 'error' ? j.error : `${j.step}${j.filename ? ` · ${j.filename}` : ''}`;
    li.append(head, step);
    if (j.state === 'receiving' || j.state === 'queued' || j.state === 'processing') {
      const bar = document.createElement('div');
      bar.className = 'bar';
      const fill = document.createElement('span');
      const frac = sending.get(j.id);
      if (j.state === 'receiving' && frac != null) fill.style.width = `${Math.round(frac * 100)}%`;
      else bar.classList.add('indeterminate');
      bar.append(fill);
      li.append(bar);
    }
    if (j.state === 'done' || j.state === 'error') {
      const dismiss = document.createElement('button');
      dismiss.className = 'btn btn-sm';
      dismiss.type = 'button';
      dismiss.textContent = 'Dismiss';
      dismiss.addEventListener('click', async () => {
        await api('DELETE', `/api/jobs/${j.id}`).catch(() => {});
        state.jobs = state.jobs.filter((x) => x.id !== j.id);
        renderJobs();
      });
      head.append(dismiss);
    }
    return li;
  }));
}

async function loadJobs() {
  try {
    const before = new Map((state.jobs || []).map((j) => [j.id, j.state]));
    state.jobs = await api('GET', '/api/jobs');
    renderJobs();
    const newlyDone = state.jobs.filter((j) => j.state === 'done' && before.get(j.id) && before.get(j.id) !== 'done');
    if (newlyDone.length) {
      toast(`Published: ${newlyDone.map((j) => j.title).join(', ')}`);
      loadClips();
    }
    return state.jobs;
  } catch { return []; }
}

function pollJobs() {
  if (state.jobsTimer) return;
  state.jobsTimer = setInterval(async () => {
    const jobs = await loadJobs();
    if (!jobs.some((j) => ['receiving', 'queued', 'processing'].includes(j.state)) && !sending.size) {
      clearInterval(state.jobsTimer);
      state.jobsTimer = null;
    }
  }, 2000);
}

// ---------------------------------------------------------------- settings

const settingsForm = $('[data-settings-form]');

function fillSettings(s) {
  for (const el of settingsForm.elements) {
    if (!el.name) continue;
    const [a, b] = el.name.split('.');
    const v = b ? (s[a] || {})[b] : s[a];
    el.value = v ?? '';
  }
}

async function loadSettings() {
  try {
    state.settings = await api('GET', '/api/settings');
    fillSettings(state.settings);
  } catch (err) { toast(err.message, true); }
}

settingsForm.addEventListener('reset', (e) => {
  e.preventDefault();
  if (state.settings) fillSettings(state.settings);
});

settingsForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = { live: {} };
  for (const el of settingsForm.elements) {
    if (!el.name) continue;
    const [a, b] = el.name.split('.');
    if (b) body[a][b] = el.value; else body[a] = el.value;
  }
  const btn = settingsForm.querySelector('button[type="submit"]');
  setBusy(btn, true, 'Saving…');
  try {
    state.settings = await api('PUT', '/api/settings', body);
    fillSettings(state.settings);
    toast('Saved. The public site updates within about a minute.');
  } catch (err) {
    toast(err.message, true);
  } finally {
    setBusy(btn, false);
  }
});

// ---------------------------------------------------------------- passkeys

async function loadPasskeys() {
  const keys = await api('GET', '/api/passkeys');
  $('[data-keys-empty]').hidden = keys.length > 0;
  $('[data-keys]').replaceChildren(...keys.map((k) => {
    const li = document.createElement('li');
    li.className = 'key';
    const main = document.createElement('div');
    main.className = 'key-main';
    main.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="8" cy="15" r="4"/><path d="M10.8 12.2L20 3m-3 3l2 2m-5 1l2 2"/></svg>';
    const text = document.createElement('div');
    const name = document.createElement('strong');
    name.textContent = k.nickname;
    const when = document.createElement('div');
    when.className = 'muted small';
    when.textContent = `Added ${fmtDate(k.created_at)} · ${k.last_used_at ? `last used ${fmtDate(k.last_used_at)}` : 'not used yet'}`;
    text.append(name, when);
    main.append(text);
    const remove = document.createElement('button');
    remove.className = 'btn btn-sm btn-danger';
    remove.type = 'button';
    remove.textContent = 'Remove';
    remove.addEventListener('click', async () => {
      if (!confirm(`Remove the passkey "${k.nickname}"? That device will no longer be able to sign in.`)) return;
      try {
        await api('DELETE', `/api/passkeys/${k.id}`);
        toast('Passkey removed.');
        loadPasskeys();
        loadStatus();
      } catch (err) { toast(err.message, true); }
    });
    li.append(main, remove);
    return li;
  }));
}

$('[data-add-key]').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const btn = form.querySelector('button');
  const nickname = form.nickname.value.trim();
  setBusy(btn, true, 'Follow your device\'s prompt…');
  try {
    await registerPasskey({
      fetchOptions: () => api('POST', '/api/webauthn/register/options', {}),
      verify: (credential) => api('POST', '/api/webauthn/register/verify', { nickname, credential }),
    });
    form.reset();
    toast('Passkey added.');
    await loadPasskeys();
    await loadStatus();
  } catch (err) {
    toast(err.message, true);
  } finally {
    setBusy(btn, false);
  }
});

// ---------------------------------------------------------------- boot

(async () => {
  if (!passkeysSupported()) {
    $('[data-passkey-unsupported]').hidden = false;
    $('[data-add-key] button').disabled = true;
  }
  try {
    await loadStatus();
  } catch { return; }
  const wanted = location.hash.slice(1);
  const first = ['clips', 'upload', 'settings', 'passkeys'].includes(wanted) ? wanted
    : (state.status.method === 'password' && state.status.passkeys === 0 ? 'passkeys' : 'clips');
  showTab(first);
  loadClips().catch((err) => toast(err.message, true));
  const jobs = await loadJobs();
  if (jobs.some((j) => ['receiving', 'queued', 'processing'].includes(j.state))) pollJobs();
})();
