const $ = (id) => document.getElementById(id);
const runId = new URLSearchParams(window.location.search).get('id');
const terminalStates = ['completed', 'verification_failed', 'evidence_incomplete', 'failed', 'cancelled'];
let pollTimer = null;
let eventSource = null;
let liveWatchdog = null;
let pollInFlight = false;
let pollFailureCount = 0;
let usagePulseTimer = null;
let usagePulseInFlight = false;
let usagePulseFailureCount = 0;
let usagePulseEnabled = false;
let graphFilter = 'all';
let graphZoom = 1;
let graphFitMode = true;
let graphFocusedNode = null;
let graphFocusedLabel = '';
let ledgerFilter = 'all';
let connectionMode = '正在建立实时事件流';
let lastProtocolAuditAt = 0;
let metricDetailsExpanded = false;
let stopRequestPending = false;
let lastAnnouncedStatus = '';
let lastAnnouncedRuntimeKey = '';
let lastLiveAnnouncementKey = '';
let lastNetworkAnnouncementKey = '';
let executionExpanded = false;
let timelineExpanded = false;
let auditNavigationStack = [];
let auditReturnFocus = null;
let auditCurrentFrame = null;
let networkProgrammaticScroll = false;
const auditPageLimit = 40;
const auditCollectionLabels = Object.freeze({
  invocations: '智能体执行记录',
  handoffs: '任务交接记录',
  receipts: '接收确认记录',
  source_fetches: '文章来源读取',
  artifacts: '阶段产物',
  input_attachments: '输入附件',
  resume_receipts: '恢复确认记录',
  worker: '执行器生命周期',
});
const protocolCollectionLabels = Object.freeze({
  external_runs: '外部生命周期',
  status_transitions: '协议状态转移',
  interrupts: '中断记录',
  message_snapshots: '消息快照元数据',
  resume_receipts: '恢复确认记录',
  worker: '执行器生命周期',
});
const auditCollectionAliases = Object.freeze({
  source_fetches: ['source_fetches', 'sourceFetches'],
  input_attachments: ['input_attachments', 'inputAttachments'],
  resume_receipts: ['resume_receipts', 'resumeReceipts'],
});
let auditPageCache = {runKey: '', collections: Object.create(null)};
let protocolPageCache = {runKey: '', collections: Object.create(null)};
const auditPageErrors = {run: Object.create(null), protocol: Object.create(null)};
const auditPageStalls = {run: Object.create(null), protocol: Object.create(null)};
const pendingAuditPages = new Set();
const pendingProtocolPages = new Set();
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
try {
  metricDetailsExpanded = window.localStorage.getItem('fieldnote.metricDetails') === 'expanded';
} catch (_) {
  metricDetailsExpanded = false;
}

const agentOrder = ['planner', 'scout', 'curator', 'critic', 'writer', 'verifier'];
const nodeAgents = {perceive_inputs:'perception',plan:'planner',generate_queries:'scout',search:'scout',search_and_fetch:'scout',fetch:'scout',ingest_evidence:'curator',extract_evidence:'curator',assess_closure:'critic',citation_repair:'critic',draft:'writer',compose_limited_answer:'writer',verify:'verifier',check_limited_delivery:'verifier',confirm_local_citation_binding:'verifier',finalize:'orchestrator',emit_finalize:'orchestrator'};
const citationContractPassLabel = '每句话都能对应到已保存的引用原文，不代表系统认证事实绝对正确';
const deliveryCheckLabel = '交付前检查';
const receiptStatePresentation = Object.freeze({
  server_validated: {
    label: '系统已确认接收',
    detail: '发送与接收记录已由系统核对；仍需查看这一条交接的阶段检查和产物。',
    raw: 'server_validated',
  },
  field_match: {
    label: '信息能对应，待系统确认',
    detail: '发送与接收字段看起来一致，但系统尚未完成确认。',
    raw: 'field_match',
  },
  unverified: {
    label: '尚未确认接收',
    detail: '目前只能看到计划交接，或记录还不完整。',
    raw: 'unverified',
  },
  invalid: {
    label: '记录不一致，暂不采用',
    detail: '这条记录存在冲突或被拒绝，不能据此认定已经交接。',
    raw: 'invalid',
  },
});
const agentMessages = {
  perception:['感知智能体正在读取附件','逐个核对不可变附件，并生成带页码、区域或时间范围的定位观察。'],
  planner:['规划智能体正在拆解问题','识别必须回答的关键目标，并定义每个子任务需要什么证据才能完成。'],
  scout:['检索智能体正在探索公开信息','沿多条检索路线寻找原始材料、独立来源和可能的反证。'],
  curator:['证据智能体正在阅读文章','从正文中抽取可逐字核对的片段，并把证据关联到具体回答目标。'],
  critic:['审查智能体正在比较证据','检查来源是否独立、候选值是否冲突，以及当前材料是否足够完整。'],
  writer:['写作智能体正在组织回答','只使用证据账本中的材料生成答案，并在每条事实后标注来源。'],
  verifier:['核验智能体正在逐句验收','重新检查声明与引用原文是否一致，不通过的部分会返回检索环节修复。'],
  orchestrator:['研究总控正在归档交付','汇总最终回答、交付前检查、引用核验和已保存产物清单，并写入可恢复状态。']
};
const agentContracts = {
  perception:{name:'多模态感知智能体',input:'已保存附件、媒体类型与用户问题',output:'带页码、区域或时间范围的定位观察',gate:'附件指纹、定位位置、感知模型和置信度必须完整；附件本身不替代独立来源'},
  planner:{name:'规划智能体',input:'用户问题、回答格式要求、预算上限',output:'回答目标槽位、子目标、完成条件',gate:'每个必需槽位必须被至少一个子目标覆盖'},
  scout:{name:'检索智能体',input:'子目标、当前证据缺口、历史查询',output:'1–3 条非重复检索路线和候选文章',gate:'查询必须对应具体缺口，并通过近重复过滤'},
  curator:{name:'证据整理智能体',input:'成功读取的文章正文与回答目标',output:'可逐字核对的原文、整理后的说法、来源与立场',gate:'引用原文必须能在正文中逐字找到'},
  critic:{name:'完整性审查智能体',input:'回答目标、证据账本、来源组和冲突说法',output:'五项检查结果、材料缺口和下一步决定',gate:'每个必答问题都有材料，并且没有未说明的冲突'},
  writer:{name:'写作智能体',input:'已通过检查的证据账本',output:'与证据编号对应的待核对回答',gate:'不得使用账本之外的材料，也不得补写没有证据的事实'},
  verifier:{name:'引用核验智能体',input:'待核对回答、引用编号与对应原文',output:'逐句“支持充分 / 支持不足 / 不支持”判断',gate:citationContractPassLabel},
  orchestrator:{name:'研究总控',input:'最终回答、交付前检查、引用核验与完整运行记录',output:'可恢复的完成状态与最终已保存产物清单',gate:'六个角色的完成记录、全部交付前检查和逐句引用核验必须与已保存产物一致'}
};
const resumeBudgetExtension = Object.freeze({additional_iterations:1, additional_search_calls:3, additional_pages:5});
const gateDefinitions = [
  {key:'coverage', label:'材料覆盖', field:'supporting_evidence_ids', explanation:'每个必答问题是否至少有一条被本次检查选入的支持材料'},
  {key:'sources', label:'来源互证', field:'source_gate_passed', explanation:'是否达到独立来源要求，或记录了可信的单一权威来源理由'},
  {key:'quotes', label:'原文可定位', field:'exact_quote_gate_passed', explanation:'每条计入证据的原文都能在保存的页面中逐字找到'},
  {key:'counter', label:'反面材料检查', field:'contradiction_checked', explanation:'是否至少阅读过一页与该问题相关的反面材料'},
  {key:'conflicts', label:'说法冲突处理', field:'conflict_gate_passed', explanation:'不同数值、人物归属或明确反面材料之间的冲突是否已说明'}
];

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function asObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function modelRouteFor(role, methodology = window.__latestState?.methodology || {}) {
  if (role === 'critic' || role === 'orchestrator') {
    const model = role === 'critic' ? 'deterministic-evidence-gate' : 'deterministic-finalize-gate';
    return {role, choice:'local', provider:'local', model, modalities:['structured_state'], deterministic:true};
  }
  const route = asObject(asObject(methodology).model_routes)[role];
  if (typeof route === 'string') {
    return {role, choice:route, provider:'', model:'', modalities:[], deterministic:false};
  }
  const normalized = asObject(route);
  if (Object.keys(normalized).length) {
    return {
      role,
      choice:String(normalized.choice || normalized.model_choice || ''),
      provider:String(normalized.provider || normalized.model_provider || ''),
      model:String(normalized.model || normalized.model_id || ''),
      modalities:asArray(normalized.modalities || normalized.input_modalities).map(String),
      deterministic:false,
    };
  }
  const choice = String(asObject(methodology).model_choice || '');
  return {
    role,
    choice,
    provider:String(asObject(methodology).model_provider || ''),
    model:String(asObject(methodology).model || ''),
    modalities:[],
    deterministic:false,
  };
}

function invocationModelRoute(item) {
  const value = asObject(item);
  const providerCalls = providerCallCount(value);
  const deterministic = providerCalls === 0 && !value.model_id && !value.model_choice;
  return {
    role:String(value.agent_id || ''),
    choice:String(value.model_choice || (deterministic ? 'local' : '')),
    provider:String(value.model_provider || (deterministic ? 'local' : '')),
    model:String(value.model_id || (deterministic ? 'deterministic-operation' : '')),
    modalities:asArray(value.input_modalities).map(String),
    deterministic,
  };
}

function modelRouteLabel(route, {includeModel = true} = {}) {
  const value = asObject(route);
  if (value.deterministic || value.choice === 'local') return '本地固定规则检查';
  const choice = providerName(value.choice || value.provider || '模型未记录');
  const model = includeModel ? String(value.model || '') : '';
  return model && model !== value.choice ? `${choice} · ${model}` : choice;
}

function modalityLabel(value) {
  return ({text:'文本',document:'文档',image:'图像',audio:'音频',structured_state:'结构化状态'})[String(value || '')] || String(value || '未记录');
}

function routeModelSummary(methodology = {}) {
  const roles = ['perception', 'planner', 'scout', 'curator', 'writer', 'verifier'];
  const routes = roles.map(role => modelRouteFor(role, methodology));
  const choices = [...new Set(routes.map(route => route.choice).filter(Boolean))];
  const models = [...new Set(routes.map(route => route.model).filter(Boolean))];
  return {routes, choices, models, isTeam:String(asObject(methodology).model_profile || '') === 'team' || choices.length > 1};
}

function pageItems(value) {
  if (Array.isArray(value)) return value;
  const object = asObject(value);
  for (const key of ['items', 'rows', 'records', 'data']) {
    if (Array.isArray(object[key])) return object[key];
  }
  return [];
}

function pageField(candidates, key) {
  for (const candidate of candidates) {
    if (candidate && Object.prototype.hasOwnProperty.call(candidate, key)) {
      return candidate[key];
    }
  }
  return undefined;
}

function pageCursorToken(value) {
  if (value === undefined || value === null || value === '') return '';
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch (_) {
      return String(value);
    }
  }
  return String(value);
}

function pageDescriptor(container, key, aliases = []) {
  const source = asObject(container);
  const names = aliases.length ? aliases : [key];
  let rawValue;
  let present = false;
  for (const name of names) {
    if (Object.prototype.hasOwnProperty.call(source, name)) {
      rawValue = source[name];
      present = true;
      break;
    }
  }
  const keyedPages = [
    source.pagination,
    source.audit_pagination,
    source.audit_pages,
    source.pages,
    source.windows,
  ].map(value => asObject(value));
  const keyedMeta = [];
  for (const name of names) {
    keyedPages.forEach(page => {
      if (page[name] !== undefined) keyedMeta.push(asObject(page[name]));
    });
  }
  const valueObject = asObject(rawValue);
  const candidates = [valueObject, ...keyedMeta, source.audit_window, source.window, source];
  const items = pageItems(rawValue);
  const cursor = pageField(candidates, 'cursor');
  const nextCursor = pageField(candidates, 'next_cursor') ?? pageField(candidates, 'nextCursor');
  const limit = finiteValue(pageField(candidates, 'limit'));
  const total = finiteValue(
    pageField(candidates, 'total_count') ?? pageField(candidates, 'totalCount'),
  );
  const returned = finiteValue(
    pageField(candidates, 'returned_count') ?? pageField(candidates, 'returnedCount'),
  );
  const declaredHasMore = pageField(candidates, 'has_more') ?? pageField(candidates, 'hasMore');
  const hasMore = typeof declaredHasMore === 'boolean'
    ? declaredHasMore
    : nextCursor !== undefined && nextCursor !== null && nextCursor !== ''
      ? true
      : null;
  const windowed = Boolean(
    cursor !== undefined
      || nextCursor !== undefined
      || limit !== null
      || total !== null
      || returned !== null
      || hasMore !== null,
  );
  return {
    key,
    present,
    raw: rawValue,
    items,
    cursor: cursor ?? null,
    nextCursor: nextCursor ?? null,
    limit,
    total,
    returned,
    hasMore,
    windowed,
  };
}

function auditItemKey(collection, item, index = 0) {
  const value = asObject(item);
  if (collection === 'source_fetches' && !value.fetch_record_id) {
    const attempt = value.attempt ?? 'unknown';
    const operation = value.operation_key || value.fetch_operation_key || 'unknown';
    const recorded = value.recorded_at || value.fetched_at || index;
    return `${collection}:${value.source_id || value.requested_url || 'unknown'}:${attempt}:${operation}:${recorded}`;
  }
  // A valid receipt and a rejected receipt can share one message_id. They are
  // separate audit facts, not two pages of the same row. Keep their identity
  // distinct so a later page cannot silently turn an invalid edge green.
  if (collection === 'receipts') {
    const message = value.message_id || value.receipt_id || value.id || 'unknown';
    const identity = value.receipt_id
      || value.id
      || `${value.consumed_by_invocation_id || 'no-consumer'}:${value.validation_status || value.status || 'unknown'}:${value.valid === false ? 'invalid' : 'valid-or-unknown'}:${value.created_at || value.validated_at || value.consumed_at || value.reason || value.validation_error || ''}`;
    return `${collection}:${message}:${identity}`;
  }
  const keys = {
    invocations: ['invocation_id', 'sequence', 'id'],
    handoffs: ['message_id', 'id'],
    receipts: ['message_id', 'receipt_id', 'id'],
    source_fetches: ['fetch_record_id', 'source_id', 'id'],
    artifacts: ['artifact_id', 'id'],
    resume_receipts: ['idempotency_key', 'resume_receipt_id', 'id'],
    worker: ['event_id', 'audit_id', 'record_id', 'created_at'],
    external_runs: ['run_id', 'id'],
    status_transitions: ['transition_id', 'run_id', 'id', 'updated_at'],
    interrupts: ['interrupt_id', 'id'],
    message_snapshots: ['thread_id', 'snapshot_id', 'id', 'updated_at'],
  }[collection] || ['id'];
  for (const key of keys) {
    if (value[key] !== undefined && value[key] !== null && value[key] !== '') {
      return `${collection}:${String(value[key])}`;
    }
  }
  try {
    return `${collection}:anonymous:${JSON.stringify(value)}:${index}`;
  } catch (_) {
    return `${collection}:anonymous:${index}`;
  }
}

function mergePageItems(collection, previous, incoming) {
  const merged = [];
  const positions = new Map();
  [...asArray(previous), ...asArray(incoming)].forEach((item, index) => {
    const key = auditItemKey(collection, item, index);
    if (!positions.has(key)) {
      positions.set(key, merged.length);
      merged.push(item);
    } else {
      const position = positions.get(key);
      const current = asObject(merged[position]);
      const next = asObject(item);
      const criticalFields = new Set(collection === 'receipts'
        ? ['message_id', 'consumed_by_invocation_id', 'consumed_by_agent_id', 'validation_status', 'status', 'valid', 'reason', 'validation_error']
        : collection === 'handoffs'
          ? ['message_id', 'run_id', 'trace_id', 'producer', 'producer_invocation_id', 'intended_consumer', 'route_target', 'envelope']
        : collection === 'artifacts'
          ? ['artifact_id', 'run_id', 'checksum', 'metadata_hash', 'producer_invocation_id', 'handoff_message_id', 'manifest_valid', 'files_present', 'passable', 'status']
          : []);
      const conflicts = asObject(current.__merge_conflict_values);
      const fields = new Set([...Object.keys(current), ...Object.keys(next)]);
      fields.forEach(field => {
        if (!criticalFields.has(field)) return;
        const left = current[field];
        const right = next[field];
        if (left === undefined || right === undefined || left === right) return;
        if (JSON.stringify(left) === JSON.stringify(right)) return;
        conflicts[field] = [...new Set([
          ...asArray(conflicts[field]),
          left,
          right,
        ].map(value => {
          try { return JSON.stringify(value); } catch (_) { return String(value); }
        }))].map(value => {
          try { return JSON.parse(value); } catch (_) { return value; }
        });
      });
      const mergedRow = {...current, ...next};
      if (Object.keys(conflicts).length) {
        mergedRow.__merge_conflict_values = conflicts;
        mergedRow.__merge_conflicts = Object.keys(conflicts);
        // Keep the first durable value as the display value. The proof model
        // sees the conflict marker and fails closed instead of shallow-merging
        // two contradictory pages into one apparently complete row.
        Object.keys(conflicts).forEach(field => { mergedRow[field] = current[field]; });
      }
      merged[position] = mergedRow;
    }
  });
  return merged;
}

function pageCacheFor(cache, runKey, collection) {
  if (cache.runKey !== runKey) {
    cache.runKey = runKey;
    cache.collections = Object.create(null);
  }
  if (!cache.collections[collection]) {
    cache.collections[collection] = {
      items: [],
      cursor: null,
      nextCursor: null,
      limit: null,
      total: null,
      returned: null,
      hasMore: null,
      windowed: false,
      continuationLoaded: false,
    };
  }
  return cache.collections[collection];
}

function collectCachedPage(cache, runKey, descriptor, collection) {
  const stored = pageCacheFor(cache, runKey, collection);
  const requestHint = asObject(window.__auditPageHint);
  const scope = cache === protocolPageCache ? 'protocol' : 'run';
  const scopeHintMatches = requestHint.scope === scope
    && requestHint.cursor !== undefined
    && pageCursorToken(requestHint.cursor) !== ''
    && pageCursorToken(stored.nextCursor) === pageCursorToken(requestHint.cursor);
  const requestedCursor = requestHint.scope === scope
    && requestHint.collection === collection
    ? requestHint.cursor
    : scopeHintMatches
      ? requestHint.cursor
      : null;
  const cursor = descriptor.cursor ?? requestedCursor;
  const isContinuation = pageCursorToken(cursor) !== '' && pageCursorToken(cursor) !== '0';
  if (!descriptor.windowed && !requestedCursor && !stored.continuationLoaded) {
    stored.items = [];
    stored.cursor = null;
    stored.nextCursor = null;
    stored.limit = null;
    stored.total = null;
    stored.returned = null;
    stored.hasMore = null;
    stored.windowed = false;
  }
  if (isContinuation) stored.continuationLoaded = true;
  stored.items = mergePageItems(collection, stored.items, descriptor.items);
  const keepContinuationMeta = stored.continuationLoaded && !isContinuation && stored.nextCursor !== null;
  if (!keepContinuationMeta || isContinuation || stored.nextCursor === null) {
    stored.cursor = isContinuation ? (descriptor.cursor ?? cursor ?? stored.cursor) : descriptor.cursor ?? stored.cursor;
    stored.nextCursor = isContinuation ? descriptor.nextCursor : descriptor.nextCursor ?? stored.nextCursor;
    stored.limit = descriptor.limit ?? stored.limit;
    stored.total = descriptor.total ?? stored.total;
    stored.returned = descriptor.returned ?? stored.returned;
    stored.hasMore = descriptor.hasMore ?? stored.hasMore;
  }
  stored.windowed = stored.windowed || descriptor.windowed;
  return {
    ...descriptor,
    items: stored.items,
    cursor: stored.cursor,
    nextCursor: stored.nextCursor,
    limit: stored.limit,
    total: stored.total,
    returned: stored.returned,
    hasMore: stored.hasMore,
    windowed: stored.windowed,
  };
}

function auditWindowModel(pages) {
  const records = Object.values(pages || {});
  const windowed = records.some(item => item?.windowed);
  const partial = records.some(item => item?.hasMore === true);
  const unknown = records.some(item => item?.present && !item?.windowed);
  const loaded = records.reduce((sum, item) => sum + asArray(item?.items).length, 0);
  return {windowed, partial, unknown, loaded, pages};
}

function finiteValue(value) {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value !== 'number' && typeof value !== 'string') return null;
  if (typeof value === 'string' && value.trim() === '') return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function usageSnapshotPresent(value) {
  const usage = asObject(value);
  return Object.keys(usage).length > 0
    && ['usage_status', 'ledger_entry_count', 'usage_revision', 'model_calls']
      .some(key => Object.prototype.hasOwnProperty.call(usage, key));
}

function usageSnapshotIsNewer(candidate, current) {
  if (!usageSnapshotPresent(candidate)) return false;
  if (!usageSnapshotPresent(current)) return true;
  const next = asObject(candidate);
  const previous = asObject(current);
  const nextRevision = finiteValue(next.usage_revision);
  const previousRevision = finiteValue(previous.usage_revision);
  if (nextRevision !== null && previousRevision !== null && nextRevision !== previousRevision) {
    return nextRevision > previousRevision;
  }
  if (nextRevision !== null && previousRevision === null) return true;
  if (nextRevision === null && previousRevision !== null) return false;
  // A ledger revision only changes when an accounting row is persisted.  The
  // light endpoint also reports an observation timestamp, so a request moving
  // from "waiting for usage" to "settled" is visible without inventing a
  // second accounting revision in the browser.
  const nextObserved = Date.parse(String(next.snapshot_at || ''));
  const previousObserved = Date.parse(String(previous.snapshot_at || ''));
  if (Number.isFinite(nextObserved) && Number.isFinite(previousObserved) && nextObserved !== previousObserved) {
    return nextObserved > previousObserved;
  }
  if (Number.isFinite(nextObserved) && !Number.isFinite(previousObserved)) return true;
  const nextUpdated = Date.parse(String(next.updated_at || next.last_updated_at || ''));
  const previousUpdated = Date.parse(String(previous.updated_at || previous.last_updated_at || ''));
  if (Number.isFinite(nextUpdated) && Number.isFinite(previousUpdated) && nextUpdated !== previousUpdated) {
    return nextUpdated > previousUpdated;
  }
  if (Number.isFinite(nextUpdated) && !Number.isFinite(previousUpdated)) return true;
  const nextEntries = finiteValue(next.ledger_entry_count);
  const previousEntries = finiteValue(previous.ledger_entry_count);
  return nextEntries !== null && previousEntries !== null && nextEntries > previousEntries;
}

function freshestUsageSnapshot(candidates = []) {
  return candidates.reduce((latest, candidate) => (
    usageSnapshotIsNewer(candidate, latest) ? asObject(candidate) : latest
  ), {});
}

function hasRecordedField(record, key) {
  return Boolean(record && Object.prototype.hasOwnProperty.call(record, key));
}

function recordedBoolean(record, key) {
  return hasRecordedField(record, key) && typeof record[key] === 'boolean' ? record[key] : null;
}

function recordedArray(record, key) {
  return hasRecordedField(record, key) && Array.isArray(record[key]) ? record[key] : null;
}

function recordedObject(record, key) {
  return hasRecordedField(record, key) && record[key] && typeof record[key] === 'object' && !Array.isArray(record[key])
    ? record[key]
    : null;
}

function missingValueLabel(value, fallback = '未记录') {
  return finiteValue(value) === null ? fallback : String(finiteValue(value));
}

function requiredSlotProgressModel(state) {
  const closure = asObject(state?.closure);
  const rows = slotAuditRows(state, false);
  const declaredRequired = finiteValue(closure.required_slots);
  const declaredPassed = finiteValue(closure.passed_slots);
  const planRecorded = Array.isArray(state?.plan?.slots);
  const requiredIdsRecorded = Array.isArray(closure.required_slot_ids) || Array.isArray(state?.required_slot_ids);
  const auditsRecorded = Array.isArray(closure.slot_audits);
  let required = rows.length ? rows.length : declaredRequired;
  if (required === null && planRecorded) {
    required = asArray(state.plan.slots).filter(slot => slot?.required !== false).length;
  }
  if (required === null && requiredIdsRecorded) {
    required = asArray(closure.required_slot_ids || state.required_slot_ids).length;
  }
  const unknownRows = rows.filter(row => row.passed === null).length;
  const knownPassed = rows.filter(row => row.passed === true).length;
  const knownEvidenceIds = evidenceIds(state?.evidence);
  const auditComplete = Boolean(
    rows.length
      && (required === null || rows.length === required)
      && rows.every(row => {
        if (!row.present || !slotAuditRecordComplete(row.audit)) return false;
        const references = [
          ...asArray(row.audit.supporting_evidence_ids),
          ...asArray(row.audit.contradicting_evidence_ids),
        ];
        return references.every(id => knownEvidenceIds.has(String(id || '')));
      }),
  );
  // A closure summary is useful context, but it cannot certify a target when
  // the per-slot audit needed to reproduce that summary is absent.
  const passed = auditComplete ? knownPassed : null;
  return {
    rows,
    required,
    passed,
    declaredPassed,
    knownPassed,
    unknownRows,
    auditComplete,
    passedSource: auditComplete
      ? 'slot_audit'
      : declaredPassed !== null
        ? 'backend_declaration_only'
        : 'unavailable',
    requiredRecorded: required !== null,
    passedRecorded: passed !== null,
    scopeRecorded:Boolean(rows.length || declaredRequired !== null || planRecorded || requiredIdsRecorded || auditsRecorded),
  };
}

function slotAuditRecordComplete(record) {
  if (!record || typeof record !== 'object') return false;
  const requiredBooleans = [
    'passed',
    'source_gate_passed',
    'exact_quote_gate_passed',
    'contradiction_checked',
    'conflict_gate_passed',
  ];
  if (requiredBooleans.some(key => recordedBoolean(record, key) === null)) return false;
  return recordedArray(record, 'supporting_evidence_ids') !== null
    && recordedArray(record, 'contradicting_evidence_ids') !== null;
}

function closureEvidenceAuditModel(state, requiredOnly = true) {
  const progress = requiredSlotProgressModel(state);
  const rows = requiredOnly ? progress.rows : slotAuditRows(state, true);
  const admitted = new Set();
  const supportingAdmitted = new Set();
  const contradictingAdmitted = new Set();
  const invalidEvidenceIds = new Set();
  const knownEvidenceIds = evidenceIds(state?.evidence);
  const evidenceById = new Map(asArray(state?.evidence).map(item => [String(item?.id || ''), item]));
  const addEvidenceIds = (values, destination = admitted, slotId = '') => asArray(values).forEach(id => {
    const normalized = String(id || '');
    if (!normalized) return;
    const evidence = evidenceById.get(normalized);
    if (knownEvidenceIds.has(normalized) && (!slotId || String(evidence?.slot_id || '') === String(slotId))) {
      admitted.add(normalized);
      destination.add(normalized);
    }
    else invalidEvidenceIds.add(normalized);
  });
  let complete = progress.requiredRecorded && (requiredOnly ? progress.required > 0 : true);
  if (requiredOnly && progress.required > 0) {
    complete = progress.auditComplete && rows.length === progress.required;
    rows.forEach(row => {
      const supporting = recordedArray(row.audit, 'supporting_evidence_ids');
      const contradicting = recordedArray(row.audit, 'contradicting_evidence_ids');
      if (!row.present || supporting === null || contradicting === null) complete = false;
      const hardGatesPassed = row.passed === true
        && ['source_gate_passed', 'exact_quote_gate_passed', 'contradiction_checked', 'conflict_gate_passed']
          .every(field => recordedBoolean(row.audit, field) === true);
      if (!hardGatesPassed) complete = false;
      addEvidenceIds(supporting, supportingAdmitted, row.slotId);
      addEvidenceIds(contradicting, contradictingAdmitted, row.slotId);
    });
  } else {
    rows.forEach(row => {
      addEvidenceIds(recordedArray(row.audit, 'supporting_evidence_ids'), supportingAdmitted, row.slotId);
      addEvidenceIds(recordedArray(row.audit, 'contradicting_evidence_ids'), contradictingAdmitted, row.slotId);
    });
  }
  if (invalidEvidenceIds.size) complete = false;
  const evidence = asArray(state?.evidence).filter(item => admitted.has(String(item?.id || '')));
  const supportingEvidence = asArray(state?.evidence).filter(item => supportingAdmitted.has(String(item?.id || '')));
  const contradictingEvidence = asArray(state?.evidence).filter(item => contradictingAdmitted.has(String(item?.id || '')));
  return {
    available:complete,
    admitted,
    supportingAdmitted,
    contradictingAdmitted,
    evidence,
    supportingEvidence,
    contradictingEvidence,
    invalidEvidenceIds,
    progress,
  };
}

function sameStringSet(left, right) {
  if (!Array.isArray(left) || !Array.isArray(right)) return false;
  const normalize = values => values.map(value => String(value || '').trim()).filter(Boolean);
  const leftValues = normalize(left);
  const rightValues = normalize(right);
  if (new Set(leftValues).size !== leftValues.length || new Set(rightValues).size !== rightValues.length) return false;
  if (leftValues.length !== rightValues.length) return false;
  const rightSet = new Set(rightValues);
  return leftValues.every(value => rightSet.has(value));
}

function verificationModel(report) {
  const present = Boolean(report && typeof report === 'object');
  const items = recordedArray(report, 'items');
  const rows = items || [];
  const expectedItemCount = finiteValue(report?.expected_item_count);
  const providerItemCount = finiteValue(report?.provider_item_count);
  const contractVersion = String(report?.contract_version || '').trim();
  const allowedStatuses = new Set(['entailed', 'unsupported']);
  const unknownStatuses = rows.filter(item => !String(item?.status || '').trim()).length;
  const invalidStatuses = rows.filter(item => !allowedStatuses.has(String(item?.status || '').trim())).length;
  const duplicateClaimIds = rows.length - new Set(rows.map(item => String(item?.claim_id || '').trim()).filter(Boolean)).size;
  const incompleteContracts = rows.filter(item => {
    const expectedIds = recordedArray(item, 'expected_evidence_ids');
    const verifierIds = recordedArray(item, 'verifier_evidence_ids');
    return !String(item?.claim_id || '').trim()
      || !String(item?.claim || '').trim()
      || expectedIds === null
      || verifierIds === null
      || recordedBoolean(item, 'citation_set_match') === null
      || !sameStringSet(expectedIds, verifierIds);
  }).length;
  const expectedCountMatches = expectedItemCount !== null
    && Number.isInteger(expectedItemCount)
    && expectedItemCount > 0
    && rows.length === expectedItemCount;
  const providerCountMatches = providerItemCount !== null
    && Number.isInteger(providerItemCount)
    && providerItemCount >= 0
    && expectedCountMatches
    && providerItemCount === expectedItemCount
    && providerItemCount === rows.length;
  const denominator = expectedCountMatches ? expectedItemCount : null;
  const contractComplete = Boolean(
    present
      && items !== null
      && expectedCountMatches
      && providerCountMatches
      && contractVersion === 'engine-verification-contract-v6'
      && recordedBoolean(report, 'passed') !== null
      && invalidStatuses === 0
      && duplicateClaimIds === 0
      && incompleteContracts === 0,
  );
  return {
    present,
    items,
    rows,
    itemsRecorded:items !== null,
    entailed:rows.filter(item => item?.status === 'entailed' && item?.citation_set_match === true).length,
    unknownStatuses,
    invalidStatuses,
    incompleteContracts,
    duplicateClaimIds,
    expectedItemCount,
    providerItemCount,
    contractVersion,
    expectedCountMatches,
    providerCountMatches,
    contractComplete,
    denominator,
    passed:recordedBoolean(report, 'passed'),
    ratio:contractComplete && denominator > 0
      ? rows.filter(item => item?.status === 'entailed' && item?.citation_set_match === true).length / denominator
      : null,
  };
}

function eventWindowModel(data) {
  const raw = data?.event_window;
  const recorded = Boolean(raw && typeof raw === 'object' && !Array.isArray(raw));
  const window = recorded ? raw : {};
  const returned = finiteValue(window.returned_count);
  const total = finiteValue(window.total_count);
  const first = finiteValue(window.first_global_index);
  const last = finiteValue(window.last_global_index);
  const limit = finiteValue(window.limit);
  const complete = recordedBoolean(window, 'complete');
  const incomplete = recorded && (complete === false || (returned !== null && total !== null && returned < total));
  const countStatus = String(window.count_status || (total === null ? 'unverified' : 'durable'));
  const countReason = String(window.count_reason || '');
  return {recorded, returned, total, first, last, limit, complete, incomplete, countStatus, countReason};
}

function eventWindowRange(windowModel = globalThis.window.__latestEventWindow) {
  if (!windowModel) return '窗口范围未记录';
  if (windowModel.first !== null && windowModel.last !== null) return `全局事件 ${windowModel.first}–${windowModel.last}`;
  if (windowModel.returned !== null) return `返回 ${windowModel.returned} 条事件，全局范围不可验证`;
  return '窗口范围未记录';
}

function eventWindowInlineMarkup(context = '事件审计') {
  const windowModel = globalThis.window.__latestEventWindow;
  if (!windowModel?.incomplete) return '';
  const returned = windowModel.returned === null ? '未记录' : String(windowModel.returned);
  const total = windowModel.total === null ? '未记录' : String(windowModel.total);
  const countText=windowModel.total===null?`当前返回 ${returned} 条，已保存总数未记录`:`当前返回 ${returned} / ${total} 条`;
  return `<aside class="event-window-inline" role="note"><strong>事件历史不完整：仅显示最近窗口</strong><span>${escapeHTML(context)}${countText}，${escapeHTML(eventWindowRange(windowModel))}；更早事件不在当前页面数据中。</span></aside>`;
}

function renderEventWindowNotice(window) {
  const notice = $('eventWindowNotice');
  if (!notice) return;
  if (!window?.incomplete) {
    notice.classList.add('hidden');
    notice.setAttribute('aria-hidden', 'true');
    notice.innerHTML = '';
    return;
  }
  const returned = window.returned === null ? '未记录' : String(window.returned);
  const total = window.total === null ? '未记录' : String(window.total);
  const key = `${returned}|${total}|${window.first}|${window.last}`;
  notice.classList.remove('hidden');
  notice.setAttribute('aria-hidden', 'false');
  if (notice.dataset.windowKey === key) return;
  notice.dataset.windowKey = key;
  const countText=window.total===null?`当前页面返回 ${returned} 条事件，但已保存总数未记录`:`当前页面返回 ${returned} / ${total} 条事件`;
  notice.innerHTML = `<span>EVENT WINDOW</span><div><strong>事件历史不完整：仅显示最近记录</strong><p>${countText}（${escapeHTML(eventWindowRange(window))}）。更早事件没有随本次响应返回，不能只凭这段记录判断完整执行顺序。</p></div><b>请在“真实研究主时间线”中继续查看已保存的执行、交接、接收确认和产物记录</b>`;
}

function eventGlobalIndex(event, events = window.__latestEvents || []) {
  const localIndex = asArray(events).indexOf(event);
  const first = window.__latestEventWindow?.first;
  return (first !== null && first !== undefined && localIndex >= 0) ? first + localIndex : null;
}

function providerCallAggregate(items = []) {
  const values = asArray(items).map(providerCallCount);
  const known = values.filter(value => value !== null);
  return {
    total:known.reduce((sum, value) => sum + value, 0),
    known:known.length,
    unknown:values.length - known.length,
    recorded:known.length > 0,
  };
}

function providerCallLabel(items = []) {
  const aggregate = providerCallAggregate(items);
  if (!items.length || !aggregate.recorded) return '能力接口调用数未记录';
  return aggregate.unknown ? `${aggregate.total} 次已记录 · ${aggregate.unknown} 条调用数未记录` : `${aggregate.total} 次能力接口调用`;
}

function replayFieldLabel(item, key, suffix = '') {
  const value = finiteValue(item?.[key]);
  return value === null ? '未记录' : `${value}${suffix}`;
}

function replayTokenLabel(item) {
  const input = finiteValue(item?.input_tokens);
  const output = finiteValue(item?.output_tokens);
  return input === null || output === null ? 'Token 不可计算（输入或输出未记录）' : `${input + output} Token`;
}

function normalizedId(value) {
  const text = String(value ?? '').trim();
  return text && !['unknown', 'unverified', 'unrecorded', 'none', 'null'].includes(text.toLowerCase()) ? text : '';
}

function stableHash(value) {
  let hash = 2166136261;
  for (const character of String(value || '')) {
    hash ^= character.charCodeAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

function graphStableMaterial(kind, item, fallbackIndex = 0) {
  const value = asObject(item);
  if (kind === 'query') {
    return value.query_id || value.id || `${value.subgoal_id || ''}|${normalizeQueryText(value.text || value.query)}|${value.strategy || ''}` || `index:${fallbackIndex}`;
  }
  if (kind === 'source') {
    return value.id || value.source_id || normalizedSourceUrl(value.final_url || value.url) || `index:${fallbackIndex}`;
  }
  if (kind === 'evidence') {
    return value.id || `${value.source_id || ''}|${value.slot_id || ''}|${value.quote || value.claim || ''}` || `index:${fallbackIndex}`;
  }
  if (kind === 'target') {
    return value.id || `${value.description || ''}|${value.subgoal_id || ''}` || `index:${fallbackIndex}`;
  }
  if (kind === 'fetch') {
    return value.graph_key || value.fetch_record_id || [
      value.source_id || '',
      value.attempt ?? '',
      value.invocation_id || '',
      value.operation_key || '',
      value.recorded_at || value.fetched_at || '',
    ].join('|') || `index:${fallbackIndex}`;
  }
  return value.id || `index:${fallbackIndex}`;
}

function graphStableNodeId(kind, item, fallbackIndex = 0) {
  return `${kind}-${stableHash(`${kind}|${graphStableMaterial(kind, item, fallbackIndex)}`)}`;
}

function sourceStableKey(source, fallbackIndex = 0) {
  return graphStableMaterial('source', source, fallbackIndex);
}

function collapseKey(prefix, value, fallback = '') {
  const material = String(value || fallback || 'unknown').trim();
  return `${prefix}:${stableHash(material)}`;
}

function eventEnvelope(event) {
  const payload = asObject(event?.payload);
  return payload.handoff_envelope && typeof payload.handoff_envelope === 'object'
    ? payload.handoff_envelope
    : null;
}

function eventInvocationId(event) {
  const payload = asObject(event?.payload);
  return String(event?.invocation_id || payload.invocation_id || payload.agent_invocation_id || '');
}

function eventAgentId(event) {
  const payload = asObject(event?.payload);
  return String(event?.agent_id || payload.agent_id || nodeAgents[event?.node] || '');
}

function eventArtifactIds(event) {
  const payload = asObject(event?.payload);
  const envelope = eventEnvelope(event);
  return [
    ...asArray(event?.output_artifact_ids),
    ...asArray(payload.output_artifact_ids),
    ...asArray(payload.artifact_ids),
    ...asArray(envelope?.output_artifacts).map(item => item?.artifact_id),
  ].map(value => String(value || '')).filter(Boolean);
}

function invocationIdentityValidation(item) {
  const validation = asObject(item?.identity_validation);
  const status = String(
    validation.status
      || item?.provenance_status
      || item?.identity_validation_status
      || '',
  ).toLowerCase();
  const trustedStatuses = new Set(['store_consistent', 'server_validated', 'validated']);
  if (!status) return {status:'not_recorded', reliable:false, reason:'调用记录没有可验证的身份校验字段'};
  const reliable = trustedStatuses.has(status);
  return {
    status,
    reliable,
    reason:String(validation.reason || item?.provenance_reason || (reliable ? '规范化身份校验通过' : '身份校验未通过')),
  };
}

function normalizeAuditInvocation(row, fallback = null) {
  const durable = asObject(row);
  const projected = asObject(durable.invocation);
  const base = fallback && typeof fallback === 'object' ? fallback : {};
  // Normalized database columns win over the JSON projection for identity fields.
  const item = {...base, ...projected};
  const outputArtifactsRecorded = [projected, durable, base].some(value => hasRecordedField(value, 'output_artifact_ids'));
  const handoffIdsRecorded = [projected, durable, base].some(value => hasRecordedField(value, 'handoff_message_ids'));
  const consumedHandoffIdsRecorded = [projected, durable, base].some(value => hasRecordedField(value, 'consumed_handoff_message_ids'));
  const identityFields = [
    'invocation_id', 'run_id', 'trace_id', 'operation_key', 'agent_id', 'role',
    'operation', 'attempt', 'started_at', 'ended_at', 'input_type',
    'execution_mode', 'replay_of_invocation_id', 'parent_invocation_id',
    'previous_in_log_id', 'status', 'side_effect_status',
    'model_provider', 'model_choice', 'model_id', 'input_modalities',
  ];
  identityFields.forEach(key => {
    if (Object.prototype.hasOwnProperty.call(durable, key) && durable[key] !== null && durable[key] !== undefined) {
      item[key] = durable[key];
    }
  });
  item.output_artifact_ids = [...new Set([
    ...asArray(projected.output_artifact_ids),
    ...asArray(durable.output_artifact_ids),
    ...asArray(base.output_artifact_ids),
  ].map(value => String(value || '')).filter(Boolean))];
  item.__output_artifact_ids_recorded = outputArtifactsRecorded;
  item.handoff_message_ids = [...new Set([
    ...asArray(projected.handoff_message_ids),
    ...asArray(durable.handoff_message_ids),
    ...asArray(base.handoff_message_ids),
  ].map(value => String(value || '')).filter(Boolean))];
  item.__handoff_message_ids_recorded = handoffIdsRecorded;
  const consumedHandoffMessageIds = [...new Set([
    ...asArray(projected.consumed_handoff_message_ids),
    ...asArray(durable.consumed_handoff_message_ids),
    ...asArray(base.consumed_handoff_message_ids),
  ].map(value => String(value || '')).filter(Boolean))];
  // A missing field is materially different from an explicitly recorded empty list:
  // legacy invocations cannot be rejected for a fact their schema never stored.
  if (consumedHandoffIdsRecorded) item.consumed_handoff_message_ids = consumedHandoffMessageIds;
  else delete item.consumed_handoff_message_ids;
  item.__consumed_handoff_message_ids_recorded = consumedHandoffIdsRecorded;
  item.identity_validation = durable.identity_validation || base.identity_validation || null;
  const identity = invocationIdentityValidation(item);
  item.identity_validation_status = identity.status;
  item.identity_reliable = identity.reliable;
  item.identity_validation_reason = identity.reason;
  item.__durable = Boolean(row && row.invocation);
  return item;
}

function normalizeAuditHandoff(row) {
  const durable = asObject(row);
  const envelope = asObject(durable.envelope);
  const merged = {...envelope};
  const identityConflicts = {};
  [
    'message_id', 'run_id', 'trace_id', 'producer', 'producer_invocation_id',
    'intended_consumer', 'route_target', 'created_at', 'checkpoint_id',
    'idempotency_key',
  ].forEach(key => {
    if (durable[key] !== undefined && durable[key] !== null && durable[key] !== '') {
      if (merged[key] !== undefined && merged[key] !== null && String(merged[key]) !== String(durable[key])) {
        identityConflicts[key] = [merged[key], durable[key]];
      }
      merged[key] = durable[key];
    }
  });
  const messageId = normalizedId(merged.message_id);
  return {
    ...durable,
    envelope: merged,
    message_id: messageId,
    durable: true,
    __identity_conflicts: identityConflicts,
    receipt_status: durable.receipt_status || durable.server_validation_status || '',
  };
}

function normalizeAuditReceipt(row) {
  const durable = asObject(row);
  const rawStatus = String(
    durable.validation_status
      || durable.server_validation_status
      || durable.status
      || '',
  ).toLowerCase().replace(/-/g, '_');
  const hasMergeConflict = asArray(durable.__merge_conflicts).length > 0
    || Object.keys(asObject(durable.__merge_conflict_values)).length > 0;
  let status = rawStatus;
  // Rejection is terminal for the message. Never let a stale or contradictory
  // server_validated flag overwrite an explicit invalid fact.
  if (hasMergeConflict || durable.valid === false || ['invalid', 'rejected', 'failed'].includes(rawStatus)) status = 'invalid';
  else if (durable.server_validated === true || rawStatus === 'server_validated') status = 'server_validated';
  else if (['valid', 'verified'].includes(rawStatus) && durable.server_validated === true) status = 'server_validated';
  else if (['field_match', 'matched', 'fields_match'].includes(rawStatus)) status = 'field_match';
  else if (!status || ['not_consumed', 'pending', 'legacy_unverified', 'unverified'].includes(status)) status = 'unverified';
  return {
    ...durable,
    message_id: normalizedId(durable.message_id),
    validation_status: rawStatus || 'unverified',
    normalized_status: status,
    server_validated: status === 'server_validated' && durable.server_validated !== false && durable.valid !== false,
    __merge_conflicts: asArray(durable.__merge_conflicts),
    durable: true,
  };
}

function normalizeAuditSourceFetch(row) {
  const durable = asObject(row);
  const declaredBinding = durable.binding_status || durable.fetch_binding_status;
  const binding = String(declaredBinding || 'legacy_unverified').toLowerCase().replace(/-/g, '_');
  const explicitBindingValid = durable.binding_valid !== undefined
    ? durable.binding_valid
    : durable.fetch_binding_valid;
  return {
    ...durable,
    fetch_record_id: normalizedId(durable.fetch_record_id || durable.id),
    source_id: normalizedId(durable.source_id || durable.id),
    invocation_id: normalizedId(durable.invocation_id || durable.fetch_invocation_id),
    result_invocation_id: normalizedId(durable.result_invocation_id || durable.fetch_result_invocation_id),
    operation_key: normalizedId(durable.operation_key || durable.fetch_operation_key),
    execution_mode: durable.execution_mode || durable.fetch_execution_mode || '',
    provider: durable.provider || durable.fetch_provider || '',
    fetch_mode: durable.fetch_mode || durable.mode || 'unknown',
    content_hash: String(durable.content_hash || ''),
    content_hash_scope: String(durable.content_hash_scope || 'unknown'),
    snapshot_sha256: String(durable.snapshot_sha256 || ''),
    snapshot_available: Boolean(durable.snapshot_sha256),
    binding_status: binding,
    binding_valid: explicitBindingValid === undefined ? undefined : explicitBindingValid === true,
    durable: true,
  };
}

function isServerBoundFetch(value) {
  const binding = String(value?.binding_status || value?.fetch_binding_status || '').toLowerCase().replace(/-/g, '_');
  const explicitValid = value?.binding_valid ?? value?.fetch_binding_valid;
  // A binding assertion is an integrity claim, so missing validation metadata
  // must remain legacy/unverified rather than being promoted by an ID alone.
  const valid = explicitValid === true;
  return ['server_bound', 'server_validated'].includes(binding) && valid;
}

function normalizeAuditArtifact(row) {
  const durable = asObject(row);
  let metadata = {};
  if (typeof durable.metadata_json === 'string') {
    try {
      metadata = asObject(JSON.parse(durable.metadata_json));
    } catch (_) {
      metadata = {};
    }
  } else {
    metadata = asObject(durable.metadata);
  }
  return {...metadata, ...durable, durable:true};
}

function normalizedResumeExecutionStatus(value) {
  const raw = String(value || '').toLowerCase().replace(/[-\s]+/g, '_');
  if (['created', 'authorized', 'pending'].includes(raw)) return 'pending';
  if (['running', 'claimed', 'executing'].includes(raw)) return 'running';
  if (['startup_failed', 'startupfailure'].includes(raw)) return 'startup_failed';
  if (['completed', 'succeeded', 'success'].includes(raw)) return 'completed';
  if (['failed', 'error', 'errored'].includes(raw)) return 'failed';
  if (raw === 'not_required') return 'not_required';
  return raw || 'legacy_unverified';
}

function resumeExecutionStatusLabel(status) {
  return ({
    pending: '已授权，等待 worker 接管',
    running: 'worker 执行中',
    startup_failed: 'worker 启动失败，可重试',
    completed: '恢复执行已完成',
    failed: '恢复执行失败',
    not_required: '无需 worker 执行',
    legacy_unverified: '旧记录，无法验证',
  })[status] || '恢复状态未记录';
}

function resumeTransitionStatusLabel(status) {
  return ({
    created: '已创建',
    pending: '等待接管',
    authorized: '已授权',
    running: '已接管并运行',
    handoff_emitted: '恢复交接已发出',
    consumed: '恢复交接已消费',
    startup_failed: '启动失败',
    completed: '执行完成',
    failed: '执行失败',
  })[String(status || '').toLowerCase()] || String(status || '状态未记录');
}

function normalizeResumeReceipt(row) {
  const durable = asObject(row);
  const {claim_owner_token: _claimOwnerToken, owner_token: _ownerToken, ...safe} = durable;
  const transitions = asArray(durable.transitions).map((item, index) => {
    const transition = asObject(item);
    return {
      ...transition,
      transition_id: transition.transition_id ?? `transition-${index + 1}`,
      transition_kind: String(transition.transition_kind || 'execution'),
      from_status: String(transition.from_status || '未记录'),
      to_status: String(transition.to_status || '未记录'),
      owner_fence: finiteValue(transition.owner_fence),
      owner_token_fingerprint: String(transition.owner_token_fingerprint || ''),
      handoff_message_id: normalizedId(transition.handoff_message_id),
      agent_invocation_id: normalizedId(transition.agent_invocation_id),
      agent_id: normalizedId(transition.agent_id),
      operation: String(transition.operation || ''),
      superseded_handoff_message_id: normalizedId(transition.superseded_handoff_message_id),
      reason: String(transition.reason || '原因未记录'),
      created_at: transition.created_at || null,
    };
  });
  return {
    ...safe,
    idempotency_key: String(durable.idempotency_key || durable.id || ''),
    execution_status: normalizedResumeExecutionStatus(durable.execution_status || durable.status),
    durable_run_status: String(durable.durable_run_status || durable.run_status || ''),
    execution_claimed: durable.execution_claimed === true,
    claim_owner_fingerprint: String(durable.claim_owner_fingerprint || ''),
    claim_fence: finiteValue(durable.claim_fence) ?? 0,
    checkpoint_id_before: finiteValue(durable.checkpoint_id_before),
    checkpoint_id_after: finiteValue(durable.checkpoint_id_after),
    transitions,
    durable: true,
  };
}

function normalizeAudit(data, state = {}) {
  const raw = data?.audit;
  const available = Boolean(raw && typeof raw === 'object');
  const audit = asObject(raw?.projection || raw?.audit_projection || raw);
  const runKey = String(
    state.run_id
      || raw?.durable_run_id
      || data?.job?.run_id
      || runId
      || 'unknown-run',
  );
  // Unit-level projections without a durable run id must not inherit receipt
  // rows from a previous synthetic projection in the same browser context.
  const cache = runKey === 'unknown-run'
    ? {runKey: '', collections: Object.create(null)}
    : auditPageCache;
  const pageDescriptors = {
    invocations: collectCachedPage(
      cache,
      runKey,
      pageDescriptor(audit, 'invocations'),
      'invocations',
    ),
    handoffs: collectCachedPage(
      cache,
      runKey,
      pageDescriptor(audit, 'handoffs'),
      'handoffs',
    ),
    receipts: collectCachedPage(
      cache,
      runKey,
      pageDescriptor(audit, 'receipts'),
      'receipts',
    ),
    source_fetches: collectCachedPage(
      cache,
      runKey,
      pageDescriptor(audit, 'source_fetches', auditCollectionAliases.source_fetches),
      'source_fetches',
    ),
    artifacts: collectCachedPage(
      cache,
      runKey,
      pageDescriptor(audit, 'artifacts'),
      'artifacts',
    ),
    input_attachments: collectCachedPage(
      cache,
      runKey,
      pageDescriptor(audit, 'input_attachments', auditCollectionAliases.input_attachments),
      'input_attachments',
    ),
    resume_receipts: collectCachedPage(
      cache,
      runKey,
      pageDescriptor(audit, 'resume_receipts', auditCollectionAliases.resume_receipts),
      'resume_receipts',
    ),
    worker: collectCachedPage(
      cache,
      runKey,
      pageDescriptor(audit, 'worker'),
      'worker',
    ),
  };
  const stateInvocations = asArray(state.agent_invocations).map(item => normalizeAuditInvocation(null, item));
  const invocationRows = pageDescriptors.invocations.present
    ? pageDescriptors.invocations.items.map(row => normalizeAuditInvocation(row)).filter(item => normalizedId(item.invocation_id))
    : stateInvocations;
  const handoffRows = pageDescriptors.handoffs.present
    ? pageDescriptors.handoffs.items.map(normalizeAuditHandoff).filter(item => item.message_id)
    : [];
  const receiptRows = pageDescriptors.receipts.present
    ? pageDescriptors.receipts.items.map(normalizeAuditReceipt).filter(item => item.message_id)
    : [];
  const sourceFetchRows = pageDescriptors.source_fetches.present
    ? pageDescriptors.source_fetches.items
      .map((row, index) => ({...normalizeAuditSourceFetch(row), __audit_order:index}))
      .filter(item => item.source_id || item.requested_url)
    : [];
  const artifactRows = pageDescriptors.artifacts.present
    ? pageDescriptors.artifacts.items.map(normalizeAuditArtifact)
    : [];
  const attachmentRows = pageDescriptors.input_attachments.present
    ? pageDescriptors.input_attachments.items.map(item => asObject(item)).filter(item => normalizedId(item.id || item.attachment_id))
    : [];
  const resumeReceiptRows = pageDescriptors.resume_receipts.present
    ? pageDescriptors.resume_receipts.items.map(normalizeResumeReceipt).filter(item => item.idempotency_key)
    : [];
  const workerRows = pageDescriptors.worker.present
    ? pageDescriptors.worker.items.map(item => asObject(item))
    : [];
  const cachedLiveUsage = window.__latestUsageRunId === runKey
    ? asObject(window.__latestUsageSnapshot)
    : {};
  const usage = freshestUsageSnapshot([
    asObject(audit.usage),
    asObject(data?.usage),
    cachedLiveUsage,
  ]);
  const receiptRowsByMessage = new Map();
  receiptRows.forEach(item => {
    const id = normalizedId(item?.message_id);
    if (!id) return;
    if (!receiptRowsByMessage.has(id)) receiptRowsByMessage.set(id, []);
    receiptRowsByMessage.get(id).push(item);
  });
  const receiptByMessage = new Map();
  receiptRowsByMessage.forEach((rows, id) => {
    const invalid = rows.find(item => normalizedReceiptStatus(item?.normalized_status || item?.validation_status || item?.status, item) === 'invalid');
    const validated = rows.find(item => normalizedReceiptStatus(item?.normalized_status || item?.validation_status || item?.status, item) === 'server_validated');
    const fieldMatch = rows.find(item => normalizedReceiptStatus(item?.normalized_status || item?.validation_status || item?.status, item) === 'field_match');
    receiptByMessage.set(id, invalid || validated || fieldMatch || rows[0]);
  });
  const fetchAttemptsBySource = new Map();
  sourceFetchRows.forEach(item => {
    const key = normalizedId(item.source_id)
      || normalizedSourceUrl(item.final_url || item.requested_url);
    if (!key) return;
    if (!fetchAttemptsBySource.has(key)) fetchAttemptsBySource.set(key, []);
    fetchAttemptsBySource.get(key).push(item);
  });
  // source_fetches pages are returned in durable row order. Preserve that
  // order across operations; `attempt` restarts per operation and therefore
  // cannot identify the latest Fetch for an Article.
  return {
    available,
    raw: audit,
    invocations: invocationRows,
    handoffs: handoffRows,
    receipts: receiptRows,
    sourceFetches: sourceFetchRows,
    artifacts: artifactRows,
    inputAttachments: attachmentRows,
    resumeReceipts: resumeReceiptRows,
    worker: workerRows,
    usage,
    sourceFetchesRecorded:pageDescriptors.source_fetches.present,
    inputAttachmentsRecorded:pageDescriptors.input_attachments.present,
    resumeReceiptsRecorded:pageDescriptors.resume_receipts.present,
    workerRecorded:pageDescriptors.worker.present,
    usageRecorded:usageSnapshotPresent(usage),
    pages: pageDescriptors,
    pagination: auditWindowModel(pageDescriptors),
    byInvocation: new Map(invocationRows.map(item => [String(item.invocation_id || ''), item]).filter(([id]) => id)),
    handoffByMessage: new Map(handoffRows.map(item => [item.message_id, item])),
    receiptByMessage,
    receiptRowsByMessage,
    fetchBySource: new Map(sourceFetchRows.filter(item => item.source_id).map(item => [item.source_id, item])),
    fetchAttemptsBySource,
    inputAttachmentById:new Map(attachmentRows.map(item => [normalizedId(item.id || item.attachment_id), item])),
    resumeReceiptById: new Map(resumeReceiptRows.map(item => [item.idempotency_key, item])),
  };
}

function mergeDurableAuditIntoState(rawState, audit) {
  const state = {...asObject(rawState)};
  // The audit endpoint is paginated to keep the first render bounded. It must
  // enrich, never replace, the complete invocation snapshot stored in final.json;
  // otherwise later writer/verifier records disappear from the role overview.
  const invocations = mergePageItems(
    'invocations',
    asArray(state.agent_invocations).map(item => normalizeAuditInvocation(null, item)),
    asArray(audit.invocations),
  );
  state.agent_invocations = invocations;
  const artifactsByInvocation = new Map();
  audit.artifacts.forEach(artifact => {
    const producer = normalizedId(artifact.producer_invocation_id || artifact.invocation_id);
    if (!producer) return;
    if (!artifactsByInvocation.has(producer)) artifactsByInvocation.set(producer, []);
    const id = normalizedId(artifact.artifact_id);
    if (id) artifactsByInvocation.get(producer).push(id);
  });
  invocations.forEach(item => {
    const ids = artifactsByInvocation.get(String(item.invocation_id || '')) || [];
    item.output_artifact_ids = [...new Set([...asArray(item.output_artifact_ids), ...ids])];
  });
  if (Array.isArray(state.sources) && audit.fetchAttemptsBySource) {
    state.sources = state.sources.map(source => {
      const sourceId = normalizedId(source?.id || source?.source_id);
      const sourceUrl = normalizedSourceUrl(source?.final_url || source?.url);
      const attempts = audit.fetchAttemptsBySource.get(sourceId)
        || audit.fetchAttemptsBySource.get(sourceUrl)
        || [];
      return attempts.length ? {...source, fetch_attempts: attempts} : source;
    });
  }
  const handoffsByInvocation = new Map();
  audit.handoffs.forEach(record => {
    const producer = normalizedId(record.producer_invocation_id || record.envelope?.producer_invocation_id);
    const id = normalizedId(record.message_id);
    if (!producer || !id) return;
    if (!handoffsByInvocation.has(producer)) handoffsByInvocation.set(producer, []);
    handoffsByInvocation.get(producer).push(id);
  });
  invocations.forEach(item => {
    const ids = handoffsByInvocation.get(String(item.invocation_id || '')) || [];
    item.handoff_message_ids = [...new Set([...asArray(item.handoff_message_ids), ...ids])];
  });
  const sources = asArray(state.sources).map(source => ({...source}));
  const sourceById = new Map(sources.map(source => [normalizedId(source.id), source]).filter(([id]) => id));
  audit.sourceFetches.forEach(fetch => {
    let source = sourceById.get(fetch.source_id);
    if (!source && fetch.source_id) {
      source = {
        id:fetch.source_id,
        url:fetch.requested_url || fetch.final_url || '',
        final_url:fetch.final_url || fetch.requested_url || '',
        title:fetch.requested_url || fetch.final_url || `来源 ${fetch.source_id}`,
        source_type:'web',
        snippet:'该来源仅从 durable source_fetches 恢复；页面摘要未保存在公开状态。',
        query_texts:[],
        status:fetch.status || 'discovered',
        iteration:0,
      };
      sources.push(source);
      sourceById.set(fetch.source_id, source);
    }
    if (!source) return;
    const attempts = audit.fetchAttemptsBySource.get(fetch.source_id)
      || audit.fetchAttemptsBySource.get(normalizedSourceUrl(fetch.final_url || fetch.requested_url))
      || [];
    if (attempts.length) source.fetch_attempts = attempts;
    const latest = attempts.at(-1) || fetch;
    source.fetch_record_id = latest.fetch_record_id || source.fetch_record_id || '';
    source.fetch_invocation_id = latest.invocation_id || source.fetch_invocation_id || '';
    source.fetch_result_invocation_id = latest.result_invocation_id || source.fetch_result_invocation_id || '';
    source.fetch_operation_key = latest.operation_key || source.fetch_operation_key || '';
    source.fetch_execution_mode = latest.execution_mode || source.fetch_execution_mode || '';
    source.fetch_provider = latest.provider || source.fetch_provider || '';
    source.fetch_mode = latest.fetch_mode || source.fetch_mode || '';
    source.fetch_binding_status = latest.binding_status || source.fetch_binding_status || 'legacy_unverified';
    if (latest.binding_valid !== undefined) source.fetch_binding_valid = latest.binding_valid === true;
    source.content_hash = latest.content_hash || source.content_hash || '';
    source.content_hash_scope = latest.content_hash_scope || source.content_hash_scope || 'unknown';
    // A source may have several immutable attempts. Do not carry a snapshot
    // from an older attempt onto the latest one; snapshot actions must name the
    // exact Fetch record that owns the bytes.
    if (attempts.length) {
      source.snapshot_sha256 = latest.snapshot_sha256 || '';
      source.snapshot_available = fetchSnapshotAvailable(latest);
    } else {
      source.snapshot_sha256 = latest.snapshot_sha256 || source.snapshot_sha256 || '';
      source.snapshot_available = Boolean(latest.snapshot_available || source.snapshot_sha256);
    }
    source._audit_fetch = latest;
    if (fetch.status && fetch.status !== 'failed' && source.status === 'discovered') source.status = 'fetched';
    if (latest.final_url) source.final_url = source.final_url || latest.final_url;
    if (latest.fetched_at) source.fetched_at = source.fetched_at || latest.fetched_at;
    if (latest.error) source.error = latest.error;
  });
  state.sources = sources;
  state.artifacts = audit.artifacts;
  state.audit_usage = audit.usage;
  state.audit_usage_recorded = audit.usageRecorded;
  return state;
}

function usageLedgerModel(state) {
  const auditUsage = asObject(state?.audit_usage);
  const ledgerRecorded = state?.audit_usage_recorded === true;
  const usageStatus = String(auditUsage.usage_status || 'unavailable').toLowerCase().replace(/-/g, '_');
  const pricingStatus = String(auditUsage.pricing_status || 'unavailable').toLowerCase().replace(/-/g, '_');
  const ledgerEntries = ledgerRecorded ? finiteValue(auditUsage.ledger_entry_count) : null;
  const updatedAt = ledgerRecorded
    ? String(auditUsage.updated_at || auditUsage.last_updated_at || '')
    : '';
  const snapshotAt = ledgerRecorded ? String(auditUsage.snapshot_at || '') : '';
  const pendingModelOperations = ledgerRecorded
    ? finiteValue(auditUsage.pending_model_operations)
    : null;
  const settledModelOperations = ledgerRecorded
    ? finiteValue(auditUsage.settled_model_operations)
    : null;
  const settledModelResponses = ledgerRecorded
    ? finiteValue(auditUsage.settled_model_responses)
    : null;
  const usageRevision = ledgerRecorded
    ? finiteValue(auditUsage.usage_revision)
    : null;
  const reconciledModelOperations = ledgerRecorded
    ? finiteValue(auditUsage.reconciled_model_operations)
    : null;
  const latestEntry = ledgerRecorded ? asObject(auditUsage.latest_entry) : {};
  const providerBreakdown = ledgerRecorded
    ? asArray(auditUsage.provider_breakdown).map(item => asObject(item)).filter(item => item.provider)
    : [];
  const counterProvenance = asObject(state?.counter_provenance);
  const legacyCountersAvailable = !ledgerRecorded && counterProvenance.usage === 'measured';
  const source = legacyCountersAvailable ? asObject(state?.counters) : auditUsage;
  const measuredValue = key => {
    if (legacyCountersAvailable) return finiteValue(source[key]);
    if (!ledgerRecorded || usageStatus === 'not_applicable' || usageStatus === 'unavailable') return null;
    if (usageStatus === 'complete') return finiteValue(source[key]);
    // A partial snapshot can establish calls, but aggregate token zeros may be
    // SQL defaults rather than provider measurements.
    if (usageStatus === 'partial' && ['model_calls', 'model_cache_hits'].includes(key)) {
      return finiteValue(source[key]);
    }
    return null;
  };
  const recordedCost = legacyCountersAvailable
    ? finiteValue(source.estimated_cost_usd)
    : ledgerRecorded
      ? finiteValue(source.estimated_cost_usd)
      : null;
  const priceHasKnownPart = ['complete', 'partial'].includes(pricingStatus);
  // A partial price is still useful as a live minimum when the gateway priced
  // text/output tokens but did not split image or audio tokens. Keep that
  // amount visible, but never present it as the final total.
  const knownCostLowerBound = legacyCountersAvailable
    ? recordedCost
    : ledgerRecorded && usageStatus !== 'not_applicable' && usageStatus !== 'unavailable' && priceHasKnownPart
      ? recordedCost
      : null;
  const estimatedCost = ledgerRecorded && usageStatus === 'complete' && pricingStatus === 'complete'
    ? knownCostLowerBound
    : legacyCountersAvailable
      ? knownCostLowerBound
      : null;
  return {
    source:ledgerRecorded ? 'durable_usage_ledger' : legacyCountersAvailable ? 'state_counters_fallback' : 'unavailable',
    durableAvailable:ledgerRecorded,
    ledgerRecorded,
    ledgerEntries,
    usageStatus:ledgerRecorded ? usageStatus : legacyCountersAvailable ? 'legacy' : 'unavailable',
    pricingStatus:ledgerRecorded ? pricingStatus : legacyCountersAvailable ? 'legacy' : 'unavailable',
    usageReason:ledgerRecorded ? String(auditUsage.reason || auditUsage.usage_reason || '') : '',
    pricingReason:ledgerRecorded ? String(auditUsage.pricing_reason || '') : '',
    modelCalls:measuredValue('model_calls'),
    modelCacheHits:measuredValue('model_cache_hits'),
    inputTokens:measuredValue('input_tokens'),
    outputTokens:measuredValue('output_tokens'),
    recordedCost,
    knownCostLowerBound,
    costIsLowerBound:estimatedCost === null && knownCostLowerBound !== null,
    estimatedCost,
    updatedAt,
    snapshotAt,
    pendingModelOperations,
    settledModelOperations,
    settledModelResponses,
    usageRevision,
    reconciledModelOperations,
    latestEntry,
    providerBreakdown,
  };
}

function formatEstimatedCost(value) {
  const amount = finiteValue(value);
  if (amount === null) return '费用未记录';
  if (amount > 0 && amount < 0.000000001) return '$<0.000000001';
  return `$${amount.toFixed(amount > 0 && amount < 0.000001 ? 9 : 6)}`;
}

function formatKnownCost(value) {
  const formatted = formatEstimatedCost(value);
  return formatted === '费用未记录' ? formatted : `${formatted}+`;
}

function usageEntryLabel(entry, {latest = false} = {}) {
  const value = asObject(entry);
  const provider = providerName(String(value.provider || '模型未记录'));
  const calls = finiteValue(value.model_calls);
  const cost = finiteValue(value.estimated_cost_usd);
  const status = String(value.pricing_status || '').toLowerCase();
  const amount = cost === null
    ? '费用未记录'
    : status === 'partial'
    ? `至少 ${formatKnownCost(cost)}`
    : cost === 0 && status === 'complete' && calls && calls > 0
      ? '价格表为 $0（调用与 Token 仍计入）'
      : formatEstimatedCost(cost);
  const callText = calls === null ? '调用数未记录' : `${calls} 次模型响应`;
  return `${latest ? '最近到账：' : ''}${provider} · ${callText} · ${amount}`;
}

function usageBreakdownLabel(usage) {
  const rows = asArray(usage?.providerBreakdown);
  if (!rows.length) return '模型接口返回用量后立即入账；后续整理与保存不会延迟金额更新。尚未返回用量的请求不会按 0 显示。';
  return rows.map(item => {
    const providerUsage = asObject(item);
    return usageEntryLabel({
      ...providerUsage,
      // A mixed-model run can contain both free and billed providers.  Use
      // each provider's own price status before falling back to legacy totals.
      pricing_status: providerUsage.pricing_status || usage?.pricingStatus,
    });
  }).join('；');
}

function usageStatusName(value) {
  return ({
    complete:'完整计量',
    partial:'部分计量，Token 合计不可验证',
    not_applicable:'Provider 未提供可计量用量',
    unavailable:'用量证据不可用',
    legacy:'历史公开计数器回退',
  })[value] || '用量状态未记录';
}

function pricingStatusName(value) {
  return ({
    complete:'费用可计算',
    partial:'价格证据不完整',
    not_applicable:'费用不适用',
    unavailable:'价格或单价未记录，费用不可计算',
    legacy:'历史费用字段回退',
  })[value] || '价格状态未记录';
}

function unmeasuredUsageLabel(status) {
  return ({
    partial:'部分计量',
    not_applicable:'未计量',
    unavailable:'不可用',
  })[status] || '未记录';
}

function auditInvocationList() {
  return window.__latestAudit?.available
    ? asArray(window.__latestAudit.invocations)
    : asArray(window.__latestState?.agent_invocations);
}

function agentRuntimeEvidence(agent, invocations = [], events = [], audit = window.__latestAudit || null) {
  const calls = asArray(invocations).filter(item => String(item?.agent_id || '') === String(agent || ''));
  const callIds = new Set(calls.map(item => String(item?.invocation_id || '')).filter(Boolean));
  const relatedEvents = asArray(events).filter(event => {
    const invocationId = eventInvocationId(event);
    return (invocationId && callIds.has(invocationId)) || eventAgentId(event) === agent;
  });
  const artifactIds = new Set([
    ...calls.flatMap(item => asArray(item?.output_artifact_ids)),
    ...relatedEvents.flatMap(eventArtifactIds),
  ].map(value => String(value || '')).filter(Boolean));
  const latest = calls.at(-1) || null;
  const reliableCalls = calls.filter(item => invocationIdentityValidation(item).reliable);
  let status = 'waiting';
  if (latest?.status === 'running' && invocationIdentityValidation(latest).reliable) status = 'running';
  else if (latest && ['failed', 'cancelled'].includes(latest.status)) status = 'blocked';
  else if (latest?.status === 'succeeded' && invocationIdentityValidation(latest).reliable) status = 'done';
  else if (calls.length || relatedEvents.length || artifactIds.size) status = 'observed';
  return {
    agent,
    calls,
    latest,
    events: relatedEvents,
    artifactIds: [...artifactIds],
    observed: Boolean(calls.length || relatedEvents.length || artifactIds.size),
    reliableCalls,
    identityIssues: calls.filter(item => !invocationIdentityValidation(item).reliable),
    status,
    statusReason: latest
      ? `invocation ${latest.invocation_id || 'ID 未记录'} · ${invocationStatus(latest.status)}${invocationIdentityValidation(latest).reliable ? '' : ' · 身份校验异常'}`
      : relatedEvents.length
        ? `${relatedEvents.length} 条阶段事件`
        : artifactIds.size
          ? `${artifactIds.size} 个阶段产物`
          : '没有 invocation、event 或 artifact 记录',
  };
}

function displayNumber(value, fallback = '历史字段未记录') {
  const numeric = finiteValue(value);
  return numeric === null ? fallback : String(numeric);
}

function recordedArrayCount(records, key) {
  const list = asArray(records);
  if (!list.length) return null;
  const marker = `__${key}_recorded`;
  const hasMetadata = list.some(item => hasRecordedField(item, marker));
  const recorded = hasMetadata
    ? list.some(item => item?.[marker] === true)
    : list.some(item => hasRecordedField(item, key));
  if (!recorded) return null;
  return list.reduce((sum, item) => sum + (Array.isArray(item?.[key]) ? item[key].length : 0), 0);
}

function countText(value, suffix, missing = '未记录') {
  return value === null || value === undefined ? missing : `${value}${suffix}`;
}

function displayPercent(value) {
  const numeric = finiteValue(value);
  if (numeric === null) return '历史字段未记录';
  return `${Math.round(Math.max(0, Math.min(1, numeric)) * 100)}%`;
}

function evidenceIds(evidence = window.__latestState?.evidence || []) {
  return new Set(asArray(evidence).map(item => String(item?.id || '')).filter(Boolean));
}

function rawSlotAudits(state) {
  return asArray(asObject(state?.closure).slot_audits).filter(item => item && typeof item === 'object');
}

function uniqueSlotDescriptors(descriptors) {
  const seen = new Set();
  return descriptors.filter(item => {
    const id = String(item?.id || '').trim();
    if (!id || seen.has(id)) return false;
    seen.add(id);
    return true;
  }).map(item => ({...item, id:String(item.id)}));
}

function requiredSlotDescriptors(state) {
  const planSlots = asArray(state?.plan?.slots).filter(item => item && typeof item === 'object' && item.id);
  const required = planSlots.filter(slot => slot.required !== false);
  if (required.length) return uniqueSlotDescriptors(required);

  const closure = asObject(state?.closure);
  const declaredIds = asArray(closure.required_slot_ids || state?.required_slot_ids)
    .map(id => String(id || '').trim()).filter(Boolean).map(id => ({id, description:id}));
  if (declaredIds.length) return uniqueSlotDescriptors(declaredIds);
  if (planSlots.length) return [];

  const audits = rawSlotAudits(state);
  const declaredCount = finiteValue(closure.required_slots);
  if (declaredCount !== null && declaredCount > 0) {
    const descriptors = uniqueSlotDescriptors(audits.slice(0, Math.floor(declaredCount)).map(item => ({id:item.slot_id, description:item.description})));
    for (let index = descriptors.length; index < Math.floor(declaredCount); index += 1) {
      descriptors.push({id:`required-slot-${index + 1}`, description:`必需回答目标 ${index + 1}`});
    }
    return descriptors;
  }
  return uniqueSlotDescriptors(audits.map(item => ({id:item.slot_id, description:item.description})));
}

function normalizeSlotAuditRow(descriptor, audit, index, required = true) {
  const record = audit && typeof audit === 'object' ? audit : null;
  const slotId = String(descriptor?.id || record?.slot_id || `required-slot-${index + 1}`);
  return {
    slotId,
    description:String(descriptor?.description || record?.description || `${required ? '必需' : '可选'}回答目标 ${index + 1}`),
    required,
    audit:record,
    present:Boolean(record),
    passed:recordedBoolean(record, 'passed')
  };
}

function slotAuditRows(state, includeOptional = true) {
  const audits = rawSlotAudits(state);
  const bySlot = new Map(audits.map(item => [String(item.slot_id || ''), item]));
  const requiredDescriptors = requiredSlotDescriptors(state);
  const requiredIds = new Set(requiredDescriptors.map(item => String(item.id)));
  const rows = requiredDescriptors.map((descriptor, index) => normalizeSlotAuditRow(
    descriptor,
    bySlot.get(String(descriptor.id)),
    index,
    true,
  ));
  if (!includeOptional) return rows;

  const planSlots = asArray(state?.plan?.slots).filter(item => item && typeof item === 'object');
  const optionalDescriptors = planSlots.filter(slot => slot.required === false && slot.id && !requiredIds.has(String(slot.id)));
  optionalDescriptors.forEach((descriptor, index) => {
    const slotId = String(descriptor.id);
    rows.push(normalizeSlotAuditRow(descriptor, bySlot.get(slotId), rows.length + index, false));
  });
  audits.forEach(audit => {
    const slotId = String(audit.slot_id || '');
    if (!slotId || requiredIds.has(slotId) || rows.some(row => row.slotId === slotId)) return;
    rows.push(normalizeSlotAuditRow({id:slotId, description:audit.description}, audit, rows.length, false));
  });
  return rows;
}

function slotGateValue(row, definition, state = null) {
  if (!row?.present) return null;
  if (definition.field === 'supporting_evidence_ids') {
    const values = recordedArray(row.audit, definition.field);
    if (values === null) return null;
    const knownIds = evidenceIds(state?.evidence);
    if (values.some(id => !knownIds.has(String(id || '')))) return null;
    return values.length > 0;
  }
  return recordedBoolean(row.audit, definition.field);
}

async function getJSON(url, options) {
  const response = await fetch(url, {cache:'no-store', ...(options || {})});
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return data;
}

async function loadConfig() {
  try {
    const config = await getJSON('/api/config');
    window.__defaultConfig = config;
    window.__configError = '';
    renderProviderBadge(window.__latestState || null);
  } catch (error) {
    window.__configError = String(error?.message || '配置接口不可用');
    renderProviderBadge(window.__latestState || null);
    throw error;
  }
}

function renderProviderBadge(state) {
  const methodology = state?.methodology || {};
  const model = methodology.model_choice || methodology.model || methodology.model_provider;
  const modelId = methodology.model || '';
  const search = methodology.search_provider;
  const config = window.__defaultConfig || {};
  const routeSummary = routeModelSummary(methodology);
  const hasRunRoutes = routeSummary.routes.some(route => route.choice || route.model);
  const hasRunRecord = Boolean(model || search || hasRunRoutes);
  if (!hasRunRecord && window.__configError) {
    $('providerBadge').innerHTML = '<span class="pulse needs-config"></span><span><b>配置不可用</b><i>默认配置读取失败</i><i>请检查服务状态后重试</i></span>';
    $('providerBadge').setAttribute('aria-label', '默认配置不可用，请检查服务状态后重试');
    $('providerBadge').title = window.__configError;
    return;
  }
  const defaultProfile = String(config.default_profile || config.default_model || '');
  const modelLabel = hasRunRecord
    ? routeSummary.isTeam
      ? `${routeSummary.choices.map(providerName).join(' + ')} 协作`
      : providerName(model || routeSummary.choices[0] || '本次运行未记录')
    : defaultProfile === 'team'
      ? 'Qwen + GPT + DeepSeek 协作'
      : providerName(config.default_model || config.model || '模型未记录');
  const searchLabel = providerName(hasRunRecord ? (search || '本次运行未记录') : (config.search_provider || '检索未记录'));
  const scope = hasRunRecord ? '本次配置' : '新任务默认';
  const exactModel = hasRunRecord && !routeSummary.isTeam && modelId && modelId !== model ? ` · ${modelId}` : '';
  const routeDetail = hasRunRecord && routeSummary.isTeam
    ? `${routeSummary.routes.map(route => `${route.role}:${route.model || route.choice || '未记录'}`).join(' · ')} · Critic:deterministic-evidence-gate`
    : `模型 ${modelLabel + exactModel}`;
  $('providerBadge').innerHTML = `<span class="pulse"></span><span><b>${scope}</b><i>${escapeHTML(modelLabel + exactModel)}</i><i>检索入口 ${escapeHTML(searchLabel)}</i></span>`;
  $('providerBadge').setAttribute('aria-label', `${scope}：${modelLabel}，检索入口 ${searchLabel}`);
  $('providerBadge').title = hasRunRecord
    ? `来自该运行持久化 methodology：${routeDetail} / 检索 ${search || '未记录'}`
    : '当前服务器创建新任务时使用的默认配置；不代表历史运行实际 provider';
}

function protocolStatusLabel(status) {
  return ({
    'validated-adapter': '有限适配已有有效验证回执',
    'adapter-blocked': '适配路径还需要补验证，不能复用已有验证结论',
    'current-runtime': '当前解释器已安装该 SDK',
    'current-lock': 'TypeScript 锁文件版本匹配',
    candidate: '候选依赖，当前未接入',
    'installed-but-unverified': '已安装但版本未覆盖',
    'not-installed': '当前解释器未安装',
  })[status] || '状态未记录';
}

async function loadSystemContract() {
  const contract = await getJSON('/api/system-contract');
  const maturityClass = {
    implemented: 'implemented',
    'validated-adapter': 'adapter',
    'adapter-blocked': 'blocked',
    'installed-but-unverified': 'blocked',
    'not-installed': 'blocked',
    candidate: 'unverified',
    'current-runtime': 'unverified',
    'implemented-unverified': 'implemented',
    verified: 'verified',
    unverified: 'unverified',
    planned: 'unverified',
  };
  $('systemBoundary').dataset.contractSource = 'runtime';
  $('boundaryDescription').textContent = contract.warning || '运行时能力契约未记录说明；以下卡片按 maturity 显示，不把实现状态当作验证状态。';
  const verification = contract.official_verification || {};
  const checkedAt = new Date(`${verification.snapshot_checked_at || verification.checked_at || ''}T00:00:00Z`);
  const ageDays = Number.isNaN(checkedAt.getTime()) ? null : Math.max(0, Math.floor((Date.now() - checkedAt.getTime()) / 86400000));
  const stale = ageDays === null || ageDays > 30;
  $('boundaryFreshness').classList.toggle('stale', stale);
  $('boundaryFreshness').innerHTML = `<div><span>OFFICIAL VERSION SNAPSHOT · ${escapeHTML(verification.snapshot_checked_at || verification.checked_at || '日期未记录')}</span><strong>${stale ? '官方快照已过期或日期无效' : '官方版本快照与本机运行时分开展示'}</strong><small>${ageDays === null ? '无法计算快照时效' : `距快照 ${ageDays} 天 · ${stale ? '需要重新联网核验' : '30 天有效期内'}`}</small>${verification.receipt_checked_at ? `<small>最近有效适配回执：${escapeHTML(verification.receipt_checked_at)}</small>` : '<small>当前没有有效适配回执；版本匹配不等于验证通过</small>'}${verification.record_url ? `<a href="${escapeHTML(verification.record_url)}" target="_blank" rel="noreferrer">打开完整核验记录 ↗</a>` : ''}</div><div>${(verification.packages || []).map(item => {
    const runtimeStatus = String(item.runtime_status || item.version_status || 'unverified');
    const adapterStatus = String(item.adapter_verification_status || item.verification_status || 'unverified');
    const receiptDate = item.receipt_checked_at || '未找到有效回执';
    return `<article><b>${escapeHTML(item.name)}</b><span>协议/上游快照：${escapeHTML(item.protocol_version || item.upstream || '未记录')}</span><span>SDK 期望：${escapeHTML(item.expected_sdk || '未记录')}</span><span>当前运行时：${escapeHTML(item.installed || '未记录')}</span><em class="protocol-version-${escapeHTML(adapterStatus)}">适配验证：${escapeHTML(protocolStatusLabel(adapterStatus))}</em><small>运行时：${escapeHTML(protocolStatusLabel(runtimeStatus))} · 回执：${escapeHTML(receiptDate)}</small><p>${escapeHTML(item.decision || '适配决策未记录')}</p>${item.source_url ? `<a href="${escapeHTML(item.source_url)}" target="_blank" rel="noreferrer">官方发布源 ↗</a>` : ''}</article>`;
  }).join('') || '<article><span>后端未返回官方版本核验记录</span></article>'}</div>`;
  $('boundaryGrid').innerHTML = asArray(contract.boundaries).map(item => {
    const maturity = String(item?.maturity || '').toLowerCase().replace(/[_\s]+/g, '-');
    const tone = maturityClass[maturity] || 'unverified';
    const maturityLabel = item?.maturity_label || (maturity === 'adapter-blocked' ? '适配器已实现 · 验证回执待补齐' : maturity || '成熟度未记录');
    const evidence = asArray(item?.verified_by).map(value => `<li>${escapeHTML(value)}</li>`).join('');
    const limits = asArray(item?.limitations).map(value => `<li>${escapeHTML(value)}</li>`).join('');
    const statusFacts = [
      item?.runtime_status ? `运行时：${protocolStatusLabel(String(item.runtime_status))}` : '',
      item?.adapter_verification_status ? `适配验证：${protocolStatusLabel(String(item.adapter_verification_status))}` : '',
      item?.typescript_lock_status ? `TS 锁：${protocolStatusLabel(String(item.typescript_lock_status))}` : '',
    ].filter(Boolean).join(' · ');
    const maturityNote = maturity === 'adapter-blocked'
      ? '<mark>适配路径还缺少明确验证；不能把 SDK 版本、静态文档或设计路线当作已验证能力。</mark>'
      : maturity === 'installed-but-unverified' || maturity === 'not-installed' || maturity === 'candidate'
        ? '<mark>当前运行时或依赖状态不足；本卡片不复用其他环境的已验证结论。</mark>'
      : maturity === 'verified'
        ? '<mark>运行时记录标为已验证；仍应查看下方证据与限制。</mark>'
        : maturity === 'implemented' || maturity === 'validated-adapter'
          ? '<mark>已实现/有限适配记录不等于完整 conformance 或事实认证。</mark>'
          : '<mark>成熟度未达到已验证；请先查看限制和核验依据。</mark>';
    const evidenceHeading = maturity === 'verified' || maturity === 'validated-adapter' ? '核验依据' : '记录依据（非完整验证）';
    return `<article class="boundary-card ${tone}" data-maturity="${escapeHTML(maturity || 'unrecorded')}"><span>${escapeHTML(item?.layer || '边界未记录')} · ${escapeHTML(maturityLabel)}</span><strong>${escapeHTML(item?.title || '能力标题未记录')}</strong><p>${escapeHTML(item?.implementation || '实现说明未记录')}</p>${maturityNote}${statusFacts ? `<div class="boundary-status-line">${escapeHTML(statusFacts)}</div>` : ''}<small>${escapeHTML(item?.protocol || '协议版本未记录')}</small><details class="boundary-audit"><summary>查看成熟度依据与明确限制</summary><div><b>${evidenceHeading}</b><ul>${evidence || '<li>未记录验证证据</li>'}</ul><b>不包含</b><ul>${limits || '<li>无额外限制记录</li>'}</ul></div></details></article>`;
  }).join('');
  $('boundarySafety').innerHTML = `<b>浏览器安全边界</b>${(contract.browser_safety || []).map(item => `<span>${escapeHTML(item)}</span>`).join('')}<small>${escapeHTML(contract.contract_version)} · ${escapeHTML(contract.source_of_truth)}</small>`;
  renderSelectionReviews(contract.selection_reviews || []);
}

function renderSelectionReviews(reviews) {
  if (!reviews.length) {
    $('selectionReviewList').innerHTML='<div class="protocol-runtime-empty">服务端没有返回选型审查记录；页面不根据静态文字推断“已质疑”。</div>';
    return;
  }
  $('selectionReviewList').innerHTML=reviews.map((review,index)=>`<article class="selection-review-card"><header><span>${String(index+1).padStart(2,'0')} · ${escapeHTML(review.id)}</span><b>${escapeHTML(review.status||'审查记录')}</b></header><h4>${escapeHTML(review.decision)}</h4><dl><dt>质疑角色</dt><dd>${escapeHTML(review.reviewer_role||'未记录')}</dd><dt>最强反对理由</dt><dd>${escapeHTML(review.challenge||'未记录')}</dd><dt>保留依据</dt><dd>${escapeHTML(review.response||'未记录')}</dd><dt>依据范围</dt><dd>${escapeHTML(review.evidence||'未记录')}</dd><dt>重新评估触发条件</dt><dd>${escapeHTML(review.revisit_when||'未记录')}</dd></dl></article>`).join('');
}

function normalizeProtocolAudit(payload) {
  const source = asObject(payload?.projection || payload?.audit_projection || payload?.audit || payload);
  const runKey = String(
    window.__latestState?.run_id
      || source.durable_run_id
      || runId
      || 'unknown-run',
  );
  const cache = runKey === 'unknown-run'
    ? {runKey: '', collections: Object.create(null)}
    : protocolPageCache;
  const pages = {};
  Object.keys(protocolCollectionLabels).forEach(collection => {
    const aliases = collection === 'resume_receipts' ? auditCollectionAliases.resume_receipts : [collection];
    pages[collection] = collectCachedPage(
      cache,
      runKey,
      pageDescriptor(source, collection, aliases),
      collection,
    );
  });
  const normalized = {...source};
  Object.keys(pages).forEach(collection => {
    if (pages[collection].present) normalized[collection] = pages[collection].items;
  });
  normalized.pages = pages;
  normalized.pagination = auditWindowModel(pages);
  return normalized;
}

function pageWindowCount(page) {
  const loaded = asArray(page?.items).length;
  if (page?.total !== null && page?.total !== undefined) return `${loaded} / ${page.total} 条`;
  return `${loaded} 条`;
}

function auditWindowNoticeMarkup(audit, scope = 'run') {
  const pagination = audit?.pagination || auditWindowModel(audit?.pages || {});
  const pages = pagination.pages || {};
  const labels = scope === 'protocol' ? protocolCollectionLabels : auditCollectionLabels;
  const presentPages = Object.entries(pages).filter(([, page]) => page?.present);
  if (!presentPages.length) return '';
  const hasWindow = presentPages.some(([, page]) => page.windowed);
  const hasMore = presentPages.some(([, page]) => page.hasMore === true);
  const title = hasWindow ? '当前为分页窗口' : '当前为历史窗口，后端尚未声明分页游标';
  const detail = hasWindow
    ? '页面只显示当前 audit projection 窗口；已载入的调用、交接、文章来源读取和产物会在这里合并，未载入的历史记录不会被猜测。'
    : '这是旧版数组响应。页面会完整展示收到的记录，但不能据此证明服务端已提供可继续加载的全量审计。';
  const controls = presentPages.map(([collection, page]) => {
    const label = labels[collection] || collection;
    const count = pageWindowCount(page);
    if (auditPageStalls[scope]?.[collection]) {
      return `<span class="audit-page-unknown"><b>${escapeHTML(label)}</b><small>续页响应没有推进，已停止重复请求；${escapeHTML(auditPageStalls[scope][collection])}</small></span>`;
    }
    if (page.hasMore === true && page.nextCursor !== null && page.nextCursor !== undefined) {
      const attribute = scope === 'protocol' ? 'data-protocol-load-more' : 'data-audit-load-more';
      const pending = scope === 'protocol' ? pendingProtocolPages.has(collection) : pendingAuditPages.has(collection);
      return `<button type="button" ${attribute}="${escapeHTML(collection)}" ${pending ? 'disabled' : ''} aria-busy="${String(pending)}" aria-label="${escapeHTML(pending ? `正在加载${label}下一页` : `加载${label}下一页`)}"><span>${escapeHTML(label)}</span><b>${pending ? '正在加载' : '继续加载'}</b><small>已载入 ${escapeHTML(count)}</small></button>`;
    }
    if (page.windowed && page.hasMore === false) {
      return `<span class="audit-page-end"><b>${escapeHTML(label)}</b><small>当前窗口 ${escapeHTML(count)} · 已到末页</small></span>`;
    }
    return `<span class="audit-page-unknown"><b>${escapeHTML(label)}</b><small>${escapeHTML(count)} · 续页游标未记录</small></span>`;
  }).join('');
  const errors = presentPages.map(([collection]) => auditPageErrors[scope]?.[collection] ? `<p class="audit-page-error">${escapeHTML(auditPageErrors[scope][collection])}</p>` : '').join('');
  return `<div class="audit-window-copy"><span>AUDIT WINDOW</span><strong>${title}</strong><p>${detail}</p></div><div class="audit-window-meta"><b>${hasMore ? '仍有审计记录未载入' : hasWindow ? '当前窗口已同步' : '分页能力未在本次响应中声明'}</b><small>${presentPages.map(([collection, page]) => `${labels[collection] || collection} ${pageWindowCount(page)}`).join(' · ')}</small></div><div class="audit-window-actions">${controls}</div>${errors}`;
}

function renderAuditWindow(audit) {
  const host = $('auditWindowStatus');
  if (!host) return;
  const markup = auditWindowNoticeMarkup(audit, 'run');
  host.classList.toggle('hidden', !markup);
  host.innerHTML = markup;
  host.querySelectorAll('[data-audit-load-more]').forEach(button => {
    button.addEventListener('click', () => loadMoreAuditRecords(button.dataset.auditLoadMore, button));
  });
}

function auditPageURL(path, collection, page, scope) {
  const cursor = page?.nextCursor;
  const params = new URLSearchParams();
  params.set('limit', String(page?.limit || auditPageLimit));
  params.set('cursor', typeof cursor === 'object' ? JSON.stringify(cursor) : String(cursor ?? ''));
  params.set('audit_collection', collection);
  params.set('audit_scope', scope);
  return `${path}?${params.toString()}`;
}

function auditPageFallbackURL(path, collection, page, scope) {
  const cursor = page?.nextCursor;
  const params = new URLSearchParams();
  params.set('audit_limit', String(page?.limit || auditPageLimit));
  params.set('audit_cursor', typeof cursor === 'object' ? JSON.stringify(cursor) : String(cursor ?? ''));
  params.set('audit_collection', collection);
  params.set('audit_scope', scope);
  return `${path}?${params.toString()}`;
}

function responseHasAuditPage(data, collection, scope) {
  const source = scope === 'protocol'
    ? asObject(data?.projection || data?.audit_projection || data?.audit || data)
    : asObject(data?.audit);
  const aliases = collection === 'source_fetches'
    ? auditCollectionAliases.source_fetches
    : collection === 'resume_receipts'
      ? auditCollectionAliases.resume_receipts
      : [collection];
  return pageDescriptor(source, collection, aliases).windowed;
}

async function fetchAuditPage(collection, scope, trigger = null) {
  const isProtocol = scope === 'protocol';
  const current = isProtocol
    ? window.__latestProtocolAudit?.pages?.[collection]
    : window.__latestAudit?.pages?.[collection];
  const pending = isProtocol ? pendingProtocolPages : pendingAuditPages;
  if (!current?.hasMore || current.nextCursor === null || pending.has(collection)) return;
  pending.add(collection);
  delete auditPageErrors[scope][collection];
  delete auditPageStalls[scope][collection];
  announceLive(
    `正在加载${(isProtocol ? protocolCollectionLabels : auditCollectionLabels)[collection] || '审计'}下一页；当前窗口暂不代表全量记录。`,
    `audit-page-loading:${scope}:${collection}`,
  );
  if (isProtocol) renderProtocolAudit(window.__latestProtocolAudit || {});
  else renderAuditWindow(window.__latestAudit || {});
  const path = isProtocol
    ? `/api/runs/${encodeURIComponent(runId)}/protocol-audit`
    : `/api/runs/${encodeURIComponent(runId)}`;
  let data = null;
  let lastError = null;
  const urls = [auditPageURL(path, collection, current, scope), auditPageFallbackURL(path, collection, current, scope)];
  try {
    for (const url of urls) {
      try {
        const candidate = await getJSON(url);
        data = candidate;
        if (responseHasAuditPage(candidate, collection, scope) || url === urls[urls.length - 1]) break;
      } catch (error) {
        lastError = error;
      }
    }
    if (!data) throw lastError || new Error('审计续页响应为空');
    const previousHint = window.__auditPageHint;
    try {
      window.__auditPageHint = {scope, collection, cursor:current.nextCursor};
      if (isProtocol) {
        window.__latestProtocolAudit = normalizeProtocolAudit(data);
        renderProtocolAudit(window.__latestProtocolAudit);
      } else {
        render(data);
      }
      const latest = isProtocol
        ? window.__latestProtocolAudit?.pages?.[collection]
        : window.__latestAudit?.pages?.[collection];
      const progressed = Boolean(
        latest
          && (asArray(latest.items).length > asArray(current.items).length
            || pageCursorToken(latest.nextCursor) !== pageCursorToken(current.nextCursor)),
      );
      if (!progressed) {
        auditPageStalls[scope][collection] = '请等待后端返回新的 cursor 或记录';
      }
      announceLive(
        progressed
          ? `${(isProtocol ? protocolCollectionLabels : auditCollectionLabels)[collection] || collection}已加载下一页审计记录`
          : `${(isProtocol ? protocolCollectionLabels : auditCollectionLabels)[collection] || collection}的续页没有推进，已停止重复请求`,
        `audit-page:${scope}:${collection}:${current.nextCursor}:${progressed}`,
      );
    } finally {
      window.__auditPageHint = previousHint;
    }
  } catch (error) {
    auditPageErrors[scope][collection] = `继续加载${(isProtocol ? protocolCollectionLabels : auditCollectionLabels)[collection] || '审计'}失败：${error.message}`;
  } finally {
    pending.delete(collection);
    if (isProtocol) renderProtocolAudit(window.__latestProtocolAudit || {});
    else renderAuditWindow(window.__latestAudit || {});
    const host = isProtocol ? $('protocolRuntimeAudit') : $('auditWindowStatus');
    const selector = isProtocol ? '[data-protocol-load-more]' : '[data-audit-load-more]';
    const nextControl = [...(host?.querySelectorAll(selector) || [])].find(
      button => button.dataset.protocolLoadMore === collection || button.dataset.auditLoadMore === collection,
    );
    (nextControl || host)?.focus?.({preventScroll: true});
  }
}

function loadMoreAuditRecords(collection, trigger = null) {
  return fetchAuditPage(collection, 'run', trigger);
}

function loadMoreProtocolRecords(collection, trigger = null) {
  return fetchAuditPage(collection, 'protocol', trigger);
}

function protocolRecordMarkup(kind, item) {
  const value = asObject(item);
  if (kind === 'external_runs') {
    return `<dl class="audit-detail-list"><dt>外部生命周期 run</dt><dd>${escapeHTML(value.run_id || '未记录')}</dd><dt>类型</dt><dd>${escapeHTML(value.kind || '未记录')}</dd><dt>Thread</dt><dd>${escapeHTML(value.thread_id || '未记录')}</dd><dt>声明的父 run</dt><dd>${escapeHTML(value.declared_parent_run_id || '无')}</dd><dt>当前状态</dt><dd>${escapeHTML(value.status || '未记录')}</dd><dt>请求摘要</dt><dd class="audit-mono">${escapeHTML(value.request_hash || '未记录')}</dd><dt>最后更新时间</dt><dd>${escapeHTML(formatTimestamp(value.updated_at))}</dd></dl>`;
  }
  if (kind === 'status_transitions') {
    return `<dl class="audit-detail-list"><dt>Transition ID</dt><dd>${escapeHTML(value.transition_id ?? '未记录')}</dd><dt>外部 run</dt><dd class="audit-mono">${escapeHTML(value.run_id || '未记录')}</dd><dt>状态变化</dt><dd>${escapeHTML(value.from_status || '初始')} → ${escapeHTML(value.to_status || '未记录')}</dd><dt>发生时间</dt><dd>${escapeHTML(formatTimestamp(value.changed_at))}</dd></dl><p class="audit-human-note">状态转移用于核对外部协议生命周期，不等于内部六智能体已经完成对应阶段。</p>`;
  }
  if (kind === 'interrupts') {
    return `<dl class="audit-detail-list"><dt>Interrupt ID</dt><dd class="audit-mono">${escapeHTML(value.interrupt_id || '未记录')}</dd><dt>原因</dt><dd>${escapeHTML(value.reason || '未记录')}</dd><dt>状态</dt><dd>${escapeHTML(value.status || '未记录')}</dd><dt>外部 run</dt><dd>${escapeHTML(value.protocol_run_id || '未记录')}</dd><dt>Thread</dt><dd>${escapeHTML(value.thread_id || '未记录')}</dd><dt>响应动作</dt><dd>${escapeHTML(value.response_schema?.properties?.action?.const || '历史记录未保存')}</dd><dt>恢复回执</dt><dd>${escapeHTML(value.resume_receipt_id || '尚未消费')}</dd></dl>`;
  }
  if (kind === 'message_snapshots') {
    const roles = asArray(value.roles).map((role, index) => `${role} · ${value.message_ids?.[index] || '无 ID'}`).join('；');
    return `<dl class="audit-detail-list"><dt>Thread</dt><dd>${escapeHTML(value.thread_id || '未记录')}</dd><dt>消息数量</dt><dd>${escapeHTML(value.message_count ?? '未记录')}</dd><dt>角色与消息 ID</dt><dd>${escapeHTML(roles || '未记录')}</dd><dt>更新时间</dt><dd>${escapeHTML(formatTimestamp(value.updated_at))}</dd></dl><p class="audit-human-note">消息正文不会从协议审计接口返回；这里展示的是用于核对线程范围的元数据。</p>`;
  }
  return `<dl class="audit-detail-list"><dt>记录类型</dt><dd>${escapeHTML(kind)}</dd><dt>状态</dt><dd>${escapeHTML(value.status || value.event_type || '未记录')}</dd><dt>时间</dt><dd>${escapeHTML(formatTimestamp(value.created_at || value.updated_at))}</dd><dt>关联回执</dt><dd>${escapeHTML(value.receipt_id || value.resume_receipt_id || '未记录')}</dd></dl>`;
}

function showProtocolRecord(kind, index) {
  const item = asArray(window.__latestProtocolAudit?.[kind])[Number(index)];
  if (!item) return;
  const label = protocolCollectionLabels[kind] || '协议审计';
  openAuditDialog(
    'PROTOCOL CONTROL PLANE',
    `${label}详情`,
    '以下是可由人工核对的协议控制面字段；页面没有把原始 JSON 直接倾倒出来。',
    protocolRecordMarkup(kind, item),
  );
}

function ledgerEntryMarkup(item) {
  const event = asObject(item?.event);
  const invocation = asObject(item?.invocation);
  const failure = asObject(item?.failure);
  const artifact = asObject(item?.artifact);
  const fields = [
    ['记录类型', ({event:'阶段事件', gate:'质量门', failure:'异常记录', artifact:'阶段产物'})[item?.kind] || item?.kind || '审计记录'],
    ['发生时间', formatTimestamp(item?.timestamp)],
    ['可读说明', item?.detail || '详情未记录'],
    ['核对依据', item?.proof || '依据未记录'],
  ];
  if (event.node) fields.push(['工作流节点', event.node]);
  if (event.event_id) fields.push(['事件 ID', event.event_id]);
  if (invocation.invocation_id) fields.push(['Invocation ID', invocation.invocation_id]);
  if (failure.type) fields.push(['故障类型', failure.type]);
  if (failure.retryable !== undefined) fields.push(['自动恢复', failure.retryable ? '记录允许定向恢复' : '记录要求人工检查']);
  if (artifact.artifact_id) fields.push(['Artifact ID', artifact.artifact_id]);
  return `<dl class="audit-detail-list">${fields.map(([label, value]) => `<dt>${escapeHTML(label)}</dt><dd>${escapeHTML(value)}</dd>`).join('')}</dl><p class="audit-human-note">阶段摘要来自 durable 记录和可排序事件窗口；没有被记录的字段不会由页面补齐。</p>`;
}

function showLedgerEntryAudit(index) {
  const item = window.__latestAuditEntries?.[Number(index)];
  if (!item) return;
  const label = ({event:'阶段事件', gate:'质量门', failure:'异常记录', artifact:'阶段产物'})[item.kind] || '审计记录';
  openAuditDialog(
    'CHRONOLOGICAL AUDIT LEDGER',
    `${label}详情`,
    '这里显示该条记录的可读核验字段；它不会把原始事件 JSON 直接堆到页面上。',
    ledgerEntryMarkup(item),
  );
}

function renderProtocolAudit(audit) {
  const externalRuns = asArray(audit.external_runs);
  const statusTransitions = asArray(audit.status_transitions);
  const interrupts = asArray(audit.interrupts);
  const snapshots = asArray(audit.message_snapshots);
  const lease = asObject(audit.execution_lease);
  const hasLease = Object.keys(lease).length > 0;
  const resumeReceipts = asArray(audit.resume_receipts).map(normalizeResumeReceipt).filter(item => item.idempotency_key);
  const worker = asArray(audit.worker).map(item => asObject(item));
  window.__latestProtocolAudit = {...audit, resume_receipts:resumeReceipts, worker};
  const windowMarkup = auditWindowNoticeMarkup(audit, 'protocol');
  if (!externalRuns.length && !statusTransitions.length && !interrupts.length && !snapshots.length && !hasLease && !resumeReceipts.length && !worker.length) {
    $('protocolRuntimeAudit').innerHTML = `${windowMarkup ? `<aside class="protocol-audit-window">${windowMarkup}</aside>` : ''}<div class="protocol-runtime-empty">该任务由浏览器自定义运行接口创建，没有经过外部 AG-UI 控制面；这不是缺失的智能体执行记录。</div>`;
    humanizeVisibleCopy($('protocolRuntimeAudit'));
    return;
  }
  const lineage = externalRuns.map((item,index)=>`<article class="protocol-record-card"><span>${String(index+1).padStart(2,'0')} · ${escapeHTML(item.kind || '外部生命周期')}</span><strong>${escapeHTML(item.run_id || 'run ID 未记录')}</strong><p>thread ${escapeHTML(item.thread_id || '未记录')}${item.declared_parent_run_id?`<br>parent ${escapeHTML(item.declared_parent_run_id)}`:''}</p><small>${escapeHTML(item.status || '状态未记录')} · request ${escapeHTML(item.request_hash || '未记录')} · ${escapeHTML(formatTimestamp(item.updated_at))}</small><button type="button" class="audit-link-button" data-protocol-record="external_runs" data-protocol-index="${index}">打开生命周期详情</button></article>`).join('');
  const transitionRows = statusTransitions.map((item,index)=>`<article class="protocol-record-card"><span>TRANSITION ${escapeHTML(item.transition_id ?? String(index + 1))}</span><strong>${escapeHTML(item.from_status || '初始')} → ${escapeHTML(item.to_status || '状态未记录')}</strong><p>run ${escapeHTML(item.run_id || '未记录')}</p><small>${escapeHTML(formatTimestamp(item.changed_at))}</small><button type="button" class="audit-link-button" data-protocol-record="status_transitions" data-protocol-index="${index}">打开状态转移详情</button></article>`).join('');
  const interruptRows = interrupts.map((item,index)=>`<details><summary><b>${escapeHTML(item.reason || '中断原因未记录')}</b><span>${escapeHTML(item.status || '状态未记录')}</span></summary><dl><dt>Interrupt ID</dt><dd>${escapeHTML(item.interrupt_id || '未记录')}</dd><dt>产生它的外部 run</dt><dd>${escapeHTML(item.protocol_run_id || '未记录')}</dd><dt>Thread</dt><dd>${escapeHTML(item.thread_id || '未记录')}</dd><dt>响应动作</dt><dd>${escapeHTML(item.response_schema?.properties?.action?.const||'历史记录未保存')}</dd><dt>Receipt</dt><dd>${escapeHTML(item.resume_receipt_id||'尚未消费')}</dd></dl><button type="button" class="audit-link-button" data-protocol-record="interrupts" data-protocol-index="${index}">打开中断详情</button></details>`).join('');
  const snapshotRows = snapshots.map((item,index)=>`<article class="protocol-record-card"><span>${escapeHTML(item.thread_id || 'Thread 未记录')}</span><strong>${escapeHTML(item.message_count ?? '未记录')} 条消息</strong><p>${asArray(item.roles).map((role,roleIndex)=>`${escapeHTML(role)} · ${escapeHTML(asArray(item.message_ids)[roleIndex]||'无 ID')}`).join('<br>') || '角色元数据未记录'}</p><small>仅展示角色和 ID，正文保持私有 · ${escapeHTML(formatTimestamp(item.updated_at))}</small><button type="button" class="audit-link-button" data-protocol-record="message_snapshots" data-protocol-index="${index}">打开线程元数据</button></article>`).join('');
  const leaseMarkup = hasLease ? `<article class="lease-card ${lease.active?'active':'expired'}"><span>${lease.active?'ACTIVE LEASE':'EXPIRED LEASE'}</span><strong>Fence ${escapeHTML(lease.fence ?? '未记录')}</strong><p>receipt ${escapeHTML(lease.receipt_id || '未记录')}<br>最近 heartbeat ${escapeHTML(lease.heartbeat_at_ms ? new Date(lease.heartbeat_at_ms).toLocaleString() : '未记录')}</p><small>到期 ${escapeHTML(lease.expires_at_ms ? new Date(lease.expires_at_ms).toLocaleString() : '未记录')}</small></article>` : '<div class="protocol-runtime-empty">当前没有执行 lease；终态正常释放 lease。</div>';
  const resumeRows = resumeReceipts.length
    ? resumeReceipts.map(receipt => resumeReceiptMarkup(receipt, worker, {compact:true})).join('')
    : '<div class="protocol-runtime-empty">没有恢复回执；普通首次运行不经过 resume worker。</div>';
  $('protocolRuntimeAudit').innerHTML = `${windowMarkup ? `<aside class="protocol-audit-window">${windowMarkup}</aside>` : ''}<section><header><span>外部运行链路</span><strong>${externalRuns.length} 个外部运行记录</strong></header><div class="protocol-lineage">${lineage||'<div class="protocol-runtime-empty">没有外部运行记录</div>'}</div></section><section><header><span>状态变化记录</span><strong>${statusTransitions.length} 条协议状态变化</strong></header><div class="protocol-lineage">${transitionRows||'<div class="protocol-runtime-empty">没有协议状态变化记录</div>'}</div></section><section><header><span>中断记录</span><strong>${interrupts.length} 条中断记录</strong></header><div class="protocol-interrupts">${interruptRows||'<div class="protocol-runtime-empty">没有中断记录</div>'}</div></section><section><header><span>私有消息快照</span><strong>${snapshots.reduce((sum,item)=>sum+(finiteValue(item.message_count)||0),0)} 条消息元数据</strong></header><div class="protocol-snapshots">${snapshotRows||'<div class="protocol-runtime-empty">没有 AG-UI 消息快照</div>'}</div></section><section><header><span>执行权记录</span><strong>数据库执行占用与执行权编号</strong></header>${leaseMarkup}</section><section class="protocol-resume-section"><header><span>恢复确认记录</span><strong>${resumeReceipts.length} 条恢复确认 · ${worker.length} 条执行器记录</strong></header><div class="protocol-resume-list">${resumeRows}</div></section><section class="protocol-worker-section"><header><span>执行器记录</span><strong>异常与生命周期记录</strong></header>${workerAuditCompactMarkup(worker)}</section><aside>${asArray(audit.limitations).map(item=>`<span>${escapeHTML(item)}</span>`).join('')}</aside>`;
  humanizeVisibleCopy($('protocolRuntimeAudit'));
  document.querySelectorAll('#protocolRuntimeAudit [data-protocol-load-more]').forEach(button => button.addEventListener('click', () => loadMoreProtocolRecords(button.dataset.protocolLoadMore, button)));
  document.querySelectorAll('#protocolRuntimeAudit [data-protocol-record]').forEach(button => button.addEventListener('click', () => showProtocolRecord(button.dataset.protocolRecord, button.dataset.protocolIndex)));
  document.querySelectorAll('#protocolRuntimeAudit [data-resume-open]').forEach(button => button.addEventListener('click', event => {
    event.preventDefault();
    event.stopPropagation();
    showResumeAudit(button.dataset.resumeOpen);
  }));
  bindResumeTransitionLinks($('protocolRuntimeAudit'));
}

async function loadProtocolAudit() {
  if (!runId) return;
  const audit = normalizeProtocolAudit(await getJSON(`/api/runs/${encodeURIComponent(runId)}/protocol-audit`));
  renderProtocolAudit(audit);
}

function markSystemContractFallback() {
  $('systemBoundary').dataset.contractSource = 'static-fallback';
  $('boundaryDescription').textContent = '无法读取后端运行时能力契约。以下内容仅为页面内置静态参考，不能作为当前 SDK 版本或协议能力已经验证的证据。';
  $('boundaryFreshness').innerHTML = '<div><span>OFFICIAL VERSION CHECK</span><strong>运行时核验记录不可用</strong><small>不得把页面静态版本当作当前安装事实</small></div>';
  document.querySelectorAll('#boundaryGrid article').forEach(article => {
    article.classList.add('unverified');
    const label = article.querySelector('span');
    if (label) label.textContent = '静态降级说明 · 当前未核验';
  });
}

function clearLiveWatchdog() {
  if (liveWatchdog !== null) {
    clearTimeout(liveWatchdog);
    liveWatchdog = null;
  }
}

function usagePulseInterval() {
  return document.visibilityState === 'hidden' ? 3200 : 800;
}

function clearUsagePulseTimer() {
  if (usagePulseTimer !== null) {
    clearTimeout(usagePulseTimer);
    usagePulseTimer = null;
  }
}

function stopUsagePulse() {
  usagePulseEnabled = false;
  usagePulseFailureCount = 0;
  clearUsagePulseTimer();
}

function scheduleUsagePulse(delay = usagePulseInterval()) {
  clearUsagePulseTimer();
  if (!usagePulseEnabled || !runId) return;
  usagePulseTimer = setTimeout(() => {
    usagePulseTimer = null;
    pollUsage();
  }, Math.max(250, Number(delay) || usagePulseInterval()));
}

function startUsagePulse() {
  if (!runId) return;
  usagePulseEnabled = true;
  clearUsagePulseTimer();
  if (!usagePulseInFlight) pollUsage();
}

function syncUsagePulse(status) {
  if (isSettledStatus(status)) {
    stopUsagePulse();
    return;
  }
  if (!usagePulseEnabled) {
    startUsagePulse();
  } else if (!usagePulseInFlight && usagePulseTimer === null) {
    scheduleUsagePulse();
  }
}

function schedulePoll(delay = 1100) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(() => {
    pollTimer = null;
    poll();
  }, delay);
}

function closeEventSource() {
  clearLiveWatchdog();
  const source = eventSource;
  eventSource = null;
  source?.close();
}

function latestSettledStatus() {
  const status = effectiveRunStatus({state: window.__latestState || {}});
  return isSettledStatus(status) ? status : null;
}

function fallbackToPolling(reason, delay = 500) {
  closeEventSource();
  if (latestSettledStatus()) return;
  connectionMode = `实时流${reason}，已回退轮询`;
  announceLive(`实时事件流${reason}，已回退到状态轮询。`, `stream-fallback:${reason}`);
  schedulePoll(delay);
}

function applyLiveUsageSnapshot(payload) {
  const incomingRunId = String(payload?.run_id || '');
  if (runId && incomingRunId && incomingRunId !== runId) return;
  const usage = asObject(payload?.usage || payload);
  if (!usageSnapshotPresent(usage)) return;
  const current = asObject(window.__latestUsageSnapshot);
  if (!usageSnapshotIsNewer(usage, current) && usageSnapshotPresent(current)) return;
  window.__latestUsageSnapshot = usage;
  window.__latestUsageRunId = incomingRunId || String(window.__latestState?.run_id || runId || '');

  const state = window.__latestState;
  if (!state || typeof state !== 'object') return;
  const stateRunId = String(state.run_id || '');
  if (incomingRunId && stateRunId && incomingRunId !== stateRunId) return;
  state.audit_usage = usage;
  state.audit_usage_recorded = true;
  if (window.__latestAudit && typeof window.__latestAudit === 'object') {
    window.__latestAudit.usage = usage;
    window.__latestAudit.usageRecorded = true;
  }
  const status = effectiveRunStatus({
    state,
    job: {status: payload?.status || window.__latestDurableStatus || state.status},
  });
  safelyRender('研究命令栏', () => renderCommandBar(state, status));
  safelyRender('研究指标', () => renderMetrics(state), 'metricsGrid');
}

async function pollUsage() {
  if (!runId || !usagePulseEnabled || usagePulseInFlight) return;
  usagePulseInFlight = true;
  try {
    const payload = await getJSON(`/api/runs/${encodeURIComponent(runId)}/usage`);
    usagePulseFailureCount = 0;
    applyLiveUsageSnapshot(payload);
    const status = String(payload?.status || '');
    if (isSettledStatus(status)) {
      stopUsagePulse();
    } else {
      scheduleUsagePulse();
    }
  } catch (_) {
    // The full state stream remains authoritative for run state.  A temporary
    // failure of this optional, lightweight cost view should not turn a valid
    // research run into a visible error state.
    usagePulseFailureCount += 1;
    const delay = Math.min(8000, usagePulseInterval() * (2 ** Math.min(usagePulseFailureCount, 3)));
    scheduleUsagePulse(delay);
  } finally {
    usagePulseInFlight = false;
  }
}

async function poll() {
  if (!runId) {
    renderStatus('failed', 'URL 中缺少运行编号');
    announceLive('运行失败：URL 中缺少运行编号','error:missing-run-id');
    return;
  }
  if (pollInFlight) return;
  pollInFlight = true;
  try {
    const data = await getJSON(`/api/runs/${encodeURIComponent(runId)}`);
    pollFailureCount = 0;
    render(data);
    const status = effectiveRunStatus(data);
    if (!isSettledStatus(status)) schedulePoll(1100);
  } catch (error) {
    pollFailureCount += 1;
    const httpStatus = Number(error?.status || 0);
    const permanent = httpStatus >= 400 && httpStatus < 500 && httpStatus !== 429;
    if (permanent) {
      closeEventSource();
      renderStatus('failed', httpStatus === 404 ? '研究记录不存在或已不可访问' : '研究记录暂时不可访问');
      announceLive('研究记录暂时不可访问，请检查运行链接或服务权限。', `poll-permanent:${httpStatus}`);
    } else if (!latestSettledStatus()) {
      const delay = Math.min(8000, 500 * (2 ** Math.min(pollFailureCount - 1, 4)));
      connectionMode = '状态接口暂时不可达，正在重试';
      renderStatus(window.__latestState?.status || 'queued', '状态接口暂时不可达，正在重试');
      announceLive(`状态接口暂时不可达，将在约 ${Math.ceil(delay / 1000)} 秒后重试。`, `poll-retry:${pollFailureCount}`);
      schedulePoll(delay);
    }
  } finally {
    pollInFlight = false;
  }
}

function startLiveUpdates() {
  startUsagePulse();
  if (!runId || !window.EventSource) {
    poll();
    return;
  }
  clearTimeout(pollTimer);
  pollTimer = null;
  closeEventSource();
  const source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/stream`);
  eventSource = source;
  let receivedSnapshot = false;
  const armWatchdog = () => {
    clearLiveWatchdog();
    liveWatchdog = setTimeout(() => {
      if (source === eventSource) fallbackToPolling('长时间没有收到有效快照');
    }, 12000);
  };
  const parsePayload = (event, fallback = '{}') => {
    try {
      return JSON.parse(event?.data || fallback);
    } catch (_) {
      throw new Error('实时事件流返回了无法解析的 JSON');
    }
  };
  armWatchdog();
  source.addEventListener('snapshot', event => {
    if (source !== eventSource) return;
    let data;
    try {
      data = parsePayload(event);
    } catch (_) {
      fallbackToPolling('返回了无法解析的数据');
      return;
    }
    receivedSnapshot = true;
    pollFailureCount = 0;
    armWatchdog();
    connectionMode = '实时事件流已连接';
    render(data);
    const status = effectiveRunStatus(data);
    if (isSettledStatus(status)) {
      stopUsagePulse();
      closeEventSource();
    }
  });
  source.addEventListener('usage', event => {
    if (source !== eventSource) return;
    let payload;
    try {
      payload = parsePayload(event);
    } catch (_) {
      return;
    }
    armWatchdog();
    applyLiveUsageSnapshot(payload);
  });
  source.addEventListener('done', () => {
    if (source !== eventSource) return;
    if (latestSettledStatus()) {
      stopUsagePulse();
      closeEventSource();
    } else fallbackToPolling('已结束但研究尚未终态');
  });
  source.addEventListener('rollover', event => {
    if (source !== eventSource) return;
    let payload;
    try {
      payload = parsePayload(event);
    } catch (_) {
      fallbackToPolling('轮换事件无法解析');
      return;
    }
    connectionMode = '实时事件流正常轮换，正在重新连接';
    closeEventSource();
    clearTimeout(pollTimer);
    pollTimer = setTimeout(startLiveUpdates, Number(payload.retry_after_ms || 500));
  });
  source.onerror = () => {
    if (source !== eventSource) return;
    fallbackToPolling(receivedSnapshot ? '连接中断' : '连接失败');
  };
}

function safelyRender(label, callback, fallbackId = '') {
  try {
    callback();
  } catch (error) {
    const fallback = fallbackId ? $(fallbackId) : null;
    if (fallback) fallback.innerHTML = `<div class="breakdown-empty">${escapeHTML(label)}的历史字段未记录或格式不可验证；其余证据与回答继续渲染。</div>`;
    console.error(`${label} render failed`, error);
  }
}

function render(data) {
  const rawState = asObject(data?.state);
  const audit = normalizeAudit(data, rawState);
  const state = mergeDurableAuditIntoState(rawState, audit);
  const events = asArray(data?.events);
  const eventWindow = eventWindowModel(data);
  const renderedRunId = String(state.run_id || data?.job?.run_id || runId || '');
  const cachedLiveUsage = window.__latestUsageRunId === renderedRunId
    ? asObject(window.__latestUsageSnapshot)
    : {};
  const newestUsage = freshestUsageSnapshot([state.audit_usage, cachedLiveUsage]);
  if (usageSnapshotPresent(newestUsage)) {
    // A full state projection can be older than the independently delivered
    // usage receipt. Always render the newest ledger revision, not whichever
    // network response happened to arrive last.
    state.audit_usage = newestUsage;
    state.audit_usage_recorded = true;
    audit.usage = newestUsage;
    audit.usageRecorded = true;
    window.__latestUsageSnapshot = newestUsage;
    window.__latestUsageRunId = renderedRunId;
  }
  window.__latestAudit = audit;
  window.__latestState = state;
  window.__latestEvents = events;
  window.__latestEventWindow = eventWindow;
  window.__latestDurableStatus = String(rawState.status || data?.job?.status || 'queued');
  window.__latestRecoveryAudit = resumeReceiptAudit(data, audit);
  renderAuditWindow(audit);
  renderEventWindowNotice(eventWindow);
  const status = effectiveRunStatus(data);
  syncUsagePulse(status);
  if (state.question) {
    $('runQuestion').textContent = state.question;
    document.title = `${state.question} · Fieldnote`;
  }
  renderProviderBadge(state);
  renderStatus(status, data.job?.error);
  prioritizeResultSections(status);
  setSectionNavCurrent(isSettledStatus(status) ? 'resultOverview' : 'researchPulse');
  safelyRender('研究命令栏',()=>renderCommandBar(state, status));
  safelyRender('运行摘要',()=>renderRunBrief(state, status));
  safelyRender('实时协作',()=>renderResearchPulse(state, status, events, audit),'researchPulse');
  safelyRender('交付前检查总览',()=>renderGateConsole(state),'gateMatrix');
  safelyRender('多模态输入与感知',()=>renderInputPerception(state, audit),'attachmentAuditGrid');
  safelyRender('角色阶段',()=>renderPhaseRail(asArray(state.agent_invocations), status));
  safelyRender('研究指标',()=>renderMetrics(state),'metricsGrid');
  safelyRender('结果摘要',()=>renderResultOverview(state, status),'overviewAnswer');
  safelyRender('评分分项',()=>renderBreakdown(state.closure || null, state.methodology || {}),'closureBreakdown');
  safelyRender('方法快照',()=>renderMethodologyMeta(asObject(state.methodology)),'methodologyMeta');
  safelyRender('逐目标检查记录',()=>renderSlotGateAudit(state.closure || null, asArray(state.contradiction_checks)),'slotGateAudit');
  safelyRender('智能体总控',()=>renderAgentCockpit(asArray(state.agent_invocations), events, status),'agentContract');
  safelyRender('协作关系',()=>renderCollaborationMap(state, events, status, audit),'collaborationMap');
  safelyRender('调用记录',()=>renderAgentExecution(asArray(state.agent_invocations)),'agentExecution');
  safelyRender('主审计时间线',()=>renderUnifiedAuditTimeline(state, events),'auditLedger');
  safelyRender('失败记录',()=>renderFailures(asArray(state.failures)),'failureList');
  safelyRender('研究计划',()=>renderSlots(asArray(state.plan?.slots)),'slots');
  safelyRender('检索路线',()=>renderQueries(asArray(state.queries)),'queryRoutes');
  safelyRender('阶段事件',()=>renderTimeline(events),'timeline');
  safelyRender('研究关系图',()=>renderResearchGraph(state),'graphAccessibleList');
  safelyRender('文章调研顺序',()=>renderSourceJourney(normalizeSources(state), asArray(state.queries)),'sourceJourney');
  safelyRender('证据账本',()=>renderEvidence(asArray(state.evidence)),'evidence');
  safelyRender('最终回答',()=>renderAnswer(state.draft_answer, state.verification, status, state.answer_delivery),'answer');
  safelyRender('页面信息层级',()=>renderResearchDisclosures(state, status));
  syncSectionNavVisibility();
  setSectionNavCurrent(isSettledStatus(status) ? 'resultOverview' : 'researchPulse');
  announceRuntimeSnapshot(state, status, data.job?.error || '');
  if (Date.now() - lastProtocolAuditAt > 5000) {
    lastProtocolAuditAt = Date.now();
    loadProtocolAudit().catch(error => {
      $('protocolRuntimeAudit').innerHTML = `<div class="protocol-runtime-empty">协议控制面审计暂不可用：${escapeHTML(error.message)}</div>`;
    });
  }
}

function scrollOptions(block = 'start') {
  return {behavior: reducedMotion.matches ? 'auto' : 'smooth', block};
}

function isSettledStatus(status) {
  return terminalStates.includes(status) || status === 'recovery_unverified';
}

function resumeStateTransition(state, receiptId) {
  const transition = asObject(state?.resume_transition);
  return normalizedId(transition.resume_receipt_id) === normalizedId(receiptId) ? transition : null;
}

function resumeReceiptAudit(data, audit = null) {
  const state = asObject(data?.state);
  const transition = asObject(state.resume_transition);
  const receiptId = normalizedId(transition.resume_receipt_id);
  if (!receiptId) {
    return {
      required: false,
      consistent: true,
      receipt: null,
      transition: null,
      stateStatus: String(state.status || data?.job?.status || 'queued'),
      status: 'not_required',
      reason: '本次运行没有 resume_transition；按普通运行状态解释。',
    };
  }
  const normalizedAudit = audit || (data?.audit ? normalizeAudit(data, state) : window.__latestAudit);
  const receipt = normalizedAudit?.resumeReceiptById?.get(receiptId) || null;
  const stateStatus = String(state.status || data?.job?.status || 'queued').toLowerCase();
  if (!receipt) {
    return {
      required: true,
      consistent: false,
      receipt: null,
      transition,
      stateStatus,
      status: 'missing',
      reason: `state.resume_transition 引用了 ${receiptId}，但 durable audit.resume_receipts 没有对应回执。`,
    };
  }
  const executionStatus = normalizedResumeExecutionStatus(receipt.execution_status);
  const durableRunStatus = String(receipt.durable_run_status || '').toLowerCase();
  const terminal = isSettledStatus(stateStatus);
  const durableOutcomeMatches = durableRunStatus === stateStatus;
  const consistent = terminal
    ? (
      (executionStatus === 'completed' && durableOutcomeMatches)
      || (executionStatus === 'failed' && durableOutcomeMatches)
    )
    : ['pending', 'running', 'startup_failed', 'failed'].includes(executionStatus);
  const transitionFence = finiteValue(transition.claim_fence);
  const receiptFence = finiteValue(receipt.claim_fence);
  const releasedClaim = receipt.execution_claimed === false
    && receiptFence === 0
    && ['completed', 'failed', 'startup_failed'].includes(executionStatus);
  const terminalFenceTransition = releasedClaim
    ? [...asArray(receipt.transitions)].reverse().find(item => (
      normalizedResumeExecutionStatus(item?.to_status) === executionStatus
      && finiteValue(item?.owner_fence) !== null
      && String(item?.transition_kind || 'execution') === 'execution'
    )) || null
    : null;
  const durableOwnerFence = releasedClaim
    ? finiteValue(terminalFenceTransition?.owner_fence)
    : receiptFence;
  const fenceConsistent = transitionFence === null
    || (durableOwnerFence !== null && transitionFence === durableOwnerFence);
  const fenceReason = releasedClaim && transitionFence !== null && durableOwnerFence === null
    ? `恢复回执已释放当前 claim，但 transition ledger 没有记录 ${executionStatus} 的 owner fence。`
    : releasedClaim && fenceConsistent
      ? `终态已正常把当前 claim fence 释放为 0；terminal transition 保留并匹配执行 fence ${durableOwnerFence}。`
      : fenceConsistent
        ? `resume_transition 与当前 durable claim fence ${durableOwnerFence ?? '未记录'} 一致。`
        : `resume_transition claim fence ${transitionFence} 与 durable owner fence ${durableOwnerFence ?? '未记录'} 不一致。`;
  const reason = !fenceConsistent
    ? fenceReason
    : !consistent
      ? `durable 状态 ${stateStatus} 与恢复回执状态 ${executionStatus} 不一致。`
      : `恢复回执 ${receiptId} 与 durable 状态一致。${fenceReason}`;
  return {
    required: true,
    consistent: consistent && fenceConsistent,
    receipt,
    transition,
    stateStatus,
    durableRunStatus,
    executionStatus,
    durableOwnerFence,
    terminalFenceTransition,
    status: consistent && fenceConsistent ? executionStatus : 'conflict',
    reason,
  };
}

function effectiveRunStatus(data){
  const durable=data?.state?.status;
  const base=data?.job?.status||durable||'queued';
  const recovery=resumeReceiptAudit(data);
  return recovery.required && !recovery.consistent ? 'recovery_unverified' : base;
}

function resumeReceiptStatusClass(receipt) {
  const status = normalizedResumeExecutionStatus(receipt?.execution_status);
  return ['pending', 'running', 'startup_failed', 'completed', 'failed', 'not_required'].includes(status)
    ? status
    : 'legacy_unverified';
}

function resumeTransitionTone(transition) {
  const status = String(transition?.to_status || '').toLowerCase().replace(/[-\s]+/g, '_');
  if (status === 'startup_failed' || status === 'failed') return 'failure';
  if (status === 'completed') return 'completed';
  if (status === 'running' && /stale|reclaim|takeover/i.test(String(transition?.reason || ''))) return 'takeover';
  if (status === 'handoff_emitted' || status === 'consumed') return 'handoff';
  return 'resume';
}

function resumeTransitionTitle(transition) {
  const status = String(transition?.to_status || '').toLowerCase().replace(/[-\s]+/g, '_');
  if (status === 'running' && /stale|reclaim|takeover/i.test(String(transition?.reason || ''))) {
    return '恢复回执 · stale fence 接管';
  }
  return `恢复回执 · ${resumeTransitionStatusLabel(status)}`;
}

function resumeWorkerRecords(receipt, worker = []) {
  const receiptId = normalizedId(receipt?.idempotency_key);
  return asArray(worker).filter(item => {
    const payload = asObject(item?.payload);
    return normalizedId(item?.receipt_id || item?.resume_receipt_id || payload.receipt_id || payload.resume_receipt_id) === receiptId;
  });
}

function workerAuditCompactMarkup(worker = []) {
  const rows = asArray(worker);
  if (!rows.length) return '<div class="protocol-runtime-empty">没有 worker audit；启动失败与异常退出不会被静态推断。</div>';
  return `<div class="protocol-worker-list">${rows.map(row => { const payload = asObject(row.payload); return `<article><span>${escapeHTML(row.event_type || 'worker 事件')}</span><strong>${escapeHTML(payload.error || payload.status || 'worker 生命周期记录')}</strong><p>receipt ${escapeHTML(payload.receipt_id || '非恢复 worker')} · fence ${escapeHTML(payload.fence ?? '未记录')}</p><small>${escapeHTML(formatTimestamp(row.created_at))} · ${escapeHTML(payload.exception_type || '无异常类型')}</small></article>`; }).join('')}</div>`;
}

function resumeReceiptMarkup(receipt, worker = [], {compact = false} = {}) {
  const item = normalizeResumeReceipt(receipt);
  const status = resumeReceiptStatusClass(item);
  const transitions = asArray(item.transitions);
  const workerRows = resumeWorkerRecords(item, worker);
  const transitionMarkup = transitions.length
    ? `<ol class="resume-transition-list">${transitions.map(transition => {
      const handoffLink = transition.handoff_message_id
        ? `<button type="button" class="audit-link-button inline" data-resume-handoff="${escapeHTML(transition.handoff_message_id)}">打开 handoff ${escapeHTML(transition.handoff_message_id)}</button>`
        : '';
      const invocationLink = transition.agent_invocation_id
        ? `<button type="button" class="audit-link-button inline" data-resume-invocation="${escapeHTML(transition.agent_invocation_id)}">打开 invocation ${escapeHTML(transition.agent_invocation_id)}</button>`
        : '';
      const superseded = transition.superseded_handoff_message_id
        ? `<small class="resume-transition-superseded">替换旧 handoff ${escapeHTML(transition.superseded_handoff_message_id)}</small>`
        : '';
      const binding = handoffLink || invocationLink
        ? `<div class="resume-transition-binding"><b>${escapeHTML(transition.agent_id || '角色未记录')} · ${escapeHTML(transition.operation || '操作未记录')}</b>${handoffLink}${invocationLink}${superseded}</div>`
        : '';
      return `<li class="${resumeTransitionTone(transition)}"><span>${escapeHTML(resumeTransitionTitle(transition))} · ${escapeHTML(transition.transition_kind === 'handoff' ? '交接事实' : '执行状态')}</span><strong>${escapeHTML(String(transition.from_status || '未记录'))} → ${escapeHTML(String(transition.to_status || '未记录'))}</strong><p>${escapeHTML(transition.reason || '原因未记录')}</p><small>${escapeHTML(formatTimestamp(transition.created_at))} · fence ${escapeHTML(transition.owner_fence ?? '未记录')} · owner ${escapeHTML(transition.owner_token_fingerprint || '未记录')}</small>${binding}</li>`;
    }).join('')}</ol>`
    : '<div class="resume-audit-empty">没有结构化 transition；旧回执不能证明 worker 如何接管。</div>';
  const workerMarkup = workerRows.length
    ? `<div class="resume-worker-list">${workerRows.map(row => { const payload = asObject(row.payload); return `<article><b>${escapeHTML(row.event_type || 'worker 事件')}</b><strong>${escapeHTML(payload.error || payload.status || 'worker 运行记录')}</strong><small>${escapeHTML(formatTimestamp(row.created_at))} · fence ${escapeHTML(payload.fence ?? '未记录')} · ${escapeHTML(payload.exception_type || '无异常类型')}</small></article>`; }).join('')}</div>`
    : '<div class="resume-audit-empty">没有匹配的 worker audit；不能把“已授权”当成“worker 已启动”。</div>';
  const body = `<div class="resume-audit-facts"><dl><dt>执行状态</dt><dd><span class="resume-status-pill ${status}">${escapeHTML(resumeExecutionStatusLabel(item.execution_status))}</span></dd><dt>恢复回执 ID</dt><dd class="audit-mono">${escapeHTML(item.idempotency_key || '未记录')}</dd><dt>来源 / 目标</dt><dd>${escapeHTML(item.source || '未记录')} · ${escapeHTML(item.protocol_run_id || item.thread_id || '协议上下文未记录')}</dd><dt>checkpoint</dt><dd>${escapeHTML(item.checkpoint_id_before ?? '未记录')} → ${escapeHTML(item.checkpoint_id_after ?? '未记录')}</dd><dt>claim</dt><dd>${item.execution_claimed ? '当前记录为已接管' : '当前记录为未持有 claim'} · fence ${escapeHTML(item.claim_fence ?? '未记录')}</dd><dt>owner fingerprint</dt><dd class="audit-mono">${escapeHTML(item.claim_owner_fingerprint || '未记录；不展示 owner token')}</dd><dt>worker 开始 / 结束</dt><dd>${escapeHTML(formatTimestamp(item.execution_started_at))} / ${escapeHTML(formatTimestamp(item.execution_completed_at))}</dd>${item.durable_run_status ? `<dt>durable 终态</dt><dd>${escapeHTML(item.durable_run_status)}</dd>` : ''}${item.execution_error ? `<dt>错误</dt><dd class="audit-error">${escapeHTML(item.execution_error)}</dd>` : ''}</dl></div><section class="resume-transitions"><header><span>RESUME TRANSITION LEDGER</span><strong>${transitions.length} 条状态转移</strong></header>${transitionMarkup}</section><section class="resume-worker-audit"><header><span>WORKER AUDIT</span><strong>${workerRows.length} 条匹配记录</strong></header>${workerMarkup}</section>`;
  if (compact) {
    return `<details class="resume-audit-card ${status}"><summary><span>${escapeHTML(item.idempotency_key || '恢复回执')}</span><strong>${escapeHTML(resumeExecutionStatusLabel(item.execution_status))}</strong><small>fence ${escapeHTML(item.claim_fence ?? '未记录')} · ${transitions.length} 次转移 · ${workerRows.length} 条 worker audit</small></summary><div>${body}<button type="button" class="audit-link-button" data-resume-open="${escapeHTML(item.idempotency_key)}">打开完整恢复审计</button></div></details>`;
  }
  return `<section class="resume-receipt-detail ${status}">${body}</section>`;
}

function prioritizeResultSections(status) {
  const overview = $('resultOverview');
  const answer = $('answerDisclosure');
  if (isSettledStatus(status)) {
    $('researchCommandBar').after(overview, answer);
    document.body.classList.add('result-first');
  } else {
    $('phaseDisclosure').after(overview, answer);
    document.body.classList.remove('result-first');
  }
}

function setRunDisclosure(id, status, {open = false, visible = true} = {}) {
  const disclosure = $(id);
  if (!disclosure) return;
  disclosure.classList.toggle('hidden', !visible);
  const renderState = `${status}:${visible ? 'visible' : 'hidden'}`;
  if (disclosure.dataset.renderState !== renderState) {
    disclosure.open = Boolean(open && visible);
    disclosure.dataset.renderState = renderState;
  }
}

function setDisclosureText(id, value) {
  const node = $(id);
  if (node) node.textContent = value;
}

function renderResearchDisclosures(state, status) {
  const settled = isSettledStatus(status);
  const needsAttention = ['failed', 'verification_failed', 'evidence_incomplete', 'recovery_unverified'].includes(status);
  const invocations = asArray(state.agent_invocations);
  const evidence = asArray(state.evidence);
  const sources = normalizeSources(state);
  const attachments = asArray(state.input_attachments);
  const gates = gateConsoleModel(state);
  const passedGates = gates.filter(item => item.tone === 'passed').length;
  const required = requiredSlotProgressModel(state);
  const verification = verificationModel(state.verification);
  const delivery = asObject(state.answer_delivery);
  const hasAnswer = Boolean(String(state.draft_answer || '').trim());
  const completedRoles = agentOrder.filter(agent => invocations.some(item => item.agent_id === agent && item.status === 'succeeded')).length;
  const localCitationBinding = delivery.mode === 'local_citation_binding';

  setRunDisclosure('operationDisclosure', status, {open: !settled || needsAttention});
  setDisclosureText('operationDisclosureSummary', settled
    ? `${invocations.length} 条真实执行记录已保存`
    : invocations.length ? `正在执行第 ${invocations.length} 条角色记录` : '正在等待第一条真实执行记录');
  setDisclosureText('operationDisclosureDetail', settled
    ? '展开回看每次角色执行、输入、输出与交接依据'
    : '展开查看当前角色、交接与六角色状态');

  setRunDisclosure('gateDisclosure', status, {open: !settled || needsAttention});
  setDisclosureText('gateDisclosureSummary', gates.every(item => item.tone === 'waiting')
    ? '回答前的五项检查等待开始'
    : `${passedGates}/5 项交付检查已确认`);
  setDisclosureText('gateDisclosureDetail', passedGates === gates.length
    ? '每个必答研究点均保留了材料、原文位置和冲突检查记录'
    : '展开查看每一项为什么通过或需要补材料');

  setRunDisclosure('inputDisclosure', status, {open: !settled && attachments.length > 0, visible: attachments.length > 0 || !settled});
  setDisclosureText('inputDisclosureSummary', attachments.length
    ? `${attachments.length} 个附件进入研究链路`
    : '本次没有附件，问题直接进入规划角色');
  setDisclosureText('inputDisclosureDetail', attachments.length
    ? '展开查看附件、定位观察和阅读记录'
    : '无需执行图片、文档或音频读取步骤');

  setRunDisclosure('phaseDisclosure', status, {open: !settled || needsAttention});
  setDisclosureText('phaseDisclosureSummary', settled
    ? localCitationBinding
      ? `${completedRoles}/6 个角色有成功调用；引用材料已完成本地绑定`
      : `${completedRoles}/6 个角色留下完成记录`
    : completedRoles ? `${completedRoles}/6 个角色已完成当前阶段` : '等待六个角色开始协作');
  setDisclosureText('phaseDisclosureDetail', localCitationBinding
    ? '外部语义核验没有返回结果；展开可查看成功调用、本地绑定和待重试的检查'
    : '展开查看每个角色实际留下的调用和产物');

  setRunDisclosure('answerDisclosure', status, {open: !settled && hasAnswer, visible: hasAnswer && status !== 'recovery_unverified'});
  setDisclosureText('answerDisclosureSummary', !hasAnswer
    ? '完成研究后可逐句回看引用'
    : localCitationBinding
      ? `${verification.rows.length}/${verification.rows.length} 句引用编号可回到已保存材料`
      : verification.rows.length ? `${verification.entailed}/${verification.rows.length} 句已完成引用检查` : '完整回答已生成');
  setDisclosureText('answerDisclosureDetail', localCitationBinding
    ? '自动逐句语义核对服务没有返回结果，系统未把该步骤标为通过'
    : '结论已在上方展示；展开后查看全文、每句话和对应材料');

  setRunDisclosure('auditDisclosure', status, {open: needsAttention && !hasAnswer});
  setDisclosureText('auditDisclosureSummary', `${invocations.length} 条执行记录 · ${evidence.length} 条证据 · ${sources.length} 条来源记录`);
  setDisclosureText('auditDisclosureDetail', '展开查看时间线、计算方式、异常和恢复记录');

  setRunDisclosure('researchArchiveDisclosure', status, {open: false});
  setDisclosureText('archiveDisclosureSummary', `${sources.length} 条来源记录、${evidence.length} 条证据和完整问题路径`);
  setDisclosureText('archiveDisclosureDetail', '展开追踪问题、文章、原文和最终回答之间的连接');

  const collaboration = $('collaborationDetailDisclosure');
  if (collaboration) {
    if (collaboration.dataset.renderState !== status) {
      collaboration.open = !settled || needsAttention;
      collaboration.dataset.renderState = status;
    }
    setDisclosureText('collaborationDisclosureSummary', `${completedRoles}/6 个角色完成 · ${invocations.length} 条执行记录可查`);
    setDisclosureText('collaborationDisclosureDetail', '总控图保留概览；展开逐项检查交接、阶段产物和调用记录');
  }

  if (required.requiredRecorded && required.required > 0) {
    $('runDisclosureGrid')?.setAttribute('data-required-progress', `${required.passed}/${required.required}`);
  }
}

function renderCommandBar(state, status) {
  const invocations = asArray(state.agent_invocations);
  const latest = [...invocations].reverse().find(item => item.status === 'running') || invocations.at(-1);
  const agent = latest?.agent_id || 'planner';
  const contract = agentContracts[agent] || agentContracts.planner;
  const sourceModel = sourceGroupModel(state);
  const sources = sourceModel.groups;
  const verifiedOrigins = new Set(sourceModel.verifiedGroups || []);
  const verification = state.verification || null;
  const verificationState = verificationModel(verification);
  const gates = gateConsoleModel(state);
  const passedGates = gates.filter(item => item.tone === 'passed').length;
  const unresolvedGates = gates.filter(item => item.tone === 'blocked').length;
  const unverifiableGates = gates.filter(item => item.tone === 'unverifiable').length;
  const hardGatesPassed = gates.length === gateDefinitions.length && gates.every(item => item.tone === 'passed');

  const completedRoles=agentOrder.filter(id=>invocations.some(item=>item.agent_id===id&&item.status==='succeeded')).length;
  $('commandAgent').textContent = status === 'completed'
    ? hardGatesPassed && verificationState.passed === true
      ? '全流程完成：交付前检查和逐句引用检查均通过'
      : '研究流程已完成，审计记录可继续查验'
    : isSettledStatus(status)
      ? '本轮协作已结束，运行现场与审计记录已保留'
      : latest ? contract.name : '等待规划智能体';
  $('commandOperation').textContent = isSettledStatus(status)
    ? `${completedRoles}/6 个角色留下完成记录 · 最终归档：${contract.name}`
    : latest ? `${operationName(latest.operation)} · ${invocationStatus(latest.status)}` : '尚无真实角色执行记录';
  document.querySelector('.command-now small').textContent=isSettledStatus(status)?'协作结果':'此刻执行';
  $('commandGate').textContent = gates.every(item => item.tone === 'waiting') ? '—/5' : `${passedGates}/5`;
  $('commandGateState').textContent = hardGatesPassed
    ? '全部通过，允许写作'
    : unresolvedGates ? `${unresolvedGates} 项需要补材料` : unverifiableGates ? `${unverifiableGates} 项历史记录不足` : '等待逐目标检查';
  $('commandSources').textContent = sourceModel.available ? String(sources.size) : '未记录';
  $('commandSourceState').textContent = !sourceModel.available
    ? '已确认来源集合未记录 · 不可计算'
    : sources.size ? (verifiedOrigins.size===sources.size?`${sources.size} 组来源关系已确认`:`${sources.size-verifiedOrigins.size} 组仍只能按域名或页面信息初步分组`) : '已记录 0 组来源 · 不可计算';
  $('commandClaims').textContent = verificationState.itemsRecorded && verificationState.rows.length
    ? `${verificationState.entailed}/${verificationState.rows.length}`
    : verificationState.present ? '不可计算' : '未记录';
  $('commandClaimState').textContent = !verificationState.present
    ? '等待回答'
    : !verificationState.itemsRecorded
      ? '逐句检查记录未完整保存 · 不可计算'
      : !verificationState.rows.length
        ? '已记录 0 个句单元 · 不可计算'
        : verificationState.passed === true ? citationContractPassLabel : verificationState.passed === false ? '存在待补声明' : '最终判定未记录 · 不可验证';
  const usage=usageLedgerModel(state);
  const modelCalls=usage.modelCalls;
  const estimatedCost=usage.estimatedCost;
  const knownCost=usage.knownCostLowerBound;
  $('commandCalls').textContent = modelCalls === null ? unmeasuredUsageLabel(usage.usageStatus) : String(modelCalls);
  $('commandCost').textContent = estimatedCost === null
    ? knownCost !== null
      ? `至少 ${formatKnownCost(knownCost)}`
      : pricingStatusName(usage.pricingStatus)
    : formatEstimatedCost(estimatedCost);
  $('commandCalls').title = usage.durableAvailable
    ? `来自已保存的用量账本，${usage.ledgerEntries===null?'账目数未记录':`共 ${usage.ledgerEntries} 条账目`}；${usageStatusName(usage.usageStatus)}${usage.usageReason?`；${usage.usageReason}`:''}；${pricingStatusName(usage.pricingStatus)}${usage.pricingReason?`；${usage.pricingReason}`:''}${usage.updatedAt?`；最后落账 ${formatTimestamp(usage.updatedAt)}`:''}`
    : usage.source === 'state_counters_fallback' ? '完整用量账本不可用，暂显示公开状态计数器' : '没有可用的开销记录';
  const pendingUsageText = usage.pendingModelOperations
    ? `当前还有 ${usage.pendingModelOperations} 个模型请求未返回用量。`
    : '';
  const latestUsageText = usageSnapshotPresent(usage.latestEntry)
    ? usageEntryLabel(usage.latestEntry, {latest:true})
    : '';
  const settledProcessingText = usage.settledModelResponses
    ? `已确认 ${usage.settledModelResponses} 次模型接口响应的用量${usage.settledModelOperations ? `，涉及 ${usage.settledModelOperations} 个仍在保存结果的步骤` : ''}。`
    : usage.settledModelOperations
      ? `${usage.settledModelOperations} 个模型请求已返回用量，正在保存本步骤结果。`
    : '';
  $('commandCost').title = pendingUsageText || settledProcessingText
    ? `费用会在模型接口返回用量后立即入账。${usage.costIsLowerBound ? '当前金额仅包含已能计价的部分；“+”表示还有未细分计价的输入。' : ''}${[pendingUsageText, settledProcessingText, latestUsageText].filter(Boolean).join(' ')}`
    : usage.updatedAt
      ? `${usage.costIsLowerBound ? '当前金额是已知费用；“+”表示还有未细分计价的输入。' : '已记录的累计费用。'}最后一次用量入账：${formatTimestamp(usage.updatedAt)}。${latestUsageText}`
      : '尚未记录模型请求的 durable 用量。';
  $('researchCommandBar').dataset.status = status;
  $('researchCommandBar').dataset.activeAgent = agent;
  $('researchCommandBar').dataset.usageUpdatedAt = usage.updatedAt || '';
}

function gateConsoleModel(state) {
  const rows = slotAuditRows(state, false);
  return gateDefinitions.map(definition => {
    if (!rows.length) {
      return {key:definition.key, label:definition.label, tone:'waiting', value:'等待', explanation:definition.explanation, targetSlotId:null, unavailable:0};
    }
    const values = rows.map(row => slotGateValue(row, definition, state));
    const passed = values.filter(value => value === true).length;
    const failed = values.filter(value => value === false).length;
    const unavailable = values.filter(value => value === null).length;
    const target = rows.find((row, index) => values[index] !== true)?.slotId || rows[0].slotId;
    if (failed) {
      const failedTarget=rows.find((row,index)=>values[index]===false)?.slotId||target;
      return {
        key:definition.key,
        label:definition.label,
        tone:'blocked',
        value:`${passed}/${rows.length}`,
        explanation:`${definition.explanation}；${failed} 个必答问题还需要补材料${unavailable ? `，另有 ${unavailable} 个没有完整检查记录` : ''}`,
        targetSlotId:failedTarget,
        unavailable
      };
    }
    if (unavailable) {
      return {
        key:definition.key,
        label:definition.label,
        tone:'unverifiable',
        value:`${passed}/${rows.length}`,
        explanation:`${definition.explanation}；${unavailable} 个必答问题没有完整检查记录，当前无法判断`,
        targetSlotId:target,
        unavailable
      };
    }
    return {
      key:definition.key,
      label:definition.label,
      tone:passed === rows.length ? 'passed' : 'blocked',
      value:`${passed}/${rows.length}`,
      explanation:definition.explanation,
      targetSlotId:target,
      unavailable:0
    };
  });
}

function renderGateConsole(state) {
  const gates = gateConsoleModel(state);
  const passed = gates.filter(item => item.tone === 'passed').length;
  const blocked = gates.filter(item => item.tone === 'blocked').length;
  const unverifiable = gates.filter(item => item.tone === 'unverifiable').length;
  const hasUnaudited = gates.some(item => item.unavailable > 0);
  const closure = asObject(state.closure);
  const gaps = asArray(closure.gaps);
  const gap = gaps[0];
  const hardGatesPassed = gates.length === gateDefinitions.length && gates.every(item => item.tone === 'passed');

  $('gateMatrix').innerHTML = gates.map((item, index) => `<button type="button" class="${item.tone}" data-gate-key="${escapeHTML(item.key)}"${item.targetSlotId ? ` data-gate-slot-id="${escapeHTML(item.targetSlotId)}"` : ''} aria-label="${escapeHTML(item.label)}：${escapeHTML(item.value)}。${escapeHTML(item.explanation)}"><span>${String(index + 1).padStart(2, '0')}</span><small>${escapeHTML(item.label)}</small><strong>${escapeHTML(item.value)}</strong><em>${escapeHTML(item.explanation)}</em><i>${item.tone === 'passed' ? '已确认' : item.tone === 'blocked' ? '需要补材料' : item.tone === 'unverifiable' ? '记录不足' : '等待记录'}</i></button>`).join('');
  $('gateConsoleSummary').textContent = hardGatesPassed
    ? '5/5 项交付前检查已确认，可以进入带引用写作'
    : `${passed}/5 已确认 · ${blocked} 项需要补材料${unverifiable ? ` · ${unverifiable} 项记录不足` : ''}${hasUnaudited && !unverifiable ? ' · 存在必答问题没有完整检查记录' : ''}`;
  $('gateDecision').className = `gate-decision ${hardGatesPassed ? 'passed' : blocked ? 'blocked' : 'waiting'}`;
  $('gateDecision').querySelector('strong').textContent = hardGatesPassed
    ? '必答问题、来源互证、原文位置、反面材料和冲突说明均已确认，可以进入写作和逐句引用核验。'
      : gap
        ? `当前不能停止研究：${gapName(gap.type)}。${auditTextPreview(gap.description || `优先寻找${sourcePreferenceName(gap.preferred_source)}。`, 240)}`
        : unverifiable
          ? '历史检查记录未完整保存，系统不能把缺失记录当作已确认。'
          : '尚未形成逐目标检查记录，系统不能只凭加权分数结束研究。';
  document.querySelectorAll('#gateMatrix [data-gate-key]').forEach(button => button.addEventListener('click', () => {
    if (!scrollToSlotAudit(button.dataset.gateSlotId, `${button.querySelector('small')?.textContent || deliveryCheckLabel}记录`)) {
      scrollToResearchTarget('methodSection', `${button.querySelector('small')?.textContent || deliveryCheckLabel}记录`);
    }
  }));
}

function renderRunBrief(state, status) {
  const invocations = asArray(state.agent_invocations);
  const latest = invocations.at(-1);
  const slots = asArray(state.plan?.slots);
  const progress = requiredSlotProgressModel(state);
  const requiredRows = progress.rows;
  const slotById = new Map(slots.map(slot => [String(slot?.id || ''), slot]));
  const known = requiredRows.filter(row => row.passed === true).map(row => slotById.get(row.slotId) || row);
  const gap = asArray(asObject(state.closure).gaps)[0];
  const gates=gateConsoleModel(state);
  const hardGatePassed=gates.length===gateDefinitions.length&&gates.every(item=>item.tone==='passed');
  const hardGateUnverifiable=gates.some(item=>item.tone==='unverifiable');
  const stageNames = {queued:'排队等待执行',initialized:'初始化研究档案',perceiving:'读取多模态附件',planning:'拆解回答目标',running:'执行证据研究',drafting:'组织引用回答',completed:'回答与引用已验收',verification_failed:'引用检查未通过',evidence_incomplete:'已生成当前回答，仍待补齐核验',failed:'运行已保存现场',cancelled:'任务已安全停止'};
  $('briefStage').textContent = stageNames[status] || operationName(latest?.operation || status);
  $('briefStageDetail').textContent = latest ? `${agentContracts[latest.agent_id]?.name || latest.role}正在/最近执行“${operationName(latest.operation)}”，后端记录状态为${invocationStatus(latest.status)}。` : '尚无真实角色执行记录，页面不会把设计职责当成本次已经发生的事实。';
  const knownLabel = !progress.requiredRecorded
    ? '必需目标分母未记录 · 不可计算'
    : !progress.passedRecorded
      ? `${progress.knownPassed} 个已知通过 / ${progress.required} 个必需目标 · ${progress.unknownRows || '通过数'}未记录`
      : progress.required
        ? `${progress.passed}/${progress.required} 个必答问题完成检查`
        : '已记录 0 个必需目标 · 无可计算比例';
  $('briefKnown').textContent = `${knownLabel}${slots.some(slot => slot?.required === false) ? ` · ${slots.filter(slot => slot?.required === false).length} 个可选目标不计入分母` : ''}`;
  $('briefKnownDetail').textContent = known.length
    ? known.map(slot => slot.value || slot.description || slot.slotId || '历史字段未记录').join('；')
    : !progress.requiredRecorded ? '没有已记录的必答问题数，就不能计算完成度；规划智能体完成后才开始计数。' : progress.unknownRows ? `${progress.unknownRows} 个必答问题缺少检查结果，不能按未通过处理。` : '当前还没有完成交付前检查的回答目标。';
  $('briefGap').textContent = gap ? gapName(gap.type) : hardGatePassed ? '交付前检查全部完成' : hardGateUnverifiable ? '历史检查记录不足' : '等待完整性审查';
  $('briefGapDetail').textContent = gap ? `${auditTextPreview(gap.description, 180)}；优先寻找：${sourcePreferenceName(gap.preferred_source)}。` : hardGatePassed ? '必答问题、来源互证、原文位置、反面材料和冲突说明均已确认。' : hardGateUnverifiable ? '至少一个必答问题缺少审计或历史检查字段，不能视为已确认。' : '完整性审查角色尚未给出可执行的材料缺口。';
  $('briefNext').textContent = nextAction(status, latest, gap);
  const limits=asObject(state.budget_limits),counters=asObject(state.counters);
  const hasBudgetRecord=['iterations','search_calls','pages'].some(key=>finiteValue(limits[key])!==null);
  const budgetSummary=hasBudgetRecord
    ? ` · 预算：轮次 ${missingValueLabel(counters.iterations)}/${missingValueLabel(limits.iterations)}，搜索 ${missingValueLabel(counters.search_calls)}/${missingValueLabel(limits.search_calls)}，页面 ${missingValueLabel(counters.pages_selected)}/${missingValueLabel(limits.pages)}`
    : ' · 预算字段未记录';
  $('briefConnection').textContent = `${connectionMode}${budgetSummary} · 页面更新于 ${new Date().toLocaleTimeString()}`;
  $('runBrief').setAttribute('data-status', status);
}

function renderResearchPulse(state, status, events = [], audit = window.__latestAudit || null) {
  const invocations = asArray(state.agent_invocations);
  events=asArray(events);
  const latest = [...invocations].reverse().find(item => item.status === 'running') || invocations.at(-1);
  const latestIndex = latest ? invocations.lastIndexOf(latest) : -1;
  const previous = latestIndex > 0 ? invocations[latestIndex - 1] : null;
  const contract = latest ? agentContracts[latest.agent_id] : agentContracts.planner;
  const isPerception = latest?.agent_id === 'perception';
  const isOrchestrator = latest?.agent_id === 'orchestrator';
  const order = Math.max(0, agentOrder.indexOf(latest?.agent_id || 'planner'));
  const handoffRecords = auditHandoffRecords(events, audit);
  const handoffs = handoffRecords.length;
  const artifacts = audit?.available
    ? asArray(audit.artifacts).length
    : invocations.reduce((sum, item) => sum + asArray(item?.output_artifact_ids).length, 0);
  const usage = usageLedgerModel(state);
  const gates = asArray(latest?.quality_gate_statuses);
  const gatePassed = gates.length > 0 && gates.every(value => String(value).toLowerCase() === 'passed');
  const gateFailed = gates.some(value => ['failed', 'blocked'].includes(String(value).toLowerCase()));
  const nextAgent = isPerception ? 'planner' : !isOrchestrator && latest && order < agentOrder.length - 1 ? agentOrder[order + 1] : null;
  const handoffIds = asArray(latest?.handoff_message_ids);
  const handoffRecord = [...handoffRecords].reverse().find(record => handoffIds.includes(record.id));
  const handoffEvent = handoffRecord?.event || null;
  const handoffEnvelope = handoffRecord?.envelope || null;
  const handoffConsumer = handoffRouteTarget(handoffEnvelope);
  const handoffAssessment = handoffEnvelope
    ? handoffReceiptAssessment(handoffEnvelope, events, invocations, audit)
    : null;

  const localCitationBinding=asObject(state.answer_delivery).mode==='local_citation_binding';
  const completed = status === 'completed';
  document.querySelector('.pulse-header>div:first-child>strong').textContent = completed ? '这轮协作已经完成什么' : '这轮协作正在发生什么';
  $('pulseAgentName').previousElementSibling.textContent = completed ? '最终归档者' : '当前执行者';
  $('pulseOperation').previousElementSibling.textContent = completed ? '完成了什么' : '正在做什么';
  $('pulseDecision').previousElementSibling.textContent = completed ? '为什么允许交付' : '为什么继续或停下';
  $('pulseAgentIndex').textContent = isPerception ? 'IN' : isOrchestrator ? '总' : String(order + 1).padStart(2, '0');
  $('pulseAgentName').textContent = contract?.name || '研究总控';
  $('pulseAgentState').textContent = latest
    ? `${invocationStatus(latest.status)} · 第 ${latest.attempt ?? '未记录'} 次尝试 · ${invocationDuration(latest)}`
    : '尚无后端角色执行记录，等待真实记录';
  $('pulseInput').textContent = runtimeSummary(latest?.input_summary, latest?.operation, 'input') || contract?.input || '等待用户问题进入规划阶段';
  $('pulseOperation').textContent = latest ? operationName(latest.operation) : '建立研究任务';
  $('pulseOutput').textContent = runtimeSummary(latest?.output_summary, latest?.operation, 'output') || (latest?.status === 'running' ? '执行中，尚未形成阶段产物' : contract?.output || '等待结构化产物');
  const eventWindowNote = window.__latestEventWindow?.incomplete
    ? window.__latestEventWindow.total===null
      ? ` · 阶段事件仅显示最近 ${window.__latestEventWindow.returned ?? '未记录'} 条，已保存总数未记录`
      : ` · 阶段事件仅显示最近 ${window.__latestEventWindow.returned ?? '未记录'} / ${window.__latestEventWindow.total} 条`
    : '';
  const modelCallProof=usage.modelCalls===null?`<b>${escapeHTML(unmeasuredUsageLabel(usage.usageStatus))}</b> 次模型服务调用（未完整计量）`:`<b>${usage.modelCalls}</b> 次模型服务调用`;
  $('pulseProof').innerHTML = `<b>${invocations.length||'未记录'}</b> 条角色执行记录 · ${modelCallProof} · <b>${handoffs||'未记录'}</b> 条任务交接 · <b>${artifacts||'未记录'}</b> 个产物${escapeHTML(eventWindowNote)}`;
  $('pulseProof').title = usage.durableAvailable
    ? `模型请求来自已保存的开销账本（${usage.ledgerEntries===null?'账目数未记录':`${usage.ledgerEntries} 条账目`}）；${usageStatusName(usage.usageStatus)}${usage.usageReason?`；${usage.usageReason}`:''}`
    : '完整开销账本不可用时，页面显示公开状态计数器的回退值';

  let decision = '等待阶段检查产生可检查结果';
  if (isSettledStatus(status)) {
    decision = status === 'completed'
      ? localCitationBinding
        ? '回答已交付；本地已检查每句引用编号对应通过材料，语义模型核验因服务超时未返回。'
        : `运行终态记录允许交付；${citationContractPassLabel}`
      : '运行已停在可恢复检查点，现有调用、交接与证据均保留供人工复核。';
  } else if (latest?.status === 'failed' || latest?.status === 'cancelled') {
    decision = '当前调用没有成功完成，不会把不完整产物交给下一角色。';
  } else if (gateFailed) {
    decision = '阶段产物已保存，但阶段检查未通过；系统会先补材料，不会把不完整结果包装成答案。';
  } else if (gatePassed) {
    decision = handoffConsumer
      ? `${contract.name}的阶段检查已通过；${handoffConsumerLabel(handoffConsumer, handoffEnvelope, handoffEvent, events, invocations, audit)}。`
      : nextAgent
        ? `${contract.name}的阶段检查已通过；按职责链下一步可能由${agentContracts[nextAgent].name}处理，但尚未记录其实际接收。`
        : `${contract.name}的阶段检查已通过；尚待研究总控写入最终状态。`;
  } else if (latest?.status === 'running') {
    decision = '调用仍在执行，页面只显示已持久化输入，不提前宣称已经完成。';
  }
  $('pulseDecision').textContent = decision;
  $('pulseAuditButton').disabled = !latest;
  $('pulseAuditButton').textContent = latest ? completed ? '查看最终归档记录' : '查看本次执行记录' : '等待调用记录';
  window.__pulseInvocation = latest || null;

  const stageNames = {planner:'规划',scout:'检索',curator:'整理',critic:'审查',writer:'撰写',verifier:'核验'};
  $('pulseStageMap').innerHTML = agentOrder.map((agent, index) => {
    const runtime = agentRuntimeEvidence(agent, invocations, events);
    const calls = runtime.calls;
    const phase = runtime.status;
    const artifactCount=runtime.artifactIds.length>0?runtime.artifactIds.length:recordedArrayCount(calls,'output_artifact_ids');
    const detail = runtime.observed ? `${countText(calls.length||null,' 次调用')} · ${phaseStateName(phase)} · ${countText(runtime.events.length||null,' 个事件')} · ${countText(artifactCount,' 个产物')}` : '尚未调用、事件或产物记录';
    return `<button type="button" class="${phase}" data-pulse-agent="${agent}" aria-label="${escapeHTML(agentContracts[agent].name)}，${escapeHTML(detail)}"><span>${String(index + 1).padStart(2, '0')}</span><b>${stageNames[agent]}</b><small>${escapeHTML(detail)}</small></button>${index < agentOrder.length - 1 ? '<i aria-hidden="true">→</i>' : ''}`;
  }).join('');
  const runtimeByAgent = agentOrder.map(agent => agentRuntimeEvidence(agent, invocations, events));
  const completedRoles = runtimeByAgent.filter(item => item.status === 'done').length;
  const activeRoles = runtimeByAgent.filter(item => item.status === 'running').length;
  const blockedRoles = runtimeByAgent.filter(item => item.status === 'blocked').length;
  $('pulseStageSummary').textContent = `${completedRoles}/6 完成${activeRoles?` · ${activeRoles} 执行中`:''}${blockedRoles?` · ${blockedRoles} 需要补材料`:''}`;
  document.querySelectorAll('[data-pulse-agent]').forEach(button => button.addEventListener('click', () => showAgentAudit(button.dataset.pulseAgent, invocations, window.__latestEvents || [], audit)));

  const transition = previous && latest && previous.agent_id !== latest.agent_id
    ? `${agentContracts[previous.agent_id]?.name || previous.agent_id} → ${contract?.name || latest.agent_id}`
    : latest ? `${contract?.name || latest.agent_id}正在持有当前任务` : '尚未发生角色交接';
  $('researchPulse').dataset.status = status;
  $('researchPulse').dataset.transition = transition;
}

function attachmentContentURL(attachment) {
  const value = String(asObject(attachment).content_url || '');
  return /^\/api\/runs\/[A-Za-z0-9_-]+\/attachments\/I[a-fA-F0-9]{64}$/.test(value) ? value : '';
}

function attachmentGroundingModel(observation) {
  const rows = asArray(asObject(observation).observations).map(item => {
    const value = asObject(item);
    const confidence = finiteValue(value.confidence);
    const locator = String(value.locator || '').trim();
    return {
      ...value,
      confidence,
      locator,
      eligible:Boolean(locator && confidence !== null && confidence >= 0.8),
    };
  });
  return {
    rows,
    total:rows.length,
    eligible:rows.filter(item => item.eligible).length,
    located:rows.filter(item => item.locator).length,
  };
}

function perceptionInvocationForAttachment(attachment, attachmentIndex, attachments, invocations) {
  const id = String(asObject(attachment).id || '');
  const calls = asArray(invocations).filter(item => item.agent_id === 'perception' || item.operation === 'perceive_inputs');
  const exact = [...calls].reverse().find(item => String(item.input_summary || '').includes(id));
  if (exact) return exact;
  const nonReplay = calls.filter(item => item.execution_mode !== 'replayed');
  return nonReplay.length === attachments.length ? nonReplay[attachmentIndex] || null : null;
}

function attachmentModalityName(value) {
  return ({image:'图像',audio:'音频',document:'文档',text:'文本'})[String(value || '')] || '附件';
}

function attachmentPreviewMarkup(attachment, observation, expanded = false) {
  const value = asObject(attachment);
  const url = attachmentContentURL(value);
  const modality = String(value.modality || 'document');
  const mediaType = String(value.media_type || '');
  const name = String(value.name || '未命名附件');
  if (!expanded && modality === 'image' && url) {
    return `<img src="${escapeHTML(url)}" alt="${escapeHTML(name)} 的原始图像预览" loading="lazy">`;
  }
  if (expanded && modality === 'audio' && url) {
    return `<audio controls preload="metadata" src="${escapeHTML(url)}">浏览器无法播放该音频，可使用下方原始文件链接。</audio>`;
  }
  if (expanded && mediaType === 'application/pdf' && url) {
    return `<iframe src="${escapeHTML(url)}#page=1&toolbar=0" title="${escapeHTML(name)} PDF 第一页预览" loading="lazy"></iframe>`;
  }
  if (expanded && modality === 'image' && url) {
    return `<img src="${escapeHTML(url)}" alt="${escapeHTML(name)} 的原始图像" loading="lazy">`;
  }
  const abbreviation = modality === 'image' ? 'IMG' : modality === 'audio' ? 'AUD' : mediaType === 'application/pdf' ? 'PDF' : modality === 'text' ? 'TXT' : 'DOC';
  const summary = String(asObject(observation).summary || '等待感知模型生成可定位摘要');
  return `<span class="attachment-modality-mark">${abbreviation}</span><small>${escapeHTML(truncate(summary, expanded ? 180 : 72))}</small>`;
}

function observationLocatorDetail(item) {
  const values = [];
  if (finiteValue(item.page) !== null) values.push(`第 ${finiteValue(item.page)} 页`);
  if (item.region) values.push(`区域 ${item.region}`);
  const start = finiteValue(item.start_ms);
  const end = finiteValue(item.end_ms);
  if (start !== null || end !== null) values.push(`时间 ${start === null ? '?' : formatDuration(start)}–${end === null ? '?' : formatDuration(end)}`);
  return values.join(' · ');
}

function observationTextMarkup(value, previewLength = 220) {
  const text = String(value || '观察文本未记录');
  if (text.length <= previewLength) {
    return `<strong class="observation-text-short">${escapeHTML(text)}</strong>`;
  }
  return `<details class="observation-text-details"><summary><span>${escapeHTML(truncate(text, previewLength))}</span><em>查看完整观察原文 · ${text.length} 字符</em></summary><div>${escapeHTML(text)}</div></details>`;
}

function renderInputPerception(state, audit = window.__latestAudit || null) {
  const rawAttachments = asArray(state.input_attachments);
  const attachments = rawAttachments.map(item => {
    const durable = audit?.inputAttachmentById?.get(normalizedId(item?.id)) || {};
    return {...item, ...durable, content_url:item?.content_url || durable.content_url || ''};
  });
  const observations = asArray(state.attachment_observations);
  const observationById = new Map(observations.map(item => [normalizedId(item?.attachment_id), item]));
  const invocations = asArray(state.agent_invocations);
  const perceptionCalls = invocations.filter(item => item.agent_id === 'perception' || item.operation === 'perceive_inputs');
  const plannerCalls = invocations.filter(item => item.agent_id === 'planner');
  const grounding = observations.map(attachmentGroundingModel);
  const totalObservations = grounding.reduce((sum, item) => sum + item.total, 0);
  const eligibleObservations = grounding.reduce((sum, item) => sum + item.eligible, 0);
  const manifestsRecorded = attachments.filter(item => Object.prototype.hasOwnProperty.call(item, 'manifest_valid'));
  const validManifests = attachments.filter(item => item.manifest_valid === true).length;
  const invalidManifests = attachments.filter(item => item.manifest_valid === false).length;
  const configuredPerception = modelRouteFor('perception', state.methodology);
  const latestPerception = perceptionCalls.at(-1);
  const actualPerception = latestPerception ? invocationModelRoute(latestPerception) : null;
  const observedPerception = observations.find(item => item?.model_id || item?.model_choice);
  const perceptionRoute = actualPerception || (observedPerception ? {
    choice:String(observedPerception.model_choice || ''),
    provider:'',
    model:String(observedPerception.model_id || ''),
    modalities:[],
  } : configuredPerception);
  const latestPlanner = plannerCalls.at(-1);
  const plannerRoute = latestPlanner ? invocationModelRoute(latestPlanner) : modelRouteFor('planner', state.methodology);
  const eligibilityLabel = totalObservations ? `${eligibleObservations}/${totalObservations} 条观察带有可查位置` : '尚无可计算观察';
  const manifestLabel = !attachments.length
    ? '无附件输入'
    : manifestsRecorded.length === attachments.length
      ? `${validManifests}/${attachments.length} 份已保存输入清单通过完整性检查${invalidManifests ? ` · ${invalidManifests} 份未通过` : ''}`
      : `${manifestsRecorded.length}/${attachments.length} 份有已保存输入清单记录`;

  $('perceptionSummary').textContent = `${attachments.length} 个附件 · ${totalObservations} 条定位观察 · ${eligibilityLabel}`;
  $('perceptionInputCount').textContent = attachments.length ? `${attachments.length} 个已保存附件` : '纯文本问题';
  $('perceptionInputDetail').textContent = attachments.length ? manifestLabel : '没有上传附件，不需要执行感知阶段';
  $('perceptionModel').textContent = attachments.length ? modelRouteLabel(perceptionRoute) : '本次不调用感知模型';
  $('perceptionModelDetail').textContent = attachments.length
    ? latestPerception
      ? `${perceptionCalls.length} 条执行记录 · 最近 ${invocationStatus(latestPerception.status)} · 输入 ${asArray(latestPerception.input_modalities).map(modalityLabel).join(' / ') || '模态未记录'}`
      : `本次配置 ${modelRouteLabel(configuredPerception)} · 尚无实际角色执行记录`
    : '附件数为 0，跳过多模态感知';
  $('perceptionObservationCount').textContent = totalObservations ? `${totalObservations} 条观察` : '尚无定位观察';
  $('perceptionObservationDetail').textContent = totalObservations ? `${eligibilityLabel} · 这不是事实正确率` : attachments.length ? '等待每个附件形成带位置的观察' : '没有附件观察分母';
  $('perceptionPlanner').textContent = modelRouteLabel(plannerRoute);
  $('perceptionPlannerDetail').textContent = latestPlanner
    ? `实际执行记录 ${latestPlanner.invocation_id || '编号未记录'} · ${invocationStatus(latestPlanner.status)}`
    : `本次配置 · 六角色协作的第 1 步`;
  $('inputPerceptionSection').dataset.state = invalidManifests ? 'invalid' : attachments.length && observations.length < attachments.length ? 'working' : attachments.length ? 'observed' : 'empty';

  if (!attachments.length) {
    $('attachmentAuditGrid').innerHTML = '<div class="attachment-audit-empty"><strong>本次没有附件</strong><p>用户问题会作为文本直接进入规划角色；页面不会虚构附件阅读过程或观察数量。</p></div>';
    return;
  }

  $('attachmentAuditGrid').innerHTML = attachments.map((attachment, index) => {
    const observation = observationById.get(normalizedId(attachment.id)) || {};
    const model = attachmentGroundingModel(observation);
    const invocation = perceptionInvocationForAttachment(attachment, index, attachments, invocations);
    const manifestRecorded = Object.prototype.hasOwnProperty.call(attachment, 'manifest_valid');
    const manifestTone = attachment.manifest_valid === true ? 'valid' : attachment.manifest_valid === false ? 'invalid' : 'unverified';
    const manifestText = attachment.manifest_valid === true ? '原始附件完整性已确认' : attachment.manifest_valid === false ? '附件完整性检查未通过' : '已保存输入清单未记录';
    const route = invocation ? invocationModelRoute(invocation) : {
      choice:String(observation.model_choice || configuredPerception.choice || ''),
      provider:String(configuredPerception.provider || ''),
      model:String(observation.model_id || configuredPerception.model || ''),
      modalities:[],
    };
    const url = attachmentContentURL(attachment);
    const observationMarkup = model.rows.length ? model.rows.map((item, observationIndex) => {
      const confidenceLabel = item.confidence === null ? '置信度未记录' : `${Math.round(Math.max(0, Math.min(1, item.confidence)) * 100)} / 100`;
      const locatorDetail = observationLocatorDetail(item);
      return `<li class="${item.eligible ? 'eligible' : 'review'}"><div class="observation-order"><span>${String(observationIndex + 1).padStart(2, '0')}</span><b>${item.eligible ? '位置可查' : '需人工补查'}</b></div><div class="observation-copy">${observationTextMarkup(item.text)}<p><b>位置</b> ${escapeHTML(item.locator || '未记录')}${locatorDetail ? ` · ${escapeHTML(locatorDetail)}` : ''}</p><small>${escapeHTML(item.kind || '观察类型未记录')} · 模型给出的定位信号 ${escapeHTML(confidenceLabel)} · 未校准，不代表事实正确率</small><div class="observation-confidence"${item.confidence === null ? ' data-unavailable="true"' : ''}><i${item.confidence === null ? '' : ` style="width:${Math.round(Math.max(0, Math.min(1, item.confidence)) * 100)}%"`}></i></div></div></li>`;
    }).join('') : '<li class="empty"><strong>尚无定位观察</strong><p>附件已保存，但阅读结果尚未写入记录或本次执行失败。</p></li>';
    const invocationAction = invocation
      ? `<button type="button" data-perception-invocation="${escapeHTML(invocation.invocation_id || '')}">查看感知执行记录</button>`
      : '<span class="attachment-invocation-missing">附件和感知执行记录尚未能一一对应</span>';
    return `<details class="attachment-audit-card modality-${escapeHTML(attachment.modality || 'document')} manifest-${manifestTone}" data-attachment-id="${escapeHTML(attachment.id || '')}" ${index === 0 ? 'open' : ''}><summary><div class="attachment-run-preview">${attachmentPreviewMarkup(attachment, observation)}</div><div class="attachment-run-title"><span>${String(index + 1).padStart(2, '0')} · ${escapeHTML(attachmentModalityName(attachment.modality))}</span><strong>${escapeHTML(attachment.name || '未命名附件')}</strong><small>${escapeHTML(attachment.media_type || '媒体类型未记录')} · ${escapeHTML(formatBytes(attachment.byte_length))}</small></div><div class="attachment-run-verdict"><b>${escapeHTML(manifestText)}</b><span>${model.eligible}/${model.total || 0} 条可回到附件位置</span><em>展开查验</em></div></summary><div class="attachment-audit-body"><div class="attachment-original-preview">${attachmentPreviewMarkup(attachment, observation, true)}</div><div class="attachment-receipt"><span>附件保存与阅读记录</span><dl><dt>附件编号（技术字段）</dt><dd>${escapeHTML(attachment.id || '未记录')}</dd><dt>完整性校验值（SHA-256）</dt><dd><code>${escapeHTML(attachment.sha256 || '未记录')}</code></dd><dt>文件大小与解析方式</dt><dd>${escapeHTML(formatBytes(attachment.byte_length))} · ${escapeHTML(attachment.parser_version || '解析方式未记录')}</dd><dt>保存清单检查</dt><dd class="${manifestTone}">${escapeHTML(manifestText)}${attachment.validation_error ? ` · ${escapeHTML(attachment.validation_error)}` : ''}</dd><dt>阅读模型</dt><dd>${escapeHTML(modelRouteLabel(route))}</dd><dt>阅读状态</dt><dd>${escapeHTML(observation.status || (invocation ? invocationStatus(invocation.status) : '未记录'))}</dd></dl><div class="attachment-actions">${url ? `<a href="${escapeHTML(url)}" target="_blank" rel="noopener">打开原始附件</a>` : '<span>原始附件地址不在允许的公开路径内</span>'}${invocationAction}</div></div><div class="attachment-observations"><header><span>可定位的附件观察</span><strong>${model.total} 条观察 · ${model.eligible} 条同时记录了位置和足够清晰的定位信号</strong><small>${escapeHTML(observation.summary || '阅读摘要未记录')} · 形成证据时仍要求与观察原文精确匹配</small></header><ol>${observationMarkup}</ol></div></div></details>`;
  }).join('');

  $('attachmentAuditGrid').querySelectorAll('[data-perception-invocation]').forEach(button => button.addEventListener('click', () => {
    const invocation = invocations.find(item => item.invocation_id === button.dataset.perceptionInvocation);
    if (invocation) showInvocationAudit(invocation, invocations, window.__latestEvents || [], audit);
  }));
}

function renderPhaseRail(invocations, runStatus) {
  const latestRunning = [...invocations].reverse().find(item => item.status === 'running');
  const latest = latestRunning || invocations.at(-1);
  const activeAgent = !isSettledStatus(runStatus) && agentOrder.includes(latest?.agent_id) ? latest.agent_id : null;
  const steps = [...document.querySelectorAll('[data-phase-agent]')];

  steps.forEach((button, index) => {
    const agent = button.dataset.phaseAgent;
    const runtime = agentRuntimeEvidence(agent, invocations, window.__latestEvents || []);
    const calls = runtime.calls;
    const state = runtime.status;
    button.className = state;
    button.setAttribute('aria-current', agent === activeAgent ? 'step' : 'false');
    const artifactCount=runtime.artifactIds.length>0?runtime.artifactIds.length:recordedArrayCount(calls,'output_artifact_ids');
    const detail=`${countText(calls.length||null,' 次调用')}、${countText(runtime.events.length||null,' 个阶段事件')}、${countText(artifactCount,' 个产物')}`;
    button.setAttribute('aria-label', `${agentContracts[agent].name}，${phaseStateName(state)}，${detail}；打开运行审计`);
    button.querySelector('small').textContent = runtime.observed
      ? `${phaseStateName(state)} · ${detail}`
      : '本次没有角色执行、阶段事件或产物记录';
    let modelBadge = button.querySelector('.phase-model');
    if (!modelBadge) {
      modelBadge = document.createElement('em');
      modelBadge.className = 'phase-model';
      button.appendChild(modelBadge);
    }
    const route = runtime.latest ? invocationModelRoute(runtime.latest) : modelRouteFor(agent, window.__latestState?.methodology || {});
    modelBadge.textContent = modelRouteLabel(route);
    modelBadge.title = runtime.latest
      ? `本次实际角色执行：${route.provider || '模型服务未记录'} / ${route.model || '模型编号未记录'}；输入类型 ${route.modalities.map(modalityLabel).join('、') || '未记录'}`
      : `本次已保存的配置；尚无该角色实际执行记录`;
  });

  const activeContract = activeAgent ? agentContracts[activeAgent] : null;
  const completed = steps.filter(step => step.classList.contains('done')).length;
  const localCitationBinding = asObject(window.__latestState?.answer_delivery).mode === 'local_citation_binding';
  $('phaseRailSummary').textContent = isSettledStatus(runStatus)
    ? localCitationBinding
      ? `${completed}/6 个角色有成功调用 · 引用材料已完成本地绑定，可逐项复核`
      : `${completed}/6 个角色留下完成记录 · 当前可逐项复核`
    : activeContract
      ? `${activeContract.name}正在推进 · ${completed}/6 个角色已完成阶段交付`
      : '等待第一条真实角色执行、阶段事件或产物记录；未记录角色保持等待';
  if (activeAgent) $('phaseFocus').dataset.agent = activeAgent;
  else delete $('phaseFocus').dataset.agent;
}

function renderStatus(status, error = '') {
  const delivery = asObject(window.__latestState?.answer_delivery);
  const hasDeliveredAnswer = Boolean(String(window.__latestState?.draft_answer || '').trim());
  const interruptedDelivery = delivery.mode === 'interrupted_evidence_limited';
  const localCitationBinding = delivery.mode === 'local_citation_binding';
  const labels = {queued:'任务已进入队列',running:'智能体正在研究',perceiving:'正在读取多模态附件',planning:'正在规划研究',drafting:'正在撰写回答',cancelling:'正在安全停止',cancelled:'任务已停止',completed:localCitationBinding?'回答已交付，本地引用检查完成':'本次研究流程完成',verification_failed:'引用仍需检查',evidence_incomplete:'当前回答已生成，仍待补齐核验',failed:interruptedDelivery&&hasDeliveredAnswer?'研究中断，已交付当前回答':'研究中断',recovery_unverified:'恢复记录无法核对'};
  const known = Object.prototype.hasOwnProperty.call(labels, status);
  const safeStatus = known ? status : 'unknown';
  const label = known ? labels[status] : '运行状态待确认';
  const detail = interruptedDelivery&&hasDeliveredAnswer&&status==='failed'
    ? '外部调用未完成；当前答复、附件观察和恢复入口已保留。具体异常可在失败记录中核查。'
    : error || (!known ? '服务端返回了页面尚未定义的状态，正在保留原始记录并等待下一次核验。' : '');
  $('runStatus').textContent = detail ? `${label}：${detail}` : label;
  const shell = $('runStatus').parentElement;
  shell.className = `run-status ${safeStatus}`;
  if (isSettledStatus(status)) stopRequestPending = false;
  $('stopButton').classList.toggle('visible', !isSettledStatus(status) && known);
  $('stopButton').disabled = stopRequestPending || status === 'cancelling' || !known;
  const canResume=['failed','verification_failed','evidence_incomplete','cancelled','recovery_unverified'].includes(status);
  $('resumeButton').classList.toggle('visible',canResume);
  const ambiguous=asArray(window.__latestState?.failures).some(item=>item?.type==='ambiguous_operation');
  $('resumeButton').textContent=status==='recovery_unverified'?'打开恢复审计':ambiguous?'确认费用风险并重试':'继续补证';
  lastAnnouncedStatus = status;
}

function announceLive(message, key = message, force = false) {
  const announcer = $('liveAnnouncer');
  if (!announcer || !message) return;
  if (!force && key === lastLiveAnnouncementKey) return;
  lastLiveAnnouncementKey = key;
  announcer.textContent = '';
  announcer.textContent = String(message);
}

function announceRuntimeSnapshot(state, status, error = '') {
  const invocations=asArray(state.agent_invocations);
  const latest=[...invocations].reverse()[0];
  const gates=gateConsoleModel(state);
  const passed=gates.filter(item=>item.tone==='passed').length;
  const unverifiable=gates.filter(item=>item.tone==='unverifiable').length;
  const gap=asArray(asObject(state.closure).gaps)[0];
  const gapKey=gap?`${gap.type||''}|${gap.slot_id||''}|${gap.description||''}`:'none';
  const invocationKey=invocations.length?invocations.map(item=>`${item?.invocation_id||''}:${item?.agent_id||''}:${item?.operation||''}:${item?.status||''}:${item?.attempt||''}`).join('|'):'none';
  const gateKey=gates.map(item=>`${item.key}:${item.tone}:${item.value}:${item.targetSlotId||''}:${item.unavailable||0}`).join('|');
  const key=`${status}|${invocationKey}|${gateKey}|${gapKey}|${error||''}`;
  if(key===lastAnnouncedRuntimeKey)return;
  lastAnnouncedRuntimeKey=key;
  const interruptedDelivery=asObject(state.answer_delivery).mode==='interrupted_evidence_limited'&&Boolean(String(state.draft_answer||'').trim());
  const statusLabels={queued:'任务已进入队列',running:'智能体正在研究',planning:'正在规划研究',drafting:'正在撰写回答',cancelling:'正在安全停止',cancelled:'任务已停止',completed:'本次研究流程完成',verification_failed:'引用仍需检查',evidence_incomplete:'当前回答已生成，仍待补齐核验',failed:interruptedDelivery?'研究中断，已交付当前回答':'研究中断',recovery_unverified:'恢复记录无法核对'};
  const invocationText=latest?`${agentContracts[latest.agent_id]?.name||latest.agent_id||'历史角色'}${invocationStatus(latest.status)}${operationName(latest.operation)?`，${operationName(latest.operation)}`:''}`:'尚无新的 AgentInvocation';
  const gateText=`交付前检查 ${passed}/${gateDefinitions.length}${unverifiable?`，${unverifiable} 项记录不足`:''}`;
  const gapText=gap?`最大缺口：${gapName(gap.type)}${gap.slot_id?`（${gap.slot_id}）`:''}`:'最大缺口：暂无记录';
  announceLive(`${statusLabels[status]||status}。${invocationText}。${gateText}。${gapText}`,`runtime:${key}`,true);
}

function renderMetrics(state) {
  const closure = asObject(state.closure);
  const usage = usageLedgerModel(state);
  const verification = state.verification && typeof state.verification === 'object' ? state.verification : null;
  const verificationState = verificationModel(verification);
  const sourceGroups = sourceGroupModel(state);
  const sources = sourceGroups.groups;
  const pageCoverage = sourcePageCoverageModel(state);
  const closureScoreStatus = String(closure.score_status || '').toLowerCase();
  const closureScore = closureScoreStatus === 'invalid' ? null : finiteValue(closure.score);
  const closureScoreUnavailable = closureScoreStatus === 'invalid'
    ? '研究计划没有有效必需目标分母 · 不可计算'
    : '未记录 · 不可计算';
  const progress = requiredSlotProgressModel(state);
  const gaps = asArray(closure.gaps);
  const gapsRecorded = recordedArray(closure, 'gaps');
  const gateFailures = recordedArray(closure, 'gate_failures');
  const gates = gateConsoleModel(state);
  const hardGatePassed = gates.length === gateDefinitions.length && gates.every(item => item.tone === 'passed');
  const issueCount = gateFailures !== null ? gateFailures.length : gapsRecorded !== null ? gapsRecorded.length : null;
  const issueLabel = issueCount === null ? '待补材料数量未记录' : `${issueCount} 项还需要补材料`;
  const gateLabel = !progress.requiredRecorded
    ? '必需目标数未记录'
    : !progress.passedRecorded
      ? `${progress.required} 个必需目标中的通过数未记录`
      : progress.required ? `${progress.passed}/${progress.required} 个必答问题完成检查` : '已记录 0 个必答问题';
  $('closureMetric').textContent = closureScore === null ? closureScoreUnavailable : displayPercent(closureScore);
  $('closureDetail').textContent = hardGatePassed ? `${gateLabel}，可以进入写作` : `${gateLabel}；${issueLabel}`;
  $('closureObserved').textContent = !progress.requiredRecorded
    ? '必答问题数未记录，当前不可计算'
    : !progress.passedRecorded
      ? `已记录 ${progress.required} 个必答问题；${progress.knownPassed} 个明确完成检查，${progress.unknownRows || '其余'}检查结果未记录`
      : progress.required
        ? `${progress.passed} 个必答问题完成检查 / ${progress.required} 个必答问题 · ${gapsRecorded === null ? '缺口数未记录' : `${gaps.length} 个待补材料缺口`}`
        : '已记录 0 个必答问题；尚未建立分母，暂无可计算比值';
  setMeter('closureMeter', closureScore);
  const gateEquation = !progress.requiredRecorded ? '分母未记录 · 不可计算' : !progress.passedRecorded ? `通过数量未记录 / ${progress.required}` : progress.required ? `${progress.passed} / ${progress.required}` : '0 / 0 · 不可计算';
  $('closureEquation').textContent = closureScore === null
    ? `加权流程分${closureScoreStatus === 'invalid' ? '无有效分母' : '未记录'} · 不可计算；交付前检查 ${gateEquation}`
    : `加权流程分 ${displayPercent(closureScore)} / 100 · 交付前检查 ${gateEquation}`;
  $('closureNext').textContent = hardGatePassed
    ? '判断：交付前检查完成，可以进入带引用写作'
    : progress.requiredRecorded ? `下一步：${gapName(closure.gaps?.[0]?.type || 'missing_evidence')}` : '下一步：先建立必答问题和完成条件';
  $('sourceMetric').textContent = sourceGroups.sourceGateAvailable
    ? `${sourceGroups.sourceGatePassed}/${sourceGroups.sourceGateRequired}`
    : '未记录 · 不可计算';
  $('pageMetric').textContent = pageCoverage.denominator === null ? '页面读取分母未记录' : `${pageCoverage.numerator === null ? '—' : pageCoverage.numerator}/${pageCoverage.denominator} Fetch 记录产生证据`;
  $('sourceObserved').textContent = !sourceGroups.sourceGateAvailable
    ? `必答问题的来源检查结果未完整保存；当前只看到 ${sourceGroups.knownAdmittedCount} 条已确认材料，不能把缺失目标当作 0`
    : `${sourceGroups.sourceGatePassed} 个目标通过来源互证 / ${sourceGroups.sourceGateRequired} 个必答问题 · 支持材料涉及 ${sources.size} 个来源组`
      + (sourceGroups.missing ? ` · ${sourceGroups.missing} 条支持材料缺少来源组编号` : '');
  setMeter('sourceMeter', null);
  $('sourceEquation').textContent = sourceGroups.sourceGateAvailable
    ? `通过来源互证的目标 ${sourceGroups.sourceGatePassed} / 必答问题 ${sourceGroups.sourceGateRequired}；另计 ${sources.size} 个支持材料来源组（不等于机构完全独立）`
    : '逐目标来源检查结果 / 必答问题数未完整保存 · 不可计算';
  $('sourceNext').textContent = sourceGroups.sourceGateAvailable && sources.size
    ? '判断：每个目标都通过来源互证；来源组只是保守去重，独立性仍要看每条来源依据'
    : '下一步：保存每个必答问题的来源检查结果和支持材料来源组';
  $('pageCoverageMetric').textContent = pageCoverage.denominator === null || pageCoverage.numerator === null ? '未记录 · 不可计算' : `${pageCoverage.numerator}/${pageCoverage.denominator}`;
  $('pageCoverageDetail').textContent = pageCoverage.denominator === null
    ? '页面读取计数未记录；没有来源级 fetch 分母，不能计算页面覆盖率'
    : pageCoverage.denominator
      ? `${pageCoverage.numerator}/${pageCoverage.denominator} 个成功读取的页面产生至少一条可用支持材料`
      : '已记录 0 个可计 fetch 页面；覆盖率分母为 0，暂无可计算比值';
  $('pageCoverageObserved').textContent = pageCoverage.denominator === null
    ? `分子未记录 / 分母未记录 · 不可计算 · ${pageCoverage.denominatorSource}`
    : `分子 ${pageCoverage.numerator === null ? '—' : pageCoverage.numerator} / 分母 ${pageCoverage.denominator} · ${pageCoverage.denominatorSource}`;
  $('pageCoverageEquation').textContent = pageCoverage.denominator === null
    ? '产生证据的页面未记录 ÷ 已读取页面未记录 · 不可计算'
    : `产生可用支持材料的成功页面读取 ${pageCoverage.numerator===null?'未记录':pageCoverage.numerator} ÷ 成功页面读取 ${pageCoverage.denominator}${pageCoverage.denominator && pageCoverage.numerator!==null ? '' : ' · 不可计算'}`;
  setMeter('pageCoverageMeter', pageCoverage.ratio);
  $('pageCoverageNext').textContent = pageCoverage.ratio === null
    ? '下一步：先加载完整 source_fetches 审计，再保存每个 Fetch 的 result_invocation_id 与来源绑定'
    : pageCoverage.numerator === pageCoverage.denominator
      ? '判断：每个成功读取的页面都产生了至少一条可用材料；仍需核对每条证据的原文位置'
      : '下一步：检查已读取但未产生可用材料的页面，并人工确认是否相关';
  $('verifyMetric').textContent = !verificationState.present
    ? '未记录'
    : !verificationState.contractComplete
      ? '不可计算'
      : `${verificationState.entailed}/${verificationState.expectedItemCount}`;
  $('verifyDetail').textContent = !verificationState.present
    ? '等待引用核验'
    : !verificationState.contractComplete
      ? `逐句检查记录不完整（预期 ${verificationState.expectedItemCount ?? '未记录'}，实际 ${verificationState.rows.length}；模型返回 ${verificationState.providerItemCount ?? '未记录'}）· 不可计算`
      : verificationState.passed === true ? citationContractPassLabel : verificationState.passed === false ? '部分解析句单元需要补证' : '最终引用判定未记录 · 不可验证';
  $('verifyObserved').textContent = !verificationState.present
    ? '逐句核验记录未记录'
    : `${verificationState.rows.length} 个解析句单元 · 预期 ${verificationState.expectedItemCount ?? '未记录'} · provider 返回 ${verificationState.providerItemCount ?? '未记录'}${verificationState.invalidStatuses ? ` · ${verificationState.invalidStatuses} 个状态不在 engine 契约中` : ''}`;
  setMeter('verifyMeter', verificationState.ratio);
  $('verifyEquation').textContent = verificationState.ratio === null
    ? '引用与原文完整对应的句子数 ÷ 预期事实句数 · 当前不可计算'
    : `${verificationState.entailed} 个充分支持的句子 ÷ ${verificationState.expectedItemCount} 个预期事实句`;
  $('verifyNext').textContent = verificationState.passed === true && verificationState.contractComplete
    ? citationContractPassLabel
    : verificationState.present ? '下一步：补齐预期句数、模型返回数、句子编号和引用对应记录后重算' : '下一步：形成带证据编号的回答后逐句重查';
  const inputTokens=usage.inputTokens;
  const outputTokens=usage.outputTokens;
  const tokenFieldsComplete=inputTokens!==null&&outputTokens!==null;
  const tokenFieldsPartlyRecorded=inputTokens!==null||outputTokens!==null;
  const tokens=tokenFieldsComplete?inputTokens+outputTokens:null;
  const modelCalls=usage.modelCalls;
  const estimatedCost=usage.estimatedCost;
  const knownCost=usage.knownCostLowerBound;
  const replayList=Array.isArray(state.operation_replays) ? state.operation_replays : Array.isArray(state.operation_replay_details) ? state.operation_replay_details : null;
  const replays=replayList===null?null:replayList.length;
  const replayLabel=replays===null?'回放计数未记录':`${replays} 次持久化回放`;
  const usageSourceLabel=usage.durableAvailable
    ? `durable usage ledger ${usage.ledgerEntries===null?'账目数未记录':`${usage.ledgerEntries} 条`} · ${usageStatusName(usage.usageStatus)}`
    : usage.source==='state_counters_fallback'?'公开状态计数器回退（非 usage ledger）':'开销来源未记录';
  const usageAnyRecorded=[modelCalls,estimatedCost,knownCost,replays,inputTokens,outputTokens].some(value=>value!==null);
  const costAuditAvailable=usage.durableAvailable||usageAnyRecorded;
  const tokenUnavailableLabel=usage.usageStatus==='not_applicable'?'Provider 未计量':usage.usageStatus==='partial'?'部分计量 · 不可合计':usage.usageStatus==='unavailable'?'Token 证据不可用':'未记录';
  $('tokenMetric').textContent = tokens===null?(tokenFieldsPartlyRecorded?'不可计算':tokenUnavailableLabel):tokens.toLocaleString();
  const costText = estimatedCost !== null
    ? `按配置单价估算 ${formatEstimatedCost(estimatedCost)}`
    : knownCost !== null
      ? `目前至少 ${formatKnownCost(knownCost)} · “+”表示仍有未细分计价的输入`
      : null;
  $('costValue').textContent = estimatedCost !== null
    ? formatEstimatedCost(estimatedCost)
    : knownCost !== null
      ? formatKnownCost(knownCost)
      : '—';
  $('costValue').title = estimatedCost !== null
    ? '累计费用已按当前记录的全部可计价 Token 计算。'
    : knownCost !== null
      ? '这是目前已能按单价计算的部分；“+”表示网关没有返回某些输入类型的细分 Token，实际费用可能更高。'
      : '尚无足以计算费用的用量与单价记录。';
  const latestUsageText = usageSnapshotPresent(usage.latestEntry)
    ? usageEntryLabel(usage.latestEntry, {latest:true})
    : '';
  const reconciliationText = usage.reconciledModelOperations && usage.reconciledModelOperations > 0
    ? `；${usage.reconciledModelOperations} 个步骤的实时记录与最终服务端用量不一致，已按最终汇总重新计算`
    : '';
  $('costObserved').textContent = `${modelCalls===null?`${unmeasuredUsageLabel(usage.usageStatus)}外部模型请求计数`:`${modelCalls} 次外部模型请求`} · ${replayLabel} · ${usageSourceLabel} · ${pricingStatusName(usage.pricingStatus)}${latestUsageText?` · ${latestUsageText}`:''}`;
  $('costMetric').textContent = !costAuditAvailable
    ? '开销字段未记录'
    : !usageAnyRecorded
      ? `${usageStatusName(usage.usageStatus)} · ${pricingStatusName(usage.pricingStatus)}`
    : modelCalls===0&&replays===0&&estimatedCost===0
      ? '已记录 0 次模型调用'
      : `${costText || pricingStatusName(usage.pricingStatus)} · ${modelCalls===null?`${unmeasuredUsageLabel(usage.usageStatus)}模型调用数`:`${modelCalls} 次外部模型调用`} · ${replays===null?'回放数未记录':`${replays} 次持久化操作回放`}`;
  setMeter('costMeter', tokenFieldsComplete&&tokens>0 ? inputTokens/tokens : null);
  $('costEquation').textContent = tokenFieldsComplete
    ? `输入 ${inputTokens.toLocaleString()} + 输出 ${outputTokens.toLocaleString()} = ${tokens.toLocaleString()} Token · ${costText || pricingStatusName(usage.pricingStatus)} · ${usageSourceLabel} · 条形图绿色为输入占比`
    : tokenFieldsPartlyRecorded
      ? `输入 ${inputTokens===null?'未记录':inputTokens.toLocaleString()} + 输出 ${outputTokens===null?'未记录':outputTokens.toLocaleString()} = 不可计算；缺失侧不能按 0 处理`
      : `Token 合计不可计算 · ${usageStatusName(usage.usageStatus)}${usage.usageReason?`；${usage.usageReason}`:''}`;
  $('costNext').textContent = (modelCalls!==null&&modelCalls>0)||(replays!==null&&replays>0)
    ? `账本：${modelCalls===null?'实际调用数未记录':`${modelCalls} 次实际调用`}，${replays===null?'回放数未记录':`${replays} 次幂等回放`}；回放不重复请求模型`
    : usage.usageStatus==='not_applicable'?'当前 Provider 未提供可计量用量；不能把账本中的零值当作零消耗'
      : usage.usageStatus==='partial'?'下一步：保存 Provider 返回的完整输入与输出 Token 快照'
        : usage.usageStatus==='complete'&&usage.pricingStatus!=='complete'?'Token 已完整计量；配置并记录单价后才能计算费用'
          : usageAnyRecorded?'已记录当前预算字段；后续操作完成后继续更新':'预算消耗将在每次持久化操作后更新';
  const pendingText = usage.pendingModelOperations
    ? `；${usage.pendingModelOperations} 个模型请求尚未返回用量`
    : '';
  const settledText = usage.settledModelResponses
    ? `；已确认 ${usage.settledModelResponses} 次模型接口响应${usage.settledModelOperations ? `，${usage.settledModelOperations} 个步骤仍在保存结果` : ''}`
    : usage.settledModelOperations
      ? `；${usage.settledModelOperations} 个步骤已入账，正在保存结果`
      : '';
  const revisionText = usage.usageRevision === null
    ? ''
    : `；账本已更新 ${usage.usageRevision} 次`;
  $('costUpdated').textContent = usage.updatedAt
    ? `用量账本最后更新：${formatTimestamp(usage.updatedAt)}${revisionText}${pendingText}${settledText}${latestUsageText?`；${latestUsageText}`:''}${reconciliationText}`
    : usage.pendingModelOperations
      ? `已有模型请求进行中；接口返回用量后写入费用${pendingText}`
      : '尚未记录模型请求的 durable 用量';
  $('costBreakdown').textContent = usageBreakdownLabel(usage);
  setMetricSignal('closureSignal', hardGatePassed ? 'passed' : closureScore !== null ? 'working' : 'waiting', hardGatePassed ? '交付前检查完成' : closureScore !== null ? '分项正在累积' : '历史字段未记录');
  setMetricSignal('sourceSignal', sourceGroups.available ? (sourceGroups.admittedCount ? 'working' : 'waiting') : 'waiting', sourceGroups.available ? (sources.size ? `${sources.size} 个来源组 · 不等于页面覆盖` : '可用证据分母为 0 · 不可计算') : '已确认来源集合未记录 · 不可计算');
  setMetricSignal('pageCoverageSignal', pageCoverage.ratio === null ? 'waiting' : pageCoverage.ratio === 1 ? 'passed' : 'working', pageCoverage.ratio === null ? (pageCoverage.denominator===null?'页面读取计数未记录':'页面分母为 0 · 不可计算') : `${Math.round(pageCoverage.ratio * 100)}% 页面证据覆盖`);
  setMetricSignal('verifySignal', verificationState.passed === true && verificationState.contractComplete ? 'passed' : verificationState.passed === false ? 'blocked' : 'waiting', verificationState.passed === true && verificationState.contractComplete ? citationContractPassLabel : verificationState.passed === false ? '存在需要补材料的句子' : '逐句引用检查记录未完整保存 · 不可计算');
  setMetricSignal('costSignal', knownCost!==null?'working':'waiting', costAuditAvailable?`${knownCost===null?'费用待计算':costText} · ${tokens===null?'Token 不可计算':`${tokens.toLocaleString()} Token`}`:'开销字段未记录');
  renderMetricDecisionStrip(metricDecisionModel({
    closureScore,
    sourceGateAvailable: sourceGroups.sourceGateAvailable,
    pageCoverageRatio: pageCoverage.ratio,
    verificationRatio: verificationState.ratio,
    tokens,
    gates,
  }));
}

function metricDecisionModel({
  closureScore = null,
  sourceGateAvailable = false,
  pageCoverageRatio = null,
  verificationRatio = null,
  tokens = null,
  gates = [],
} = {}) {
  const checks = [
    {label:'流程完成度', available:finiteValue(closureScore) !== null, reason:'流程分数缺少有效必需目标分母'},
    {label:'来源互证', available:sourceGateAvailable === true, reason:'逐目标来源检查结果未完整保存'},
    {label:'页面证据覆盖', available:finiteValue(pageCoverageRatio) !== null, reason:'完整页面读取记录或证据关联不足'},
    {label:'逐句引用检查', available:finiteValue(verificationRatio) !== null, reason:'逐句引用检查的分子或分母不足'},
    {label:'Token 合计', available:finiteValue(tokens) !== null, reason:'Provider 输入或输出 Token 缺失'},
  ];
  const availableItems = checks.filter(item => item.available);
  const missingItems = checks.filter(item => !item.available);
  const gateRows = asArray(gates);
  const gatePassed = gateRows.filter(item => item.tone === 'passed').length;
  const gateBlocked = gateRows.filter(item => item.tone === 'blocked').length;
  const gateUnverifiable = gateRows.filter(item => item.tone === 'unverifiable').length;
  const gateWaiting = gateRows.filter(item => item.tone === 'waiting').length;
  const hardGateComplete = gateRows.length === gateDefinitions.length && gateRows.every(item => item.tone === 'passed');
  const gateRecorded = gateRows.length === gateDefinitions.length && gateWaiting === 0;
  const gateValue = gateRecorded ? `${gatePassed}/${gateDefinitions.length}` : '未记录';
  const gateTone = hardGateComplete ? 'passed' : gateBlocked ? 'blocked' : gateUnverifiable ? 'unverifiable' : gateRecorded ? 'working' : 'waiting';
  const decision = hardGateComplete
    ? {tone:'passed', value:'检查完成 · 可进入写作', note:'五项交付前检查均完成；仍须保留逐句引用核验'}
    : gateBlocked
      ? {tone:'blocked', value:'需要补材料 · 继续研究', note:`${gateBlocked} 项交付前检查未完成；加权分数不能代替材料`}
      : gateUnverifiable
        ? {tone:'unverifiable', value:'记录不足，无法判断', note:`${gateUnverifiable} 项交付前检查缺少可重新核对的字段；缺失不能当作已确认`}
        : {tone:'waiting', value:'等待逐目标检查', note:'尚未形成五项交付前检查的完整运行记录'};
  return {
    available: availableItems.length,
    total: checks.length,
    availableLabels: availableItems.map(item => item.label),
    missing: missingItems.map(item => item.label),
    missingReasons: missingItems.map(item => `${item.label}：${item.reason}`),
    gateValue,
    gateTone,
    gatePassed,
    gateBlocked,
    gateUnverifiable,
    gateRecorded,
    decision,
  };
}

function renderMetricDecisionStrip(model) {
  const strip = $('metricDecisionStrip');
  if (!strip || !model) return;
  const cells = {
    available: strip.querySelector('[data-metric-decision="available"]'),
    gates: strip.querySelector('[data-metric-decision="gates"]'),
    missing: strip.querySelector('[data-metric-decision="missing"]'),
    decision: strip.querySelector('[data-metric-decision="decision"]'),
  };
  const setCell = (key, value, note, tone) => {
    const cell = cells[key];
    if (!cell) return;
    const strong = cell.querySelector('strong');
    const small = cell.querySelector('small');
    if (strong) strong.textContent = value;
    if (small) small.textContent = note;
    cell.dataset.tone = tone || 'waiting';
  };
  setCell(
    'available',
    `${model.available}/${model.total}`,
    model.available === model.total
      ? '五项主值均有有效分子/分母或完整 Token 合计'
      : `${model.available} 项可复算；缺失项不会被当作 0`,
    model.available === model.total ? 'passed' : model.available ? 'working' : 'waiting',
  );
  setCell(
    'gates',
    model.gateValue,
    model.gateRecorded
      ? `${model.gatePassed}/${gateDefinitions.length} 项已确认 · ${model.gateBlocked} 项需要补材料 · ${model.gateUnverifiable} 项记录不足`
      : '五项交付前检查尚无完整逐目标记录',
    model.gateTone,
  );
  setCell(
    'missing',
    model.missing.length ? `${model.missing.length} 项` : '无',
    model.missing.length ? model.missingReasons.join('；') : '五项主值均可复算；仍需查看各卡限制条件',
    model.missing.length ? 'working' : 'passed',
  );
  setCell('decision', model.decision.value, model.decision.note, model.decision.tone);
}

function setMeter(id, ratio) {
  const meter = $(id);
  if (!meter) return;
  const numeric = finiteValue(ratio);
  const unavailable = numeric === null;
  const value = unavailable ? null : Math.max(0, Math.min(1, numeric));
  const container = meter.parentElement;
  container?.setAttribute('role', 'progressbar');
  container?.setAttribute('aria-valuemin', '0');
  container?.setAttribute('aria-valuemax', '100');
  if (value === null) meter.style.removeProperty('width');
  else meter.style.width = `${Math.round(value * 100)}%`;
  if (value === null) {
    container?.removeAttribute('aria-valuenow');
    container?.setAttribute('aria-valuetext', '未记录或分母为 0，当前不可计算');
  } else {
    const percent = Math.round(value * 100);
    container?.setAttribute('aria-valuenow', String(percent));
    container?.setAttribute('aria-valuetext', `${percent}%`);
  }
  container?.toggleAttribute('data-unavailable', unavailable);
}

function setMetricSignal(id, tone, label) {
  const signal = $(id);
  if (!signal) return;
  signal.dataset.tone = tone;
  const text = signal.querySelector('b');
  if (text) text.textContent = label;
}

function renderResultOverview(state, runStatus = null) {
  const overview = $('resultOverview');
  if (!state.draft_answer) {
    overview.classList.add('hidden');
    return;
  }
  overview.classList.remove('hidden');
  const recoveryBlocked = runStatus === 'recovery_unverified';
  const delivery = asObject(state.answer_delivery);
  const interruptedDelivery = delivery.mode === 'interrupted_evidence_limited';
  const limitedDelivery = delivery.mode === 'evidence_limited' || interruptedDelivery;
  const localCitationBinding = delivery.mode === 'local_citation_binding';
  const verification = state.verification && typeof state.verification === 'object' ? state.verification : null;
  const verificationState = verificationModel(verification);
  const evidence = asArray(state.evidence);
  const sourceGroups = sourceGroupModel(state);
  const sources = sourceGroups.groups;
  const passed = verificationState.entailed;
  const normalizedAnswer=recoveryBlocked ? '' : String(state.draft_answer).trim();
  const answerSummary=recoveryBlocked ? {text:'',label:'恢复审计状态'} : buildAnswerSummary(normalizedAnswer,evidence,state.question);
  const overviewCitations=recoveryBlocked ? [] : overviewCitationItems(normalizedAnswer,evidence);
  const hasUnknownCitation=overviewCitations.some(item=>!item.item);
  const invocations=asArray(state.agent_invocations);
  const events=asArray(window.__latestEvents);
  const audit=window.__latestAudit || null;
  const completionExplanation=deliveryCompletionExplanation(state, invocations, events);
  $('overviewStatus').textContent = recoveryBlocked
    ? '恢复记录无法核对 · 回答已暂时隐藏'
    : localCitationBinding ? '回答已交付：每句引用编号可回到已保存材料；自动逐句语义核对没有返回结果，系统未把这一步标为通过'
    : interruptedDelivery ? '运行中断，已交付当前可查回答'
    : limitedDelivery ? '当前回答已生成 · 仍待补齐独立来源、反面材料或逐句引用检查'
    : hasUnknownCitation ? '回答含无法找到的证据编号，当前不能核查' : verificationState.passed === true ? citationContractPassLabel : '待核对回答，仍需检查';
  $('overviewSummaryLabel').textContent = interruptedDelivery ? '运行中断后的当前回答 · 可查已保存材料' : limitedDelivery ? '当前可交付回答 · 可查已保存材料' : answerSummary.label;
  $('overviewAnswer').innerHTML = recoveryBlocked
    ? '<div class="recovery-answer-block" role="alert"><strong>恢复审计未通过</strong><p>当前 durable 状态与恢复回执、transition 或执行 fence 无法一致对应。候选回答正文与引用入口已隐藏，避免把未核验结果当作完成结果。</p><button type="button" class="audit-link-button" data-overview-recovery>打开恢复审计</button></div>'
    : formatOverviewCitations(answerSummary.text, evidence);
  $('overviewSources').innerHTML=recoveryBlocked
    ? '<span>恢复记录无法核对，暂不展示回答的引用入口。</span>'
    : overviewCitations.length?`<span>本结论引用依据</span><div>${overviewCitations.map(({item,id,index})=>item?`<button type="button" class="overview-citation-source" data-evidence="${escapeHTML(id)}"><b>${index}</b><span><strong>${escapeHTML(item.source_title||sourceDomain(item.source_url))}</strong><small>${escapeHTML(id)} · ${escapeHTML(sourceDomain(item.source_url))}</small></span><i>核查</i></button>`:`<div class="overview-citation-source unknown" role="note"><b>${index}</b><span><strong>未知 Evidence ID</strong><small>${escapeHTML(id)} · 无当前证据记录，不能核查</small></span><i>不可核查</i></div>`).join('')}</div>`:'<span>本结论没有可解析的 Evidence ID</span>';
  $('overviewAnswer').querySelector('[data-overview-recovery]')?.addEventListener('click', openCurrentRecoveryAudit);
  const progress=requiredSlotProgressModel(state);
  const requiredPassed=progress.passed;
  const requiredCount=progress.required;
  const iterationCount=finiteValue(asObject(state.counters).iterations);
  const requiredSummary = requiredCount===null||requiredPassed===null ? '未记录 · 不可计算' : requiredCount ? `${requiredPassed}/${requiredCount}` : '已记录 0/0 · 不可计算';
  const verificationSummary = localCitationBinding
    ? `${verificationState.rows.length}/${verificationState.rows.length} 本地绑定`
    : !verificationState.present ? '未记录' : !verificationState.itemsRecorded || !verificationState.rows.length || verificationState.unknownStatuses ? '不可计算' : `${passed}/${verificationState.rows.length}`;
  const sourceRecordCount = normalizeSources(state).length;
  const evidenceCount = evidence.length;
  $('overviewFacts').innerHTML = `<div><span>已检查研究点</span><strong>${requiredSummary}</strong><small>每个必答问题都完成五项交付前检查</small></div><div><span>来源与证据</span><strong>${sourceRecordCount} / ${evidenceCount}</strong><small>来源记录 / 可回到原文的证据片段</small></div><div><span>回答引用对应</span><strong>${verificationSummary}</strong><small>每句引用编号都可打开对应保存材料</small></div><div><span>研究轮次</span><strong>${iterationCount === null ? '未记录' : iterationCount}</strong><small>每一轮依据缺口继续检索和复查</small></div>`;
  const handoffRecords=auditHandoffRecords(events,audit).map(record=>({...record,assessment:handoffReceiptAssessment(record.envelope,events,invocations,audit)}));
  const serverValidatedHandoffs=handoffRecords.filter(item=>item.assessment.status==='server_validated');
  const receiptCounts=handoffRecords.reduce((counts,item)=>{counts[item.assessment.status]=(counts[item.assessment.status]||0)+1;return counts},{});
  const executed=invocations.filter(item=>item.execution_mode!=='replayed').length;
  const replayed=invocations.filter(item=>item.execution_mode==='replayed').length;
  const nonSequential=serverValidatedHandoffs.filter(({envelope,assessment})=>agentOrder.indexOf(assessment.receipt?.consumed_by_agent_id)!==agentOrder.indexOf(envelope.producer)+1).length;
  const allRolesCompleted=state.status==='completed'&&agentOrder.every(agent=>agentRuntimeEvidence(agent,invocations,events).status==='done');
  const roleCompletionLabel=allRolesCompleted?'六个内部角色已结束本轮协作':'六个内部角色状态按真实记录展示';
  $('overviewTraceSummary').textContent=`${roleCompletionLabel} · ${completionExplanation} · ${invocations.length} 条执行记录：${executed} 条真实执行、${replayed} 条复用已有结果 · 接收确认：${receiptCounts.server_validated||0} 已确认 / ${receiptCounts.field_match||0} 待确认 / ${receiptCounts.unverified||0} 未确认 / ${receiptCounts.invalid||0} 暂不采用${nonSequential?` · ${nonSequential} 条已确认交接用于补充材料或跨步骤协作`:''}`;
  $('overviewAgents').innerHTML=agentOrder.map((agent,index)=>{
    const runtime=agentRuntimeEvidence(agent,invocations,events);
    const calls=runtime.calls;
    const latest=runtime.latest;
    const providerCalls=providerCallLabel(calls);
    const artifacts=runtime.artifactIds.length>0?runtime.artifactIds.length:recordedArrayCount(calls,'output_artifact_ids');
    const eventCount=runtime.events.length||null;
    const incoming=handoffRecords.filter(({assessment})=>assessment.receipt?.consumed_by_agent_id===agent);
    const serverIncoming=incoming.filter(({assessment})=>assessment.status==='server_validated');
    const stateClass=runtime.status==='done'?'done':runtime.status==='blocked'?'blocked':runtime.status==='running'?'running':runtime.status==='observed'?'observed':'missing';
  const stateLabel=phaseStateName(runtime.status);
    const incomingLabel=agent==='planner'?'入口：用户研究任务':serverIncoming.length?`${serverIncoming.length} 条系统已确认的接收`:incoming.length?`${incoming.length} 条接收确认记录，均未获系统确认`:'没有接收确认记录';
    const card=`<button type="button" class="overview-agent-card ${stateClass}" data-overview-agent="${agent}"><span class="overview-agent-seq">${String(index+1).padStart(2,'0')}</span><span class="overview-agent-role">${escapeHTML(agentContracts[agent].name)}</span><em>${escapeHTML(stateLabel)}</em><strong>${escapeHTML(latest?operationName(latest.operation):runtime.events.length?'有阶段事件但无 invocation 完成记录':agentContracts[agent].output)}</strong><small>${countText(calls.length||null,' 条 invocation')} · ${countText(eventCount,' 条 event')}${window.__latestEventWindow?.incomplete?'（事件仅为最近窗口）':''} · ${escapeHTML(providerCalls)} · ${countText(artifacts,' 个产物')}</small><b>${escapeHTML(incomingLabel)}</b></button>`;
    if(index===agentOrder.length-1)return card;
    const nextAgent=agentOrder[index+1];
    const transition=handoffRecords.find(({envelope,assessment})=>envelope.producer===agent&&(assessment.receipt?.consumed_by_agent_id===nextAgent||handoffRouteTarget(envelope)===nextAgent));
    const transitionStatus=transition?.assessment.status||'unverified';
    const transitionClass=transition?`receipt-${transitionStatus}${transitionStatus==='server_validated'?' verified':''}`:'planned';
    return `${card}<button type="button" class="overview-agent-handoff ${transitionClass}" ${transition?`data-overview-handoff="${escapeHTML(transition.envelope.message_id)}"`:'disabled'} aria-label="${transition?`${agentContracts[agent].name}到${agentContracts[nextAgent].name}：${receiptStateLabel(transitionStatus)}；打开人工核验`:`${agentContracts[agent].name}到${agentContracts[nextAgent].name}仅有设计路线，没有交接信封`}"><span>${transition?escapeHTML(receiptStateLabel(transitionStatus)):'计划路线'}</span><i>→</i></button>`;
  }).join('');
  document.querySelectorAll('[data-overview-agent]').forEach(button=>button.addEventListener('click',()=>showAgentAudit(button.dataset.overviewAgent,invocations,window.__latestEvents||[],audit)));
  document.querySelectorAll('[data-overview-handoff]').forEach(button=>button.addEventListener('click',()=>showHandoffAudit(button.dataset.overviewHandoff,invocations,events,audit)));
  bindCitationLinks(overview);
}

function renderAgentCockpit(invocations, events, status) {
  const nodes = [...document.querySelectorAll('.agent-node')];
  nodes.forEach(node => node.classList.remove('active','done','failed'));
  const latestByAgent = new Map();
  invocations.forEach(item => latestByAgent.set(item.agent_id, item));
  const running = [...invocations].reverse().find(item => item.status === 'running');
  const recordedLatest = invocations.at(-1);
  const inferredLatest = [...events].reverse().map(event => eventAgentId(event)).find(Boolean);
  const latest = running?.agent_id || recordedLatest?.agent_id || inferredLatest || null;
  nodes.forEach(node => {
    const agent = node.dataset.agent;
    const runtimeEvidence = agentRuntimeEvidence(agent, invocations, events);
    const invocation = runtimeEvidence.latest || latestByAgent.get(agent);
    const count = runtimeEvidence.calls.length;
    const invocationCount = count > 0 ? count : null;
    const artifactCount = runtimeEvidence.artifactIds.length > 0 ? runtimeEvidence.artifactIds.length : recordedArrayCount(runtimeEvidence.calls, 'output_artifact_ids');
    const handoffCount = recordedArrayCount(runtimeEvidence.calls, 'handoff_message_ids');
    const eventCount = runtimeEvidence.events.length || null;
    const route = invocation ? invocationModelRoute(invocation) : modelRouteFor(agent, window.__latestState?.methodology || {});
    const routeLabel = modelRouteLabel(route, {includeModel:false});
    node.dataset.count = String(count);
    node.dataset.runtimeState = runtimeEvidence.status;
    node.dataset.observed = String(runtimeEvidence.observed);
    node.removeAttribute('aria-pressed');
    if (latest && agent === latest) node.setAttribute('aria-current', 'step');
    else node.removeAttribute('aria-current');
    const runtime = node.querySelector('.agent-runtime');
    if (runtime) runtime.textContent = runtimeEvidence.observed
      ? `${phaseStateName(runtimeEvidence.status)} · ${countText(invocationCount,' 调用')} · ${countText(eventCount,' 事件')} · ${countText(artifactCount,' 产物')}`
      : '没有角色执行、阶段事件或产物记录';
    const proof = node.querySelector('.agent-node-proof');
    if (proof) {
      proof.textContent = runtimeEvidence.observed
        ? `${routeLabel} · ${countText(artifactCount,' 产物')} · ${countText(handoffCount,' 计划投递')}`
        : `${routeLabel}（配置） · 调用未记录`;
      proof.title = invocation
        ? `实际模型 ${modelRouteLabel(route)}；输入模态 ${route.modalities.map(modalityLabel).join('、') || '未记录'}`
        : `持久化配置路线；尚无实际 invocation`;
    }
    node.setAttribute('aria-label', `${agentContracts[agent].name}；${routeLabel}；${runtimeEvidence.statusReason}`);
    if (runtimeEvidence.status === 'done') node.classList.add('done');
    if (runtimeEvidence.status === 'blocked') node.classList.add('failed');
    if (!isSettledStatus(status) && latest && agent === latest) node.classList.add('active');
  });
  const runtimeByAgent = agentOrder.map(agent => agentRuntimeEvidence(agent, invocations, events));
  const completedRoles = runtimeByAgent.filter(item => item.status === 'done').length;
  const localCitationBinding = asObject(window.__latestState?.answer_delivery).mode === 'local_citation_binding';
  if (status === 'completed' && completedRoles === agentOrder.length) {
    $('agentHeadline').textContent = '研究总控已完成全部验收';
    $('agentNarration').textContent = '六个角色均留下 succeeded invocation；回答、证据来源与逐句核验结果可继续打开查验。';
  } else if (status === 'completed' && localCitationBinding) {
    $('agentHeadline').textContent = '最终回答已交付，引用材料可逐句回查';
    $('agentNarration').textContent = `${completedRoles}/6 个角色有成功调用；引用语义核验服务没有返回结果，系统已完成引用编号与保存材料的本地绑定。展开角色卡可区分已成功调用与待重试的语义检查。`;
  } else if (status === 'completed') {
    $('agentHeadline').textContent = `任务已进入完成态，但仅 ${completedRoles}/6 个角色有 succeeded invocation`;
    $('agentNarration').textContent = '终态来自运行记录；缺失角色不会被补画成完成，请打开阶段审计检查事件和产物是否完整。';
  } else if (status === 'cancelled') {
    $('agentHeadline').textContent = '研究任务已安全停止';
    $('agentNarration').textContent = `${runtimeByAgent.filter(item => item.observed).length}/6 个角色留下 invocation、event 或 artifact；现有记录已保存供人工复核。`;
  } else if (status === 'failed' || status === 'verification_failed') {
    $('agentHeadline').textContent = status === 'failed' ? '研究总控已保存故障现场' : '核验智能体拒绝交付未充分支持的回答';
    $('agentNarration').textContent = '已执行角色、失败调用、交接产物与恢复建议均保留；未执行角色不会被标记为完成。';
  } else {
    const message = agentMessages[latest] || ['研究总控正在等待真实运行记录','尚未发现 invocation、event 或阶段产物；页面不会把设计路线当成本次执行。'];
    [$('agentHeadline').textContent, $('agentNarration').textContent] = message;
  }
  renderAgentLiveBrief(latest || 'planner', invocations, status);
  renderAgentContract(latest || 'planner', invocations);
  renderAgentNetworkFlow(invocations, status, events, window.__latestAudit || null);
}

function outerAgentRoutePath(fromAgent, toAgent) {
  const center = [350, 260];
  const outerRadius = 252;
  const points = {planner:[350,55],scout:[527.535,157.5],curator:[527.535,362.5],critic:[350,465],writer:[172.465,362.5],verifier:[172.465,157.5]};
  const from = points[fromAgent];
  const to = points[toAgent];
  if (!from || !to || fromAgent === toAgent) return '';
  const angle = point => Math.atan2(point[1] - center[1], point[0] - center[0]);
  const project = point => [
    center[0] + outerRadius * Math.cos(angle(point)),
    center[1] + outerRadius * Math.sin(angle(point)),
  ];
  const number = value => {
    const rounded = Number(value.toFixed(3));
    return Object.is(rounded, -0) ? 0 : rounded;
  };
  const fromOuter = project(from).map(number);
  const toOuter = project(to).map(number);
  const fullTurn = Math.PI * 2;
  const clockwise = (angle(to) - angle(from) + fullTurn) % fullTurn;
  const sweep = clockwise <= Math.PI ? 1 : 0;
  const span = sweep ? clockwise : fullTurn - clockwise;
  const largeArc = span > Math.PI ? 1 : 0;
  return `M${from[0]} ${from[1]} L${fromOuter[0]} ${fromOuter[1]} A${outerRadius} ${outerRadius} 0 ${largeArc} ${sweep} ${toOuter[0]} ${toOuter[1]} L${to[0]} ${to[1]}`;
}

function renderAgentNetworkFlow(invocations, status, events = [], audit = window.__latestAudit || null) {
  const network = $('agentNetwork');
  if (!network) return;
  const preservedScrollLeft = network.scrollLeft;
  const preserveMobilePosition = network.dataset.positionInitialized === 'true';
  const edges = [...network.querySelectorAll('.agent-edge')];
  edges.forEach(edge => edge.classList.remove('observed', 'server-validated', 'traversed', 'invalid', 'active'));
  const edgeGroups = [...network.querySelectorAll('.agent-edge-group')];
  const invocationTransitions = invocationTransitionRecords(invocations);
  const handoffRecords = auditHandoffRecords(events, audit).map(record => ({
    ...record,
    assessment:handoffReceiptAssessment(record.envelope, events, invocations, audit),
  }));
  const observedTransitions = receiptBackedAgentTransitions(handoffRecords);
  const receiptBackedNonstandard = [];
  observedTransitions.forEach(({from, to}) => {
    const edge = network.querySelector(`.agent-edge[data-from="${from}"][data-to="${to}"]`);
    if (edge) edge.classList.add('observed');
    else receiptBackedNonstandard.push([from, to]);
  });
  const serverValidatedHandoffs = handoffRecords.filter(record => record.assessment.status === 'server_validated');
  serverValidatedHandoffs.forEach(({envelope, assessment}) => {
    const consumer = assessment.receipt?.consumed_by_agent_id;
    network.querySelector(`.agent-edge[data-from="${envelope.producer}"][data-to="${consumer}"]`)?.classList.add('server-validated');
  });
  const verifiedHandoffs = handoffRecords.filter(record => {
    const proof = handoffProofModel(
      record,
      record.envelope?.producer,
      record.assessment?.receipt?.consumed_by_agent_id,
      audit,
    );
    return proof.strong;
  });
  const verifiedNonstandard = [];
  verifiedHandoffs.forEach(({envelope, assessment}) => {
    const receipt = assessment.receipt;
    const consumer = receipt.consumed_by_agent_id;
    const edge = network.querySelector(`.agent-edge[data-from="${envelope.producer}"][data-to="${consumer}"]`);
    if (edge) edge.classList.add('traversed');
    else if (agentOrder.includes(envelope.producer) && agentOrder.includes(consumer)) verifiedNonstandard.push([envelope.producer, consumer]);
  });
  const latest = [...invocations].reverse().find(item => item.status === 'running') || invocations.at(-1);
  const latestIndex = latest ? invocations.lastIndexOf(latest) : -1;
  const previous = latestIndex > 0 ? invocations[latestIndex - 1] : null;
  // A yellow active edge needs a receipt tied to this exact invocation. A
  // neighboring row in the invocation log is only an order signal.
  const currentReceiptRecord = previous && latest && handoffRecords.find(({envelope, assessment}) => {
    const receipt = assessment.receipt;
    return envelope?.producer === previous.agent_id
      && normalizedId(envelope?.producer_invocation_id) === normalizedId(previous.invocation_id)
      && receipt?.consumed_by_agent_id === latest.agent_id
      && receipt?.consumed_by_invocation_id === latest.invocation_id
      && ['server_validated', 'field_match'].includes(assessment.status);
  });
  const currentReceiptTransition = Boolean(currentReceiptRecord);
  const directEdge = currentReceiptTransition
    ? network.querySelector(`.agent-edge[data-from="${previous.agent_id}"][data-to="${latest.agent_id}"]`)
    : null;
  const repairGroup = $('agentRepairEdgeGroup');
  const repairEdge = $('agentRepairEdge');
  const repairEdgeHit = $('agentRepairEdgeHit');
  repairEdge?.classList.remove('active', 'traversed', 'verified');
  repairGroup?.classList.remove('active', 'traversed', 'verified');
  let repairRoute = null;
  const drawRepairRoute = (fromAgent, toAgent) => {
    if (!repairEdge) return false;
    const path = outerAgentRoutePath(fromAgent, toAgent);
    if (!path) return false;
    repairEdge.setAttribute('d', path);
    repairEdgeHit?.setAttribute('d', path);
    return true;
  };
  const lastVerifiedNonstandard = verifiedNonstandard.at(-1);
  const lastNonstandard = lastVerifiedNonstandard || receiptBackedNonstandard.at(-1);
  if (lastNonstandard && drawRepairRoute(...lastNonstandard)) {
    repairRoute = lastNonstandard;
    repairEdge?.classList.add('traversed');
    repairGroup?.classList.add('traversed');
  }
  if (lastVerifiedNonstandard) {
    repairEdge?.classList.add('verified');
    repairGroup?.classList.add('verified');
  }
  if (directEdge && !isSettledStatus(status)) directEdge.classList.add('active');
  if (previous && latest && previous.agent_id !== latest.agent_id && currentReceiptTransition && !directEdge && !isSettledStatus(status)) {
    if (drawRepairRoute(previous.agent_id, latest.agent_id)) {
      repairRoute = [previous.agent_id, latest.agent_id];
      repairEdge?.classList.add('active');
      repairGroup?.classList.add('active');
    }
  }
  if (repairGroup) {
    repairGroup.classList.toggle('is-hidden', !repairRoute);
    if (repairRoute) {
      const [from, to] = repairRoute;
      repairGroup.dataset.from = from;
      repairGroup.dataset.to = to;
      repairGroup.setAttribute('tabindex', '0');
      repairGroup.setAttribute('aria-hidden', 'false');
      repairGroup.setAttribute('aria-label', `${agentContracts[from]?.name || from}到${agentContracts[to]?.name || to}的旁路补材料路线；点击查看角色执行、任务交接和接收确认审计`);
    } else {
      repairGroup.removeAttribute('data-from');
      repairGroup.removeAttribute('data-to');
      repairGroup.setAttribute('tabindex', '-1');
      repairGroup.setAttribute('aria-hidden', 'true');
    }
  }
  edgeGroups.forEach(group => {
    const from = group.dataset.from;
    const to = group.dataset.to;
    const rawTransitionsForEdge = invocationTransitions.filter(item => item.from === from && item.to === to);
    const transitionsForEdge = observedTransitions.filter(item => item.from === from && item.to === to);
    const recordsForEdge = handoffRecords.filter(({envelope, assessment}) => envelope.producer === from && (handoffRouteTarget(envelope) === to || assessment.receipt?.consumed_by_agent_id === to));
    const receiptCounts = recordsForEdge.reduce((counts, record) => { counts[record.assessment.status] = (counts[record.assessment.status] || 0) + 1; return counts; }, {});
    const verifiedForEdge = verifiedHandoffs.filter(({envelope, assessment}) => envelope.producer === from && assessment.receipt?.consumed_by_agent_id === to);
    const path = group.querySelector('.agent-edge');
    const fromName = agentContracts[from]?.name || from || '未解析发送方';
    const toName = agentContracts[to]?.name || to || '未解析接收方';
    const serverForEdge = recordsForEdge.filter(record => record.assessment.status === 'server_validated');
    const invalidForEdge = recordsForEdge.filter(record => record.assessment.status === 'invalid');
    group.classList.toggle('observed', transitionsForEdge.length > 0);
    group.classList.toggle('server-validated', serverForEdge.length > 0);
    group.classList.toggle('traversed', verifiedForEdge.length > 0);
    group.classList.toggle('invalid', invalidForEdge.length > 0 && transitionsForEdge.length === 0);
    group.classList.toggle('active', Boolean(directEdge && path === directEdge && !isSettledStatus(status)));
    group.dataset.transitionCount = String(transitionsForEdge.length);
    group.dataset.invocationTransitionCount = String(rawTransitionsForEdge.length);
    group.dataset.receiptCount = String(receiptCounts.server_validated || 0);
    group.dataset.receiptStates = JSON.stringify(receiptCounts);
    group.setAttribute('aria-label', `${fromName}到${toName}：设计职责路线；有接收确认的角色转移 ${transitionsForEdge.length} 次；相邻角色执行顺序记录 ${rawTransitionsForEdge.length} 次（顺序本身不证明因果）；接收确认状态 ${receiptCounts.server_validated || 0} 条系统已确认、${receiptCounts.field_match || 0} 条信息能对应、${receiptCounts.unverified || 0} 条尚未确认、${receiptCounts.invalid || 0} 条记录不一致；点击查看人工核验审计`);
  });
  if (latest) {
    $('networkOperation').textContent = `${operationName(latest.operation)} · ${invocationStatus(latest.status)}`;
    const receiptBindingNote = currentReceiptRecord?.assessment.status === 'server_validated'
      ? '交付已通过服务端验证'
      : '字段指向当前 invocation，但尚未服务端验证';
    const transfer = currentReceiptRecord
      ? `接收确认：${receiptStateLabel(currentReceiptRecord.assessment.status)}；${agentContracts[previous.agent_id]?.name || previous.agent_id}→${agentContracts[latest.agent_id]?.name || latest.agent_id}；${receiptBindingNote}`
      : previous && previous.agent_id !== latest.agent_id
        ? `原始调用顺序显示${agentContracts[previous.agent_id]?.name || previous.agent_id}→${agentContracts[latest.agent_id]?.name || latest.agent_id}，未证明交接`
        : `${agentContracts[latest.agent_id]?.name || latest.agent_id}正在处理当前阶段`;
    const receiptStateCounts = handoffRecords.reduce((counts, record) => { counts[record.assessment.status] = (counts[record.assessment.status] || 0) + 1; return counts; }, {});
    const loopNote = `接收确认：${receiptStateCounts.server_validated || 0} 条系统已确认 / ${receiptStateCounts.field_match || 0} 条信息能对应 / ${receiptStateCounts.unverified || 0} 条尚未确认 / ${receiptStateCounts.invalid || 0} 条记录不一致${receiptBackedNonstandard.length ? ` · ${receiptBackedNonstandard.length} 次有记录的旁路补材料` : ''}`;
    const inputSummary=runtimeSummary(latest.input_summary,latest.operation,'input')||'输入摘要未记录';
    const outputSummary=runtimeSummary(latest.output_summary,latest.operation,'output')||(latest.status==='running'?'等待输出':'输出摘要未记录');
    const providerCalls=providerCallCount(latest);
    $('networkTransfer').innerHTML = `<div class="network-transfer-head"><span>当前真实调用</span><small>${escapeHTML(latest.invocation_id||'invocation ID 未记录')}</small></div><div class="network-transfer-flow"><p><b>拿到</b><em>${escapeHTML(truncate(inputSummary,96))}</em></p><i aria-hidden="true">→</i><p><b>执行</b><em>${escapeHTML(truncate(transfer,78))}</em></p><i aria-hidden="true">→</i><p><b>交付</b><em>${escapeHTML(truncate(outputSummary,96))}</em></p></div><small>${escapeHTML(loopNote)} · ${providerCalls===null?'能力接口调用数未记录':`${providerCalls} 次能力接口调用`} · ${escapeHTML(invocationStatus(latest.status))}</small>`;
    $('networkTransfer').title = `${inputSummary} → ${outputSummary} · ${loopNote}`;
  } else {
    $('networkOperation').textContent = '等待真实调用';
    $('networkTransfer').innerHTML = '<div class="network-transfer-head"><span>当前真实调用</span><small>尚无 invocation ID</small></div><div class="network-transfer-flow"><p><b>拿到</b><em>等待输入</em></p><i aria-hidden="true">→</i><p><b>执行</b><em>等待第一条真实调用记录</em></p><i aria-hidden="true">→</i><p><b>交付</b><em>等待结构化产物</em></p></div><small>没有记录时不绘制虚构交接</small>';
  }
  updateAgentNetworkAccessibility(
    network,
    latest,
    invocationTransitions,
    observedTransitions,
    verifiedHandoffs,
    handoffRecords,
    status,
  );
  renderNetworkEdgeLedger(invocations, events, audit);
  requestAnimationFrame(() => {
    if (!window.matchMedia('(max-width: 800px)').matches) return;
    if (preserveMobilePosition) {
      networkProgrammaticScroll = true;
      network.scrollLeft = preservedScrollLeft;
      requestAnimationFrame(() => { networkProgrammaticScroll = false; });
      return;
    }
    centerAgentNetwork(false);
  });
}

function centerAgentNetwork(animate = true) {
  const network = $('agentNetwork');
  if (!network) return;
  networkProgrammaticScroll = true;
  const left = Math.max(0, (network.scrollWidth - network.clientWidth) / 2);
  network.scrollTo({left, behavior:animate && !reducedMotion.matches ? 'smooth' : 'auto'});
  network.dataset.positionInitialized = 'true';
  window.setTimeout(() => { networkProgrammaticScroll = false; }, animate && !reducedMotion.matches ? 450 : 0);
}

function updateAgentNetworkAccessibility(
  network,
  latest,
  invocationTransitions,
  observedTransitions,
  verifiedHandoffs,
  handoffRecords,
  status,
) {
  const svg = network?.querySelector('svg');
  const latestName = latest ? (agentContracts[latest.agent_id]?.name || latest.agent_id || '历史角色') : '尚无真实执行记录';
  const rawCount = asArray(invocationTransitions).length;
  const observedCount = asArray(observedTransitions).length;
  const verifiedCount = asArray(verifiedHandoffs).length;
  const label = `六智能体正六边形协作关系图；当前${latestName}；有接收确认的角色交接 ${observedCount} 次，其中完整可查交接 ${verifiedCount} 次；相邻执行记录 ${rawCount} 次，先后顺序本身不证明已经交接；点击节点或连线查看记录`;
  svg?.setAttribute('aria-label', label);
  const description = svg?.querySelector('desc');
  if (description) {
    description.textContent = `六个角色围绕研究总控按顺时针排列。灰线是设计路线，蓝线表示交接信息能对应，青线表示系统确认接收，绿线表示同一条交接记录的接收、阶段检查和已保存产物都能核查。红线表示有记录的补充材料路线或记录不一致。执行先后顺序会单独展示，不自动等于任务交接。当前状态：${status || '未记录'}。`;
  }
  const announcer = $('agentNetworkLive');
  if (!announcer) return;
  const latestKey = latest?.invocation_id || latest?.agent_id || 'none';
  const key = `${status}|${latestKey}|${rawCount}|${observedCount}|${verifiedCount}|${handoffRecords.length}`;
  if (key === lastNetworkAnnouncementKey) return;
  lastNetworkAnnouncementKey = key;
  announcer.textContent = `协作图更新：${latestName}；有接收确认的交接 ${observedCount} 次，其中完整可查 ${verifiedCount} 次；相邻执行记录 ${rawCount} 次，先后顺序本身不证明已经交接。`;
}

function renderNetworkEdgeLedger(invocations = [], events = [], audit = window.__latestAudit || null) {
  const list = $('networkEdgeList');
  const summary = $('networkEdgeSummary');
  const sequenceList = $('networkSequenceList');
  if (!list) return;
  const model = networkEdgeLedgerModel(invocations, events, audit);
  if (summary) {
    const denominator = model.totalEnvelopeCount || '—';
    summary.innerHTML = `<b>${model.strongCount}/${denominator}</b><span>已确认且可复查的交接</span><small>${model.receiptRouteCount}/6 条路线有接收确认记录 · ${model.rawTransitionCount} 次执行先后记录</small>`;
  }
  if (sequenceList) {
    sequenceList.innerHTML = model.sequence.length
      ? model.sequence.map(item => {
        const kindLabel = ({strong:'已确认且可复查',server:'系统已确认接收',field:'信息能对应，待确认',order:'仅有执行先后记录'})[item.kind] || '仅有执行先后记录';
        const note = item.kind === 'order'
          ? '相邻执行记录，不证明已经交接'
          : item.evidence?.consumerInvocationId
            ? `接收记录指向执行记录 ${item.evidence.consumerInvocationId}`
            : '未找到与当前执行记录对应的接收确认';
        return `<li class="network-sequence-item ${escapeHTML(item.kind)}"><button type="button" data-network-sequence-open data-network-edge-from="${escapeHTML(item.from)}" data-network-edge-to="${escapeHTML(item.to)}"><span>${String(item.index + 1).padStart(2, '0')}</span><strong>${escapeHTML(item.fromName)} <i>→</i> ${escapeHTML(item.toName)}</strong><em>${escapeHTML(kindLabel)}</em><small>${escapeHTML(note)} · 角色执行记录 ID（技术字段）${escapeHTML(item.current?.invocation_id || '未记录')}</small></button></li>`;
      }).join('')
      : '<li class="network-sequence-empty">等待至少两个不同角色留下执行记录</li>';
  }
  list.innerHTML = model.routes.map(route => {
    const total = route.records.length;
    const totalLabel = total ? `${total} 条任务交接记录` : '没有任务交接记录';
    const receiptDetail = route.receiptEvidenceCount
      ? `已确认 ${route.receiptCounts.server_validated} · 待确认 ${route.receiptCounts.field_match} · 未确认 ${route.receiptCounts.unverified} · 暂不采用 ${route.receiptCounts.invalid}`
      : '没有接收确认记录，不能证明已经接收';
    const gateDetail = total ? `${route.gatePassedCount}/${total} 条交接的阶段检查通过` : '— · 没有交接记录可计算';
    const manifestDetail = total ? `${route.manifestValidCount}/${total} 条交接有完整的已保存产物清单` : '— · 没有交接记录可计算';
    const strongDetail = total ? `${route.strongCount}/${total} 条交接的信息全部齐全` : '— · 不可计算';
    const recordButtonLabel = total ? '查看这条路线的先后顺序、接收确认与产物' : '查看这条路线，确认为什么没有交接记录';
    const receiptTone = route.receiptCounts.server_validated ? 'server' : route.receiptCounts.field_match ? 'partial' : route.receiptEvidenceCount ? 'invalid' : 'missing';
    const gateTone = route.gatePassedCount ? 'present' : 'missing';
    const artifactTone = route.manifestValidCount ? 'present' : 'missing';
    const strongTone = route.strongCount ? 'present' : 'missing';
    return `<li class="network-edge-card ${escapeHTML(route.tone)}"><button type="button" class="network-edge-card-button" data-network-edge-open data-network-edge-from="${escapeHTML(route.from)}" data-network-edge-to="${escapeHTML(route.to)}" aria-label="${escapeHTML(`${route.fromName}到${route.toName}：接收、阶段检查和产物均可复查 ${route.strongCount}/${total || 0}；${route.label}；点击查看逐条记录`)}"><div class="same-envelope-verdict ${strongTone}"><span>本条交接的完整检查</span><strong>信息齐全 ${route.strongCount}/${total || 0}</strong><small>同一条交接记录内：发送、接收、阶段检查和已保存产物都能对应</small></div><header><span>${String(route.index + 1).padStart(2, '0')} · 顺时针设计路线</span><strong>${escapeHTML(route.fromName)} <i>→</i> ${escapeHTML(route.toName)}</strong><em>${escapeHTML(route.label)}</em></header><p class="network-edge-card-note">${escapeHTML(route.note)}</p><div class="network-edge-proof" aria-label="同一条交接记录的三项检查；不得从不同记录拼凑"><span class="${receiptTone}"><b>01</b><strong>系统确认接收</strong><small>${escapeHTML(route.receiptCounts.server_validated ? `${route.receiptCounts.server_validated}/${total} 条已确认` : '0 条已确认')}</small></span><i aria-hidden="true">→</i><span class="${gateTone}"><b>02</b><strong>阶段检查通过</strong><small>${escapeHTML(route.gatePassedCount ? `${route.gatePassedCount}/${total} 条通过` : '0 条通过')}</small></span><i aria-hidden="true">→</i><span class="${artifactTone}"><b>03</b><strong>已保存产物可核对</strong><small>${escapeHTML(route.manifestValidCount ? `${route.manifestValidCount}/${total} 条可复查` : '0 条可复查')}</small></span><i aria-hidden="true">→</i><span class="${strongTone}"><b>✓</b><strong>已确认且可复查</strong><small>${escapeHTML(strongDetail)}</small></span></div><p class="network-edge-proof-boundary">每项都必须来自同一条交接记录，不能把不同记录里的通过项拼在一起；三项同时成立时才会把连线标绿。</p><dl class="network-edge-facts"><div><dt>执行先后</dt><dd>${route.orderSignalCount} 次<small>仅相邻角色执行记录</small></dd></div><div><dt>接收确认</dt><dd>${escapeHTML(receiptDetail)}</dd></div><div><dt>阶段检查</dt><dd>${escapeHTML(gateDetail)}</dd></div><div><dt>产物清单</dt><dd>${escapeHTML(manifestDetail)}</dd></div></dl><footer><small>${escapeHTML(totalLabel)} · 点击逐条核对交接编号、两端角色执行记录和已保存产物</small><b>${escapeHTML(recordButtonLabel)} ↗</b></footer></button></li>`;
  }).join('');
  const repair = $('networkRepairLedger');
  if (repair) {
    if (!model.repairs.length) {
      repair.classList.add('hidden');
      repair.innerHTML = '';
    } else {
      repair.classList.remove('hidden');
      repair.innerHTML = `<header><span>补充材料路线</span><strong>有接收确认才显示</strong><small>这些路径不改变六边形的设计顺序；它们只表示实际接收方在本次运行中走了旁路。</small></header><div>${model.repairs.map(item => `<button type="button" data-network-edge-open data-network-edge-from="${escapeHTML(item.from)}" data-network-edge-to="${escapeHTML(item.to)}"><b>${escapeHTML(item.fromName)} → ${escapeHTML(item.toName)}</b><span class="receipt-state ${escapeHTML(item.assessment.status)}">${escapeHTML(receiptStateLabel(item.assessment.status))}</span><small>交接编号（技术字段）${escapeHTML(item.envelope.message_id || '未记录')} · ${item.strong ? '阶段检查和产物完整性也已在同一条记录中核对' : '还需人工核对阶段检查和产物完整性'}</small></button>`).join('')}</div>`;
    }
  }
  document.querySelectorAll('[data-network-edge-open], [data-network-sequence-open]').forEach(button => {
    if (button.dataset.networkEdgeBound === 'true') return;
    button.dataset.networkEdgeBound = 'true';
    button.addEventListener('click', () => showNetworkEdgeAudit(button.dataset.networkEdgeFrom, button.dataset.networkEdgeTo, invocations, events, audit));
  });
}

function showNetworkEdgeAudit(from, to, invocations = [], events = [], audit = window.__latestAudit || null) {
  const fromName = agentContracts[from]?.name || from || '未解析发送方';
  const toName = agentContracts[to]?.name || to || '未解析接收方';
  const transitions = invocationTransitionRecords(invocations).filter(item => item.from === from && item.to === to);
  const records = auditHandoffRecords(events, audit).filter(record => {
    const envelope = record.envelope;
    const assessment = handoffReceiptAssessment(envelope, events, invocations, audit);
    return envelope?.producer === from && (handoffRouteTarget(envelope) === to || assessment.receipt?.consumed_by_agent_id === to);
  }).map(record => ({...record, assessment:handoffReceiptAssessment(record.envelope, events, invocations, audit)}));
  const strongRecords = records.filter(record => handoffProofModel(record, from, to, audit).strong);
  const handoffMarkup = records.length ? records.map((record, index) => {
    const {envelope, event, assessment} = record;
    const receipt=assessment.receipt;
    const proof=handoffProofModel(record,from,to,audit);
    const tone=proof.strong?'strong':assessment.status==='server_validated'?'server':'';
    const verdict=proof.strong?'这条交接的接收、检查和产物都已核对':assessment.status==='server_validated'?'系统已确认接收，但阶段检查或产物信息仍不完整':receiptStateLabel(assessment.status);
    return `<article class="edge-audit-record receipt-${escapeHTML(assessment.status)} ${tone}"><div class="per-envelope-verdict ${proof.strong?'passed':'incomplete'}"><span>第 ${String(index + 1).padStart(2,'0')} 条交接记录</span><strong>${escapeHTML(verdict)}</strong><small>交接编号（技术字段）${escapeHTML(envelope.message_id || '未记录')} · 发送执行记录 ${escapeHTML(proof.producerInvocationId || '未记录')} · 接收执行记录 ${escapeHTML(proof.consumerInvocationId || '未记录')}</small></div><header><b>${String(index + 1).padStart(2,'0')} · ${escapeHTML(envelope.message_id)}</b><span class="receipt-state ${escapeHTML(assessment.status)}">${escapeHTML(receiptStateLabel(assessment.status))}</span></header><p>${escapeHTML(`输出 ${asArray(envelope.output_artifacts).length} 个阶段产物`)} · ${escapeHTML(formatTimestamp(event?.created_at || envelope.created_at))}</p><small>计划接收角色：${escapeHTML(handoffRouteTarget(envelope) || '未记录')} · 阶段检查：${escapeHTML(gateStatusName(envelope.quality_gate?.status || 'unknown'))} · ${receipt ? `接收执行记录：${escapeHTML(receipt.consumed_by_invocation_id || '未记录')}` : '尚无接收确认记录'}</small>${handoffProofChecklistMarkup(proof)}<p class="audit-human-check">${escapeHTML(proof.strong ? '所有检查都来自同一条交接记录，没有借用其他记录。' : proof.proofReasons.join(' ') || assessment.reasons.join(' '))} ${escapeHTML(assessment.manualCheck)}</p><button type="button" class="audit-link-button" data-edge-handoff="${escapeHTML(envelope.message_id)}">查看交接详情、产物清单、接收确认与两端角色执行记录</button></article>`;
  }).join('') : '<div class="audit-empty">这条路线没有持久化交接信封；不能把六边形上的设计箭头当成本次实际协作。</div>';
  const receiptCounts=records.reduce((counts,record)=>{counts[record.assessment.status]=(counts[record.assessment.status]||0)+1;return counts},{});
  const transitionMarkup = transitions.length ? transitions.map(({previous, current}, index) => `<article class="edge-audit-record observed"><header><b>${String(index + 1).padStart(2,'0')} · 日志中的相邻执行顺序</b><span>角色执行记录 ID（技术字段）${escapeHTML(current.invocation_id || '未记录')}</span></header><p>${escapeHTML(operationName(previous.operation))} → ${escapeHTML(operationName(current.operation))}</p><small>前一条 ${escapeHTML(previous.invocation_id || 'ID 未记录')} · 后一条 ${escapeHTML(current.invocation_id || 'ID 未记录')}；这只能说明日志先后，不能单独说明任务已交给下一角色。</small><button type="button" class="audit-link-button" data-edge-invocation="${escapeHTML(current.invocation_id || '')}">打开后一条角色执行记录</button></article>`).join('') : '<div class="audit-empty">本次角色执行记录中没有角色切换的先后信号。</div>';
  openAuditDialog('协作路线检查', `${fromName} → ${toName}`, '六边形箭头只表示设计职责路线。角色执行的先后顺序与真正的任务交接分开显示；一条交接只有在同一份记录中同时找到发送、接收、阶段检查和已保存产物时，才会标为可复查。', `<div class="edge-audit-hero"><strong>${escapeHTML(fromName)} <i>→</i> ${escapeHTML(toName)}</strong><span>设计职责路线 · 点击记录查看谁在何时交付了什么</span></div><div class="audit-summary-grid"><article><span>已确认且可复查</span><strong>${strongRecords.length} / ${records.length || 0}</strong><small>所有检查必须来自同一条交接记录</small></article><article><span>执行先后信号</span><strong>${transitions.length} 次</strong><small>日志排列，不自动等于实际交接</small></article><article><span>任务交接记录</span><strong>${records.length} 条</strong><small>按交接编号去重</small></article><article><span>接收确认状态</span><strong>${receiptCounts.server_validated||0} / ${receiptCounts.field_match||0} / ${receiptCounts.unverified||0} / ${receiptCounts.invalid||0}</strong><small>已确认 / 待确认 / 未确认 / 暂不采用</small></article></div><section class="edge-audit-section"><header><span>角色执行顺序</span><strong>日志中的先后关系</strong><small>它帮助阅读过程，但不是任务交接的证据。</small></header><div class="edge-audit-list">${transitionMarkup}</div></section><section class="edge-audit-section"><header><span>逐条任务交接</span><strong>接收、检查和产物是否都能核对</strong><small>每条记录独立展示接收确认、阶段检查和产物清单；局部通过项不能跨记录合并。</small></header><div class="edge-audit-list">${handoffMarkup}</div></section>`);
  $('auditContent').querySelectorAll('[data-edge-handoff]').forEach(button => button.addEventListener('click', () => showHandoffAudit(button.dataset.edgeHandoff, invocations, events, audit)));
  $('auditContent').querySelectorAll('[data-edge-invocation]').forEach(button => button.addEventListener('click', () => {
    const item = invocations.find(value => value.invocation_id === button.dataset.edgeInvocation);
    if (item) showInvocationAudit(item, invocations, events, audit);
  }));
}

function renderAgentLiveBrief(agent, invocations, status) {
  const calls = invocations.filter(item => item.agent_id === agent);
  const latest = calls.at(-1);
  const contract = agentContracts[agent] || agentContracts.planner;
  if (!latest) {
    $('agentLiveBrief').innerHTML = `<div><span>当前执行者</span><strong>${escapeHTML(contract.name)}</strong><small>本次尚无角色执行记录</small></div><div><span>当前处理</span><strong>设计中的职责</strong><small>尚未看到实际角色执行，不能判断它正在处理什么</small></div><div><span>推进依据</span><strong>暂无法判断</strong><small>角色输入、输出和阶段检查尚未写入可核对的记录</small></div>`;
    return;
  }
  const gates = latest?.quality_gate_statuses || [];
  const gatePassed = gates.length && gates.every(value => String(value).toLowerCase() === 'passed');
  const localCitationBinding=asObject(window.__latestState?.answer_delivery).mode==='local_citation_binding';
  const decision = isSettledStatus(status)
    ? status === 'completed' ? localCitationBinding ? '回答已交付；本地引用绑定检查完成，语义模型核验服务超时' : '所有交付前检查和引用核对均已通过' : '运行已停止，已保留现场供人工复核'
    : latest?.status === 'failed' ? '当前角色执行失败，先处理问题再继续'
    : gatePassed ? '本阶段检查通过，可以交给下一角色'
    : gates.length ? '本阶段还有未通过的检查项，先补齐再继续' : '等待本阶段产生可检查结果';
  const inputSummary=runtimeSummary(latest?.input_summary,latest?.operation,'input')||contract.input;
  const outputSummary=runtimeSummary(latest?.output_summary,latest?.operation,'output')||contract.gate;
  const route=invocationModelRoute(latest);
  const completed = status === 'completed';
  $('agentLiveBrief').innerHTML = `<div><span>${completed ? '最终归档者' : '当前执行者'}</span><strong>${escapeHTML(contract.name)}</strong><small>${escapeHTML(modelRouteLabel(route))} · ${invocationStatus(latest.status)} · 第 ${latest.attempt} 次尝试</small></div><div><span>${completed ? '已完成操作' : '正在处理'}</span><strong>${escapeHTML(operationName(latest.operation))}</strong><small>${escapeHTML(inputSummary)} · 输入模态 ${escapeHTML(route.modalities.map(modalityLabel).join(' / ') || '未记录')}</small></div><div><span>${completed ? '交付依据' : '推进依据'}</span><strong>${escapeHTML(decision)}</strong><small>${escapeHTML(outputSummary)}</small></div>`;
}

function renderAgentContract(agent, invocations = window.__latestState?.agent_invocations || []) {
  const contract=agentContracts[agent]||{name:'研究总控',input:'用户问题与已有状态',output:'调度决策与结构化产物',gate:'所有阶段产物均可检查'};
  const calls=invocations.filter(item=>item.agent_id===agent);
  const latest=calls.at(-1);
  const providerCalls=providerCallLabel(calls);
  const actual=latest?`${calls.length} 条角色执行记录 · ${providerCalls} · 最近 ${invocationStatus(latest.status)}`:'尚无后端角色执行记录';
  const route=latest?invocationModelRoute(latest):modelRouteFor(agent,window.__latestState?.methodology||{});
  const routeState=latest?'本次实际模型':'本次已保存的配置';
  const inputSummary=runtimeSummary(latest?.input_summary,latest?.operation,'input');
  const outputSummary=runtimeSummary(latest?.output_summary,latest?.operation,'output');
  $('agentContract').innerHTML=`<div><span>当前角色</span><strong>${escapeHTML(contract.name)}</strong><p>${escapeHTML(actual)}</p><small>${escapeHTML(routeState)}：${escapeHTML(modelRouteLabel(route))}</small><button type="button" class="contract-audit-button" data-agent-audit="${escapeHTML(agent)}">打开该角色的完整审计</button></div><div><span>职责输入</span><p>${escapeHTML(contract.input)}</p>${inputSummary?`<small>本次拿到：${escapeHTML(inputSummary)}</small>`:''}</div><div><span>阶段交付</span><p>${escapeHTML(contract.output)}</p>${outputSummary?`<small>本次交付：${escapeHTML(outputSummary)}</small>`:''}</div><div><span>阶段检查</span><p>${escapeHTML(contract.gate)}</p>${latest?.quality_gate_statuses?.length?`<small>实际结果：${escapeHTML(latest.quality_gate_statuses.map(gateStatusName).join(' / '))}</small>`:''}</div>`;
  $('agentContract').querySelector('[data-agent-audit]')?.addEventListener('click', () => showAgentAudit(agent, invocations, window.__latestEvents || [], window.__latestAudit || null));
}

function captureAuditFrame() {
  if (!auditCurrentFrame) return null;
  const fragment = document.createDocumentFragment();
  const content = $('auditContent');
  while (content.firstChild) fragment.appendChild(content.firstChild);
  return {
    ...auditCurrentFrame,
    fragment,
    scrollTop:$('auditDialog').scrollTop,
  };
}

function updateAuditNavigation() {
  const back = $('auditBack');
  const trail = $('auditTrail');
  if (back) {
    back.hidden = auditNavigationStack.length === 0;
    back.disabled = auditNavigationStack.length === 0;
  }
  if (trail) {
    const titles = [...auditNavigationStack.map(item => item.title), auditCurrentFrame?.title]
      .filter(Boolean)
      .slice(-3);
    trail.textContent = titles.length ? `当前路径：${titles.join(' / ')}` : '当前审计';
  }
}

function restorePreviousAuditFrame() {
  const frame = auditNavigationStack.pop();
  if (!frame) return;
  $('auditContent').replaceChildren(frame.fragment);
  $('auditKicker').textContent = frame.kicker;
  $('auditTitle').textContent = frame.title;
  $('auditSubtitle').textContent = frame.subtitle;
  auditCurrentFrame = {
    kicker:frame.kicker,
    title:frame.title,
    subtitle:frame.subtitle,
  };
  updateAuditNavigation();
  requestAnimationFrame(() => {
    $('auditDialog').scrollTop = frame.scrollTop || 0;
    $('auditBack').focus({preventScroll:true});
  });
}

function resetAuditNavigation(restoreFocus = true) {
  auditNavigationStack = [];
  auditCurrentFrame = null;
  updateAuditNavigation();
  if (restoreFocus && auditReturnFocus?.isConnected && typeof auditReturnFocus.focus === 'function') {
    auditReturnFocus.focus({preventScroll:true});
  }
  auditReturnFocus = null;
}

function openAuditDialog(kicker, title, subtitle, content) {
  const dialog = $('auditDialog');
  const alreadyOpen = Boolean(dialog.open || dialog.hasAttribute('open'));
  if (alreadyOpen && auditCurrentFrame) {
    const frame = captureAuditFrame();
    if (frame) auditNavigationStack.push(frame);
  } else if (!alreadyOpen) {
    auditNavigationStack = [];
    auditReturnFocus = document.activeElement && typeof document.activeElement.focus === 'function'
      ? document.activeElement
      : null;
  }
  $('auditKicker').textContent = humanizeAuditText(kicker);
  $('auditTitle').textContent = humanizeAuditText(title);
  $('auditSubtitle').textContent = humanizeAuditText(subtitle);
  $('auditContent').innerHTML = content;
  humanizeVisibleCopy($('auditContent'));
  auditCurrentFrame = {kicker:humanizeAuditText(kicker), title:humanizeAuditText(title), subtitle:humanizeAuditText(subtitle)};
  updateAuditNavigation();
  dialog.scrollTop = 0;
  if (!alreadyOpen && typeof dialog.showModal === 'function') {
    dialog.showModal();
  } else if (!alreadyOpen) {
    dialog.setAttribute('open', '');
  }
}

function showAgentAudit(agent, invocations = [], events = [], audit = window.__latestAudit || null) {
  const contract = agentContracts[agent] || {name: agent, input: '未记录', output: '未记录', gate: '未记录'};
  const calls = invocations.filter(item => item.agent_id === agent);
  const agentEvents = events.filter(event => nodeAgents[event.node] === agent);
  const artifacts = recordedArrayCount(calls,'output_artifact_ids');
  const handoffs = recordedArrayCount(calls,'handoff_message_ids');
  const failed = calls.filter(item => item.status === 'failed' || item.status === 'cancelled').length;
  const durations = calls.map(item => {
    if (!item.started_at) return null;
    const start = new Date(item.started_at).getTime();
    const end = item.ended_at ? new Date(item.ended_at).getTime() : start;
    return Number.isFinite(start)&&Number.isFinite(end)?Math.max(0,end-start):null;
  }).filter(value=>value!==null);
  const totalMs = durations.length ? durations.reduce((sum,value)=>sum+value,0) : null;
  const last = calls.at(-1);
  const replayedCalls = calls.filter(item => item.execution_mode === 'replayed').length;
  const providerCalls = providerCallLabel(calls);
  const stateText = calls.length ? `${invocationStatus(last.status)} · 最近一次第 ${last.attempt ?? '未记录'} 次尝试` : '本次运行没有角色执行记录';
  const callMarkup = calls.length ? calls.map((item, index) => invocationAuditMarkup(item, index, events)).join('') : '<div class="audit-empty">后端没有记录该角色的实际执行，不能把设计中的流程当成本次已经发生的事实。</div>';
  openAuditDialog(
    '角色执行审计',
    `${contract.name} · 完整运行审计`,
    '以下内容来自已保存的角色执行记录和阶段事件；技术编号用于回到原始记录。',
    `<div class="audit-summary-grid"><article><span>本次状态</span><strong>${escapeHTML(stateText)}</strong><small>${calls.length ? `${failed} 次失败/取消` : '尚未参与'}</small></article><article><span>模型服务调用与复用</span><strong>${escapeHTML(providerCalls)}</strong><small>${countText(calls.length||null,' 条角色执行记录')} · ${calls.length?`${replayedCalls} 次复用已保存结果`:'复用次数未记录'} · 累计 ${formatDuration(totalMs)}</small></article><article><span>阶段产物</span><strong>${countText(artifacts,' 个')}</strong><small>${countText(handoffs,' 条计划交接')}</small></article><article><span>阶段事件</span><strong>${countText(agentEvents.length||null,' 条')}</strong><small>${window.__latestEventWindow?.incomplete?'最近事件窗口中的已保存记录':'事件日志中的已保存记录'}</small></article></div>${eventWindowInlineMarkup('该角色事件审计')}<section class="audit-contract"><header><span>角色职责</span><strong>这个角色应该交付什么</strong></header><div class="audit-contract-grid"><article><b>输入</b><p>${escapeHTML(contract.input)}</p></article><article><b>输出</b><p>${escapeHTML(contract.output)}</p></article><article><b>阶段检查</b><p>${escapeHTML(contract.gate)}</p></article></div></section><section class="audit-calls"><header><span>角色执行记录</span><strong>模型服务调用与已保存结果复用</strong><small>一条角色执行记录也可能是本地固定步骤；是否实际调用模型服务以调用次数为准</small></header><div class="audit-call-list">${callMarkup}</div></section>`
  );
  bindAuditLinks(invocations, events, audit);
}

function invocationAuditMarkup(item, index, events) {
  const artifacts = asArray(item.output_artifact_ids);
  const handoffIds = asArray(item.handoff_message_ids);
  const artifactsRecorded = item.__output_artifact_ids_recorded !== false;
  const handoffsRecorded = item.__handoff_message_ids_recorded !== false;
  const gates = item.quality_gate_statuses || [];
  const replayed = item.execution_mode === 'replayed';
  const linkedHandoffs = handoffIds.map(id => `<button type="button" class="audit-link-button" data-audit-handoff="${escapeHTML(id)}">查看计划交接 ${escapeHTML(id.slice(0, 12))}</button>`).join('');
  const eventMatches = events.filter(event => event.payload?.handoff_envelope && handoffIds.includes(event.payload.handoff_envelope.message_id));
  const providerCalls=providerCallCount(item);
  const providerLabel=providerCalls===null?'历史未记录':`${providerCalls} 次`;
  const modelRoute=invocationModelRoute(item);
  const modelLabel=modelRouteLabel(modelRoute);
  const modalityText=modelRoute.modalities.map(modalityLabel).join(' / ') || '未记录';
  const replayLabel=replayed
    ? providerCalls===0 ? '持久化回放；已记录 Provider 路径调用 0 次' : providerCalls===null ? '持久化回放；Provider 路径调用数未记录，不能确认是否再次调用' : `标记为回放，但记录了 ${providerCalls} 次 Provider 路径调用，需人工核查`
    : escapeHTML(item.execution_mode || '历史未记录');
  const inputSummary=runtimeSummary(item.input_summary,item.operation,'input')||'未记录';
  const outputSummary=runtimeSummary(item.output_summary,item.operation,'output')||'未记录';
  const rawInput=item.input_summary&&inputSummary!==item.input_summary?`<dt>原始输入字段</dt><dd class="audit-raw-summary">${escapeHTML(item.input_summary)}</dd>`:'';
  const rawOutput=item.output_summary&&outputSummary!==item.output_summary?`<dt>原始输出字段</dt><dd class="audit-raw-summary">${escapeHTML(item.output_summary)}</dd>`:'';
  return `<details class="audit-call ${escapeHTML(item.status)} ${replayed ? 'replayed' : ''}" ${index === 0 ? 'open' : ''}><summary><span class="audit-call-number">${String(index + 1).padStart(2, '0')}</span><div><strong>${escapeHTML(operationName(item.operation))}${replayed ? ' · 已复用保存结果' : ''}</strong><small>${escapeHTML(modelLabel)} · ${providerLabel} 次模型服务路径调用 · ${escapeHTML(invocationStatus(item.status))} · 尝试 ${item.attempt ?? '未记录'} · ${escapeHTML(invocationDuration(item))}</small></div><b>${artifactsRecorded?`${artifacts.length} 产物`:'产物未记录'}</b></summary><div class="audit-call-body"><div class="audit-call-flow"><span>${escapeHTML(runtimeTypeName(item.input_type || '输入未记录'))}</span><i>→</i><span>${escapeHTML(runtimeTypeName(item.output_type || '输出未记录'))}</span></div><dl><dt>角色执行记录 ID（技术字段）</dt><dd>${escapeHTML(item.invocation_id)}</dd><dt>执行方式</dt><dd>${escapeHTML(replayLabel)}</dd><dt>模型选择</dt><dd>${escapeHTML(modelLabel)} · 技术字段 provider：${escapeHTML(modelRoute.provider || '未记录')} · choice：${escapeHTML(modelRoute.choice || '未记录')}</dd><dt>输入类型</dt><dd>${escapeHTML(modalityText)}；来自已保存的角色执行记录，不由文件扩展名推断</dd><dt>模型服务路径调用数</dt><dd>${providerLabel}；包含离线模拟，是否为外部计费请求以运行开销账本为准</dd><dt>日志中的上一条记录</dt><dd>${escapeHTML(item.previous_in_log_id || '无')}</dd><dt>明确指定的上游执行记录</dt><dd>${escapeHTML(item.parent_invocation_id || '无；日志相邻不自动等于因果')}</dd><dt>输入摘要</dt><dd>${escapeHTML(inputSummary)}</dd>${rawInput}<dt>输出摘要</dt><dd>${escapeHTML(outputSummary)}</dd>${rawOutput}<dt>阶段检查</dt><dd>${gates.length ? gates.map(gateStatusName).map(escapeHTML).join(' · ') : '未记录'}</dd>${item.error ? `<dt>错误</dt><dd class="audit-error">${escapeHTML(item.error)}</dd>` : ''}</dl><div class="audit-id-list"><span>阶段产物</span>${artifactsRecorded?(artifacts.map(id => `<code>${escapeHTML(id)}</code>`).join('') || '<em>已记录 0 个</em>'):'<em>产物字段未记录</em>'}</div><div class="audit-id-list"><span>计划交接</span>${handoffsRecorded?(linkedHandoffs || '<em>已记录 0 个</em>'):'<em>计划交接字段未记录</em>'}</div>${eventMatches.length ? `<small class="audit-event-note">事件日志中找到 ${eventMatches.length} 条对应的计划交接，可继续核对接收确认、校验值和重试编号。</small>` : ''}</div></details>`;
}

function bindAuditLinks(invocations, events, audit = window.__latestAudit || null) {
  document.querySelectorAll('[data-audit-handoff]').forEach(button => button.addEventListener('click', event => {
    event.preventDefault();
    event.stopPropagation();
    showHandoffAudit(button.dataset.auditHandoff, invocations, events, audit);
  }));
}

function showInvocationAudit(item, invocations = [], events = [], audit = window.__latestAudit || null) {
  const contract = agentContracts[item.agent_id] || {name: item.role || item.agent_id, input: '未记录', output: '未记录', gate: '未记录'};
  openAuditDialog('单次角色执行', `${contract.name} · 第 ${item.attempt ?? '未记录'} 次尝试`, '从单次角色执行记录核查输入、输出、交接和阶段检查，不把阶段名称当作执行证据。', `<div class="audit-focus-call">${invocationAuditMarkup(item, 0, events)}</div><button type="button" class="audit-secondary-action" data-audit-agent="${escapeHTML(item.agent_id)}">查看该角色全部执行记录</button>`);
  bindAuditLinks(invocations, events, audit);
  $('auditContent').querySelector('[data-audit-agent]')?.addEventListener('click', () => showAgentAudit(item.agent_id, invocations, events, audit));
}

function showHandoffAudit(messageId, invocations = [], events = [], audit = window.__latestAudit || null) {
  invocations = asArray(invocations);
  events = asArray(events);
  const record = auditHandoffRecords(events, audit).find(item => item.id === String(messageId));
  const envelope = record?.envelope || {};
  const durable = record?.durable || null;
  const event = record?.event || null;
  const producerInvocationId = normalizedId(durable?.producer_invocation_id || envelope.producer_invocation_id);
  const invocation = invocations.find(item => String(item.invocation_id || '') === producerInvocationId) || null;
  const artifacts = handoffArtifactRecords(envelope, audit);
  const inputs = asArray(envelope.input_artifacts);
  const assessment = handoffReceiptAssessment(envelope, events, invocations, audit);
  const receipt = assessment.receipt;
  const producerId = envelope.producer || invocation?.agent_id || '未记录';
  const producer = agentContracts[producerId]?.name || producerId;
  const consumer = handoffRouteTarget(envelope) || '未记录';
  const consumerLabel = handoffConsumerLabel(consumer, envelope, event, events, invocations, audit);
  const proof = handoffProofModel({...record, envelope, assessment}, producerId, receipt?.consumed_by_agent_id || consumer, audit);
  const receiptDetail = assessment.status === 'server_validated'
    ? `${receiptStateLabel(assessment.status)} · 接收角色：${receipt?.consumed_by_agent_id || '未记录'} · 接收时间：${formatTimestamp(receipt?.consumed_at)}`
    : assessment.status === 'field_match'
      ? `信息能对应，待系统确认 · 记录显示由 ${receipt?.consumed_by_agent_id || '未记录角色'} 接收`
      : assessment.status === 'invalid'
        ? `记录不一致，暂不采用 · ${assessment.reasons.join(' ')}`
        : '尚无可验证的接收确认，只能证明发送方创建了计划交接';
  const sourceLabel = durable ? '已保存的任务交接记录' : event ? '阶段事件中的交接记录；没有独立保存行' : '没有找到已保存的交接记录';
  const validationStatus = receipt?.validation_status || receipt?.normalized_status || durable?.server_validation_status || durable?.receipt_status || 'unverified';
  const artifactProofById = new Map(proof.artifactProofs.map(item => [item.artifactId, item]));
  const artifactMarkup = [...inputs.map(item => ({...item, direction:'输入'})), ...artifacts.map(item => ({...item, direction:'输出'}))].map(item => {
    const artifactProof = item.direction === '输出' ? artifactProofById.get(normalizedId(item.artifact_id)) : null;
    const manifestState = item.direction === '输入'
      ? '上游输入引用；完整性由其来源交接记录单独核对'
      : artifactProof?.complete
        ? '已保存产物清单可重新核对：文件、完整性、校验信息和发送方输出清单均能对应'
      : artifactProof
        ? artifactProof.reasons.join(' ')
          : '尚未形成可核对的已保存产物清单';
    return `<article class="${artifactProof?.complete ? 'verified' : item.direction === '输出' ? 'unverified' : 'input'}"><span>${escapeHTML(item.direction)} · ${escapeHTML(item.kind || '类型未记录')}</span><strong>${escapeHTML(item.artifact_id || 'ID 未记录')}</strong><small>版本号（技术字段）${escapeHTML(item.revision ?? '未记录')} · 发送执行记录 ID ${escapeHTML(item.producer_invocation_id || producerInvocationId || '未记录')}</small><p>${item.content_uri ? `保存内容的位置：${escapeHTML(item.content_uri)} · ${formatBytes(item.byte_length)}` : '历史产物没有记录可重新计算的内容位置'}<br>${escapeHTML(item.media_type || '媒体类型未记录')} · ${escapeHTML(item.canonicalization || '保存规则未记录')}<br>${escapeHTML(manifestState)}</p><code>${escapeHTML(item.checksum || '校验值未记录')}</code>${item.artifact_id && item.content_uri ? `<button type="button" data-artifact-snapshot="${escapeHTML(item.artifact_id)}">打开已保存内容并重新计算校验值</button>` : ''}</article>`;
  }).join('') || '<div class="audit-empty">该投递没有可关联的输入或输出产物。</div>';
  openAuditDialog(
    '任务交接记录',
    '任务交接、接收确认与完整性检查',
    '逐项确认同一条交接记录的发送方、接收方、系统确认、阶段检查和已保存产物。计划接收方、日志先后或单独的校验值，都不能单独证明已经接收；原始技术字段在下方保留。',
    `<div class="handoff-audit-hero ${escapeHTML(assessment.status)}"><strong>${escapeHTML(producer)} <i>→</i> ${escapeHTML(consumerLabel)}</strong><span>${escapeHTML(messageId)}</span></div><div class="handoff-strong-verdict ${proof.strong?'passed':'incomplete'}"><span>本条交接的核对结果</span><strong>${proof.strong?'这条交接已确认且可复查':'这条交接的材料还不完整'}</strong><small>${proof.strong?'发送方、接收方、阶段检查和全部已保存产物，都能在同一条交接记录中对应。':escapeHTML(proof.proofReasons.join(' ') || '核对所需的信息不完整。')}</small></div><div class="receipt-verdict ${escapeHTML(assessment.status)}"><b>${escapeHTML(receiptStateLabel(assessment.status))}</b><p>${escapeHTML(assessment.reasons.join(' ') || receiptDetail)}</p><small>人工核验：${escapeHTML(assessment.manualCheck)}</small></div>${handoffProofChecklistMarkup(proof)}<dl class="audit-detail-list"><dt>记录来源</dt><dd>${escapeHTML(sourceLabel)}</dd><dt>交接编号（技术字段 message_id）</dt><dd class="audit-mono">${escapeHTML(messageId)}</dd><dt>发送执行记录 ID（技术字段）</dt><dd>${escapeHTML(producerInvocationId || invocation?.invocation_id || '未记录')}${invocation ? ` <button type="button" class="audit-link-button inline" data-handoff-producer-invocation="${escapeHTML(invocation.invocation_id)}">打开发送方执行记录</button>` : ''}</dd><dt>追踪与运行编号（技术字段）</dt><dd>${escapeHTML(envelope.trace_id || durable?.trace_id || '未记录')} / ${escapeHTML(envelope.run_id || durable?.run_id || window.__latestState?.run_id || '未记录')}</dd><dt>计划接收角色</dt><dd>${escapeHTML(envelope.intended_consumer || consumer)}；计划路线不等于已经接收</dd><dt>计划中的工作步骤</dt><dd>${escapeHTML(envelope.route_target || durable?.route_target || '历史未记录')}</dd><dt>接收确认状态</dt><dd><b class="receipt-state ${escapeHTML(assessment.status)}">${escapeHTML(receiptStateLabel(assessment.status))}</b> · 技术状态 ${escapeHTML(validationStatus)}</dd><dt>实际接收依据</dt><dd>${escapeHTML(receiptDetail)}${assessment.consumerInvocation ? ` <button type="button" class="audit-link-button inline" data-handoff-consumer-invocation="${escapeHTML(assessment.consumerInvocation.invocation_id)}">打开接收方执行记录</button>` : ''}</dd><dt>创建 / 接收时间</dt><dd>${escapeHTML(formatTimestamp(envelope.created_at || durable?.created_at || event?.created_at))} / ${escapeHTML(formatTimestamp(receipt?.consumed_at))}</dd><dt>尝试次数</dt><dd>${escapeHTML(envelope.attempt ?? invocation?.attempt ?? '未记录')}</dd><dt>去重编号（技术字段）</dt><dd class="audit-mono">${escapeHTML(envelope.idempotency_key || durable?.idempotency_key || '未记录')}</dd><dt>阶段检查</dt><dd>${escapeHTML(gateStatusName(envelope.quality_gate?.status || 'unknown'))} · ${escapeHTML(envelope.quality_gate?.rule || '未记录')}</dd><dt>检查结果说明</dt><dd>${escapeHTML(envelope.quality_gate?.reason || '未记录')}</dd></dl><section class="artifact-audit"><header><span>已保存产物的逐项核对</span><strong>输入 ${inputs.length} 个 · 输出 ${artifacts.length} 个 · 可完整核对 ${proof.manifestValidCount}/${proof.artifactProofs.length || 0}</strong><small>逐项比较内容校验值、元数据校验值、交接编号、发送方、运行编号、文件是否存在以及保存状态。</small></header><div class="artifact-audit-list">${artifactMarkup}</div></section>`,
  );
  $('auditContent').querySelectorAll('[data-artifact-snapshot]').forEach(button=>button.addEventListener('click',()=>showArtifactSnapshot(button.dataset.artifactSnapshot)));
  $('auditContent').querySelector('[data-handoff-producer-invocation]')?.addEventListener('click', event => {
    const item = invocations.find(value => value.invocation_id === event.currentTarget.dataset.handoffProducerInvocation);
    if (item) showInvocationAudit(item, invocations, events, audit);
  });
  $('auditContent').querySelector('[data-handoff-consumer-invocation]')?.addEventListener('click', event => {
    const item = invocations.find(value => value.invocation_id === event.currentTarget.dataset.handoffConsumerInvocation);
    if (item) showInvocationAudit(item, invocations, events, audit);
  });
}

function openCurrentRecoveryAudit() {
  const recovery = window.__latestRecoveryAudit;
  const receiptId = recovery?.receipt?.idempotency_key;
  if (receiptId) {
    showResumeAudit(receiptId);
    return;
  }
  openAuditDialog('RESUME RECEIPT', '恢复链路无法核验', '页面没有拿到与 resume_transition 对应的 durable receipt。', '<div class="audit-empty">先查看协议控制面和主时间线；不要在缺少回执时把本次状态当作已完成。</div>');
}

function showResumeAudit(receiptId) {
  const normalizedIdValue = normalizedId(receiptId);
  const audit = window.__latestAudit || {};
  const protocolAudit = window.__latestProtocolAudit || {};
  const receipt = audit.resumeReceiptById?.get(normalizedIdValue)
    || asArray(protocolAudit.resume_receipts).map(normalizeResumeReceipt).find(item => item.idempotency_key === normalizedIdValue);
  if (!receipt) {
    openAuditDialog('RESUME RECEIPT', '恢复回执不可用', '页面收到恢复引用，但当前响应没有对应的 durable receipt。', '<div class="audit-empty">不能把 state.resume_transition 单独当作恢复执行证明；请重新加载运行审计。</div>');
    return;
  }
  const worker = audit.worker?.length ? audit.worker : asArray(protocolAudit.worker);
  const recovery = window.__latestRecoveryAudit;
  const verdict = recovery?.receipt?.idempotency_key === receipt.idempotency_key
    ? recovery
    : {consistent:true, reason:'当前打开的是历史恢复回执，未将它与页面当前 durable 状态强行合并。'};
  openAuditDialog(
    'RESUME RECEIPT',
    '恢复执行、fence 与 worker 审计',
    '授权、claim、worker 启动、恢复 handoff 和终态必须由同一回执与 fence 串起来；owner token 永不在浏览器中展示。',
    `<div class="resume-audit-verdict ${verdict.consistent ? 'consistent' : 'conflict'}"><b>${verdict.consistent ? '当前恢复链路可与页面状态对应' : '当前恢复链路无法核验'}</b><p>${escapeHTML(verdict.reason || '核验说明未记录')}</p></div>${resumeReceiptMarkup(receipt, worker)}`,
  );
  bindResumeTransitionLinks($('auditContent'));
}

function bindResumeTransitionLinks(root) {
  const invocations = asArray(window.__latestState?.agent_invocations);
  const events = asArray(window.__latestEvents);
  const audit = window.__latestAudit || null;
  root.querySelectorAll('[data-resume-handoff]').forEach(button => button.addEventListener('click', event => {
    event.preventDefault();
    event.stopPropagation();
    showHandoffAudit(button.dataset.resumeHandoff, invocations, events, audit);
  }));
  root.querySelectorAll('[data-resume-invocation]').forEach(button => button.addEventListener('click', event => {
    event.preventDefault();
    event.stopPropagation();
    const invocation = invocations.find(item => item.invocation_id === button.dataset.resumeInvocation);
    if (invocation) showInvocationAudit(invocation, invocations, events, audit);
  }));
}

async function showArtifactSnapshot(artifactId){
  const snapshot=await getJSON(`/api/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}`);
  const artifact=snapshot.artifact||{};
  const matched=artifact.checksum===snapshot.recomputed_sha256&&Number(artifact.byte_length)===Number(snapshot.recomputed_bytes);
  $('snapshotTitle').textContent=`阶段产物 ${artifactId}`;
  $('snapshotMeta').innerHTML=`<span class="snapshot-integrity ${matched?'matched':'mismatched'}">${matched?'现场重算与信封记录一致':'现场重算不一致，产物可能损坏'}</span><span>${escapeHTML(snapshot.recomputed_sha256)}</span><span>${formatBytes(snapshot.recomputed_bytes)} · ${escapeHTML(artifact.canonicalization||'规范化规则未记录')}</span>`;
  $('snapshotText').textContent=snapshot.canonical_json||'';
  $('snapshotDialog').showModal();
}

function formatDuration(milliseconds) {
  const value = finiteValue(milliseconds);
  if (value === null) return '未记录';
  if (value === 0) return '0 ms';
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(1)} s`;
}

function renderAgentExecution(invocations) {
  invocations=asArray(invocations);
  if (!invocations.length) {
    $('agentExecution').innerHTML='<div class="execution-empty">等待后端记录真实 Agent 调用；这里不会根据页面阶段虚构协作过程。</div>';
    return;
  }
  const replayedInvocations = invocations.filter(item => item.execution_mode === 'replayed');
  const providerCalls = providerCallLabel(invocations);
  const visibleStart=executionExpanded ? 0 : Math.max(0,invocations.length-18);
  const visibleInvocations=invocations.slice(visibleStart);
  const cards=visibleInvocations.map((item,index)=>{
    const globalIndex=visibleStart+index+1;
    const contract=agentContracts[item.agent_id]||{name:item.role||item.agent_id||'历史角色'};
    const duration=invocationDuration(item);
    const artifacts=(item.output_artifact_ids||[]).length;
    const replayed=item.execution_mode==='replayed';
    const route=invocationModelRoute(item);
    const inputSummary=runtimeSummary(item.input_summary,item.operation,'input')||runtimeTypeName(item.input_type)||'输入未记录';
    const outputSummary=runtimeSummary(item.output_summary,item.operation,'output')||runtimeTypeName(item.output_type)||'等待输出';
    return `<button class="invocation-card ${escapeHTML(item.status||'unknown')} ${replayed?'replayed':''}" data-invocation-agent="${escapeHTML(item.agent_id||'')}" data-invocation-id="${escapeHTML(item.invocation_id||'')}" aria-label="第 ${globalIndex} 条，共 ${invocations.length} 条：${escapeHTML(contract.name)}，${escapeHTML(modelRouteLabel(route))}，打开这次角色执行记录"><span class="invocation-order">${String(globalIndex).padStart(3,'0')}</span><div><strong>${escapeHTML(contract.name)}</strong><span class="invocation-model">${escapeHTML(modelRouteLabel(route))}</span><b>${escapeHTML(operationName(item.operation))}${replayed?' · 已复用结果':''}</b><small>${replayed?'未再次调用能力接口 · ':''}${escapeHTML(invocationStatus(item.status))} · 尝试 ${escapeHTML(item.attempt??'未记录')} · ${escapeHTML(duration)}</small><p>${escapeHTML(inputSummary)} → ${escapeHTML(outputSummary)}</p><em>输入 ${escapeHTML(route.modalities.map(modalityLabel).join(' / ') || '模态未记录')} · ${artifacts} 个阶段产物 · ${asArray(item.handoff_message_ids).length} 条计划交接</em>${item.error?`<mark>${escapeHTML(item.error)}</mark>`:''}</div></button>`;
  }).join('<i class="invocation-arrow">→</i>');
  const windowText=invocations.length>18
    ? executionExpanded
      ? `已展开全部 ${invocations.length} 条角色执行记录。`
      : `最近 ${visibleInvocations.length} / ${invocations.length} 条角色执行记录；已截断更早记录。`
    : `共 ${invocations.length} 条角色执行记录。`;
  const expandAction=invocations.length>18?`<button type="button" class="record-expand-toggle" data-execution-expand aria-expanded="${String(executionExpanded)}">${executionExpanded?'收起更早调用':`展开全部 ${invocations.length} 条调用`}</button>`:'';
  $('agentExecution').innerHTML=`<div class="execution-head"><div><span>ROLE EXECUTION TRACE</span><strong>${windowText}全局编号 · ${escapeHTML(providerCalls)} · ${replayedInvocations.length} 次复用已保存结果${window.__latestEventWindow?.incomplete?' · 事件历史仅为最近窗口':''}</strong></div><div>${expandAction}<button type="button" data-execution-scroll="-1" aria-label="向前查看更早的角色执行记录">←</button><button type="button" data-execution-scroll="1" aria-label="向后查看更新的角色执行记录">→</button></div></div><div class="execution-track">${cards}</div>`;
  document.querySelectorAll('[data-invocation-agent]').forEach(button=>button.addEventListener('click',()=>{
    const item = invocations.find(value => value.agent_id === button.dataset.invocationAgent && value.invocation_id === button.dataset.invocationId) || invocations.find(value => value.agent_id === button.dataset.invocationAgent);
    if (item) showInvocationAudit(item, invocations, window.__latestEvents || [], window.__latestAudit || null);
  }));
  document.querySelectorAll('[data-execution-scroll]').forEach(button=>button.addEventListener('click',()=>document.querySelector('.execution-track')?.scrollBy({left:Number(button.dataset.executionScroll)*520,behavior:reducedMotion.matches?'auto':'smooth'})));
  document.querySelector('[data-execution-expand]')?.addEventListener('click',()=>{
    executionExpanded=!executionExpanded;
    renderAgentExecution(invocations);
  });
}

function renderCollaborationMap(state, events, runStatus, audit = window.__latestAudit || null) {
  const invocations = asArray(state.agent_invocations);
  const steps = agentOrder.map((agent, index) => {
    const runtime = agentRuntimeEvidence(agent, invocations, events);
    const calls = runtime.calls;
    const latest = runtime.latest;
    const eventCount = runtime.events.length;
    const artifacts = runtime.artifactIds.length>0?runtime.artifactIds.length:recordedArrayCount(calls,'output_artifact_ids');
    const handoffs = recordedArrayCount(calls,'handoff_message_ids');
    const status = runtime.status;
    const label = {waiting:'等待输入', running:'正在执行', blocked:'需要处理', done:'阶段完成', observed:'有阶段记录'}[status];
    const route = latest ? invocationModelRoute(latest) : modelRouteFor(agent, state.methodology || {});
    const summary = runtimeSummary(latest?.output_summary,latest?.operation,'output') || runtimeSummary(latest?.input_summary,latest?.operation,'input') || (eventCount ? `记录 ${eventCount} 个阶段事件` : '尚无真实运行记录');
    return `<button type="button" class="collaboration-step ${status}" data-collaboration-agent="${agent}" aria-label="查看${escapeHTML(agentContracts[agent].name)}详情；${escapeHTML(modelRouteLabel(route))}；依据 ${escapeHTML(runtime.statusReason)}"><span class="collaboration-seq">0${index + 1}</span><span class="collaboration-role"><b>${escapeHTML(agentContracts[agent].name)}</b><small>${escapeHTML(label)} · ${escapeHTML(modelRouteLabel(route,{includeModel:false}))}</small></span><p>${escapeHTML(truncate(summary, 64))}</p><span class="collaboration-counts"><i>${countText(calls.length||null,' 调用')}</i><i>${countText(eventCount||null,' 事件')}</i><i>${countText(artifacts,' 产物')}</i><i>${countText(handoffs,' 计划投递')}</i></span></button>`;
  }).join('<span class="collaboration-arrow" aria-hidden="true">→</span>');
  const gap = state.closure?.gaps?.[0];
  const failure = (state.failures || []).at(-1);
  const replayList = Array.isArray(state.operation_replays) ? state.operation_replays : Array.isArray(state.operation_replay_details) ? state.operation_replay_details : null;
  const replayCount = replayList === null ? null : replayList.length;
  const replayDetails = Array.isArray(state.operation_replay_details) ? state.operation_replay_details : [];
  const handoffItems = auditHandoffRecords(events, audit).map((record, index) => {
    const envelope = record.envelope || {};
    const producerInvocationId = normalizedId(record.durable?.producer_invocation_id || envelope.producer_invocation_id);
    const item = invocations.find(value => String(value.invocation_id || '') === producerInvocationId)
      || invocations.find(value => asArray(value.handoff_message_ids).map(String).includes(record.id));
    return {
      ...record,
      index,
      item:item || null,
      assessment:handoffReceiptAssessment(envelope, events, invocations, audit),
    };
  });
  let loopLabel = '正向研究链路';
  const completedRoles = agentOrder.filter(agent => agentRuntimeEvidence(agent, invocations, events).status === 'done').length;
  let loopDetail = runStatus === 'completed' ? `本次运行已完成；${completedRoles}/6 个角色留下成功执行记录，最终回答仍可沿角色执行、交接、来源和证据逐项核对。` : '阶段检查未通过时，完整性审查或引用核验角色会把明确缺口交回检索角色；没有发生的设计路线不会标成实际流转。';
  let loopClass = runStatus === 'completed' ? 'passed' : 'open';
  if (failure || gap) {
    loopClass = 'repair';
    loopLabel = failure ? '恢复回路已记录' : '补证回路已开启';
    loopDetail = failure ? `${failureName(failure.type)}：${humanizeAuditText(failure.reason || failure.instruction || '保留现场并定向恢复')}` : `${gapName(gap.type)}：${humanizeAuditText(gap.description || '当前材料尚未通过交付前检查')}`;
  }
  if (replayCount !== null && replayCount > 0) loopDetail += ` 本次恢复复用了 ${replayCount} 个已保存操作结果，没有重复调用对应模型服务。`;
  if (replayCount === null) loopDetail += ' 回放计数未记录，不能把缺失账目当作 0。';
  const replayLedger=replayDetails.length?`<details class="replay-ledger"><summary>查看 ${replayDetails.length} 条持久化回放与费用账目</summary><div>${replayDetails.map(item=>`<article><span>${escapeHTML(operationName(item.node))} · ${escapeHTML(operationKindName(item.kind))}</span><b>操作记录 ${escapeHTML(String(item.operation_key||'未记录').slice(0,16))}</b><p>原结果完成：${escapeHTML(formatTimestamp(item.original_completed_at))}<br>本次回放：${escapeHTML(formatTimestamp(item.replayed_at))}</p><small>历史尝试 ${replayFieldLabel(item,'attempt_count',' 次')} · 原实际调用 ${replayFieldLabel(item,'original_model_calls',' 次')} · ${replayTokenLabel(item)} · ${finiteValue(item.estimated_cost_usd)===null?'费用未记录':`$${finiteValue(item.estimated_cost_usd).toFixed(6)}`}<br><strong>${finiteValue(item.replay_provider_calls)===null?'本次服务商调用数未记录，不能确认是否重复请求':finiteValue(item.replay_provider_calls)===0?'本次服务商调用 0 次，没有重复请求':`本次服务商调用 ${finiteValue(item.replay_provider_calls)} 次，不能标作纯回放`}</strong></small></article>`).join('')}</div></details>`:'';
  const receiptCounts = handoffItems.reduce((counts, record) => {
    counts[record.assessment.status] = (counts[record.assessment.status] || 0) + 1;
    return counts;
  }, {});
  const handoffFlow = handoffItems.length ? `<div class="handoff-flow"><header><div><span>已保存的任务交接</span><strong>谁在何时把什么交给谁</strong></div><small>${receiptCounts.server_validated || 0} 已确认 · ${receiptCounts.field_match || 0} 信息相符待确认 · ${receiptCounts.unverified || 0} 尚未确认 · ${receiptCounts.invalid || 0} 暂不采用；点击任一卡片人工核对</small></header><div class="handoff-track">${handoffItems.map(({id,index,item,envelope,event,durable,assessment}) => {
    const fromId=envelope?.producer || item?.agent_id || '发送方未解析';
    const from=agentContracts[fromId]?.name || fromId;
    const to=handoffConsumerLabel(handoffRouteTarget(envelope),envelope,event,events,invocations,audit);
    const artifacts=handoffArtifactRecords(envelope,audit);
    const artifactKinds=[...new Set(artifacts.map(value=>value?.kind).filter(Boolean))];
    const gate=gateStatusName(envelope?.quality_gate?.status||'unknown');
    const recordedAt=formatTimestamp(envelope?.created_at || durable?.created_at || event?.created_at);
    const outputSummary=runtimeSummary(item?.output_summary,item?.operation,'output') || (artifactKinds.length ? `交付 ${artifactKinds.join('、')}` : '交付内容摘要未记录；请展开查看已保存产物');
    const receipt=assessment.receipt;
    const receiptText=assessment.status==='server_validated'
      ? `系统已确认：${receipt?.consumed_by_agent_id || '角色未记录'} / 接收执行记录 ${receipt?.consumed_by_invocation_id || '未记录'}`
      : assessment.status==='field_match'
        ? `信息相符：接收执行记录 ${receipt?.consumed_by_invocation_id || '未记录'}；系统尚未确认`
        : assessment.status==='invalid' ? `暂不采用：${assessment.reasons[0] || '记录信息冲突'}` : '尚无可验证的接收记录';
    return `<button type="button" class="handoff-card receipt-${escapeHTML(assessment.status)}${assessment.status==='server_validated'?' verified':''}" data-handoff-agent="${escapeHTML(item?.agent_id || fromId)}" data-handoff-id="${escapeHTML(id)}" aria-label="第 ${index+1} 条交接：${escapeHTML(from)}到${escapeHTML(to)}；${escapeHTML(receiptStateLabel(assessment.status))}；打开人工核对"><span>${String(index+1).padStart(2,'0')} · ${escapeHTML(recordedAt)} · ${durable?'已保存记录':'阶段事件中的记录'}</span><strong>${escapeHTML(from)} <i>→</i> ${escapeHTML(to)}</strong><p>${escapeHTML(outputSummary)}</p><small>${artifacts.length} 个关联产物 · 阶段检查 ${escapeHTML(gate)} · ${escapeHTML(receiptStateLabel(assessment.status))}</small><em>${escapeHTML(receiptText)} · 交接编号 ${escapeHTML(id)}</em></button>`;
  }).join('<i class="handoff-arrow" aria-hidden="true">→</i>')}</div></div>` : '<div class="handoff-empty">没有已保存的任务交接记录或阶段事件中的交接内容；页面不会用设计流程伪造实际流转。</div>';
  $('collaborationMap').innerHTML = `${eventWindowInlineMarkup('协作事件')}<div class="collaboration-head"><div><span>协作状态</span><strong>先看六个角色当前状态，再沿交接轨迹核查产物</strong></div><div class="collaboration-legend"><i class="running">执行</i><i class="done">完成</i><i class="blocked">需处理</i></div></div><div class="collaboration-track">${steps}</div><div class="collaboration-loop ${loopClass}"><span>${escapeHTML(loopLabel)}</span><p>${escapeHTML(loopDetail)}</p><b>${gap || failure ? '↶ 返回补充材料' : '→ 通过阶段检查后继续'}</b></div>${handoffFlow}${replayLedger}`;
  document.querySelectorAll('[data-collaboration-agent]').forEach(button => button.addEventListener('click', () => {
    renderAgentContract(button.dataset.collaborationAgent, invocations);
    document.querySelectorAll('.collaboration-step').forEach(step => step.classList.toggle('selected', step === button));
    showAgentAudit(button.dataset.collaborationAgent, invocations, events, audit);
  }));
  document.querySelectorAll('[data-handoff-agent]').forEach(button => button.addEventListener('click', () => {
    document.querySelectorAll('.handoff-card').forEach(card => card.classList.toggle('selected', card === button));
    showHandoffAudit(button.dataset.handoffId, invocations, events, audit);
  }));
}

function renderFailures(failures) {
  $('failurePanel').classList.toggle('hidden', !failures.length);
  $('failurePanel').setAttribute('role', failures.length ? 'alert' : 'region');
  if (!failures.length) return;
  $('failureList').innerHTML=failures.map((item,index)=>`<details ${index===failures.length-1?'open':''}><summary><span>${String(index+1).padStart(2,'0')}</span><strong>${escapeHTML(failureName(item.type))}</strong><b>${item.retryable?'可定向恢复':'需要人工检查'}</b></summary><p>${escapeHTML(item.reason||'未提供错误原因')}</p><dl><dt>建议返回节点</dt><dd>${escapeHTML(item.next_node||'未指定')}</dd><dt>恢复指令</dt><dd>${escapeHTML(item.instruction||'保留现场并人工审查')}</dd>${item.url?`<dt>相关页面</dt><dd>${escapeHTML(item.url)}</dd>`:''}${item.query?`<dt>相关查询</dt><dd>${escapeHTML(item.query)}</dd>`:''}</dl></details>`).join('');
}

function requestResumeConfirmation(state, ambiguousFailures) {
  const dialog=$('resumeDialog');
  const staleRecovery=['initialized','perceiving','planning','running','drafting'].includes(String(state?.status||''));
  const gaps=asArray(asObject(state?.closure).gaps);
  const failureNode=asArray(state?.failures).at(-1)?.next_node;
  const resumeNode=state?.next_node||failureNode||'历史字段未记录';
  const limits=asObject(state?.budget_limits);
  const counters=asObject(state?.counters);
  const budgetLine=(label,limitKey,counterKey,extensionKey)=>{
    const currentLimit=finiteValue(limits[limitKey]);
    const used=finiteValue(counters[counterKey]);
    const extension=resumeBudgetExtension[extensionKey];
    const current=currentLimit===null?'历史字段未记录':`${used===null?'历史字段未记录':used}/${currentLimit}`;
    const next=currentLimit===null?'历史字段未记录':String(currentLimit+extension);
    return `<div><strong>${label}</strong><span>当前已用 / 上限：${current} · 本次新增：+${extension} · 确认后上限：${next}</span></div>`;
  };
  const gapMarkup=gaps.length?gaps.map((gap,index)=>`<p>${String(index+1).padStart(2,'0')} · ${escapeHTML(gapName(gap?.type))}${gap?.slot_id?` · slot ${escapeHTML(gap.slot_id)}`:''}：${escapeHTML(gap?.description||'历史字段未记录')}</p>`).join(''):'<p>当前状态未记录具体缺口；仍会从持久化恢复节点继续。</p>';
  const ambiguousMarkup=ambiguousFailures.length?`<p class="resume-ambiguous">${ambiguousFailures.length} 个模型请求状态未知，确认后可能产生重复费用。${ambiguousFailures.map(item=>item?.operation_key).filter(Boolean).length?`<br>${escapeHTML(ambiguousFailures.map(item=>item?.operation_key).filter(Boolean).join('\n'))}`:''}</p>`:'';
  const recoveryMarkup=staleRecovery?'<section><strong>中断后恢复</strong><span>上一次执行没有留下结束记录。确认后会先把“结果未知”的外部请求记为人工授权重试，再从保存的节点继续；本次不会额外提高预算。</span></section>':'';
  const budgetMarkup=staleRecovery?'<section><strong>本次预算</strong><span>沿用上一次已经批准的上限；恢复只接续未完成步骤，不在这里新增检索或页面预算。</span></section>':`<section><strong>当前 / 新增预算</strong>${budgetLine('研究轮次','iterations','iterations','additional_iterations')}${budgetLine('搜索调用','search_calls','search_calls','additional_search_calls')}${budgetLine('页面读取','pages','pages_selected','additional_pages')}</section>`;
  $('resumeDialogDetails').innerHTML=`<section><strong>待补缺口（${gaps.length}）</strong>${gapMarkup}</section><section><strong>恢复节点</strong><span>${escapeHTML(operationName(resumeNode))} · ${escapeHTML(resumeNode)}</span></section>${recoveryMarkup}${budgetMarkup}${ambiguousMarkup}`;
  announceLive(`打开继续补证确认：${gaps.length} 个缺口，恢复节点 ${operationName(resumeNode)}。`, `resume-dialog:${resumeNode}:${gaps.length}`, true);
  return new Promise(resolve=>{
    let settled=false;
    const finish=accepted=>{
      if(settled)return;
      settled=true;
      dialog.removeEventListener('cancel',onCancel);
      dialog.removeEventListener('click',onBackdrop);
      $('resumeCancel').removeEventListener('click',onCancelClick);
      $('resumeConfirm').removeEventListener('click',onConfirmClick);
      if(dialog.open)dialog.close();else dialog.removeAttribute('open');
      resolve(accepted);
    };
    const onCancel=event=>{event.preventDefault();finish(false)};
    const onBackdrop=event=>{if(event.target===dialog)finish(false)};
    const onCancelClick=()=>finish(false);
    const onConfirmClick=()=>finish(true);
    dialog.addEventListener('cancel',onCancel);
    dialog.addEventListener('click',onBackdrop);
    $('resumeCancel').addEventListener('click',onCancelClick);
    $('resumeConfirm').addEventListener('click',onConfirmClick);
    if(typeof dialog.showModal==='function')dialog.showModal();else dialog.setAttribute('open','');
    $('resumeConfirm').focus({preventScroll:true});
  });
}

async function resumeRun() {
  const ambiguousFailures=asArray(window.__latestState?.failures).filter(item=>item?.type==='ambiguous_operation');
  const ambiguous=ambiguousFailures.length>0;
  const staleRecovery=['initialized','perceiving','planning','running','drafting'].includes(String(window.__latestState?.status||''));
  if(!await requestResumeConfirmation(window.__latestState||{},ambiguousFailures))return;
  const resumeKey=`fieldnote:resume:${runId}:${window.__latestState?.status||'unknown'}`;
  let resumeRequestId=sessionStorage.getItem(resumeKey);
  if(!resumeRequestId){resumeRequestId=window.crypto?.randomUUID?.()||`${Date.now()}-${Math.random().toString(16).slice(2)}`;sessionStorage.setItem(resumeKey,resumeRequestId)}
  const budgetExtensions=staleRecovery?{}:{...resumeBudgetExtension};
  const resumePayload={resume_request_id:resumeRequestId,confirm_ambiguous_retry:ambiguous||staleRecovery,...budgetExtensions};
  const result=await getJSON(`/api/runs/${encodeURIComponent(runId)}/resume`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(resumePayload)});
  sessionStorage.removeItem(resumeKey);
  const before=asObject(result.budget_before),after=asObject(result.budget_after),used=asObject(result.budget_consumed);
  connectionMode=`已从 ${operationName(result.next_node)} 恢复 · 轮次 ${displayNumber(used.iterations,'历史字段未记录')}/${displayNumber(before.iterations,'历史字段未记录')}→上限 ${displayNumber(after.iterations,'历史字段未记录')}，搜索上限 ${displayNumber(before.search_calls,'历史字段未记录')}→${displayNumber(after.search_calls,'历史字段未记录')}，页面上限 ${displayNumber(before.pages,'历史字段未记录')}→${displayNumber(after.pages,'历史字段未记录')}`;
  renderStatus('queued');
  clearTimeout(pollTimer);
  eventSource?.close();
  startLiveUpdates();
}

function renderBreakdown(closure, methodology = {}) {
  if(!closure){$('closureBreakdown').innerHTML='<div class="breakdown-empty">规划完成后展示五个分项的当前值、权重与实际贡献</div>';return}
  const recordedClosure=asObject(closure);
  const weights=asObject(methodology?.closure_score);
  const currentState=window.__latestState||{closure:recordedClosure};
  const requiredRows=slotAuditRows(currentState,false);
  const gateModels=gateConsoleModel(currentState);
  const hardGatePassed=gateModels.length===gateDefinitions.length&&gateModels.every(item=>item.tone==='passed');
  const progress=requiredSlotProgressModel(currentState);
  const requiredCount=progress.required;
  const passedCount=progress.passed;
  const items=[
    ['answer_slot_coverage','回答目标覆盖',recordedClosure.slot_coverage,'必需回答目标中已有证据的比例'],
    ['source_independence','来源独立性',recordedClosure.source_independence,'按 origin 来源簇与 provenance 规则计算；不同域名只是未知上游时的弱 fallback'],
    ['exact_quote_localization','原文与声明一致性',recordedClosure.evidence_entailment,'quote 逐字定位，并检查数字、否定极性与词项覆盖；仍不代表完整语义蕴含概率'],
    ['source_reliability_prior','来源等级',recordedClosure.source_reliability,'官方、论文、参考资料等类型先验'],
    ['conflict_resolution','冲突解决',recordedClosure.conflict_resolution,'候选值和反证是否已经裁决']
  ];
  const failures=asArray(recordedClosure.gate_failures).map(item=>typeof item==='string'?item:(item?.description||item?.type||JSON.stringify(item)));
  const gateTone=hardGatePassed?'passed':gateModels.every(item=>item.tone==='waiting')?'waiting':gateModels.some(item=>item.tone==='unverifiable')?'unverifiable':'blocked';
  const gateMessage=hardGatePassed?'交付前检查已通过':gateTone==='waiting'?'等待逐项检查':gateTone==='unverifiable'?'检查记录不完整，暂不能判断':'交付前检查尚未完成，先补材料';
  const missingAuditNote=gateModels.some(item=>item.unavailable>0)?'至少一个必需目标未审计或历史字段未记录':'';
  const failureMessage=[failures.map(humanizeAuditText).join('；')||(gateTone==='waiting'?'尚未建立可对齐的必答问题':gateTone==='unverifiable'?'历史运行没有保存完整的逐问题检查记录，当前无法判断':'必答问题覆盖、独立来源、反面材料检查与冲突处理均满足'),missingAuditNote].filter(Boolean).join('；');
  const gateCountText=requiredCount===null||passedCount===null?'未记录 · 不可计算':requiredCount?`${passedCount}/${requiredCount}`:'已记录 0/0 · 不可计算';
  const gates=`<div class="gate-strip ${gateTone}"><strong>${gateMessage}</strong><span>${gateCountText} 必需目标通过</span><p>${escapeHTML(failureMessage)}</p></div>`;
  const methodNote=Object.keys(weights).length?'权重来自本次运行保存的方法说明，可与方法版本和定义编号一起复核。':'本次运行没有保存完成度权重，页面不猜测权重，也不展示虚构的贡献分。';
  $('closureBreakdown').innerHTML=gates+`<p class="breakdown-method-note">${escapeHTML(methodNote)}</p>`+items.map(([key,name,value,explanation])=>{
    const numericValue=finiteValue(value),weight=finiteValue(weights[key]),hasWeight=weight!==null&&weight>=0;
    const contribution=hasWeight&&numericValue!==null?numericValue*weight:null;
    const weightLabel=hasWeight?`权重 ${Math.round(weight*100)}%`:'权重未记录';
    const equation=hasWeight&&numericValue!==null?`${Math.round(numericValue*100)}% × ${Math.round(weight*100)}% = 贡献 ${(contribution*100).toFixed(1)} 分`:numericValue===null?'历史字段未记录，不能验证当前值':'当前值已记录，但缺少本次运行权重，不能复算贡献分';
    const width=numericValue===null?null:Math.round(Math.max(0,Math.min(1,numericValue))*100);
    return `<article class="breakdown-item"><header><span>${escapeHTML(name)}</span><b>${weightLabel}</b></header><strong>${displayPercent(numericValue)}</strong><div class="breakdown-bar"${width===null?' data-unavailable="true"':''} aria-label="${escapeHTML(displayPercent(numericValue))}"><i${width===null?'':' style="width:'+width+'%"'}></i></div><p>${escapeHTML(equation)}<br>${escapeHTML(explanation)}</p></article>`;
  }).join('');
}

function renderSlotGateAudit(closure, contradictionChecks) {
  const state=window.__latestState||{closure:closure||{}};
  const rows=slotAuditRows(state,true);
  const checksList=asArray(contradictionChecks);
  if(!rows.length){$('slotGateAudit').innerHTML='<div class="breakdown-empty">完整性审查完成后展示每个必答问题的检查依据；当前没有可对齐的必答问题记录。</div>';return}
  $('slotGateAudit').innerHTML=rows.map((row,index)=>{
    const audit=row.audit||{};
    const checks=checksList.filter(item=>String(item?.slot_id||'')===row.slotId);
    const weakRecorded=recordedArray(audit,'weak_provenance_evidence_ids');
    const dependentRecorded=recordedArray(audit,'dependent_evidence_ids');
    const weak=weakRecorded||[];
    const dependent=dependentRecorded||[];
    const sourceGate=recordedBoolean(audit,'source_gate_passed');
    const authoritative=recordedBoolean(audit,'authoritative_exception_used');
    const sourceGateText=authoritative===true?'使用单一权威来源例外：已核对来源路径、原始材料、来源等级至少 90 / 100，且该来源确实覆盖这个问题':sourceGate===true?'已达到独立来源数量要求':sourceGate===false?'未达到来源数量要求；网页自称为原始来源不能单独作为例外':'历史字段未记录，暂不能核对来源数量';
    const reasons=recordedArray(audit,'source_counting_reasons');
    const failureReasons=recordedArray(audit,'failure_reasons');
    const supporting=recordedArray(audit,'supporting_evidence_ids');
    const contradicting=recordedArray(audit,'contradicting_evidence_ids');
    const candidates=recordedArray(audit,'candidate_values');
    const originClusters=recordedArray(audit,'origin_clusters');
    const sourceClusters=recordedArray(audit,'source_clusters');
    const origins=originClusters?.length?originClusters:sourceClusters||originClusters;
    const effectiveSource=displayNumber(audit.effective_source_count);
    const requiredSource=displayNumber(audit.required_source_count);
    const quoteGate=recordedBoolean(audit,'exact_quote_gate_passed');
    const contradictionGate=recordedBoolean(audit,'contradiction_checked');
    const conflictGate=recordedBoolean(audit,'conflict_gate_passed');
    const contradictionSummary=checks.map(item=>`${contradictionStatusName(item?.status)||'历史状态未记录'}：${displayNumber(item?.relevant_pages_inspected ?? item?.pages_inspected)}/${displayNumber(item?.pages_inspected)} 页相关/已读`).join('；')||(!row.present?'历史字段未记录 · 没有逐目标反证检索记录':'没有可验证的反证检索记录');
    const checkDetails=checks.map(item=>{
      const relevantValue=item?.relevant_pages_inspected===undefined?item?.pages_inspected:item.relevant_pages_inspected;
      const legacy=item?.relevant_pages_inspected===undefined;
      const relevantSources=recordedArray(item,'relevant_source_ids')||recordedArray(item,'inspected_source_ids')||[];
      const irrelevantSources=recordedArray(item,'irrelevant_source_ids')||[];
      return `<details class="contradiction-trace"><summary>${escapeHTML(contradictionStatusName(item?.status)||'历史状态未记录')} · ${escapeHTML(displayNumber(item?.result_count))} 个结果 · ${escapeHTML(displayNumber(relevantValue))}/${escapeHTML(displayNumber(item?.pages_inspected))} 页相关/已读${legacy?'（历史未区分）':''}</summary><p>${escapeHTML(item?.query_text||'历史字段未记录')}</p><small>${escapeHTML(formatTimestamp(item?.executed_at))}${item?.error?` · ${escapeHTML(item.error)}`:''}</small><div>${relevantSources.map(id=>`<button type="button" data-audit-source="${escapeHTML(id)}">相关 ${escapeHTML(id)}</button>`).join('')||'<span>没有通过目标相关性准入的页面</span>'}${irrelevantSources.map(id=>`<span class="irrelevant-audit-source">已排除 ${escapeHTML(id)}</span>`).join('')}</div></details>`;
    }).join('')||'<p>没有反证检索记录</p>';
    const stateClass=row.passed===true?'passed':row.passed===false?'blocked':'unverifiable';
    const headline=row.required?(!row.present?'检查记录未保存':row.passed===true?'交付前检查已通过':row.passed===false?'需要补材料':'检查记录不完整，暂不能判断'):row.present?'可选问题的检查记录':'可选问题尚无检查记录';
    const action=row.required?(!row.present||row.passed!==true?'需要继续补材料':'可以进入写作'):'可选问题，不影响必答问题检查';
    const countText=contradicting===null||candidates===null?'历史字段未记录':`${contradicting.length} 条反证；${candidates.length} 个候选陈述`;
    const reasonValues=reasons?.length?reasons:failureReasons||reasons||[];
    const reasonMarkup=reasonValues.map(reason=>`<mark>${escapeHTML(reason)}</mark>`).join('')||'历史运行未记录逐来源判定';
    const originMarkup=(origins||[]).map(value=>`<code>${escapeHTML(value)}</code>`).join('')||'历史字段未记录';
    const supportMarkup=supporting===null?'历史字段未记录 · 不可验证':supporting.map(id=>evidenceReferenceMarkup(id,`支持 ${id}`)).join('')||'无支持';
    const conflictMarkup=contradicting===null?'':contradicting.map(id=>evidenceReferenceMarkup(id,`冲突 ${id}`)).join('');
    const candidateMarkup=(candidates||[]).map((value,candidateIndex)=>`<li><span>${String(candidateIndex+1).padStart(2,'0')}</span><p>${escapeHTML(value)}</p></li>`).join('')||'<li><p>没有记录候选答案文本</p></li>';
    const provenanceCounts=weakRecorded===null||dependentRecorded===null?'来源路径较弱或同源材料的数量没有完整保存':`${weak.length} 条来源路径较弱的材料，${dependent.length} 条同源材料不重复计数`;
    return `<details class="slot-audit-card ${stateClass} ${row.required?'required':'optional'}" data-slot-audit="${escapeHTML(row.slotId)}" tabindex="-1" open><summary><div><span>${headline}${row.required?'':' · 不计入必答问题检查'}</span><strong>${escapeHTML(row.description)}</strong><small>问题编号（技术字段）：${escapeHTML(row.slotId)}${row.present?'':' · 历史字段未记录'}</small></div><b>${action}</b></summary><div class="slot-audit-grid"><div><span>来源数量</span><strong>${escapeHTML(effectiveSource)}/${escapeHTML(requiredSource)}</strong><p>${escapeHTML(sourceGateText)}；${escapeHTML(provenanceCounts)}。</p></div><div><span>原文可定位</span><strong>${quoteGate===null?'历史字段未记录，暂不能核对':quoteGate?'通过':'未通过'}</strong><p>检查引用原文是否能在保存的页面内容中逐字找到；这不等于系统判断该说法绝对为真。</p></div><div><span>反面材料检查</span><strong>${contradictionGate===null?'历史字段未记录，暂不能核对':contradictionGate?'已检查相关页面':'未完成'}</strong><p>${escapeHTML(contradictionSummary)}</p></div><div><span>冲突处理</span><strong>${conflictGate===null?'历史字段未记录，暂不能核对':conflictGate?'通过':'需要处理'}</strong><p>${escapeHTML(countText)}</p></div></div><div class="slot-audit-detail"><div><h4>来源分组（技术 ID）</h4><p>${originMarkup}</p></div><div><h4>支持与相反材料（技术 ID）</h4><p>${supportMarkup}${conflictMarkup}</p></div><div><h4>来源计数依据</h4><p>${reasonMarkup}</p></div><div class="candidate-values"><h4>系统比较过的候选答案</h4><ol>${candidateMarkup}</ol><small>候选答案不同不自动等于事实冲突；数值和人物归属使用已记录的规则，其他语义仍需人工复核。</small></div></div><div class="contradiction-audit-list"><h4>反面材料搜索记录</h4>${checkDetails}</div></details>`;
  }).join('');
  bindCitationLinks($('slotGateAudit'));
  document.querySelectorAll('[data-audit-source]').forEach(button=>button.addEventListener('click',()=>{
    const source=asArray(window.__latestState?.sources).find(item=>item?.id===button.dataset.auditSource);
    if(source)inspectSource(source,asArray(window.__latestState?.evidence));
  }));
}

function renderMethodologyMeta(methodology) {
  if (!Object.keys(methodology).length) {
    $('methodologyMeta').textContent='历史运行未记录方法版本，不能证明与当前评分定义完全一致。';
    return;
  }
  const recordedThreshold=finiteValue(methodology.admission_thresholds?.slot_relevance);
  const thresholdLabel=recordedThreshold!==null&&recordedThreshold>=0&&recordedThreshold<=1?`${Math.round(recordedThreshold*100)} / 100`:'历史未记录，当前不可验证';
  const routeNames={perception:'感知',planner:'规划',scout:'检索策略',curator:'证据整理',critic:'交付前检查',writer:'写作',verifier:'独立核验'};
  const routeMarkup=['perception',...agentOrder].map(role=>{
    const route=modelRouteFor(role,methodology);
    const modalities=route.modalities.length?` · 声明输入 ${route.modalities.map(modalityLabel).join('/')}`:'';
    return `<span class="method-route"><b>${escapeHTML(routeNames[role])}</b>${escapeHTML(modelRouteLabel(route))}${escapeHTML(modalities)}<small>${route.deterministic?'本地固定检查，不调用模型服务':'本次已保存的配置；实际执行以角色执行记录为准'}</small></span>`;
  }).join('');
  const summary=routeModelSummary(methodology);
  const profileLabel=summary.isTeam?`${summary.choices.map(providerName).join(' + ')} 角色协作`:modelRouteLabel(summary.routes[0]||{});
  $('methodologyMeta').innerHTML=`<span><b>方法</b>${escapeHTML(methodology.methodology_version||'历史未记录')}</span><span><b>模型配置</b>${escapeHTML(profileLabel||'历史未记录')}</span><span><b>检索入口</b>${escapeHTML(providerName(methodology.search_provider))}</span><span><b>相关性准入</b>${escapeHTML(thresholdLabel)}</span>${routeMarkup}<span><b>抽取契约</b>${escapeHTML(methodology.extractor_contract||'历史未记录')}</span><span><b>写作白名单</b>${escapeHTML(methodology.writer_contract||'历史未记录')}</span><span><b>核验契约</b>${escapeHTML(methodology.verifier_contract||'历史未记录')}</span><span title="${escapeHTML(methodology.metric_definition_hash)}"><b>定义哈希</b>${escapeHTML(methodology.metric_definition_hash?.slice(0,12)||'历史未记录')}</span>`;
}

function renderSlots(slots) {
  slots=asArray(slots);
  if (!slots.length) {
    $('slots').classList.add('empty-state');
    $('slots').textContent='尚未记录研究计划；页面不会用角色设计路线代替实际计划。';
    return;
  }
  $('slots').classList.remove('empty-state');
  $('slots').innerHTML = slots.map((slot,index) => {
    const scoreValue=finiteValue(slot?.confidence);
    const score=scoreValue===null?null:Math.round(Math.max(0,Math.min(1,scoreValue))*100);
    const scoreLabel=scoreValue===null?'历史字段未记录':`${score}`;
    const unavailable=scoreValue===null;
    return `<div class="slot"><span class="slot-index">${String(index+1).padStart(2,'0')}</span><div><strong>${escapeHTML(slot?.description||'历史字段未记录')}</strong><p>${escapeHTML(slot?.value || '智能体正在寻找可验证答案')}</p><span class="slot-status ${slot?.value ? '' : 'pending'}">${slot?.value ? '候选结论' : '调查中'}</span><div class="confidence-bar"${unavailable?' data-unavailable="true"':''} role="img" aria-label="${unavailable?'流程充分度尚未记录，当前不可计算':`流程充分度 ${escapeHTML(scoreLabel)} / 100，不是事实概率`}"><i${unavailable?'':' style="width:'+score+'%"'}></i></div><small class="slot-score-note">${unavailable?'本次运行没有记录该目标的流程充分度，不能把缺失字段当作 0；请查看逐问题检查记录。':'由来源等级、互证、原文定位与冲突处理加权；只用于安排补材料顺序，不能代替交付前检查。'}</small></div><span class="confidence ${unavailable?'unavailable':''}">流程充分度 ${escapeHTML(scoreLabel)}${unavailable?'':' / 100'}<br><small>${unavailable?'不可计算':'非事实概率'}</small></span></div>`;
  }).join('');
}

function renderQueries(queries) {
  queries=asArray(queries);
  if (!queries.length) {
    $('queryRoutes').innerHTML='<span>尚未记录检索路线；页面不会把角色设计路线当成本次已执行查询。</span>';
    return;
  }
  const names={source_targeting:'定向来源',entity_resolution:'实体消歧',contradiction_check:'反证搜索',bridge:'多跳桥接',broad_discovery:'广泛探索'};
  $('queryRoutes').innerHTML=queries.map((query,index)=>`<span class="query-chip"><b>Q${index+1} · ${escapeHTML(names[query.strategy]||'检索')}</b>${escapeHTML(query.text)}</span>`).join('');
}

function renderTimeline(events) {
  events=asArray(events);
  const windowModel=globalThis.window.__latestEventWindow;
  const audit=window.__latestAudit||null;
  if (!events.length) {
    $('timeline').classList.add('empty-state');
    $('timeline').innerHTML=eventWindowInlineMarkup('阶段事件')+'历史运行未记录当前返回的阶段事件。';
    return;
  }
  $('timeline').classList.remove('empty-state');
  const visible=(timelineExpanded ? events : events.slice(-20)).reverse();
  const totalText=windowModel?.incomplete ? (windowModel.total===null?'未记录':windowModel.total) : events.length;
  const windowText=windowModel?.incomplete
    ? windowModel.total===null
      ? `后端返回最近窗口 ${windowModel.returned===null?'未记录':windowModel.returned} 条，但 durable 总数未记录（${eventWindowRange(windowModel)}）；更早事件不在当前页面数据中。`
      : `后端仅返回最近窗口 ${windowModel.returned===null?'未记录':windowModel.returned} / ${totalText} 条（${eventWindowRange(windowModel)}）；更早事件不在当前页面数据中。`
    : events.length>20
      ? timelineExpanded ? `已展开当前响应中的全部 ${events.length} 条阶段事件。` : `最近 ${visible.length} / ${events.length} 条阶段事件；已截断当前响应中更早记录。`
      : `当前响应共 ${events.length} 条阶段事件。`;
  const expandAction=events.length>20?`<button type="button" class="record-expand-toggle timeline" data-timeline-expand aria-expanded="${String(timelineExpanded)}">${timelineExpanded?'收起当前窗口内更早事件':`展开当前窗口内 ${events.length} 条事件`}</button>`:'';
  const numberingNote=windowModel?.first===null?'当前只显示窗口内编号，不能声称全局序号。':'事件编号沿用后端全局顺序。';
  const windowNote=`${eventWindowInlineMarkup('阶段事件')}<span class="record-window-note">${windowText}${numberingNote}${expandAction}</span>`;
  $('timeline').innerHTML=windowNote+visible.map(event=>{const friendly=describeEvent(event||{});const time=formatTimestamp(event?.created_at);const p=asObject(event?.payload);const envelope=asObject(p.handoff_envelope);const hasEnvelope=Boolean(p.handoff_envelope&&typeof p.handoff_envelope==='object');const artifacts=asArray(envelope.output_artifacts);const assessment=hasEnvelope?handoffReceiptAssessment(envelope,events,auditInvocationList(),audit):null;const route=hasEnvelope?handoffRouteTarget(envelope):null;const handoff=hasEnvelope?`<details class="handoff-detail"><summary>查看计划交接、产物校验与接收确认</summary><dl><dt>交接编号</dt><dd>${escapeHTML(envelope.message_id||'未记录')}</dd><dt>计划路线</dt><dd>${escapeHTML(envelope.producer||'未记录')} → ${escapeHTML(route||'未记录')}</dd><dt>接收状态</dt><dd><span class="receipt-state ${escapeHTML(assessment.status)}">${escapeHTML(receiptStateLabel(assessment.status))}</span> · ${escapeHTML(assessment.reasons.join(' '))}</dd><dt>追踪与尝试</dt><dd>${escapeHTML(envelope.trace_id||'未记录')} / ${escapeHTML(envelope.attempt??'未记录')}</dd><dt>输出产物</dt><dd>${artifacts.map(item=>`${escapeHTML(item?.artifact_id||'未记录')} · ${escapeHTML(item?.kind||'未记录')}`).join('<br>')||'无'}</dd><dt>校验值</dt><dd class="checksum">${artifacts.map(item=>escapeHTML(item?.checksum||'未记录')).join('<br>')||'无'}</dd><dt>去重编号</dt><dd class="checksum">${escapeHTML(envelope.idempotency_key||'未记录')}</dd><dt>人工核对</dt><dd>${escapeHTML(assessment.manualCheck)}</dd><dt>阶段检查</dt><dd><b class="gate-${escapeHTML(envelope.quality_gate?.status||'unknown')}">${escapeHTML(envelope.quality_gate?.status||'unknown')}</b> · ${escapeHTML(envelope.quality_gate?.rule||p.quality_gate||'未记录')}</dd></dl></details>`:'';const globalIndex=eventGlobalIndex(event,events);const localIndex=events.indexOf(event)+1;const sequenceLabel=globalIndex===null?`窗口内 ${localIndex} / ${events.length}`:`事件 ${globalIndex} / ${totalText}`;return `<div class="event"><span class="event-dot"></span><div><span class="event-seq">${escapeHTML(sequenceLabel)}</span><strong>${escapeHTML(friendly.title)}</strong><p>${escapeHTML(friendly.detail)}</p>${handoff}</div><time>${escapeHTML(time)}</time></div>`}).join('');
  humanizeVisibleCopy($('timeline'));
  $('timeline').querySelector('[data-timeline-expand]')?.addEventListener('click',()=>{
    timelineExpanded=!timelineExpanded;
    renderTimeline(events);
  });
}

function updateResumeAuditGuide(entries) {
  const guide = $('resumeAuditGuide');
  if (!guide) return;
  const resumeEntries = asArray(entries).filter(item => item?.kind === 'resume');
  const visible = ledgerFilter === 'resume' && resumeEntries.length > 0;
  guide.hidden = !visible;
  guide.classList.toggle('hidden', !visible);
  guide.setAttribute('aria-hidden', String(!visible));
  const summary = $('resumeAuditGuideSummary');
  if (summary) summary.textContent = visible
    ? `本次有 ${resumeEntries.length} 条恢复记录；按同一恢复编号核对 4 项`
    : '按同一恢复编号，依次核对 4 项记录';
}

function renderUnifiedAuditTimeline(state, events) {
  const invocations = asArray(state.agent_invocations);
  const failures = asArray(state.failures);
  const audit = window.__latestAudit || null;
  events=asArray(events);
  const entries = [];
  const eventHandoffIds = new Set(events.map(event => normalizedId(eventEnvelope(event)?.message_id)).filter(Boolean));
  const artifactIdsInEvents = new Set(events.flatMap(eventArtifactIds));
  const invocationIds = new Set(invocations.map(item => normalizedId(item?.invocation_id)).filter(Boolean));
  invocations.forEach((item, index) => {
    const route=invocationModelRoute(item);
    entries.push({
      kind:'invocation', timestamp:item.started_at || item.ended_at || null, sourceOrder:index,
      agent:item.agent_id, invocation:item,
      title:`${agentContracts[item.agent_id]?.name || item.role || item.agent_id} · ${operationName(item.operation)}`,
      detail:`${modelRouteLabel(route)} · 输入 ${route.modalities.map(modalityLabel).join(' / ') || '模态未记录'} · ${invocationStatus(item.status)} · 尝试 ${item.attempt ?? '未记录'} · ${invocationDuration(item)} · 结束 ${formatTimestamp(item.ended_at)}`,
      proof:`调用 ID ${item.invocation_id || '未记录'} · ${asArray(item.output_artifact_ids).length} 个产物 · ${asArray(item.handoff_message_ids).length} 个交接消息 ID`
    });
  });
  events.forEach((event, index) => {
    const envelope = event.payload?.handoff_envelope;
    if (envelope) {
      entries.push({kind:'handoff',timestamp:envelope.created_at || event.created_at || null,sourceOrder:index,event,envelope,
        title:`${agentContracts[envelope.producer]?.name || envelope.producer || '发送方未解析'} → ${handoffConsumerLabel(handoffRouteTarget(envelope),envelope,event,events,invocations,audit)}`,
        detail:`${event.payload?.summary || `交接消息 ${envelope.message_id}`} · 事件持久化 ${formatTimestamp(event.created_at)}`,
        proof:`信封 ${envelope.message_id} · 事件 ${event.event_id || 'ID 未记录'} · ${asArray(envelope.output_artifacts).length} 个产物 · 质量门 ${gateStatusName(envelope.quality_gate?.status || 'unknown')}`});
    }
    const isGate = event.node === 'assess_closure' || event.node === 'verify' || Boolean(event.payload?.quality_gate);
    if (isGate) {
      const friendly = describeEvent(event);
      entries.push({kind:'gate',timestamp:event.created_at || null,sourceOrder:index,event,
        title:friendly.title,detail:friendly.detail,
        proof:`事件 ${event.event_id || 'ID 未记录'} · ${event.payload?.quality_gate ? `规则：${event.payload.quality_gate}` : '查看对应调用与逐目标交付前检查记录'}`});
    } else if (!envelope && ['recover','cancelled'].includes(event.node)) {
      const friendly = describeEvent(event);
      entries.push({kind:'failure',timestamp:event.created_at || null,sourceOrder:index,event,title:friendly.title,detail:friendly.detail,proof:'持久化恢复或停止事件'});
    } else if (!envelope) {
      const friendly = describeEvent(event);
      entries.push({kind:'event',timestamp:event.created_at || null,sourceOrder:index,event,title:friendly.title,detail:friendly.detail,proof:`持久化阶段事件 · ${event.event_id || 'ID 未记录'}`});
    }
  });
  auditHandoffRecords(events, audit).forEach((record, index) => {
    if (eventHandoffIds.has(record.id)) return;
    const envelope = record.envelope || {};
    const assessment = handoffReceiptAssessment(envelope, events, invocations, audit);
    const artifacts = handoffArtifactRecords(envelope, audit);
    entries.push({
      kind:'handoff',
      timestamp:envelope.created_at || record.durable?.created_at || null,
      sourceOrder:index,
      envelope,
      title:`${agentContracts[envelope.producer]?.name || envelope.producer || '发送方未解析'} → ${handoffConsumerLabel(handoffRouteTarget(envelope),envelope,null,events,invocations,audit)}`,
      detail:`durable handoff 无对应 event · ${receiptStateLabel(assessment.status)} · ${assessment.reasons.join(' ')}`,
      proof:`信封 ${record.id} · ${artifacts.length} 个 durable/内嵌产物 · 人工核验：${assessment.manualCheck}`,
    });
  });
  if (audit?.available) {
    asArray(audit.artifacts).forEach((artifact, index) => {
      const producerInvocationId = normalizedId(artifact?.producer_invocation_id);
      const handoffMessageId = normalizedId(artifact?.handoff_message_id);
      if (artifactIdsInEvents.has(normalizedId(artifact?.artifact_id))) return;
      entries.push({
        kind:'artifact',
        timestamp:artifact.created_at || null,
        sourceOrder:index,
        artifact,
        title:`durable 阶段产物 · ${artifact.artifact_id || 'ID 未记录'}`,
        detail:producerInvocationId && invocationIds.has(producerInvocationId)
          ? `已持久化，但该 artifact 没有在 event 中出现；绑定到 invocation ${producerInvocationId}。`
          : handoffMessageId && audit.handoffByMessage?.has(handoffMessageId)
            ? `已持久化，但该 artifact 没有在 event 中出现；绑定到 handoff ${handoffMessageId}。`
            : 'durable artifact manifest 未能绑定到当前 invocation 或 handoff；不可作为阶段完成证明。',
        proof:`producer invocation ${producerInvocationId || '未记录'} · handoff ${handoffMessageId || '未记录'} · manifest ${artifact.passable === true ? '可通过' : '不可通过或未记录'}`,
      });
    });
    asArray(audit.resumeReceipts).forEach((receipt, index) => {
      const transitions = asArray(receipt.transitions);
      if (!transitions.length) {
        entries.push({
          kind:'resume', timestamp:receipt.created_at || null, sourceOrder:index,
          resumeReceipt:receipt,
          title:`恢复回执 · ${resumeExecutionStatusLabel(receipt.execution_status)}`,
          detail:`已记录恢复授权，但没有结构化 transition；不能证明 worker 如何接管。`,
          proof:`receipt ${receipt.idempotency_key} · checkpoint ${receipt.checkpoint_id_before ?? '未记录'} → ${receipt.checkpoint_id_after ?? '未记录'} · fence ${receipt.claim_fence ?? '未记录'}`,
        });
      } else {
        transitions.forEach((transition, transitionIndex) => {
          const status = normalizedResumeExecutionStatus(receipt.execution_status);
          entries.push({
            kind:'resume', timestamp:transition.created_at || receipt.created_at || null,
            sourceOrder:index * 100 + transitionIndex, resumeReceipt:receipt, transition,
            title:resumeTransitionTitle(transition),
            detail:`${resumeTransitionStatusLabel(transition.to_status)} · 当前回执状态：${resumeExecutionStatusLabel(status)} · ${transition.reason || '原因未记录'}`,
            proof:`receipt ${receipt.idempotency_key} · ${transition.from_status || '未记录'} → ${transition.to_status || '未记录'} · fence ${transition.owner_fence ?? receipt.claim_fence ?? '未记录'} · owner ${transition.owner_token_fingerprint || receipt.claim_owner_fingerprint || '未记录'}${transition.handoff_message_id ? ` · handoff ${transition.handoff_message_id}` : ''}${transition.agent_invocation_id ? ` · invocation ${transition.agent_invocation_id}` : ''}`,
          });
        });
      }
    });
    asArray(audit.worker).forEach((worker, index) => {
      const payload = asObject(worker.payload);
      const receiptId = normalizedId(worker.receipt_id || worker.resume_receipt_id || payload.receipt_id || payload.resume_receipt_id);
      entries.push({
        kind:'resume', timestamp:worker.created_at || null, sourceOrder:100000 + index,
        resumeReceipt:receiptId ? audit.resumeReceiptById?.get(receiptId) : null,
        title:`worker 审计 · ${worker.event_type || '生命周期记录'}`,
        detail:payload.error || payload.status || 'worker 生命周期事件已持久化',
        proof:`receipt ${receiptId || '非恢复 worker'} · fence ${payload.fence ?? '未记录'} · ${payload.exception_type || '未记录异常类型'}`,
      });
    });
  }
  failures.forEach((item, index) => entries.push({kind:'failure',timestamp:item.created_at || item.occurred_at || null,sourceOrder:index,failure:item,
    title:failureName(item.type),detail:item.reason || item.instruction || '故障详情未记录',proof:item.retryable ? '可定向恢复' : '需要人工检查'}));
  entries.sort((a,b) => {
    const parsedA=a.timestamp?new Date(a.timestamp).getTime():Number.NaN;
    const parsedB=b.timestamp?new Date(b.timestamp).getTime():Number.NaN;
    const at=Number.isFinite(parsedA)?parsedA:Number.POSITIVE_INFINITY;
    const bt=Number.isFinite(parsedB)?parsedB:Number.POSITIVE_INFINITY;
    if (at !== bt) return at - bt;
    const rank={event:0,invocation:1,gate:2,handoff:3,resume:4,artifact:5,failure:6};
    return (rank[a.kind]||0)-(rank[b.kind]||0) || a.sourceOrder-b.sourceOrder;
  });
  entries.forEach((item, index) => { item.__ledgerIndex = index; });
  window.__latestAuditEntries = entries;
  const counts = entries.reduce((result,item)=>{result[item.kind]=(result[item.kind]||0)+1;return result},{});
  $('auditLedgerSummary').innerHTML = `<b>${entries.length}</b> 条合并记录 · <span>${counts.invocation||0} 条角色执行</span><span>${counts.handoff||0} 条任务交接</span><span>${counts.resume||0} 条恢复确认</span><span>${counts.artifact||0} 条未关联产物</span><span>${counts.gate||0} 条阶段检查</span><span>${counts.failure||0} 条异常</span>${window.__latestEventWindow?.incomplete?'<em>阶段事件仅最近窗口</em>':''}`;
  updateResumeAuditGuide(entries);
  const visible = entries.filter(item => ledgerFilter === 'all' || item.kind === ledgerFilter);
  if (!visible.length) {
    $('auditLedger').innerHTML = eventWindowInlineMarkup('主时间线中的阶段事件')+'<div class="audit-ledger-empty">当前筛选条件下没有持久化记录。</div>';
    return;
  }
  const labels={invocation:'角色执行',handoff:'任务交接',resume:'恢复确认',artifact:'未关联产物',gate:'阶段检查',failure:'异常记录',event:'阶段事件'};
  $('auditLedger').innerHTML = eventWindowInlineMarkup('主时间线中的阶段事件')+visible.map(item => {
    const globalIndex=entries.indexOf(item)+1;
    const time=item.timestamp?formatTimestamp(item.timestamp):'时间未记录 · 排在有时间记录之后';
    const actionable=item.kind==='invocation'||item.kind==='handoff'||item.kind==='resume';
    const inspectable=['event','gate','failure','artifact'].includes(item.kind);
    const auditKey=item.kind==='invocation'?item.invocation.invocation_id:item.kind==='handoff'?item.envelope.message_id:item.resumeReceipt?.idempotency_key;
    const actionMarkup = actionable && auditKey
      ? `<button type="button" data-ledger-open="${item.kind}" data-ledger-key="${escapeHTML(auditKey)}">打开完整${labels[item.kind]}审计</button>`
      : inspectable
        ? `<button type="button" data-ledger-open="detail" data-ledger-index="${item.__ledgerIndex}">查看这条记录详情</button>`
        : '';
    return `<article class="audit-ledger-entry ${item.kind} ${item.transition ? resumeTransitionTone(item.transition) : ''}" data-ledger-kind="${item.kind}"><div class="ledger-seq"><span>${String(globalIndex).padStart(3,'0')}</span><i></i></div><div class="ledger-entry-body"><header><b>${labels[item.kind]}</b><time>${escapeHTML(time)}</time></header><h4>${escapeHTML(item.title)}</h4><p>${escapeHTML(item.detail)}</p><small>${escapeHTML(item.proof)}</small>${actionMarkup}</div></article>`;
  }).join('');
  humanizeVisibleCopy($('auditLedger'));
  document.querySelectorAll('[data-ledger-open]').forEach(button=>button.addEventListener('click',()=>{
    if(button.dataset.ledgerOpen==='invocation'){
      const item=invocations.find(value=>value.invocation_id===button.dataset.ledgerKey);
      if(item)showInvocationAudit(item,invocations,events,audit);
    }else if(button.dataset.ledgerOpen==='resume') showResumeAudit(button.dataset.ledgerKey);
    else if(button.dataset.ledgerOpen==='handoff') showHandoffAudit(button.dataset.ledgerKey,invocations,events,audit);
    else showLedgerEntryAudit(button.dataset.ledgerIndex);
  }));
}

function normalizedSourceUrl(value) {
  try {
    const url = new URL(String(value || ''));
    url.hash = '';
    url.hostname = url.hostname.toLowerCase();
    if (url.pathname.length > 1) url.pathname = url.pathname.replace(/\/+$/, '');
    return url.toString();
  } catch (_) {
    return String(value || '').trim().replace(/#.*$/, '').replace(/\/+$/, '');
  }
}

function sourceMatchesEvidence(source, evidence) {
  const sourceId = normalizedId(source?.id || source?.source_id);
  const evidenceSourceId = normalizedId(evidence?.source_id);
  const evidenceUrl = normalizedSourceUrl(evidence?.source_url);
  const sourceUrls = [source?.final_url, source?.canonical_url, source?.url]
    .map(normalizedSourceUrl)
    .filter(Boolean);
  // Article identity is stable across immutable Fetch attempts. Its aggregate
  // content_hash may describe a newer attempt, so comparing that hash here
  // would detach Evidence that correctly points to an older fetch_record_id.
  // An explicit URL disagreement is still an identity conflict even when a
  // reused source_id happens to match.
  if (sourceId && evidenceSourceId) {
    if (sourceId !== evidenceSourceId) return false;
    if (evidenceUrl && sourceUrls.length && !sourceUrls.includes(evidenceUrl)) return false;
    return true;
  }
  return Boolean(evidenceUrl && sourceUrls.includes(evidenceUrl));
}

function sourceEvidenceSummary(source, evidence = [], state = window.__latestState || {}) {
  const related = asArray(evidence).filter(item => sourceMatchesEvidence(source, item));
  const classified = related.map(item => ({item, role:evidenceEffectiveRole(item, state)}));
  const admitted = classified.filter(({role}) => ['supports', 'contradicts'].includes(role.kind));
  const excluded = classified.filter(({role}) => role.kind === 'excluded');
  return {related, admitted, excluded};
}

function sourceFetchAttempts(source) {
  const direct = asArray(source?.fetch_attempts);
  if (direct.length) return direct;
  if (source?._audit_fetch && (source._audit_fetch.fetch_record_id || source._audit_fetch.invocation_id)) {
    return [source._audit_fetch];
  }
  if (source?.fetch_record_id || source?.fetch_invocation_id) {
    return [{
      fetch_record_id: normalizedId(source.fetch_record_id),
      source_id: normalizedId(source.id || source.source_id),
      invocation_id: normalizedId(source.fetch_invocation_id),
      result_invocation_id: normalizedId(source.fetch_result_invocation_id),
      operation_key: normalizedId(source.fetch_operation_key),
      execution_mode: source.fetch_execution_mode || '',
      provider: source.fetch_provider || '',
      fetch_mode: source.fetch_mode || 'unknown',
      status: source.status || 'unknown',
      attempt: source.fetch_attempt || null,
      content_hash: source.content_hash || '',
      content_hash_scope: source.content_hash_scope || 'unknown',
      snapshot_sha256: source.snapshot_sha256 || '',
      binding_status: source.fetch_binding_status || 'legacy_unverified',
      binding_valid: source.fetch_binding_valid,
      final_url: source.final_url || source.url || '',
      fetched_at: source.fetched_at || '',
    }];
  }
  const audit = window.__latestAudit;
  const bySource = audit?.fetchAttemptsBySource;
  if (!(bySource instanceof Map)) return [];
  const sourceId = normalizedId(source?.id || source?.source_id);
  const sourceUrl = normalizedSourceUrl(source?.final_url || source?.url);
  return bySource.get(sourceId) || bySource.get(sourceUrl) || [];
}

function exactFetchRecordId(value) {
  return normalizedId(value?.fetch_record_id);
}

function evidenceFetchHashAudit(evidence, fetch) {
  const evidenceContent = String(evidence?.content_hash || '').trim();
  const fetchContent = String(fetch?.content_hash || '').trim();
  const evidenceSnapshot = String(evidence?.snapshot_sha256 || '').trim();
  const fetchSnapshot = String(fetch?.snapshot_sha256 || '').trim();
  const evidenceScope = String(evidence?.content_hash_scope || '').trim();
  const fetchScope = String(fetch?.content_hash_scope || '').trim();
  if (evidenceContent && fetchContent && evidenceContent !== fetchContent) {
    return {status:'invalid', label:'Evidence 与 Fetch 的正文 hash 不一致', detail:'相同 fetch_record_id 不能覆盖正文 hash 冲突；关系已拒绝。'};
  }
  if (evidenceSnapshot && fetchSnapshot && evidenceSnapshot !== fetchSnapshot) {
    return {status:'invalid', label:'Evidence 与 Fetch 的快照 hash 不一致', detail:'相同 fetch_record_id 不能覆盖保存快照 SHA-256 冲突；关系已拒绝。'};
  }
  if (evidenceScope && fetchScope && evidenceScope !== fetchScope) {
    return {status:'scope_difference', label:'hash 作用域不同，完整性暂不可合并', detail:`Evidence 为 ${hashScopeName(evidenceScope)}，Fetch 为 ${hashScopeName(fetchScope)}；身份关系可见，但不能把 hash 直接当作同一作用域。`};
  }
  return {status:'matched', label:'Evidence 与 Fetch 的 hash 字段一致', detail:'已记录的正文 hash、作用域和快照 hash 没有发现字段冲突。'};
}

function exactEvidenceFetchBinding(evidence, state = window.__latestState || {}) {
  const fetchRecordId = exactFetchRecordId(evidence);
  const sources = asArray(normalizeSources(state));
  if (!fetchRecordId) {
    return {
      status: 'unbound',
      label: '无法唯一回链',
      detail: 'Evidence.fetch_record_id 未记录；不能把同一 source 的最新 Fetch 当作证据来源。',
      fetch_record_id: '',
      source: sources.find(candidate => sourceMatchesEvidence(candidate, evidence)) || null,
      fetch: null,
    };
  }
  const sourceCandidates = asArray(sources).filter(candidate => sourceMatchesEvidence(candidate, evidence));
  const fetches = sourceFetchGraphItems(sources).filter(item => !item.is_placeholder);
  const candidates = fetches.filter(item => exactFetchRecordId(item) === fetchRecordId);
  if (!candidates.length) {
    return {
      status: 'not_loaded',
      label: 'Fetch 记录未载入',
      detail: `Evidence 已记录 ${fetchRecordId}，但当前 audit 窗口没有该 immutable fetch 行；不能据此判定绑定有效。`,
      fetch_record_id: fetchRecordId,
      source: sourceCandidates.length === 1 ? sourceCandidates[0] : null,
      fetch: null,
    };
  }
  if (candidates.length !== 1) {
    return {
      status: 'invalid',
      label: 'Fetch 记录重复，关系已拒绝',
      detail: `当前运行中有 ${candidates.length} 条记录声称使用 ${fetchRecordId}；在唯一性恢复前不能选择其中一条。`,
      fetch_record_id: fetchRecordId,
      source: sourceCandidates.length === 1 ? sourceCandidates[0] : null,
      fetch: null,
    };
  }
  const graphFetch = candidates[0];
  const fetch = graphFetch.attempt || graphFetch;
  const fetchSourceId = normalizedId(fetch?.source_id || graphFetch.source_id);
  const fetchSource = sources.find(candidate => normalizedId(candidate?.id || candidate?.source_id) === fetchSourceId) || null;
  const source = sourceCandidates.length === 1 ? sourceCandidates[0] : fetchSource;
  const evidenceSourceId = normalizedId(evidence?.source_id);
  const selectedSourceId = normalizedId(source?.id || source?.source_id);
  const crossSourcePair = Boolean(selectedSourceId && fetchSourceId && selectedSourceId !== fetchSourceId);
  const sourceIdentityConflict = Boolean(fetchSource && !sourceMatchesEvidence(fetchSource, evidence));
  if (
    sourceCandidates.length > 1
    || !fetchSourceId
    || !source
    || crossSourcePair
    || sourceIdentityConflict
    || (evidenceSourceId && evidenceSourceId !== fetchSourceId)
  ) {
    return {
      status: 'invalid',
      label: '来源字段不一致，关系已拒绝',
      detail: sourceCandidates.length > 1
        ? `Evidence 的来源字段对应 ${sourceCandidates.length} 篇文章，无法唯一确定 Article。`
        : !fetchSourceId
          ? '精确 Fetch 没有 source_id，无法证明它属于当前 Article。'
          : !source
            ? `当前 audit 中没有与 Fetch.source_id=${fetchSourceId} 对应的 Article。`
            : crossSourcePair
              ? `Evidence 的 Article ${selectedSourceId} 与 Fetch.source_id=${fetchSourceId} 不一致。`
              : sourceIdentityConflict
                ? 'Evidence 的 source_id 虽与 Article 相同，但显式 source_url 与 Article URL 集合冲突。'
              : `Evidence.source_id=${evidenceSourceId} 与 Fetch.source_id=${fetchSourceId} 不一致。`,
      fetch_record_id: fetchRecordId,
      source: source || null,
      fetch: null,
    };
  }
  const declaredBinding = String(evidence?.fetch_binding_status || '').toLowerCase().replace(/[-\s]+/g, '_');
  const declaredValid = evidence?.fetch_binding_valid;
  const bindingConflict = (declaredValid === false && isServerBoundFetch(fetch))
    || (['server_bound', 'server_validated'].includes(declaredBinding) && declaredValid === false)
    || (['invalid', 'rejected'].includes(declaredBinding));
  if (bindingConflict) {
    return {
      status: 'invalid',
      label: 'Evidence 绑定字段冲突，关系已拒绝',
      detail: `Evidence 声明为 ${bindingStatusName(declaredBinding)}，但其 binding_valid 或状态与 Fetch 行冲突。`,
      fetch_record_id: fetchRecordId,
      source,
      fetch: null,
    };
  }
  const hashAudit = evidenceFetchHashAudit(evidence, fetch);
  if (hashAudit.status === 'invalid') {
    return {
      status: 'invalid',
      label: hashAudit.label,
      detail: hashAudit.detail,
      fetch_record_id: fetchRecordId,
      source,
      fetch: null,
      hash_audit: hashAudit,
    };
  }
  const verified = isServerBoundFetch(fetch);
  return {
    status: verified ? 'bound' : 'unverified',
    label: verified
      ? hashAudit.status === 'scope_difference' ? '已精确回链，hash 作用域待核对' : '已精确回链'
      : '已定位但绑定未核验',
    detail: verified
      ? `Evidence → Fetch ${fetchRecordId}，${bindingStatusName(fetch.binding_status)} 且 binding_valid=true。${hashAudit.status === 'scope_difference' ? ` ${hashAudit.detail}` : ''}`
      : `Evidence → Fetch ${fetchRecordId}，Fetch 行存在但 ${bindingStatusName(fetch.binding_status)} 或 binding_valid 未通过。`,
    fetch_record_id: fetchRecordId,
    source,
    fetch: graphFetch,
    hash_audit: hashAudit,
  };
}

function hashScopeName(value) {
  return ({
    page_text: '页面正文文本',
    full_extracted_text: '截断前完整抽取文本',
    snapshot_text: '保存快照正文',
    unknown: '作用域未记录',
  })[String(value || 'unknown')] || String(value || '作用域未记录');
}

function hashConsistencyModel(contentHash, contentScope, snapshotHash) {
  const content = String(contentHash || '').trim();
  const snapshot = String(snapshotHash || '').trim();
  const scope = String(contentScope || 'unknown').trim() || 'unknown';
  if (!content || !snapshot) {
    return {status: 'unverifiable', label: 'hash 不完整，无法比较', detail: '正文 hash 或保存快照 hash 未记录。'};
  }
  if (content === snapshot) {
    return {status: 'matched', label: '正文 hash 与快照 hash 一致', detail: `${hashScopeName(scope)} 与保存快照逐字一致。`};
  }
  if (scope === 'full_extracted_text') {
    return {
      status: 'scope_difference',
      label: 'hash 不同，但不能直接判损坏',
      detail: '正文 hash 的作用域是截断前完整抽取文本，而保存快照可能是截断后的可查验正文；应先核对作用域和快照 hash。',
    };
  }
  if (scope === 'page_text' || scope === 'snapshot_text') {
    return {
      status: 'mismatched',
      label: '正文 hash 与快照 hash 不一致',
      detail: `${hashScopeName(scope)} 按定义应可直接比较；可能存在内容变化、快照替换或记录损坏。`,
    };
  }
  return {
    status: 'scope_unknown',
    label: 'hash 不同且作用域未知',
    detail: '没有足够元数据判断差异来自截断、规范化还是内容变化；不可把它标为完整性通过。',
  };
}

function fetchAttemptStatusName(value) {
  const key = String(value || '').toLowerCase().replace(/[-\s]+/g, '_');
  return ({
    fetched:'读取成功',
    failed:'读取失败',
    running:'读取中',
    pending:'等待读取',
    cancelled:'已取消',
    canceled:'已取消',
    timeout:'读取超时',
    error:'读取异常',
    discovered:'仅发现，未读取',
    evidence_only:'仅证据回填',
    replayed:'已回放',
    unknown:'结果未知',
  })[key] || (key ? `状态：${key}` : '状态未记录');
}

function fetchModeName(value) {
  const key = String(value || '').toLowerCase().replace(/[-\s]+/g, '_');
  return ({
    live:'Provider 实时读取',
    live_provider:'Provider 实时读取',
    network:'网络读取',
    provider_cache:'Provider 缓存结果',
    offline_corpus:'离线语料读取',
    replay:'持久化操作回放',
    replayed:'持久化操作回放',
    durable_operation_replay:'持久化操作回放',
    evidence_only:'仅证据回填',
    failed:'读取失败',
    unknown:'读取模式未记录',
  })[key] || (key ? `读取模式：${key}` : '读取模式未记录');
}

function executionModeName(value) {
  const key = String(value || '').toLowerCase().replace(/[-\s]+/g, '_');
  return ({
    executed:'实际执行',
    live:'实际执行',
    replayed:'持久化回放',
    replay:'持久化回放',
    cached:'缓存执行',
    simulated:'模拟执行',
    unknown:'执行方式未记录',
  })[key] || (key ? `执行方式：${key}` : '执行方式未记录');
}

function bindingStatusName(value) {
  const key = String(value || '').toLowerCase().replace(/[-\s]+/g, '_');
  return ({
    server_bound:'系统已确认这次页面读取对应当前文章',
    server_validated:'系统已确认对应关系',
    field_match:'记录信息相符，等待系统确认',
    legacy_unverified:'历史记录没有对应关系检查',
    unverified:'文章与读取记录尚未确认对应',
    invalid:'文章与读取记录不一致，暂不采用',
    evidence_only:'只有证据片段，没有本次页面读取记录',
    not_loaded:'页面读取记录尚未载入',
    unknown:'文章与读取记录的对应情况未记录',
  })[key] || (key ? `对应关系状态：${key}` : '对应关系状态未记录');
}

function fetchAttemptOrderValue(attempt) {
  const value = asObject(attempt);
  for (const key of ['completed_at', 'finished_at', 'recorded_at', 'fetched_at', 'ended_at', 'created_at', 'updated_at']) {
    if (value[key]) {
      const timestamp = new Date(value[key]).getTime();
      if (Number.isFinite(timestamp)) return {timestamp, key, raw:value[key]};
    }
  }
  return null;
}

function latestSourceFetchSelection(source) {
  const attempts = sourceFetchAttempts(source);
  if (!attempts.length) return {fetch:null, determinate:false, reason:'这篇文章没有逐次保存的页面读取记录'};
  const ordered = attempts.map((fetch, index) => ({
    fetch,
    index,
    order:fetchAttemptOrderValue(fetch),
  }));
  if (ordered.some(item => !item.order)) {
    return {fetch:null, determinate:false, reason:'至少一条页面读取缺少可排序时间，不能确定最新一次'};
  }
  ordered.sort((left, right) => left.order.timestamp - right.order.timestamp || left.index - right.index);
  const latestTimestamp = ordered.at(-1).order.timestamp;
  const latest = ordered.filter(item => item.order.timestamp === latestTimestamp);
  if (latest.length !== 1) {
    return {fetch:null, determinate:false, reason:`${latest.length} 条页面读取的最新时间相同，完成先后未记录`};
  }
  return {fetch:latest[0].fetch, determinate:true, reason:`按保存时间确定最新一次页面读取`};
}

function fetchAttemptOrderDescriptor(attempt, attempts = []) {
  const current = fetchAttemptOrderValue(attempt);
  if (!current) return {label:'顺序未记录', detail:'没有可排序的完成或持久化时间，不能从列表位置推断先后。'};
  const ordered = asArray(attempts)
    .map((row, index) => ({row, index, order:fetchAttemptOrderValue(row)}))
    .filter(item => item.order)
    .sort((left, right) => left.order.timestamp - right.order.timestamp || String(left.row.fetch_record_id || '').localeCompare(String(right.row.fetch_record_id || '')));
  const currentIndex = ordered.findIndex(item => item.row === attempt);
  const sameTime = ordered.filter(item => item.order.timestamp === current.timestamp);
  const fieldLabel = current.key === 'recorded_at' ? '持久化记录序' : current.key === 'fetched_at' ? '读取完成时间' : '完成记录时间';
  if (sameTime.length > 1) {
    const group = ordered.findIndex(item => item.order.timestamp === current.timestamp) + 1;
    return {label:`${fieldLabel}并列 ${group}`, detail:`${fieldLabel}相同（${sameTime.length} 条并发/同刻记录），先后未记录。`};
  }
  return {label:`${fieldLabel} #${currentIndex + 1}`, detail:`按 ${current.key} 排序；这不是 attempt 重试编号。`};
}

function fetchAttemptLabel(attempt) {
  const number = finiteValue(attempt?.attempt);
  return number === null ? '读取次数未记录' : `第 ${number} 次读取`;
}

function sourceFetchAttemptMarkup(source) {
  const attempts = sourceFetchAttempts(source);
  if (!attempts.length) return '<div class="fetch-attempt-ledger empty"><b>逐次页面读取记录未保存</b><span>这篇文章没有可逐次核对的页面读取记录。</span></div>';
  const sourceKey = sourceStableKey(source);
  return `<details class="fetch-attempt-ledger" data-collapse-key="${escapeHTML(collapseKey('fetch-ledger', sourceKey))}" open><summary><span>逐次页面读取记录</span><strong>${attempts.length} 条独立保存的读取记录</strong></summary><ol>${attempts.map((attempt) => {
    const binding = isServerBoundFetch(attempt) ? '系统已核对文章与读取记录的对应关系' : '文章与读取记录尚未能完整核对';
    const bindingValid = attempt.binding_valid ?? attempt.fetch_binding_valid;
    const invocation = normalizedId(attempt.invocation_id);
    const invocationButton = invocation ? `<button type="button" class="audit-link-button inline" data-source-invocation="${escapeHTML(invocation)}">打开对应的角色执行记录</button>` : '';
    const statusKey = String(attempt.status || 'unknown').toLowerCase().replace(/[-\s]+/g, '_');
    const tone = ['failed', 'timeout', 'error', 'cancelled', 'canceled'].includes(statusKey) ? 'failed' : statusKey === 'fetched' ? 'fetched' : 'unknown';
    const order = fetchAttemptOrderDescriptor(attempt, attempts);
    return `<li class="${tone}" data-attempt-key="${escapeHTML(collapseKey('fetch-attempt', `${sourceKey}|${attempt.fetch_record_id || ''}|${attempt.attempt ?? ''}|${attempt.invocation_id || ''}|${attempt.recorded_at || attempt.fetched_at || ''}`))}"><header><b>${escapeHTML(fetchAttemptLabel(attempt))}</b><span>${escapeHTML(fetchAttemptStatusName(attempt.status))}</span></header><p>${escapeHTML(executionModeName(attempt.execution_mode))} · ${escapeHTML(fetchModeName(attempt.fetch_mode))} · ${escapeHTML(attempt.provider || '模型服务未记录')}</p><small>读取记录 ID（技术字段）${escapeHTML(attempt.fetch_record_id || '未记录')} · 角色执行记录 ID ${escapeHTML(invocation || '未记录')} · 操作编号 ${escapeHTML(attempt.operation_key || '未记录')}</small><small>${escapeHTML(binding)} · ${escapeHTML(bindingStatusName(attempt.binding_status || (isServerBoundFetch(attempt) ? 'server_bound' : 'unknown')))} · 系统关联检查：${bindingValid === true ? '通过' : '未通过或未记录'} · ${escapeHTML(order.label)} · ${escapeHTML(formatTimestamp(attempt.recorded_at || attempt.fetched_at))}</small><small>正文校验值 ${escapeHTML(attempt.content_hash || '未记录')} · 覆盖范围 ${escapeHTML(hashScopeName(attempt.content_hash_scope))} · 快照校验值（SHA-256）${escapeHTML(attempt.snapshot_sha256 || '未记录')}</small>${attempt.error ? `<em>${escapeHTML(humanizeAuditText(attempt.error))}</em>` : ''}${invocationButton}</li>`;
  }).join('')}</ol></details>`;
}

function sourceReadMode(source) {
  const status = String(source?.status || '').toLowerCase().replace(/-/g, '_');
  const fetchMode = String(source?.fetch_mode || '').toLowerCase().replace(/-/g, '_');
  const executionMode = String(source?.fetch_execution_mode || '').toLowerCase().replace(/-/g, '_');
  const binding = String(source?.fetch_binding_status || '').toLowerCase().replace(/-/g, '_');
  const fetchInvocation = normalizedId(source?.fetch_result_invocation_id || source?.fetch_invocation_id);
  if (status === 'failed' || fetchMode === 'failed') return 'failed';
  if (status === 'evidence_only' || fetchMode === 'evidence_only' || binding === 'evidence_only') return 'evidence_only';
  const bound = isServerBoundFetch(source);
  if (executionMode === 'replay' || executionMode === 'replayed' || ['replay', 'replayed', 'operation_replay', 'durable_operation_replay'].includes(fetchMode)) return 'replayed';
  if (fetchMode === 'provider_cache') {
    return 'provider_cache';
  }
  if (fetchMode === 'offline_corpus') {
    return 'offline_corpus';
  }
  if (['live', 'network', 'online', 'real_time', 'realtime', 'live_provider'].includes(fetchMode)) {
    return fetchInvocation && bound ? 'live' : 'fetched_unbound';
  }
  if (status === 'discovered') return 'discovered';
  if (status === 'fetched') {
    if (!fetchInvocation || !bound) return 'fetched_unbound';
    return 'fetched_bound';
  }
  return 'unknown';
}

function sourceReadAssessment(source) {
  const mode = sourceReadMode(source);
  const binding = String(source?.fetch_binding_status || '').toLowerCase().replace(/-/g, '_');
  const fetchInvocation = normalizedId(source?.fetch_result_invocation_id || source?.fetch_invocation_id);
  const bound = isServerBoundFetch(source) && Boolean(fetchInvocation);
  const labels = {
    live:['在线页面读取','本次页面读取已和角色执行记录对应；可以确认本次读取路径，但不保证在线页面现在仍是相同内容。'],
    provider_cache:['模型服务缓存页面','本次得到的是模型服务返回的缓存页面，不应描述为实时网络读取。'],
    offline_corpus:['离线材料读取','页面内容来自本地回放材料，不是在线抓取。'],
    replayed:['复用已保存的读取结果','本次复用了先前成功的页面读取结果或本地回放，没有再次请求模型服务，也不能描述为实时读取。'],
    evidence_only:['只有引用片段','有证据片段和来源链接，但没有本次页面读取记录；不能把引用片段当作本次已经阅读的正文。'],
    fetched_bound:['读取记录已对应','页面读取记录已对应当前文章，但读取方式尚未完整保存。'],
    fetched_unbound:['历史读取无法对应','文章标为已读取，但没有可核对的角色执行记录；不能把摘要或证据链接当作本次已经阅读的正文。'],
    discovered:['仅在搜索中发现','只出现在检索结果中，未记录正文读取。'],
    failed:['页面读取失败','页面读取失败，不能当作已经阅读正文。'],
    unknown:['读取方式未知','当前字段不足以判断是否读取、回放或仅有证据链接。'],
  };
  let [label, explanation] = labels[mode];
  if (['provider_cache','offline_corpus','replayed'].includes(mode) && !bound) {
    explanation += ' 当前没有完整的系统对应记录，正文来源链仍需人工核对。';
  }
  return {
    mode,
    label,
    explanation,
    fetched:['live','provider_cache','offline_corpus','replayed','fetched_bound','fetched_unbound'].includes(mode),
    bodyReadRecorded:['live','provider_cache','offline_corpus','replayed','fetched_bound'].includes(mode),
    boundFetched:['live','provider_cache','offline_corpus','replayed','fetched_bound'].includes(mode) && bound,
  };
}

function sourceCacheLabel(source) {
  if (!hasRecordedField(source, 'cache_hit') || typeof source?.cache_hit !== 'boolean') return '缓存状态未记录';
  return source.cache_hit ? '命中读取缓存' : '未命中读取缓存；不等于已证明实时网络请求';
}

function sourceInvocationBindings(source) {
  const discovery = [...new Set(asArray(source?.discovery_invocation_ids).map(normalizedId).filter(Boolean))];
  const fetch = [...new Set([
    normalizedId(source?.fetch_invocation_id),
    normalizedId(source?.fetch_result_invocation_id),
  ].filter(Boolean))];
  return {discovery, fetch};
}

function sourceBindingAuditMarkup(source) {
  const bindings = sourceInvocationBindings(source);
  const discoveryKeys = [...new Set(asArray(source?.discovery_operation_keys).map(normalizedId).filter(Boolean))];
  const fetchKeys = [normalizedId(source?.fetch_operation_key)].filter(Boolean);
  const attempts = sourceFetchAttempts(source);
  const latest = attempts.at(-1) || {};
  const fetchRecordId = latest.fetch_record_id || source?.fetch_record_id || '';
  const invocationButton = id => `<button type="button" class="audit-link-button inline" data-source-invocation="${escapeHTML(id)}">打开角色执行记录</button>`;
  const list = values => values.length ? values.map(id => `<code>${escapeHTML(id)}</code>${invocationButton(id)}`).join(' ') : '<em>未记录</em>';
  return `<details class="source-binding-audit" data-collapse-key="${escapeHTML(collapseKey('source-binding', sourceStableKey(source)))}" open><summary>查看这篇文章为何能对应到本次页面读取</summary><dl><dt>页面读取记录 ID（技术字段）</dt><dd class="audit-mono">${escapeHTML(fetchRecordId || '未记录；证据无法唯一回到阅读记录')}</dd><dt>发现文章的角色执行记录</dt><dd>${list(bindings.discovery)}</dd><dt>发现操作编号（技术字段）</dt><dd>${discoveryKeys.length ? discoveryKeys.map(id=>`<code>${escapeHTML(id)}</code>`).join(' ') : '<em>未记录</em>'}</dd><dt>读取文章的角色执行记录</dt><dd>${list(bindings.fetch)}</dd><dt>读取操作编号（技术字段）</dt><dd>${fetchKeys.length ? fetchKeys.map(id=>`<code>${escapeHTML(id)}</code>`).join(' ') : '<em>未记录</em>'}</dd><dt>读取结果对应的执行记录</dt><dd>${escapeHTML(source?.fetch_result_invocation_id || latest.result_invocation_id || '未记录')}</dd><dt>模型服务与执行方式</dt><dd>${escapeHTML(source?.fetch_provider || latest.provider || '模型服务未记录')} / ${escapeHTML(executionModeName(source?.fetch_execution_mode || latest.execution_mode))}</dd><dt>页面读取方式</dt><dd>${escapeHTML(fetchModeName(source?.fetch_mode || latest.fetch_mode))}</dd><dt>文章与读取记录的对应情况</dt><dd>${escapeHTML(bindingStatusName(source?.fetch_binding_status || latest.binding_status))} · 系统关联检查：${source?.fetch_binding_valid === true || latest.binding_valid === true ? '通过' : '未通过或未记录'}</dd><dt>正文校验值</dt><dd class="audit-mono">${escapeHTML(source?.content_hash || latest.content_hash || '未记录')}</dd><dt>校验值覆盖范围</dt><dd>${escapeHTML(hashScopeName(source?.content_hash_scope || latest.content_hash_scope))}</dd><dt>保存快照校验值（SHA-256）</dt><dd class="audit-mono">${escapeHTML(source?.snapshot_sha256 || latest.snapshot_sha256 || '未记录')}</dd><dt>人工核对方法</dt><dd>打开文章读取对应的角色执行记录，确认操作编号和文章编号一致；再将读取结果状态、页面地址、正文校验值范围和快照校验值与证据卡逐项比对。证据没有相同的页面读取记录 ID 时，不能自动借用这篇文章的最新读取。</dd></dl></details>`;
}

function sourceGroupModel(state) {
  const evidenceModel = closureEvidenceAuditModel(state, true);
  const admitted = evidenceModel.evidence;
  const sources = normalizeSources(state);
  const groups = new Set();
  const verifiedGroups = new Set();
  let missing = 0;
  const supportingIds = new Set();
  requiredSlotProgressModel(state).rows.forEach(row => {
    asArray(recordedArray(row.audit, 'supporting_evidence_ids')).forEach(id => supportingIds.add(String(id || '')));
  });
  const supportingEvidence = admitted.filter(item => (
    supportingIds.has(String(item?.id || ''))
    && item?.independence_status !== 'dependent'
  ));
  supportingEvidence.forEach(item => {
    const linkedSource = sources.find(source => sourceMatchesEvidence(source, item));
    const group = normalizedId(item?.origin_cluster_id || item?.source_cluster_id || linkedSource?.origin_cluster_id);
    if (group) groups.add(group);
    else missing += 1;
    if (item?.independence_status === 'verified' && group) verifiedGroups.add(group);
  });
  const progress = requiredSlotProgressModel(state);
  const gateRows = progress.rows;
  const sourceGateValues = gateRows.map(row => recordedBoolean(row.audit, 'source_gate_passed'));
  const sourceGateAvailable = Boolean(
    progress.requiredRecorded
      && progress.required > 0
      && gateRows.length === progress.required
      && sourceGateValues.every(value => value !== null),
  );
  const sourceGatePassed = sourceGateValues.filter(value => value === true).length;
  return {
    groups,
    verifiedGroups,
    missing,
    admittedCount:evidenceModel.available ? supportingEvidence.length : null,
    knownAdmittedCount:admitted.length,
    available:evidenceModel.available,
    invalidEvidenceIds:evidenceModel.invalidEvidenceIds,
    supportingEvidence,
    sourceGateAvailable,
    sourceGatePassed:sourceGateAvailable ? sourceGatePassed : null,
    sourceGateRequired:progress.required,
    sourceGateValues,
  };
}

function sourcePageCoverageModel(state) {
  const sources = normalizeSources(state);
  const audit = window.__latestAudit || null;
  const unavailable = (reason, extra = {}) => ({
    numerator: null,
    denominator: null,
    ratio: null,
    evidenceSources: [],
    fetchedSources: [],
    denominatorSource: reason,
    ...extra,
  });
  const statusOf = value => String(value?.status || '').toLowerCase().replace(/[-\s]+/g, '_');
  const modeOf = value => String(value?.fetch_mode || value?.mode || '').toLowerCase().replace(/[-\s]+/g, '_');
  const successful = value => (
    statusOf(value) === 'fetched'
      && modeOf(value) !== 'evidence_only'
      && isServerBoundFetch(value)
      && Boolean(exactFetchRecordId(value))
  );
  const uniqueFetches = rows => {
    const byId = new Map();
    rows.forEach(row => {
      const id = exactFetchRecordId(row);
      if (id && !byId.has(id)) byId.set(id, row);
    });
    return [...byId.values()];
  };
  const duplicateFetchIds = rows => {
    const counts = new Map();
    rows.forEach(row => {
      const id = exactFetchRecordId(row);
      if (id) counts.set(id, (counts.get(id) || 0) + 1);
    });
    return [...counts.entries()].filter(([, count]) => count > 1);
  };
  let fetchedSources = [];
  let denominatorSource = '';
  let auditWindowComplete = true;
  let malformedSuccessful = 0;
  let duplicateSuccessfulIds = [];

  if (audit?.available && audit.sourceFetchesRecorded) {
    const page = audit.pages?.source_fetches;
    if (page?.windowed !== true || page.hasMore !== false) {
      const loaded = asArray(audit.sourceFetches).length;
      return unavailable(
        page?.hasMore === true
          ? `source_fetches 仍有未载入的分页记录：当前 ${loaded} 条，先加载完整审计后再计算`
          : 'source_fetches 是旧版未声明完整性的数组；不能把当前窗口当作全量 Fetch 分母',
        {loadedDenominator: loaded, auditWindowComplete: false},
      );
    }
    const sourceFetchRows = asArray(audit.sourceFetches);
    malformedSuccessful = sourceFetchRows.filter(row => (
      statusOf(row) === 'fetched'
      && modeOf(row) !== 'evidence_only'
      && isServerBoundFetch(row)
      && !exactFetchRecordId(row)
    )).length;
    fetchedSources = uniqueFetches(sourceFetchRows.filter(successful));
    duplicateSuccessfulIds = duplicateFetchIds(sourceFetchRows.filter(successful));
    denominatorSource = `完整 durable source_fetches：${fetchedSources.length} 条唯一成功 Fetch 记录；按 fetch_record_id 计数，不按文章或最新尝试合并`;
  } else {
    // Public state contains useful context for the graph, but it cannot prove
    // the denominator is complete. Never turn that partial view into a normal
    // percentage when the durable fetch ledger is absent or unavailable.
    const reason = audit?.available === false
      ? 'durable source_fetches 审计接口不可用'
      : !audit?.sourceFetchesRecorded
        ? 'durable source_fetches 审计未记录'
        : 'durable source_fetches 审计窗口不可用';
    return unavailable(`${reason}；公开 state 只作为已知下界，不能形成可计算分母`, {
      auditWindowComplete: false,
      lowerBound: true,
    });
  }
  if (duplicateSuccessfulIds.length) {
    return unavailable(
      `${denominatorSource}；成功 Fetch ID 重复：${duplicateSuccessfulIds.map(([id, count]) => `${id}（${count} 条）`).join('、')}，分母冲突不可验证`,
      {auditWindowComplete, duplicateFetchIds: duplicateSuccessfulIds.map(([id]) => id)},
    );
  }
  if (malformedSuccessful) {
    return unavailable(
      `${denominatorSource}；另有 ${malformedSuccessful} 条成功读取缺少 fetch_record_id，精确分母不完整`,
      {loadedDenominator: fetchedSources.length, auditWindowComplete},
    );
  }
  if (!fetchedSources.length) {
    return unavailable(`${denominatorSource}；已记录 0 条可计 Fetch，分母为 0`, {
      denominator: 0,
      loadedDenominator: 0,
      auditWindowComplete,
    });
  }

  // Only evidence admitted by the required-slot closure and bound to the same
  // immutable Fetch record can enter the numerator. Source-wide URL matching
  // would silently promote an unrelated retry or an excluded Evidence row.
  const closureEvidence = closureEvidenceAuditModel(state, true);
  if (!closureEvidence.available) {
    return {
      numerator: null,
      denominator: fetchedSources.length,
      ratio: null,
      evidenceSources: [],
      fetchedSources,
      denominatorSource: `${denominatorSource}；必需槽位闭包审计未完整记录，Evidence 分子不可验证`,
      auditWindowComplete,
      closureAvailable: false,
    };
  }
  const evidenceFetchIds = new Set();
  const evidenceSources = [];
  closureEvidence.supportingEvidence.forEach(item => {
    const binding = exactEvidenceFetchBinding(item, state);
    if (binding.status !== 'bound' || !binding.fetch_record_id) return;
    evidenceFetchIds.add(binding.fetch_record_id);
  });
  fetchedSources.forEach(fetch => {
    if (evidenceFetchIds.has(exactFetchRecordId(fetch))) evidenceSources.push(fetch);
  });
  const numerator = evidenceSources.length;
  return {
    numerator,
    denominator: fetchedSources.length,
    ratio: numerator / fetchedSources.length,
    evidenceSources,
    fetchedSources,
    denominatorSource: `${denominatorSource}；分子仅含闭包采纳且 exact fetch_record_id + server-bound 绑定通过的 Evidence`,
    auditWindowComplete,
    closureAvailable: true,
  };
}

function normalizeSources(state) {
  const sources=asArray(state?.sources);
  if (sources.length) return sources;
  const byUrl=new Map();
  asArray(state?.evidence).forEach(item=>{
    const url=normalizedSourceUrl(item?.source_url);
    if(!url||byUrl.has(url))return;
    byUrl.set(url,{id:item?.source_id||`legacy-${byUrl.size}`,url:item.source_url,final_url:item.source_url,title:item.source_title,source_type:'web',snippet:'历史运行仅保存了 Evidence；没有页面读取摘要',query_texts:[],status:'evidence_only',iteration:null,content_hash:item.content_hash,error:null,parser_version:'legacy-run',fetch_mode:'evidence_only',fetch_binding_status:'evidence_only',fetch_binding_valid:false});
  });
  return [...byUrl.values()];
}

function normalizeQueryText(value) {
  return String(value ?? '').trim().replace(/\s+/g, ' ').toLocaleLowerCase();
}

function sourceQueryIndices(source, queries = []) {
  const declaredIds = new Set([
    ...asArray(source?.query_ids),
    source?.query_id,
  ].map(normalizedId).filter(Boolean));
  const declaredTexts = new Set(asArray(source?.query_texts).map(normalizeQueryText).filter(Boolean));
  return asArray(queries).map((query, index) => {
    const queryId = normalizedId(query?.query_id || query?.id);
    const queryText = normalizeQueryText(query?.text || query?.query);
    return (queryId && declaredIds.has(queryId)) || (queryText && declaredTexts.has(queryText)) ? index : -1;
  }).filter(index => index >= 0);
}

function sourceQueryLabels(source, queries = []) {
  const associations = sourceQueryIndices(source, queries).map(index => ({
    index,
    text: String(queries[index]?.text || queries[index]?.query || '').trim(),
  }));
  const knownTexts = new Set(associations.map(item => normalizeQueryText(item.text)).filter(Boolean));
  asArray(source?.query_texts).forEach(text => {
    const normalized = normalizeQueryText(text);
    if (!normalized || knownTexts.has(normalized)) return;
    knownTexts.add(normalized);
    associations.push({index:null, text:String(text).trim()});
  });
  return associations;
}

function sourceFetchGraphItems(sources) {
  const items = [];
  const nodeOccurrences = new Map();
  asArray(sources).forEach((source, sourceIndex) => {
    const attempts = sourceFetchAttempts(source);
    // Keep every immutable attempt visible. A source-level "latest fetch"
    // node would make an Evidence edge look exact when it is only source-wide.
    const rows = attempts.length ? attempts : [null];
    rows.forEach((attempt, attemptIndex) => {
      const row = attempt || {};
      const fetchRecordId = exactFetchRecordId(row) || (!attempt ? exactFetchRecordId(source) : '');
      const status = row.status || source?.status || 'unknown';
      const mode = row.fetch_mode || source?.fetch_mode || 'unknown';
      const sourceId = normalizedId(row.source_id || source?.id || source?.source_id);
      const graphKey = [
        fetchRecordId ? `record:${fetchRecordId}` : 'record:missing',
        `source:${sourceStableKey(source, sourceIndex)}`,
        `attempt:${row.attempt ?? 'missing'}`,
        `invocation:${normalizedId(row.invocation_id)}`,
        `operation:${normalizedId(row.operation_key)}`,
        `recorded:${row.recorded_at || row.fetched_at || ''}`,
      ].join('|');
      const baseNodeId = graphStableNodeId('fetch', {graph_key:graphKey});
      const occurrence = (nodeOccurrences.get(baseNodeId) || 0) + 1;
      nodeOccurrences.set(baseNodeId, occurrence);
      items.push({
        id: occurrence === 1 ? baseNodeId : `${baseNodeId}-${occurrence}`,
        graph_node_id: occurrence === 1 ? baseNodeId : `${baseNodeId}-${occurrence}`,
        graph_key: graphKey,
        fetch_record_id: fetchRecordId,
        source_id: sourceId,
        source_index: sourceIndex,
        source,
        attempt: row,
        attempts,
        attempt_index: attemptIndex,
        is_placeholder: !attempt,
        status,
        fetch_mode: mode,
        execution_mode: row.execution_mode || source?.fetch_execution_mode || 'unknown',
        invocation_id: row.invocation_id || source?.fetch_invocation_id || '',
        result_invocation_id: row.result_invocation_id || source?.fetch_result_invocation_id || '',
        operation_key: row.operation_key || source?.fetch_operation_key || '',
        provider: row.provider || source?.fetch_provider || '',
        content_hash: attempt ? (row.content_hash || '') : (source?.content_hash || ''),
        content_hash_scope: attempt ? (row.content_hash_scope || 'unknown') : (source?.content_hash_scope || 'unknown'),
        snapshot_sha256: attempt ? (row.snapshot_sha256 || '') : (source?.snapshot_sha256 || ''),
        snapshot_available: attempt
          ? fetchSnapshotAvailable(row)
          : Boolean(source?.snapshot_available || source?.snapshot_sha256),
        binding_status: attempt ? (row.binding_status || 'legacy_unverified') : (source?.fetch_binding_status || 'legacy_unverified'),
        binding_valid: attempt ? row.binding_valid : source?.fetch_binding_valid,
        recorded_at: row.recorded_at || '',
        fetched_at: row.fetched_at || source?.fetched_at || '',
        title: fetchRecordId
          ? `${fetchAttemptStatusName(status)} · ${String(fetchRecordId).slice(0, 14)}`
          : 'Fetch attempt 未记录',
      });
    });
  });
  const ordered = items
    .filter(item => !item.is_placeholder)
    .map(item => ({item, order:fetchAttemptOrderValue(item.attempt)}))
    .filter(value => value.order)
    .sort((left, right) => left.order.timestamp - right.order.timestamp || String(left.item.fetch_record_id || '').localeCompare(String(right.item.fetch_record_id || '')) || String(left.item.graph_node_id).localeCompare(String(right.item.graph_node_id)));
  const rankByTimestamp = new Map();
  ordered.forEach(({order}) => {
    if (!rankByTimestamp.has(order.timestamp)) rankByTimestamp.set(order.timestamp, rankByTimestamp.size + 1);
  });
  const countByTimestamp = new Map();
  ordered.forEach(({order}) => countByTimestamp.set(order.timestamp, (countByTimestamp.get(order.timestamp) || 0) + 1));
  items.forEach(item => {
    const order = fetchAttemptOrderValue(item.attempt);
    if (!order) {
      item.order_label = '顺序未记录';
      item.order_detail = '没有可排序的完成或持久化时间，不能从文章编号或数组位置推断先后。';
      return;
    }
    const rank = rankByTimestamp.get(order.timestamp);
    const fieldLabel = order.key === 'recorded_at' ? '持久化记录序' : order.key === 'fetched_at' ? '读取完成时间' : '完成记录时间';
    if ((countByTimestamp.get(order.timestamp) || 0) > 1) {
      item.order_label = `${fieldLabel}并列 ${rank}`;
      item.order_detail = `${fieldLabel}相同，当前有 ${countByTimestamp.get(order.timestamp)} 条并发/同刻记录，先后未记录。`;
    } else {
      item.order_label = `${fieldLabel} #${rank}`;
      item.order_detail = `按 ${order.key} 排序；这不是 attempt 重试编号。`;
    }
    item.order_timestamp = order.raw;
    item.order_field = order.key;
  });
  return items;
}

function graphEvidenceTargetRelation(role, binding) {
  if (binding?.status !== 'bound') {
    return {
      draw: false,
      status: 'rejected',
      marker: null,
      label: '未连到回答目标',
      detail: `精确 Fetch 绑定${binding?.label ? `：${binding.label}` : '未通过'}；该 Evidence 不能作为目标支持路径。`,
    };
  }
  if (role?.kind === 'excluded') {
    return {
      draw: false,
      status: 'excluded',
      marker: null,
      label: '候选已排除',
      detail: '该 Evidence 已被相关性、共识或其他审计规则排除，不进入回答目标关系。',
    };
  }
  const kind = role?.kind || 'context';
  return {
    draw: true,
    status: 'linked',
    marker: graphMarkerForStance(kind),
    label: '已连到回答目标',
    detail: `${stanceName(kind)} Evidence 已通过精确 Fetch 绑定并进入目标关系图。`,
  };
}

function renderResearchGraph(state) {
  const svg=$('researchGraph');
  const queries=asArray(state.queries), sources=asArray(normalizeSources(state)), fetches=sourceFetchGraphItems(sources), evidence=asArray(state.evidence), slots=asArray(state.plan?.slots);
  const relevanceThreshold=relevanceAdmissionThreshold(state);
  $('sourceCount').textContent=sources.length;
  const roles=evidence.map(item=>evidenceEffectiveRole(item,state));
  const excludedCount=roles.filter(role=>role.kind==='excluded').length;
  const supportCount=roles.filter(role=>role.kind==='supports').length;
  const conflictCount=roles.filter(role=>role.kind==='contradicts').length;
  const queryNodeIds=queries.map((item,index)=>graphStableNodeId('query',item,index));
  const sourceNodeIds=sources.map((item,index)=>graphStableNodeId('source',item,index));
  const fetchNodeIds=fetches.map((item,index)=>item.graph_node_id||graphStableNodeId('fetch',item,index));
  const evidenceNodeIds=evidence.map((item,index)=>graphStableNodeId('evidence',item,index));
  const targetNodeIds=slots.map((item,index)=>graphStableNodeId('target',item,index));
  const fetchByRecordId = new Map();
  fetches.forEach((item,index)=>{
    if (item.is_placeholder || !item.fetch_record_id) return;
    const list=fetchByRecordId.get(item.fetch_record_id)||[];
    list.push(index);
    fetchByRecordId.set(item.fetch_record_id,list);
  });
  const bindingRows=evidence.map(item=>exactEvidenceFetchBinding(item,state));
  const exactEvidenceCount = bindingRows.filter(item=>item.status==='bound').length;
  const unboundEvidenceCount = bindingRows.filter(item=>['unbound','unverified','invalid','not_loaded'].includes(item.status)).length;
  const unloadedFetchCount = bindingRows.filter(item=>item.status==='not_loaded').length;
  const invalidBindingCount = bindingRows.filter(item=>item.status==='invalid').length;
  const targetRelations = evidence.map((item, index) => graphEvidenceTargetRelation(roles[index], bindingRows[index]));
  const blockedTargetRelationCount = targetRelations.filter(item => !item.draw).length;
  const recordedFetchCount = fetches.filter(item => !item.is_placeholder && (item.attempts.length || item.fetch_record_id)).length;
  const unrecordedFetchNodeCount = fetches.filter(item=>item.is_placeholder).length;
  const duplicateFetchCount = [...fetchByRecordId.values()].filter(items=>items.length>1).reduce((sum,items)=>sum+items.length,0);
  const thresholdNarrative=relevanceThreshold===null?'本次运行未记录相关性准入阈值，未用当前默认值重判':`相关性准入 ${Math.round(relevanceThreshold*100)} / 100`;
  $('graphNarrative').innerHTML=`<span>当前图谱</span><strong>${queries.length} 条检索路线发现 ${sources.length} 篇文章，${recordedFetchCount} 个独立 Fetch attempt 节点，抽取 ${evidence.length} 条证据，其中 ${supportCount} 条计入支持、${conflictCount} 条计入冲突、${excludedCount} 条按已记录审计被排除，关联 ${slots.length} 个回答目标</strong><small>${escapeHTML(thresholdNarrative)} · ${exactEvidenceCount} 条 Evidence 通过唯一 Fetch 绑定并连线；${blockedTargetRelationCount} 条 Evidence→Target 关系未画线（${unboundEvidenceCount} 条未形成可验证绑定，${excludedCount} 条被排除），${unloadedFetchCount} 条 Fetch 行未载入，${invalidBindingCount} 条因审计冲突拒绝${duplicateFetchCount ? `，${duplicateFetchCount} 条 Fetch ID 重复` : ''}${unrecordedFetchNodeCount ? `，另有 ${unrecordedFetchNodeCount} 个文章没有 Fetch attempt 记录` : ''}。缺失或冲突不会隐式回链</small>`;
  renderGraphAccessibleList(state,queries,sources,fetches,evidence,slots,roles);
  while(svg.firstChild)svg.removeChild(svg.firstChild);
  if(!queries.length&&!sources.length){svg.setAttribute('viewBox','0 0 1000 300');addSvgText(svg,500,150,'等待智能体建立调研关系图','graph-empty');return}
  const defs=svgElement('defs');
  [['graphArrowNeutral','#9dad9f'],['graphArrowSupport','#195b43'],['graphArrowConflict','#a7422f'],['graphArrowContext','#c88a28']].forEach(([id,color])=>{
    const marker=svgElement('marker',{id,viewBox:'0 0 10 10',refX:'9',refY:'5',markerWidth:'6',markerHeight:'6',orient:'auto-start-reverse',markerUnits:'strokeWidth'});
    marker.appendChild(svgElement('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:color}));
    defs.appendChild(marker);
  });
  svg.appendChild(defs);
  const columns=[{x:30,w:220,label:'检索路线'},{x:285,w:220,label:'调研文章'},{x:540,w:220,label:'页面读取'},{x:795,w:220,label:'原文证据'},{x:1050,w:220,label:'回答目标'}];
  const rows=Math.max(queries.length,sources.length,fetches.length,evidence.length,slots.length,3);
  const height=Math.max(430,rows*88+82);svg.setAttribute('viewBox',`0 0 1300 ${height}`);
  columns.forEach(column=>{addSvgText(svg,column.x,28,column.label,'graph-column-label');addSvgLine(svg,column.x,42,column.x+column.w,42,'graph-column-rule')});
  const qPos=positions(queries,columns[0],height),sPos=positions(sources,columns[1],height),fPos=positions(fetches,columns[2],height),ePos=positions(evidence,columns[3],height),slotPos=positions(slots,columns[4],height);
  sources.forEach((source,si)=>sourceQueryIndices(source,queries).forEach(qi=>{if(qPos[qi]&&sPos[si])addSvgCurve(svg,qPos[qi],sPos[si],`edge query-source`,queryNodeIds[qi],sourceNodeIds[si],'graphArrowNeutral')}));
  fetches.forEach((item,index)=>{const sourcePosition=sPos[item.source_index];if(sourcePosition)addSvgCurve(svg,sourcePosition,fPos[index],`edge source-fetch${item.is_placeholder?' placeholder':''}`,sourceNodeIds[item.source_index],fetchNodeIds[index],'graphArrowNeutral')});
  evidence.forEach((item,ei)=>{
    const role=roles[ei];
    const relation=role.kind==='excluded'?'excluded':role.kind;
    const binding=bindingRows[ei];
    const targetRelation=targetRelations[ei];
    const fetchIndex=binding.fetch ? fetches.findIndex(candidate=>candidate.graph_node_id===binding.fetch.graph_node_id) : -1;
    if (binding.status==='bound' && fetchIndex>=0) addSvgCurve(svg,fPos[fetchIndex],ePos[ei],`edge fetch-evidence ${relation}`,fetchNodeIds[fetchIndex],evidenceNodeIds[ei],role.kind==='excluded'?'graphArrowNeutral':graphMarkerForStance(role.kind));
    const ti=slots.findIndex(slot=>String(slot?.id||'')===String(item?.slot_id||''));
    if(ti>=0 && targetRelation.draw)addSvgCurve(svg,ePos[ei],slotPos[ti],`edge evidence-target ${relation}`,evidenceNodeIds[ei],targetNodeIds[ti],targetRelation.marker);
  });
  queries.forEach((item,index)=>addGraphNode(svg,qPos[index],`Q${String(index+1).padStart(2,'0')}`,item.text,'query','query',queryNodeIds[index],()=>inspectGraphItem('query',item,state)));
  sources.forEach((item,index)=>{const assessment=sourceReadAssessment(item);const iteration=finiteValue(item?.iteration);addGraphNode(svg,sPos[index],`A${String(index+1).padStart(2,'0')} · R${iteration===null?'?':iteration}`,item.title,`source ${assessment.mode}`,'source',sourceNodeIds[index],()=>inspectSource(item,evidence),`${assessment.label} · ${sourceTypeName(item.source_type)}`)});
  fetches.forEach((item,index)=>{const exact=item.fetch_record_id?`Fetch ${String(item.fetch_record_id).slice(0,12)}`:'Fetch ID 未记录';const binding=bindingStatusName(item.binding_status);const order=item.order_label||'顺序未记录';addGraphNode(svg,fPos[index],`F${String(index+1).padStart(2,'0')} · ${fetchAttemptLabel(item.attempt)}`,item.title,`fetch ${String(item.status||'unknown').toLowerCase()}`,'fetch',fetchNodeIds[index],()=>inspectGraphItem('fetch',item,state),`${fetchAttemptStatusName(item.status)} · ${order} · ${exact} · ${binding}`)});
  evidence.forEach((item,index)=>{const role=roles[index];const excluded=role.kind==='excluded';const trace=bindingRows[index];addGraphNode(svg,ePos[index],`${item.id} · ${excluded?'已排除':stanceName(role.kind)}`,item.quote,`evidence ${excluded?'excluded':role.kind} ${trace.status==='bound'?'':'unbound'}`,'evidence',evidenceNodeIds[index],()=>inspectGraphItem('evidence',item,state),`${excluded?role.reason:`材料用途：${stanceName(role.kind)}`} · ${trace.label}`)});
  slots.forEach((item,index)=>addGraphNode(svg,slotPos[index],`目标 ${String(index+1).padStart(2,'0')}`,item.description,'target','target',targetNodeIds[index],()=>inspectGraphItem('target',item,state),item.value?'已有结论':'调查中'));
  applyGraphView();
}

function renderGraphAccessibleList(state,queries,sources,fetches,evidence,slots,roles){
  const entries=[
    ...queries.map((item,index)=>({kind:'query',index,nodeId:graphStableNodeId('query',item,index),badge:`Q${String(index+1).padStart(2,'0')}`,label:item?.text||'历史字段未记录',detail:`${methodName(item?.strategy)||'检索策略未记录'} · ${item?.subgoal_id||'子目标未记录'}`,item})),
    ...sources.map((item,index)=>({kind:'source',index,nodeId:graphStableNodeId('source',item,index),badge:`A${String(index+1).padStart(2,'0')}`,label:item?.title||'历史字段未记录',detail:`${sourceReadAssessment(item).label} · ${sourceTypeName(item?.source_type)}`,item})),
    ...fetches.map((item,index)=>({kind:'fetch',index,nodeId:item.graph_node_id||graphStableNodeId('fetch',item,index),badge:`F${String(index+1).padStart(2,'0')}`,label:item.title||'页面读取未记录',detail:`${item.fetch_record_id ? `页面读取记录 ID ${item.fetch_record_id}` : '页面读取记录 ID 未记录'} · ${fetchAttemptStatusName(item.status)} · ${item.order_label||'顺序未记录'} · ${bindingStatusName(item.binding_status)}`,item})),
    ...evidence.map((item,index)=>{const trace=exactEvidenceFetchBinding(item,state);return {kind:'evidence',index,nodeId:graphStableNodeId('evidence',item,index),badge:item?.id||`证据 ${index+1}`,label:item?.claim||item?.quote||'历史字段未记录',detail:`${roles[index]?.kind==='excluded'?`已排除：${roles[index].reason}`:`材料用途：${stanceName(roles[index]?.kind)}`} · ${trace.label}`,item,tone:roles[index]?.kind||'excluded'}}),
    ...slots.map((item,index)=>({kind:'target',index,nodeId:graphStableNodeId('target',item,index),badge:`目标 ${String(index+1).padStart(2,'0')}`,label:item?.description||'历史字段未记录',detail:`${item?.required===false?'可选目标，不计入必答目标完成度':'必需目标'} · ${item?.value||'调查中'}`,item}))
  ];
  if(!entries.length){$('graphAccessibleList').innerHTML='<span class="graph-accessible-empty">尚无关系图节点。</span>';return}
  $('graphAccessibleList').innerHTML=entries.map(entry=>`<button type="button" class="graph-accessible-node ${entry.kind} ${escapeHTML(entry.tone||'')}" data-graph-access-kind="${entry.kind}" data-graph-access-index="${entry.index}" data-graph-node-id="${entry.nodeId}" aria-pressed="${String(graphFocusedNode===entry.nodeId)}"><span>${escapeHTML(entry.badge)} · ${escapeHTML(graphKindName(entry.kind))}</span><strong>${escapeHTML(entry.label)}</strong><small>${escapeHTML(entry.detail)}</small></button>`).join('');
  $('graphAccessibleList').querySelectorAll('[data-graph-access-kind]').forEach(button=>button.addEventListener('click',()=>{
    const kind=button.dataset.graphAccessKind;
    const collections={query:queries,source:sources,fetch:fetches,evidence,target:slots};
    const item=collections[kind]?.[Number(button.dataset.graphAccessIndex)];
    if(!item)return;
    focusResearchGraph(kind,item,false);
    if(kind==='source')inspectSource(item,evidence);else inspectGraphItem(kind,item,state);
    announceLive(`${graphKindName(kind)}已在全图中聚焦，并在下方打开完整内容。`,`graph-focus:${button.dataset.graphNodeId}`,true);
  }));
}

function positions(items,column,height){const gap=(height-120)/Math.max(items.length,1);return items.map((_,i)=>({x:column.x,y:62+i*gap,w:column.w,h:68}))}
function svgElement(name,attrs={}){const el=document.createElementNS('http://www.w3.org/2000/svg',name);Object.entries(attrs).forEach(([k,v])=>el.setAttribute(k,v));return el}
function addSvgText(svg,x,y,text,cls){const el=svgElement('text',{x,y,class:cls});el.textContent=text;svg.appendChild(el)}
function addSvgLine(svg,x1,y1,x2,y2,cls){svg.appendChild(svgElement('line',{x1,y1,x2,y2,class:cls}))}
function graphMarkerForStance(stance){return stance==='contradicts'?'graphArrowConflict':stance==='context'?'graphArrowContext':stance==='supports'?'graphArrowSupport':'graphArrowNeutral'}
function addSvgCurve(svg,left,right,cls,from,to,marker){const x1=left.x+left.w,y1=left.y+left.h/2,x2=right.x,y2=right.y+right.h/2;svg.appendChild(svgElement('path',{d:`M${x1} ${y1} C${x1+42} ${y1},${x2-42} ${y2},${x2} ${y2}`,class:cls,'data-from':from,'data-to':to,'marker-end':`url(#${marker})`}))}
function wrapGraphLabel(value,maxLength=30){const text=String(value||'');if(text.length<=maxLength)return[text];const second=text.slice(maxLength-1,maxLength*2-3);return[`${text.slice(0,maxLength-1)}…`,text.length>maxLength*2-3?`${second}…`:second];}
function addGraphNode(svg,pos,badge,label,cls,kind,nodeId,onClick,stateLabel=''){const accessible=`${graphKindName(kind)}：${label}；状态：${badge}${stateLabel?`；${stateLabel}`:''}`;const selected=graphFocusedNode===nodeId;const group=svgElement('g',{class:`graph-node ${cls}${selected?' selected':''}`,transform:`translate(${pos.x} ${pos.y})`,tabindex:'0','data-kind':kind,'data-node-id':nodeId,role:'button','aria-label':accessible,'aria-pressed':String(selected)});const rect=svgElement('rect',{width:pos.w,height:pos.h,rx:4});const badgeText=svgElement('text',{x:10,y:17,class:'node-badge'});badgeText.textContent=truncate(badge,34);const labelText=svgElement('text',{x:10,y:38,class:'node-label'});wrapGraphLabel(label,30).forEach((line,index)=>{const tspan=svgElement('tspan',{x:10,dy:index?'15':'0'});tspan.textContent=line;labelText.appendChild(tspan)});const state=svgElement('text',{x:10,y:62,class:'node-state'});state.textContent=truncate(stateLabel,34);const title=svgElement('title');title.textContent=accessible;group.append(rect,badgeText,labelText,state,title);const activate=()=>{svg.querySelectorAll('.graph-node.selected').forEach(node=>{node.classList.remove('selected');node.setAttribute('aria-pressed','false')});group.classList.add('selected');group.setAttribute('aria-pressed','true');graphFocusedNode=nodeId;graphFocusedLabel=`${graphKindName(kind)} · ${truncate(label,14)}`;graphFilter='all';document.querySelectorAll('[data-graph-filter]').forEach(item=>{item.classList.toggle('active',item.dataset.graphFilter==='all');item.setAttribute('aria-pressed',String(item.dataset.graphFilter==='all'))});applyGraphView();onClick?.()};if(onClick){group.classList.add('clickable');group.addEventListener('click',activate);group.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();activate()}})}svg.appendChild(group)}

function graphPathIds(svg,seed){const ancestors=new Set([seed]),descendants=new Set([seed]);let changed=true;while(changed){changed=false;svg.querySelectorAll('.edge').forEach(edge=>{const from=edge.dataset.from,to=edge.dataset.to;if(ancestors.has(to)&&!ancestors.has(from)){ancestors.add(from);changed=true}if(descendants.has(from)&&!descendants.has(to)){descendants.add(to);changed=true}})}return new Set([...ancestors,...descendants])}
function focusResearchGraph(kind, item, scroll = true) {
  const state = window.__latestState || {};
  const sources = asArray(normalizeSources(state));
  const collections = {query:asArray(state.queries),source:sources,fetch:sourceFetchGraphItems(sources),evidence:asArray(state.evidence),target:asArray(state.plan?.slots)};
  const items = asArray(collections[kind]);
  const index = items.findIndex(value => {
    const candidateId=kind==='fetch'
      ? value.graph_node_id || graphStableNodeId('fetch',value)
      : graphStableNodeId(kind,value);
    const wantedId=item?.graph_node_id || graphStableNodeId(kind,item);
    if (candidateId===wantedId) return true;
    if (kind === 'source') return value.id === item.id || value.url === item.url || value.final_url === item.final_url;
    if (kind === 'fetch') return Boolean(item.fetch_record_id && value.fetch_record_id === item.fetch_record_id && !value.is_placeholder);
    return value.id ? value.id === item.id : value.text === item.text;
  });
  if (index < 0) return;
  graphFocusedNode = kind==='fetch'
    ? (items[index].graph_node_id || graphStableNodeId('fetch',items[index],index))
    : graphStableNodeId(kind,items[index],index);
  graphFocusedLabel = `${graphKindName(kind)} · ${truncate(item.title || item.claim || item.description || item.text, 14)}`;
  graphFilter = 'all';
  document.querySelectorAll('[data-graph-filter]').forEach(button => {
    const selected = button.dataset.graphFilter === 'all';
    button.classList.toggle('active', selected);
    button.setAttribute('aria-pressed', String(selected));
  });
  $('researchGraph').querySelectorAll('.graph-node').forEach(node => {
    const selected = node.dataset.nodeId === graphFocusedNode;
    node.classList.toggle('selected', selected);
    node.setAttribute('aria-pressed', String(selected));
  });
  applyGraphView();
  if (scroll) scrollToResearchTarget('graphSection',`${graphKindName(kind)}关系路径`);
}
function applyGraphView(){
  const svg=$('researchGraph');
  const shell=svg.closest('.graph-shell');
  const readingWidth=Math.round(1300*graphZoom);
  svg.style.minWidth=graphFitMode?'0':`${readingWidth}px`;
  svg.style.width=graphFitMode?'100%':`${readingWidth}px`;
  shell?.classList.toggle('fit-mode',graphFitMode);
  $('graphZoomLabel').textContent=graphFitMode?'全局':`${Math.round(graphZoom*100)}%`;
  $('graphFit').textContent=graphFitMode?'已显示全图':'压缩全图';
  $('graphFit').classList.toggle('active',graphFitMode);
  $('graphPanHint').textContent=graphFitMode?'全局总览':'放大阅读';
  svg.querySelectorAll('.graph-node,.edge').forEach(item=>item.classList.remove('graph-muted','graph-focus'));
  if(graphFilter==='sources'){
    svg.querySelectorAll('.graph-node[data-kind="evidence"],.graph-node[data-kind="target"],.edge.fetch-evidence,.edge.evidence-target').forEach(item=>item.classList.add('graph-muted'));
    svg.querySelectorAll('.graph-node[data-kind="query"],.graph-node[data-kind="source"],.graph-node[data-kind="fetch"],.edge.query-source,.edge.source-fetch').forEach(item=>item.classList.add('graph-focus'));
  }
  if(graphFilter==='conflicts'){
    svg.querySelectorAll('.graph-node,.edge').forEach(item=>item.classList.add('graph-muted'));
    const conflictNodes=[...svg.querySelectorAll('.graph-node.evidence.contradicts')].map(item=>item.dataset.nodeId);
    const relevant=conflictNodes.length?new Set(conflictNodes.flatMap(id=>[...graphPathIds(svg,id)])):new Set();
    svg.querySelectorAll('.graph-node,.edge').forEach(item=>{
      const isNode=item.classList.contains('graph-node');
      const relevantItem=isNode?relevant.has(item.dataset.nodeId):relevant.has(item.dataset.from)&&relevant.has(item.dataset.to);
      if(relevantItem){item.classList.remove('graph-muted');item.classList.add('graph-focus')}
    });
  }
  if(graphFocusedNode){
    const relevant=graphPathIds(svg,graphFocusedNode);
    svg.querySelectorAll('.graph-node,.edge').forEach(item=>{
      const isNode=item.classList.contains('graph-node');
      const relevantItem=isNode?relevant.has(item.dataset.nodeId):relevant.has(item.dataset.from)&&relevant.has(item.dataset.to);
      item.classList.toggle('graph-muted',!relevantItem);
      item.classList.toggle('graph-focus',relevantItem);
    });
    $('graphClearFocus').disabled=false;
    $('graphClearFocus').textContent=`退出聚焦：${graphFocusedLabel||'当前路径'}`;
  }else{
    $('graphClearFocus').disabled=true;
    $('graphClearFocus').textContent='显示全部路径';
  }
  document.querySelectorAll('[data-graph-node-id]').forEach(button=>{
    const selected=button.dataset.graphNodeId===graphFocusedNode;
    button.classList.toggle('selected',selected);
    button.setAttribute('aria-pressed',String(selected));
  });
}

function sourceSnapshotActionLabel(assessment) {
  return ({
    live:'查看已保存的读取快照并高亮证据',
    provider_cache:'查看缓存结果快照（不是实时网络正文）',
    offline_corpus:'查看离线语料快照（不是在线正文）',
    replayed:'查看回放结果快照（本次没有再次读取 Provider）',
    fetched_bound:'查看已绑定读取快照',
    fetched_unbound:'查看未绑定快照（不能证明本次读取）',
    evidence_only:'查看已保存证据快照（没有 fetch 绑定）',
  })[assessment.mode] || '查看已保存快照（读取方式未完全记录）';
}

function fetchSnapshotAvailable(fetch) {
  if (!fetch || typeof fetch !== 'object') return false;
  if (typeof fetch.snapshot_available === 'boolean') return fetch.snapshot_available;
  return Boolean(String(fetch.snapshot_sha256 || '').trim());
}

function exactSnapshotCapability(trace) {
  if (!trace || trace.status !== 'bound' || !trace.fetch) {
    return {available:false, label:'精确 Fetch 未通过绑定核验，不能打开本次证据快照', fetch:null, fetch_record_id:''};
  }
  const fetch = trace.fetch.attempt || trace.fetch;
  const fetchRecordId = exactFetchRecordId(fetch) || normalizedId(trace.fetch_record_id);
  if (!fetchRecordId) {
    return {available:false, label:'精确 Fetch 缺少 fetch_record_id，不能定向读取快照', fetch:null, fetch_record_id:''};
  }
  return fetchSnapshotAvailable(fetch)
    ? {available:true, label:'打开与 Evidence 精确绑定的保存快照', fetch, fetch_record_id:fetchRecordId}
    : {available:false, label:'精确 Fetch 已绑定，但该 attempt 没有保存快照', fetch, fetch_record_id:fetchRecordId};
}

function snapshotIdentityAssessment(snapshot, source, fetchAttempt) {
  const expectedSourceId = normalizedId(source?.id || source?.source_id);
  const expectedFetchRecordId = exactFetchRecordId(fetchAttempt);
  const actualSourceId = normalizedId(snapshot?.source_id);
  const actualFetchRecordId = normalizedId(snapshot?.fetch_record_id);
  const reasons = [];
  if (!expectedSourceId || !expectedFetchRecordId) reasons.push('页面没有完整的期望 source_id / fetch_record_id。');
  if (!actualSourceId || !actualFetchRecordId) reasons.push('snapshot API 响应缺少 source_id / fetch_record_id。');
  if (expectedSourceId && actualSourceId && expectedSourceId !== actualSourceId) reasons.push(`snapshot.source_id=${actualSourceId} 与期望 ${expectedSourceId} 不一致。`);
  if (expectedFetchRecordId && actualFetchRecordId && expectedFetchRecordId !== actualFetchRecordId) reasons.push(`snapshot.fetch_record_id=${actualFetchRecordId} 与期望 ${expectedFetchRecordId} 不一致。`);
  return {
    valid: reasons.length === 0,
    reasons,
    expectedSourceId,
    expectedFetchRecordId,
    actualSourceId,
    actualFetchRecordId,
  };
}

function sourceSnapshotCapability(source, relatedEvidence = [], state = window.__latestState || {}) {
  const selection = latestSourceFetchSelection(source);
  const fetch = selection.fetch;
  if (!selection.determinate) {
    return {available:false, label:`最新 Fetch 无法唯一确定：${selection.reason}`, fetch:null, fetch_record_id:'', evidence:[]};
  }
  const fetchRecordId = exactFetchRecordId(fetch);
  if (!fetch || !fetchRecordId) {
    return {available:false, label:'当前文章来源没有可唯一定位的 Fetch 快照', fetch:null, fetch_record_id:'', evidence:[]};
  }
  if (!isServerBoundFetch(fetch)) {
    return {available:false, label:'最新 Fetch 未通过服务端绑定核验，来源级快照入口已关闭', fetch, fetch_record_id:fetchRecordId, evidence:[]};
  }
  if (!fetchSnapshotAvailable(fetch)) {
    return {available:false, label:'最新 Fetch 已绑定，但该 attempt 没有保存快照', fetch, fetch_record_id:fetchRecordId, evidence:[]};
  }
  const exactEvidence = asArray(relatedEvidence).filter(item => {
    const trace = exactEvidenceFetchBinding(item, state);
    return trace.status === 'bound' && normalizedId(trace.fetch_record_id) === fetchRecordId;
  });
  return {
    available:true,
    label:'打开最新一次已绑定 Fetch 的保存快照',
    fetch,
    fetch_record_id:fetchRecordId,
    evidence:exactEvidence,
  };
}

function evidenceForExactFetch(fetch, evidence = [], state = window.__latestState || {}) {
  const fetchRecordId = exactFetchRecordId(fetch);
  if (!fetchRecordId) return [];
  return asArray(evidence).filter(item => {
    const trace = exactEvidenceFetchBinding(item, state);
    return trace.status === 'bound' && normalizedId(trace.fetch_record_id) === fetchRecordId;
  });
}

function inspectSource(source,evidence){
  const evidenceSummary=sourceEvidenceSummary(source,evidence);
  const related=evidenceSummary.related;
  const assessment=sourceReadAssessment(source);
  const iteration=finiteValue(source?.iteration);
  const discovered=formatTimestamp(source?.discovered_at);
  const recordedTime=formatTimestamp(source?.fetched_at || source?.discovered_at);
  const timeLabel=assessment.bodyReadRecorded?'读取结果时间':'相关记录时间';
  const provenance=source?.independence_reason||'历史运行未记录来源独立性判定，只能看到域名';
  const bindings=sourceBindingAuditMarkup(source);
  const latestFetch=sourceFetchAttempts(source).at(-1) || {};
  const snapshotCapability=sourceSnapshotCapability(source,related,window.__latestState||{});
  const sourceHash=hashConsistencyModel(
    source?.content_hash || latestFetch.content_hash,
    source?.content_hash_scope || latestFetch.content_hash_scope,
    source?.snapshot_sha256 || latestFetch.snapshot_sha256,
  );
  const sourceHashAudit=`<div class="source-hash-audit ${escapeHTML(sourceHash.status)}"><b>${escapeHTML(sourceHash.label)}</b><p>${escapeHTML(sourceHash.detail)}</p><dl><dt>页面读取记录 ID（技术字段）</dt><dd class="audit-mono">${escapeHTML(source?.fetch_record_id || latestFetch.fetch_record_id || '未记录')}</dd><dt>正文校验值</dt><dd class="audit-mono">${escapeHTML(source?.content_hash || latestFetch.content_hash || '未记录')}</dd><dt>校验值覆盖范围</dt><dd>${escapeHTML(hashScopeName(source?.content_hash_scope || latestFetch.content_hash_scope))}</dd><dt>保存快照校验值（SHA-256）</dt><dd class="audit-mono">${escapeHTML(source?.snapshot_sha256 || latestFetch.snapshot_sha256 || '未记录')}</dd></dl></div>`;
  const snapshotButton=snapshotCapability.available
    ? `<button type="button" class="snapshot-button" data-snapshot-source="${escapeHTML(source?.id||'')}">${escapeHTML(snapshotCapability.label)}</button>`
    : '';
  const snapshotMissing=snapshotCapability.available?'':`<p class="snapshot-missing">${escapeHTML(snapshotCapability.label)}</p>`;
  const sourceUrl=source?.final_url||source?.url||'';
  const provenanceMeta=`<details class="publisher-audit" data-collapse-key="${escapeHTML(collapseKey('publisher',sourceStableKey(source)))}"><summary>查看网页声明的发布方与上游线索</summary><dl><dt>发布方声明</dt><dd>${escapeHTML(source?.publisher_name||'网页未声明')}${source?.publisher_url?` · ${escapeHTML(source.publisher_url)}`:''}</dd><dt>站点名称</dt><dd>${escapeHTML(source?.site_name||'网页未声明')}</dd><dt>作者声明</dt><dd>${asArray(source?.author_names).map(escapeHTML).join('；')||'网页未声明'}</dd><dt>规范网址</dt><dd>${escapeHTML(source?.canonical_url||'网页未声明')}</dd><dt>引用/转载上游</dt><dd>${asArray(source?.upstream_urls).map(escapeHTML).join('<br>')||'网页未声明'}</dd><dt>原始元数据线索</dt><dd>${asArray(source?.provenance_signals).map(escapeHTML).join(' · ')||'无'}</dd></dl><p>以上均为网页自报元数据，只用于保守合并潜在同源内容，不证明发布方身份或来源独立。</p></details>`;
  $('sourceInspector').classList.remove('empty-state');
  $('sourceInspector').innerHTML=`<div><span class="source-order">第 ${iteration===null?'未记录':iteration} 轮发现 · ${escapeHTML(discovered)}</span><h4>${escapeHTML(source?.title||'历史字段未记录')}</h4><p>${escapeHTML(source?.snippet||'未保存页面摘要')}</p><div class="source-read-verdict ${escapeHTML(assessment.mode)}"><b>${escapeHTML(assessment.label)}</b><p>${escapeHTML(assessment.explanation)}</p><small>人工核验：先打开文章读取对应的角色执行记录，再核对操作编号、文章编号和保存快照校验值；只有引用片段、无法对应的历史读取和结果复用，都不能自动说明本次实时阅读过正文。</small></div><div class="source-meta"><span>${escapeHTML(assessment.label)}</span><span>文章编号（技术字段）${escapeHTML(source?.id||source?.source_id||'未记录')}</span><span>${escapeHTML(sourceTypeName(source?.source_type))}</span><span>${escapeHTML(source?.registrable_domain||sourceDomain(source?.final_url||source?.url))}</span><span>抽取 ${related.length} 条 · 纳入回答材料 ${evidenceSummary.admitted.length} 条 · 排除 ${evidenceSummary.excluded.length} 条</span><span>网页响应 ${source?.http_status??'—'} · ${escapeHTML(source?.content_type||'类型未记录')}</span><span>${formatBytes(source?.bytes_read)} · ${escapeHTML(sourceCacheLabel(source))}</span><span>正文解析方式 ${escapeHTML(source?.parser_version||'未记录')}</span></div><div class="provenance-callout ${escapeHTML(source?.independence_status||'unknown')}"><b>能否作为另一组来源：${escapeHTML(independenceStatusName(source?.independence_status))}</b><p>${escapeHTML(provenance)}</p><small>去重来源组（技术 ID）：${escapeHTML(source?.origin_cluster_id||'未记录')}${source?.near_duplicate_of_source_id?` · 近重复于 ${escapeHTML(source.near_duplicate_of_source_id)}（文本相似度 ${displayPercent(source.near_duplicate_similarity)}）`:''}</small></div>${sourceHashAudit}${bindings}${sourceFetchAttemptMarkup(source)}${provenanceMeta}${source?.final_url&&source.final_url!==source.url?`<p class="redirect-note">页面跳转：${escapeHTML(source.url)} → ${escapeHTML(source.final_url)}</p>`:''}<p class="snapshot-time">${timeLabel}：${escapeHTML(recordedTime)}</p><div class="source-actions"><button type="button" data-graph-source>在关系图中查看完整路径</button>${snapshotButton}${sourceUrl?`<a href="${escapeHTML(sourceUrl)}" target="_blank" rel="noreferrer">打开在线地址（不作为本次运行证据） ↗</a>`:''}</div>${snapshotMissing}</div><div class="related-evidence">${related.map(item=>`<button data-evidence="${escapeHTML(item.id)}">${escapeHTML(item.id)} · ${escapeHTML(truncate(item.claim,45))}</button>`).join('')||'<span>该文章尚未抽取出有效证据</span>'}</div>`;
  humanizeVisibleCopy($('sourceInspector'));
  bindCitationLinks($('sourceInspector'));
  $('sourceInspector').querySelector('[data-graph-source]')?.addEventListener('click',()=>focusResearchGraph('source',source));
  $('sourceInspector').querySelector('[data-snapshot-source]')?.addEventListener('click',()=>showSourceSnapshot(source,snapshotCapability.evidence,snapshotCapability.fetch));
  $('sourceInspector').querySelectorAll('[data-source-invocation]').forEach(button=>button.addEventListener('click',()=>{const item=auditInvocationList().find(value=>String(value.invocation_id||'')===button.dataset.sourceInvocation);if(item)showInvocationAudit(item,auditInvocationList(),window.__latestEvents||[],window.__latestAudit||null)}));
}

async function showSourceSnapshot(source, relatedEvidence, fetchAttempt = null) {
  const fetchRecordId = exactFetchRecordId(fetchAttempt);
  if (!fetchRecordId) {
    $('snapshotTitle').textContent = `${source?.title || '来源'} · 快照入口已关闭`;
    $('snapshotMeta').innerHTML = '<span class="snapshot-integrity unverifiable">没有精确的页面读取记录编号，不能读取这篇文章的保存快照</span>';
    $('snapshotText').textContent = '请从具体的页面读取记录，或从与证据精确对应的入口打开快照；页面不会自动选择同一来源的其他读取尝试。';
    $('snapshotDialog').showModal();
    return;
  }
  const query = `?fetch_record_id=${encodeURIComponent(fetchRecordId)}`;
  let snapshot;
  try {
    snapshot = await getJSON(`/api/runs/${encodeURIComponent(runId)}/sources/${encodeURIComponent(source.id)}/snapshot${query}`);
  } catch (error) {
    $('snapshotTitle').textContent = `${source?.title || '来源'} · 快照不可用`;
    $('snapshotMeta').innerHTML = '<span class="snapshot-integrity mismatched">无法读取对应的正文快照</span>';
    $('snapshotText').textContent = String(error?.message || '快照接口暂时不可用');
    $('snapshotDialog').showModal();
    return;
  }
  const identity = snapshotIdentityAssessment(snapshot, source, fetchAttempt);
  if (!identity.valid) {
    $('snapshotTitle').textContent = `${source?.title || '来源'} · 快照身份冲突`;
    $('snapshotMeta').innerHTML = `<span class="snapshot-integrity mismatched">snapshot API 返回了错误的不可变记录，正文已拒绝展示</span><span>期望 source ${escapeHTML(identity.expectedSourceId || '未记录')} · Fetch ${escapeHTML(identity.expectedFetchRecordId || '未记录')}</span><span>实际 source ${escapeHTML(identity.actualSourceId || '未记录')} · Fetch ${escapeHTML(identity.actualFetchRecordId || '未记录')}</span>`;
    $('snapshotText').textContent = identity.reasons.join(' ');
    $('snapshotDialog').showModal();
    return;
  }
  const assessment=sourceReadAssessment(source);
  const expectedHash=String(fetchAttempt?.snapshot_sha256||'').trim();
  const actualHash=String(snapshot?.sha256||'').trim();
  const hashMatches=expectedHash&&actualHash ? actualHash===expectedHash : null;
  const contentHashAudit=hashConsistencyModel(fetchAttempt?.content_hash,fetchAttempt?.content_hash_scope,actualHash);
  const integrityLabel=hashMatches===true?'保存快照 SHA-256 与运行记录一致':hashMatches===false?'保存快照 SHA-256 与运行记录不一致，快照可能被替换':'运行记录没有保存快照 SHA-256，当前不可验证一致性';
  $('snapshotTitle').textContent = `${source?.title || '来源'} · ${sourceSnapshotActionLabel(assessment)}`;
  $('snapshotMeta').innerHTML = `<span class="snapshot-integrity ${hashMatches===true?'matched':hashMatches===false?'mismatched':'unverifiable'}">${integrityLabel}</span><span>保存快照 SHA-256：${escapeHTML(actualHash||'未记录')}</span><span>正文 hash：${escapeHTML(fetchAttempt?.content_hash||'未记录')} · 作用域：${escapeHTML(hashScopeName(fetchAttempt?.content_hash_scope))}</span><span class="snapshot-integrity ${escapeHTML(contentHashAudit.status)}">${escapeHTML(contentHashAudit.label)}：${escapeHTML(contentHashAudit.detail)}</span><span>snapshot API 身份已核对：source_id ${escapeHTML(identity.actualSourceId)} · Fetch ${escapeHTML(identity.actualFetchRecordId)}</span><span>${formatBytes(snapshot?.bytes)} · ${asArray(relatedEvidence).length} 条相关证据</span><span>${escapeHTML(assessment.label)} · ${assessment.mode==='live'?'本次记录为实时 Provider 路径':'不得解释为本次实时正文读取'}</span>`;
  let rendered = escapeHTML(snapshot?.text ?? '快照正文未记录');
  [...asArray(relatedEvidence)].sort((a,b)=>String(b?.quote||'').length-String(a?.quote||'').length).forEach(item=>{
    const quote=escapeHTML(item?.quote||'');
    if(quote) rendered=rendered.split(quote).join(`<mark id="snapshot-${escapeHTML(item.id)}" title="${escapeHTML(item.id)}">${quote}</mark>`);
  });
  $('snapshotText').innerHTML = rendered;
  $('snapshotDialog').showModal();
}

function inspectGraphItem(type,item,state){
  $('sourceInspector').classList.remove('empty-state');
  if(type==='query'){
    const allSources=asArray(normalizeSources(state));
    // Reuse the graph's normalized ID/text association so the inspector
    // cannot hide sources that differ only by whitespace/case or use query_id.
    const sources=allSources.filter(source=>sourceQueryIndices(source,[item]).includes(0));
    $('sourceInspector').innerHTML=`<div><span class="source-order">完整检索路线</span><h4>${escapeHTML(item?.text||'历史字段未记录')}</h4><p>为什么搜索：采用“${escapeHTML(methodName(item?.strategy)||'历史策略未记录')}”来回答目标 ${escapeHTML(item?.subgoal_id||'历史字段未记录')}。</p><div class="source-meta"><span>发现 ${sources.length} 篇文章</span><span>点击右侧文章继续追踪</span></div></div><div class="related-evidence">${sources.map(source=>`<button data-source-url="${escapeHTML(source?.url||'')}">${escapeHTML(source?.title||'历史字段未记录')}</button>`).join('')||'<span>该查询尚未返回文章</span>'}</div>`;
    $('sourceInspector').querySelectorAll('[data-source-url]').forEach(button=>button.addEventListener('click',()=>{const source=allSources.find(value=>value?.url===button.dataset.sourceUrl);if(source)inspectSource(source,asArray(state?.evidence))}));
    return;
  }
  if(type==='fetch'){
    const source=item?.source || normalizeSources(state).find(value=>String(value?.id||'')===String(item?.source_id||''));
    const attempts=asArray(item?.attempts).length ? item.attempts : sourceFetchAttempts(source);
    const selected=item?.attempt || attempts.find(attempt=>exactFetchRecordId(attempt)===exactFetchRecordId(item)) || attempts.at(-1) || {};
    const fetchHash=hashConsistencyModel(item?.content_hash||selected.content_hash||source?.content_hash,item?.content_hash_scope||selected.content_hash_scope||source?.content_hash_scope,item?.snapshot_sha256||selected.snapshot_sha256||source?.snapshot_sha256);
    const exactId=item?.fetch_record_id||selected.fetch_record_id||'';
    const selectedBinding=isServerBoundFetch(item?.attempt||selected||item);
    const selectedSnapshotAvailable=selectedBinding && fetchSnapshotAvailable(selected);
    $('sourceInspector').innerHTML=`<div><span class="source-order">显式 Fetch 读取节点 · 独立 attempt</span><h4>${escapeHTML(source?.title||'文章标题未记录')}</h4><p>该节点只代表这一条 immutable Fetch 记录；Evidence 只有持有相同 fetch_record_id 才会从此处连入，不能用文章或“最新尝试”代替。</p><div class="source-meta"><span>fetch_record_id ${escapeHTML(exactId||'未记录')}</span><span>source_id ${escapeHTML(item?.source_id||source?.id||'未记录')}</span><span>${escapeHTML(fetchAttemptLabel(selected))}</span><span>${escapeHTML(fetchAttemptStatusName(item?.status||selected.status))}</span><span>${escapeHTML(bindingStatusName(selected.binding_status||item?.binding_status))}</span><span>${escapeHTML(item?.order_label||'顺序未记录')}</span></div><div class="source-hash-audit ${escapeHTML(fetchHash.status)}"><b>${escapeHTML(fetchHash.label)}</b><p>${escapeHTML(fetchHash.detail)}</p><dl><dt>正文 content hash</dt><dd class="audit-mono">${escapeHTML(item?.content_hash||selected.content_hash||source?.content_hash||'未记录')}</dd><dt>hash 作用域</dt><dd>${escapeHTML(hashScopeName(item?.content_hash_scope||selected.content_hash_scope||source?.content_hash_scope))}</dd><dt>保存快照 SHA-256</dt><dd class="audit-mono">${escapeHTML(item?.snapshot_sha256||selected.snapshot_sha256||source?.snapshot_sha256||'未记录')}</dd></dl></div><dl class="fetch-record-facts"><dt>Fetch invocation</dt><dd>${escapeHTML(selected.invocation_id||item?.invocation_id||'未记录')}</dd><dt>Result invocation</dt><dd>${escapeHTML(selected.result_invocation_id||'未记录')}</dd><dt>Operation key</dt><dd class="audit-mono">${escapeHTML(selected.operation_key||item?.operation_key||'未记录')}</dd><dt>Provider / execution</dt><dd>${escapeHTML(selected.provider||item?.provider||'Provider 未记录')} / ${escapeHTML(executionModeName(selected.execution_mode||item?.execution_mode))}</dd><dt>Fetch mode</dt><dd>${escapeHTML(fetchModeName(selected.fetch_mode||item?.fetch_mode))}</dd><dt>Binding status</dt><dd>${escapeHTML(bindingStatusName(selected.binding_status||item?.binding_status))} · ${selected.binding_valid===true?'binding_valid=true':'binding_valid 未通过'}</dd><dt>状态 / 时间</dt><dd>${escapeHTML(fetchAttemptStatusName(selected.status||item?.status))} · ${escapeHTML(formatTimestamp(selected.recorded_at||selected.fetched_at))}</dd></dl><p class="inspector-caveat">${escapeHTML(item?.order_detail||'读取完成顺序没有记录时，不能从文章编号推断并发 fetch 的先后。')}</p>${sourceFetchAttemptMarkup(source || item)}</div><div class="related-evidence"><button type="button" data-fetch-source>打开文章来源卡</button>${selectedSnapshotAvailable?'<button type="button" class="snapshot-button" data-fetch-snapshot>打开与本次 Fetch 精确绑定的快照</button>':'<span class="snapshot-missing">该 Fetch 没有可验证的保存快照</span>'}${selected.invocation_id?`<button type="button" data-fetch-selected-invocation="${escapeHTML(selected.invocation_id)}">打开本次 invocation</button>`:''}${attempts.map(attempt=>normalizedId(attempt?.invocation_id)&&attempt.invocation_id!==selected.invocation_id?`<button type="button" data-fetch-invocation="${escapeHTML(attempt.invocation_id)}">打开 invocation ${escapeHTML(attempt.invocation_id)}</button>`:'').join('')}</div>`;
    humanizeVisibleCopy($('sourceInspector'));
    $('sourceInspector').querySelector('[data-fetch-source]')?.addEventListener('click',()=>source&&inspectSource(source,asArray(state?.evidence)));
    $('sourceInspector').querySelector('[data-fetch-snapshot]')?.addEventListener('click',()=>source&&showSourceSnapshot(source,evidenceForExactFetch(selected,asArray(state?.evidence),state),selected));
    $('sourceInspector').querySelectorAll('[data-source-invocation],[data-fetch-invocation],[data-fetch-selected-invocation]').forEach(button=>button.addEventListener('click',()=>{const invocation=auditInvocationList().find(value=>String(value.invocation_id||'')===String(button.dataset.sourceInvocation||button.dataset.fetchInvocation||button.dataset.fetchSelectedInvocation));if(invocation)showInvocationAudit(invocation,auditInvocationList(),window.__latestEvents||[],window.__latestAudit||null)}));
    return;
  }
  if(type==='evidence'){
    const trace=exactEvidenceFetchBinding(item,state);
    const source=trace.source || null;
    const snapshotCapability=exactSnapshotCapability(trace);
    const evidenceHash=hashConsistencyModel(item?.content_hash||trace.fetch?.content_hash,item?.content_hash_scope||trace.fetch?.content_hash_scope,item?.snapshot_sha256||trace.fetch?.snapshot_sha256);
    const fetchGraphItem=trace.status==='bound' && trace.fetch ? trace.fetch : null;
    $('sourceInspector').innerHTML=`<div><span class="source-order">完整证据内容</span><h4>${escapeHTML(item?.claim||'历史字段未记录')}</h4><p>原文：“${escapeHTML(item?.quote||'历史字段未记录')}”</p><div class="source-meta"><span>${escapeHTML(stanceName(item?.stance))}</span><span>${escapeHTML(humanReliability(item?.reliability))}（类型先验）</span><span>原文逐字定位 ${displayPercent(item?.extraction_confidence)}</span><span>source_id ${escapeHTML(item?.source_id||source?.id||'未记录')}</span><span>${escapeHTML(trace.label)}</span></div><div class="evidence-provenance-audit ${escapeHTML(trace.status)}"><b>${escapeHTML(trace.label)}</b><p>${escapeHTML(trace.detail)}</p><dl><dt>Evidence.fetch_record_id</dt><dd class="audit-mono">${escapeHTML(item?.fetch_record_id||'未记录')}</dd><dt>Fetch invocation</dt><dd>${escapeHTML(trace.fetch?.invocation_id||'未记录')}</dd><dt>Operation key</dt><dd class="audit-mono">${escapeHTML(trace.fetch?.operation_key||'未记录')}</dd><dt>Binding status</dt><dd>${escapeHTML(bindingStatusName(trace.fetch?.binding_status||item?.fetch_binding_status))} · ${trace.fetch?.binding_valid===true?'binding_valid=true':'binding_valid 未通过'}</dd></dl></div><div class="source-hash-audit ${escapeHTML(evidenceHash.status)}"><b>${escapeHTML(evidenceHash.label)}</b><p>${escapeHTML(evidenceHash.detail)}</p><dl><dt>正文 content hash</dt><dd class="audit-mono">${escapeHTML(item?.content_hash||trace.fetch?.content_hash||'未记录')}</dd><dt>hash 作用域</dt><dd>${escapeHTML(hashScopeName(item?.content_hash_scope||trace.fetch?.content_hash_scope))}</dd><dt>保存快照 SHA-256</dt><dd class="audit-mono">${escapeHTML(item?.snapshot_sha256||trace.fetch?.snapshot_sha256||'未记录')}</dd></dl></div><p class="inspector-caveat">这些分值用于流程检查，不表示该声明为真的概率。请沿 source_id → 精确 fetch_record_id → Fetch invocation → snapshot 核查原文来源链；缺少精确 ID 时不会自动选择最新读取。</p>${source?`<button type="button" class="audit-link-button" data-evidence-source>打开唯一来源卡与 fetch 审计</button>${snapshotCapability.available?'<button type="button" class="snapshot-button" data-evidence-snapshot>打开与证据精确绑定的来源 snapshot</button>':`<span class="snapshot-missing">${escapeHTML(snapshotCapability.label)}</span>`}`:''}${fetchGraphItem?`<button type="button" class="audit-link-button" data-evidence-fetch>在关系图中定位精确 Fetch</button>`:''}${!source?'<span>没有唯一 source_id 或来源 URL 对应记录，当前不可完整回链</span>':''}</div><div class="related-evidence"><span>证据 ID：${escapeHTML(item?.id||'历史字段未记录')}</span><span>回答目标：${escapeHTML(item?.slot_id||'历史字段未记录')}</span><span>来源域名：${escapeHTML(sourceDomain(item?.source_url))}</span><a href="${escapeHTML(item?.source_url||'#')}" target="_blank" rel="noreferrer">查看来源文章 ↗</a></div>`;
    humanizeVisibleCopy($('sourceInspector'));
    const inspectorSnapshot = $('sourceInspector').querySelector('[data-evidence-snapshot]');
    if (inspectorSnapshot && !(trace.status === 'bound' && trace.fetch)) {
      const note = document.createElement('span');
      note.className = 'snapshot-missing';
      note.textContent = '精确 Fetch 未通过绑定核验，快照入口已关闭';
      inspectorSnapshot.replaceWith(note);
    }
    $('sourceInspector').querySelector('[data-evidence-source]')?.addEventListener('click',()=>inspectSource(source,asArray(state?.evidence)));
    $('sourceInspector').querySelector('[data-evidence-snapshot]')?.addEventListener('click',()=>source&&snapshotCapability.available&&showSourceSnapshot(source,[item],snapshotCapability.fetch));
    $('sourceInspector').querySelector('[data-evidence-fetch]')?.addEventListener('click',()=>focusResearchGraph('fetch',fetchGraphItem));
    return;
  }
  if(type==='target'){
    const supporting=asArray(item?.supporting_evidence);
    $('sourceInspector').innerHTML=`<div><span class="source-order">完整回答目标</span><h4>${escapeHTML(item?.description||'历史字段未记录')}</h4><p>${escapeHTML(item?.value||'尚未形成结论')}</p><div class="source-meta"><span>${item?.required===false?'可选目标，不计入必答目标完成度':'必需目标'}</span><span>流程充分度 ${displayPercent(item?.confidence)}</span><span>${supporting.length} 条支持证据</span></div><p class="inspector-caveat">流程充分度只用于排序补证优先级，是否通过仍由来源、原文定位、反面材料和冲突处理共同决定。</p></div><div class="related-evidence">${supporting.map(id=>evidenceReferenceMarkup(id,id)).join('')||'<span>尚无支持证据</span>'}</div>`;
    humanizeVisibleCopy($('sourceInspector'));
    bindCitationLinks($('sourceInspector'));
  }
}

function renderSourceJourney(sources, queries = []) {
  sources=asArray(sources);
  if (!sources.length) {$('sourceJourney').classList.add('empty-state');$('sourceJourney').innerHTML='检索开始后展示文章发现与阅读顺序';return}
  $('sourceJourney').classList.remove('empty-state');
  const evidence = asArray(window.__latestState?.evidence);
  const groups = new Map();
  sources.forEach((source, index) => {
    const iteration = finiteValue(source?.iteration);
    if (!groups.has(iteration)) groups.set(iteration, []);
    groups.get(iteration).push({source, index});
  });
  $('sourceJourney').innerHTML = [...groups.entries()].map(([iteration, items]) => `<section class="journey-round"><header><span>ROUND ${iteration===null?'??':String(iteration).padStart(2,'0')}</span><strong>第 ${iteration===null?'未记录':iteration} 轮调研</strong><small>${items.length} 篇候选文章 · ${items.filter(item => sourceReadAssessment(item.source).boundFetched).length} 条有绑定读取结果 · 并发读取不推断完成先后</small></header><div>${items.map(({source, index}) => {
    const assessment = sourceReadAssessment(source);
    const evidenceSummary = sourceEvidenceSummary(source, evidence);
    const related = evidenceSummary.related;
    const queryLabels = sourceQueryLabels(source, queries);
    const queryMarkup = queryLabels.length
      ? queryLabels.map(item => `<span class="journey-query-chip">Q${item.index === null ? '??' : String(item.index + 1).padStart(2,'0')} · ${escapeHTML(truncate(item.text, 70))}</span>`).join('')
      : '<span class="journey-query-missing">发现该文章的检索词未记录</span>';
    const cluster = source.origin_cluster_id || source.registrable_domain || sourceDomain(source.final_url || source.url) || '未记录';
    const independence = source.independence_reason || '当前按域名近似来源簇，不能证明编辑独立性';
    const independenceLabel=independenceStatusName(source.independence_status);
    const actionLabel=sourceSnapshotActionLabel(assessment).replace('并高亮证据','').trim();
    return `<article class="journey-item ${escapeHTML(assessment.mode)}" data-source-key="${escapeHTML(collapseKey('source',sourceStableKey(source,index)))}"><button type="button" class="journey-main" data-journey="${index}"><span class="journey-index">${String(index + 1).padStart(2,'0')}</span><div class="journey-body"><span class="journey-state">${escapeHTML(assessment.label)}</span><strong>${escapeHTML(source.title||'历史字段未记录')}</strong><p class="journey-query-list"><span>关联检索路线</span>${queryMarkup}</p><div class="journey-facts"><span><b>发现序号 ${String(index + 1).padStart(2,'0')}</b>（不是 fetch 完成顺序）</span><span><b>抽取 ${related.length} · 纳入闭包 ${evidenceSummary.admitted.length}</b> 条证据</span><span><b>${escapeHTML(sourceTypeName(source.source_type))}</b> 来源性质</span><span><b>${escapeHTML(independenceLabel)}</b> 来源计数</span></div><small>${escapeHTML(sourceDomain(source.final_url || source.url))} · ${escapeHTML(formatTimestamp(source.fetched_at || source.discovered_at))} · ${escapeHTML(sourceCacheLabel(source))}</small><i>查看文章证据链、fetch 尝试与${escapeHTML(actionLabel)} →</i></div></button><details class="journey-technical" data-collapse-key="${escapeHTML(collapseKey('journey-technical',sourceStableKey(source,index)))}"><summary><span>抓取、绑定与去重审计</span><b>${escapeHTML(fetchModeName(assessment.mode))} · ${sourceFetchAttempts(source).length || '未记录'} 条 durable 记录 · ${source.http_status ? `HTTP ${source.http_status}` : '未记录 HTTP'} · ${formatBytes(source.bytes_read)}</b></summary><dl><dt>读取判定</dt><dd>${escapeHTML(assessment.explanation)}</dd><dt>正文解析</dt><dd>${escapeHTML(source.parser_version || '未记录')}</dd><dt>去重来源组</dt><dd>${escapeHTML(cluster)}</dd><dt>来源计数依据</dt><dd>${escapeHTML(independence)}</dd><dt>发现检索词</dt><dd>${escapeHTML(queryLabels.map(item => item.text).join('；') || '未记录')}</dd><dt>Fetch invocation</dt><dd>${escapeHTML(source.fetch_invocation_id || '未记录')} · result ${escapeHTML(source.fetch_result_invocation_id || '未记录')}</dd><dt>Operation / provider</dt><dd>${escapeHTML(source.fetch_operation_key || '未记录')} · ${escapeHTML(source.fetch_provider || 'Provider 未记录')}</dd><dt>Binding status</dt><dd>${escapeHTML(bindingStatusName(source.fetch_binding_status))} · 显式 integrity ${source.fetch_binding_valid === true ? '通过' : '未核验'}</dd></dl>${sourceFetchAttemptMarkup(source)}</details></article>`;
  }).join('')}</div></section>`).join('');
  document.querySelectorAll('[data-journey]').forEach(button => button.addEventListener('click', () => {
    inspectSource(sources[Number(button.dataset.journey)], evidence);
    $('sourceInspector').scrollIntoView(scrollOptions('center'));
  }));
  humanizeVisibleCopy($('sourceJourney'));
}

function renderEvidence(evidence){
  evidence=asArray(evidence);
  $('evidenceCount').textContent=evidence.length;
  if(!evidence.length){$('evidence').classList.add('empty-state');$('evidence').innerHTML='尚未收集证据';return}
  $('evidence').classList.remove('empty-state');
  $('evidence').innerHTML=evidence.map((item,index)=>{
    const state=window.__latestState||{};
    const trace=exactEvidenceFetchBinding(item,state);
    const source=trace.source || null;
    const relevanceThreshold=relevanceAdmissionThreshold(state);
    const thresholdLabel=relevanceThreshold===null?'本次运行准入阈值未记录，当前不可验证':`本运行准入线 ${Math.round(relevanceThreshold*100)} / 100`;
    const role=evidenceEffectiveRole(item,state);
    const excluded=role.kind==='excluded';
    const statusLabel=excluded?'不计入结论':stanceName(role.kind);
    const snapshotCapability=exactSnapshotCapability(trace);
    const snapshotMarkup=source
      ? snapshotCapability.available
        ? `<button type="button" class="snapshot-button" data-snapshot-evidence="${escapeHTML(item.id)}">打开与证据精确绑定的快照</button>`
        : `<span class="snapshot-missing">${escapeHTML(snapshotCapability.label)}</span>`
      : '<span>来源卡不可唯一对应，快照入口已关闭</span>';
    const evidenceHash=hashConsistencyModel(item?.content_hash||trace.fetch?.content_hash,item?.content_hash_scope||trace.fetch?.content_hash_scope,item?.snapshot_sha256||trace.fetch?.snapshot_sha256);
    return `<details class="evidence-card ${excluded?'excluded':escapeHTML(role.kind)}" id="${escapeHTML(item.id)}" data-collapse-key="${escapeHTML(collapseKey('evidence',item.id,index))}" tabindex="-1" ${index<2?'open':''}><summary><div class="evidence-head"><span class="evidence-id">证据 ${escapeHTML(item.id)}</span><span class="human-score">${excluded?'已排除':humanReliability(item.reliability)}</span></div><blockquote>“${escapeHTML(truncate(item.quote,180))}”</blockquote><div class="evidence-summary-meta"><span class="stance-label">${statusLabel}</span><span>${escapeHTML(sourceDomain(item.source_url))}</span><span class="evidence-binding-pill ${escapeHTML(trace.status)}">${escapeHTML(trace.label)}</span><b>展开核对原始抽取与${excluded?'排除依据':'是否纳入结论'}</b></div></summary><div class="evidence-detail">${excluded?`<div class="evidence-exclusion"><b>该条不会用于最终回答、来源数量或完成度</b><span>${escapeHTML(role.reason)}</span></div>`:''}<h4>抽取器生成的候选声明</h4><p>${escapeHTML(item.claim||'历史字段未记录')}</p><h4>可逐字定位的原文</h4><blockquote>“${escapeHTML(item.quote||'历史字段未记录')}”</blockquote><dl><dt>原始抽取立场</dt><dd>${escapeHTML(stanceName(item.stance))}</dd><dt>最终材料用途</dt><dd>${excluded?`已排除：${escapeHTML(role.reason)}`:escapeHTML(stanceName(role.kind))}</dd><dt>回答目标</dt><dd>${escapeHTML(item.slot_id||'历史字段未记录')}</dd><dt>Source ID</dt><dd>${escapeHTML(item.source_id || source?.id || '历史字段未记录')}</dd><dt>精确 Fetch 回链</dt><dd>${escapeHTML(trace.label)} · ${escapeHTML(item.fetch_record_id||'fetch_record_id 未记录')}</dd><dt>Fetch invocation / operation</dt><dd>${escapeHTML(trace.fetch?.invocation_id||'未记录')} · ${escapeHTML(trace.fetch?.operation_key||'未记录')}</dd><dt>Binding status</dt><dd>${escapeHTML(bindingStatusName(item.fetch_binding_status||trace.fetch?.binding_status))} · ${item.fetch_binding_valid===true||trace.fetch?.binding_valid===true?'binding_valid=true':'binding_valid 未通过'}</dd><dt>来源等级</dt><dd>${displayPercent(item.reliability)}（策略先验，不是真实概率）</dd><dt>原文定位</dt><dd>${displayPercent(item.extraction_confidence)}（quote 在快照中逐字存在）</dd><dt>Claim-quote 一致性</dt><dd>${displayPercent(item.claim_quote_consistency)}（规则检查，不是语义概率）${asArray(item.claim_quote_check_reasons).length?`<br>${asArray(item.claim_quote_check_reasons).map(escapeHTML).join('；')}`:''}</dd><dt>目标相关性</dt><dd>${displayPercent(item.slot_relevance_score)}（${escapeHTML(thresholdLabel)}）${asArray(item.slot_relevance_reasons).length?`<br>${asArray(item.slot_relevance_reasons).map(escapeHTML).join('；')}`:''}</dd><dt>Origin 来源簇</dt><dd>${escapeHTML(item.origin_cluster_id||item.source_cluster_id||'历史字段未记录')}</dd><dt>独立性依据</dt><dd>${escapeHTML(item.independence_basis||'历史运行未记录')}</dd><dt>正文 content hash</dt><dd class="audit-mono">${escapeHTML(item.content_hash||trace.fetch?.content_hash||'历史字段未记录')}</dd><dt>Hash 作用域</dt><dd>${escapeHTML(hashScopeName(item.content_hash_scope||trace.fetch?.content_hash_scope))}</dd><dt>保存快照 SHA-256</dt><dd class="audit-mono">${escapeHTML(item.snapshot_sha256||trace.fetch?.snapshot_sha256||'历史字段未记录')}</dd><dt>Hash 一致性</dt><dd>${escapeHTML(evidenceHash.label)} · ${escapeHTML(evidenceHash.detail)}</dd></dl><div class="evidence-actions"><button type="button" data-graph-evidence="${escapeHTML(item.id)}">在关系图中查看上下游路径</button>${source?`<button type="button" data-source-evidence="${escapeHTML(item.id)}">打开来源卡与页面读取审计</button>`:''}${snapshotMarkup}<a href="${escapeHTML(item.source_url)}" target="_blank" rel="noreferrer">打开 ${escapeHTML(item.source_title||'来源页面')} ↗</a></div></div></details>`;
  }).join('');
  document.querySelectorAll('[data-graph-evidence]').forEach(button=>button.addEventListener('click',()=>{const item=evidence.find(value=>value.id===button.dataset.graphEvidence);if(item)focusResearchGraph('evidence',item)}));
  document.querySelectorAll('[data-source-evidence]').forEach(button=>button.addEventListener('click',()=>{
    const item=evidence.find(value=>String(value?.id||'')===button.dataset.sourceEvidence);
    const source=item ? exactEvidenceFetchBinding(item,window.__latestState||{}).source : null;
    if(source) inspectSource(source,evidence);
  }));
  document.querySelectorAll('[data-snapshot-evidence]').forEach(button=>button.addEventListener('click',()=>{
    const item=evidence.find(value=>String(value?.id||'')===button.dataset.snapshotEvidence);
    const trace = item ? exactEvidenceFetchBinding(item, window.__latestState || {}) : null;
    const source=trace?.source || null;
    const capability = exactSnapshotCapability(trace);
    if(source && capability.available) {
      showSourceSnapshot(source, item ? [item] : [], capability.fetch);
    }
  }));
  document.querySelectorAll('[data-snapshot-evidence]').forEach(button=>{
    const item=evidence.find(value=>String(value?.id||'')===button.dataset.snapshotEvidence);
    const trace=item ? exactEvidenceFetchBinding(item, window.__latestState || {}) : null;
    if(exactSnapshotCapability(trace).available) return;
    const note=document.createElement('span');
    note.className='snapshot-missing';
    note.textContent='精确 Fetch 未通过绑定核验，快照入口已关闭';
    button.replaceWith(note);
  });
  humanizeVisibleCopy($('evidence'));
}

function renderAnswer(answer,verification,runStatus=null,deliveryRaw=null){
  const section=$('answerSection');
  const disclosure=$('answerDisclosure');
  const recoveryBlocked=runStatus==='recovery_unverified';
  section.classList.toggle('hidden',!answer||recoveryBlocked);
  disclosure?.classList.toggle('hidden',!answer||recoveryBlocked);
  if(!answer||recoveryBlocked){
    $('answer').innerHTML=recoveryBlocked?'<div class="recovery-answer-block" role="alert"><strong>回答正文暂不展示</strong><p>恢复记录无法核对。请先查看恢复确认、状态变化、执行器记录和恢复权限记录。</p></div>':'';
    $('verificationContractSummary').innerHTML='';
    $('verification').innerHTML='';
    return;
  }
  const normalized=String(answer).trim();
  const shortAnswer=normalized.length<=260&&!/\n\s*\n|\n/.test(normalized);
  const report=verification&&typeof verification==='object'?verification:null;
  const delivery=asObject(deliveryRaw);
  const interruptedDelivery=delivery.mode==='interrupted_evidence_limited';
  const limitedDelivery=delivery.mode==='evidence_limited'||interruptedDelivery;
  const localCitationBinding=delivery.mode==='local_citation_binding';
  const items=asArray(report?.items);
  const evidence=asArray(window.__latestState?.evidence);
  const hasUnknownCitation=citationMarkers(answer,evidence).some(marker=>!marker.item);
  section.dataset.answerMode=shortAnswer?'verification-only':'full';
  section.dataset.deliveryMode=String(delivery.mode||'candidate');
  $('answerPanelTitle').textContent=interruptedDelivery?'运行中断后的当前回答与可查材料':limitedDelivery?'当前可交付回答与核验进度':localCitationBinding?'最终回答与可查引用':shortAnswer?'逐句引用对应检查':'最终回答与逐句引用对应检查';
  $('answerBridge').classList.toggle('hidden',!shortAnswer);
  const recordedPassed=recordedBoolean(report,'passed');
  $('answerGateLabel').textContent=interruptedDelivery?'运行中断后的当前回答 · 已保留附件观察和恢复入口':limitedDelivery?'当前回答已生成 · 正在补齐来源、原文定位和反面材料检查':localCitationBinding?'引用编号已对应保存材料；自动逐句语义核对未返回结果':hasUnknownCitation?'未知引用 · 当前不可核查':recordedPassed===true?citationContractPassLabel:recordedPassed===false?'待核对回答 · 存在需要补材料的句子':'待核对回答 · 最终判断未记录';
  $('answer').classList.remove('empty-state');
  const limitedNotice=limitedDelivery?`<div class="evidence-limited-answer" role="note"><strong>${interruptedDelivery?'运行中断后的当前回答':'当前可交付回答'}</strong><p>${interruptedDelivery?'外部调用在完成材料整理前中断。以下内容只使用已保存的附件观察和证据；可以继续研究补齐材料，系统不会把这次中断当作空白答复。':'系统已先交付当前材料能支持的完整回答，并继续保留补齐来源互证、原文定位、反面材料和逐句引用检查的入口。它还不能标记为“全部核验通过”。'}</p></div>`:'';
  $('answer').innerHTML=limitedNotice+formatCitations(answer,evidence);
  bindCitationLinks($('answer'));
  const providerPassed=recordedBoolean(report,'provider_passed');
  const recordedSystemPassed=recordedBoolean(report,'passed');
  const systemPassed=hasUnknownCitation?null:recordedSystemPassed;
  const systemLabel=hasUnknownCitation?'当前证据集中存在未知引用，不能复核已保存判断':localCitationBinding?'本地引用绑定检查完成（非语义核验）':systemPassed===null?'历史字段未记录 · 不可验证':systemPassed?'逐句引用对应检查通过':'暂不交付';
  const providerLabel=localCitationBinding?'服务超时，未返回判断':providerPassed===null?'历史未记录':providerPassed?'声称通过':'判断失败';
  $('verificationContractSummary').innerHTML=report?`<div><span>模型原始判断</span><strong class="${providerPassed===true?'passed':providerPassed===false?'blocked':'unverifiable'}">${providerLabel}</strong></div><i>→ 系统重新检查 →</i><div><span>${localCitationBinding?'本地绑定检查':'最终系统判断'}</span><strong class="${localCitationBinding?'unverifiable':systemPassed===true?'passed':systemPassed===false?'blocked':'unverifiable'}">${escapeHTML(systemLabel)}</strong></div><div><span>句子覆盖</span><strong>${localCitationBinding?`${escapeHTML(displayNumber(report.expected_item_count,'—'))} / ${escapeHTML(displayNumber(report.expected_item_count,'—'))}`:`${escapeHTML(displayNumber(report.provider_item_count,'—'))} / ${escapeHTML(displayNumber(report.expected_item_count,'—'))}`}</strong><small>${localCitationBinding?'本地编号对应 / 可解析句子':'模型返回 / 系统期望'}</small></div><div><span>检查版本</span><strong>${escapeHTML(report.contract_version||'历史未记录')}</strong></div>`:limitedDelivery?'<div><span>交付等级</span><strong class="blocked">当前回答已生成</strong></div><i>→</i><div><span>逐句引用核验</span><strong class="unverifiable">仍待补齐</strong></div><div><span>可查证据编号</span><strong>见回答正文</strong><small>只使用已保存材料</small></div>':'';
  $('verification').innerHTML=items.map((item,index)=>{
    const citationMatch=recordedBoolean(item,'citation_set_match');
    const citedIds=recordedArray(item,'evidence_ids');
    const citations=citedIds===null?'<span class="citation-unknown">历史字段未记录<small>不可验证</small></span>':citedIds.map(id=>evidenceReferenceMarkup(id,id)).join('')||'无有效引用';
    const matchText=citationMatch===null?'历史字段未记录，引用集合不可验证':citationMatch?citationContractPassLabel:'系统发现引用缺失、额外添加或集合不一致';
    return `<div class="verify-item ${escapeHTML(item?.status||'unverifiable')}"><span>${String(index+1).padStart(2,'0')}</span><div><strong>${item?.status==='entailed'?'按当前快照判为支持':item?.status?'需要补证':'历史状态未记录'}</strong><p>${escapeHTML(item?.claim||'历史字段未记录')}</p><small>${escapeHTML(item?.reason||'历史字段未记录')}</small><em class="citation-match ${citationMatch===true?'matched':citationMatch===false?'mismatched':'unverifiable'}">${escapeHTML(matchText)}</em></div><b>${citations}</b></div>`;
  }).join('');
  bindCitationLinks($('verification'));
}

function citationMarkers(answer,evidence){
  const text=String(answer||'');
  const byId=new Map(asArray(evidence).filter(item=>item?.id).map(item=>[String(item.id),item]));
  const markers=[];
  const pattern=/\[([^\]\r\n]+)\]/g;
  for(const match of text.matchAll(pattern)){
    const id=match[1];
    const item=byId.get(id);
    if(!item&&!/^E[^\s\[\]]+$/.test(id))continue;
    markers.push({id,item:item||null,index:match.index||0,length:match[0].length,raw:match[0]});
  }
  return markers;
}

function renderCitationText(answer,evidence,renderer,textRenderer=escapeHTML){
  const text=String(answer||'');
  const markers=citationMarkers(text,evidence);
  if(!markers.length)return textRenderer(text);
  let cursor=0;
  let rendered='';
  markers.forEach(marker=>{
    rendered+=textRenderer(text.slice(cursor,marker.index));
    rendered+=renderer(marker);
    cursor=marker.index+marker.length;
  });
  return rendered+textRenderer(text.slice(cursor));
}

function answerSentences(answer){
  return String(answer||'').replace(/\r/g,'').split(/\n+/).flatMap(line=>line.match(/[^。！？!?]+(?:[。！？!?]+|$)/g)||[]).map(item=>item.trim()).filter(Boolean);
}

function buildAnswerSummary(answer,evidence,question=''){
  const text=extractDirectAnswerSection(answer).trim() || stripAnswerChrome(answer).trim();
  const sentences=answerSentences(text);
  const asksForLatest=/(最新|近期|当前|进展|latest|recent|current|state.of.the.art)/i.test(String(question||''));
  if(asksForLatest){
    const progressStart=sentences.findIndex(sentence=>/(总体来看|最新进展|近年第|近年主要|鲁棒性评测|面向真实落地|开放挑战|未来方向|合成到真实)/.test(sentence));
    if(progressStart>=0){
      const progressSentences=sentences.slice(progressStart,Math.min(sentences.length,progressStart+4));
      return{text:truncate(progressSentences.join(''),840),label:'针对最新进展的回答摘要 · 先看技术变化'};
    }
  }
  const citedIndex=sentences.findIndex(sentence=>citationMarkers(sentence,evidence).some(marker=>Boolean(marker.item)));
  if(citedIndex>=0){
    const summary=sentences.slice(Math.max(0,citedIndex-1),Math.min(sentences.length,citedIndex+3)).join('');
    return{text:truncate(summary,720),label:'当前回答摘要 · 可核查引用'};
  }
  const unknownCitation=sentences.find(sentence=>citationMarkers(sentence,evidence).length>0);
  if(unknownCitation)return{text:truncate(unknownCitation,520),label:'当前回答摘要 · 引用 ID 尚不可核查'};
  const excerpt=sentences.slice(0,Math.min(3,sentences.length)).join('')||text;
  return{text:truncate(excerpt,720),label:'当前回答摘要 · 未发现可核查 Evidence ID'};
}

function extractDirectAnswerSection(answer){
  const lines=String(answer||'').replace(/\r/g,'').split('\n');
  const start=lines.findIndex(line=>/^#{1,3}\s*对问题的直接回答\s*#*\s*$/.test(line.trim()));
  if(start<0)return'';
  const collected=[];
  for(const line of lines.slice(start+1)){
    if(/^#{1,3}\s+/.test(line.trim()))break;
    collected.push(line);
  }
  return stripAnswerChrome(collected.join('\n'));
}

function stripAnswerChrome(answer){
  return String(answer||'')
    .replace(/^\s*#{1,3}\s+.+$/gm,'')
    .replace(/^\s*以下先.+$/gm,'')
    .replace(/^\s*本轮没有完成全部材料整理.+$/gm,'')
    .replace(/^\s*回答只使用本轮已保存.+$/gm,'')
    .replace(/\n{3,}/g,'\n\n')
    .trim();
}

function deliveryCompletionExplanation(state, invocations = [], events = []) {
  const runtimeByAgent = agentOrder.map(agent => agentRuntimeEvidence(agent, invocations, events));
  const done = runtimeByAgent.filter(item => item.status === 'done').length;
  const missing = runtimeByAgent.filter(item => item.status === 'waiting').map(item => agentContracts[item.agent]?.name || item.agent);
  const delivery = asObject(state?.answer_delivery);
  if (state?.status === 'completed') return done === agentOrder.length ? '完整链路已经走到写作和引用核验' : `已完成最终归档；${missing.length ? `仍有 ${missing.join('、')} 没有可核对的执行记录` : '部分角色只有历史事件记录'}`;
  if (state?.status === 'evidence_incomplete') return '系统已经给出当前回答；未标为完整验收，是因为独立来源、反面材料或逐句引用检查仍有待补齐';
  if (state?.status === 'verification_failed') return '写作已经完成，但引用核验没有全部通过，下一轮会围绕失败句子补材料';
  if (state?.status === 'failed' && delivery.mode === 'interrupted_evidence_limited') return '外部调用或页面读取中断前已保存材料；系统已用这些材料交付当前回答并保留恢复入口';
  if (state?.status === 'cancelled') return '任务已停止，页面只展示停止前已经保存的角色记录和材料';
  return done ? `${done}/6 个内部角色已有完成记录` : '等待真实角色执行记录写入';
}

function formatCitations(answer,evidence=window.__latestState?.evidence||[]){
  return formatAnswerDocument(answer,evidence);
}

function formatAnswerDocument(answer,evidence){
  const lines=String(answer||'').replace(/\r/g,'').split('\n');
  const blocks=[];
  let paragraph=[];
  let list=null;
  const inline=value=>renderCitationText(value,evidence,marker=>marker.item?`<a class="cite" href="#${escapeHTML(marker.id)}" data-evidence="${escapeHTML(marker.id)}">[${escapeHTML(marker.id)}]</a>`:`<span class="citation-unknown" role="note" aria-label="未知 Evidence ID ${escapeHTML(marker.id)}">[${escapeHTML(marker.id)}]<small>未知 ID</small></span>`,formatAnswerInlineText);
  const flushParagraph=()=>{
    if(!paragraph.length)return;
    blocks.push(`<p>${paragraph.map(inline).join('<br>')}</p>`);
    paragraph=[];
  };
  const flushList=()=>{
    if(!list)return;
    const start=list.type==='ol'&&list.start>1?` start="${list.start}"`:'';
    blocks.push(`<${list.type}${start}>${list.items.map(item=>`<li>${inline(item)}</li>`).join('')}</${list.type}>`);
    list=null;
  };
  const flush=()=>{
    flushParagraph();
    flushList();
  };
  lines.forEach(rawLine=>{
    const line=rawLine.trim();
    if(!line){
      flush();
      return;
    }
    const heading=line.match(/^#{1,3}\s+(.+?)\s*#*$/);
    if(heading){
      flush();
      blocks.push(`<h4>${inline(heading[1])}</h4>`);
      return;
    }
    const ordered=line.match(/^(\d+)\.\s+(.+)$/);
    const unordered=line.match(/^[-*]\s+(.+)$/);
    if(ordered||unordered){
      flushParagraph();
      const type=ordered?'ol':'ul';
      if(list&&list.type!==type)flushList();
      if(!list){
        list={type,items:[],start:ordered?Number(ordered[1]):1};
      }
      list.items.push(ordered?ordered[2]:unordered[1]);
      return;
    }
    flushList();
    paragraph.push(line);
  });
  flush();
  return blocks.join('');
}

function formatAnswerInlineText(value){
  return escapeHTML(value)
    .replace(/`([^`\r\n]+)`/g,'<code>$1</code>')
    .replace(/\*\*([^*\r\n]+)\*\*/g,'<strong>$1</strong>');
}

function overviewCitationItems(answer,evidence){
  const seen=new Set();
  const items=[];
  citationMarkers(answer,evidence).forEach(marker=>{
    if(seen.has(marker.id))return;
    seen.add(marker.id);
    items.push({id:marker.id,item:marker.item,index:items.length+1});
  });
  return items;
}

function formatOverviewCitations(answer,evidence){
  const citations=overviewCitationItems(answer,evidence);
  const indexes=new Map(citations.map(item=>[item.id,item]));
  return renderCitationText(answer,evidence,marker=>{
    const citation=indexes.get(marker.id);
    if(!marker.item)return`<span class="citation-unknown" role="note" aria-label="未知 Evidence ID ${escapeHTML(marker.id)}">[${escapeHTML(marker.id)}]<small>未知 ID</small></span>`;
    const fullLabel=marker.item.source_title||sourceDomain(marker.item.source_url);
    return `<span class="cite-cluster"><a class="cite source-labeled" href="#${escapeHTML(marker.id)}" data-evidence="${escapeHTML(marker.id)}" title="${escapeHTML(`打开证据 ${marker.id}：${fullLabel}`)}" aria-label="引用 ${citation?.index||'?'}，打开证据 ${escapeHTML(marker.id)}：${escapeHTML(fullLabel)}"><span>${citation?.index||'?'}</span></a></span>`;
  });
}

function evidenceReferenceMarkup(id,label=id){
  const evidenceId=String(id||'');
  if(evidenceIds().has(evidenceId))return`<a href="#${escapeHTML(evidenceId)}" data-evidence="${escapeHTML(evidenceId)}">${escapeHTML(label)}</a>`;
  return`<span class="citation-unknown" role="note" aria-label="未知 Evidence ID ${escapeHTML(evidenceId)}">${escapeHTML(label)}<small>未知 ID</small></span>`;
}

function revealAndFocus(target,message,block='center'){
  if(!target)return false;
  let details=target.matches?.('details')?target:target.closest?.('details');
  while(details){details.open=true;details=details.parentElement?.closest?.('details')||null}
  target.scrollIntoView(scrollOptions(block));
  let focusTarget=target.matches?.('a,button,summary,[tabindex]')?target:target.querySelector?.('summary,a,button,[tabindex]')||target;
  if(!focusTarget.matches?.('a,button,summary,[tabindex]'))focusTarget.setAttribute('tabindex','-1');
  focusTarget.focus?.({preventScroll:true});
  target.classList?.add('flash');
  window.setTimeout(()=>target.classList?.remove('flash'),1400);
  announceLive(message,`interaction:${message}`,true);
  return true;
}

function navigateToEvidence(id,sourceLabel='引用'){
  const evidenceId=String(id||'');
  const card=document.getElementById(evidenceId);
  if(!card){announceLive(`${sourceLabel} ${evidenceId} 是未知 Evidence ID，当前无法展开或核查。`,`unknown-evidence:${evidenceId}`,true);return false}
  return revealAndFocus(card,`${sourceLabel} ${evidenceId} 已展开并定位到证据账本。`);
}

function bindCitationLinks(root){
  if(!root)return;
  root.querySelectorAll('[data-evidence]').forEach(link=>link.addEventListener('click',event=>{event.preventDefault();navigateToEvidence(link.dataset.evidence,'证据引用')}));
}

function eventNumberLabel(payload, key, suffix = '') {
  const value = finiteValue(payload?.[key]);
  return value === null ? '未记录' : `${value}${suffix}`;
}

function eventScoreLabel(payload) {
  const value = finiteValue(payload?.score);
  return value === null ? '流程充分度未记录 · 不可计算' : `流程充分度 ${Math.round(Math.max(0, Math.min(1, value)) * 100)} / 100`;
}

function describeEvent(event){
  const p=asObject(event?.payload);
  const closed=recordedBoolean(p,'closed');
  const passed=recordedBoolean(p,'passed');
  const repairRound=recordedBoolean(p,'repair_round');
  const closureDetail=closed===true
    ? `${eventScoreLabel(p)}；记录的闭包状态为 closed，后端允许进入后续阶段。`
    : closed===false
      ? `${eventScoreLabel(p)}；记录的闭包状态未通过，必须继续补证。`
      : `${eventScoreLabel(p)}；闭包状态未记录，不能判断是否允许进入后续阶段。`;
  const verificationDetail=passed===true
    ? citationContractPassLabel
    : passed===false
      ? '发现需要补证的解析句单元，返回检索阶段修复。'
      : '引用判定字段未记录，不能判断是否通过。';
  const draftDetail=repairRound===true
    ? '根据核验意见重新组织候选回答。'
    : repairRound===false
      ? '候选回答应只使用已记录 Evidence Ledger 材料。'
      : '修复轮次字段未记录；候选回答仍需人工核对来源。';
  const status=String(p.status ?? '').trim();
  const d={
    perceive_inputs:['多模态感知智能体完成附件读取',`处理 ${eventNumberLabel(p,'attachments',' 个')} 内容寻址附件，形成 ${eventNumberLabel(p,'observations',' 条')} 带定位观察。`],
    plan:['规划智能体完成问题拆解',`识别出 ${eventNumberLabel(p,'slots',' 个')} 必须回答的关键目标。`],
    generate_queries:['检索智能体制定搜索路线',`生成 ${eventNumberLabel(p,'count',' 条')} 互补检索方案。`],
    search:['网页侦察员完成一次搜索',`发现 ${eventNumberLabel(p,'results',' 个')} 候选页面。`],
    search_and_fetch:['网页侦察员完成材料收集',`去重并保留 ${eventNumberLabel(p,'pages',' 个')} 可读取页面。`],
    ingest_evidence:['证据整理员更新账本',`新增 ${eventNumberLabel(p,'count',' 条')} 可回到原文核对的证据。`],
    assess_closure:['完整性审查员完成检查',closureDetail],
    draft:['写作智能体生成候选回答',draftDetail],
    verify:['引用核验员逐句验收',verificationDetail],
    citation_repair:['审查智能体执行证据修复',`补充 ${eventNumberLabel(p,'new_evidence',' 条')} 证据并重新检查。`],
    finalize:['研究总控完成交付',status==='completed'?'答案、来源和核验记录均已保存。':status?`任务以 ${status} 状态结束。`:'任务终态字段未记录。'],
    cancelled:['研究总控安全停止任务','已保存当前计划、文章与证据。'],
    recover:['故障恢复器记录异常','系统已保存现场和错误类型。']
  };
  const [title,detail]=d[event?.node]||['智能体完成一个内部步骤','研究状态已经更新。'];
  return{title,detail};
}

async function showMethodology(){
  const current=await getJSON('/api/methodology');
  const recorded=window.__latestState?.methodology||{};
  const hasRecordedFormula=Array.isArray(recorded.metric_contracts)&&recorded.metric_contracts.length>0;
  const hashesMatch=Boolean(recorded.metric_definition_hash&&current.metric_definition_hash&&recorded.metric_definition_hash===current.metric_definition_hash);
  const method=hasRecordedFormula?recorded:current;
  let provenance='当前运行没有持久化方法快照；以下仅是服务器当前方法，不能用于复算该历史运行。';
  let tone='mismatch';
  if(hasRecordedFormula){provenance=`以下公式来自该运行自身的持久化方法快照（${recorded.methodology_version}），可与运行分数一起归档复核。`;tone='matched'}
  else if(hashesMatch){provenance=`该运行记录的定义哈希与服务器当前方法一致（${recorded.metric_definition_hash.slice(0,12)}），以下当前公式可作为同定义解释。`;tone='matched'}
  else if(recorded.methodology_version){provenance=`该运行使用 ${recorded.methodology_version}，服务器当前为 ${current.methodology_version}；定义不一致，以下只作当前版本参考。`}
  const contracts=(method.metric_contracts||[]).map(item=>`<details class="metric-contract"><summary><b>${escapeHTML(item.name)}</b><span>${escapeHTML(item.metric_id)}</span></summary><dl><dt>分子</dt><dd>${escapeHTML(item.numerator)}</dd><dt>分母</dt><dd>${escapeHTML(item.denominator)}</dd><dt>算法</dt><dd>${escapeHTML(item.algorithm)}</dd><dt>决策作用</dt><dd>${escapeHTML(item.decision_role)}</dd></dl></details>`).join('');
  $('methodContent').innerHTML=`<div class="method-provenance ${tone}">${escapeHTML(provenance)}</div><div class="method-warning">${escapeHTML(method.warning||'这些指标不是事实概率。')}</div><p class="method-version">展示方法：${escapeHTML(method.methodology_version||'未记录')} · 定义哈希 ${escapeHTML(method.metric_definition_hash?.slice(0,12)||'未记录')}</p><h3>指标契约</h3><div class="metric-contracts">${contracts||'<p>该历史运行未保存完整指标契约。</p>'}</div><h3>回答目标的证据充分度</h3>${weightTable(method.slot_evidence_score||{})}<h3>整体研究完成度</h3>${weightTable(method.closure_score||{})}<h3>来源类型初始等级</h3>${weightTable(method.source_priors||{})}<h3>必须明确的限制</h3><ul>${(method.limitations||[]).map(item=>`<li>${escapeHTML(item)}</li>`).join('')}</ul>`;
  $('methodDialog').showModal()
}
function showMethodologyFailure(error){
  const message=String(error?.message||'评分方法接口暂时不可用');
  $('methodContent').innerHTML=`<div class="method-error" role="alert"><strong>评分方法暂时不可用</strong><p>当前无法读取服务器方法快照，不能把分数解释为可复算结果。</p><small>${escapeHTML(message)}</small><button type="button" data-method-retry>重新读取评分方法</button></div>`;
  if(!$('methodDialog').open) $('methodDialog').showModal();
  $('methodContent').querySelector('[data-method-retry]')?.addEventListener('click',()=>showMethodology().catch(showMethodologyFailure));
}
function weightTable(values){return `<div class="weight-table">${Object.entries(asObject(values)).map(([key,value])=>{const numeric=finiteValue(value);return `<div><span>${escapeHTML(methodName(key))}</span><b>${numeric===null?'权重未记录 · 不可计算':`${Math.round(Math.max(0,Math.min(1,numeric))*100)}%`}</b></div>`}).join('')}</div>`}
function methodName(key){return ({source_level:'来源等级',independent_source_corroboration:'独立来源互证',verbatim_extraction_consistency:'原文抽取一致性',conflict_resolution:'冲突是否解决',answer_slot_coverage:'回答目标覆盖',source_independence:'来源独立性',evidence_entailment:'原文定位一致性',exact_quote_localization:'原文逐字定位',source_reliability_prior:'来源类型先验',official:'官方网站/原始材料',paper:'学术论文',reference:'参考资料',web:'一般网页',source_targeting:'定向来源检索',entity_resolution:'实体消歧检索',contradiction_check:'反证搜索',bridge:'多跳桥接检索',broad_discovery:'广泛探索'})[key]||'指标类型未识别'}
function humanReliability(score){const value=finiteValue(score);if(value===null)return'来源等级未记录 · 不可计算';if(value>=.9)return'高等级来源';if(value>=.75)return'较高等级来源';return'一般来源'}
function stanceName(stance){return({supports:'支持结论',contradicts:'冲突证据',context:'背景信息'})[stance]||'参考证据'}
function operationName(value){return({perceive_inputs:'多模态附件感知',plan:'问题拆解',generate_queries:'缺口驱动查询',search:'候选来源检索',fetch:'文章正文读取',search_and_fetch:'检索并读取文章',extract_evidence:'逐字证据抽取',ingest_evidence:'证据账本入库',assess_closure:'证据充分度检查',citation_repair:'定向引用补材料',draft:'基于引用写作',compose_limited_answer:'组织当前可交付回答',verify:'逐句引用核验',check_limited_delivery:'检查回答交付边界',confirm_local_citation_binding:'本地引用绑定检查',finalize:'归档最终交付',emit_finalize:'研究总控归档交付'})[value]||'操作类型未识别'}
function invocationStatus(value){return({running:'执行中',succeeded:'已完成',failed:'失败',cancelled:'已取消'})[value]||'调用状态未识别'}
function phaseStateName(value){return({waiting:'等待输入',running:'正在执行',blocked:'等待补材料',done:'阶段完成',observed:'已有阶段记录'})[value]||'阶段状态未识别'}
function gateStatusName(value){return({passed:'已通过',failed:'未通过',blocked:'需要补材料',pending:'待检查',unknown:'未记录'})[String(value||'').toLowerCase()]||'阶段检查状态未识别'}
function invocationDuration(item){if(!item.started_at)return'未记录耗时';const start=new Date(item.started_at).getTime(),end=item.ended_at?new Date(item.ended_at).getTime():Date.now();const ms=Math.max(0,end-start);return ms<1000?`${ms} ms`:`${(ms/1000).toFixed(1)} s`}
function failureName(value){return({query_error:'查询生成失败',retrieval_miss:'检索失败',fetch_error:'页面读取失败',citation_error:'引用核验失败',ambiguous_operation:'模型请求状态不确定，为避免重复计费已暂停',runtime_error:'运行时异常'})[value]||'异常类型未识别'}
function gapName(value){return({missing_evidence:'缺少支持证据',missing_independent_source:'缺少独立来源',ungrounded_evidence:'原文无法可靠定位',contradiction_not_checked:'尚未执行反证搜索',unresolved_conflict:'证据冲突尚未裁决',unsupported_claim:'回答声明缺少完整支持'})[value]||'证据缺口类型未识别'}
function contradictionStatusName(value){return({search_failed:'搜索失败',no_results:'搜索无结果',results_returned:'已返回结果，等待正文检查',fetch_failed:'结果页面均未成功读取',inspected_irrelevant_only:'页面已读取，但全部未通过目标相关性准入',inspected_no_counterevidence:'已检查相关页面，未发现显式反证',cross_source_review_after_search_failure:'外部搜索限流后，已复核两篇独立已读取材料',counterevidence_found:'已在相关页面中发现反证'})[value]||'反证状态未识别'}
function sourcePreferenceName(value){return({independent_source:'独立来源',contradiction_search:'反证或纠错来源',official_source:'第一方原始来源'})[value]||'可核查来源'}
function independenceStatusName(value){return({verified:'按已记录 provenance 规则计为独立',weak_host_fallback:'不同发布域的弱近似',declared_publisher:'网页自报发布方，仅用于保守分组',declared_upstream:'网页声明跨域上游，仅用于保守分组',same_publisher_group:'同一发布方组，不重复计数',dependent:'同源或近重复，不重复计数',unknown:'尚未判定'})[value]||value||'尚未判定'}
function relevanceAdmissionThreshold(state){const value=finiteValue(state?.methodology?.admission_thresholds?.slot_relevance);return value!==null&&value>=0&&value<=1?value:null}
function evidenceEffectiveRole(item,state){
  const threshold=relevanceAdmissionThreshold(state);
  const relevance=finiteValue(item?.slot_relevance_score);
  if(threshold!==null&&relevance!==null&&relevance<threshold)return{kind:'excluded',reason:`目标相关性低于本运行准入线 ${Math.round(threshold*100)} / 100`};
  const thresholdNote=threshold===null?'本次运行准入阈值未记录，未使用当前默认值重判':`目标相关性按本运行准入线 ${Math.round(threshold*100)} / 100 检查`;
  const audit=rawSlotAudits(state).find(value=>value?.slot_id===item?.slot_id);
  const originalStance=['supports','contradicts','context'].includes(item?.stance)?item.stance:'context';
  if(!audit)return{kind:originalStance,reason:relevance===null?`历史目标相关性与闭包审计字段未记录，暂显示原始抽取立场；${thresholdNote}`:`尚无闭包审计，暂显示原始抽取立场；${thresholdNote}`};
  const supporting=recordedArray(audit,'supporting_evidence_ids');
  const contradicting=recordedArray(audit,'contradicting_evidence_ids');
  const excluded=recordedArray(audit,'consensus_excluded_evidence_ids');
  if(supporting?.includes(item?.id))return{kind:'supports',reason:'被闭包审计选入支持集'};
  if(contradicting?.includes(item?.id))return{kind:'contradicts',reason:'被共识或反证流程归入冲突集'};
  if(excluded?.includes(item?.id))return{kind:'excluded',reason:'通过相关性准入，但未被结构化共识 winner 选中'};
  if(supporting===null||contradicting===null)return{kind:originalStance,reason:`历史闭包审计集合字段未记录，当前不可验证，暂显示原始抽取立场；${thresholdNote}`};
  return{kind:'excluded',reason:'未出现在本次闭包审计的支持或冲突集合中'};
}
function closureAdmittedEvidence(state,requiredOnly=false){
  let audits=rawSlotAudits(state);
  if(requiredOnly){
    const requiredIds=new Set(requiredSlotDescriptors(state).map(item=>String(item.id)));
    audits=audits.filter(audit=>requiredIds.has(String(audit.slot_id||'')));
  }
  if(!audits.length)return[];
  const admitted=new Set(audits.flatMap(audit=>[...asArray(audit.supporting_evidence_ids),...asArray(audit.contradicting_evidence_ids)]));
  return asArray(state?.evidence).filter(item=>admitted.has(item?.id));
}
function isAmbiguousConsumer(value){return typeof value==='string'&&value.includes('-or-')}
function handoffRouteTarget(envelope){return envelope?.intended_consumer||envelope?.consumer||envelope?.route_target||null}

function invocationTransitionRecords(invocations = []) {
  const records = [];
  asArray(invocations).forEach((item, index, list) => {
    if (!index) return;
    const previous = list[index - 1];
    if (!previous?.agent_id || !item?.agent_id || previous.agent_id === item.agent_id) return;
    records.push({
      from: String(previous.agent_id),
      to: String(item.agent_id),
      previous,
      current: item,
    });
  });
  return records;
}

function receiptBackedAgentTransitions(handoffRecords = []) {
  return asArray(handoffRecords).flatMap(record => {
    const from = normalizedId(record?.envelope?.producer);
    const assessment = record?.assessment || {};
    const to = normalizedId(assessment.receipt?.consumed_by_agent_id);
    if (!agentOrder.includes(from) || !agentOrder.includes(to) || from === to) return [];
    if (!['server_validated', 'field_match'].includes(assessment.status)) return [];
    return [{from, to, status: assessment.status, messageId: normalizedId(record.id || record.envelope?.message_id)}];
  });
}

function handoffProofModel(record, expectedFrom = '', expectedTo = '', audit = window.__latestAudit || null) {
  const envelope = asObject(record?.envelope);
  const assessment = record?.assessment || handoffReceiptAssessment(envelope, [], [], audit);
  const receipt = assessment.receipt || null;
  const producer = normalizedId(envelope.producer);
  const consumer = normalizedId(receipt?.consumed_by_agent_id);
  const gatePassed = String(envelope.quality_gate?.status || '').toLowerCase() === 'passed';
  const artifacts = handoffArtifactRecords(envelope, audit);
  const artifactProofs = artifacts.map(item => artifactManifestProof(item, envelope, assessment, audit));
  const manifestValidCount = artifactProofs.filter(item => item.complete).length;
  const manifestComplete = artifactProofs.length > 0 && manifestValidCount === artifactProofs.length;
  const pairMatches = (!expectedFrom || producer === normalizedId(expectedFrom))
    && (!expectedTo || consumer === normalizedId(expectedTo));
  const serverValidated = assessment.status === 'server_validated';
  const proofReasons = [];
  if (!serverValidated) proofReasons.push(`接收确认状态为“${receiptStateLabel(assessment.status)}”，尚未由系统完整确认。`);
  if (!gatePassed) proofReasons.push('这条交接的阶段检查尚未明确通过。');
  if (!artifactProofs.length) proofReasons.push('这条交接没有可逐项核对的输出产物。');
  artifactProofs.filter(item => !item.complete).forEach(item => {
    proofReasons.push(`${item.artifactId || '未记录产物'}：${item.reasons.join(' ')}`);
  });
  if (!pairMatches) proofReasons.push('交接的发送方或实际接收方与当前路线不一致。');
  if (!producer || !consumer || producer === consumer) proofReasons.push('发送方或接收方身份不完整，或两者相同。');
  return {
    envelope,
    assessment,
    receipt,
    producer,
    consumer,
    artifacts,
    artifactProofs,
    gatePassed,
    manifestValidCount,
    manifestComplete,
    hasChecksummedArtifact: manifestComplete,
    pairMatches,
    proofReasons,
    strong: Boolean(serverValidated && gatePassed && manifestComplete && pairMatches && producer && consumer && producer !== consumer),
    producerInvocationId: normalizedId(envelope.producer_invocation_id),
    consumerInvocationId: normalizedId(receipt?.consumed_by_invocation_id),
  };
}

function isSha256Digest(value) {
  return /^[a-f0-9]{64}$/i.test(String(value || '').trim());
}

function artifactManifestProof(item, envelope, assessment, audit = window.__latestAudit || null) {
  const value = asObject(item);
  const embedded = asObject(value.__embedded_record);
  const manifest = asObject(value.__manifest_record);
  const artifactId = normalizedId(value.artifact_id || embedded.artifact_id || manifest.artifact_id);
  const messageId = normalizedId(envelope?.message_id);
  const runIdValue = normalizedId(envelope?.run_id);
  const producer = normalizedId(envelope?.producer);
  const producerInvocationId = normalizedId(envelope?.producer_invocation_id);
  const producerInvocation = assessment?.producerInvocation
    || audit?.byInvocation?.get(producerInvocationId)
    || null;
  const outputIdsRecorded = Boolean(
    producerInvocation
      && Array.isArray(producerInvocation.output_artifact_ids)
      && producerInvocation.__output_artifact_ids_recorded !== false,
  );
  const identityConflicts = [...new Set([
    ...asArray(value.__identity_conflicts),
    ...asArray(value.__merge_conflicts),
    ...asArray(manifest.__merge_conflicts),
  ])];
  const embeddedPresent = value.__embedded === true;
  const manifestPresent = value.__manifest === true;
  const embeddedChecksum = String(embedded.checksum || '').trim();
  const manifestChecksum = String(manifest.checksum || '').trim();
  const embeddedMetadataHash = String(embedded.metadata_hash || '').trim();
  const manifestMetadataHash = String(manifest.metadata_hash || '').trim();
  const checks = {
    embeddedReference: embeddedPresent,
    durableManifest: manifestPresent,
    checksumSha256: isSha256Digest(embeddedChecksum) && isSha256Digest(manifestChecksum),
    checksumAgreement: Boolean(embeddedChecksum && embeddedChecksum === manifestChecksum),
    metadataHashSha256: isSha256Digest(embeddedMetadataHash) && isSha256Digest(manifestMetadataHash),
    metadataHashAgreement: Boolean(embeddedMetadataHash && embeddedMetadataHash === manifestMetadataHash),
    messageBinding: Boolean(
      messageId
        && normalizedId(embedded.handoff_message_id) === messageId
        && normalizedId(manifest.handoff_message_id) === messageId,
    ),
    producerBinding: Boolean(
      producerInvocationId
        && normalizedId(embedded.producer_invocation_id) === producerInvocationId
        && normalizedId(manifest.producer_invocation_id) === producerInvocationId,
    ),
    roleBinding: Boolean(producer && normalizedId(embedded.producer) === producer),
    runBinding: Boolean(runIdValue && normalizedId(manifest.run_id) === runIdValue),
    contentAddress: Boolean(
      String(embedded.content_uri || manifest.content_uri || '').trim()
        && finiteValue(embedded.byte_length ?? manifest.byte_length) !== null
        && String(embedded.media_type || manifest.media_type || '').trim()
        && String(embedded.canonicalization || manifest.canonicalization || '').trim(),
    ),
    manifestValid: manifest.manifest_valid === true,
    filesPresent: manifest.files_present === true,
    committed: String(manifest.status || '').toLowerCase() === 'committed',
    integrityVerified: String(manifest.integrity_status || '').toLowerCase() === 'verified',
    passable: manifest.passable === true,
    producerInvocation: Boolean(
      producerInvocation
        && producerInvocation.status === 'succeeded'
        && invocationIdentityValidation(producerInvocation).reliable
        && outputIdsRecorded
        && asArray(producerInvocation.output_artifact_ids).map(String).includes(artifactId),
    ),
    noIdentityConflict: identityConflicts.length === 0,
  };
  const labels = {
    embeddedReference:'交接记录中没有引用这个产物。',
    durableManifest:'没有找到这个产物对应的已保存清单。',
    checksumSha256:'产物校验信息不是完整的 SHA-256 值。',
    checksumAgreement:'交接记录与已保存清单的产物校验信息不一致。',
    metadataHashSha256:'产物元数据校验信息不是完整的 SHA-256 值。',
    metadataHashAgreement:'交接记录与已保存清单的元数据校验信息不一致。',
    messageBinding:'产物没有同时关联到当前这条交接记录。',
    producerBinding:'产物没有同时关联到当前发送方的执行记录。',
    roleBinding:'产物记录的发送角色与交接记录不一致。',
    runBinding:'已保存产物清单与交接记录不属于同一次运行。',
    contentAddress:'缺少可重新核对的内容位置、大小、媒体类型或保存规则。',
    manifestValid:'已保存产物清单尚未确认有效。',
    filesPresent:'产物内容文件或元数据文件未确认存在。',
    committed:'产物清单尚未标记为已保存。',
    integrityVerified:'系统尚未确认产物完整性。',
    passable:'产物清单尚未标记为可用于后续步骤。',
    producerInvocation:'发送方执行记录未完成、身份未核对，或没有列出这个产物。',
    noIdentityConflict:`交接记录与已保存产物清单存在关键字段冲突${identityConflicts.length ? `：${identityConflicts.join('、')}` : ''}。`,
  };
  const reasons = Object.entries(checks).filter(([, passed]) => !passed).map(([key]) => labels[key]);
  return {
    artifactId,
    embeddedPresent,
    manifestPresent,
    orphanManifest: manifestPresent && !embeddedPresent,
    identityConflicts,
    checks,
    reasons,
    complete: reasons.length === 0,
  };
}

function handoffProofChecklistMarkup(proof) {
  const receiptLabels = {
    durableHandoff:'发送交接记录已保存',
    durableReceipt:'接收确认记录已保存',
    runTrace:'属于同一次运行',
    producerInvocation:'发送方执行记录能对应',
    consumerInvocation:'接收方执行记录能对应',
    operation:'接收操作能对应',
    producerReference:'接收记录能指回发送方',
    serverChecks:'系统检查项已完成',
  };
  const receiptChecks = Object.entries(receiptLabels).map(([key, label]) => {
    const passed = proof.assessment?.checks?.[key] === true;
    return `<li class="${passed ? 'passed' : 'missing'}"><b>${passed ? '通过' : '缺失'}</b><span>${escapeHTML(label)}</span></li>`;
  }).join('');
  const artifactRows = proof.artifactProofs.length
    ? proof.artifactProofs.map(item => `<li class="${item.complete ? 'passed' : 'missing'}"><b>${item.complete ? '可重新核对' : '还无法确认'}</b><span>${escapeHTML(item.artifactId || '产物编号未记录')}</span><small>${escapeHTML(item.complete ? '产物摘要、文件、完整性和发送方输出清单均能对应' : item.reasons.join(' '))}</small></li>`).join('')
    : '<li class="missing"><b>未记录</b><span>没有输出产物可核对</span></li>';
  return `<div class="handoff-proof-checklist"><div><span>发送与接收记录</span><ol>${receiptChecks}</ol></div><div><span>阶段检查与已保存产物</span><ol><li class="${proof.gatePassed ? 'passed' : 'missing'}"><b>${proof.gatePassed ? '已通过' : '未通过'}</b><span>本次交接的阶段检查</span></li>${artifactRows}</ol></div></div>`;
}

function networkEdgeStatus(route) {
  if (route.strongCount > 0) return {tone: 'strong', label: '完整可查交接', note: '至少一条交接记录的发送、接收、系统确认、阶段检查和已保存产物都能对上'};
  if (route.receiptCounts.server_validated > 0) return {tone: 'server', label: '系统已确认接收，信息仍不完整', note: '系统已经确认接收方，但本次交接的阶段检查或已保存产物还无法完整核对'};
  if (route.receiptCounts.field_match > 0) return {tone: 'field', label: '信息能对应，待系统确认', note: '字段指向接收方，但系统尚未完成确认'};
  if (route.receiptCounts.invalid > 0) return {tone: 'invalid', label: '记录不一致，暂不采用', note: '不能把这条路线当作已接收'};
  if (route.receiptCounts.unverified > 0) return {tone: 'unverified', label: '交接尚未确认', note: '只看到了计划交接，或接收记录不完整'};
  if (route.orderSignalCount > 0) return {tone: 'order', label: '只有执行先后记录', note: '相邻执行记录不证明角色已经交接'};
  return {tone: 'designed', label: '只有设计路线', note: '本次运行还没有可查的交接记录'};
}

function networkEdgeLedgerModel(invocations = [], events = [], audit = window.__latestAudit || null) {
  const records = auditHandoffRecords(events, audit).map(record => ({
    ...record,
    assessment: handoffReceiptAssessment(record.envelope, events, invocations, audit),
  }));
  const orderSignals = invocationTransitionRecords(invocations);
  const designPairs = new Set(agentOrder.map((from, index) => `${from}->${agentOrder[(index + 1) % agentOrder.length]}`));
  const routes = agentOrder.map((from, index) => {
    const to = agentOrder[(index + 1) % agentOrder.length];
    const pair = `${from}->${to}`;
    const orderSignalsForEdge = orderSignals.filter(item => item.from === from && item.to === to);
    const edgeRecords = records.filter(record => {
      const envelope = record.envelope || {};
      const actualConsumer = normalizedId(record.assessment?.receipt?.consumed_by_agent_id);
      return envelope.producer === from && (handoffRouteTarget(envelope) === to || actualConsumer === to);
    });
    const proof = edgeRecords.map(record => handoffProofModel(record, from, to, audit));
    const receiptCounts = proof.reduce((counts, item) => {
      const status = item.assessment.status || 'unverified';
      counts[status] = (counts[status] || 0) + 1;
      return counts;
    }, {server_validated: 0, field_match: 0, unverified: 0, invalid: 0});
    const route = {
      index,
      from,
      to,
      pair,
      fromName: agentContracts[from]?.name || from,
      toName: agentContracts[to]?.name || to,
      orderSignalCount: orderSignalsForEdge.length,
      orderSignals: orderSignalsForEdge,
      records: edgeRecords,
      proof,
      receiptEvidenceCount: proof.filter(item => Boolean(item.receipt)).length,
      receiptCounts,
      gatePassedCount: proof.filter(item => item.gatePassed).length,
      checksummedArtifactCount: proof.filter(item => item.hasChecksummedArtifact).length,
      manifestValidCount: proof.filter(item => item.manifestComplete).length,
      strongCount: proof.filter(item => item.strong).length,
    };
    return {...route, ...networkEdgeStatus(route)};
  });
  const repairs = records.filter(record => {
    const producer = normalizedId(record.envelope?.producer);
    const consumer = normalizedId(record.assessment?.receipt?.consumed_by_agent_id);
    return agentOrder.includes(producer) && agentOrder.includes(consumer)
      && producer !== consumer
      && !designPairs.has(`${producer}->${consumer}`)
      && ['server_validated', 'field_match'].includes(record.assessment?.status);
  }).map(record => {
    const proof = handoffProofModel(record, record.envelope?.producer, record.assessment?.receipt?.consumed_by_agent_id, audit);
    return {
      ...proof,
      from: proof.producer,
      to: proof.consumer,
      fromName: agentContracts[proof.producer]?.name || proof.producer,
      toName: agentContracts[proof.consumer]?.name || proof.consumer,
    };
  });
  const sequence = orderSignals.map((item, index) => {
    const matches = records
      .map(record => handoffProofModel(record, item.from, item.to, audit))
      .filter(proof => (
        proof.producerInvocationId === normalizedId(item.previous?.invocation_id)
          && proof.consumerInvocationId === normalizedId(item.current?.invocation_id)
      ));
    const evidence = matches.find(proof => proof.strong)
      || matches.find(proof => proof.assessment.status === 'server_validated')
      || matches.find(proof => proof.assessment.status === 'field_match')
      || null;
    const kind = evidence?.strong ? 'strong' : evidence?.assessment.status === 'server_validated' ? 'server' : evidence?.assessment.status === 'field_match' ? 'field' : 'order';
    return {
      ...item,
      index,
      kind,
      evidence,
      fromName: agentContracts[item.from]?.name || item.from,
      toName: agentContracts[item.to]?.name || item.to,
    };
  });
  return {
    routes,
    repairs,
    sequence,
    rawTransitionCount: orderSignals.length,
    receiptRouteCount: routes.filter(route => route.receiptEvidenceCount > 0).length,
    totalEnvelopeCount: records.length,
    strongCount: routes.reduce((sum, route) => sum + route.strongCount, 0),
  };
}

function receiptStateLabel(status) {
  return receiptStatePresentation[status]?.label || '尚未确认接收';
}

function receiptStateRawLabel(status) {
  return receiptStatePresentation[status]?.raw || String(status || 'unverified');
}

function receiptManualCheck(status) {
  return ({
    server_validated:'打开接收方的执行记录，核对它是否记录了这次交接、同一次运行和对应产物。',
    field_match:'两端信息看起来一致，但系统尚未确认；请查看接收方执行记录或重新运行检查。',
    unverified:'目前只能确认发送方计划交接；请查看后续执行记录是否明确接收了这条交接。',
    invalid:'不要把这条记录当作已经交接；请根据错误原因检查是否跨运行、缺少执行记录或角色不一致。',
  })[status] || '打开任务交接记录与执行记录进行人工核对。';
}

function normalizedReceiptStatus(value, row = null) {
  const raw = String(value || '').toLowerCase().replace(/-/g, '_');
  if (row?.valid === false || asArray(row?.__merge_conflicts).length || ['invalid', 'rejected', 'failed'].includes(raw)) return 'invalid';
  if (row?.server_validated === true || raw === 'server_validated') return 'server_validated';
  if (['field_match', 'matched', 'fields_match'].includes(raw)) return 'field_match';
  return 'unverified';
}

function auditHandoffRecords(events = window.__latestEvents || [], audit = window.__latestAudit || null) {
  const records = new Map();
  const durableHandoffs = audit?.available ? asArray(audit.handoffs) : [];
  durableHandoffs.forEach(row => {
    const envelope = asObject(row?.envelope);
    const messageId = normalizedId(row?.message_id || envelope.message_id);
    if (!messageId) return;
    const receipt = durableReceiptForMessage(messageId, audit) || (
      row?.consumed_by_invocation_id
        ? normalizeAuditReceipt({
          ...row,
          message_id:messageId,
          validation_status:row.server_validation_status || row.validation_status,
          server_validated:row.server_validation_status === 'server_validated',
        })
        : null
    );
    records.set(messageId, {id:messageId, envelope, durable:row, receipt, event:null});
  });
  asArray(events).forEach(event => {
    const envelope = eventEnvelope(event);
    const messageId = normalizedId(envelope?.message_id);
    if (!messageId) return;
    const current = records.get(messageId);
    if (current) current.event = event;
    else records.set(messageId, {id:messageId, envelope, durable:null, receipt:null, event});
  });
  return [...records.values()];
}

function handoffArtifactRecords(envelope, audit = window.__latestAudit || null) {
  const messageId = normalizedId(envelope?.message_id);
  const embedded = asArray(envelope?.output_artifacts);
  const byId = new Map(embedded.map(item => {
    const record = asObject(item);
    return [normalizedId(record.artifact_id), {
      ...record,
      __embedded:true,
      __manifest:false,
      __embedded_record:{...record},
      __manifest_record:null,
      __identity_conflicts:[],
    }];
  }).filter(([id]) => id));
  if (audit?.available && messageId) {
    asArray(audit.artifacts).forEach(item => {
      if (normalizedId(item?.handoff_message_id) !== messageId) return;
      const id = normalizedId(item?.artifact_id);
      if (!id) return;
      const current = asObject(byId.get(id));
      const embeddedItem = asObject(current.__embedded_record);
      const manifestItem = asObject(item);
      const identityFields = ['checksum', 'metadata_hash', 'producer', 'producer_invocation_id', 'handoff_message_id', 'content_uri', 'byte_length', 'media_type', 'canonicalization'];
      const conflicts = identityFields.filter(field => (
        embeddedItem[field] !== undefined
          && manifestItem[field] !== undefined
          && String(embeddedItem[field]) !== String(manifestItem[field])
      ));
      byId.set(id, {
        ...current,
        ...manifestItem,
        __embedded:current.__embedded === true,
        __manifest:true,
        __embedded_record:current.__embedded === true ? embeddedItem : null,
        __manifest_record:{...manifestItem},
        __orphan_manifest:current.__embedded !== true,
        __identity_conflicts:[...new Set([...asArray(current.__identity_conflicts), ...conflicts, ...asArray(manifestItem.__merge_conflicts)])],
      });
    });
  }
  return [...byId.values()];
}

function durableReceiptForMessage(messageId, audit = window.__latestAudit || null) {
  if (!audit?.available) return null;
  const id = normalizedId(messageId);
  const rows = audit.receiptRowsByMessage?.get(id) || [];
  const invalid = asArray(rows).find(item => normalizedReceiptStatus(item?.normalized_status || item?.validation_status || item?.status, item) === 'invalid');
  return invalid || audit.receiptByMessage?.get(id) || null;
}

function handoffReceiptAssessment(
  envelope,
  events = window.__latestEvents || [],
  invocations = auditInvocationList(),
  audit = window.__latestAudit || null,
) {
  const source = envelope && typeof envelope === 'object' ? envelope : null;
  const messageId = normalizedId(source?.message_id);
  const invocationList = asArray(invocations);
  const eventList = asArray(events);
  const result = {
    status:'unverified',
    receipt:null,
    receiptEnvelope:null,
    receiptEvent:null,
    producerInvocation:null,
    consumerInvocation:null,
    checks:{},
    reasons:[],
    manualCheck:receiptManualCheck('unverified'),
  };
  if (!messageId) {
    result.reasons.push('发送信封没有 message_id，无法建立消费关系。');
    return result;
  }

  const durableHandoff = audit?.available ? audit.handoffByMessage?.get(messageId) : null;
  const durableReceipt = durableReceiptForMessage(messageId, audit)
    || (durableHandoff?.receipt || null);
  const candidates = [];
  // Durable audit is the source of truth. Event receipts can supplement fields,
  // but can never elevate a handoff to server_validated.
  if (durableReceipt) {
    const durableEnvelope = durableHandoff?.envelope || source;
    const receipt = {
      ...durableReceipt,
      message_id:normalizedId(durableReceipt.message_id || messageId),
    };
    candidates.push({
      receipt,
      envelope:durableEnvelope,
      event:null,
      durable:true,
      durableStatus:normalizedReceiptStatus(
        durableReceipt?.normalized_status
          || durableReceipt?.validation_status
          || durableHandoff?.server_validation_status
          || durableHandoff?.receipt_status,
        durableReceipt || durableHandoff,
      ),
    });
  }
  eventList.forEach(event => {
    const candidateEnvelope = eventEnvelope(event);
    const receipt = candidateEnvelope?.receipt;
    if (receipt && String(receipt.message_id || '') === messageId) {
      // Do not duplicate a durable candidate. This event is still retained as
      // a timestamp/display supplement when the durable row exists.
      const existing = candidates.find(item => item.durable);
      if (existing) {
        existing.event ||= event;
      } else {
        candidates.push({receipt, envelope:candidateEnvelope, event, durable:false, durableStatus:null});
      }
    }
  });
  if (source?.receipt && String(source.receipt.message_id || '') === messageId && !candidates.some(item => item.receipt === source.receipt)) {
    candidates.push({receipt:source.receipt, envelope:source, event:null, durable:false, durableStatus:null});
  }

  const directConsumers = invocationList.filter(item => asArray(item?.consumed_handoff_message_ids).map(String).includes(messageId));
  const invocationAuditAuthoritative = Boolean(
    audit?.available
      && audit?.pages?.invocations?.present
      && audit.pages.invocations.hasMore !== true,
  );
  if (!candidates.length) {
    result.consumerInvocation = directConsumers.length === 1 ? directConsumers[0] : null;
    result.reasons.push(durableHandoff
      ? '已保存的发送交接记录存在，但没有对应的接收确认；当前只能显示为尚未确认接收。'
      : directConsumers.length
        ? `${directConsumers.length} 条执行记录声明使用了这条交接，但没有配套接收确认记录。`
        : '后续事件没有引用这个交接编号的接收确认。');
    return result;
  }

  const assessments = [];
  for (const candidate of candidates) {
    const receipt = candidate.receipt;
    const receiptEnvelope = candidate.envelope || {};
    const consumerId = normalizedId(receipt?.consumed_by_invocation_id);
    const consumerAgent = normalizedId(receipt?.consumed_by_agent_id);
    const consumerInvocation = invocationList.find(item => String(item?.invocation_id || '') === consumerId) || null;
    const producerId = normalizedId(receiptEnvelope?.producer_invocation_id || source?.producer_invocation_id || durableHandoff?.producer_invocation_id);
    const producerAgent = normalizedId(receiptEnvelope?.producer || source?.producer || durableHandoff?.producer);
    const producerInvocation = invocationList.find(item => normalizedId(item?.invocation_id) === producerId) || null;
    const errors = [];
    const missing = [];
    const declaredStatus = candidate.durableStatus || normalizedReceiptStatus(
      receiptEnvelope?.receipt_validation,
      receipt,
    );
    const sourceRun = normalizedId(receiptEnvelope?.run_id || durableHandoff?.run_id || source?.run_id);
    const sourceTrace = normalizedId(receiptEnvelope?.trace_id || durableHandoff?.trace_id || source?.trace_id);
    const intendedConsumer = normalizedId(receiptEnvelope?.intended_consumer || receiptEnvelope?.consumer || source?.intended_consumer || source?.consumer);
    const receiptValidation = asObject(receipt?.validation || durableHandoff?.receipt_validation);
    const serverChecks = asObject(receiptValidation.checks);
    const expectedServerChecks = [
      'run_trace_scope', 'source_message_exists', 'source_producer_binding',
      'intended_consumer_binding', 'consumer_invocation_exists',
      'consumer_operation_matches_route', 'explicit_consumption_binding',
      'active_consumption_fence', 'single_consumer', 'timestamp_order',
    ];

    if (declaredStatus === 'invalid' || receipt?.valid === false) {
      errors.push(receiptEnvelope?.receipt_validation_error || receipt?.validation_error || '系统已将这条接收确认标记为记录不一致。');
    }
    if (asArray(receipt?.__merge_conflicts).length) errors.push(`接收确认记录存在关键字段冲突：${receipt.__merge_conflicts.join('、')}。`);
    if (Object.keys(asObject(durableHandoff?.__identity_conflicts)).length || asArray(durableHandoff?.__merge_conflicts).length) errors.push('已保存任务交接记录的身份字段与交接内容或分页记录冲突。');
    if (!consumerId) missing.push('接收确认没有写明接收方执行记录。');
    if (!consumerAgent) missing.push('接收确认没有写明接收角色。');
    if (!consumerInvocation && consumerId) {
      (invocationAuditAuthoritative ? errors : missing).push(
        invocationAuditAuthoritative
          ? `接收确认引用了不存在的执行记录 ${consumerId}。`
          : `当前审计窗口尚未载入接收方执行记录 ${consumerId}。`,
      );
    }
    if (!sourceRun) missing.push('发送交接记录缺少运行编号。');
    if (!sourceTrace) missing.push('发送交接记录缺少链路编号。');
    if (!normalizedId(receipt?.run_id)) missing.push('接收确认缺少运行编号。');
    if (!normalizedId(receipt?.trace_id)) missing.push('接收确认缺少链路编号。');
    if (sourceRun && normalizedId(receipt?.run_id) && normalizedId(receipt.run_id) !== sourceRun) errors.push('接收确认与发送交接记录不属于同一次运行。');
    if (sourceTrace && normalizedId(receipt?.trace_id) && normalizedId(receipt.trace_id) !== sourceTrace) errors.push('接收确认与发送交接记录不属于同一条链路。');
    if (sourceRun && normalizedId(receiptEnvelope?.run_id) && normalizedId(receiptEnvelope.run_id) !== sourceRun) errors.push('承载接收确认的记录不属于同一次运行。');
    if (sourceTrace && normalizedId(receiptEnvelope?.trace_id) && normalizedId(receiptEnvelope.trace_id) !== sourceTrace) errors.push('承载接收确认的记录不属于同一条链路。');
    if (intendedConsumer && consumerAgent && intendedConsumer !== consumerAgent) errors.push(`实际声明角色 ${consumerAgent} 与计划接收角色 ${intendedConsumer} 不一致。`);
    if (consumerInvocation && consumerAgent && String(consumerInvocation.agent_id || '') !== consumerAgent) errors.push('接收确认中的角色与接收方执行记录不一致。');
    if (consumerInvocation && !consumerInvocation.status) missing.push('接收方执行记录缺少执行状态。');
    if (consumerInvocation && consumerInvocation.status && consumerInvocation.status !== 'succeeded') errors.push(`接收方执行记录状态为 ${consumerInvocation.status}，不是已完成。`);
    if (consumerInvocation) {
      const identity = invocationIdentityValidation(consumerInvocation);
      if (identity.status === 'not_recorded') missing.push('接收方执行记录未保存身份核对结果。');
      else if (!identity.reliable) errors.push(`接收方执行记录身份核对异常：${identity.reason}`);
    }
    const consumedIdsRecorded = Boolean(consumerInvocation && Array.isArray(consumerInvocation.consumed_handoff_message_ids) && consumerInvocation.__consumed_handoff_message_ids_recorded !== false);
    if (consumerInvocation && !consumedIdsRecorded) missing.push('接收方执行记录没有保存它接收的交接编号。');
    if (consumerInvocation && consumedIdsRecorded && !asArray(consumerInvocation.consumed_handoff_message_ids).map(String).includes(messageId)) errors.push('接收方执行记录没有声明接收当前交接编号。');
    if (sourceRun && consumerInvocation && normalizedId(consumerInvocation.run_id) && normalizedId(consumerInvocation.run_id) !== sourceRun) errors.push('接收方执行记录与交接记录不属于同一次运行。');
    if (sourceTrace && consumerInvocation && normalizedId(consumerInvocation.trace_id) && normalizedId(consumerInvocation.trace_id) !== sourceTrace) errors.push('接收方执行记录与交接记录不属于同一条链路。');
    if (consumerInvocation && !normalizedId(consumerInvocation.run_id)) missing.push('接收方执行记录缺少运行编号。');
    if (consumerInvocation && !normalizedId(consumerInvocation.trace_id)) missing.push('接收方执行记录缺少链路编号。');
    if (!normalizedId(receipt?.consumed_by_operation)) missing.push('接收确认没有写明接收操作。');
    if (normalizedId(receipt?.consumed_by_operation) && consumerInvocation?.operation && normalizedId(receipt.consumed_by_operation) !== normalizedId(consumerInvocation.operation)) errors.push('接收确认中的操作与接收方执行记录不一致。');
    if (!producerId) missing.push('发送交接记录缺少发送方执行记录。');
    if (!producerInvocation && producerId) {
      (invocationAuditAuthoritative ? errors : missing).push(
        invocationAuditAuthoritative
          ? `发送交接记录引用了不存在的执行记录 ${producerId}。`
          : `当前审计窗口尚未载入发送方执行记录 ${producerId}。`,
      );
    }
    if (producerInvocation && normalizedId(producerInvocation.agent_id) !== producerAgent) errors.push('发送交接记录中的角色与发送方执行记录不一致。');
    if (producerInvocation && !producerInvocation.status) missing.push('发送方执行记录缺少执行状态。');
    if (producerInvocation && producerInvocation.status && producerInvocation.status !== 'succeeded') errors.push(`发送方执行记录状态为 ${producerInvocation.status}，不是已完成。`);
    if (producerInvocation) {
      const identity = invocationIdentityValidation(producerInvocation);
      if (identity.status === 'not_recorded') missing.push('发送方执行记录未保存身份核对结果。');
      else if (!identity.reliable) errors.push(`发送方执行记录身份核对异常：${identity.reason}`);
    }
    const producedIdsRecorded = Boolean(producerInvocation && Array.isArray(producerInvocation.handoff_message_ids) && producerInvocation.__handoff_message_ids_recorded !== false);
    if (producerInvocation && !producedIdsRecorded) missing.push('发送方执行记录没有保存它产生的交接编号。');
    if (producerInvocation && producedIdsRecorded && !asArray(producerInvocation.handoff_message_ids).map(String).includes(messageId)) errors.push('发送方执行记录没有声明产生当前交接编号。');
    if (sourceRun && producerInvocation && normalizedId(producerInvocation.run_id) && normalizedId(producerInvocation.run_id) !== sourceRun) errors.push('发送方执行记录与交接记录不属于同一次运行。');
    if (sourceTrace && producerInvocation && normalizedId(producerInvocation.trace_id) && normalizedId(producerInvocation.trace_id) !== sourceTrace) errors.push('发送方执行记录与交接记录不属于同一条链路。');
    if (producerInvocation && !normalizedId(producerInvocation.run_id)) missing.push('发送方执行记录缺少运行编号。');
    if (producerInvocation && !normalizedId(producerInvocation.trace_id)) missing.push('发送方执行记录缺少链路编号。');
    const receiptProducerId = normalizedId(receipt?.consumed_from_producer_invocation_id);
    if (!receiptProducerId) missing.push('接收确认没有指回发送方执行记录。');
    if (receiptProducerId && producerId && receiptProducerId !== producerId) errors.push('接收确认指向了错误的发送方执行记录。');
    if (candidate.durable && !durableHandoff) errors.push('找到了孤立的接收确认，但没有同一交接编号的发送记录。');
    if (candidate.durable && declaredStatus === 'server_validated') {
      if (receipt?.valid !== true) missing.push('系统确认记录没有明确标注为有效。');
      if (receipt?.server_validated !== true) missing.push('系统确认记录没有明确标注为已由系统验证。');
      if (receiptValidation.valid !== true || normalizedReceiptStatus(receiptValidation.status, receiptValidation) !== 'server_validated') missing.push('接收确认缺少完整的系统检查结果。');
      const failedChecks = expectedServerChecks.filter(key => serverChecks[key] !== true);
      if (failedChecks.length) missing.push(`系统检查未完成：${failedChecks.join('、')}。技术字段可在详细记录中查看。`);
    }

    const checks = {
      durableHandoff:Boolean(durableHandoff),
      durableReceipt:Boolean(candidate.durable),
      runTrace:Boolean(sourceRun && sourceTrace && normalizedId(receipt?.run_id) === sourceRun && normalizedId(receipt?.trace_id) === sourceTrace),
      producerInvocation:Boolean(
        producerInvocation
          && producerInvocation.status === 'succeeded'
          && invocationIdentityValidation(producerInvocation).reliable
          && producedIdsRecorded
          && asArray(producerInvocation.handoff_message_ids).map(String).includes(messageId),
      ),
      consumerInvocation:Boolean(
        consumerInvocation
          && consumerInvocation.status === 'succeeded'
          && invocationIdentityValidation(consumerInvocation).reliable
          && consumedIdsRecorded
          && asArray(consumerInvocation.consumed_handoff_message_ids).map(String).includes(messageId),
      ),
      operation:Boolean(normalizedId(receipt?.consumed_by_operation) && normalizedId(receipt?.consumed_by_operation) === normalizedId(consumerInvocation?.operation)),
      producerReference:Boolean(receiptProducerId && receiptProducerId === producerId),
      serverChecks:Boolean(expectedServerChecks.every(key => serverChecks[key] === true)),
    };
    const fieldComplete = Boolean(
      consumerId
        && consumerAgent
        && consumerInvocation
        && consumedIdsRecorded
        && asArray(consumerInvocation.consumed_handoff_message_ids).map(String).includes(messageId),
    );
    const serverComplete = Boolean(
      candidate.durable
        && declaredStatus === 'server_validated'
        && Object.values(checks).every(Boolean)
        && !errors.length
        && !missing.length,
    );
    assessments.push({
      ...candidate,
      declaredStatus,
      producerInvocation,
      consumerInvocation,
      errors,
      missing,
      fieldComplete,
      serverComplete,
      checks,
    });
  }

  const invalid = assessments.find(item => item.errors.length);
  const serverValidated = assessments.find(item => item.serverComplete && !item.errors.length);
  const fieldMatch = assessments.find(item => item.fieldComplete && !item.errors.length);
  const chosen = invalid || serverValidated || fieldMatch || assessments[0] || candidates[0];
  result.receipt = chosen.receipt || null;
  result.receiptEnvelope = chosen.envelope || null;
  result.receiptEvent = chosen.event || null;
  result.producerInvocation = chosen.producerInvocation || null;
  result.consumerInvocation = chosen.consumerInvocation || null;
  result.checks = chosen.checks || {};
  if (invalid) {
    result.status = 'invalid';
    result.reasons = invalid.errors.length ? invalid.errors : ['接收确认被明确标记为记录不一致。'];
  } else if (serverValidated) {
    result.status = 'server_validated';
    result.reasons = ['发送与接收记录使用同一交接编号，双方执行记录、运行范围、接收关系和系统检查项均能对应。'];
  } else if (fieldMatch) {
    result.status = 'field_match';
    result.reasons = [fieldMatch.durable
      ? `接收信息能对应，但系统确认尚未完整。${fieldMatch.missing.length ? ` 缺少：${fieldMatch.missing.join(' ')}` : ''}`
      : '事件或交接记录的信息看起来一致，但没有完整的系统确认；事件字段不能单独证明系统已确认接收。'];
  } else {
    result.reasons = [`找到了接收确认字段，但发送到接收的记录链不完整。${asArray(chosen.missing).length ? ` 缺少：${chosen.missing.join(' ')}` : ''}`];
  }
  result.manualCheck = receiptManualCheck(result.status);
  return result;
}

function handoffReceipt(
  envelope,
  events = window.__latestEvents || [],
  invocations = window.__latestState?.agent_invocations || [],
  audit = window.__latestAudit || null,
) {
  return handoffReceiptAssessment(envelope, events, invocations, audit).receipt;
}

function handoffConsumerLabel(
  value,
  envelope,
  event,
  events = window.__latestEvents || [],
  invocations = window.__latestState?.agent_invocations || [],
  audit = window.__latestAudit || null,
) {
  if (!value) return '计划接收方未记录';
  const target = agentContracts[value]?.name || value;
  if (isAmbiguousConsumer(value)) return `历史计划指向 ${value}，无法确认实际接收方`;
  const assessment = handoffReceiptAssessment(envelope, events, invocations, audit);
  const actualId = assessment.receipt?.consumed_by_agent_id;
  const actual = agentContracts[actualId]?.name || actualId || target;
  if (assessment.status === 'server_validated') return `系统确认由 ${actual} 接收`;
  if (assessment.status === 'field_match') return `记录显示由 ${actual} 接收，等待系统确认`;
  if (assessment.status === 'invalid') return `记录称由 ${actual} 接收，但存在不一致，暂不采用`;
  return `计划交给 ${target}；实际接收尚未确认`;
}
function graphKindName(value){return({query:'检索路线',source:'调研文章',fetch:'页面读取',evidence:'原文证据',target:'回答目标'})[value]||value}
function sourceTypeName(value){const key=String(value??'').trim().toLowerCase().replace(/[-\s]+/g,'_');return({official:'第一方原始资料',paper:'学术论文',reference:'参考资料',web:'一般网页',signal:'信号类 · 身份未验证',signal_source:'信号类 · 身份未验证',unverified_signal:'信号类 · 身份未验证'})[key]||value||'一般网页'}
function operationKindName(value){return({model:'模型推理',search:'网页搜索',fetch:'页面读取'})[value]||value||'系统操作'}
function runtimeTypeName(value){
  const text=String(value||'').trim();
  return ({
    ResearchQuestion:'用户研究问题',
    ResearchPlan:'结构化研究计划',
    QueryBatch:'检索路线集合',
    SearchQueries:'检索路线集合',
    SourcePages:'已读取文章集合',
    EvidenceLedger:'证据账本',
    ClosureReport:'逐目标证据检查报告',
    DraftAnswer:'带引用待核对回答',
    EvidenceLimitedMaterial:'已保存材料与附件观察',
    BoundedCitedAnswer:'当前可交付回答',
    DeliveryBoundaryCheck:'回答交付边界检查',
    VerificationReport:'逐句引用核验报告',
    ResearchState:'完整研究状态',
    CanonicalStageArtifact:'可核查的最终归档产物'
  })[text]||text;
}
function runtimeSummary(value,operation='',direction=''){
  const text=String(value||'').trim();
  if(!text)return'';
  const exact={
    ResearchPlan:'结构化研究计划',
    ClosureReport:'逐目标证据检查报告',
    VerificationReport:'逐句引用核验报告',
    'Projected durable finalize stage':'最终回答、检查结论与已保存产物清单已归档',
    'Replayed persisted result; provider was not called':'已复用持久化结果，本次没有再次调用能力接口',
    '当前可交付回答已由已保存证据、附件观察和明确边界组成；未使用未保存材料。':'已用保存材料组织当前可交付回答',
    '已检查交付边界：当前回答可以展示，但仍不能标记为完整核验通过。':'已检查回答边界，仍待补齐完整核验'
  };
  if(exact[text])return exact[text];
  let match=text.match(/^(\d+) evidence gaps?; (\d+) prior queries$/i);
  if(match)return`${match[1]} 个待补证缺口，已有 ${match[2]} 条检索路线`;
  match=text.match(/^(\d+) fetched source pages?$/i);
  if(match)return`已成功读取 ${match[1]} 篇候选文章`;
  match=text.match(/^(\d+) ledger entries?$/i);
  if(match)return`证据账本中有 ${match[1]} 条候选证据`;
  match=text.match(/^(\d+) closure-admitted supporting evidence entries?$/i);
  if(match)return`${match[1]} 条通过交付前检查的支持材料`;
  match=text.match(/^(\d+) answer characters?; (\d+) closure-admitted evidence entries?$/i);
  if(match)return`待核对回答 ${match[1]} 字符，引用 ${match[2]} 条通过交付前检查的材料`;
  match=text.match(/^(\d+) characters?$/i);
  if(match)return`形成 ${match[1]} 字符的带引用回答`;
  match=text.match(/^(\d+) items?$/i);
  if(match&&operation==='generate_queries'&&direction==='output')return`生成 ${match[1]} 条缺口驱动检索路线`;
  if(match&&operation==='extract_evidence'&&direction==='output')return`抽取 ${match[1]} 条可定位候选证据`;
  if(match)return`${match[1]} 项结构化记录`;
  if(/^operation [0-9a-f]+ already succeeded$/i.test(text))return'同一操作此前已完成，准备复用持久化结果';
  return text;
}
function humanizeAuditText(value) {
  return String(value ?? '')
    .replace(/\bclosure-admitted\b/gi, '通过交付前检查的')
    .replace(/\bEvidence\.fetch_record_id\b/gi, '证据对应的页面读取记录 ID（技术字段）')
    .replace(/\bfetch_record_id\b/gi, '页面读取记录 ID（技术字段）')
    .replace(/\bresume_receipt_id\b/gi, '恢复确认编号（技术字段）')
    .replace(/\breceipt_id\b/gi, '接收确认编号（技术字段）')
    .replace(/\bsource_id\b/gi, '文章编号（技术字段）')
    .replace(/\binvocation_id\b/gi, '角色执行记录 ID（技术字段）')
    .replace(/\boperation_key\b/gi, '操作编号（技术字段）')
    .replace(/\bbinding_status\b/gi, '对应关系状态（技术字段）')
    .replace(/\bvalidation_status\b/gi, '核对状态（技术字段）')
    .replace(/\bmessage_id\b/gi, '交接编号（技术字段）')
    .replace(/\bcontent_hash_scope\b/gi, '正文校验值覆盖范围')
    .replace(/\bcontent_hash\b/gi, '正文校验值')
    .replace(/\bmetadata_hash\b/gi, '元数据校验值')
    .replace(/\bsnapshot_sha256\b/gi, '保存快照校验值')
    .replace(/\bslot_id\b/gi, '问题编号（技术字段）')
    .replace(/\bFetch invocation \/ operation\b/gi, '页面读取记录 / 操作编号')
    .replace(/\bFetch invocation\b/gi, '页面读取对应的角色执行记录')
    .replace(/\bResult invocation\b/gi, '结果对应的角色执行记录')
    .replace(/\bOperation key\b/gi, '操作编号')
    .replace(/\bOperation \/ provider\b/gi, '操作编号 / 模型服务')
    .replace(/\bProvider \/ execution\b/gi, '模型服务 / 执行方式')
    .replace(/\bFetch mode\b/gi, '页面读取方式')
    .replace(/\bBinding status\b/gi, '对应关系状态')
    .replace(/\bSource ID\b/gi, '文章编号')
    .replace(/\bEvidence\b/gi, '证据')
    .replace(/\bdurable manifests?\b/gi, '已保存产物清单')
    .replace(/\bdurable handoffs?\b/gi, '已保存的任务交接记录')
    .replace(/\bdurable receipts?\b/gi, '已保存的接收确认记录')
    .replace(/\bhard gates?\b/gi, '交付前检查')
    .replace(/\bquality gates?\b/gi, '阶段检查')
    .replace(/\breceipt-backed\b/gi, '带接收确认的')
    .replace(/\binvocations?\b/gi, '角色执行记录')
    .replace(/\bhandoffs?\b/gi, '任务交接')
    .replace(/\breceipts?\b/gi, '接收确认')
    .replace(/\bmanifests?\b/gi, '保存清单')
    .replace(/\bdurable\b/gi, '已保存的')
    .replace(/\bfetch(?:es|ed|ing)?\b/gi, '页面读取')
    .replace(/\bprovider\b/gi, '模型服务')
    .replace(/\bbinding\b/gi, '对应关系')
    .replace(/\bprovenance\b/gi, '来源路径')
    .replace(/\bimmutable\b/gi, '不可变')
    .replace(/\bcanonical\b/gi, '规范化')
    .replace(/claim[- ]quote/gi, '声明与原文')
    .replace(/quality_gate/gi, '阶段检查')
    .replace(/\bcontent hash\b/gi, '正文校验值')
    .replace(/\bhash\b/gi, '校验值')
    .replace(/\bintegrity\b/gi, '完整性检查')
    .replace(/\boperation\b/gi, '操作')
    .replace(/\bresult\b/gi, '结果')
    .replace(/\bArticle\b/gi, '文章')
    .replace(/\bsnapshot\b/gi, '保存快照')
    .replace(/\bproof\b/gi, '核查依据')
    .replace(/\bfence\b/gi, '执行权编号')
    .replace(/\bworker\b/gi, '执行器')
    .replace(/\blease\b/gi, '执行占用')
    .replace(/\bclaim\b/gi, '接管记录')
    .replace(/\btransition\b/gi, '状态变化')
    .replace(/\bclosure\b/gi, '交付材料检查')
    .replace(/闭包/g, '纳入回答材料')
    .replace(/可证明交接/g, '可核对的任务交接')
    .replace(/可证明/g, '可核对')
    .replace(/硬门/g, '交付前检查')
    .replace(/阻断/g, '需要补材料')
    .replace(/同信封强校验/g, '同一条交接记录的三项检查')
    .replace(/同信封闭环/g, '同一条交接记录的信息齐全');
}
function humanizeVisibleCopy(root) {
  if (!root || typeof document === 'undefined') return;
  const walker = document.createTreeWalker(root, globalThis.NodeFilter?.SHOW_TEXT || 4);
  const nodes = [];
  let current = walker.nextNode();
  while (current) {
    if (!current.parentElement?.closest('code,.audit-mono,[data-raw]')) nodes.push(current);
    current = walker.nextNode();
  }
  nodes.forEach(node => { node.nodeValue = humanizeAuditText(node.nodeValue); });
}
function providerCallCount(item){if(item?.provider_call_count===undefined||item?.provider_call_count===null)return null;const value=Number(item.provider_call_count);return Number.isFinite(value)&&value>=0?value:null}
function providerName(value){return({MockModelProvider:'离线 Mock',ReplaySearchProvider:'离线回放检索',DeepSeekModelProvider:'DeepSeek',OpenAICompatibleModelProvider:'OpenAI 兼容网关',DuckDuckGoSearchProvider:'DuckDuckGo',qwen:'Qwen',gpt:'GPT',deepseek:'DeepSeek',openai_compatible:'OpenAI 兼容网关',duckduckgo:'DuckDuckGo'})[value]||String(value||'未记录').replace(/Provider$/,'')}
function nextAction(status,latest,gap){if(status==='completed')return'可自主核查回答和证据';if(status==='recovery_unverified')return'先查看恢复记录，核对接收确认、执行器记录和恢复编号；不要把最终状态当作已确认完成';if(status==='failed'||status==='verification_failed'||status==='evidence_incomplete')return'查看材料缺口并决定是否继续补充';if(status==='cancelled')return'查看已保存的中间产物';if(gap)return`补充：${gapName(gap.type)}`;return latest?`等待${agentContracts[latest.agent_id]?.name||latest.role}完成${operationName(latest.operation)}`:'等待规划智能体启动'}
function formatTimestamp(value){if(!value)return'时间未记录';const date=new Date(value);return Number.isNaN(date.getTime())?'时间未记录':date.toLocaleString()}
function formatBytes(value){const bytes=finiteValue(value);if(bytes===null)return'大小未记录';if(bytes<0)return'大小字段无效';if(bytes<1024)return`${bytes} B`;if(bytes<1024*1024)return`${(bytes/1024).toFixed(1)} KB`;return`${(bytes/1024/1024).toFixed(2)} MB`}
function sourceDomain(url){try{return new URL(url).hostname.replace(/^www\./,'')}catch(_){return'unknown'}}
function truncate(value,length){const text=String(value||'');return text.length>length?`${text.slice(0,length-1)}…`:text}
function auditTextPreview(value,length=180){const text=String(value||'历史字段未记录').trim()||'历史字段未记录';return text.length>length?`${truncate(text,length)}（完整说明见逐目标检查记录）`:text}
function escapeHTML(value){return String(value??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]))}

function setSectionNavCurrent(sectionId){
  const section=$(sectionId),current=$('sectionNavCurrent');
  if(!section||!current||section.classList.contains('hidden'))return false;
  current.textContent=section.dataset.section||'研究进度';
  document.querySelectorAll('[data-nav-target]').forEach(link=>{
    const selected=link.dataset.navTarget===sectionId;
    link.classList.toggle('active',selected);
    if(selected) link.setAttribute('aria-current','location');
    else link.removeAttribute('aria-current');
  });
  return true;
}

function syncSectionNavVisibility(){
  const links=[...document.querySelectorAll('[data-nav-target]')];
  const visible=[];
  links.forEach(link=>{
    const section=$(link.dataset.navTarget);
    const hidden=!section||section.classList.contains('hidden')||Boolean(section.closest('details.run-disclosure:not([open])'));
    link.classList.toggle('hidden',hidden);
    link.setAttribute('aria-hidden',String(hidden));
    link.tabIndex=hidden?-1:0;
    if(hidden){
      link.classList.remove('active');
      link.removeAttribute('aria-current');
    }else visible.push(link);
  });
  const nav=$('sectionNav');
  nav?.classList.toggle('has-visible-links',visible.length>0);
  if(visible.length&&!visible.some(link=>link.classList.contains('active'))){
    const preferred=document.body.classList.contains('result-first')?'resultOverview':'researchPulse';
    setSectionNavCurrent(visible.some(link=>link.dataset.navTarget===preferred)?preferred:visible[0].dataset.navTarget);
  }
}

function initSectionNav(){
  const sections=[...document.querySelectorAll('[data-section]')];
  const nav=$('sectionNav'),links=$('sectionNavLinks'),toggle=$('sectionNavToggle'),current=$('sectionNavCurrent');
  links.innerHTML=sections.map(section=>`<a href="#${section.id}" data-nav-target="${section.id}"><i></i><span>${escapeHTML(section.dataset.section)}</span></a>`).join('');
  const setOpen=open=>{nav.classList.toggle('is-open',open);toggle.setAttribute('aria-expanded',String(open))};
  toggle.addEventListener('click',()=>setOpen(toggle.getAttribute('aria-expanded')!=='true'));
  links.addEventListener('click',event=>{
    const link=event.target.closest('a[data-nav-target]');
    if(!link||link.classList.contains('hidden'))return;
    event.preventDefault();
    setOpen(false);
    const target=$(link.dataset.navTarget);
    if(!target||target.classList.contains('hidden')){
      syncSectionNavVisibility();
      announceLive('该章节当前尚未产生结果，导航入口已保持隐藏。','section-nav:hidden-target',true);
      return;
    }
    setSectionNavCurrent(target.id);
    revealAndFocus(target,`${target.dataset.section||'研究区域'}已展开并定位。`,'start');
  });
  document.addEventListener('click',event=>{if(!nav.contains(event.target))setOpen(false)});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&nav.classList.contains('is-open')){setOpen(false);toggle.focus()}});
  const observer=new IntersectionObserver(entries=>{
    const visible=entries.filter(entry=>entry.isIntersecting).sort((a,b)=>b.intersectionRatio-a.intersectionRatio)[0];
    if(!visible)return;
    setSectionNavCurrent(visible.target.id);
  },{rootMargin:'-20% 0px -65% 0px',threshold:[0,.25,.5]});
  sections.forEach(section=>observer.observe(section));
  syncSectionNavVisibility();
  setSectionNavCurrent('researchPulse');
}

function scrollToSlotAudit(slotId, label = '交付前检查') {
  if (!slotId) return false;
  const target=[...document.querySelectorAll('[data-slot-audit]')].find(item=>item.dataset.slotAudit===String(slotId));
  if (!target) return false;
  return revealAndFocus(target,`${label}已展开并定位到 slot_id ${slotId} 的逐目标审计。`);
}

function scrollToResearchTarget(targetId, label = '') {
  let target = $(targetId);
  if (!target || target.classList.contains('hidden')) {
    target = targetId === 'answerSection' ? $('metricConsole') : $('researchPulse');
  }
  return revealAndFocus(target,`${label || target?.dataset?.section || '研究区域'}已展开并定位。`,'start');
}

function setMetricDetails(expanded) {
  metricDetailsExpanded = Boolean(expanded);
  $('metricsGrid').classList.toggle('details-expanded', metricDetailsExpanded);
  $('metricDetailToggle').setAttribute('aria-expanded', String(metricDetailsExpanded));
  $('metricDetailToggle').textContent = metricDetailsExpanded ? '收起计算依据' : '展开五项计算依据';
  try {
    window.localStorage.setItem('fieldnote.metricDetails', metricDetailsExpanded ? 'expanded' : 'summary');
  } catch (_) {
    // The view still works when storage is unavailable.
  }
}

$('stopButton').addEventListener('click',()=>{if(!stopRequestPending&&!$('stopDialog').open)$('stopDialog').showModal()});
$('stopCancel').addEventListener('click',()=>$('stopDialog').close());
$('stopConfirm').addEventListener('click',async()=>{
  if(stopRequestPending)return;
  stopRequestPending=true;
  $('stopConfirm').disabled=true;
  $('stopCancel').disabled=true;
  $('stopButton').disabled=true;
  try{
    $('stopDialog').close();
    renderStatus('cancelling');
    await getJSON(`/api/runs/${encodeURIComponent(runId)}`,{method:'DELETE'});
  }catch(error){
    stopRequestPending=false;
    renderStatus(window.__latestState?.status||'failed',error.message);
  }finally{
    $('stopConfirm').disabled=false;
    $('stopCancel').disabled=false;
  }
});
 $('resumeButton').addEventListener('click',()=>{
  if (window.__latestRecoveryAudit?.status === 'conflict' || effectiveRunStatus({state:window.__latestState}) === 'recovery_unverified') {
    openCurrentRecoveryAudit();
    return;
  }
  resumeRun().catch(error=>renderStatus('failed',error.message));
 });
$('methodButton').addEventListener('click',()=>showMethodology().catch(showMethodologyFailure));
$('methodClose').addEventListener('click',()=>$('methodDialog').close());
$('snapshotClose').addEventListener('click',()=>$('snapshotDialog').close());
$('auditClose').addEventListener('click',()=>$('auditDialog').close());
$('auditBack').addEventListener('click',restorePreviousAuditFrame);
$('auditDialog').addEventListener('close',()=>resetAuditNavigation(true));
$('jumpToAnswer').addEventListener('click',()=>scrollToResearchTarget('answerSection','完整回答与逐句核验'));
$('overviewTimelineJump').addEventListener('click',()=>scrollToResearchTarget('auditTimelineSection','真实主时间线'));
$('graphZoomOut').addEventListener('click',()=>{graphFitMode=false;graphZoom=Math.max(.9,graphZoom-.1);applyGraphView()});
$('graphZoomIn').addEventListener('click',()=>{graphFitMode=false;graphZoom=Math.min(1.75,graphZoom+.15);applyGraphView()});
$('graphFit').addEventListener('click',()=>{graphFitMode=true;graphZoom=1;applyGraphView();document.querySelector('.graph-shell').scrollTo({left:0,behavior:reducedMotion.matches?'auto':'smooth'})});
$('graphClearFocus').addEventListener('click',()=>{graphFocusedNode=null;graphFocusedLabel='';$('researchGraph').querySelectorAll('.graph-node.selected').forEach(node=>{node.classList.remove('selected');node.setAttribute('aria-pressed','false')});applyGraphView()});
document.querySelectorAll('[data-graph-filter]').forEach(button=>button.addEventListener('click',()=>{graphFocusedNode=null;graphFocusedLabel='';graphFilter=button.dataset.graphFilter;document.querySelectorAll('[data-graph-filter]').forEach(item=>{const selected=item===button;item.classList.toggle('active',selected);item.setAttribute('aria-pressed',String(selected))});applyGraphView()}));
document.querySelectorAll('.agent-node').forEach(node=>node.addEventListener('click',()=>{renderAgentContract(node.dataset.agent,window.__latestState?.agent_invocations||[]);showAgentAudit(node.dataset.agent,window.__latestState?.agent_invocations||[],window.__latestEvents||[],window.__latestAudit||null)}));
document.querySelectorAll('[data-phase-agent]').forEach(button=>button.addEventListener('click',()=>showAgentAudit(button.dataset.phaseAgent,window.__latestState?.agent_invocations||[],window.__latestEvents||[],window.__latestAudit||null)));
$('phaseFocus').addEventListener('click',()=>{const agent=$('phaseFocus').dataset.agent||'planner';if(agent==='perception'){revealAndFocus($('inputPerceptionSection'),'已打开输入材料记录。');$('attachmentAuditGrid').querySelector('summary')?.focus({preventScroll:true});return}revealAndFocus($('orchestrationSection'),'已定位到智能体总控。');renderAgentContract(agent,window.__latestState?.agent_invocations||[]);document.querySelector(`.agent-node[data-agent="${agent}"]`)?.focus({preventScroll:true})});
$('phaseAuditJump').addEventListener('click',()=>scrollToResearchTarget('auditTimelineSection','真实主时间线'));
$('pulseAuditButton').addEventListener('click',()=>{const item=window.__pulseInvocation;if(item)showInvocationAudit(item,window.__latestState?.agent_invocations||[],window.__latestEvents||[],window.__latestAudit||null)});
$('gateAuditJump').addEventListener('click',()=>{const gates=gateConsoleModel(window.__latestState||{});const target=gates.find(item=>item.tone!=='passed'&&item.targetSlotId)||gates.find(item=>item.targetSlotId);if(!scrollToSlotAudit(target?.targetSlotId,'当前交付前检查'))scrollToResearchTarget('methodSection','逐目标检查记录')});
$('metricDetailToggle').addEventListener('click',()=>setMetricDetails(!metricDetailsExpanded));
document.querySelectorAll('[data-command-target]').forEach(button=>button.addEventListener('click',()=>scrollToResearchTarget(button.dataset.commandTarget,button.textContent.trim())));
document.querySelectorAll('.metric-help').forEach(button=>button.addEventListener('click',showMethodology));
document.querySelectorAll('details.run-disclosure').forEach(disclosure=>disclosure.addEventListener('toggle',()=>{
  syncSectionNavVisibility();
  if (disclosure.open) setSectionNavCurrent(disclosure.querySelector('[data-section]')?.id || 'resultOverview');
}));
document.querySelectorAll('[data-ledger-filter]').forEach(button=>button.addEventListener('click',()=>{
  ledgerFilter=button.dataset.ledgerFilter;
  document.querySelectorAll('[data-ledger-filter]').forEach(item=>{const selected=item===button;item.classList.toggle('active',selected);item.setAttribute('aria-pressed',String(selected))});
  renderUnifiedAuditTimeline(window.__latestState||{},window.__latestEvents||[]);
}));
[$('methodDialog'),$('snapshotDialog'),$('auditDialog'),$('stopDialog')].forEach(dialog=>dialog.addEventListener('click',event=>{if(event.target===dialog&&!stopRequestPending)dialog.close()}));
$('networkToggle').addEventListener('click',()=>{const expanded=$('networkToggle').getAttribute('aria-expanded')==='true';$('networkToggle').setAttribute('aria-expanded',String(!expanded));$('agentNetwork').classList.toggle('mobile-collapsed',expanded);$('networkToggle').textContent=expanded?'展开六智能体关系图':'收起六智能体关系图';if(!expanded&&$('agentNetwork').dataset.positionInitialized!=='true')requestAnimationFrame(()=>centerAgentNetwork(false))});
$('networkCenter').addEventListener('click',()=>centerAgentNetwork(true));
$('agentNetwork').addEventListener('scroll',()=>{
  if (!networkProgrammaticScroll) $('agentNetwork').dataset.positionInitialized = 'true';
},{passive:true});
document.querySelectorAll('.agent-edge-group').forEach(group=>{const activate=()=>showNetworkEdgeAudit(group.dataset.from,group.dataset.to,window.__latestState?.agent_invocations||[],window.__latestEvents||[]);group.addEventListener('click',activate);group.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();activate()}})});
const repairEdgeGroup=$('agentRepairEdgeGroup');
if(repairEdgeGroup){
  const activateRepairEdge=()=>{
    const {from,to}=repairEdgeGroup.dataset;
    if(!from||!to||(repairEdgeGroup.getAttribute('aria-hidden') === 'true'))return;
    showNetworkEdgeAudit(from,to,window.__latestState?.agent_invocations||[],window.__latestEvents||[]);
  };
  repairEdgeGroup.addEventListener('click',activateRepairEdge);
  repairEdgeGroup.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();activateRepairEdge()}});
}
setMetricDetails(metricDetailsExpanded);
initSectionNav();
loadConfig().catch(()=>{});
loadSystemContract().catch(markSystemContractFallback);
startLiveUpdates();
