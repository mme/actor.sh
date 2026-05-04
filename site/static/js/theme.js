// Theme toggle (persisted) + responsive sidebar drawer + TOC scroll-spy.
// The pre-paint theme init lives inline in head.html to avoid FOUC.
(function () {
  const STORAGE_KEY = 'actor-theme';
  const root = document.documentElement;

  function currentTheme() {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === 'light' || stored === 'dark') return stored;
    return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function applyTheme(theme) {
    if (theme === 'dark') root.dataset.theme = 'dark';
    else delete root.dataset.theme;
  }

  function initThemeToggle() {
    const btn = document.querySelector('[data-theme-toggle]');
    if (!btn) return;
    btn.addEventListener('click', () => {
      const next = currentTheme() === 'dark' ? 'light' : 'dark';
      localStorage.setItem(STORAGE_KEY, next);
      applyTheme(next);
    });
    matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
      if (!localStorage.getItem(STORAGE_KEY)) applyTheme(e.matches ? 'dark' : 'light');
    });
  }

  function initSidebarDrawer() {
    const toggle = document.querySelector('[data-sidebar-toggle]');
    const backdrop = document.querySelector('[data-sidebar-backdrop]');
    const sidebar = document.getElementById('site-sidebar');
    if (!toggle || !sidebar) return;

    function setOpen(open) {
      if (open) root.dataset.navOpen = 'true';
      else delete root.dataset.navOpen;
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    toggle.addEventListener('click', () => setOpen(root.dataset.navOpen !== 'true'));
    if (backdrop) backdrop.addEventListener('click', () => setOpen(false));
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && root.dataset.navOpen === 'true') setOpen(false);
    });
    sidebar.addEventListener('click', (e) => {
      if (e.target.closest('a')) setOpen(false);
    });
    matchMedia('(min-width: 768px)').addEventListener('change', (e) => {
      if (e.matches) setOpen(false);
    });
  }

  function initTocScrollSpy() {
    const links = document.querySelectorAll('[data-toc-link]');
    if (!links.length) return;

    const byId = new Map();
    links.forEach((a) => byId.set(a.dataset.tocLink, a));

    const targets = [];
    byId.forEach((_a, id) => {
      const el = document.getElementById(id);
      if (el) targets.push(el);
    });
    if (!targets.length) return;

    let current = null;
    function setActive(id) {
      if (id === current) return;
      current = id;
      links.forEach((a) => {
        if (a.dataset.tocLink === id) a.setAttribute('aria-current', 'true');
        else a.removeAttribute('aria-current');
      });
    }

    // rootMargin top: 64px (just below topbar) + a generous bottom buffer so
    // the section that's actually being read wins over the next one entering.
    const io = new IntersectionObserver(
      (entries) => {
        // Build the list of currently-intersecting sections, sorted by document order.
        const visible = entries
          .filter((e) => e.isIntersecting)
          .map((e) => e.target);
        if (visible.length) {
          visible.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
          setActive(visible[0].id);
        }
      },
      { rootMargin: '-72px 0px -65% 0px', threshold: 0 }
    );
    targets.forEach((t) => io.observe(t));

    // Fallback: pick the heading whose top is closest above the viewport top.
    function pickByScroll() {
      const probe = 80;
      let best = targets[0];
      for (const t of targets) {
        if (t.getBoundingClientRect().top - probe <= 0) best = t;
        else break;
      }
      setActive(best.id);
    }
    window.addEventListener('scroll', pickByScroll, { passive: true });
    pickByScroll();
  }

  function init() {
    initThemeToggle();
    initSidebarDrawer();
    initTocScrollSpy();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
