/* TradingAgents Dashboard -- Page-level diff highlights
 * Compares current state snapshot vs last-visited snapshot (localStorage)
 * and highlights changed sections with pulse + UPDATED badges.
 *
 * Fields tracked:
 *   final_trade_decision, market_report, sentiment_report, news_report,
 *   fundamentals_report, trader_investment_decision, investment_plan,
 *   investment_debate_state (bull/bear/judge), risk_debate_state (3-way + judge)
 *
 * Rating changes get a "was: X" indicator on the rating badge.
 */
(function (global) {
  'use strict';

  var SNAP_KEY = 'ta-page-snapshot-';
  var PULSE_DURATION = 15000; // 15s

  // ---------- hash (simple, non-crypto) ----------
  function hash(s) {
    if (!s) return '0';
    s = String(s).slice(0, 500);
    var h = 5381;
    for (var i = 0; i < s.length; i++) {
      h = ((h << 5) + h + s.charCodeAt(i)) & 0x7fffffff;
    }
    return String(h);
  }

  // ---------- snapshot ----------
  function buildSnapshot(state) {
    state = state || {};
    var s = {};
    s.final_trade_decision = hash(state.final_trade_decision);
    s.trader_investment_decision = hash(state.trader_investment_decision);
    s.investment_plan = hash(state.investment_plan);
    s.market_report = hash(state.market_report);
    s.sentiment_report = hash(state.sentiment_report);
    s.news_report = hash(state.news_report);
    s.fundamentals_report = hash(state.fundamentals_report);
    var deb = state.investment_debate_state || {};
    s.inv_bull = hash(deb.bull_history);
    s.inv_bear = hash(deb.bear_history);
    s.inv_judge = hash(deb.judge_decision);
    var rdeb = state.risk_debate_state || {};
    s.risk_agg = hash(rdeb.aggressive_history);
    s.risk_con = hash(rdeb.conservative_history);
    s.risk_neu = hash(rdeb.neutral_history);
    s.risk_judge = hash(rdeb.judge_decision);
    // Rating (first ~60 chars of final_trade_decision)
    var fd = String(state.final_trade_decision || '');
    s.rating = fd.slice(0, 60).replace(/\s+/g, ' ').trim();
    return s;
  }

  function storeSnapshot(ticker, snap) {
    if (!ticker || !snap) return;
    try {
      global.localStorage.setItem(SNAP_KEY + ticker, JSON.stringify(snap));
    } catch (e) {}
  }

  function loadSnapshot(ticker) {
    if (!ticker) return null;
    try {
      var raw = global.localStorage.getItem(SNAP_KEY + ticker);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }

  // ---------- DOM helpers ----------
  function addBadge(el, text, cls) {
    if (!el) return;
    var existing = el.querySelector('.diff-badge');
    if (existing) return; // already has one
    var badge = document.createElement('span');
    badge.className = 'diff-badge' + (cls ? ' ' + cls : '');
    badge.textContent = text;
    el.appendChild(badge);
  }

  function pulse(el) {
    if (!el) return;
    el.classList.add('diff-pulse');
    setTimeout(function () {
      el.classList.remove('diff-pulse');
    }, PULSE_DURATION);
  }

  function addWasIndicator(el, oldVal, newVal) {
    if (!el || !oldVal) return;
    var ind = document.createElement('span');
    ind.className = 'diff-was-rating';
    ind.innerHTML = ' <span class="diff-was-arrow">&rarr;</span> was: <span class="diff-was-old">' +
                    escapeHtml(oldVal) + '</span>';
    el.appendChild(ind);
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
           .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ---------- main diff runner ----------
  function runDiff(state, ticker) {
    if (!state || !ticker) return;
    var current = buildSnapshot(state);
    var prev = loadSnapshot(ticker);
    if (!prev) {
      // First visit — just store snapshot, no highlights.
      storeSnapshot(ticker, current);
      return;
    }

    var anyChange = false;

    // 1. Rating change (topbar + summary)
    if (prev.rating && prev.rating !== current.rating) {
      var ttRating = document.querySelector('.topbar .tt-rating');
      var smRating = document.querySelector('.summary .rating-display');
      pulse(ttRating);
      pulse(smRating);
      addWasIndicator(smRating, prev.rating, current.rating);
      anyChange = true;
    }

    // 2. Final Trade Decision
    if (prev.final_trade_decision !== current.final_trade_decision) {
      var fdEl = document.querySelector('.final-decision');
      if (fdEl) {
        pulse(fdEl);
        var title = fdEl.querySelector('.section-title');
        addBadge(title, 'UPDATED', 'diff-updated');
      }
      anyChange = true;
    }

    // 3. Analyst reports (4 cards)
    var analystMap = {
      market_report: '.analyst-wrap.market',
      sentiment_report: '.analyst-wrap.sentiment',
      news_report: '.analyst-wrap.news',
      fundamentals_report: '.analyst-wrap.fundamentals'
    };
    for (var key in analystMap) {
      if (prev[key] !== current[key]) {
        var card = document.querySelector(analystMap[key]);
        if (card) {
          pulse(card);
          var header = card.querySelector('.card-title, summary, h3');
          addBadge(header || card, 'UPDATED', 'diff-updated');
        }
        anyChange = true;
      }
    }

    // 4. Trader Decision + Investment Plan
    var traderSection = document.querySelector('.non-key-section .debate-2');
    if (traderSection) {
      var collapses = traderSection.querySelectorAll('.collapse');
      if (prev.trader_investment_decision !== current.trader_investment_decision && collapses[0]) {
        pulse(collapses[0]);
        addBadge(collapses[0].querySelector('.card-title'), 'UPDATED', 'diff-updated');
        anyChange = true;
      }
      if (prev.investment_plan !== current.investment_plan && collapses[1]) {
        pulse(collapses[1]);
        addBadge(collapses[1].querySelector('.card-title'), 'UPDATED', 'diff-updated');
        anyChange = true;
      }
    }

    // 5. Investment debate (bull/bear/judge)
    var invDeb = document.querySelector('.non-key-section .debate-2.bull-side, .non-key-section details.bull-side');
    // Use a broader selector: find the debate section by its title
    var sections = document.querySelectorAll('.non-key-section');
    sections.forEach(function (sec) {
      var title = sec.querySelector('.section-title');
      if (!title) return;
      var titleText = title.textContent || '';
      if (titleText.indexOf('Research Debate') !== -1) {
        var bullSide = sec.querySelector('.bull-side');
        var bearSide = sec.querySelector('.bear-side');
        var judgeSide = sec.querySelector('.judge-card');
        if (prev.inv_bull !== current.inv_bull && bullSide) {
          pulse(bullSide); addBadge(bullSide.querySelector('.card-title'), 'UPDATED', 'diff-updated');
          anyChange = true;
        }
        if (prev.inv_bear !== current.inv_bear && bearSide) {
          pulse(bearSide); addBadge(bearSide.querySelector('.card-title'), 'UPDATED', 'diff-updated');
          anyChange = true;
        }
        if (prev.inv_judge !== current.inv_judge && judgeSide) {
          pulse(judgeSide); addBadge(judgeSide.querySelector('.card-title'), 'UPDATED', 'diff-updated');
          anyChange = true;
        }
      }
      if (titleText.indexOf('Risk Debate') !== -1) {
        var aggSide = sec.querySelector('.aggressive-side');
        var conSide = sec.querySelector('.conservative-side');
        var neuSide = sec.querySelector('.neutral-side');
        var rjSide = sec.querySelector('.judge-card');
        if (prev.risk_agg !== current.risk_agg && aggSide) {
          pulse(aggSide); addBadge(aggSide.querySelector('.card-title'), 'UPDATED', 'diff-updated');
          anyChange = true;
        }
        if (prev.risk_con !== current.risk_con && conSide) {
          pulse(conSide); addBadge(conSide.querySelector('.card-title'), 'UPDATED', 'diff-updated');
          anyChange = true;
        }
        if (prev.risk_neu !== current.risk_neu && neuSide) {
          pulse(neuSide); addBadge(neuSide.querySelector('.card-title'), 'UPDATED', 'diff-updated');
          anyChange = true;
        }
        if (prev.risk_judge !== current.risk_judge && rjSide) {
          pulse(rjSide); addBadge(rjSide.querySelector('.card-title'), 'UPDATED', 'diff-updated');
          anyChange = true;
        }
      }
    });

    // 6. Topbar brand pulse if any change
    if (anyChange) {
      var brand = document.querySelector('.topbar .brand');
      if (brand) {
        brand.classList.add('diff-pulse');
        setTimeout(function () { brand.classList.remove('diff-pulse'); }, PULSE_DURATION);
      }
    }

    // Store current as new baseline
    storeSnapshot(ticker, current);
  }

  // ---------- per-analyst content diff (underline) ----------
  // Stores the full text of each analyst field per ticker, then on the
  // next render underlines blocks (paragraphs / list items / table cells)
  // whose normalized text was NOT present in the previous snapshot.
  // Underlines persist until the next reload — they are a "note" of what
  // changed since the user's last visit, not a transient pulse.
  var TEXT_SNAP_KEY = 'ta-text-snapshot-';

  function normalizeLine(s) {
    return String(s || '')
      // Normalize dates so a header that only changed its date
      // (分析日期：2026-09-18 vs 2026-09-01) doesn't trigger a false
      // underline. Both sides get the same <DATE> placeholder.
      .replace(/\b20\d{2}-\d{2}-\d{2}\b/g, '<DATE>')
      .replace(/\b20\d{2}\/\d{2}\/\d{2}\b/g, '<DATE>')
      .replace(/\b\d{4}年\d{1,2}月\d{1,2}日\b/g, '<DATE>')
      // Strip markdown syntax so source lines match marked.js output
      // (which has already dropped these markers from textContent):
      //   list-item prefix  '- foo' / '* foo' / '1. foo'  -> 'foo'
      //   heading hashes    '## Title'                    -> 'Title'
      //   bold/italic       '**bold**' / '__bold__'        -> 'bold'
      //   table cell pipes  '| cell1 | cell2 |'          -> 'cell1 cell2'
      .replace(/^\s*(?:[-*+]|\d+\.)\s+/, '')
      .replace(/^\s*#{1,6}\s+/, '')
      .replace(/\*\*(.+?)\*\*/g, '$1')
      .replace(/__(.+?)__/g, '$1')
      .replace(/\*(.+?)\*/g, '$1')
      .replace(/_(.+?)_/g, '$1')
      .replace(/\|/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function buildOldLineSet(text) {
    if (!text) return null;
    var set = {};
    // Add individual lines (matches <li>, <h2>, single-line blocks).
    var lines = String(text).split(/\r?\n/);
    for (var i = 0; i < lines.length; i++) {
      var n = normalizeLine(lines[i]);
      if (n) set[n] = true;
    }
    // Add full paragraphs (matches <p> blocks — marked.js merges a
    // paragraph's embedded \n into one element, so we need the whole-
    // paragraph text in the set too, otherwise an unchanged paragraph
    // is falsely flagged as new because its textContent doesn't match
    // any individual line).
    var paras = String(text).split(/\r?\n\r?\n/);
    for (var j = 0; j < paras.length; j++) {
      var p = normalizeLine(paras[j]);
      if (p) set[p] = true;
    }
    return set;
  }

  function storeTextSnapshot(ticker, state) {
    if (!ticker || !state) return;
    var snap = {};
    var fields = ['market_report', 'sentiment_report', 'news_report',
                  'fundamentals_report', 'trader_investment_decision',
                  'investment_plan', 'final_trade_decision'];
    for (var i = 0; i < fields.length; i++) {
      if (state[fields[i]]) snap[fields[i]] = String(state[fields[i]]);
    }
    try {
      global.localStorage.setItem(TEXT_SNAP_KEY + ticker, JSON.stringify(snap));
    } catch (e) {}
  }

  function loadTextSnapshot(ticker) {
    if (!ticker) return null;
    try {
      var raw = global.localStorage.getItem(TEXT_SNAP_KEY + ticker);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }

  // Map field name -> CSS selector for the rendered .md-body container.
  // For trader/plan we match by .card-title text (robust against template
  // reordering).
  function findMdBodyForField(field) {
    var analystMap = {
      market_report: '.analyst-wrap.market',
      sentiment_report: '.analyst-wrap.sentiment',
      news_report: '.analyst-wrap.news',
      fundamentals_report: '.analyst-wrap.fundamentals'
    };
    if (analystMap[field]) {
      var c = document.querySelector(analystMap[field]);
      return c ? c.querySelector('.md-body') : null;
    }
    var titleMap = {
      trader_investment_decision: 'Trader Decision',
      investment_plan: 'Investment Plan'
    };
    var titles = document.querySelectorAll('.card-title');
    for (var i = 0; i < titles.length; i++) {
      if (titles[i].textContent.indexOf(titleMap[field]) !== -1) {
        var details = titles[i].closest('details');
        return details ? details.querySelector('.md-body') : null;
      }
    }
    return null;
  }

  function runContentDiff(state, ticker) {
    if (!state || !ticker) return;
    var prev = loadTextSnapshot(ticker);

    var fields = ['market_report', 'sentiment_report', 'news_report',
                  'fundamentals_report', 'trader_investment_decision',
                  'investment_plan'];

    for (var fi = 0; fi < fields.length; fi++) {
      var field = fields[fi];
      var container = findMdBodyForField(field);
      if (!container) continue;
      var oldText = prev ? prev[field] : null;
      var oldSet = buildOldLineSet(oldText);
      if (!oldSet) continue; // first visit — no baseline, no underlines

      // Walk rendered block children. Marking at block level (p, li, td, h*,
      // pre, blockquote) keeps underlines aligned to marked.js output
      // instead of raw markdown lines.
      var blocks = container.querySelectorAll('p, li, h1, h2, h3, h4, h5, h6, pre, blockquote, tr');
      for (var bi = 0; bi < blocks.length; bi++) {
        var block = blocks[bi];
        // Skip nested li children (we already underline the parent li).
        if (block.parentElement && block.parentElement.closest('li') &&
            block.parentElement.closest('li') !== block) continue;
        var text = normalizeLine(block.textContent);
        if (text.length < 4) continue; // skip very short lines / noise
        if (!oldSet[text]) {
          block.classList.add('diff-underline');
        }
      }
    }

    // Persist current text as the new baseline for next visit.
    storeTextSnapshot(ticker, state);
  }

  // ---------- bootstrap ----------
  function bootstrap() {
    var state = global.__SELECTED_STATE__;
    var ticker = global.__SELECTED_TICKER__;
    if (state && ticker) {
      // Small delay so DOM is fully rendered by marked.js
      setTimeout(function () {
        runDiff(state, ticker);
        // Per-analyst content underline (must run AFTER marked.js so DOM
        // blocks exist; 500ms delay above covers that).
        runContentDiff(state, ticker);
      }, 500);
    }
  }

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', bootstrap);
    } else {
      bootstrap();
    }
  }

  // Expose for manual trigger after AJAX refresh
  global.runPageDiff = runDiff;
  global.runContentDiff = runContentDiff;

})((typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
