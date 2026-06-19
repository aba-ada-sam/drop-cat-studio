/**
 * Drop Cat Go Studio -- Forge Couple visual region editor.
 *
 * Lets the user place regions anywhere (rule-of-thirds, off-center hero, grids)
 * instead of equal Basic strips, so generated images stop landing dead-center.
 *
 * Public API (unchanged where the tab relies on it):
 *   const editor = new RegionEditor(containerEl, { rows, cols, direction, onChange });
 *   editor.setFromGrid(rows, cols, direction)   -> equal grid
 *   editor.setRegionPrompts(["left", "center", ...])  -> fill prompts by index
 *   editor.getRegions()  -> [{ x, y, w, h, weight, prompt }]   (percent geometry)
 *   editor.getMapping()  -> [{ x1, x2, y1, y2, weight, prompt }] (0..1, for backend)
 */

const SNAP_TARGETS = [0, 33.333, 50, 66.667, 100]; // edges/halves -> rule of thirds
const SNAP_TOL = 4; // percent

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const clamp01 = (v) => Math.max(0, Math.min(1, v));
function snap(v) {
  for (const t of SNAP_TARGETS) if (Math.abs(v - t) <= SNAP_TOL) return t;
  return Math.round(v);
}

// Geometry-only layout presets. Each returns an array of {x,y,w,h} in percent.
// Single-region presets pair with the global prompt that fills the rest of the frame.
const PRESETS = {
  cols3:  { label: '|||',  title: 'Three equal columns (rule-of-thirds lines)', make: () => grid(1, 3) },
  rows3:  { label: '≡',    title: 'Three equal rows',                           make: () => grid(3, 1) },
  grid3:  { label: '⊞',    title: '3x3 rule-of-thirds grid (9 regions)',        make: () => grid(3, 3) },
  heroL:  { label: '◧',    title: 'Hero in the LEFT third (subject off-center)', make: () => [{ x: 0,      y: 0,  w: 33.333, h: 100 }] },
  heroR:  { label: '◨',    title: 'Hero in the RIGHT third (subject off-center)',make: () => [{ x: 66.667, y: 0,  w: 33.333, h: 100 }] },
  nodeTL: { label: '◰',    title: 'Power point: upper-left thirds intersection', make: () => [{ x: 8,  y: 8,  w: 38, h: 38 }] },
  nodeTR: { label: '◳',    title: 'Power point: upper-right thirds intersection',make: () => [{ x: 54, y: 8,  w: 38, h: 38 }] },
  nodeBL: { label: '◱',    title: 'Power point: lower-left thirds intersection', make: () => [{ x: 8,  y: 54, w: 38, h: 38 }] },
  nodeBR: { label: '◲',    title: 'Power point: lower-right thirds intersection',make: () => [{ x: 54, y: 54, w: 38, h: 38 }] },
  wideC:  { label: '▭',    title: 'Wide center: 25 / 50 / 25 columns',           make: () => [{ x: 0, y: 0, w: 25, h: 100 }, { x: 25, y: 0, w: 50, h: 100 }, { x: 75, y: 0, w: 25, h: 100 }] },
};

function grid(rows, cols) {
  const out = [];
  const pw = 100 / cols, ph = 100 / rows;
  for (let r = 0; r < rows; r++)
    for (let c = 0; c < cols; c++)
      out.push({ x: c * pw, y: r * ph, w: pw, h: ph });
  return out;
}

export class RegionEditor {
  constructor(containerEl, opts = {}) {
    this._el   = containerEl;
    this._opts = opts;
    this._dir  = opts.direction || 'Horizontal';
    this._mode = 'visual'; // 'visual' | 'numeric'
    this._selected = 0;
    this._drag = null;
    this._regions = [];
    const rows = opts.rows || 1, cols = opts.cols || 3;
    this._applyGeometry(grid(rows, cols));
    this._render();
  }

  // -- Public API --------------------------------------------------------------

  setFromGrid(rows, cols, direction) {
    this._dir = direction || this._dir;
    this._applyGeometry(grid(rows, cols));
    this._render();
  }

  getRegions() {
    return this._regions.map(r => ({ ...r }));
  }

  getMapping() {
    return this._regions.map(r => ({
      x1: clamp01(r.x / 100),
      x2: clamp01((r.x + r.w) / 100),
      y1: clamp01(r.y / 100),
      y2: clamp01((r.y + r.h) / 100),
      weight: r.weight,
      prompt: r.prompt || '',
    }));
  }

  setRegionPrompts(prompts) {
    (prompts || []).forEach((p, i) => { if (this._regions[i]) this._regions[i].prompt = p; });
    this._render();
  }

  setMode(mode) { this._mode = mode; this._render(); }

  // -- Geometry ----------------------------------------------------------------

  // Replace region rectangles, keeping existing prompts/weights by index.
  _applyGeometry(rects) {
    const prev = this._regions || [];
    this._regions = rects.map((g, i) => ({
      x: g.x, y: g.y, w: g.w, h: g.h,
      prompt: prev[i]?.prompt || '',
      weight: prev[i]?.weight ?? 1.0,
    }));
    if (this._selected >= this._regions.length) this._selected = this._regions.length - 1;
    if (this._selected < 0) this._selected = 0;
  }

  _applyPreset(name) {
    const p = PRESETS[name];
    if (!p) return;
    this._applyGeometry(p.make());
    this._selected = 0;
    this._render();
    this._onChange();
  }

  // -- Render ------------------------------------------------------------------

  _render() {
    this._el.innerHTML = '';
    this._el.appendChild(this._presetBar());
    this._el.appendChild(this._modeRow());
    this._content = document.createElement('div');
    this._el.appendChild(this._content);
    this._paint();
  }

  _paint() {
    this._content.innerHTML = '';
    if (this._mode === 'visual') this._paintVisual();
    else this._paintNumeric();
  }

  _presetBar() {
    const bar = document.createElement('div');
    bar.style.cssText = 'display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px';
    const lead = document.createElement('span');
    lead.textContent = 'Layout';
    lead.style.cssText = 'font-size:.68rem;font-weight:800;letter-spacing:.06em;color:var(--text-3);text-transform:uppercase;align-self:center;margin-right:2px';
    bar.appendChild(lead);
    for (const [key, p] of Object.entries(PRESETS)) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn btn-sm';
      b.textContent = p.label;
      b.title = p.title;
      b.style.cssText = 'font-size:.8rem;min-width:28px;padding:2px 6px;line-height:1.2';
      b.addEventListener('click', () => this._applyPreset(key));
      bar.appendChild(b);
    }
    return bar;
  }

  _modeRow() {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:6px;margin-bottom:8px;align-items:center';
    for (const m of ['visual', 'numeric']) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = m === 'visual' ? 'Visual' : 'Numeric';
      b.className = `btn btn-sm${this._mode === m ? ' btn-primary' : ''}`;
      b.style.fontSize = '.72rem';
      b.addEventListener('click', () => { this._mode = m; this._render(); });
      row.appendChild(b);
    }
    const hint = document.createElement('span');
    hint.textContent = 'drag to move · drag corner to resize · snaps to thirds';
    hint.style.cssText = 'font-size:.66rem;color:var(--text-3)';
    row.appendChild(hint);
    return row;
  }

  _paintVisual() {
    const wrapper = document.createElement('div');
    wrapper.style.cssText = 'display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap';

    const aspect = this._dir === 'Vertical' ? 0.7 : 1.6;
    const svgW = 280;
    const svgH = Math.round(svgW / aspect);
    this._svgW = svgW; this._svgH = svgH;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', svgW);
    svg.setAttribute('height', svgH);
    svg.style.cssText = 'border:1px solid var(--border-2);border-radius:var(--r-md);background:var(--bg);flex-shrink:0;touch-action:none';
    this._svg = svg;

    const NS = 'http://www.w3.org/2000/svg';
    const px = (pctX) => pctX / 100 * svgW;
    const py = (pctY) => pctY / 100 * svgH;

    // Rule-of-thirds guide lines (always on, so the eye composes to thirds)
    for (const t of [33.333, 66.667]) {
      const vl = document.createElementNS(NS, 'line');
      vl.setAttribute('x1', px(t)); vl.setAttribute('y1', 0); vl.setAttribute('x2', px(t)); vl.setAttribute('y2', svgH);
      vl.setAttribute('stroke', 'var(--text-3)'); vl.setAttribute('stroke-width', '1'); vl.setAttribute('stroke-dasharray', '2,4'); vl.setAttribute('opacity', '.45');
      svg.appendChild(vl);
      const hl = document.createElementNS(NS, 'line');
      hl.setAttribute('x1', 0); hl.setAttribute('y1', py(t)); hl.setAttribute('x2', svgW); hl.setAttribute('y2', py(t));
      hl.setAttribute('stroke', 'var(--text-3)'); hl.setAttribute('stroke-width', '1'); hl.setAttribute('stroke-dasharray', '2,4'); hl.setAttribute('opacity', '.45');
      svg.appendChild(hl);
    }

    // Regions
    this._regions.forEach((reg, idx) => {
      const x = px(reg.x), y = py(reg.y), w = px(reg.w), h = py(reg.h);
      const sel = idx === this._selected;

      const rect = document.createElementNS(NS, 'rect');
      rect.setAttribute('x', x + 1); rect.setAttribute('y', y + 1);
      rect.setAttribute('width', Math.max(0, w - 2)); rect.setAttribute('height', Math.max(0, h - 2));
      rect.setAttribute('fill', sel ? 'rgba(212,160,23,.20)' : 'rgba(212,160,23,.06)');
      rect.setAttribute('stroke', sel ? 'var(--accent)' : 'var(--border-2)');
      rect.setAttribute('stroke-width', '1.5'); rect.setAttribute('rx', '3');
      rect.style.cursor = sel ? 'move' : 'pointer';
      rect.addEventListener('pointerdown', (e) => {
        if (this._selected !== idx) { this._selected = idx; this._paint(); return; }
        this._beginDrag(e, idx, 'move');
      });
      svg.appendChild(rect);

      const label = document.createElementNS(NS, 'text');
      label.setAttribute('x', x + w / 2); label.setAttribute('y', y + h / 2);
      label.setAttribute('text-anchor', 'middle'); label.setAttribute('dominant-baseline', 'middle');
      label.setAttribute('fill', sel ? 'var(--accent)' : 'var(--text-3)');
      label.setAttribute('font-size', '10'); label.style.pointerEvents = 'none';
      const t = reg.prompt ? reg.prompt.slice(0, 14) + (reg.prompt.length > 14 ? '…' : '') : `R${idx + 1}`;
      label.textContent = t;
      svg.appendChild(label);

      // SE resize handle on the selected region
      if (sel) {
        const hs = 9;
        const handle = document.createElementNS(NS, 'rect');
        handle.setAttribute('x', x + w - hs); handle.setAttribute('y', y + h - hs);
        handle.setAttribute('width', hs); handle.setAttribute('height', hs);
        handle.setAttribute('fill', 'var(--accent)'); handle.setAttribute('rx', '2');
        handle.style.cursor = 'nwse-resize';
        handle.addEventListener('pointerdown', (e) => { e.stopPropagation(); this._beginDrag(e, idx, 'resize'); });
        svg.appendChild(handle);
      }
    });

    wrapper.appendChild(svg);
    wrapper.appendChild(this._sidebar());
    this._content.appendChild(wrapper);
  }

  _sidebar() {
    const reg = this._regions[this._selected];
    const side = document.createElement('div');
    side.style.cssText = 'flex:1;min-width:150px;display:flex;flex-direction:column;gap:8px';
    if (!reg) return side;

    const title = document.createElement('div');
    title.style.cssText = 'font-size:.7rem;font-weight:800;letter-spacing:.08em;color:var(--accent);text-transform:uppercase';
    title.textContent = `Region ${this._selected + 1} / ${this._regions.length}`;
    side.appendChild(title);

    const pos = document.createElement('div');
    pos.style.cssText = 'font-size:.66rem;color:var(--text-3);font-family:monospace';
    pos.textContent = `x ${Math.round(reg.x)}%  y ${Math.round(reg.y)}%  w ${Math.round(reg.w)}%  h ${Math.round(reg.h)}%`;
    side.appendChild(pos);

    const pLbl = document.createElement('label');
    pLbl.style.cssText = 'font-size:.75rem;color:var(--text-3)';
    pLbl.textContent = 'Region prompt';
    const ta = document.createElement('textarea');
    ta.rows = 3; ta.value = reg.prompt; ta.placeholder = 'What is unique to this region...';
    ta.style.cssText = 'width:100%;font-size:.78rem;resize:vertical';
    ta.addEventListener('input', () => { reg.prompt = ta.value; this._onChange(); });
    side.appendChild(pLbl); side.appendChild(ta);

    const wLbl = document.createElement('div');
    wLbl.style.cssText = 'display:flex;justify-content:space-between;font-size:.75rem;color:var(--text-3)';
    const wVal = document.createElement('span');
    wVal.textContent = reg.weight.toFixed(1); wVal.style.cssText = 'color:var(--accent);font-family:monospace';
    wLbl.appendChild(document.createTextNode('Weight')); wLbl.appendChild(wVal);
    const wSlider = document.createElement('input');
    wSlider.type = 'range'; wSlider.min = '0.1'; wSlider.max = '3'; wSlider.step = '0.1';
    wSlider.value = String(reg.weight); wSlider.style.width = '100%';
    wSlider.addEventListener('input', () => { reg.weight = parseFloat(wSlider.value); wVal.textContent = reg.weight.toFixed(1); this._onChange(); });
    side.appendChild(wLbl); side.appendChild(wSlider);

    return side;
  }

  _paintNumeric() {
    const grid_ = document.createElement('div');
    grid_.style.cssText = 'display:flex;flex-direction:column;gap:8px';
    this._regions.forEach((reg, i) => {
      const box = document.createElement('div');
      box.style.cssText = `border:1px solid ${i === this._selected ? 'var(--accent)' : 'var(--border-2)'};border-radius:var(--r-sm);padding:6px;display:flex;flex-direction:column;gap:4px`;

      const head = document.createElement('div');
      head.style.cssText = 'font-size:.7rem;font-weight:700;color:var(--text-3)';
      head.textContent = `Region ${i + 1}`;
      box.appendChild(head);

      const promptInp = document.createElement('input');
      promptInp.type = 'text'; promptInp.value = reg.prompt; promptInp.placeholder = 'Region prompt';
      promptInp.style.cssText = 'font-size:.78rem;width:100%';
      promptInp.addEventListener('focus', () => { this._selected = i; });
      promptInp.addEventListener('input', () => { reg.prompt = promptInp.value; this._onChange(); });
      box.appendChild(promptInp);

      const nums = document.createElement('div');
      nums.style.cssText = 'display:grid;grid-template-columns:repeat(5,1fr);gap:4px';
      const fields = [
        ['x', 'x', 0, 100], ['y', 'y', 0, 100], ['w', 'w', 1, 100], ['h', 'h', 1, 100], ['wt', 'weight', 0.1, 3],
      ];
      for (const [lbl, prop, lo, hi] of fields) {
        const cell = document.createElement('div');
        cell.style.cssText = 'display:flex;flex-direction:column';
        const cl = document.createElement('span');
        cl.textContent = lbl; cl.style.cssText = 'font-size:.6rem;color:var(--text-3)';
        const inp = document.createElement('input');
        inp.type = 'number'; inp.min = String(lo); inp.max = String(hi);
        inp.step = prop === 'weight' ? '0.1' : '1';
        inp.value = String(prop === 'weight' ? reg.weight : Math.round(reg[prop]));
        inp.style.cssText = 'font-size:.72rem;width:100%';
        inp.addEventListener('input', () => {
          let v = parseFloat(inp.value); if (isNaN(v)) return;
          v = clamp(v, lo, hi);
          if (prop === 'weight') reg.weight = v; else reg[prop] = v;
          // keep box inside the frame
          reg.w = clamp(reg.w, 1, 100 - reg.x);
          reg.h = clamp(reg.h, 1, 100 - reg.y);
          this._onChange();
        });
        cell.appendChild(cl); cell.appendChild(inp);
        nums.appendChild(cell);
      }
      box.appendChild(nums);
      grid_.appendChild(box);
    });
    this._content.appendChild(grid_);
  }

  // -- Drag --------------------------------------------------------------------

  _beginDrag(e, idx, mode) {
    e.preventDefault();
    const r = this._regions[idx];
    this._drag = { mode, idx, sx: e.clientX, sy: e.clientY, orig: { x: r.x, y: r.y, w: r.w, h: r.h } };
    const move = (ev) => this._dragMove(ev);
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      this._dragEnd();
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  _dragMove(e) {
    if (!this._drag) return;
    const d = this._drag;
    const dxp = (e.clientX - d.sx) / this._svgW * 100;
    const dyp = (e.clientY - d.sy) / this._svgH * 100;
    const r = this._regions[d.idx];
    if (d.mode === 'move') {
      r.x = clamp(d.orig.x + dxp, 0, 100 - d.orig.w);
      r.y = clamp(d.orig.y + dyp, 0, 100 - d.orig.h);
    } else {
      r.w = clamp(d.orig.w + dxp, 4, 100 - d.orig.x);
      r.h = clamp(d.orig.h + dyp, 4, 100 - d.orig.y);
    }
    this._paint();
  }

  _dragEnd() {
    const d = this._drag; this._drag = null;
    if (d) {
      const r = this._regions[d.idx];
      if (d.mode === 'move') {
        r.x = clamp(snap(r.x), 0, 100 - r.w);
        r.y = clamp(snap(r.y), 0, 100 - r.h);
      } else {
        const right = snap(r.x + r.w), bottom = snap(r.y + r.h);
        r.w = clamp(right - r.x, 4, 100 - r.x);
        r.h = clamp(bottom - r.y, 4, 100 - r.y);
      }
    }
    this._render();
    this._onChange();
  }

  _onChange() {
    if (this._opts.onChange) this._opts.onChange(this.getRegions());
  }
}
