'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name=csrf]').content;
const cwd = document.body.dataset.path || '';
const LAST = 'barefile.last';

function show(title) {
  $('panel').hidden = false;
  $('panel-bar').hidden = false;
  $('panel-title').textContent = title;
  $('panel-output').textContent = '';
}

function progress(value, max, label) {
  $('panel-bar').max = max || 1;
  $('panel-bar').value = value;
  $('panel-count').textContent = label;
}

function log(line) {
  const output = $('panel-output');
  output.textContent += line + '\n';
  output.scrollTop = output.scrollHeight;
}

// The page reloads to show the result, so carry the output across the reload.
function finish(summary) {
  sessionStorage.setItem(LAST, JSON.stringify({
    title: `${$('panel-title').textContent} ${summary}`,
    output: $('panel-output').textContent,
  }));
  location.reload();
}

// Reads the Server-Sent Events from /api/op. fetch, because EventSource cannot POST.
async function op(title, body) {
  show(title);
  progress(0, 0, '');
  let done = 0, failed = 0, total = 0, buffer = '';
  const handle = ev => {
    if (ev.line !== undefined) log(ev.line);
    if (ev.total === undefined) return;
    ({ done, failed = 0, total } = ev);
    progress(done + failed, total, `${done + failed} of ${total} items completed`);
  };
  try {
    const res = await fetch('/api/op', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      log(`error: ${(await res.json().catch(() => ({}))).error || res.statusText}`);
      return;
    }
    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    for (;;) {
      const { value, done: closed } = await reader.read();
      if (closed) break;
      buffer += value;
      const events = buffer.split('\n\n');
      buffer = events.pop();
      events.forEach(event => handle(JSON.parse(event.slice('data: '.length))));
    }
  } catch (error) {
    log(`error: connection lost (${error.message})`);
    return;
  }
  finish(failed ? `${done} done, ${failed} failed.` : 'done.');
}

async function uploadAll(files) {
  if (!files.length) return;
  show('Uploading files...');
  const total = files.reduce((sum, file) => sum + file.size, 0);
  const mb = bytes => (bytes / 1048576).toFixed(1);
  let sent = 0, failed = 0;
  for (const [index, file] of files.entries()) {
    const report = loaded => progress(sent + loaded, total,
      `file ${index + 1} of ${files.length}, ${mb(sent + loaded)} of ${mb(total)} MB`);
    report(0);
    const error = await upload(file, report);
    sent += file.size;
    if (error) failed += 1;
    log(error ? `error: ${file.name}: ${error}` : `${file.name} uploaded`);
  }
  finish(failed ? `${files.length - failed} done, ${failed} failed.` : 'done.');
}

function upload(file, onprogress) {
  return new Promise(resolve => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `/api/upload?${new URLSearchParams({ path: cwd, name: file.name })}`);
    xhr.setRequestHeader('X-CSRF-Token', token);
    xhr.upload.onprogress = e => onprogress(e.loaded);
    xhr.onload = () => {
      let message = xhr.statusText;
      try { message = JSON.parse(xhr.responseText).error || message; } catch { /* not JSON */ }
      resolve(xhr.status === 200 ? null : message);
    };
    xhr.onerror = () => resolve('connection lost');
    xhr.send(file);
  });
}

const selected = () => [...document.querySelectorAll('.select:checked')].map(box => box.value);

function bindFiles() {
  const refresh = () => { $('selected-actions').hidden = !selected().length; };
  const on = (id, handler) => $(id).addEventListener('click', handler);
  const askName = (label, action, title) => {
    const name = prompt(label);
    if (name) op(title, { action, items: [name], path: cwd });
  };
  const askDest = (action, title) => {
    const dest = prompt('Destination folder', `/${cwd}`);
    if (dest !== null) op(title, { action, items: selected(), dest });
  };

  on('add-file', () => askName('File name', 'add_file', 'Creating file...'));
  on('add-folder', () => askName('Folder name', 'add_folder', 'Creating folder...'));
  on('upload', () => $('upload-input').click());
  on('copy', () => askDest('copy', 'Copying files...'));
  on('move', () => askDest('move', 'Moving files...'));
  on('delete', () => op('Moving to Recycle Bin...', { action: 'delete', items: selected() }));
  $('upload-input').addEventListener('change', e => uploadAll([...e.target.files]));

  document.addEventListener('dragover', e => e.preventDefault());
  document.addEventListener('drop', e => {
    e.preventDefault();
    uploadAll([...e.dataTransfer.files]);
  });

  const all = $('select-all');
  if (!all) return;  // search results have no selection
  all.addEventListener('change', () => {
    document.querySelectorAll('.select').forEach(box => { box.checked = all.checked; });
    refresh();
  });
  document.querySelectorAll('.select').forEach(box => box.addEventListener('change', refresh));
  document.querySelectorAll('[data-rename]').forEach(button => button.addEventListener('click', () => {
    const name = prompt('New name', button.dataset.name);
    if (name && name !== button.dataset.name) {
      op('Renaming...', { action: 'rename', items: [button.dataset.rename], name });
    }
  }));
}

// Also runs in read-only mode, where bindFiles does not.
function bindListing() {
  $('sort-form').addEventListener('change', e => e.currentTarget.submit());

  // du runs on demand so a huge folder never delays the listing.
  fetch(`/api/du?${new URLSearchParams({ path: cwd })}`)
    .then(res => res.json())
    .then(data => data.du || 'unavailable')
    .catch(() => 'unavailable')
    .then(size => { $('du').textContent = size; $('du-footer').textContent = size; });
}

function bindTrash() {
  document.querySelectorAll('[data-op]').forEach(button => button.addEventListener('click', () => {
    const { op: action, item, name } = button.dataset;
    const items = item ? [Number(item)] : [];
    if (action === 'restore') return op('Restoring...', { action, items });
    const message = action === 'empty'
      ? 'Permanently delete everything in the Recycle Bin? This cannot be undone.'
      : `Permanently delete "${name}"? This cannot be undone.`;
    if (confirm(message)) op('Deleting permanently...', { action, items, confirm: true });
  }));
}

if ($('panel')) {
  const last = sessionStorage.getItem(LAST);
  if (last) {
    sessionStorage.removeItem(LAST);
    const { title, output } = JSON.parse(last);
    show(title);
    $('panel-bar').hidden = true;
    $('panel-output').textContent = output;
  }
  if ($('add-file')) bindFiles();
  if ($('du')) bindListing();
  bindTrash();
}
