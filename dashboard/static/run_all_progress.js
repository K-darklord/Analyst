/* Run All progress controller (Phase 4).
 *
 * Owns the EventSource connection to /api/run-all/stream and the UI of the
 * #runAllPanel overlay. The page's runAll() button delegates here when SSE
 * is available; the legacy polling path is a fallback.
 *
 * Lifecycle:
 *   1. RunAllProgress.start()  -> opens EventSource, shows panel.
 *   2. EventSource.onmessage   -> dispatches by event name to handlers below.
 *   3. On 'all_done'           -> waits 800ms (lets the last card render)
 *      then triggers the existing diff_highlights.js refresh by reloading
 *      the page (same behavior as the legacy path).
 *   4. On error / EventSource.onerror -> fall back to legacy polling for
 *      robustness (network drop, proxy buffering, etc.).
 */
(function () {
  "use strict";

  var es = null;            // EventSource instance
  var panel = null;         // #runAllPanel
  var bar = null;           // #rapBar
  var barText = null;       // #rapBarText
  var pill = null;          // top-bar status pill (#runStatus)
  var pillTop = null;       // #rapPill (inside the panel)
  var tickerListEl = null; // #rapTickerList
  var logEl = null;         // #rapLog
  var tickerState = {};     // ticker -> {status, started_at, finished_at, duration, error, message}
  var total = 0;
  var completed = 0;
  var failed = 0;
  var runAllBtn = null;
  var runBtn = null;

  function cacheDom() {
    panel = document.getElementById('runAllPanel');
    bar = document.getElementById('rapBar');
    barText = document.getElementById('rapBarText');
    pill = document.getElementById('runStatus');
    pillTop = document.getElementById('rapPill');
    tickerListEl = document.getElementById('rapTickerList');
    logEl = document.getElementById('rapLog');
    runAllBtn = document.getElementById('runAllBtn');
    runBtn = document.getElementById('runBtn');
  }

  function show() {
    if (panel) panel.hidden = false;
  }
  function hide() {
    if (panel) panel.hidden = true;
  }

  function setTopPill(html) {
    if (pill) pill.innerHTML = html;
  }
  function setPanelPill(text) {
    if (pillTop) pillTop.textContent = text;
  }

  function updateBar() {
    var pct = total > 0 ? Math.round((completed / total) * 100) : 0;
    if (bar) bar.style.width = pct + '%';
    if (barText) barText.textContent = completed + ' / ' + total + (failed > 0 ? '  (' + failed + ' failed)' : '');
  }

  function renderTickerList() {
    if (!tickerListEl) return;
    var html = '';
    var tickers = Object.keys(tickerState);
    for (var i = 0; i < tickers.length; i++) {
      var t = tickers[i];
      var s = tickerState[t];
      var cls = 'rap-ticker';
      var icon = '&#8230;';  // horizontal ellipsis (running) by default
      if (s.status === 'success') { cls += ' done'; icon = '&#10003;'; }
      else if (s.status === 'failed') { cls += ' failed'; icon = '&#10007;'; }
      else if (s.status === 'running') { cls += ' running'; icon = '<span class="rap-spin">&#9696;</span>'; }
      html += '<div class="' + cls + '">'
        + '<span class="rap-t-icon">' + icon + '</span>'
        + '<span class="rap-t-name mono">' + t + '</span>'
        + (s.duration != null ? '<span class="rap-t-dur mono">' + s.duration + 's</span>' : '')
        + (s.error ? '<span class="rap-t-err" title="' + escapeAttr(s.error) + '">' + escapeHtml(s.error.slice(0, 80)) + '</span>' : '')
        + '</div>';
    }
    tickerListEl.innerHTML = html;
  }

  function appendLog(ticker, message) {
    if (!logEl) return;
    var line = document.createElement('div');
    line.className = 'rap-log-line';
    var ts = new Date().toLocaleTimeString();
    line.innerHTML = '<span class="rap-log-ts mono">' + ts + '</span> '
      + (ticker ? '<span class="rap-log-tk mono">' + escapeHtml(ticker) + '</span> ' : '')
      + '<span class="rap-log-msg">' + escapeHtml(message) + '</span>';
    logEl.appendChild(line);
    // Keep the log scrolled to the latest; cap to last 200 lines so a long
    // batch run doesn't blow up DOM size.
    while (logEl.children.length > 200) {
      logEl.removeChild(logEl.firstChild);
    }
    logEl.scrollTop = logEl.scrollHeight;
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function escapeAttr(s) { return escapeHtml(s); }

  // ---- Event handlers ----
  function onHello(payload) {
    total = payload.total || 0;
    completed = 0;
    failed = 0;
    tickerState = {};
    if (Array.isArray(payload.tickers)) {
      for (var i = 0; i < payload.tickers.length; i++) {
        tickerState[payload.tickers[i]] = { status: 'pending' };
      }
    }
    renderTickerList();
    updateBar();
    setPanelPill('Running 0 / ' + total);
    setTopPill('<span class="status-pill running">Running All...</span>');
    show();
  }

  function onAllStart(payload) {
    total = payload.total || total;
    if (Array.isArray(payload.tickers)) {
      for (var i = 0; i < payload.tickers.length; i++) {
        if (!tickerState[payload.tickers[i]]) {
          tickerState[payload.tickers[i]] = { status: 'pending' };
        }
      }
    }
    renderTickerList();
  }

  function onTickerStart(payload) {
    var t = payload.ticker;
    if (!t) return;
    var s = tickerState[t] || (tickerState[t] = {});
    s.status = 'running';
    s.started_at = payload.started_at;
    s.index = payload.index;
    setPanelPill('Running ' + (payload.index != null ? (payload.index + 1) : '?') + ' / ' + (payload.total || total) + ': ' + t);
    renderTickerList();
    appendLog(t, 'starting analysis ...');
  }

  function onLog(payload) {
    appendLog(payload.ticker, payload.message || '');
  }

  function onTickerDone(payload) {
    var t = payload.ticker;
    if (!t) return;
    var s = tickerState[t] || (tickerState[t] = {});
    s.status = payload.status;  // 'success' or 'failed'
    s.finished_at = payload.finished_at;
    s.duration = payload.duration_sec;
    s.error = payload.error;
    if (payload.status === 'success') completed++;
    if (payload.status === 'failed') failed++;
    updateBar();
    renderTickerList();
    var tag = payload.status === 'success' ? 'done' : 'FAILED';
    appendLog(t, tag + (payload.duration_sec ? ' (' + payload.duration_sec + 's)' : '') + (payload.error ? ' — ' + payload.error : ''));
  }

  function onAllDone(payload) {
    completed = payload.completed != null ? payload.completed : completed;
    failed = payload.failed != null ? payload.failed : failed;
    updateBar();
    var ok = !payload.error;
    setPanelPill(ok ? 'Done (' + completed + '/' + total + ')' : 'Failed (' + failed + '/' + total + ')');
    setTopPill(ok
      ? '<span class="status-pill success">Done (' + completed + '/' + total + ')</span>'
      : '<span class="status-pill failed">Failed: ' + (payload.error || '') + '</span>');
    appendLog(null, '== Run All finished: ' + completed + '/' + total + ' ok, ' + failed + ' failed' + (payload.duration_sec ? ' (' + payload.duration_sec + 's)' : '') + ' ==');
    // Re-enable the buttons so the user can launch another batch.
    if (runAllBtn) runAllBtn.disabled = false;
    if (runBtn) runBtn.disabled = false;
    // Close the SSE stream cleanly so the browser doesn't auto-reconnect.
    if (es) { try { es.close(); } catch (e) {} es = null; }
    // Reload the page so diff_highlights.js re-snapshots and shows UPDATED
    // badges on sections that changed during the batch run (same behavior
    // as the legacy polling path).
    setTimeout(function () { window.location.reload(); }, 800);
  }

  function onError(err) {
    // If the SSE stream drops before all_done, fall back to the legacy
    // polling endpoint so the user still gets completion feedback.
    if (!es) return;  // already closed by onAllDone
    console.warn('[RunAllProgress] SSE error, falling back to polling', err);
    try { es.close(); } catch (e) {} es = null;
    if (window.pollRunAll) {
      window.pollRunAll(total || 1);
    }
  }

  // ---- Public API ----
  function start() {
    cacheDom();
    if (es) { try { es.close(); } catch (e) {} }
    // Reset state for a fresh run.
    completed = 0;
    failed = 0;
    tickerState = {};
    if (logEl) logEl.innerHTML = '';
    if (tickerListEl) tickerListEl.innerHTML = '';
    updateBar();
    es = new EventSource('/api/run-all/stream');
    // Use addEventListener for named events so we get one handler per
    // event type (the server emits event: <name> lines).
    es.addEventListener('hello', function (e) {
      try { onHello(JSON.parse(e.data)); } catch (e) {}
    });
    es.addEventListener('all_start', function (e) {
      try { onAllStart(JSON.parse(e.data)); } catch (e) {}
    });
    es.addEventListener('ticker_start', function (e) {
      try { onTickerStart(JSON.parse(e.data)); } catch (e) {}
    });
    es.addEventListener('log', function (e) {
      try { onLog(JSON.parse(e.data)); } catch (e) {}
    });
    es.addEventListener('ticker_done', function (e) {
      try { onTickerDone(JSON.parse(e.data)); } catch (e) {}
    });
    es.addEventListener('all_done', function (e) {
      try { onAllDone(JSON.parse(e.data)); } catch (e) {}
    });
    es.onerror = onError;
    show();
  }

  // Export as a namespaced singleton so the inline onclick in index.html
  // can call window.RunAllProgress.start() / hide().
  window.RunAllProgress = {
    start: start,
    hide: hide,
    show: show,
  };

  // Expose the controller for debugging in the browser console.
  window.__runAllProgress = { tickerState: tickerState, total: total, completed: completed, failed: failed };
})();
