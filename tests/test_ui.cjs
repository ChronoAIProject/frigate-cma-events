const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../ui/i18n.js'), 'utf8'), context);
const {t, analysis} = context.MonitorI18n;

test('both languages cover every translated HTML element and placeholder', () => {
  const html = fs.readFileSync(path.join(__dirname, '../ui/index.html'), 'utf8');
  for (const [, key] of html.matchAll(/data-i18n(?:-placeholder|-aria-label)?="([^"]+)"/g)) {
    for (const language of ['zh', 'en']) assert.notEqual(t(language, key), key, `${language}: ${key}`);
  }
  assert.match(html, /ui\/i18n.js.*ui\/app.js/);
  assert.equal(t('en', 'currentRevision', {revision: 3}), 'Current v3');
  assert.equal(t('zh', 'currentRevision', {revision: 3}), '当前 v3');
});

test('analysis labels distinguish suppression, delivery, unfinished callbacks and completed work in both languages', () => {
  const cases = [
    [{state: 'skipped', reason: 'cooldown'}, '已跳过 · 处于冷却期', 'Skipped · within the cooldown period'],
    [{state: 'skipped', reason: 'custom_condition'}, '已跳过 · custom_condition', 'Skipped · custom_condition'],
    [{state: 'local_only'}, '仅本地记录', 'Local record only'],
    [{state: 'paused'}, '已暂停', 'Paused'],
    [{state: 'queued'}, '等待推送', 'Queued'],
    [{state: 'retrying'}, '推送重试中', 'Retrying delivery'],
    [{state: 'delivery_failed', http_status: 401}, '推送失败 · HTTP 401', 'Delivery failed · HTTP 401'],
    [{state: 'awaiting_result'}, '等待分析回执', 'awaiting analysis callback'],
    [{state: 'pending', result_status: 'pending'}, '分析待完成', 'Analysis unfinished'],
    [{state: 'completed', result_status: 'recorded'}, '分析完成 · 已记录', 'Analysis complete · recorded'],
    [{state: 'completed', result_status: 'processed'}, '分析完成', 'Analysis complete'],
    [{state: 'test_completed'}, '测试完成 · 非正式分析', 'Test complete · synthetic analysis'],
    [{state: 'failed'}, '分析失败', 'Analysis failed'],
    [{state: 'reported', result_status: 'custom_review'}, '已收到回执 · custom_review', 'Callback received · custom_review'],
    [{state: 'not_sent'}, '未发现投递记录', 'no delivery record'],
  ];
  for (const [value, zh, en] of cases) {
    assert.ok(analysis('zh', value).includes(zh), JSON.stringify(value));
    assert.ok(analysis('en', value).includes(en), JSON.stringify(value));
    assert.doesNotMatch(analysis('en', value), /[\u3400-\u9fff]|\{\w+\}/);
  }
});
