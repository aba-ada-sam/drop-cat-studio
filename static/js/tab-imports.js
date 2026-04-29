/**
 * Drop Cat Go Studio -- Import Assets / Project Entry Point
 *
 * Allows users to:
 * - Load existing images, videos, prompts, projects
 * - Start a new project
 * - Browse and restore previous work
 */
import { api } from './api.js?v=20260414';
import { createDropZone, el } from './components.js?v=20260429b';
import { toast } from './shell/toast.js?v=20260429d';

export function init(panel) {
  panel.innerHTML = '';

  const root = el('div', {
    style: 'display:flex; flex-direction:column; gap:20px; padding:20px; background:var(--bg); height:100%; overflow-y:auto;'
  });
  panel.appendChild(root);

  // ════════════════════════════════════════════════════════════════════════════
  // HEADER
  // ════════════════════════════════════════════════════════════════════════════

  const header = el('div', {
    style: 'border-bottom:1px solid var(--border); padding-bottom:12px;'
  });
  root.appendChild(header);

  header.appendChild(el('h2', {
    text: '📥 Import & Project Entry',
    style: 'font-size:1.4rem; color:var(--text); margin-bottom:4px;'
  }));

  header.appendChild(el('p', {
    text: 'Start a new project or load existing images, videos, and prompts to continue creative work.',
    style: 'color:var(--text-2); font-size:.95rem;'
  }));

  // ════════════════════════════════════════════════════════════════════════════
  // NEW PROJECT
  // ════════════════════════════════════════════════════════════════════════════

  const newProjCard = el('div', {
    style: 'padding:16px; background:var(--surface); border:1px solid var(--border); border-radius:var(--r-md);'
  });
  root.appendChild(newProjCard);

  newProjCard.appendChild(el('h3', {
    text: '✨ Start Fresh',
    style: 'font-size:1.1rem; color:var(--accent-2); margin-bottom:8px;'
  }));

  newProjCard.appendChild(el('p', {
    text: 'Begin a new creative project from scratch. Go to "Generate Images" to start with prompts, or import assets first.',
    style: 'color:var(--text-2); font-size:.9rem; margin-bottom:12px; line-height:1.5;'
  }));

  const newProjBtn = el('button', {
    class: 'btn btn-primary',
    text: 'Create New Project',
    style: 'padding:10px 20px;',
    onclick() { toast('New project ready — go to "Generate Images" tab to begin', 'info'); }
  });
  newProjCard.appendChild(newProjBtn);

  // ════════════════════════════════════════════════════════════════════════════
  // IMPORT ASSETS
  // ════════════════════════════════════════════════════════════════════════════

  const importCard = el('div', {
    style: 'padding:16px; background:var(--surface); border:1px solid var(--border); border-radius:var(--r-md);'
  });
  root.appendChild(importCard);

  importCard.appendChild(el('h3', {
    text: '📂 Import Existing Assets',
    style: 'font-size:1.1rem; color:var(--accent-2); margin-bottom:8px;'
  }));

  importCard.appendChild(el('p', {
    text: 'Drag and drop files here, or use the buttons below to import images, videos, and prompts.',
    style: 'color:var(--text-2); font-size:.9rem; margin-bottom:12px; line-height:1.5;'
  }));

  // Drop zone for files
  const dropZone = createDropZone(importCard, {
    label: 'Drop files here',
    onFiles: async (files) => {
      toast(`Loading ${files.length} file(s)...`, 'info');
      // TODO: implement file upload
    }
  });

  const importBtnsRow = el('div', {
    style: 'display:flex; gap:10px; flex-wrap:wrap; margin-top:12px;'
  });
  importCard.appendChild(importBtnsRow);

  const importImgBtn = el('button', {
    class: 'btn btn-sm',
    text: 'Import Images',
    onclick() { toast('Image import coming soon', 'info'); }
  });
  importBtnsRow.appendChild(importImgBtn);

  const importVidBtn = el('button', {
    class: 'btn btn-sm',
    text: 'Import Videos',
    onclick() { toast('Video import coming soon', 'info'); }
  });
  importBtnsRow.appendChild(importVidBtn);

  const importPromptBtn = el('button', {
    class: 'btn btn-sm',
    text: 'Import Prompts',
    onclick() { toast('Prompt import coming soon', 'info'); }
  });
  importBtnsRow.appendChild(importPromptBtn);

  // ════════════════════════════════════════════════════════════════════════════
  // PIPELINE GUIDE
  // ════════════════════════════════════════════════════════════════════════════

  const guideCard = el('div', {
    style: 'padding:16px; background:var(--surface); border:1px solid var(--border-2); border-radius:var(--r-md);'
  });
  root.appendChild(guideCard);

  guideCard.appendChild(el('h3', {
    text: '🗺️ Creative Pipeline Roadmap',
    style: 'font-size:1.1rem; color:var(--accent); margin-bottom:12px;'
  }));

  const stages = [
    { icon: '🎨', name: 'Generate Images', desc: 'Create images from prompts using AI (Forge SD)', next: '→' },
    { icon: '🎬', name: 'Create Videos', desc: 'Turn images into video with AI music', next: '→' },
    { icon: '🌉', name: 'Add Transitions', desc: 'Create transitions between video clips', next: '→' },
    { icon: '🎵', name: 'Audio & Export', desc: 'Mix audio and export final video', next: '' },
  ];

  stages.forEach((stage, i) => {
    const stageRow = el('div', {
      style: `display:flex; gap:12px; align-items:flex-start; padding:10px 0; ${i < stages.length - 1 ? 'border-bottom:1px solid var(--border);' : ''}`
    });

    stageRow.appendChild(el('span', {
      text: stage.icon,
      style: 'font-size:1.2rem; flex-shrink:0;'
    }));

    const textCol = el('div', { style: 'flex:1; min-width:0;' });
    textCol.appendChild(el('strong', {
      text: stage.name,
      style: 'color:var(--text); font-size:.95rem; display:block; margin-bottom:2px;'
    }));
    textCol.appendChild(el('span', {
      text: stage.desc,
      style: 'color:var(--text-2); font-size:.85rem;'
    }));
    stageRow.appendChild(textCol);

    if (stage.next) {
      stageRow.appendChild(el('span', {
        text: stage.next,
        style: 'color:var(--accent); flex-shrink:0; font-weight:600;'
      }));
    }

    guideCard.appendChild(stageRow);
  });

  guideCard.appendChild(el('p', {
    text: '💡 You can enter at any stage — if you have existing images, jump to "Create Videos". If you have videos, go to "Add Transitions".',
    style: 'color:var(--text-3); font-size:.8rem; margin-top:12px; padding-top:12px; border-top:1px solid var(--border); font-style:italic;'
  }));
}
