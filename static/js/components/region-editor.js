/**
 * Drop Cat Go Studio -- Forge Couple visual SVG region editor (WS6).
 * Usage:
 *   const editor = new RegionEditor(containerEl, { rows, cols, direction, onChange });
 *   editor.getRegions() -> [{ row, col, weight, prompt, bgAnchor }]
 *   editor.setFromGrid(rows, cols, direction) -> rebuild
 */

const SNAP = 5; // percent grid

export class RegionEditor {
  constructor(containerEl, opts = {}) {
    this._el    = containerEl;
    this._opts  = opts;
    this._rows  = opts.rows      || 1;
    this._cols  = opts.cols      || 3;
    this._dir   = opts.direction || 'Horizontal';
    this._regions = [];
    this._selected = null;
    this._mode  = 'visual'; // 'visual' | 'numeric'
    this._init();
  }

  // ── Public API ───────────────────────────────────────────────────────────────

  setFromGrid(rows, cols, direction) {
    this._rows = rows;
    this._cols = cols;
    this._dir  = direction;
    this._buildRegions();
    this._render();
  }

  getRegions() {
    return this._regions.map(r => ({ ...r }));
  }

  setMode(mode) {
    this._mode = mode;
    this._render();
  }

  // ── Init ─────────────────────────────────────────────────────────────────────

  _init() {
    this._buildRegions();
    this._render();
  }

  _buildRegions() {
    const totalCells = this._rows * this._cols;
    const pctW = 100 / this._cols;
    const pctH = 100 / this._rows;
    this._regions = [];
    for (let r = 0; r < this._rows; r++) {
      for (let c = 0; c < this._cols; c++) {
        const existing = this._regions.find(x => x.row === r && x.col === c);
        this._regions.push({
          row: r, col: c,
          x: c * pctW, y: r * pctH,
          w: pctW, h: pctH,
          prompt: existing?.prompt || '',
          weight: existing?.weight || 1.0,
          bgAnchor: (r === 0 && c === 0 && totalCells > 1),
        });
      }
    }
    if (this._selected !== null && this._selected >= this._regions.length) {
      this._selected = null;
    }
  }

  _render() {
    this._el.innerHTML = '';

    // Mode toggle
    const modeRow = document.createElement('div');
    modeRow.style.cssText = 'display:flex;gap:6px;margin-bottom:8px;align-items:center';
    const visualBtn = document.createElement('button');
    visualBtn.textContent = 'Visual';
    visualBtn.className = `btn btn-sm${this._mode === 'visual' ? ' btn-primary' : ''}`;
    visualBtn.style.fontSize = '.72rem';
    visualBtn.addEventListener('click', () => { this._mode = 'visual'; this._render(); });
    const numBtn = document.createElement('button');
    numBtn.textContent = 'Numeric';
    numBtn.className = `btn btn-sm${this._mode === 'numeric' ? ' btn-primary' : ''}`;
    numBtn.style.fontSize = '.72rem';
    numBtn.addEventListener('click', () => { this._mode = 'numeric'; this._render(); });
    modeRow.appendChild(visualBtn);
    modeRow.appendChild(numBtn);
    this._el.appendChild(modeRow);

    if (this._mode === 'visual') {
      this._renderVisual();
    } else {
      this._renderNumeric();
    }
  }

  _renderVisual() {
    const wrapper = document.createElement('div');
    wrapper.style.cssText = 'display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap';

    // SVG canvas
    const aspect = this._dir === 'Vertical' ? 0.6 : 1.6;
    const svgW = 260;
    const svgH = Math.round(svgW / aspect);
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', svgW);
    svg.setAttribute('height', svgH);
    svg.style.cssText = `border:1px solid var(--border-2);border-radius:var(--r-md);cursor:pointer;background:var(--bg);flex-shrink:0`;

    // Draw regions
    for (const reg of this._regions) {
      const x = reg.x / 100 * svgW;
      const y = reg.y / 100 * svgH;
      const w = reg.w / 100 * svgW;
      const h = reg.h / 100 * svgH;
      const isSelected = this._selected === this._regions.indexOf(reg);

      const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      rect.setAttribute('x', x + 1); rect.setAttribute('y', y + 1);
      rect.setAttribute('width', w - 2); rect.setAttribute('height', h - 2);
      rect.setAttribute('fill', isSelected ? 'rgba(212,160,23,.18)' : 'rgba(212,160,23,.05)');
      rect.setAttribute('stroke', isSelected ? 'var(--accent)' : 'var(--border-2)');
      rect.setAttribute('stroke-width', '1.5');
      rect.setAttribute('rx', '3');
      rect.style.cursor = 'pointer';
      const idx = this._regions.indexOf(reg);
      rect.addEventListener('click', () => { this._selected = idx; this._render(); });

      // Label
      const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      text.setAttribute('x', x + w / 2); text.setAttribute('y', y + h / 2);
      text.setAttribute('text-anchor', 'middle'); text.setAttribute('dominant-baseline', 'middle');
      text.setAttribute('fill', isSelected ? 'var(--accent)' : 'var(--text-3)');
      text.setAttribute('font-size', '10');
      text.setAttribute('font-family', 'inherit');
      text.textContent = reg.prompt ? reg.prompt.slice(0, 12) + (reg.prompt.length > 12 ? '...' : '') : `R${reg.row + 1}C${reg.col + 1}`;
      text.style.pointerEvents = 'none';

      svg.appendChild(rect);
      svg.appendChild(text);

      // Pin icon for bgAnchor
      if (reg.bgAnchor) {
        const pin = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        pin.setAttribute('x', x + w - 8); pin.setAttribute('y', y + 12);
        pin.setAttribute('font-size', '12'); pin.setAttribute('fill', 'var(--accent)');
        pin.style.pointerEvents = 'none';
        pin.textContent = '\uD83D\uDCCC';
        svg.appendChild(pin);
      }
    }

    // Draggable divider lines (simplified — visual guides)
    if (this._dir === 'Horizontal' && this._cols > 1) {
      for (let c = 1; c < this._cols; c++) {
        const lineX = c / this._cols * svgW;
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', lineX); line.setAttribute('y1', 0);
        line.setAttribute('x2', lineX); line.setAttribute('y2', svgH);
        line.setAttribute('stroke', 'var(--accent)'); line.setAttribute('stroke-width', '1');
        line.setAttribute('stroke-dasharray', '3,3'); line.setAttribute('opacity', '.5');
        svg.appendChild(line);
      }
    }
    if (this._rows > 1) {
      for (let r = 1; r < this._rows; r++) {
        const lineY = r / this._rows * svgH;
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', 0); line.setAttribute('y1', lineY);
        line.setAttribute('x2', svgW); line.setAttribute('y2', lineY);
        line.setAttribute('stroke', 'var(--accent)'); line.setAttribute('stroke-width', '1');
        line.setAttribute('stroke-dasharray', '3,3'); line.setAttribute('opacity', '.5');
        svg.appendChild(line);
      }
    }

    wrapper.appendChild(svg);

    // Region sidebar (shown when a region is selected)
    if (this._selected !== null) {
      const reg = this._regions[this._selected];
      const sidebar = document.createElement('div');
      sidebar.style.cssText = 'flex:1;min-width:140px;display:flex;flex-direction:column;gap:8px';

      const sideLbl = document.createElement('div');
      sideLbl.style.cssText = 'font-size:.7rem;font-weight:800;letter-spacing:.08em;color:var(--accent);text-transform:uppercase';
      sideLbl.textContent = `Region ${reg.row + 1}/${reg.col + 1}`;
      sidebar.appendChild(sideLbl);

      const promptLbl = document.createElement('label');
      promptLbl.style.cssText = 'font-size:.75rem;color:var(--text-3)';
      promptLbl.textContent = 'Prompt';
      const promptTA = document.createElement('textarea');
      promptTA.rows = 3; promptTA.value = reg.prompt;
      promptTA.style.cssText = 'width:100%;font-size:.78rem;resize:vertical';
      promptTA.placeholder = 'Describe this region...';
      promptTA.addEventListener('input', () => { reg.prompt = promptTA.value; this._renderSVGLabels(svg); this._onChange(); });
      sidebar.appendChild(promptLbl); sidebar.appendChild(promptTA);

      const weightLbl = document.createElement('div');
      weightLbl.style.cssText = 'display:flex;justify-content:space-between;align-items:center;font-size:.75rem;color:var(--text-3)';
      const weightVal = document.createElement('span');
      weightVal.textContent = reg.weight.toFixed(1);
      weightVal.style.cssText = 'color:var(--accent);font-family:monospace';
      weightLbl.appendChild(document.createTextNode('Weight'));
      weightLbl.appendChild(weightVal);
      const weightSlider = document.createElement('input');
      weightSlider.type = 'range'; weightSlider.min = '0.1'; weightSlider.max = '3'; weightSlider.step = '0.1';
      weightSlider.value = String(reg.weight); weightSlider.style.width = '100%';
      weightSlider.addEventListener('input', () => { reg.weight = parseFloat(weightSlider.value); weightVal.textContent = reg.weight.toFixed(1); this._onChange(); });
      sidebar.appendChild(weightLbl); sidebar.appendChild(weightSlider);

      const bgRow = document.createElement('div');
      bgRow.style.cssText = 'display:flex;align-items:center;gap:6px';
      const bgChk = document.createElement('input');
      bgChk.type = 'checkbox'; bgChk.checked = reg.bgAnchor;
      bgChk.addEventListener('change', () => {
        this._regions.forEach(r => { r.bgAnchor = false; });
        if (bgChk.checked) reg.bgAnchor = true;
        this._render();
        this._onChange();
      });
      const bgLbl = document.createElement('label');
      bgLbl.style.cssText = 'font-size:.75rem;cursor:pointer;color:var(--text-2)';
      bgLbl.textContent = '\uD83D\uDCCC Background anchor';
      bgRow.appendChild(bgChk); bgRow.appendChild(bgLbl);
      sidebar.appendChild(bgRow);

      wrapper.appendChild(sidebar);
    }

    this._el.appendChild(wrapper);
  }

  _renderSVGLabels(svg) {
    const texts = svg.querySelectorAll('text');
    texts.forEach(t => {
      const idx = [...svg.children].indexOf(t);
    });
  }

  _renderNumeric() {
    const grid = document.createElement('div');
    grid.style.cssText = 'display:flex;flex-direction:column;gap:6px';
    this._regions.forEach((reg, i) => {
      const row = document.createElement('div');
      row.style.cssText = 'display:grid;grid-template-columns:60px 1fr;gap:6px;align-items:center';
      const label = document.createElement('div');
      label.style.cssText = 'font-size:.72rem;color:var(--text-3)';
      label.textContent = `R${reg.row + 1}C${reg.col + 1}`;
      const promptInp = document.createElement('input');
      promptInp.type = 'text'; promptInp.value = reg.prompt;
      promptInp.style.cssText = 'font-size:.78rem;width:100%';
      promptInp.placeholder = 'Prompt for this region';
      promptInp.addEventListener('input', () => { reg.prompt = promptInp.value; this._onChange(); });
      row.appendChild(label); row.appendChild(promptInp);
      grid.appendChild(row);
    });
    this._el.appendChild(grid);
  }

  _onChange() {
    if (this._opts.onChange) this._opts.onChange(this.getRegions());
  }
}
