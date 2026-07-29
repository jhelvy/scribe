// Applied before first paint so a dark-mode reader never sees a flash of paper
// white. A separate file rather than an inline script so the page can be served
// under `script-src 'self'` with no `unsafe-inline`.
(function () {
  try {
    var stored = localStorage.getItem("scribe-theme");
    if (stored) document.documentElement.dataset.theme = stored;
  } catch (e) {}
})();
