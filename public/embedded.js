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

  // ---- Chat profile detection ----
  // Chainlit renders the active profile name in a dropdown trigger button.
  // We poll briefly after load/navigation to detect profile changes.
  var REQUIRED_PROFILE = 'Eagle Agent';
  var _currentProfile = null;

  function detectChatProfile() {
    // Chainlit 2.x renders the profile selector with id="chat-profiles".
    // The selected profile name appears as text inside a trigger/value span.
    var selector = document.getElementById('chat-profiles');
    if (selector) {
      // The trigger button/span contains the active profile name
      var trigger = selector.querySelector('[class*="SelectValue"], [class*="placeholder"], span');
      if (trigger && trigger.textContent.trim() && trigger.textContent.trim() !== 'Select profile') {
        return trigger.textContent.trim();
      }
    }
    // Fallback: look for the profile icon+name in the header area
    // Chainlit shows "Eagle Agent ∨" style dropdown text
    var headerText = document.querySelector('#chat-profiles span[class*="font-semibold"], #chat-profiles span');
    if (headerText && headerText.textContent.trim()) {
      var text = headerText.textContent.trim();
      if (text !== 'Select profile') return text;
    }
    return _currentProfile; // retain last known
  }

  function startProfileWatcher() {
    // Use MutationObserver to detect profile changes in the DOM
    var observer = new MutationObserver(function () {
      var profile = detectChatProfile();
      if (profile && profile !== _currentProfile) {
        _currentProfile = profile;
        // Notify parent of profile change
        window.parent.postMessage(
          { type: 'chat_profile_change', profile: profile },
          window.location.origin
        );
        // Re-evaluate banner with new profile info
        updateRfqBanner(window.__dashboardContext);
      }
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });

    // Also detect on thread_id messages (profile may change on new thread)
    setTimeout(function () {
      var profile = detectChatProfile();
      if (profile) {
        _currentProfile = profile;
        window.parent.postMessage(
          { type: 'chat_profile_change', profile: profile },
          window.location.origin
        );
      }
    }, 1500);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', startProfileWatcher);
  } else {
    startProfileWatcher();
  }

  function isOnRequiredProfile() {
    return !_currentProfile || _currentProfile === REQUIRED_PROFILE;
  }

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
      updateRfqBanner(window.__dashboardContext);
    }
  });

  // ---- RFQ context banner (top-right of chat) ----

  var rfqBanner = null;

  function ensureBanner() {
    if (rfqBanner) return rfqBanner;
    rfqBanner = document.createElement('div');
    rfqBanner.id = 'rfq-context-banner';
    rfqBanner.setAttribute('role', 'status');
    document.body.appendChild(rfqBanner);
    return rfqBanner;
  }

  function updateRfqBanner(ctx) {
    var banner = ensureBanner();

    // Only show on RFQ detail views
    if (!ctx || ctx.view !== 'rfq_detail' || !ctx.id) {
      banner.style.display = 'none';
      return;
    }

    var rfqId = ctx.id;
    var customer = ctx.customer || '';
    var boundThread = ctx.thread_id || null;
    var activeThread = ctx._activeThreadId || null;
    var isLinked = boundThread && activeThread && boundThread === activeThread;
    var onCorrectProfile = isOnRequiredProfile();

    var label = rfqId + (customer ? ' \u2014 ' + customer : '');

    if (!onCorrectProfile) {
      // Wrong profile — always show unlinked warning, no Link button
      banner.className = 'rfq-banner rfq-banner--unlinked';
      banner.innerHTML = '<span class="rfq-banner__icon">\u26A0\uFE0F</span>' +
        '<span class="rfq-banner__label">Switch to Eagle Agent for ' + escapeHtml(rfqId) + '</span>';
    } else if (isLinked) {
      banner.className = 'rfq-banner rfq-banner--linked';
      banner.innerHTML = '<span class="rfq-banner__icon">\uD83D\uDCCB</span>' +
        '<span class="rfq-banner__label">' + escapeHtml(label) + '</span>';
    } else if (boundThread) {
      // A different thread is bound — show the mismatch warning
      banner.className = 'rfq-banner rfq-banner--unlinked';
      banner.innerHTML = '<span class="rfq-banner__icon">\u26A0\uFE0F</span>' +
        '<span class="rfq-banner__label">Not linked to ' + escapeHtml(rfqId) + '</span>' +
        '<button class="rfq-banner__link-btn" title="Link this chat to ' + escapeHtml(rfqId) + '">Link</button>';
      var btn = banner.querySelector('.rfq-banner__link-btn');
      btn.addEventListener('click', function () {
        window.parent.postMessage(
          { type: 'bind_rfq_thread', rfqId: rfqId },
          window.location.origin
        );
      });
    } else {
      // No binding yet (new thread, pending auto-bind) — stay quiet
      banner.style.display = 'none';
      return;
    }

    banner.style.display = 'block';
  }

  function escapeHtml(str) {
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
  }

  // ---- Persist sidebar (thread list) open/closed state ----

  /**
   * Chainlit writes a "sidebar:state" cookie on toggle but never reads
   * it back on init — so the sidebar always opens on reload.
   * We fix that by reading the cookie and clicking the toggle button
   * once the UI has mounted if the user previously closed it.
   */
  function getSidebarCookie() {
    var match = document.cookie.match(/(?:^|;\s*)sidebar:state=([^;]*)/);
    return match ? match[1] : null;
  }

  function restoreSidebarState() {
    var saved = getSidebarCookie();
    if (saved !== 'false') return;                 // open is the default — nothing to do
    var btn = document.querySelector('[aria-label="Toggle Sidebar"]');
    if (btn) {
      btn.click();                                 // close it
    } else {
      // React hasn't mounted yet — retry briefly
      var attempts = 0;
      var timer = setInterval(function () {
        var b = document.querySelector('[aria-label="Toggle Sidebar"]');
        if (b) { b.click(); clearInterval(timer); }
        if (++attempts > 20) clearInterval(timer); // give up after ~2s
      }, 100);
    }
  }

  // Run after DOM is ready (script may load before React mounts)
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', restoreSidebarState);
  } else {
    restoreSidebarState();
  }

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
