/**
 * Drop Cat Go Studio — Shared UI components.
 * Reusable DOM builders for all feature tabs.
 */

// ── Utilities ────────────────────────────────────────────────────────────────

export function pathToUrl(p) {
  if (!p || p.startsWith('/') || p.startsWith('http')) return p || '';
  const norm = p.replace(/\\/g, '/');
  const idx  = norm.toLowerCase().indexOf('/output/');
  return idx !== -1 ? norm.substring(idx) : `/output/${norm.split('/').pop()}`;
}

export function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

export function el(tag, attrs = {}, children = []) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k === 'html') e.innerHTML = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const child of children) {
    if (typeof child === 'string') e.appendChild(document.createTextNode(child));
    else if (child) e.appendChild(child);
  }
  return e;
}

export function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatDuration(sec) {
  if (!sec || sec <= 0) return '0:00';
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

// ── Toast ────────────────────────────────────────────────────────────────────

export function toast(_msg, _level) { /* no popups */ }

// ── DropZone ─────────────────────────────────────────────────────────────────

/**
 * Create a drag-and-drop file upload zone.
 * @param {HTMLElement} container
 * @param {object} opts - { accept: string, multiple: bool, label: string, onFiles: (File[]) => void }
 */
/**
 * Add a "paste/type a file path" row below a drop zone.
 * Calls opts.onPaths(paths: string[]) when the user submits.
 * Supports comma-separated paths, folder paths, and wildcards.
 */
export function createPathInput(container, opts = {}) {
  const { onPaths, placeholder = 'Or paste file paths here (comma-separated, or a folder)...' } = opts;
  const row = el('div', { style: 'display:flex; gap:6px; margin-top:6px' }, [
    el('input', { type: 'text', id: `path-input-${Math.random().toString(36).slice(2,7)}`,
                  placeholder, style: 'flex:1; font-size:.8rem' }),
    el('button', { class: 'btn btn-sm', text: 'Add' }),
  ]);
  container.appendChild(row);

  const input = row.querySelector('input');
  const btn = row.querySelector('button');

  function submit() {
    const raw = input.value.trim();
    if (!raw) return;
    const paths = raw.split(',').map(p => p.trim()).filter(Boolean);
    if (paths.length && onPaths) onPaths(paths);
    input.value = '';
  }

  btn.addEventListener('click', submit);
  input.addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
  return row;
}

export function createDropZone(container, opts = {}) {
  const zone = el('div', { class: 'drop-zone' }, [
    el('div', { class: 'drop-zone-content' }, [
      el('svg', { html: '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>' }),
      el('p', { text: opts.label || 'Drag & drop files here or click to browse' }),
    ]),
  ]);

  const input = el('input', {
    type: 'file',
    style: 'display:none',
    ...(opts.accept ? { accept: opts.accept } : {}),
    ...(opts.multiple !== false ? { multiple: 'true' } : {}),
  });
  zone.appendChild(input);

  zone.addEventListener('click', () => input.click());
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag-over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('drag-over');
    if (opts.onFiles && e.dataTransfer.files.length) {
      opts.onFiles(Array.from(e.dataTransfer.files));
    }
  });
  input.addEventListener('change', () => {
    if (opts.onFiles && input.files.length) {
      opts.onFiles(Array.from(input.files));
    }
    input.value = '';
  });

  container.appendChild(zone);
  return zone;
}

// ── ProgressCard ─────────────────────────────────────────────────────────────

/**
 * Create a progress tracking card with bar + message + cancel button.
 * Returns an object with update(progress, message), show(), hide(), onCancel(fn).
 */
export function createProgressCard(container) {
  const card = el('div', { class: 'card progress-card', style: 'display:none' }, [
    el('div', { class: 'progress-header' }, [
      el('span', { class: 'progress-message', text: 'Starting...' }),
      el('button', { class: 'btn btn-sm btn-cancel', text: 'Cancel' }),
    ]),
    el('div', { class: 'progress-track' }, [
      el('div', { class: 'progress-fill' }),
    ]),
    el('div', { class: 'progress-pct', text: '0%' }),
  ]);
  container.appendChild(card);

  const msgEl = card.querySelector('.progress-message');
  const fillEl = card.querySelector('.progress-fill');
  const pctEl = card.querySelector('.progress-pct');
  const cancelBtn = card.querySelector('.btn-cancel');

  let cancelFn = null;
  cancelBtn.addEventListener('click', () => { if (cancelFn) cancelFn(); });

  return {
    el: card,
    update(progress, message) {
      fillEl.style.width = `${progress}%`;
      pctEl.textContent = `${progress}%`;
      if (message) msgEl.textContent = message;
    },
    show() { card.style.display = ''; },
    hide() { card.style.display = 'none'; },
    onCancel(fn) { cancelFn = fn; },
  };
}

// ── Video Player ─────────────────────────────────────────────────────────────

export function createVideoPlayer(container) {
  const vlcBtn    = el('button', { class: 'btn btn-sm', text: '▶ VLC' });
  const revealBtn = el('button', { class: 'btn btn-sm', text: 'Show in folder' });
  const wrap = el('div', { class: 'card video-result', style: 'display:none' }, [
    el('video', { controls: 'true', preload: 'auto', class: 'video-player' }),
    el('div', { class: 'video-actions' }, [
      el('a', { class: 'btn btn-primary download-link', text: 'Download', download: '' }),
      vlcBtn,
      revealBtn,
      el('button', { class: 'btn btn-sm btn-start-over', text: 'Start Over' }),
    ]),
  ]);
  container.appendChild(wrap);

  const video = wrap.querySelector('video');
  const downloadLink = wrap.querySelector('.download-link');
  const startOverBtn = wrap.querySelector('.btn-start-over');

  let currentRawPath = null;
  let startOverFn = null;
  startOverBtn.addEventListener('click', () => { if (startOverFn) startOverFn(); });

  async function _reveal(action) {
    if (!currentRawPath) return;
    try {
      await fetch('/api/reveal', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: currentRawPath, action }),
      });
    } catch (_) {}
  }
  vlcBtn.addEventListener('click',    () => _reveal('vlc'));
  revealBtn.addEventListener('click', () => _reveal('explorer'));

  // Handle video load errors — show a clear message instead of silent failure
  video.addEventListener('error', () => {
    if (video.src) {
      const errDiv = wrap.querySelector('.video-error') || (() => {
        const d = el('div', { class: 'video-error', style: 'color:var(--red); font-size:.85rem; padding:8px 0' });
        video.after(d);
        return d;
      })();
      errDiv.textContent = `Video file could not be loaded — use Download to save it, or Show in folder to open it directly.`;
    }
  });

  const labelEl = el('div', { style: 'font-size:.78rem; font-weight:600; color:var(--text-3); margin-bottom:4px; display:none;' });
  wrap.prepend(labelEl);

  return {
    el: wrap,
    show(url, rawPath) {
      currentRawPath = rawPath || null;
      video.src = url;
      downloadLink.href = url;
      downloadLink.download = url.split('/').pop();
      const existing = wrap.querySelector('.video-error');
      if (existing) existing.remove();
      labelEl.style.display = 'none';
      wrap.style.display = '';
    },
    showLabelled(url, rawPath, label) {
      this.show(url, rawPath);
      labelEl.textContent = label;
      labelEl.style.display = '';
    },
    hide() { wrap.style.display = 'none'; video.src = ''; currentRawPath = null; },
    onStartOver(fn) { startOverFn = fn; },
  };
}

// ── Slider Input ─────────────────────────────────────────────────────────────

export function createSlider(container, opts = {}) {
  const { label, min = 0, max = 100, step = 1, value = 50, unit = '', onChange } = opts;
  const id = `slider-${Math.random().toString(36).slice(2, 8)}`;

  const group = el('div', { class: 'form-group slider-group' }, [
    el('div', { class: 'slider-header' }, [
      el('label', { for: id, text: label || '' }),
      el('span', { class: 'slider-value', text: `${value}${unit}` }),
    ]),
    el('input', {
      type: 'range', id, min: String(min), max: String(max),
      step: String(step), value: String(value), class: 'slider',
    }),
  ]);
  container.appendChild(group);

  const input = group.querySelector('input');
  const valueEl = group.querySelector('.slider-value');

  input.addEventListener('input', () => {
    valueEl.textContent = `${input.value}${unit}`;
    if (onChange) onChange(parseFloat(input.value));
  });

  return {
    el: group,
    get value() { return parseFloat(input.value); },
    set value(v) { input.value = v; valueEl.textContent = `${v}${unit}`; },
  };
}

// ── Number Input ────────────────────────────────────────────────────────────

export function createNumberInput(container, opts = {}) {
  const { label, min = 0, max = 9999, step = 1, value = '', onChange } = opts;
  const id = `num-${Math.random().toString(36).slice(2, 8)}`;

  const group = el('div', { class: 'form-group' }, [
    el('label', { for: id, text: label || '' }),
    el('input', { type: 'number', id, min: String(min), max: String(max), step: String(step), value: String(value), style: 'width:100%' }),
  ]);
  container.appendChild(group);

  const input = group.querySelector('input');
  if (onChange) input.addEventListener('change', () => onChange(parseInt(input.value)));

  return {
    el: group,
    get value() { return input.value; },
    set value(v) { input.value = v; },
  };
}

// ── Select Input ─────────────────────────────────────────────────────────────

export function createSelect(container, opts = {}) {
  const { label, options = [], value = '', onChange } = opts;
  const id = `select-${Math.random().toString(36).slice(2, 8)}`;

  const group = el('div', { class: 'form-group' }, [
    el('label', { for: id, text: label || '' }),
    el('select', { id }, options.map(o => {
      const optEl = el('option', { value: typeof o === 'string' ? o : o.value, text: typeof o === 'string' ? o : o.label });
      if ((typeof o === 'string' ? o : o.value) === value) optEl.selected = true;
      return optEl;
    })),
  ]);
  container.appendChild(group);

  const select = group.querySelector('select');
  if (onChange) select.addEventListener('change', () => onChange(select.value));

  return {
    el: group,
    get value() { return select.value; },
    set value(v) { select.value = v; },
  };
}

// ── Checkbox ─────────────────────────────────────────────────────────────────

export function createCheckbox(container, opts = {}) {
  const { label, checked = false, onChange } = opts;
  const id = `cb-${Math.random().toString(36).slice(2, 8)}`;

  const group = el('div', { class: 'form-group checkbox-group' }, [
    el('input', { type: 'checkbox', id, ...(checked ? { checked: 'true' } : {}) }),
    el('label', { for: id, text: label || '' }),
  ]);
  container.appendChild(group);

  const input = group.querySelector('input');
  if (onChange) input.addEventListener('change', () => onChange(input.checked));

  return {
    el: group,
    get checked() { return input.checked; },
    set checked(v) { input.checked = v; },
  };
}
