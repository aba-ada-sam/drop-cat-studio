/**
 * Drop Cat Go Studio -- Chat.
 * Talk with an LLM, ask for an image, review/adjust the proposed prompt,
 * generate via Forge, refine through conversation, animate the result.
 */
import { pollJob } from './api.js';
import { el, pathToUrl } from './components.js';
import { toast, apiFetch } from './shell/toast.js';
import { handoff } from './handoff.js';

const HISTORY_KEY = 'dropcat_chat_history';
const STYLE_KEY = 'dropcat_chat_preset';
const SEND_CAP = 40;

// M21: providers that silently degrade from the configured cloud/paid LLM to
// an uncensored fallback (core/llm_router.py UNCENSORED_PROVIDERS is the
// canonical list) -- flagged with a distinct color so the degrade is visible
// instead of discarded client-side.
const _UNCENSORED_PROVIDERS = ['featherless', 'kobold'];

// Module-scoped so receiveHandoff() (called by app.js outside init()'s
// closure, once TAB_HANDOFF wires it -- see tab-express.js's _applyImageFn
// pattern) can reach into the live tab instance.
let _recvApply = null;

// M20: Image Studio -> Chat handoff. Populates a prompt card with the
// incoming prompt/seed/preset so the user can keep iterating in
// conversation. See tab-image-studio.js's "Send to Chat" button.
export function receiveHandoff(data) {
  _recvApply?.(data);
}

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

  // M22: was labeled "Style" here vs "Preset" in Image Studio for the exact
  // same catalog (core/image_presets.py) -- unified on "Preset".
  const styleSel = el('select', { style: 'font-size:.8rem;' });
  const styleDesc = el('span', { style: 'font-size:.72rem; color:var(--text-3);' });
  root.appendChild(el('div', { style: 'display:flex; align-items:center; gap:8px; flex-wrap:wrap;' }, [
    el('span', { style: 'font-size:.72rem; color:var(--text-3); text-transform:uppercase; letter-spacing:.05em;', text: 'Preset' }),
    styleSel,
    styleDesc,
  ]));

  // C6 + M34: Chat used to hardcode subject="auto"/creature=false server-side
  // with no UI to override it at all. Mirrors Image Studio's Subject row
  // (tab-image-studio.js), shown only for NSFW presets, plus M34's
  // always-visible hint so the controls aren't a total surprise when they
  // do appear.
  const subjectHint = el('div', {
    style: 'font-size:.68rem; color:var(--text-3); font-style:italic;',
    text: 'NSFW presets add Subject / Creature controls here.',
  });
  const subjectSel = el('select', { style: 'font-size:.8rem; display:none;' }, [
    el('option', { value: 'auto', text: 'Auto (detect from prompt)' }),
    el('option', { value: 'male', text: 'Male' }),
    el('option', { value: 'female', text: 'Female' }),
    el('option', { value: 'multi', text: 'Multi: man + woman (regional)' }),
    el('option', { value: 'multi_male', text: 'Multi: two+ men (regional)' }),
    el('option', { value: 'multi_female', text: 'Multi: two+ women (regional)' }),
  ]);
  const creatureCb = el('input', { type: 'checkbox', id: 'chat-is-creature-cb' });
  const subjectRow = el('div', { style: 'display:none; align-items:center; gap:14px; flex-wrap:wrap;' }, [
    el('span', { style: 'font-size:.72rem; color:var(--text-3); text-transform:uppercase; letter-spacing:.05em;', text: 'Subject' }),
    subjectSel,
    el('label', { for: 'chat-is-creature-cb', style: 'display:flex; align-items:center; gap:5px; font-size:.8rem; color:var(--text); cursor:pointer;' }, [
      creatureCb,
      el('span', { text: 'Anthropomorphic creature' }),
    ]),
  ]);
  root.appendChild(subjectHint);
  root.appendChild(subjectRow);

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
    // M22: this option used to just say "Current (no style override)", which
    // didn't explain what "current" meant. Spell out the actual behavior --
    // it renders on whatever checkpoint Forge already has loaded, unlike a
    // real preset which actively switches Forge to a specific checkpoint.
    styleSel.appendChild(el('option', { value: '', text: 'No preset (render on whatever Forge already has loaded)' }));
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
    // C6/M34: same gating Image Studio uses -- Subject/Creature only matter
    // once an NSFW preset is actively steering Forge's checkpoint.
    subjectRow.style.display = (p && p.nsfw) ? 'flex' : 'none';
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
    const {
      url, path, seed, prompt, negative, width, height, steps, cfgVal, preset, checkpointUsed,
      subject, creature, subjectUsed, creatureApplied,
    } = opts;

    const img = el('img', {
      src: url,
      style: 'max-width:100%; border-radius:8px; cursor:pointer; display:block;',
      title: 'Click to open full size',
    });
    img.addEventListener('click', () => window.open(url, '_blank'));

    // C6: echo the resolved subject/creature the same way Image Studio's
    // resultMeta does, instead of leaving them invisible after the fact.
    const metaBits = [checkpointUsed ? `seed ${seed}  --  ${checkpointUsed}` : `seed ${seed}`];
    if (subjectUsed && subjectUsed !== 'n/a') metaBits.push(`subject: ${subjectUsed}`);
    if (creature && creatureApplied) metaBits.push('creature');
    if (creature && !creatureApplied) metaBits.push('creature NOT applied (multi-subject not yet supported)');
    const seedNote = el('div', {
      style: 'font-size:.7rem; color:var(--text-3); margin-top:4px;',
      text: metaBits.join('  --  '),
    });

    const refineBtn  = el('button', { class: 'btn btn-sm', text: 'Refine' });
    const reseedBtn  = el('button', { class: 'btn btn-sm', text: 'Same prompt, new seed' });
    const animateBtn = el('button', { class: 'btn btn-sm', text: 'Animate this' });
    // M20: carries prompt/seed/preset over to Image Studio via handoff.js,
    // the same mechanism Express/Bridges use to hand off to other tabs.
    const sendBtn = el('button', { class: 'btn btn-sm', text: 'Send to Image Studio' });
    const actions = el('div', { style: 'display:flex; gap:6px; margin-top:8px; flex-wrap:wrap;' }, [refineBtn, reseedBtn, animateBtn, sendBtn]);
    const reseedErr = el('div', { style: 'display:none; font-size:.72rem; color:#e88; margin-top:6px;' });

    const videoInput = el('input', { type: 'text', style: 'flex:1; font-size:.82rem;', placeholder: 'Describe the motion...' });
    const goBtn = el('button', { class: 'btn btn-sm btn-primary', text: 'Go', style: 'flex-shrink:0;' });
    const videoRow = el('div', { style: 'display:none; gap:6px; margin-top:8px;' }, [videoInput, goBtn]);

    refineBtn.addEventListener('click', () => { inputTa.focus(); });

    sendBtn.addEventListener('click', () => {
      handoff('image-studio', {
        type: 'image-prompt', prompt, negative, width, height, steps, cfg: cfgVal, seed, preset,
        subject: subjectUsed && subjectUsed !== 'n/a' ? subjectUsed : subject, creature,
      });
      document.querySelector('.rail-tab[data-tab="image-studio"]')?.click();
    });

    reseedBtn.addEventListener('click', async () => {
      reseedBtn.disabled = true;
      reseedErr.style.display = 'none';
      try {
        const data = await apiFetch('/api/chat-studio/generate-image', {
          method: 'POST',
          body: JSON.stringify({ prompt, negative_prompt: negative, width, height, steps, cfg: cfgVal, seed: -1, preset, subject, creature }),
          context: 'chat.generate-image',
        });
        _currentPromptText = prompt;
        _currentImagePath = data.image_path;
        _addImageBubble({
          url: data.image_url, path: data.image_path, seed: data.seed, prompt, negative, width, height, steps, cfgVal, preset, checkpointUsed: data.checkpoint_used,
          subject, creature, subjectUsed: data.subject_used, creatureApplied: data.creature_applied,
        });
      } catch (e) {
        // M23: apiFetch already toasted + logged the server error centrally --
        // this adds the inline message Image Studio shows (errBox), which
        // Chat was missing (it only ever toasted).
        reseedErr.textContent = e.message || 'Generation failed';
        reseedErr.style.display = '';
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

    const bubble = _bubble('assistant', [img, seedNote, actions, reseedErr, videoRow]);
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
    const genErr = el('div', { style: 'display:none; font-size:.72rem; color:#e88; margin-top:6px;' });
    genBtn.addEventListener('click', async () => {
      const prompt = promptTa.value.trim();
      if (!prompt) { toast('Prompt is empty', 'error'); return; }
      const negative = negTa.value.trim();
      const width  = parseInt(widthIn.value, 10)  || 1024;
      const height = parseInt(heightIn.value, 10) || 1024;
      const steps  = parseInt(stepsIn.value, 10)  || 25;
      const cfgVal = parseFloat(cfgIn.value)      || 5.0;

      genBtn.disabled = true;
      genErr.style.display = 'none';
      const origText = genBtn.textContent;
      genBtn.textContent = 'Generating...';
      const preset = styleSel.value || '';
      // C6: subject/creature now travel with the request instead of being
      // silently hardcoded to auto/false server-side.
      const subject = subjectSel.value || 'auto';
      const creature = creatureCb.checked === true;
      try {
        const data = await apiFetch('/api/chat-studio/generate-image', {
          method: 'POST',
          body: JSON.stringify({ prompt, negative_prompt: negative, width, height, steps, cfg: cfgVal, seed: -1, preset, subject, creature }),
          context: 'chat.generate-image',
        });
        _currentPromptText = prompt;
        _currentImagePath = data.image_path;
        _addImageBubble({
          url: data.image_url, path: data.image_path, seed: data.seed, prompt, negative, width, height, steps, cfgVal, preset, checkpointUsed: data.checkpoint_used,
          subject, creature, subjectUsed: data.subject_used, creatureApplied: data.creature_applied,
        });
      } catch (e) {
        // M23: apiFetch already toasted + logged the server error centrally --
        // this adds the inline message (matching Image Studio's errBox),
        // which Chat was missing before (it only ever toasted).
        genErr.textContent = e.message || 'Generation failed';
        genErr.style.display = '';
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
      genErr,
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

      const replyBubble = _addTextBubble('assistant', data.reply || '...');
      // M21: provider_used was computed server-side and discarded here --
      // a silent degrade (cloud LLM down -> uncensored fallback answered
      // instead) was invisible. Surface it, calling out the fallback case.
      if (data.provider_used) {
        const isFallback = _UNCENSORED_PROVIDERS.includes(data.provider_used);
        replyBubble.appendChild(el('div', {
          style: `font-size:.68rem; margin-top:4px; ${isFallback ? 'color:#e0a030;' : 'color:var(--text-3);'}`,
          text: isFallback ? `via ${data.provider_used} (uncensored fallback)` : `via ${data.provider_used}`,
        }));
      }
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

  // M20: receive side of the Image Studio -> Chat handoff. Drops a prompt
  // card pre-filled with the incoming prompt/preset/subject/creature (seed
  // is not carried into the card -- Chat's card has no seed field by design,
  // it always generates fresh; use "Same prompt, new seed" after generating
  // once here if a specific seed matters).
  _recvApply = (data) => {
    if (data?.type === 'image-prompt' && data.prompt) {
      if (data.preset && styleSel.querySelector(`option[value="${data.preset}"]`)) {
        styleSel.value = data.preset;
        try { localStorage.setItem(STYLE_KEY, styleSel.value); } catch (e) { /* non-fatal */ }
        _updateStyleDesc();
      }
      if (data.subject && subjectSel.querySelector(`option[value="${data.subject}"]`)) subjectSel.value = data.subject;
      creatureCb.checked = !!data.creature;
      _addTextBubble('assistant', 'Picked up from Image Studio -- review the prompt below.');
      _addPromptCard({ prompt: data.prompt, negative: data.negative || '', width: data.width, height: data.height });
    }
  };

  _loadStyles();
}
