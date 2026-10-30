'use strict';
const $ = id => document.getElementById(id);
let token = '', rules = [], selected = null, cameras = [], settings = {}, revisions = [];
let language = /^zh\b/i.test(navigator.language) ? 'zh' : 'en', notice = null, lastHistory = null, lastStatus = null;
const base = new URL('./', window.location.href);
const t = (key, values) => MonitorI18n.t(language, key, values);
const date = at => new Date(at * 1000).toLocaleString(language === 'zh' ? 'zh-CN' : 'en');
function message(key, error = false) { notice = {key, error}; const text = t(key); $('message').textContent = error && text === key ? t('requestError', {detail: key}) : text; $('message').classList.toggle('error', error); }
function editorText() {
  $('editor-title').textContent = selected ? selected.label : t($('editor').hidden ? 'selectRule' : 'newRuleTitle');
  $('revision').textContent = selected ? t('currentRevision', {revision: selected.revision}) : $('editor').hidden ? '' : t('unsaved');
}
function applyLanguage() {
  document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en'; document.title = t('title'); $('language').value = language;
  for (const node of document.querySelectorAll('[data-i18n]')) node.textContent = t(node.dataset.i18n);
  for (const node of document.querySelectorAll('[data-i18n-placeholder]')) node.placeholder = t(node.dataset.i18nPlaceholder);
  for (const node of document.querySelectorAll('[data-i18n-aria-label]')) node.setAttribute('aria-label', t(node.dataset.i18nAriaLabel));
  if (notice) message(notice.key, notice.error);
  editorText(); renderRules(); renderCameras(); renderVersions(); renderHistory(); renderMetrics();
}
async function api(path, method = 'GET', data, revision) {
  const headers = {'X-Monitoring-Request': '1'};
  if (token) headers.Authorization = 'Bearer ' + token;
  if (revision !== undefined) { headers['If-Match'] = '"' + revision + '"'; data = {...(data || {}), expected_revision: revision}; }
  if (data !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(new URL(path, base), {method, headers, body: data === undefined ? undefined : JSON.stringify(data)});
  const value = await response.json();
  if (!response.ok) { if (response.status === 401) $('auth').hidden = false; throw Error(value.code === 'revision_conflict' ? 'revisionConflict' : value.code || 'requestFailed'); }
  return value;
}
const run = fn => async event => { try { await fn(event); } catch (e) { message(e.message, true); } };
function option(value, text) { const node = document.createElement('option'); node.value = value; node.textContent = text; return node; }
function renderRules() {
  $('rules').replaceChildren();
  for (const rule of rules) {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'rule' + (selected?.id === rule.id ? ' selected' : '');
    const title = document.createElement('strong'); title.textContent = rule.label;
    const sub = document.createElement('small'); sub.textContent = t('ruleSubtitle', {status: t(rule.archived ? 'archived' : rule.enabled ? 'active' : 'disabled'), revision: rule.revision, mode: t(rule.delivery_enabled ? 'deliveryOn' : 'localMode')});
    button.append(title, sub); button.addEventListener('click', run(() => select(rule))); $('rules').append(button);
  }
}
function loadEditor(rule) {
  $('editor').hidden = false; editorText();
  $('identity').value = rule?.id || ''; $('identity').disabled = !!rule;
  $('label').value = rule?.label || ''; $('enabled').checked = rule?.enabled || false; $('delivery').checked = rule?.delivery_enabled || false;
  $('delivery').disabled = !settings.cma_configured;
  $('policy').value = rule ? JSON.stringify(rule.pipeline, null, 2) : '';
  $('archive').hidden = !rule || rule.archived; $('duplicate').hidden = !rule;
  $('save').disabled = !!rule?.archived; $('history').hidden = !rule;
  const p = rule?.pipeline;
  if (p) {
    $('camera').value = p.source.subject; updateZones();
    $('crop').value = p.evidence.crop_zone;
    $('duration').value = p.activity.minimum_seconds; $('merge').value = p.window.merge_seconds;
    $('settle').value = p.window.settle_seconds; $('cooldown').value = p.limits.cooldown_seconds;
    $('daily').value = p.limits.max_per_day; $('timezone').value = p.limits.timezone; $('evidence').checked = p.evidence.adapter === 'frigate';
  }
}
async function select(rule) {
  selected = rule; renderRules(); loadEditor(rule);
  revisions = (await api(`api/rules/${rule.id}/revisions`)).revisions;
  renderVersions();
  await showHistory(rule.revision);
}
function renderVersions() {
  const current = $('history-version').value;
  $('history-version').replaceChildren(...revisions.map(r => option(r.revision, `v${r.revision} · ${date(r.created_at)}`)));
  if (revisions.some(r => String(r.revision) === current)) $('history-version').value = current;
}
async function showHistory(revision) {
  const id = selected.id;
  const [state, history] = await Promise.all([api(`api/rules/${id}/revisions/${revision}/status`), api(`api/rules/${id}/revisions/${revision}/decisions`)]);
  if (selected?.id !== id) return;
  $('history-version').value = revision; lastHistory = {state, history}; renderHistory();
}
function renderHistory() {
  if (!lastHistory) return;
  const {state, history} = lastHistory;
  $('history-state').textContent = t('historySummary', {processing: state.runtime_error ? t('runtimeError', {type: state.runtime_error.type}) : t(state.processing ? 'processing' : 'paused'), decisions: state.status.decisions, delivered: state.status.cma_deliveries.delivered, pending: state.status.cma_deliveries.pending});
  $('decisions').replaceChildren();
  if (!history.decisions.length) $('decisions').textContent = t('noDecisions');
  for (const row of history.decisions) {
    const details = document.createElement('details'); details.className = 'entry';
    const title = document.createElement('summary'); title.textContent = `${date(row.at)} · ${row.candidate.subject} · ${MonitorI18n.analysis(language, row.analysis)}`;
    const pre = document.createElement('pre'); pre.textContent = JSON.stringify(row, null, 2); details.append(title, pre); $('decisions').append(details);
  }
}
function renderMetrics() {
  if (!lastStatus) return;
  $('summary').replaceChildren();
  for (const [label, value] of [['metricEnabled', lastStatus.enabled_rules], ['metricDecisions', lastStatus.decisions], ['metricDelivered', lastStatus.cma_deliveries.delivered], ['metricPending', lastStatus.cma_deliveries.pending]]) {
    const div = document.createElement('div'); div.className = 'metric'; const count = document.createElement('b'); count.textContent = value; div.append(count, document.createTextNode(t(label))); $('summary').append(div);
  }
}
function renderCameras() {
  const camera = $('camera').value, zone = $('zone').value, crop = $('crop').value;
  $('camera').replaceChildren(option('', t('chooseCamera')), ...cameras.map(c => option(c.name, c.name + (c.recording ? '' : t('notRecording')))));
  $('camera').value = camera; updateZones(); $('zone').value = zone; $('crop').value = crop;
}
async function refresh() {
  const [meta, listing, status] = await Promise.all([api('api/management'), api('api/rules'), api('api/status')]);
  settings = meta; rules = listing.rules; lastStatus = status; $('auth').hidden = true; renderMetrics();
  renderRules();
  try { cameras = (await api('api/cameras')).cameras; } catch (e) { cameras = []; message('cameraUnavailable', true); }
  renderCameras();
  if (selected) await select(rules.find(r => r.id === selected.id)); else if (rules.length) await select(rules[0]);
}
function updateZones() {
  const camera = cameras.find(c => c.name === $('camera').value);
  for (const id of ['zone', 'crop']) $(id).replaceChildren(option('', t(id === 'zone' ? 'anyZone' : 'fullFrame')), ...(camera?.zones || []).map(z => option(z, z)));
}
$('language').addEventListener('change', () => { language = $('language').value; applyLanguage(); });
$('camera').addEventListener('change', updateZones);
$('connect').addEventListener('click', run(async () => { token = $('token').value; $('token').value = ''; await refresh(); message('connected'); }));
$('refresh').addEventListener('click', run(refresh));
$('new').addEventListener('click', () => { selected = null; renderRules(); loadEditor(null); });
$('duplicate').addEventListener('click', () => { const policy = $('policy').value, label = $('label').value; selected = null; renderRules(); loadEditor(null); $('policy').value = policy; $('label').value = label + ' · ' + t('copySuffix'); });
$('generate').addEventListener('click', run(() => {
  const camera = $('camera').value; if (!camera) throw Error('chooseCameraFirst');
  if ($('policy').value.trim() && !window.confirm(t('generateConfirm'))) return;
  const filters = []; const labels = $('labels').value.split(',').map(s => s.trim()).filter(Boolean), zone = $('zone').value;
  if (labels.length) filters.push({field: 'labels', op: 'contains_any', value: labels});
  if (zone) filters.push({field: 'zones', op: 'contains_any', value: [zone]});
  const match = filters.length > 1 ? {all: filters} : filters[0] || {};
  const policy = {version: 1, name: selected?.pipeline.name || $('identity').value || 'camera-activity', source: {adapter: 'frigate_reviews', source: settings.source, subject: camera},
    window: {merge_seconds: Number($('merge').value), settle_seconds: Number($('settle').value), maximum_seconds: 14400},
    activity: {name: 'activity', match, minimum_seconds: Number($('duration').value)},
    limits: {cooldown_seconds: Number($('cooldown').value), max_per_day: Number($('daily').value), timezone: $('timezone').value},
    evidence: $('evidence').checked ? {adapter: 'frigate', crop_zone: $('crop').value, labels} : {adapter: 'none'}};
  $('policy').value = JSON.stringify(policy, null, 2); message('generated');
}));
$('editor').addEventListener('submit', run(async event => {
  event.preventDefault();
  let policy; try { policy = JSON.parse($('policy').value); } catch { throw Error('invalidJson'); }
  const value = {label: $('label').value, pipeline: policy, enabled: $('enabled').checked, delivery_enabled: $('delivery').checked};
  if (selected) await api('api/rules/' + selected.id, 'PUT', value, selected.revision);
  else { value.id = $('identity').value; selected = await api('api/rules', 'POST', value); }
  await refresh(); message('saved');
}));
$('archive').addEventListener('click', run(async () => {
  if (!window.confirm(t('archiveConfirm'))) return;
  await api('api/rules/' + selected.id, 'DELETE', undefined, selected.revision); await refresh(); message('archiveDone');
}));
$('history-version').addEventListener('change', run(() => showHistory(Number($('history-version').value))));
applyLanguage(); run(refresh)();
