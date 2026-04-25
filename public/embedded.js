/**
 * Chainlit UI customisation for embedded mode.
 *
 * When Chainlit runs inside the FastAPI dashboard iframe, the parent
 * frame provides its own auth UI, dark mode toggle, and header.
 * This script hides the redundant Chainlit header elements.
 */
(function () {
  'use strict';

  /** Selectors for elements to hide. Since all agent panel controls and
   *  user/theme UI now live in the parent dashboard header, we hide the
   *  entire Chainlit header bar. */
  const SELECTORS = [
    // The container holding readme, theme-toggle, and user-nav buttons
    '#readme-button',
    '#theme-toggle',
    '#user-nav-button',
  ];

  function hideElements() {
    for (const sel of SELECTORS) {
      document.querySelectorAll(sel).forEach(function (el) {
        el.style.display = 'none';
      });
    }
    // Also hide the parent flex container that holds all three buttons
    // so no empty space remains at the top
    var readme = document.getElementById('readme-button');
    if (readme && readme.parentElement) {
      readme.parentElement.style.display = 'none';
    }
  }

  // Run immediately for elements already in DOM
  hideElements();

  // Watch for late-rendered React elements
  var observer = new MutationObserver(hideElements);
  observer.observe(document.body, { childList: true, subtree: true });

  // Stop observing after 10s to avoid unnecessary overhead
  setTimeout(function () { observer.disconnect(); }, 10000);

  /** Theme sync: listen for messages from the parent dashboard frame. */
  function applyTheme(theme) {
    var root = document.documentElement;
    if (theme === 'dark') {
      root.classList.add('dark');
    } else {
      root.classList.remove('dark');
    }
    // Persist so Chainlit's own code stays in sync on next render
    try { localStorage.setItem('chainlit-theme', theme); } catch (_) {}
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
    // Only accept same-origin messages when embedded
    if (event.data && event.data.type === 'theme-change') {
      applyTheme(event.data.theme);
    }
    if (event.data && event.data.type === 'dashboard_context') {
      window.__dashboardContext = event.data.payload || {};
      pushContextToServer(window.__dashboardContext);
    }
  });

  /**
   * Navigate the parent dashboard from inside the Chainlit iframe.
   * Called by the navigate_dashboard tool via a Chainlit action.
   */
  window.navigateDashboard = function (route) {
    window.parent.postMessage(
      { type: 'agent_navigate', payload: { url: route } },
      window.location.origin
    );
  };
})();
