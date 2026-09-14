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

  /* Quote button: drops a quote stub into the reply box ------------------ */
  var replyBox = document.getElementById('reply-body');
  document.querySelectorAll('[data-quote]').forEach(function (button) {
    button.addEventListener('click', function (event) {
      if (!replyBox) { return; }
      event.preventDefault();
      var author = button.getAttribute('data-quote');
      replyBox.value += (replyBox.value ? '\n\n' : '') + '[QUOTE=' + author + ']\n\n[/QUOTE]\n';
      replyBox.focus();
      replyBox.setSelectionRange(replyBox.value.length, replyBox.value.length);
    });
  });
})();

