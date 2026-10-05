#!/usr/bin/env node
// Release regressions against the real helpers in index.html; no test dependencies.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)];
assert.equal(scripts.length, 1, 'Expected one real inline application script');
const source = scripts[0][1];

function extract(pattern, name) {
  const match = source.match(pattern);
  assert.ok(match, `Cannot find the real ${name} helper in index.html`);
  return match[0];
}

// Extract declarations, not rewritten implementations. Changing their behaviour
// in production therefore changes the result of these tests.
const helperSource = [
  extract(/^const esc=[^\n]+$/m, 'esc'),
  extract(/^function catalogImage\(path\)\{[\s\S]*?(?=^function showModal\()/m, 'catalogImage'),
  extract(/^const copyResetTimers=new WeakMap\(\);$/m, 'copyResetTimers'),
  extract(/^async function copyValue\(button\)\{[\s\S]*?(?=^document\.addEventListener\()/m, 'copyValue'),
].join('\n');

function button(value = 'Проверочное значение', label = 'Скопировать') {
  return {
    dataset: { copyValue: value },
    textContent: label,
    focusCount: 0,
    focus() { this.focusCount++; },
  };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function runtime({ secure = true, writeText = async () => {}, execCommand = () => true } = {}) {
  const timers = new Map(), attached = new Set(), modalCalls = [], copies = [], toasts = [];
  let now = 0, nextTimer = 0;
  const fallback = {
    focusCount: 0, selectCount: 0,
    focus() { this.focusCount++; },
    select() { this.selectCount++; },
  };
  const context = vm.createContext({
    navigator: secure ? { clipboard: { writeText(value) { copies.push(value); return writeText(value); } } } : {},
    window: { isSecureContext: secure },
    document: {
      createElement(tag) {
        assert.equal(tag, 'textarea', 'Clipboard fallback must never create a form');
        const field = {
          value: '', style: {}, selected: false,
          select() { this.selected = true; },
          remove() { attached.delete(this); },
        };
        return field;
      },
      body: { append(field) { attached.add(field); } },
      execCommand(command) { assert.equal(command, 'copy'); return execCommand(); },
    },
    modal: { querySelector(selector) { assert.equal(selector, 'textarea'); return fallback; } },
    showModal(markup, trigger) { modalCalls.push({ markup, trigger }); },
    toast(value) { toasts.push(value); },
    setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, at: now + delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  vm.runInContext(helperSource + '\nglobalThis.releaseHelpers={esc,catalogImage,copyValue};', context, { timeout: 1000 });
  return {
    ...context.releaseHelpers, timers, attached, modalCalls, copies, toasts, fallback,
    advance(milliseconds) {
      const target = now + milliseconds;
      while (true) {
        const due = [...timers.entries()].filter(([, value]) => value.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        timers.delete(due[0]); now = due[1].at; due[1].callback();
      }
      now = target;
    },
  };
}

test('catalogImage accepts local WebP paths and rejects URL/attribute injection', () => {
  const { catalogImage } = runtime();
  for (const path of ['catalog/photo-001.webp', 'catalog/thumb-beki.webp', 'catalog/A_B-9.webp']) {
    assert.equal(catalogImage(path), path);
  }
  const rejected = [
    '', undefined, null, 7, [], {},
    'https://example.invalid/photo.webp', '//example.invalid/photo.webp',
    'javascript:alert(1)', 'data:image/svg+xml,<svg onload="alert(1)">',
    '/catalog/photo.webp', '../catalog/photo.webp', 'catalog/../photo.webp',
    'catalog/nested/photo.webp', 'catalog\\photo.webp', 'catalog/%70hoto.webp',
    'catalog/photo.webp?remote=1', 'catalog/photo.webp#fragment', 'catalog/photo.WEBP',
    'catalog/photo.webp" onerror="alert(1)', 'catalog/photo.webp\n',
    'catalog/photo.webp\r', 'catalog/photo.webp\r\n', ' catalog/photo.webp',
  ];
  for (const path of rejected) assert.equal(catalogImage(path), '', `Unsafe path was accepted: ${String(path)}`);
});

test('esc protects text and quoted attributes against markup payloads', () => {
  const { esc } = runtime();
  assert.equal(esc('&<>"\''), '&amp;&lt;&gt;&quot;&#39;');
  const payload = '\"><img src=x onerror=alert(1)><!-- & \' ';
  const escaped = esc(payload);
  assert.ok(!/[<>"']/.test(escaped));
  assert.ok(escaped.includes('&quot;&gt;&lt;img'));
  assert.ok(escaped.includes('&amp;'));
});

test('repeated copy clicks retain the initial button label and replace its timer', async () => {
  const app = runtime(), control = button('value', 'Скопировать номер');
  await app.copyValue(control);
  assert.equal(control.textContent, 'Скопировано');
  app.advance(1700);
  await app.copyValue(control);
  assert.equal(app.timers.size, 1);
  app.advance(600);
  assert.equal(control.textContent, 'Скопировано', 'Previous timer must not reset the latest click');
  app.advance(1700);
  assert.equal(control.textContent, 'Скопировать номер');
  assert.equal(app.timers.size, 0);
  assert.deepEqual(app.copies, ['value', 'value']);
});

test('overlapping clipboard promises resolving in reverse order restore the initial label', async () => {
  const first = deferred(), second = deferred(); let calls = 0;
  const app = runtime({ writeText: () => (++calls === 1 ? first : second).promise });
  const control = button('value', 'Скопировать назначение');
  const one = app.copyValue(control), two = app.copyValue(control);
  second.resolve(); await two;
  app.advance(800);
  first.resolve(); await one;
  assert.equal(control.textContent, 'Скопировано');
  assert.equal(app.timers.size, 1);
  app.advance(2300);
  assert.equal(control.textContent, 'Скопировать назначение');
  assert.equal(app.timers.size, 0);
});

test('copy feedback timers remain independent for different buttons', async () => {
  const app = runtime(), first = button('first', 'Первый номер'), second = button('second', 'Второй номер');
  await app.copyValue(first); app.advance(1000); await app.copyValue(second);
  assert.equal(app.timers.size, 2);
  app.advance(1300);
  assert.equal(first.textContent, 'Первый номер');
  assert.equal(second.textContent, 'Скопировано');
  app.advance(1000);
  assert.equal(second.textContent, 'Второй номер');
  assert.equal(app.timers.size, 0);
});

test('clipboard denial produces escaped, read-only manual fallback with no form', async () => {
  const app = runtime({ writeText: async () => { throw new Error('permission denied'); } });
  const payload = '</textarea><form action="https://example.invalid"><input name="secret"><script>alert(1)</script>';
  const control = button(payload, 'Скопировать');
  await app.copyValue(control);
  assert.equal(app.attached.size, 0);
  assert.equal(app.timers.size, 0);
  assert.equal(control.textContent, 'Скопировать');
  assert.equal(app.modalCalls.length, 1);
  const { markup, trigger } = app.modalCalls[0];
  assert.equal(trigger, control);
  assert.ok(markup.includes('textarea class="copy-fallback" readonly'));
  assert.ok(markup.includes(app.esc(payload)));
  assert.ok(!/<form\b|<input\b|<script\b/i.test(markup));
  assert.equal(app.fallback.focusCount, 1);
  assert.equal(app.fallback.selectCount, 1);
});

test('successful legacy clipboard fallback removes its temporary textarea', async () => {
  const app = runtime({ secure: false }), control = button('value', 'Копировать');
  await app.copyValue(control);
  assert.equal(app.attached.size, 0);
  assert.equal(control.focusCount, 1);
  assert.equal(control.textContent, 'Скопировано');
  assert.equal(app.modalCalls.length, 0);
  app.advance(2300);
  assert.equal(control.textContent, 'Копировать');
});

test('legacy clipboard refusal removes its temporary textarea before manual copying', async () => {
  const app = runtime({ secure: false, execCommand: () => false }), control = button();
  await app.copyValue(control);
  assert.equal(app.attached.size, 0);
  assert.equal(app.modalCalls.length, 1);
  assert.equal(app.timers.size, 0);
});

test('legacy clipboard exception still cleans its temporary textarea and offers manual copying', async () => {
  const app = runtime({ secure: false, execCommand: () => { throw new Error('legacy copy blocked'); } });
  await app.copyValue(button());
  assert.equal(app.attached.size, 0, 'A failed copy must not leave a hidden textarea in the document');
  assert.equal(app.modalCalls.length, 1);
  assert.equal(app.timers.size, 0);
  assert.equal(app.fallback.focusCount, 1);
  assert.equal(app.fallback.selectCount, 1);
});
