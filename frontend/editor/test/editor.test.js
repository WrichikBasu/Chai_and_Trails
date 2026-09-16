// Tests for the visual editor, run with `npm test` (Node's own test runner, with
// jsdom standing in for the browser).

import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { JSDOM } from 'jsdom';

let createEditor;
let toMarkdown;
let clampWidth;
let setUpTray;

before(async () => {
  const { window } = new JSDOM('<!doctype html><html><body></body></html>', { pretendToBeVisual: true });
  for (const name of ['window', 'document', 'Node', 'Element', 'HTMLElement', 'MutationObserver', 'getComputedStyle',
    'DOMParser', 'Text', 'DocumentFragment', 'Event', 'MouseEvent', 'KeyboardEvent', 'FormData',
    'requestAnimationFrame', 'cancelAnimationFrame']) {
    Object.defineProperty(globalThis, name, { value: window[name], configurable: true, writable: true });
  }
  Object.defineProperty(globalThis, 'navigator', { value: window.navigator, configurable: true });
  ({ createEditor } = await import('../src/editor.js'));
  ({ toMarkdown } = await import('../src/markdown.js'));
  ({ clampWidth } = await import('../src/photo.js'));
  ({ setUpTray } = await import('../src/tray.js'));
});

beforeEach(() => { document.body.innerHTML = ''; });

function editorWith(html) {
  const element = document.createElement('div');
  document.body.append(element);
  return createEditor(element, html);
}

const markdown = (editor) => toMarkdown(editor.state.doc);
const photo = (id, extra = {}) => ({ type: 'photo', attrs: { id, src: `/media/p${id}-1600.jpg`, alt: 'photo', ...extra } });

test('formatting is written out as Markdown the server understands', () => {
  const editor = editorWith(
    '<p><strong>Bold</strong>, <em>italic</em>, <s>gone</s> and <code>code</code> with a <a href="https://e.com">link</a></p>'
    + '<ul><li><p>fuel</p></li><li><p>water</p></li></ul><ol start="3"><li><p>third</p></li></ol>'
    + '<blockquote><p>quoted</p></blockquote><h2>Day two</h2>',
  );
  assert.equal(markdown(editor), [
    '**Bold**, *italic*, ~~gone~~ and `code` with a [link](https://e.com)',
    '',
    '* fuel',
    '* water',
    '',
    '3. third',
    '',
    '> quoted',
    '',
    '## Day two',
  ].join('\n'));
});

test('aligned text is written out as HTML, which posts allow', () => {
  const editor = editorWith('<p>Day one.</p><p>Kaza at last</p>');
  editor.commands.setTextSelection(15);          // in the second paragraph
  editor.chain().focus().setTextAlign('center').run();
  assert.equal(markdown(editor), 'Day one.\n\n<p style="text-align: center;">Kaza at last</p>');

  editor.chain().focus().setTextAlign('left').run();  // back to normal: plain Markdown again
  assert.equal(markdown(editor), 'Day one.\n\nKaza at last');
});

test('a table is written out as HTML, keeping its header row', () => {
  const editor = editorWith('');
  editor.chain().focus().insertTable({ rows: 2, cols: 2, withHeaderRow: true }).run();
  editor.commands.insertContent('Fuel');
  const out = markdown(editor);
  // The column widths TipTap adds (min-width, <colgroup>) are dropped by the server's cleaner.
  assert.match(out, /^<table class="post-table"[^>]*>/);
  assert.match(out, /<th[^>]*>\s*<p>Fuel<\/p>\s*<\/th>/);
  assert.equal((out.match(/<tr>/g) || []).length, 2);
});

test('an aligned paragraph and a table reopen from the server\'s HTML', () => {
  const editor = editorWith(
    '<p style="text-align:center">Kaza at last</p>'
    + '<table><tbody><tr><th><p>Stage</p></th><td><p>4 h</p></td></tr></tbody></table>',
  );
  assert.equal(editor.state.doc.firstChild.attrs.textAlign, 'center');
  const out = markdown(editor);
  assert.match(out, /^<p style="text-align: center;">Kaza at last<\/p>/);
  assert.match(out, /<table[^>]*>[\s\S]*Stage[\s\S]*4 h[\s\S]*<\/table>/);
});

test('a photo goes in at the cursor, between the paragraphs', () => {
  const editor = editorWith('<p>Day one.</p><p>Day two.</p>');
  editor.commands.setTextSelection(9);  // just after "Day one."
  editor.chain().focus().insertContent(photo(41)).run();
  assert.equal(markdown(editor), 'Day one.\n\n![photo](attachment:41)\n\nDay two.');
});

test('a photo inserted mid-paragraph splits it there', () => {
  const editor = editorWith('<p>Day one. Day two.</p>');
  editor.commands.setTextSelection(10);  // after "Day one. "
  editor.chain().focus().insertContent(photo(7)).run();
  assert.match(markdown(editor), /^Day one\.\s*\n\n!\[photo\]\(attachment:7\)\n\nDay two\.$/);
});

test('widths snap to 5% between 20% and 100%', () => {
  assert.deepEqual([7, 20, 63, 88, 140].map(clampWidth), [20, 20, 65, 90, 100]);
});

test('a resized photo keeps its width in the Markdown', () => {
  const editor = editorWith('<p>Intro</p>');
  editor.chain().focus('end').insertContent(photo(41)).run();
  let photoPos = null;
  editor.state.doc.descendants((node, pos) => { if (node.type.name === 'photo') { photoPos = pos; } });
  editor.view.dispatch(editor.state.tr.setNodeMarkup(photoPos, undefined, { ...editor.state.doc.nodeAt(photoPos).attrs, width: 60 }));
  assert.match(markdown(editor), /!\[photo\]\(attachment:41\)\{width="60%"\}/);
});

test('the + and − buttons on a photo resize it', () => {
  const editor = editorWith('');
  editor.commands.insertContent(photo(41, { width: 50 }));
  const figure = editor.view.dom.querySelector('figure.editor-photo');
  assert.equal(figure.style.width, '50%');
  figure.querySelector('[aria-label="Make the photo larger"]').click();
  assert.match(markdown(editor), /\{width="60%"\}/);
  figure.querySelector('[aria-label="Make the photo smaller"]').click();
  figure.querySelector('[aria-label="Make the photo smaller"]').click();
  assert.match(markdown(editor), /\{width="40%"\}/);
});

test('dragging the corner resizes the photo', () => {
  const editor = editorWith('');
  editor.commands.insertContent(photo(41));
  const figure = editor.view.dom.querySelector('figure.editor-photo');
  const handle = figure.querySelector('.editor-photo__handle');
  // jsdom lays nothing out, so give the photo and the column real sizes.
  figure.getBoundingClientRect = () => ({ width: 600 });
  Object.defineProperty(editor.view.dom, 'clientWidth', { value: 600 });
  handle.setPointerCapture = () => {};
  const pointer = (type, clientX) => handle.dispatchEvent(new MouseEvent(type, { clientX, bubbles: true }));
  pointer('pointerdown', 600);
  pointer('pointermove', 400);   // dragged 200px to the left: 400 of 600px is 67%, snapped to 65%
  assert.equal(figure.style.width, '65%');
  pointer('pointerup', 400);
  assert.match(markdown(editor), /\{width="65%"\}/);
});

test('the × button takes a photo out of the post', () => {
  const editor = editorWith('<p>Intro</p>');
  editor.chain().focus('end').insertContent(photo(41)).run();
  editor.view.dom.querySelector('[aria-label="Remove the photo from the post"]').click();
  assert.equal(markdown(editor).trim(), 'Intro');
});

test("the server's HTML reopens in the editor with photos and widths", () => {
  const editor = editorWith(
    '<p>Intro</p><figure class="photo" style="width: 60%"><a href="/media/a.jpg">'
    + '<img src="/media/a-1600.jpg" alt="Rohtang" data-photo="41"></a></figure><p>Outro</p>',
  );
  assert.equal(markdown(editor), 'Intro\n\n![Rohtang](attachment:41){width="60%"}\n\nOutro');
});

// --- The photo tray --------------------------------------------------------

function trayFor(editor, items = '') {
  const form = document.createElement('form');
  form.innerHTML = `
    <input type="hidden" name="draft" value="0123456789abcdef0123456789abcdef">
    <section data-photo-tray data-upload-url="/photos/upload/" data-max-mb="10" hidden>
      <button type="button" data-tray-add>Add photos</button>
      <input type="file" hidden data-tray-input>
      <ul data-tray-list>${items}</ul>
      <p data-tray-empty>No photos yet.</p>
      <ul data-tray-errors></ul>
    </section>`;
  document.body.append(form);
  const tray = form.querySelector('[data-photo-tray]');
  setUpTray(tray, editor, 'csrf-token');
  return tray;
}

const waitingItem = (id) => `
  <li class="tray__item" data-photo="${id}" data-src="/media/p${id}-1600.jpg" data-remove-url="/photos/${id}/remove/">
    <img src="/media/p${id}-800.jpg" alt="">
    <div class="tray__actions"><button type="button" data-tray-insert>Insert</button><button type="button" data-tray-remove>×</button></div>
  </li>`;

test('Insert on a tray photo places it at the cursor', () => {
  const editor = editorWith('<p>Day one.</p><p>Day two.</p>');
  const tray = trayFor(editor, waitingItem(12));
  assert.equal(tray.hidden, false);
  assert.equal(tray.querySelector('[data-tray-empty]').hidden, true);
  editor.commands.setTextSelection(9);
  tray.querySelector('[data-tray-insert]').click();
  assert.equal(markdown(editor), 'Day one.\n\n![photo](attachment:12)\n\nDay two.');
});

test('added photos upload, then appear in the tray ready to insert', async () => {
  const editor = editorWith('<p>Intro</p>');
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, csrf: options.headers['X-CSRFToken'] });
    return new Response(JSON.stringify({
      id: 55, preview_url: '/media/n-800.jpg', display_url: '/media/n-1600.jpg', remove_url: '/photos/55/remove/',
    }), { status: 201 });
  };
  URL.createObjectURL = () => 'blob:local';
  URL.revokeObjectURL = () => {};
  const tray = trayFor(editor);
  const input = tray.querySelector('[data-tray-input]');
  Object.defineProperty(input, 'files', { value: [new window.File(['x'], 'kaza.jpg', { type: 'image/jpeg' })], configurable: true });
  input.dispatchEvent(new Event('change'));
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.deepEqual(requests, [{ url: '/photos/upload/', csrf: 'csrf-token' }]);
  const item = tray.querySelector('.tray__item');
  assert.equal(item.dataset.photo, '55');
  assert.equal(item.querySelector('img').getAttribute('src'), '/media/n-800.jpg');
  editor.commands.setTextSelection(6);
  item.querySelector('[data-tray-insert]').click();
  assert.equal(markdown(editor), 'Intro\n\n![photo](attachment:55)');
});

test('a camera file with no type still uploads', async () => {
  // Browsers often report type "" for .JPG files straight from a camera; the name decides,
  // and the server checks what the file really is.
  const editor = editorWith('');
  let sent = 0;
  globalThis.fetch = async () => { sent += 1; return new Response(JSON.stringify({
    id: 77, preview_url: '/media/c-800.jpg', display_url: '/media/c-1600.jpg', remove_url: '/photos/77/remove/',
  }), { status: 201 }); };
  URL.createObjectURL = () => 'blob:local';
  URL.revokeObjectURL = () => {};
  const tray = trayFor(editor);
  const input = tray.querySelector('[data-tray-input]');
  Object.defineProperty(input, 'files', { value: [new window.File(['x'], 'DSCF1234.JPG', { type: '' })], configurable: true });
  input.dispatchEvent(new Event('change'));
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(sent, 1);
  assert.equal(tray.querySelector('.tray__item').dataset.photo, '77');
  assert.equal(tray.querySelectorAll('[data-tray-errors] li').length, 0);
});

test('an upload says which draft it belongs to', async () => {
  // Posting deletes the unused photos of this draft only, so each upload carries its key.
  const editor = editorWith('');
  let draftSent = null;
  globalThis.fetch = async (url, options) => {
    draftSent = options.body.get('draft');
    return new Response(JSON.stringify({ id: 9, preview_url: '/a-800.jpg', display_url: '/a-1600.jpg', remove_url: '/photos/9/remove/' }), { status: 201 });
  };
  URL.createObjectURL = () => 'blob:local';
  URL.revokeObjectURL = () => {};
  const tray = trayFor(editor);
  const input = tray.querySelector('[data-tray-input]');
  Object.defineProperty(input, 'files', { value: [new window.File(['x'], 'a.jpg', { type: 'image/jpeg' })], configurable: true });
  input.dispatchEvent(new Event('change'));
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(draftSent, '0123456789abcdef0123456789abcdef');
});

test('a refused upload is reported and leaves nothing in the tray', async () => {
  const editor = editorWith('');
  globalThis.fetch = async () => new Response(JSON.stringify({ error: 'big.jpg is larger than 10 MB.' }), { status: 400 });
  URL.createObjectURL = () => 'blob:local';
  const tray = trayFor(editor);
  const input = tray.querySelector('[data-tray-input]');
  Object.defineProperty(input, 'files', {
    value: [new window.File(['x'], 'big.jpg', { type: 'image/jpeg' }), new window.File(['x'], 'anim.gif', { type: 'image/gif' })],
    configurable: true,
  });
  input.dispatchEvent(new Event('change'));
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(tray.querySelectorAll('.tray__item').length, 0);
  assert.deepEqual([...tray.querySelectorAll('[data-tray-errors] li')].map((li) => li.textContent),
    ['anim.gif isn’t a JPEG, PNG or WebP photo.', 'big.jpg is larger than 10 MB.']);
});

test('a file over the server\'s limit is refused before it is uploaded', async () => {
  // The tray reads the limit from data-max-mb, so it always matches forum/photos.py.
  const editor = editorWith('');
  let sent = 0;
  globalThis.fetch = async () => { sent += 1; return new Response('{}', { status: 201 }); };
  const tray = trayFor(editor);
  const input = tray.querySelector('[data-tray-input]');
  const tooBig = new window.File(['x'], 'raw.JPG', { type: 'image/jpeg' });
  Object.defineProperty(tooBig, 'size', { value: 11 * 1024 * 1024 });
  Object.defineProperty(input, 'files', { value: [tooBig], configurable: true });
  input.dispatchEvent(new Event('change'));
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(sent, 0);
  assert.deepEqual([...tray.querySelectorAll('[data-tray-errors] li')].map((li) => li.textContent),
    ['raw.JPG is larger than 10 MB.']);
});

test('removing a tray photo deletes it and takes it out of the post', async () => {
  const editor = editorWith('<p>Intro</p>');
  const calls = [];
  globalThis.fetch = async (url) => { calls.push(url); return new Response(null, { status: 204 }); };
  const tray = trayFor(editor, waitingItem(12));
  editor.commands.setTextSelection(6);
  tray.querySelector('[data-tray-insert]').click();
  tray.querySelector('[data-tray-remove]').click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(calls, ['/photos/12/remove/']);
  assert.equal(tray.querySelectorAll('.tray__item').length, 0);
  assert.equal(markdown(editor).trim(), 'Intro');
});
