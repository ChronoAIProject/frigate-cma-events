'use strict';
const $ = id => document.getElementById(id);
let token = '', rules = [], selected = null, cameras = [], settings = {}, revisions = [];
const base = new URL('./', window.location.href);
function message(text, error = false) { $('message').textContent = text; $('message').classList.toggle('error', error); }
async function api(path, method = 'GET', data, revision) {
  const headers = {'X-Monitoring-Request': '1'};
  if (token) headers.Authorization = 'Bearer ' + token;
  if (revision !== undefined) { headers['If-Match'] = '"' + revision + '"'; data = {...(data || {}), expected_revision: revision}; }
  if (data !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(new URL(path, base), {method, headers, body: data === undefined ? undefined : JSON.stringify(data)});
  const value = await response.json();
  if (!response.ok) { if (response.status === 401) $('auth').hidden = false; throw Error(value.code === 'revision_conflict' ? '规则已被其他页面修改，请刷新后重试。' : value.code || '请求失败'); }
  return value;
}
const run = fn => async event => { try { await fn(event); } catch (e) { message(e.message, true); } };
function option(value, text) { const node = document.createElement('option'); node.value = value; node.textContent = text; return node; }
function renderRules() {
  $('rules').replaceChildren();
  for (const rule of rules) {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'rule' + (selected?.id === rule.id ? ' selected' : '');
    const title = document.createElement('strong'); title.textContent = rule.label;
    const sub = document.createElement('small'); sub.textContent = `${rule.archived ? '已归档' : rule.enabled ? '已启用' : '已停用'} · v${rule.revision} · ${rule.delivery_enabled ? 'CMA 推送' : '本地观察'}`;
    button.append(title, sub); button.addEventListener('click', run(() => select(rule))); $('rules').append(button);
  }
}
function loadEditor(rule) {
  $('editor').hidden = false; $('editor-title').textContent = rule ? rule.label : '新规则';
  $('identity').value = rule?.id || ''; $('identity').disabled = !!rule;
  $('label').value = rule?.label || ''; $('enabled').checked = rule?.enabled || false; $('delivery').checked = rule?.delivery_enabled || false;
  $('delivery').disabled = !settings.cma_configured;
  $('policy').value = rule ? JSON.stringify(rule.pipeline, null, 2) : '';
  $('revision').textContent = rule ? `当前 v${rule.revision}` : '尚未保存';
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
  $('history-version').replaceChildren(...revisions.map(r => option(r.revision, `v${r.revision} · ${new Date(r.created_at * 1000).toLocaleString()}`)));
  await showHistory(rule.revision);
}
async function showHistory(revision) {
  const id = selected.id;
  const [state, history] = await Promise.all([api(`api/rules/${id}/revisions/${revision}/status`), api(`api/rules/${id}/revisions/${revision}/decisions`)]);
  $('history-state').textContent = `${state.runtime_error ? '处理异常：' + state.runtime_error.type : state.processing ? '正在处理' : '处理已暂停'} · ${state.status.decisions} 条判定 · CMA 已送达 ${state.status.cma_deliveries.delivered} / 待处理 ${state.status.cma_deliveries.pending}`;
  $('decisions').replaceChildren();
  if (!history.decisions.length) $('decisions').textContent = '此版本还没有事件判定。';
  for (const row of history.decisions) {
    const details = document.createElement('details'); details.className = 'entry';
    const title = document.createElement('summary'); title.textContent = `${new Date(row.at * 1000).toLocaleString()} · ${row.candidate.subject} · ${row.candidate.decision} · ${row.result?.status || '暂无分析结果'}`;
    const pre = document.createElement('pre'); pre.textContent = JSON.stringify(row, null, 2); details.append(title, pre); $('decisions').append(details);
  }
}
async function refresh() {
  const [meta, listing, status] = await Promise.all([api('api/management'), api('api/rules'), api('api/status')]);
  settings = meta; rules = listing.rules; $('auth').hidden = true; $('summary').replaceChildren();
  for (const [label, value] of [['启用规则', status.enabled_rules], ['历史判定', status.decisions], ['CMA 已送达', status.cma_deliveries.delivered], ['待处理 / 暂停', status.cma_deliveries.pending]]) {
    const div = document.createElement('div'); div.className = 'metric'; const count = document.createElement('b'); count.textContent = value; div.append(count, document.createTextNode(label)); $('summary').append(div);
  }
  renderRules();
  try { cameras = (await api('api/cameras')).cameras; } catch (e) { cameras = []; message('摄像头列表暂不可用，仍可通过 JSON 配置规则。', true); }
  $('camera').replaceChildren(option('', '请选择'), ...cameras.map(c => option(c.name, c.name + (c.recording ? '' : '（未启用录像）'))));
  if (selected) await select(rules.find(r => r.id === selected.id)); else if (rules.length) await select(rules[0]);
}
function updateZones() {
  const camera = cameras.find(c => c.name === $('camera').value);
  for (const id of ['zone', 'crop']) $(id).replaceChildren(option('', id === 'zone' ? '任意区域' : '完整画面'), ...(camera?.zones || []).map(z => option(z, z)));
}
$('camera').addEventListener('change', updateZones);
$('connect').addEventListener('click', run(async () => { token = $('token').value; $('token').value = ''; await refresh(); message('已连接。'); }));
$('refresh').addEventListener('click', run(refresh));
$('new').addEventListener('click', () => { selected = null; renderRules(); loadEditor(null); });
$('duplicate').addEventListener('click', () => { const policy = $('policy').value, label = $('label').value; selected = null; renderRules(); loadEditor(null); $('policy').value = policy; $('label').value = label + ' · 副本'; });
$('generate').addEventListener('click', run(() => {
  const camera = $('camera').value; if (!camera) throw Error('请先选择摄像头。');
  if ($('policy').value.trim() && !window.confirm('生成策略将替换下方 JSON，包括额外条件。继续？')) return;
  const filters = []; const labels = $('labels').value.split(',').map(s => s.trim()).filter(Boolean), zone = $('zone').value;
  if (labels.length) filters.push({field: 'labels', op: 'contains_any', value: labels});
  if (zone) filters.push({field: 'zones', op: 'contains_any', value: [zone]});
  const match = filters.length > 1 ? {all: filters} : filters[0] || {};
  const policy = {version: 1, name: selected?.pipeline.name || $('identity').value || 'camera-activity', source: {adapter: 'frigate_reviews', source: settings.source, subject: camera},
    window: {merge_seconds: Number($('merge').value), settle_seconds: Number($('settle').value), maximum_seconds: 14400},
    activity: {name: 'activity', match, minimum_seconds: Number($('duration').value)},
    limits: {cooldown_seconds: Number($('cooldown').value), max_per_day: Number($('daily').value), timezone: $('timezone').value},
    evidence: $('evidence').checked ? {adapter: 'frigate', crop_zone: $('crop').value, labels} : {adapter: 'none'}};
  $('policy').value = JSON.stringify(policy, null, 2); message('策略已生成；保存后生效。');
}));
$('editor').addEventListener('submit', run(async event => {
  event.preventDefault();
  const value = {label: $('label').value, pipeline: JSON.parse($('policy').value), enabled: $('enabled').checked, delivery_enabled: $('delivery').checked};
  if (selected) await api('api/rules/' + selected.id, 'PUT', value, selected.revision);
  else { value.id = $('identity').value; selected = await api('api/rules', 'POST', value); }
  await refresh(); message('规则已保存到本地。');
}));
$('archive').addEventListener('click', run(async () => {
  if (!window.confirm('归档后停止处理此规则，并保留历史。归档不能撤销，可复制为新规则。继续？')) return;
  await api('api/rules/' + selected.id, 'DELETE', undefined, selected.revision); await refresh(); message('规则已归档，历史已保留。');
}));
$('history-version').addEventListener('change', run(() => showHistory(Number($('history-version').value))));
run(refresh)();
