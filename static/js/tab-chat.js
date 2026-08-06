/**
 * Drop Cat Go Studio -- Chat.
 * Talk with an LLM, ask for an image, review/adjust the proposed prompt,
 * generate via Forge, refine through conversation, animate the result.
 */
import { pollJob } from './api.js?v=20260806a';
import { el, pathToUrl } from './components.js?v=20260806a';
import { toast, apiFetch } from './shell/toast.js?v=20260806a';

const HISTORY_KEY = 'dropcat_chat_history';
const STYLE_KEY = 'dropcat_chat_style';
const SEND_CAP = 40;

export function init(panel) {
  panel.innerHTML = '';

  // -- State -------------------------------------------------------------
  let history = _loadHistory();
  let _currentPromptText = '';
  let _currentImagePath = null;
  let _lastVideoPrompt = '';
  // Bumped by Clear chat -- lets an in-flight _send() notice its chat was
  // cleared out from under it and discard its own result instead of applying
  // a reply (and re-populating history/_currentPromptText/_currentImagePath)
  // into a transcript the user just wiped.
  let _chatGen = 0;

  // -- Layout --------------------------------------------------------------
  const root = el('div', {
    style: 'max-width:760px; margin:0 auto; padding:24px 16px; display:flex; flex-direction:column; gap:14px;',
  });
  panel.appendChild(root);

  const clearBtn = el('button', { class: 'btn btn-sm', text: 'Clear chat' });
  root.appendChild(el('div', { style: 'display:flex; align-items:center; justify-content:space-between; gap:10px;' }, [
    el('div', {}, [
      el('div', { style: 'font-size:1.3rem; font-weight:700; color:var(--text);', text: 'Chat' }),
      el('div', { style: 'font-size:.82rem; color:var(--text-3);', text: 'Talk it through, review the prompt, generate, refine, animate.' }),
    ]),
    clearBtn,
  ]));

  const styleSel = el('select', { style: 'font-size:.8rem;' });
  const styleDesc = el('span', { style: 'font-size:.72rem; color:var(--text-3);' });
  root.appendChild(el('div', { style: 'display:flex; align-items:center; gap:8px; flex-wrap:wrap;' }, [
    el('span', { style: 'font-size:.72rem; color:var(--text-3); text-transform:uppercase; letter-spacing:.05em;', text: 'Style' }),
    styleSel,
    styleDesc,
  ]));

  const transcript = el('div', {
    style: 'display:flex; flex-direction:column; gap:10px; min-height:320px; max-height:60vh; overflow-y:auto; padding:4px 2px;',
  });
  root.appendChild(transcript);

  const inputTa = el('textarea', {
    rows: '2',
    style: 'flex:1; resize:vertical; font-size:.9rem;',
    placeholder: 'Ask for an image, chat, or describe changes... (Enter to send, Shift+Enter for a newline)',
  });
  const sendBtn = el('button', { class: 'btn btn-primary', text: 'Send', style: 'flex-shrink:0;' });
  root.appendChild(el('div', { style: 'display:flex; gap:8px; align-items:flex-end;' }, [inputTa, sendBtn]));

  // -- Helpers ---------------------------------------------------------------
  function _scrollToBottom() {
    transcript.scrollTop = transcript.scrollHeight;
  }

  function _bubble(role, children) {
    const isUser = role === 'user';
    return el('div', {
      class: 'card',
      style: [
        'max-width:82%; padding:10px 12px;',
        isUser ? 'align-self:flex-end; background:var(--accent); color:#000;'
               : 'align-self:flex-start; background:var(--bg-raised); color:var(--text);',
      ].join(' '),
    }, children);
  }

  function _addTextBubble(role, text) {
    const bubble = _bubble(role, [
      el('div', { style: 'white-space:pre-wrap; font-size:.9rem; line-height:1.45;', text: text || '' }),
    ]);
    transcript.appendChild(bubble);
    _scrollToBottom();
    return bubble;
  }

  const _activePollers = [];

  // -- Style preset (same catalog Image Studio uses) ---------------------------
  let _presetsCache = [];

  async function _loadStyles() {
    try {
      const data = await apiFetch('/api/image-studio/presets', { context: 'chat.presets', silent: true });
      _presetsCache = data.presets || [];
    } catch (e) {
      _presetsCache = [];
    }
    styleSel.innerHTML = '';
    styleSel.appendChild(el('option', { value: '', text: 'Current (no style override)' }));
    for (const p of _presetsCache) {
      styleSel.appendChild(el('option', { value: p.id, text: p.label }));
    }
    let saved = '';
    try { saved = localStorage.getItem(STYLE_KEY) || ''; } catch (e) { /* non-fatal */ }
    if (styleSel.querySelector(`option[value="${saved}"]`)) styleSel.value = saved;
    _updateStyleDesc();
  }

  function _updateStyleDesc() {
    const p = _presetsCache.find(p => p.id === styleSel.value);
    styleDesc.textContent = p ? p.description : '';
  }

  styleSel.addEventListener('change', () => {
    try { localStorage.setItem(STYLE_KEY, styleSel.value); } catch (e) { /* non-fatal */ }
    _updateStyleDesc();
  });

  function _saveHistory() {
    try { localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(-200))); } catch (e) { /* storage full/unavailable -- non-fatal */ }
  }

  function _loadHistory() {
    try {
      const raw = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
      return Array.isArray(raw) ? raw : [];
    } catch (e) {
      return [];
    }
  }

  // -- Image bubble (generated result + actions) ------------------------------
  function _addImageBubble(opts) {
    const { url, path, seed, prompt, negative, width, height, steps, cfgVal, preset, checkpointUsed } = opts;

    const img = el('img', {
      src: url,
      style: 'max-width:100%; border-radius:8px; cursor:pointer; display:block;',
      title: 'Click to open full size',
    });
    img.addEventListener('click', () => window.open(url, '_blank'));

    const seedNote = el('div', {
      style: 'font-size:.7rem; color:var(--text-3); margin-top:4px;',
      text: checkpointUsed ? `seed ${seed}  --  ${checkpointUsed}` : `seed ${seed}`,
    });

    const refineBtn  = el('button', { class: 'btn btn-sm', text: 'Refine' });
    const reseedBtn  = el('button', { class: 'btn btn-sm', text: 'Same prompt, new seed' });
    const animateBtn = el('button', { class: 'btn btn-sm', text: 'Animate this' });
    const actions = el('div', { style: 'display:flex; gap:6px; margin-top:8px; flex-wrap:wrap;' }, [refineBtn, reseedBtn, animateBtn]);

    const videoInput = el('input', { type: 'text', style: 'flex:1; font-size:.82rem;', placeholder: 'Describe the motion...' });
    const goBtn = el('button', { class: 'btn btn-sm btn-primary', text: 'Go', style: 'flex-shrink:0;' });
    const videoRow = el('div', { style: 'display:none; gap:6px; margin-top:8px;' }, [videoInput, goBtn]);

    refineBtn.addEventListener('click', () => { inputTa.focus(); });

    reseedBtn.addEventListener('click', async () => {
      reseedBtn.disabled = true;
      try {
        const data = await apiFetch('/api/chat-studio/generate-image', {
          method: 'POST',
          body: JSON.stringify({ prompt, negative_prompt: negative, width, height, steps, cfg: cfgVal, seed: -1, preset }),
          context: 'chat.generate-image',
        });
        _currentPromptText = prompt;
        _currentImagePath = data.image_path;
        _addImageBubble({ url: data.image_url, path: data.image_path, seed: data.seed, prompt, negative, width, height, steps, cfgVal, preset, checkpointUsed: data.checkpoint_used });
      } catch (e) {
        // apiFetch already toasted the server error string
      } finally {
        reseedBtn.disabled = false;
      }
    });

    animateBtn.addEventListener('click', () => {
      videoInput.value = _lastVideoPrompt || '';
      videoRow.style.display = 'flex';
      videoInput.focus();
    });

    goBtn.addEventListener('click', async () => {
      const vp = videoInput.value.trim();
      if (!vp) { toast('Describe the motion first', 'error'); return; }
      goBtn.disabled = true;
      try {
        const data = await apiFetch('/api/chat-studio/animate', {
          method: 'POST',
          body: JSON.stringify({ image_path: path, video_prompt: vp }),
          context: 'chat.animate',
        });
        videoRow.style.display = 'none';
        _addVideoJobBubble(data.job_id);
      } catch (e) {
        // apiFetch already toasted the server error string
      } finally {
        goBtn.disabled = false;
      }
    });

    const bubble = _bubble('assistant', [img, seedNote, actions, videoRow]);
    transcript.appendChild(bubble);
    _scrollToBottom();
    return bubble;
  }

  // -- Video job bubble (polls the generic job queue) -------------------------
  function _addVideoJobBubble(jobId) {
    const statusEl = el('div', { style: 'font-size:.82rem; color:var(--text-3);', text: 'Rendering video...' });
    const track = el('div', { style: 'height:4px; width:220px; background:var(--border-2); border-radius:2px; overflow:hidden; margin-top:6px;' });
    const fill  = el('div', { style: 'height:100%; width:0%; background:var(--accent); transition:width .4s;' });
    track.appendChild(fill);

    const bubble = _bubble('assistant', [statusEl, track]);
    transcript.appendChild(bubble);
    _scrollToBottom();

    const poller = pollJob(
      jobId,
      (j) => {
        fill.style.width = `${j.progress || 0}%`;
        statusEl.textContent = j.message || (j.status === 'queued' ? 'Queued -- waiting for GPU...' : 'Rendering video...');
      },
      (j) => {
        const outputs = Array.isArray(j.output) ? j.output : [j.output];
        const videoPath = outputs[0];
        bubble.innerHTML = '';
        bubble.appendChild(el('div', { style: 'font-size:.82rem; color:var(--text-3); margin-bottom:4px;', text: 'Video ready' }));
        bubble.appendChild(el('video', {
          controls: '', style: 'max-width:100%; border-radius:8px; background:#000;', src: pathToUrl(videoPath),
        }));
        _scrollToBottom();
      },
      (err) => {
        bubble.innerHTML = '';
        bubble.appendChild(el('div', { style: 'font-size:.82rem; color:#e88;', text: `Video failed: ${err}` }));
      },
      3000,
    );
    _activePollers.push(poller);
  }

  // -- Prompt card (editable, generates on demand) -----------------------------
  function _addPromptCard(imagePrompt) {
    const promptTa = el('textarea', { rows: '3', style: 'width:100%; font-size:.85rem; resize:vertical;' });
    promptTa.value = imagePrompt.prompt || '';
    // _currentPromptText previously only got set once Generate was clicked, so
    // asking for a tweak before that ("actually make it two women" right
    // after seeing the proposal) sent the backend a stale/empty current_prompt
    // -- it had no idea what was actually sitting in this visible, editable
    // card. Keep it live: set on creation, and follow further edits.
    _currentPromptText = promptTa.value;
    promptTa.addEventListener('input', () => { _currentPromptText = promptTa.value; });

    const negTa = el('input', { type: 'text', style: 'width:100%; font-size:.8rem; margin-top:6px;', placeholder: 'Negative prompt (optional)' });
    negTa.value = imagePrompt.negative || '';

    const widthIn  = el('input', { type: 'number', min: '256', max: '2048', step: '64', style: 'width:80px; font-size:.8rem;' });
    widthIn.value  = String(imagePrompt.width  || 1024);
    const heightIn = el('input', { type: 'number', min: '256', max: '2048', step: '64', style: 'width:80px; font-size:.8rem;' });
    heightIn.value = String(imagePrompt.height || 1024);
    const dimsRow = el('div', { style: 'display:flex; align-items:center; gap:8px; margin-top:6px;' }, [
      el('span', { style: 'font-size:.75rem; color:var(--text-3);', text: 'W' }), widthIn,
      el('span', { style: 'font-size:.75rem; color:var(--text-3);', text: 'H' }), heightIn,
    ]);

    const stepsIn = el('input', { type: 'number', min: '1', max: '80', style: 'width:70px; font-size:.8rem;' });
    stepsIn.value = '25';
    const cfgIn = el('input', { type: 'number', min: '1', max: '20', step: '0.5', style: 'width:70px; font-size:.8rem;' });
    cfgIn.value = '3';
    const advanced = el('details', { style: 'margin-top:6px;' }, [
      el('summary', { style: 'cursor:pointer; font-size:.72rem; color:var(--text-3);', text: '+ steps & cfg' }),
      el('div', { style: 'display:flex; align-items:center; gap:10px; margin-top:6px;' }, [
        el('span', { style: 'font-size:.75rem; color:var(--text-3);', text: 'Steps' }), stepsIn,
        el('span', { style: 'font-size:.75rem; color:var(--text-3);', text: 'CFG' }), cfgIn,
      ]),
    ]);

    const genBtn = el('button', { class: 'btn btn-primary btn-sm', text: 'Generate image' });
    genBtn.addEventListener('click', async () => {
      const prompt = promptTa.value.trim();
      if (!prompt) { toast('Prompt is empty', 'error'); return; }
      const negative = negTa.value.trim();
      const width  = parseInt(widthIn.value, 10)  || 1024;
      const height = parseInt(heightIn.value, 10) || 1024;
      const steps  = parseInt(stepsIn.value, 10)  || 25;
      const cfgVal = parseFloat(cfgIn.value)      || 5.0;

      genBtn.disabled = true;
      const origText = genBtn.textContent;
      genBtn.textContent = 'Generating...';
      const preset = styleSel.value || '';
      try {
        const data = await apiFetch('/api/chat-studio/generate-image', {
          method: 'POST',
          body: JSON.stringify({ prompt, negative_prompt: negative, width, height, steps, cfg: cfgVal, seed: -1, preset }),
          context: 'chat.generate-image',
        });
        _currentPromptText = prompt;
        _currentImagePath = data.image_path;
        _addImageBubble({ url: data.image_url, path: data.image_path, seed: data.seed, prompt, negative, width, height, steps, cfgVal, preset, checkpointUsed: data.checkpoint_used });
      } catch (e) {
        // apiFetch already toasted the server error string (409/503/etc.)
      } finally {
        genBtn.disabled = false;
        genBtn.textContent = origText;
      }
    });

    const card = _bubble('assistant', [
      el('div', { style: 'font-size:.72rem; color:var(--text-3); text-transform:uppercase; letter-spacing:.05em; margin-bottom:4px;', text: 'Image prompt' }),
      promptTa,
      negTa,
      dimsRow,
      advanced,
      el('div', { style: 'margin-top:8px;' }, [genBtn]),
    ]);
    transcript.appendChild(card);
    _scrollToBottom();
    return card;
  }

  // -- Send flow ---------------------------------------------------------------
  async function _send() {
    // sendBtn.disabled already guards the button itself, but the Enter-key
    // handler below didn't check it -- typing a follow-up and hitting Enter
    // before the previous reply lands (a completely normal chat pattern) fired
    // a second overlapping _send(), and whichever response resolved first
    // spliced its bubble into whatever was then the end of the transcript,
    // landing replies under the wrong turn and desyncing `history` (sent back
    // to the LLM as literal context) from what was actually typed.
    if (sendBtn.disabled) return;
    const text = inputTa.value.trim();
    if (!text) return;
    inputTa.value = '';
    const myGen = _chatGen;

    _addTextBubble('user', text);
    history.push({ role: 'user', content: text });
    _saveHistory();

    sendBtn.disabled = true;
    const thinking = _addTextBubble('assistant', 'Thinking...');

    try {
      const payload = {
        message: text,
        history: history.slice(-SEND_CAP).map(h => ({ role: h.role, content: h.content })),
        current_prompt: _currentPromptText || null,
        image_path: _currentImagePath || null,
      };
      const data = await apiFetch('/api/chat-studio/message', {
        method: 'POST', body: JSON.stringify(payload), context: 'chat.message',
      });
      thinking.remove();
      if (myGen !== _chatGen) return; // chat was cleared while this was in flight

      _addTextBubble('assistant', data.reply || '...');
      history.push({ role: 'assistant', content: data.reply || '' });
      _saveHistory();

      if (data.image_prompt) {
        _addPromptCard(data.image_prompt);
      }
      if (data.video_prompt) {
        _lastVideoPrompt = data.video_prompt;
        // No image generated yet in this session -- nowhere to attach an
        // "Animate this" action, so surface the idea as plain reply text.
        if (!_currentImagePath) {
          _addTextBubble('assistant', `Video idea: ${data.video_prompt}`);
        }
      }
    } catch (e) {
      thinking.remove();
      // apiFetch already toasted the server error string
    } finally {
      sendBtn.disabled = false;
    }
  }

  sendBtn.addEventListener('click', _send);
  inputTa.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      _send();
    }
  });

  clearBtn.addEventListener('click', () => {
    _chatGen++; // any _send() still in flight discards its result on return
    for (const p of _activePollers) { try { p.stop(); } catch (e) { /* already finished */ } }
    _activePollers.length = 0;
    history = [];
    _currentPromptText = '';
    _currentImagePath = null;
    _lastVideoPrompt = '';
    try { localStorage.removeItem(HISTORY_KEY); } catch (e) { /* non-fatal */ }
    transcript.innerHTML = '';
  });

  // -- Restore prior conversation text (image/prompt-card state is not
  // persisted -- only the transcript is, matching the localStorage contract) --
  for (const h of history) {
    _addTextBubble(h.role === 'assistant' ? 'assistant' : 'user', h.content);
  }

  _loadStyles();
}
