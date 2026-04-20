# Drop Cat Go Studio — Designer Brief

## What This Is

Drop Cat Go Studio is a personal AI video production tool owned by Andrew. It runs locally at `http://127.0.0.1:7860` as a FastAPI + vanilla JS web app with no build step. Andrew uses it to:

1. **Generate Images** — text → image via Stable Diffusion (Forge)
2. **Create Videos** — image → AI video (WanGP)
3. **Add Transitions** — bridge clips between videos
4. **Ken Burns / Slideshows** — image sequences with motion
5. **Video Tools** — batch transforms, music mixing

The design system is called **Obsidian Studio** — a dark violet/electric-purple theme with near-black backgrounds, electric violet (`#7c6ff7`) as the primary accent, and cream text.

---

## File Map (what you have)

```
static/
  index.html                  — full app shell (HTML structure, no framework)
  css/
    design-system.css         — ALL CSS: tokens, layout, components, tabs
    tabs.css                  — tab-panel visibility rules (small file)
  js/
    components.js             — shared JS UI factory: el(), toast(), createSlider(), etc.
    app.js                    — shell controller: tab routing, split pane, gallery init
    handoff.js                — inter-tab data passing
    shell/
      gallery.js              — right-panel persistent history gallery
      command-palette.js      — Ctrl+K command palette
      toast.js                — toast notifications + apiFetch wrapper
    tab-sd-prompts.js         — Generate Images tab (main generation workflow)
    tab-fun-videos.js         — Create Videos tab
    tab-bridges.js            — Add Transitions tab
    panel-image2video.js      — Ken Burns slideshow tab
    panel-video-tools.js      — Video tools tab
    panel-wildcards.js        — Wildcard manager tab
```

The backend (Python/FastAPI) is **not in scope** — do not modify `.py` files.

---

## Layout Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  HEADER: logo | Gallery | Errors | Search | Help | Settings     │
├──────────────┬──────────────────────────────┬───────────────────┤
│              │                              │                   │
│  LEFT RAIL   │    CENTER (active tab)       │   RIGHT PANEL     │
│  200px wide  │    flex:1                    │   ~35% width      │
│              │                              │                   │
│  Tab nav:    │  Each tab renders its own    │  #split-gallery   │
│  • Generate  │  UI here. Content varies     │  Persistent cross-│
│  • Videos    │  by tab.                     │  tab history      │
│  • Bridges   │                              │  gallery          │
│  • Ken Burns │                              │                   │
│  • Tools     │                              │                   │
│  • Wildcards │                              │                   │
│  • Services  │                              │                   │
│  • Settings  │                              │                   │
│              │                              │                   │
├──────────────┴──────────────────────────────┴───────────────────┤
│  FOOTER: Server Logs (collapsible)                              │
└─────────────────────────────────────────────────────────────────┘
```

The right panel is draggable (split divider). The left rail can collapse to 52px icon-only mode.

---

## Current Design System Tokens

```css
/* Surfaces */
--bg:          #0b0b12   /* near-black, slight violet */
--bg-raised:   #10101a
--surface:     #16161f   /* card backgrounds */
--surface-2:   #1e1e2a   /* input backgrounds */
--surface-3:   #26263a   /* hover states */

/* Accent */
--accent:      #7c6ff7   /* electric violet — primary action color */
--accent-2:    #b0a4ff   /* lighter violet for labels/links */

/* Text */
--text:        #f0f0f8   /* near white */
--text-2:      #9898b8   /* secondary */
--text-3:      #5a5a78   /* muted */

/* Semantic */
--green:  #3dba6b  --red: #e05252  --amber: #e8a840  --blue: #4d9fd6

/* Radii */
--r-sm: 5px  --r-md: 9px  --r-lg: 13px  --r-xl: 18px
```

---

## Priority Problems to Solve

### 1. Workflow is Invisible

The core pipeline is: **Generate Image → Create Video → Add Transitions → Export**. Nothing in the UI communicates this. A new user opening the app has no idea what order to use the tabs or how they connect.

**What exists:** A "🎬 Create Video" button was recently added to the image viewer — it handoffs the current image to the Create Videos tab. But there's no broader workflow guidance.

**Goals:**
- Make the pipeline order obvious at a glance
- Show which tabs feed into which (Generate Images feeds Create Videos feeds Transitions)
- Consider a "Getting Started" or welcome state for first-time use

### 2. Right Panel Underuse

The right panel (`#split-gallery`) is a persistent gallery of all past generations across all tabs. When empty (fresh session or new install) it shows "No generations yet." This wastes a third of the screen.

A `setPreview()` function was recently added to `gallery.js` — after image generation, the right panel now shows the current result with action buttons. But the panel is still wasted on non-generating tabs and on first load.

**Goals:**
- What should the right panel show when the user hasn't generated anything yet?
- What should it show on non-SD tabs (Create Videos, Bridges, etc.)?
- Should the panel be context-sensitive per tab, or always gallery history?

### 3. Tab Navigation Labels Are Unclear

Current left rail labels:
- "Generate Images / Prompts → Images" ← fine
- "Create Videos / Images → Videos" ← fine
- "Add Transitions / Connect clips" ← vague
- "Audio & Export / Final output" ← confusing, this is actually Ken Burns slideshow
- "Video Tools / Batch transforms" ← ok
- "Wildcard Manager" ← jargon (SD-specific term)

**Goal:** Labels and subtitles should be plain English for what the tab does, not what it's technically called.

### 4. Dense Controls in Generate Images Tab

The Generate Images tab packs many controls into a scrollable accordion: Prompt, Regional (Forge Couple), Refinements (ADetailer/HiRes/Img2Img), Advanced, Gallery. The accordion pattern requires multiple clicks to access common settings.

**Goals:**
- The most-used controls (prompt, seed, steps, size) should be immediately visible
- Advanced controls can stay in accordions
- The image result should be prominent — it's currently squeezed above the scrollable area

### 5. Status Pills Are Inscrutable

The header shows service status pills for Forge, WanGP, ACE-Step, Ollama. They show a colored dot + service name but don't communicate what they mean to a non-technical user.

**Goal:** Status indicators should say what impact they have ("Image generation ready" vs "Image generation offline") rather than showing service names.

---

## Constraints

- **Vanilla JS only** — no React, Vue, or any framework. Components are built with the `el()` factory in `components.js`.
- **No build step** — files are served directly. Import paths must be relative with `?v=YYYYMMDD[letter]` cache-busting stamps. When you change any file, bump the letter on that file's stamp in every file that imports it.
- **The backend is untouchable** — only modify files in `static/`.
- **Do not break existing functionality** — the app is in active daily use.
- **CSS is single-file** — all styles live in `design-system.css`. There is no component-level CSS isolation.

---

## How the JS Component System Works

All DOM creation goes through the `el()` factory:

```js
import { el, toast, createSlider } from './components.js?v=20260414';

// el(tag, {props}, [children])
const btn = el('button', { class: 'btn btn-primary', text: 'Click me', onclick() { ... } });
const div = el('div', { style: 'display:flex; gap:8px' }, [btn, el('span', { text: 'hello' })]);
```

`createSlider(container, { label, min, max, step, value })` appends a labeled range input and returns the `<input>` element.

`toast(message, type)` shows a toast notification. Type is `'success'`, `'error'`, `'info'`, or `'warning'`.

---

## How Tab Handoff Works

Tabs can pass data to each other via `handoff.js`:

```js
// Sender (e.g., Generate Images)
import { handoff } from './handoff.js?v=20260415';
handoff('fun-videos', { type: 'image', url: imageDataUrl, path: '/output/file.png' });
document.querySelector('[data-tab="fun-videos"]')?.click();  // switch tab

// Receiver (e.g., Create Videos) — called automatically when user lands on tab
export function receiveHandoff(data) {
  if (data.type === 'image') loadStartImage(data.path, data.url);
}
```

---

## Gallery / Right Panel

The gallery (`shell/gallery.js`) manages `#split-gallery`. Key exports:

```js
// Push a generation to the persistent history (saved to SQLite)
pushFromTab(tab, savedPath, prompt, seed, settingsObject);

// Show the current result prominently in the right panel
setPreview(imageUrl, prompt, [
  { label: '🎬 Create Video', primary: true, onClick: fn },
  { label: '⬇ Save', onClick: fn },
]);
```

The detail overlay (click on any gallery item) shows the image full-size with Load Settings and Branch & Tweak buttons.

---

## Andrew's Screen

Andrew uses a 49" ultrawide at 5120×1440. The CSS already has responsive breakpoints for this:

```css
@media (min-width: 3840px) { /* ultrawide — 3-column layout, larger type */ }
@media (min-width: 2560px) { /* wide — expanded sidebar + main */ }
```

Don't break these breakpoints. Test at 1440p and consider 5120×1440.

---

## Out of Scope

- Mobile/tablet — this is a desktop-only tool
- Dark/light mode toggle — dark only
- The splash screen — do not modify it, Andrew likes it
- Backend Python files — not in the zip, not your concern
- Any feature that requires a new API endpoint
