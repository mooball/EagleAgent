/**
 * Chainlit UI customisation for embedded mode.
 *
 * When Chainlit runs inside the FastAPI dashboard iframe, the parent
 * frame provides its own auth UI, dark mode toggle, and header.
 * Redundant header elements are hidden via public/stylesheet.css.
 */
(function () {
  'use strict';

  /** Theme sync: listen for messages from the parent dashboard frame. */
  function applyTheme(theme) {
    var root = document.documentElement;
    if (theme === 'dark') {
      root.classList.add('dark');
    } else {
      root.classList.remove('dark');
    }
    // Persist so Chainlit's own code stays in sync on next render
    // Chainlit uses 'vite-ui-theme' as its storage key
    try { localStorage.setItem('vite-ui-theme', theme); } catch (_) {}
  }

  /**
   * Dashboard context: stored locally AND pushed to the server so the
   * agent can inject it into its system prompt.
   */
  window.__dashboardContext = {};

  function pushContextToServer(ctx) {
    fetch('/api/dashboard-context', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(ctx),
      credentials: 'same-origin',
    }).catch(function () { /* best-effort */ });
  }

  window.addEventListener('message', function (event) {
    if (event.data && event.data.type === 'theme-change') {
      applyTheme(event.data.theme);
    }
    if (event.data && event.data.type === 'dashboard_context') {
      window.__dashboardContext = event.data.payload || {};
      pushContextToServer(window.__dashboardContext);
    }
  });

  // ---- Agent → Dashboard communication ----

  /**
   * Navigate the parent dashboard from inside the Chainlit iframe.
   * Called by embedded.js helpers or agent tools.
   */
  window.navigateDashboard = function (route) {
    window.parent.postMessage(
      { type: 'agent_navigate', payload: { url: route } },
      window.location.origin
    );
  };

  /**
   * Ask the parent dashboard to refresh its current view.
   * Useful after an action modifies data that the dashboard is displaying.
   */
  window.refreshDashboard = function () {
    window.parent.postMessage(
      { type: 'dashboard_refresh' },
      window.location.origin
    );
  };

  /**
   * Intercept clicks on dashboard links (e.g. /suppliers/*, /products/*, /rfqs/*)
   * inside the Chainlit iframe and navigate the parent dashboard instead.
   */
  document.addEventListener('click', function (e) {
    var link = e.target.closest('a[href]');
    if (!link) return;
    var href = link.getAttribute('href');
    if (href && /^\/(suppliers|products|rfqs)(\/|$)/.test(href)) {
      e.preventDefault();
      window.navigateDashboard(href);
    }
  });
})();
