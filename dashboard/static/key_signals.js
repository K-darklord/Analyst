/* TradingAgents Dashboard -- Key Signals extraction
 * Pure-regex/text parsing module that extracts structured tradeable data
 * (price targets, fundamentals, sentiment, key positives/negatives) from
 * the agent report markdown. No LLM call -- deterministic and instant.
 *
 * Round 4 fixes:
 *   - Strict price validation (reject %, billion/million/trillion, year-like)
 *   - Range validation ($1-$100000)
 *   - Range syntax "$4 to $280" prefers upper bound
 *   - Diff visualization vs localStorage last-visited state per ticker
 *
 * Public API:
 *   extractKeySignals(state)  -> { price_targets, fundamentals, sentiment,
 *                                  key_positives, key_negatives, news_headlines,
 *                                  rating_text }
 *   renderKeySignals(state)   -> renders into #keySignalsCard (auto-diffs)
 *   diffStates(oldSig, newSig) -> change descriptor
 *   applyDiffHighlights(diff, ticker) -> DOM pulse/badges
 *   storeLastState(ticker, sig) / loadLastState(ticker) -> localStorage
 */
(function (global) {
  'use strict';

  // ---------- price validation (Round 4 fix) ----------
  // Tokens that, when found within 30 chars of a candidate price number,
  // indicate it is NOT a stock price (it's a market cap, EPS, percent, etc.).
  var NON_PRICE_TOKENS = /(\b%|\bpercent\b|\bbps\b|\bbillion\b|\bmillion\b|\btrillion\b|\bBn\b|\bMn\b|\bB\b|\bM\b|亿|万|%)/i;

  // Strict price validator. Returns the number if it plausibly is a stock
  // price, otherwise null. Rejects:
  //   - out of range (< $1 or > $100000)
  //   - year-like numbers (1990-2099)
  //   - numbers whose surrounding context contains %, billion, million, etc.
  function validatePrice(num, contextText, matchStart, matchEnd) {
    if (num == null || isNaN(num)) return null;
    if (num < 1 || num > 100000) return null;
    // Reject year-like numbers (1990-2099).
    if (num >= 1990 && num <= 2099) return null;
    if (!contextText) return num;
    // Before-window (5 chars): only reject tokens immediately preceding the
    // number, so "billion"/"%" belonging to a DIFFERENT nearby number don't
    // wrongly reject a valid price (e.g. "market cap $4 billion, target $280").
    var beforePart = contextText.slice(Math.max(0, matchStart - 5), matchStart);
    // After-window: up to 30 chars, truncated at the first sentence boundary
    // (. ! ? ; newline) so tokens in a LATER clause (e.g. "$110. By 2026 EPS
    // growth 4.5%") don't reject the current price. Rejection tokens like
    // "billion"/"%" follow the number WITHIN its own clause ("$4 billion").
    var rawAfter = contextText.slice(matchEnd, matchEnd + 30);
    var sb = rawAfter.search(/[.!?\n;]/);
    var afterPart = (sb !== -1) ? rawAfter.slice(0, sb) : rawAfter;
    if (NON_PRICE_TOKENS.test(beforePart + afterPart)) return null;
    return num;
  }

  // Scan text for the first valid price that follows the given label regex.
  // labelRegex is a regex fragment like 'target(?:\\s*price)?' (caller-controlled).
  // Returns { value, start, end } or null. Skips invalid candidates.
  function findValidPrice(text, labelRegex) {
    if (!text) return null;
    // Build a global regex that finds the label, then captures the next
    // number (optionally preceded by $ or USD). Use 'g' flag to iterate.
    var re = new RegExp(
      '(?:' + labelRegex + ')[^\\d]{0,10}(?:\\$|USD\\s*)?(\\d{1,5}(?:\\.\\d{1,4})?)',
      'gi'
    );
    var m;
    while ((m = re.exec(text)) !== null) {
      var raw = parseFloat(m[1]);
      if (isNaN(raw)) continue;
      // m.index is start of whole match (label). The number is at the
      // captured group; compute its offset from m.index + offset of group.
      var numStart = m.index + m[0].length - m[1].length;
      var numEnd = numStart + m[1].length;
      var ok = validatePrice(raw, text, numStart, numEnd);
      if (ok != null) {
        // Negative context filter: skip prices in cautionary context
        var before20 = text.slice(Math.max(0, m.index - 20), m.index);
        if (/(?:避免|不要|而非|勿|不应|不可|不要因|别因为)/.test(before20)) continue;
        return { value: ok, start: numStart, end: numEnd };
      }
    }
    return null;
  }

  // Handle "$4 to $280" / "$4-$280" range syntax: when a label is followed by
  // TWO price-like numbers, prefer the second (the upper bound). Returns the
  // first VALID price, but if a "to"/"-" range is present, prefer the upper.
  function findValidPriceWithRange(text, labelRegex) {
    if (!text) return null;
    var first = findValidPrice(text, labelRegex);
    if (!first) return null;
    // Look ahead 25 chars after the first number for "to" or "-" then a
    // second number; if that second number is valid, prefer it.
    var after = text.slice(first.end, first.end + 30);
    var rangeM = /^\s*(?:to|-|–|—|~)\s*(?:\$|USD\s*)?(\d{1,5}(?:\.\d{1,4})?)/i.exec(after);
    if (rangeM) {
      var raw2 = parseFloat(rangeM[1]);
      if (!isNaN(raw2)) {
        var s2 = first.end + after.indexOf(rangeM[1]);
        var e2 = s2 + rangeM[1].length;
        var ok2 = validatePrice(raw2, text, s2, e2);
        if (ok2 != null) return { value: ok2, start: s2, end: e2 };
      }
    }
    return first;
  }

  // Reverse search: find $NUMBER ... LABEL (price appears BEFORE label).
  // Used when forward search (LABEL ... NUMBER) finds nothing.
  function findValidPriceReverse(text, labelRegex) {
    if (!text) return null;
    var re = new RegExp(
      '(?:\\$|USD\\s*)?(\\d{1,5}(?:\\.\\d{1,4})?)[^\\d]{0,25}(?:' + labelRegex + ')',
      'gi'
    );
    var m;
    while ((m = re.exec(text)) !== null) {
      var raw = parseFloat(m[1]);
      if (isNaN(raw)) continue;
      var numStart = m.index + (m[0].indexOf(m[1]));
      var numEnd = numStart + m[1].length;
      var ok = validatePrice(raw, text, numStart, numEnd);
      if (ok != null) {
        // Negative context filter
        var before20 = text.slice(Math.max(0, numStart - 20), numStart);
        if (/(?:避免|不要|而非|勿|不应|不可|不要因|别因为)/.test(before20)) continue;
        return { value: ok, start: numStart, end: numEnd };
      }
    }
    return null;
  }

  // Search a list of fields in priority order for a price labeled by labelRegex.
  function searchPriceFields(state, fields, labelRegex) {
    for (var i = 0; i < fields.length; i++) {
      var txt = state && state[fields[i]];
      if (!txt) continue;
      var hit = findValidPriceWithRange(String(txt), labelRegex);
      if (hit) return hit.value;
      // Fallback: reverse search ($NUMBER ... LABEL)
      var revHit = findValidPriceReverse(String(txt), labelRegex);
      if (revHit) return revHit.value;
    }
    return null;
  }

  // ---------- field aggregation ----------
  var PRICE_FIELDS = [
    'final_trade_decision',
    'trader_investment_decision',
    'investment_plan',
    'market_report',
    'fundamentals_report',
  ];
  var FUND_FIELDS = [
    'fundamentals_report',
    'market_report',
    'final_trade_decision',
    'trader_investment_decision',
    'investment_plan',
  ];
  var SENTIMENT_FIELDS = ['sentiment_report', 'news_report', 'final_trade_decision'];
  var BULLET_FIELDS = [
    'final_trade_decision',
    'trader_investment_decision',
    'investment_plan',
    'market_report',
    'sentiment_report',
    'news_report',
    'fundamentals_report',
  ];
  var NEWS_FIELDS = ['news_report', 'market_report'];

  function get(state, key) {
    var v = state && state[key];
    if (v == null) return '';
    return String(v);
  }

  function concatFields(state, keys) {
    return keys.map(function (k) { return get(state, k); }).join('\n\n');
  }

  // ---------- price targets ----------
  function extractPriceTargets(state) {
    return {
      entry_price:   searchPriceFields(state, PRICE_FIELDS, 'entry(?:\\s*price)?|入场价|买入价'),
      target_price:  searchPriceFields(state, PRICE_FIELDS, 'target(?:\\s*price)?|price\\s*target|目标价|目标位'),
      stop_loss:     searchPriceFields(state, PRICE_FIELDS, 'stop\\s*loss|止损'),
      take_profit:   searchPriceFields(state, PRICE_FIELDS, 'take\\s*profit|止盈'),
      support:       searchPriceFields(state, PRICE_FIELDS, 'support|支撑'),
      resistance:    searchPriceFields(state, PRICE_FIELDS, 'resistance|阻力'),
    };
  }

  // ---------- fundamentals ----------
  function extractFundamentals(state) {
    var fundText = concatFields(state, FUND_FIELDS);
    function grab(labelPattern) {
      var re = new RegExp(labelPattern, 'i');
      var m = re.exec(fundText);
      if (!m || !m[1]) return null;
      var n = parseFloat(m[1].replace(/,/g, ''));
      return isNaN(n) ? null : n;
    }
    return {
      pe_ratio:       grab('(?:P/E(?:\\s*ratio)?|PE(?:\\s*\\(TTM\\))?|市盈率)[^\\d]{0,15}(\\d{1,4}(?:\\.\\d{1,4})?)'),
      pb_ratio:       grab('(?:P/B(?:\\s*ratio)?|市净率)[^\\d]{0,15}(\\d{1,4}(?:\\.\\d{1,4})?)'),
      roe:            grab('(?:ROE|净资产收益率)[^\\d]{0,15}(\\d{1,4}(?:\\.\\d{1,4})?)'),
      eps:            grab('(?:EPS|每股收益)[^\\d]{0,15}(\\$?\\s*-?\\d{1,5}(?:\\.\\d{1,4})?)'),
      debt_to_equity: grab('(?:D\\s*/\\s*E|debt\\s*to\\s*equity|债务[/／]权益|资产负债率)[^\\d]{0,15}(\\d{1,4}(?:\\.\\d{1,4})?)'),
      revenue_growth: grab('(?:revenue\\s*growth|营收(?:增长|同比)|收入(?:增长|同比))[^\\d]{0,15}(-?\\d{1,3}(?:\\.\\d{1,4})?)\\s*%'),
    };
  }

  // ---------- sentiment ----------
  function extractSentiment(state) {
    var sentText = concatFields(state, SENTIMENT_FIELDS);
    var bullish = null, bearish = null;
    // Prefer reverse pattern (XX% Bullish) — handles "40% Bullish vs 20% Bearish"
    var mBullRev = /(\d{1,3}(?:\.\d+)?)\s*%\s*(?:bullish|看多)/i.exec(sentText);
    if (mBullRev) bullish = parseFloat(mBullRev[1]);
    var mBearRev = /(\d{1,3}(?:\.\d+)?)\s*%\s*(?:bearish|看空)/i.exec(sentText);
    if (mBearRev) bearish = parseFloat(mBearRev[1]);
    // Fallback: forward pattern (Bullish XX%) if reverse not found
    if (bullish === null) {
      var mBull = /(?:bullish|看多)[^\d]{0,15}(\d{1,3}(?:\.\d+)?)\s*%/i.exec(sentText);
      if (mBull) bullish = parseFloat(mBull[1]);
    }
    if (bearish === null) {
      var mBear = /(?:bearish|看空)[^\d]{0,15}(\d{1,3}(?:\.\d+)?)\s*%/i.exec(sentText);
      if (mBear) bearish = parseFloat(mBear[1]);
    }

    var overall = null;
    var ovMatch = /\b(bullish|bearish|neutral)\b/i.exec(sentText);
    if (ovMatch) {
      overall = ovMatch[1].charAt(0).toUpperCase() + ovMatch[1].slice(1).toLowerCase();
    }
    if (!overall && bullish !== null && bearish !== null) {
      overall = bullish > bearish ? 'Bullish' : (bearish > bullish ? 'Bearish' : 'Neutral');
    }
    return { bullish_pct: bullish, bearish_pct: bearish, overall: overall };
  }

  // ---------- bullets (positives / negatives) ----------
  var POS_KW = ['strong', 'growth', 'bullish', 'beat', 'outperform', 'upside',
    'above', 'gain', 'record', 'positive', '上', '增', '强', '好', '利', '涨'];
  var NEG_KW = ['weak', 'decline', 'bearish', 'miss', 'underperform', 'downside',
    'below', 'loss', 'drop', 'negative', 'risk', 'concern', '下', '降', '弱', '差', '跌'];

  function scoreLine(line, kws) {
    var low = line.toLowerCase();
    var s = 0;
    for (var i = 0; i < kws.length; i++) {
      if (low.indexOf(kws[i]) !== -1) s += 1;
    }
    return s;
  }

  function extractBullets(state) {
    var allText = concatFields(state, BULLET_FIELDS);
    var lines = allText.split(/\r?\n/);
    var bullets = [];
    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i].trim();
      if (!ln) continue;
      if (!/^([-•*]|\d+\.)\s+/.test(ln)) continue;
      var body = ln.replace(/^([-•*]|\d+\.)\s+/, '');
      if (body.length < 3) continue;
      bullets.push({
        text: body.slice(0, 200),
        posScore: scoreLine(body, POS_KW),
        negScore: scoreLine(body, NEG_KW),
      });
    }
    var seen = {};
    var uniq = [];
    for (var j = 0; j < bullets.length; j++) {
      var key = bullets[j].text.toLowerCase().slice(0, 80);
      if (seen[key]) continue;
      seen[key] = true;
      uniq.push(bullets[j]);
    }
    var pos = uniq.filter(function (b) { return b.posScore > 0; })
                  .sort(function (a, b) { return b.posScore - a.posScore; })
                  .slice(0, 5)
                  .map(function (b) { return b.text; });
    var neg = uniq.filter(function (b) { return b.negScore > 0; })
                  .sort(function (a, b) { return b.negScore - a.negScore; })
                  .slice(0, 5)
                  .map(function (b) { return b.text; });
    return { positives: pos, negatives: neg };
  }

  // ---------- news headlines ----------
  function extractNewsHeadlines(state) {
    var newsText = get(state, 'news_report') || get(state, 'market_report');
    if (!newsText) return [];
    var lines = newsText.split(/\r?\n/);
    var heads = [];
    for (var i = 0; i < lines.length && heads.length < 3; i++) {
      var ln = lines[i].trim();
      if (!ln) continue;
      var isHead = /^#{1,2}\s+/.test(ln) || /^\*\*[^*]{5,}\*\*/.test(ln);
      if (!isHead) continue;
      var body = ln.replace(/^#{1,2}\s+/, '').replace(/^\*\*|\*\*$/g, '').trim();
      if (body.length < 5) continue;
      heads.push(body.slice(0, 80));
    }
    return heads;
  }

  // ---------- main extractor ----------
  function extractKeySignals(state) {
    state = state || {};
    var pt = extractPriceTargets(state);
    var fund = extractFundamentals(state);
    var sent = extractSentiment(state);
    var bns = extractBullets(state);
    var news = extractNewsHeadlines(state);
    var ratingText = '';
    var fd = get(state, 'final_trade_decision');
    if (fd) {
      // Pull first ~60 chars of final decision as the rating text.
      ratingText = fd.slice(0, 60).replace(/\s+/g, ' ').trim();
    }
    return {
      price_targets: pt,
      fundamentals: fund,
      sentiment: sent,
      key_positives: bns.positives,
      key_negatives: bns.negatives,
      news_headlines: news,
      rating_text: ratingText,
    };
  }

  // ---------- rendering ----------
  function esc(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function fmtPrice(n, prev, cls) {
    if (n == null) return '<span class="ks-metric-value muted">&mdash;</span>';
    var c = cls ? ' ' + cls : '';
  var html = '<span class="ks-metric-value' + c + '">$' + n.toFixed(2) + '</span>';
    if (prev != null && prev !== n) {
      html += ' <span class="ks-was-line"><span class="ks-was-label">was</span> ' +
              '<span class="ks-was-strike">$' + prev.toFixed(2) + '</span></span>';
    }
    return html;
  }

  function fmtFund(n, suffix, cls) {
    if (n == null) return '<span class="ks-metric-value muted">&mdash;</span>';
    var s = suffix ? ' ' + suffix : '';
    var c = cls ? ' ' + cls : '';
    return '<span class="ks-metric-value' + c + '">' + n.toFixed(2) + s + '</span>';
  }

  function fmtSentiment(sent) {
    if (!sent) return '<span class="ks-metric-value muted">&mdash;</span>';
    var parts = [];
    if (sent.overall) {
      var cls = sent.overall === 'Bullish' ? 'bull' :
                (sent.overall === 'Bearish' ? 'bear' : 'neutral');
      parts.push('<span class="ks-sentiment-tag ks-sentiment-' + cls + '">' + esc(sent.overall) + '</span>');
    }
    if (sent.bullish_pct != null) parts.push('<span class="ks-pct bull">Bull ' + sent.bullish_pct + '%</span>');
    if (sent.bearish_pct != null) parts.push('<span class="ks-pct bear">Bear ' + sent.bearish_pct + '%</span>');
    return parts.length ? parts.join(' ') : '<span class="ks-metric-value muted">&mdash;</span>';
  }

  function bulletList(items, prevItems, diff) {
    if (!items || !items.length) return '<div class="ks-bullet-empty muted">—</div>';
    var prevSet = {};
    if (prevItems) for (var i = 0; i < prevItems.length; i++) prevSet[prevItems[i]] = true;
    var html = '<ul class="ks-bullet-list">';
    for (var j = 0; j < items.length; j++) {
      var isNew = !prevSet[items[j]];
      var badge = isNew ? ' <span class="ks-new-badge">NEW</span>' : '';
      html += '<li>' + esc(items[j]) + badge + '</li>';
    }
    html += '</ul>';
    return html;
  }

  function renderKeySignals(state) {
    var card = document.getElementById('keySignalsCard');
    if (!card) return;
    var sig = extractKeySignals(state);
    var ticker = (state && state.ticker) || (window.__SELECTED_TICKER__ || '');
    var prev = loadLastState(ticker);
    var diff = prev ? diffStates(prev, sig) : null;

    var pt = sig.price_targets;
    var prevPt = prev ? prev.price_targets : null;
    var fund = sig.fundamentals;
    var sent = sig.sentiment;

    var html = '';
    html += '<div class="ks-section ks-prices">';
    html += '<div class="ks-section-title">Price Targets</div>';
    html += '<div class="ks-metric-grid">';
    html += '<div class="ks-metric-cell"><div class="ks-metric-label">Entry</div>' + fmtPrice(pt.entry_price, prevPt && prevPt.entry_price, 'amber') + '</div>';
    html += '<div class="ks-metric-cell"><div class="ks-metric-label">Target</div>' + fmtPrice(pt.target_price, prevPt && prevPt.target_price, 'amber') + '</div>';
    html += '<div class="ks-metric-cell"><div class="ks-metric-label">Stop Loss</div>' + fmtPrice(pt.stop_loss, prevPt && prevPt.stop_loss, 'bear') + '</div>';
    html += '<div class="ks-metric-cell"><div class="ks-metric-label">Take Profit</div>' + fmtPrice(pt.take_profit, prevPt && prevPt.take_profit, 'bull') + '</div>';
    html += '<div class="ks-metric-cell"><div class="ks-metric-label">Support</div>' + fmtPrice(pt.support, prevPt && prevPt.support, 'bull') + '</div>';
    html += '<div class="ks-metric-cell"><div class="ks-metric-label">Resistance</div>' + fmtPrice(pt.resistance, prevPt && prevPt.resistance, 'bear') + '</div>';
    html += '</div></div>';

    html += '<div class="ks-section ks-fundamentals">';
    html += '<div class="ks-section-title">Fundamentals</div>';
    html += '<div class="ks-metric-grid">';
    html += '<div class="ks-metric-cell"><div class="ks-metric-label">P/E</div>' + fmtFund(fund.pe_ratio, null, 'amber') + '</div>';
    html += '<div class="ks-metric-cell"><div class="ks-metric-label">P/B</div>' + fmtFund(fund.pb_ratio, null, 'amber') + '</div>';
    html += '<div class="ks-metric-cell"><div class="ks-metric-label">ROE</div>' + fmtFund(fund.roe, '%', 'bull') + '</div>';
    html += '<div class="ks-metric-cell"><div class="ks-metric-label">EPS</div>' + fmtFund(fund.eps, null, 'bull') + '</div>';
    html += '<div class="ks-metric-cell"><div class="ks-metric-label">D/E</div>' + fmtFund(fund.debt_to_equity, null, 'bear') + '</div>';
    html += '<div class="ks-metric-cell"><div class="ks-metric-label">Rev Growth</div>' + fmtFund(fund.revenue_growth, '%', 'bull') + '</div>';
    html += '</div></div>';

    html += '<div class="ks-section ks-sentiment">';
    html += '<div class="ks-section-title">Sentiment</div>';
    html += '<div class="ks-sentiment-row">' + fmtSentiment(sent) + '</div>';
    html += '</div>';

    html += '<div class="ks-section ks-positives">';
    html += '<div class="ks-section-title ks-pos">Key Positives</div>';
    html += bulletList(sig.key_positives, prev && prev.key_positives, diff);
    html += '</div>';

    html += '<div class="ks-section ks-negatives">';
    html += '<div class="ks-section-title ks-neg">Key Negatives</div>';
    html += bulletList(sig.key_negatives, prev && prev.key_negatives, diff);
    html += '</div>';

    if (sig.news_headlines && sig.news_headlines.length) {
      html += '<div class="ks-section ks-news">';
      html += '<div class="ks-section-title">News Headlines</div>';
      html += '<ul class="ks-bullet-list">';
      for (var i = 0; i < sig.news_headlines.length; i++) {
        html += '<li>' + esc(sig.news_headlines[i]) + '</li>';
      }
      html += '</ul></div>';
    }

    card.innerHTML = html;

    // Persist current state as the new "last visited" baseline.
    storeLastState(ticker, sig);

    // Apply diff highlights (pulse + badges) for 10 seconds.
    if (diff) applyDiffHighlights(diff, ticker);
  }

  // ---------- diff ----------
  function diffStates(oldSig, newSig) {
    if (!oldSig || !newSig) return null;
    var oldPt = oldSig.price_targets || {};
    var newPt = newSig.price_targets || {};
    var priceFields = ['entry_price', 'target_price', 'stop_loss', 'take_profit', 'support', 'resistance'];
    var priceChanged = {};
    var anyPriceChanged = false;
    for (var i = 0; i < priceFields.length; i++) {
      var k = priceFields[i];
      var changed = (oldPt[k] != null || newPt[k] != null) && (oldPt[k] !== newPt[k]);
      priceChanged[k] = changed;
      if (changed) anyPriceChanged = true;
    }
    var oldSent = oldSig.sentiment || {};
    var newSent = newSig.sentiment || {};
    var sentimentChanged = (oldSent.overall || '') !== (newSent.overall || '');

    var oldPos = oldSig.key_positives || [];
    var newPos = newSig.key_positives || [];
    var oldPosSet = {};
    for (var p = 0; p < oldPos.length; p++) oldPosSet[oldPos[p]] = true;
    var posAdded = [];
    for (var q = 0; q < newPos.length; q++) {
      if (!oldPosSet[newPos[q]]) posAdded.push(newPos[q]);
    }

    var oldNeg = oldSig.key_negatives || [];
    var newNeg = newSig.key_negatives || [];
    var oldNegSet = {};
    for (var r = 0; r < oldNeg.length; r++) oldNegSet[oldNeg[r]] = true;
    var negAdded = [];
    for (var s = 0; s < newNeg.length; s++) {
      if (!oldNegSet[newNeg[s]]) negAdded.push(newNeg[s]);
    }

    var ratingChanged = (oldSig.rating_text || '') !== (newSig.rating_text || '');

    return {
      rating_changed: ratingChanged,
      price_targets_changed: anyPriceChanged,
      price_changes: priceChanged,
      sentiment_changed: sentimentChanged,
      positives_added: posAdded,
      negatives_added: negAdded,
      has_changes: ratingChanged || anyPriceChanged || sentimentChanged ||
                   posAdded.length > 0 || negAdded.length > 0,
    };
  }

  function applyDiffHighlights(diff, ticker) {
    if (!diff || !diff.has_changes) return;
    var card = document.getElementById('keySignalsCard');
    if (!card) return;

    // Pulse the whole card for 10s.
    card.classList.add('ks-pulse-amber');
    setTimeout(function () {
      card.classList.remove('ks-pulse-amber');
    }, 10000);

    // Pulse individual price cells that changed.
    if (diff.price_targets_changed) {
      var cells = card.querySelectorAll('.ks-prices .ks-metric-cell');
      var keys = ['entry_price', 'target_price', 'stop_loss', 'take_profit', 'support', 'resistance'];
      for (var i = 0; i < cells.length && i < keys.length; i++) {
        if (diff.price_changes[keys[i]]) {
          cells[i].classList.add('ks-pulse-amber');
          (function (el) {
            setTimeout(function () { el.classList.remove('ks-pulse-amber'); }, 10000);
          })(cells[i]);
        }
      }
    }

    // "NEW" badges are already injected by bulletList() during render.
    // Schedule badge fade-out at 10s.
    var badges = card.querySelectorAll('.ks-new-badge');
    for (var j = 0; j < badges.length; j++) {
      (function (el) {
        setTimeout(function () {
          el.classList.add('ks-fade-out');
          setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 600);
        }, 10000);
      })(badges[j]);
    }

    // Sentiment pulse.
    if (diff.sentiment_changed) {
      var sentRow = card.querySelector('.ks-sentiment-row');
      if (sentRow) {
        sentRow.classList.add('ks-pulse-amber');
        setTimeout(function () { sentRow.classList.remove('ks-pulse-amber'); }, 10000);
      }
    }
  }

  // ---------- localStorage ----------
  var LS_PREFIX = 'ta-last-state-';

  function storeLastState(ticker, sig) {
    if (!ticker || !sig) return;
    try {
      global.localStorage.setItem(LS_PREFIX + ticker, JSON.stringify(sig));
    } catch (e) {
      // localStorage may be unavailable (private mode, SSR); ignore.
    }
  }

  function loadLastState(ticker) {
    if (!ticker) return null;
    try {
      var raw = global.localStorage.getItem(LS_PREFIX + ticker);
      if (!raw) return null;
      return JSON.parse(raw);
    } catch (e) {
      return null;
    }
  }

  // ---------- bootstrap ----------
  function bootstrap() {
    var state = (global.__SELECTED_STATE__ != null) ? global.__SELECTED_STATE__ : null;
    if (state) renderKeySignals(state);
  }

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', bootstrap);
    } else {
      bootstrap();
    }
  }

  // ---------- exports ----------
  global.extractKeySignals = extractKeySignals;
  global.renderKeySignals = renderKeySignals;
  global.diffStates = diffStates;
  global.applyDiffHighlights = applyDiffHighlights;
  global.storeLastState = storeLastState;
  global.loadLastState = loadLastState;

  // Test exports for Node verification.
  global._ta_test = {
    validatePrice: validatePrice,
    findValidPrice: findValidPrice,
    findValidPriceWithRange: findValidPriceWithRange,
    searchPriceFields: searchPriceFields,
    extractPriceTargets: extractPriceTargets,
    diffStates: diffStates,
  };

})((typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
