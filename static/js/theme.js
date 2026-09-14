/* Chai & Trails: apply the saved or system theme before the page is drawn.
   Loaded in <head> without defer, ahead of the stylesheet, so a dark-mode
   visitor never sees a flash of the light theme. site.js runs the Night/Day
   toggle once the page has loaded. */
(function () {
  'use strict';

  var theme = null;
  try { theme = window.localStorage.getItem('ct-theme'); } catch (e) { /* storage blocked */ }

  if (!theme && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
    theme = 'dark';
  }
  if (theme) {
    document.documentElement.setAttribute('data-theme', theme);
  }
})();
