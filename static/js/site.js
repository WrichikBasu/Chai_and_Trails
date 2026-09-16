/* Chai & Trails: small progressive enhancements. Nothing here is required
   for the pages to work, so it is safe to defer. */
(function () {
  'use strict';

  var root = document.documentElement;

  /* Theme toggle (theme.js has already applied the starting theme) ------- */
  function store(value) {
    try { window.localStorage.setItem('ct-theme', value); } catch (e) { /* private mode */ }
  }

  var toggle = document.querySelector('[data-theme-toggle]');
  if (toggle) {
    var label = function () {
      var dark = root.getAttribute('data-theme') === 'dark';
      toggle.textContent = dark ? 'Day' : 'Night';
      toggle.setAttribute('aria-label', dark ? 'Switch to the day theme' : 'Switch to the night theme');
    };
    label();
    toggle.addEventListener('click', function () {
      var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      store(next);
      label();
    });
  }

  /* Mobile navigation --------------------------------------------------- */
  var burger = document.querySelector('[data-nav-toggle]');
  var navList = document.getElementById('mainnav-list');
  if (burger && navList) {
    burger.addEventListener('click', function () {
      var open = navList.classList.toggle('is-open');
      burger.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  }

  /* Collapse a category block ------------------------------------------- */
  document.querySelectorAll('[data-collapse]').forEach(function (button) {
    button.addEventListener('click', function () {
      var block = button.closest('.block');
      var body = block && block.querySelector('[data-collapse-target]');
      if (!body) { return; }
      var hidden = body.hasAttribute('hidden');
      if (hidden) { body.removeAttribute('hidden'); } else { body.setAttribute('hidden', ''); }
      button.setAttribute('aria-expanded', hidden ? 'true' : 'false');
      button.textContent = hidden ? 'Collapse' : 'Expand';
    });
  });

  /* Quote button: copies the post into the reply box as a Markdown quote -- */
  var replyBox = document.getElementById('reply-body');
  document.querySelectorAll('[data-quote]').forEach(function (button) {
    button.addEventListener('click', function (event) {
      // With the visual editor running, editor.js handles quoting instead.
      if (!replyBox || replyBox.closest('.editor--visual')) { return; }
      event.preventDefault();
      var source = document.getElementById(button.getAttribute('data-quote-from'));
      var lines = ['> ' + button.getAttribute('data-quote') + ' wrote:', '>'];
      (source ? source.innerText.trim() : '').split('\n').forEach(function (line) {
        lines.push(line ? '> ' + line : '>');
      });
      replyBox.value += (replyBox.value ? '\n\n' : '') + lines.join('\n') + '\n\n';
      replyBox.focus();
      replyBox.setSelectionRange(replyBox.value.length, replyBox.value.length);
    });
  });

  /* Formatting buttons: wrap the selected text in Markdown ----------------- */
  var FORMATS = {
    bold: { before: '**', after: '**', example: 'bold text' },
    italic: { before: '_', after: '_', example: 'italic text' },
    link: { before: '[', after: '](https://)', example: 'link text' },
    quote: { line: '> ', example: 'quoted text' },
    list: { line: '- ', example: 'list item' }
  };
  document.querySelectorAll('[data-md]').forEach(function (button) {
    button.addEventListener('click', function () {
      if (button.closest('.editor--visual')) { return; }  // editor.js drives these buttons then
      var box = button.closest('.editor').querySelector('textarea');
      var format = FORMATS[button.getAttribute('data-md')];
      if (!box || !format) { return; }
      var start = box.selectionStart;
      var picked = box.value.slice(start, box.selectionEnd) || format.example;
      var before = format.line || format.before;
      var text = format.line
        ? picked.split('\n').map(function (line) { return format.line + line; }).join('\n')
        : format.before + picked + format.after;
      box.setRangeText(text, start, box.selectionEnd, 'end');
      box.focus();
      // Leave the wrapped words selected, so typing replaces the example text.
      box.setSelectionRange(start + before.length, start + text.length - (format.after || '').length);
    });
  });
})();

