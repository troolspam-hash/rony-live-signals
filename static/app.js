(function() {
  var cards = [];
  var knownSignalIds = {};
  var signalAlertReady = false;
  var signalPollInitialized = false;
  var dashboardVersion = null;
  var historyVersion = null;
  var isAuthenticated = document.body && document.body.dataset.authenticated === '1';

  function collectCards() {
    cards = Array.prototype.slice.call(document.querySelectorAll('.signal-card[data-symbol]'));
  }

  collectCards();
  cards.forEach(function(card) {
    if (card.dataset.signalId) knownSignalIds[card.dataset.signalId] = true;
  });

  function unlockSignalAlert() {
    var audio = document.getElementById('signalAlertAudio');
    if (!audio || signalAlertReady) return;
    audio.volume = 0.85;
    audio.play().then(function() {
      audio.pause();
      audio.currentTime = 0;
      signalAlertReady = true;
    }).catch(function() {
      signalAlertReady = true;
    });
  }

  document.addEventListener('click', unlockSignalAlert, { once: true });
  document.addEventListener('keydown', unlockSignalAlert, { once: true });

  function fmtPrice(value) {
    if (!Number.isFinite(value)) return '--';
    if (value >= 1000) return value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (value >= 1) return value.toLocaleString('en-US', { minimumFractionDigits: 4, maximumFractionDigits: 4 });
    return value.toFixed(8);
  }

  function fmtPct(value) {
    if (!Number.isFinite(value)) return '--';
    var sign = value >= 0 ? '+' : '';
    return sign + value.toFixed(2) + '%';
  }

  function buildSparkPath(values, width, height, pad) {
    if (!values.length) return { line: '', area: '', min: NaN, max: NaN };
    var min = Math.min.apply(null, values);
    var max = Math.max.apply(null, values);
    var span = max - min || 1;
    var points = values.map(function(value, idx) {
      var x = values.length === 1 ? 0 : idx / (values.length - 1) * width;
      var y = pad + (max - value) / span * (height - pad * 2);
      return [x, y];
    });
    var line = points.map(function(point, idx) {
      return (idx === 0 ? 'M' : 'L') + point[0].toFixed(2) + ' ' + point[1].toFixed(2);
    }).join(' ');
    var area = 'M0 ' + height.toFixed(2) + ' ' + points.map(function(point) {
      return 'L' + point[0].toFixed(2) + ' ' + point[1].toFixed(2);
    }).join(' ') + ' L' + width.toFixed(2) + ' ' + height.toFixed(2) + ' Z';
    return { line: line, area: area, min: min, max: max };
  }

  function drawMarketSpark(el, klines) {
    var closes = [];
    klines.forEach(function(row) {
      closes.push(Number(row[4]));
    });
    if (!closes.length) return;
    var spark = buildSparkPath(closes, 160, 70, 5);
    var line = el.querySelector('.spark-path');
    var area = el.querySelector('.spark-area');
    if (line) line.setAttribute('d', spark.line);
    if (area) area.setAttribute('d', spark.area);
  }

  async function updateMarketSparks() {
    await Promise.all(Array.prototype.slice.call(document.querySelectorAll('[data-ticker]')).map(async function(el) {
      try {
        var url = 'https://fapi.binance.com/fapi/v1/klines?symbol=' + encodeURIComponent(el.dataset.ticker) + '&interval=1h&limit=80';
        var res = await fetch(url, { cache: 'no-store' });
        if (!res.ok) return;
        drawMarketSpark(el, await res.json());
      } catch (err) {
        console.warn('market spark update failed', err);
      }
    }));
  }

  function symbols() {
    var set = {};
    collectCards();
    cards.forEach(function(card) { set[card.dataset.symbol] = true; });
    document.querySelectorAll('[data-ticker]').forEach(function(el) { set[el.dataset.ticker] = true; });
    return Object.keys(set);
  }

  function calcPnl(card, price) {
    var entry = Number(card.dataset.entry);
    var direction = card.dataset.direction;
    if (!entry || !price) return 0;
    if (direction === 'short') return (entry - price) / entry * 100;
    return (price - entry) / entry * 100;
  }

  function calcMarkerPosition(card, price) {
    var entry = Number(card.dataset.entry);
    var tp = Number(card.dataset.tp);
    var sl = Number(card.dataset.sl);
    if (!entry || !tp || !sl || !price) return 0;
    var direction = card.dataset.direction;
    var pnl = calcPnl(card, price);
    var tpDist = Math.abs(tp - entry);
    var slDist = Math.abs(sl - entry);
    var distance = Math.abs(price - entry);
    if (pnl >= 0) {
      var profitRatio = tpDist ? Math.min(1, distance / tpDist) : 0;
      return direction === 'short' ? 50 - profitRatio * 50 : 50 + profitRatio * 50;
    }
    var lossRatio = slDist ? Math.min(1, distance / slDist) : 0;
    return direction === 'short' ? 50 + lossRatio * 50 : 50 - lossRatio * 50;
  }

  async function update() {
    try {
      collectCards();
      var list = symbols();
      var prices = {};
      await Promise.all(list.map(async function(symbol) {
        var res = await fetch('https://fapi.binance.com/fapi/v1/premiumIndex?symbol=' + encodeURIComponent(symbol), { cache: 'no-store' });
        if (!res.ok) return;
        var data = await res.json();
        prices[symbol] = Number(data.markPrice || data.price);
      }));

      document.querySelectorAll('[data-ticker]').forEach(function(el) {
        var price = prices[el.dataset.ticker];
        var value = el.querySelector('.market-price b');
        if (value && Number.isFinite(price)) value.textContent = fmtPrice(price);
      });

      cards.forEach(function(card) {
        var price = prices[card.dataset.symbol];
        if (!Number.isFinite(price)) return;
        var pnl = calcPnl(card, price);
        var pnlEl = card.querySelector('.live-pnl');
        var priceEl = card.querySelector('.live-price');
        var thumb = card.querySelector('.range-thumb');
        if (pnlEl) {
          pnlEl.textContent = fmtPct(pnl);
          pnlEl.classList.toggle('positive', pnl >= 0);
          pnlEl.classList.toggle('negative', pnl < 0);
        }
        if (priceEl) priceEl.textContent = 'mark $' + fmtPrice(price);
        if (thumb) {
          thumb.style.left = calcMarkerPosition(card, price).toFixed(1) + '%';
          thumb.classList.toggle('positive', pnl >= 0);
          thumb.classList.toggle('negative', pnl < 0);
        }
      });
    } catch (err) {
      console.warn('live price update failed', err);
    }
  }

  async function checkNewSignals() {
    var audio = document.getElementById('signalAlertAudio');
    if (!audio) return;
    try {
      var res = await fetch('/api/open-signals', { cache: 'no-store' });
      if (!res.ok) return;
      var rows = await res.json();
      var hasNew = false;
      rows.forEach(function(row) {
        if (!row.id) return;
        if (!signalPollInitialized) {
          knownSignalIds[row.id] = true;
          return;
        }
        if (!knownSignalIds[row.id]) {
          knownSignalIds[row.id] = true;
          hasNew = true;
        }
      });
      signalPollInitialized = true;
      if (hasNew) {
        audio.currentTime = 0;
        audio.play().catch(function() {
          console.warn('alert sound blocked until user interaction');
        });
      }
    } catch (err) {
      console.warn('signal alert check failed', err);
    }
  }

  function currentDashboardQuery() {
    var params = new URLSearchParams(window.location.search || '');
    return '?asset=' + encodeURIComponent(params.get('asset') || 'all') +
      '&direction=' + encodeURIComponent(params.get('direction') || 'all');
  }

  async function refreshDashboardFragments() {
    if (!document.querySelector('[data-refresh-block="signals"]')) return;
    try {
      var res = await fetch('/api/dashboard-fragments' + currentDashboardQuery(), { cache: 'no-store' });
      if (!res.ok) return;
      var data = await res.json();
      if (!data || data.version === dashboardVersion) return;
      dashboardVersion = data.version;

      var stats = document.querySelector('[data-refresh-block="stats"]');
      var summary = document.querySelector('[data-refresh-block="summary"]');
      var signals = document.querySelector('[data-refresh-block="signals"]');
      if (stats && data.stats_html) stats.outerHTML = data.stats_html;
      if (summary && data.summary_html) summary.outerHTML = data.summary_html;
      if (signals && data.signals_html) signals.outerHTML = data.signals_html;

      collectCards();
      update();
    } catch (err) {
      console.warn('dashboard refresh failed', err);
    }
  }

  async function refreshHistoryFragments() {
    if (!document.querySelector('[data-refresh-block="history"]')) return;
    try {
      var res = await fetch('/api/history-fragments' + (window.location.search || ''), { cache: 'no-store' });
      if (!res.ok) return;
      var data = await res.json();
      if (!data || data.version === historyVersion) return;
      historyVersion = data.version;
      var history = document.querySelector('[data-refresh-block="history"]');
      if (history && data.history_html) history.outerHTML = data.history_html;
    } catch (err) {
      console.warn('history refresh failed', err);
    }
  }

  async function loadHistoryFragment(url, pushState) {
    if (!document.querySelector('[data-refresh-block="history"]')) return;
    var target = new URL(url, window.location.origin);
    try {
      var res = await fetch('/api/history-fragments' + target.search, { cache: 'no-store' });
      if (!res.ok) return;
      var data = await res.json();
      var history = document.querySelector('[data-refresh-block="history"]');
      if (history && data.history_html) history.outerHTML = data.history_html;
      historyVersion = data.version || null;
      if (pushState) window.history.pushState({}, '', '/history' + target.search);
    } catch (err) {
      console.warn('history page load failed', err);
    }
  }

  document.addEventListener('click', function(event) {
    var link = event.target.closest('[data-refresh-block="history"] .pager a');
    if (!link || link.classList.contains('disabled')) return;
    event.preventDefault();
    loadHistoryFragment(link.href, true);
  });

  document.addEventListener('submit', function(event) {
    var form = event.target.closest('.history-board .filters');
    if (!form) return;
    event.preventDefault();
    var params = new URLSearchParams(new FormData(form));
    params.set('page', '1');
    loadHistoryFragment('/history?' + params.toString(), true);
  });

  document.addEventListener('change', function(event) {
    var select = event.target.closest('[data-refresh-block="history"] .per-page select');
    if (!select) return;
    event.preventDefault();
    var form = select.form;
    var params = new URLSearchParams(new FormData(form));
    params.set('page', '1');
    loadHistoryFragment('/history?' + params.toString(), true);
  });

  window.addEventListener('popstate', function() {
    if (document.querySelector('[data-refresh-block="history"]')) {
      loadHistoryFragment(window.location.href, false);
    }
  });

  async function checkSession() {
    if (!isAuthenticated) return;
    try {
      var res = await fetch('/api/session-check', {
        cache: 'no-store',
        credentials: 'same-origin',
        redirect: 'manual'
      });
      if (res.status === 401 || res.status === 403 || res.type === 'opaqueredirect' || res.status === 0) {
        window.location.href = '/login';
        return;
      }
      if (!res.ok) return;
      var contentType = res.headers.get('content-type') || '';
      if (contentType.indexOf('application/json') === -1) {
        window.location.href = '/login';
        return;
      }
      var data = await res.json();
      if (!data.active) window.location.href = '/login';
    } catch (err) {
      console.warn('session check failed', err);
    }
  }

  update();
  updateMarketSparks();
  if (isAuthenticated) {
    checkNewSignals();
    refreshDashboardFragments();
    refreshHistoryFragments();
    checkSession();
  }
  setInterval(update, 4000);
  setInterval(updateMarketSparks, 60000);
  if (isAuthenticated) {
    setInterval(checkNewSignals, 5000);
    setInterval(refreshDashboardFragments, 7000);
    setInterval(refreshHistoryFragments, 10000);
    setInterval(checkSession, 10000);
  }
})();
