// Tiny theme toggle: light/dark, persisted in localStorage.
// The pre-paint init lives inline in head.html to avoid FOUC.
(function () {
  const STORAGE_KEY = 'actor-theme';
  const root = document.documentElement;

  function apply(theme) {
    if (theme === 'dark') root.dataset.theme = 'dark';
    else delete root.dataset.theme;
  }

  function current() {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === 'light' || stored === 'dark') return stored;
    return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function init() {
    const btn = document.querySelector('[data-theme-toggle]');
    if (!btn) return;
    btn.addEventListener('click', () => {
      const next = current() === 'dark' ? 'light' : 'dark';
      localStorage.setItem(STORAGE_KEY, next);
      apply(next);
    });

    matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
      if (!localStorage.getItem(STORAGE_KEY)) apply(e.matches ? 'dark' : 'light');
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
