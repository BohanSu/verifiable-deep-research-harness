const $ = (id) => document.getElementById(id);

const roleLabels = {
  perception: '输入感知',
  planner: '研究规划',
  scout: '检索侦察',
  curator: '证据整理',
  writer: '引用写作',
  verifier: '独立核验',
};
const fallbackLimits = {
  max_attachments: 6,
  max_attachment_bytes: 8 * 1024 * 1024,
  max_total_bytes: 24 * 1024 * 1024,
};
const extensionMediaTypes = {
  png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp',
  wav: 'audio/wav', mp3: 'audio/mpeg', ogg: 'audio/ogg', flac: 'audio/flac', webm: 'audio/webm', m4a: 'audio/mp4', mp4: 'audio/mp4',
  pdf: 'application/pdf', json: 'application/json', csv: 'text/csv', md: 'text/markdown', markdown: 'text/markdown', txt: 'text/plain',
};

let loadedConfig = null;
let runBusy = false;
let attachmentError = '';
const selectedAttachments = [];

async function getJSON(url, options) {
  const response = await fetch(url, options);
  let data;
  try {
    data = await response.json();
  } catch (_) {
    data = {};
  }
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function modelOptions(config = loadedConfig) {
  return Array.isArray(config?.models) ? config.models : [];
}

function profileOptions(config = loadedConfig) {
  if (Array.isArray(config?.profiles) && config.profiles.length) return config.profiles;
  return modelOptions(config).map(model => ({
    id: model.id,
    label: model.label,
    configured: model.configured,
    routes: Object.fromEntries(Object.keys(roleLabels).map(role => [role, model.id])),
    input_modalities: model.modalities || ['text', 'document'],
    capability_status: model.capability_status || 'declared_unverified',
  }));
}

function selectedProfileId(config = loadedConfig) {
  return document.querySelector('input[name="profile"]:checked')?.value
    || config?.default_profile
    || 'team';
}

function selectedProfileOption(config = loadedConfig) {
  const selected = selectedProfileId(config);
  return profileOptions(config).find(option => option?.id === selected) || null;
}

function modelOption(id, config = loadedConfig) {
  return modelOptions(config).find(option => option?.id === id) || null;
}

function humanModelName(value) {
  const text = String(value || '').trim();
  const lower = text.toLowerCase();
  if (lower === 'team') return '三模型协作';
  if (lower.includes('qwen')) return 'Qwen';
  if (lower.includes('gpt')) return 'GPT';
  if (lower.includes('deepseek')) return 'DeepSeek';
  if (lower.includes('mock')) return '离线演示模型';
  return text || '模型未记录';
}

function humanSearchName(value) {
  const text = String(value || '').trim();
  const lower = text.toLowerCase();
  if (lower.includes('openalex')) return 'OpenAlex 文献检索（免费）';
  if (lower.includes('brave')) return 'Brave 网络检索';
  if (lower.includes('duck')) return '网页检索';
  if (lower.includes('replay') || lower.includes('offline')) return '离线回放检索';
  return text || '检索方式未记录';
}

function renderProfileSelector(config) {
  const profiles = new Map(profileOptions(config).map(option => [option.id, option]));
  const defaultProfile = String(config.default_profile || 'team').toLowerCase();
  const defaultInput = document.querySelector(`input[name="profile"][value="${defaultProfile}"]`);
  if (defaultInput) defaultInput.checked = true;
  document.querySelectorAll('[data-profile-option]').forEach(label => {
    const profile = profiles.get(label.dataset.profileOption);
    const detail = label.querySelector('[data-profile-detail]');
    label.dataset.configured = String(profile?.configured === true);
    if (!detail || !profile) return;
    if (profile.id === 'team') {
      const models = Array.from(new Set(Object.values(profile.routes || {})));
      detail.textContent = `${models.map(humanModelName).join(' + ')} · ${Object.keys(profile.routes || {}).length} 条角色路由`;
    } else {
      const model = modelOption(profile.id, config);
      detail.textContent = model?.model || '模型 ID 待填写';
    }
  });
  syncProfileSelection();
}

function renderRoleRoutes(profile) {
  const routes = profile?.routes || {};
  const orderedRoles = Object.keys(roleLabels);
  $('roleRouteList').innerHTML = orderedRoles.map((role, index) => {
    const choice = routes[role];
    const model = modelOption(choice);
    return `<div><span>${String(index + 1).padStart(2, '0')} · ${escapeHTML(roleLabels[role])}</span><b class="route-model route-${escapeHTML(choice || 'unknown')}">${escapeHTML(humanModelName(choice))}</b><small>${escapeHTML(model?.model || '模型 ID 未记录')}</small></div>`;
  }).join('');
}

function profileCapabilityLabel(profile) {
  const modalities = Array.isArray(profile?.input_modalities) ? profile.input_modalities : [];
  const verified = Array.isArray(profile?.verified_input_modalities) ? profile.verified_input_modalities : [];
  const labels = {text: '文本', document: '文档', image: '图片', audio: '音频'};
  const readable = modalities.map(item => labels[item] || item).join(' / ') || '未记录';
  const verifiedLabel = verified.map(item => labels[item] || item).join(' / ');
  return verified.length
    ? `${readable} · 已实测 ${verifiedLabel}；其余能力由配置声明`
    : `${readable} · 网关能力由配置声明，运行时仍会强制校验`;
}

function syncProfileSelection() {
  const offline = $('offline').checked;
  const selector = $('modelSelector');
  selector.disabled = offline;
  selector.dataset.offline = String(offline);
  const selected = selectedProfileOption();
  $('profileRuntimeTitle').textContent = offline
    ? '离线回放使用本地规则模型'
    : selected?.label || humanModelName(selected?.id || selectedProfileId());
  $('profileCapabilityStatus').textContent = offline
    ? '可解析 UTF-8 文本与可提取文本的 PDF，不执行图片或音频感知'
    : profileCapabilityLabel(selected);
  renderRoleRoutes(offline ? {
    routes: Object.fromEntries(Object.keys(roleLabels).map(role => [role, 'mock'])),
  } : selected);

  const status = $('modelSelectorStatus');
  if (offline) {
    status.textContent = '离线回放不调用共享 API；单模型与团队选择会保留到切回在线模式。';
  } else if (!selected) {
    status.textContent = '服务器未返回所选编组。';
  } else if (!selected.configured) {
    status.textContent = `${selected.label || humanModelName(selected.id)} 的共享 API 或模型 ID 尚未配置完整。`;
  } else if (loadedConfig?.search_configured !== true) {
    status.textContent = `${humanSearchName(loadedConfig?.search_provider)} 尚未配置完成，请在 .env 填写 DR_BRAVE_API_KEY。`;
  } else {
    status.textContent = selected.id === 'team'
      ? '本次运行会按角色分别调用 GPT、DeepSeek 与 Qwen。'
      : `本次六个模型角色都使用 ${selected.label || humanModelName(selected.id)}，用于单模型对照。`;
  }
  validateCurrentAttachments();
  renderProviderBadge();
  syncRunButtonState();
}

function profileReady() {
  if ($('offline').checked) return true;
  if (!loadedConfig) return true;
  return selectedProfileOption()?.configured === true
    && loadedConfig.search_configured === true;
}

function renderProviderBadge() {
  if (!loadedConfig) return;
  const offline = $('offline').checked;
  const selected = selectedProfileOption();
  const modelReady = selected?.configured === true;
  const searchReady = loadedConfig.search_configured === true;
  const ready = offline || (modelReady && searchReady);
  const state = offline
    ? '离线回放已选择'
    : !modelReady
      ? '模型配置待补全'
      : !searchReady
        ? '检索配置待补全'
        : '协作编组已就绪';
  let modelDetail;
  if (offline) {
    modelDetail = '本地规则模型';
  } else if (selected?.id === 'team') {
    const used = Array.from(new Set(Object.values(selected.routes || {})));
    modelDetail = used.map(choice => modelOption(choice)?.model || humanModelName(choice)).join(' · ');
  } else {
    modelDetail = modelOption(selected?.id)?.model || humanModelName(selected?.id);
  }
  const detail = `${modelDetail} · ${humanSearchName(offline ? 'replay' : loadedConfig.search_provider)}`;
  $('providerBadge').innerHTML = `<span class="pulse ${ready ? 'ready' : 'needs-config'}" aria-hidden="true"></span><span class="provider-copy"><b>${escapeHTML(state)}</b><small>${escapeHTML(detail)}</small></span>`;
  $('providerBadge').setAttribute('aria-label', `${state}：${detail}`);
  $('providerBadge').title = detail;
}

async function loadConfig() {
  try {
    loadedConfig = await getJSON('/api/config');
    renderProfileSelector(loadedConfig);
    renderAttachmentList();
  } catch (_) {
    $('providerBadge').innerHTML = '<span class="pulse needs-config" aria-hidden="true"></span><span class="provider-copy"><b>连接状态未知</b><small>暂时无法读取运行配置</small></span>';
    $('providerBadge').setAttribute('aria-label', '暂时无法读取运行配置');
    syncRunButtonState();
  }
}

function attachmentLimits() {
  return {...fallbackLimits, ...(loadedConfig?.multimodal || {})};
}

function fileMediaType(file) {
  const declared = String(file.type || '').split(';', 1)[0].toLowerCase();
  if (declared && declared !== 'application/octet-stream') return declared;
  const extension = String(file.name || '').split('.').pop()?.toLowerCase();
  return extensionMediaTypes[extension] || declared;
}

function fileModality(file) {
  const mediaType = fileMediaType(file);
  if (mediaType.startsWith('image/')) return 'image';
  if (mediaType.startsWith('audio/')) return 'audio';
  if (mediaType === 'text/plain') return 'text';
  return 'document';
}

function supportedMediaTypes() {
  const configured = loadedConfig?.multimodal?.accepted_media_types;
  return new Set(Array.isArray(configured) && configured.length ? configured : Object.values(extensionMediaTypes));
}

function attachmentKey(file) {
  return `${file.name}\u0000${file.size}\u0000${file.lastModified}`;
}

function addAttachments(files) {
  const limits = attachmentLimits();
  const supported = supportedMediaTypes();
  const existing = new Set(selectedAttachments.map(item => item.key));
  const rejected = [];
  for (const file of Array.from(files || [])) {
    const mediaType = fileMediaType(file);
    const key = attachmentKey(file);
    if (existing.has(key)) continue;
    if (!supported.has(mediaType)) {
      rejected.push(`${file.name}：格式不支持`);
      continue;
    }
    if (file.size <= 0) {
      rejected.push(`${file.name}：文件为空`);
      continue;
    }
    if (file.size > limits.max_attachment_bytes) {
      rejected.push(`${file.name}：超过单文件 ${formatBytes(limits.max_attachment_bytes)}`);
      continue;
    }
    if (selectedAttachments.length >= limits.max_attachments) {
      rejected.push(`${file.name}：最多 ${limits.max_attachments} 个附件`);
      continue;
    }
    const nextTotal = selectedAttachments.reduce((sum, item) => sum + item.file.size, 0) + file.size;
    if (nextTotal > limits.max_total_bytes) {
      rejected.push(`${file.name}：附件总量超过 ${formatBytes(limits.max_total_bytes)}`);
      continue;
    }
    selectedAttachments.push({file, key, mediaType, modality: fileModality(file), objectUrl: ''});
    existing.add(key);
  }
  attachmentError = rejected.join('；');
  renderAttachmentList();
  if (attachmentError) setAttachmentStatus(attachmentError, 'error');
}

function removeAttachment(key) {
  const index = selectedAttachments.findIndex(item => item.key === key);
  if (index < 0) return;
  const [removed] = selectedAttachments.splice(index, 1);
  if (removed.objectUrl) URL.revokeObjectURL(removed.objectUrl);
  attachmentError = '';
  renderAttachmentList();
}

function validateCurrentAttachments() {
  const offline = $('offline').checked;
  const incompatible = selectedAttachments.filter(item => offline && ['image', 'audio'].includes(item.modality));
  if (incompatible.length) {
    attachmentError = `离线回放不能感知：${incompatible.map(item => item.file.name).join('、')}`;
  } else if (attachmentError.startsWith('离线回放不能感知')) {
    attachmentError = '';
  }
  if (attachmentError) setAttachmentStatus(attachmentError, 'error');
  else updateAttachmentStatusSummary();
}

function setAttachmentStatus(message, tone = 'neutral') {
  $('attachmentStatus').textContent = message;
  $('attachmentStatus').dataset.tone = tone;
  syncRunButtonState();
}

function updateAttachmentStatusSummary() {
  const count = selectedAttachments.length;
  const total = selectedAttachments.reduce((sum, item) => sum + item.file.size, 0);
  setAttachmentStatus(
    count
      ? `${count} 个附件 · ${formatBytes(total)} · 服务端将重新校验 MIME 与 SHA-256`
      : '附件会在服务端重新校验 MIME、大小与 SHA-256。',
    count ? 'success' : 'neutral',
  );
}

function renderAttachmentList() {
  const list = $('attachmentList');
  const limits = attachmentLimits();
  $('attachmentBudget').textContent = `${selectedAttachments.length} / ${limits.max_attachments}`;
  if (!selectedAttachments.length) {
    list.innerHTML = '<span class="attachment-empty">当前仅使用研究问题作为输入</span>';
    validateCurrentAttachments();
    return;
  }
  list.innerHTML = '';
  selectedAttachments.forEach((item, index) => {
    const card = document.createElement('article');
    card.className = `attachment-card modality-${item.modality}`;
    card.dataset.attachmentKey = item.key;
    const preview = document.createElement('div');
    preview.className = 'attachment-preview';
    const file = item.file;
    if (['image', 'audio'].includes(item.modality) || item.mediaType === 'application/pdf') {
      item.objectUrl ||= URL.createObjectURL(file);
    }
    if (item.modality === 'image') {
      const image = document.createElement('img');
      preview.dataset.state = 'loading';
      image.src = item.objectUrl;
      image.alt = `${file.name} 预览`;
      image.decoding = 'async';
      image.draggable = false;
      image.addEventListener('load', () => {
        if (card.isConnected) preview.dataset.state = 'ready';
      }, {once: true});
      image.addEventListener('error', () => {
        if (!card.isConnected) return;
        preview.dataset.state = 'error';
        image.remove();
        const failure = document.createElement('span');
        failure.className = 'attachment-preview-error';
        failure.textContent = '图片预览失败';
        preview.appendChild(failure);
      }, {once: true});
      preview.appendChild(image);
    } else if (item.modality === 'audio') {
      const audio = document.createElement('audio');
      audio.src = item.objectUrl;
      audio.controls = true;
      audio.preload = 'metadata';
      preview.appendChild(audio);
    } else if (item.mediaType === 'application/pdf') {
      const link = document.createElement('a');
      link.href = item.objectUrl;
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = 'PDF 预览 ↗';
      preview.appendChild(link);
    } else {
      const text = document.createElement('pre');
      text.textContent = '正在读取文本预览…';
      preview.appendChild(text);
      file.text().then(value => {
        if (card.isConnected) text.textContent = value.slice(0, 420) || '文本内容为空';
      }).catch(() => {
        if (card.isConnected) text.textContent = '文本预览不可用';
      });
    }
    const meta = document.createElement('div');
    meta.className = 'attachment-meta';
    const sequence = document.createElement('span');
    sequence.textContent = `${String(index + 1).padStart(2, '0')} · ${item.modality.toUpperCase()}`;
    const name = document.createElement('strong');
    name.textContent = file.name;
    const facts = document.createElement('small');
    facts.textContent = `${item.mediaType} · ${formatBytes(file.size)}`;
    meta.append(sequence, name, facts);
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'attachment-remove';
    remove.dataset.removeAttachment = item.key;
    remove.setAttribute('aria-label', `移除附件 ${file.name}`);
    remove.title = '移除附件';
    remove.textContent = '×';
    card.append(preview, meta, remove);
    list.appendChild(card);
  });
  validateCurrentAttachments();
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes >= 10240 ? 0 : 1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function ensureFormStatus() {
  const form = $('researchForm');
  let status = $('formStatus');
  if (status) return status;
  status = document.createElement('p');
  status.id = 'formStatus';
  status.className = 'form-status';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  status.setAttribute('aria-atomic', 'true');
  status.hidden = true;
  form.appendChild(status);
  return status;
}

function setFormStatus(message, tone = 'neutral') {
  const status = ensureFormStatus();
  status.textContent = message;
  status.dataset.tone = tone;
  status.hidden = !message;
}

function syncRunButtonState() {
  const button = $('runButton');
  button.disabled = runBusy || Boolean(attachmentError) || !profileReady();
  button.setAttribute('aria-busy', String(runBusy));
  const label = button.querySelector('span:first-child');
  if (label) label.textContent = runBusy ? '正在建立研究档案' : '开始研究';
}

async function loadHistory() {
  const history = $('runHistory');
  const refresh = $('refreshHistory');
  const status = $('historyStatus');
  history.setAttribute('aria-busy', 'true');
  refresh.disabled = true;
  history.dataset.state = 'loading';
  if (!history.querySelector('.history-item')) history.innerHTML = '<span class="history-state loading">正在读取运行记录……</span>';
  try {
    const data = await getJSON('/api/runs');
    const runs = Array.isArray(data.runs) ? data.runs : [];
    history.dataset.state = runs.length ? 'ready' : 'empty';
    history.innerHTML = runs.length ? runs.map(run => `
      <button class="history-item" type="button" data-run="${escapeHTML(run.run_id)}" aria-label="打开研究记录：${escapeHTML(run.question || '未记录问题')}；${escapeHTML(statusName(run.status))}">
        <strong>${escapeHTML(run.question || '历史运行未记录问题')}</strong>
        <span class="history-status">${escapeHTML(statusName(run.status))}</span>
        <small>${escapeHTML(historyScore(run))}${historyDate(run) ? ` · ${escapeHTML(historyDate(run))}` : ''}</small>
      </button>`).join('') : '<span class="history-state empty">尚无已保存的运行记录</span>';
    document.querySelectorAll('.history-item').forEach(button => {
      button.addEventListener('click', () => window.location.href = `/run.html?id=${encodeURIComponent(button.dataset.run)}`);
    });
    status.textContent = runs.length ? `已读取 ${runs.length} 条运行记录` : '当前没有已保存的运行记录';
  } catch (error) {
    history.dataset.state = 'error';
    history.innerHTML = '<div class="history-state error"><strong>历史记录暂时不可用</strong><span>运行服务没有返回可读取的记录。</span><button type="button" data-history-retry>重新读取</button></div>';
    history.querySelector('[data-history-retry]')?.addEventListener('click', loadHistory);
    status.textContent = `历史记录读取失败：${error.message}`;
  } finally {
    history.setAttribute('aria-busy', 'false');
    refresh.disabled = false;
  }
}

$('researchForm').addEventListener('submit', async event => {
  event.preventDefault();
  const question = $('question').value.trim();
  validateCurrentAttachments();
  if (!question || attachmentError || !profileReady()) return;
  runBusy = true;
  syncRunButtonState();
  setFormStatus('正在创建研究档案，随后进入独立运行页。', 'pending');
  const body = new FormData();
  body.append('question', question);
  body.append('offline', String($('offline').checked));
  body.append('profile', selectedProfileId());
  selectedAttachments.forEach(item => body.append('attachments', item.file, item.file.name));
  try {
    const result = await getJSON('/api/runs', {method: 'POST', body});
    setFormStatus('研究档案已创建，正在打开运行页。', 'success');
    window.location.href = `/run.html?id=${encodeURIComponent(result.run_id)}`;
  } catch (error) {
    setFormStatus(`研究档案未创建：${error.message || '运行服务没有返回原因'}`, 'error');
    runBusy = false;
    syncRunButtonState();
  }
});

function statusName(status) {
  return ({completed:'研究完成', failed:'运行已保存', cancelled:'已停止', verification_failed:'待补充引用', evidence_incomplete:'当前回答待补证', drafting:'正在撰写', perceiving:'正在感知输入', planning:'正在规划', queued:'排队等待', running:'正在研究', initialized:'已初始化'})[status] || '研究进行中';
}

function historyScore(run) {
  const status = String(run?.closure_score_status || '').trim();
  const raw = run?.closure_score;
  if (raw === null || raw === undefined || raw === '' || status !== 'observed' || typeof raw === 'boolean') return '流程检查未记录';
  const score = Number(raw);
  return Number.isFinite(score) ? `流程检查 ${Math.round(Math.max(0, Math.min(1, score)) * 100)} / 100` : '流程检查未记录';
}

function historyDate(run) {
  const value = run?.updated_at || run?.created_at || run?.finished_at;
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString('zh-CN', {month: 'numeric', day: 'numeric'});
}

function escapeHTML(value) {
  return String(value ?? '').replace(/[&<>'"]/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[character]));
}

document.querySelectorAll('input[name="profile"]').forEach(input => input.addEventListener('change', syncProfileSelection));
$('offline').addEventListener('change', syncProfileSelection);
$('refreshHistory').addEventListener('click', loadHistory);
$('attachmentInput').addEventListener('change', event => {
  addAttachments(event.target.files);
  event.target.value = '';
});
$('attachmentList').addEventListener('click', event => {
  const button = event.target.closest('[data-remove-attachment]');
  if (button) removeAttachment(button.dataset.removeAttachment);
});
['dragenter', 'dragover'].forEach(type => $('attachmentDropzone').addEventListener(type, event => {
  event.preventDefault();
  $('attachmentDropzone').classList.add('is-dragging');
}));
['dragleave', 'drop'].forEach(type => $('attachmentDropzone').addEventListener(type, event => {
  event.preventDefault();
  $('attachmentDropzone').classList.remove('is-dragging');
}));
$('attachmentDropzone').addEventListener('drop', event => addAttachments(event.dataTransfer.files));
window.addEventListener('beforeunload', () => selectedAttachments.forEach(item => item.objectUrl && URL.revokeObjectURL(item.objectUrl)));

loadConfig();
loadHistory();
syncRunButtonState();
