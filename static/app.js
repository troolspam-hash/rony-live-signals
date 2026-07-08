(function() {
  var cards = Array.prototype.slice.call(document.querySelectorAll('.signal-card[data-symbol]'));

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

  function symbols() {
    var set = {};
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
        var value = el.querySelector('b');
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

  update();
  setInterval(update, 4000);
})();
