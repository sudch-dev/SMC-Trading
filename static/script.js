function rowHTML(p) {
  const strike = p.strike != null ? ` ${p.strike}` : '';
  const lots = p.suggested_lots ?? 0;
  const qty = lots * (p.lot_size || 1);
  const tp = p.tp != null ? p.tp.toFixed(2) : '—';
  const sl = p.sl != null ? p.sl.toFixed(2) : '—';

  return `
    <div class="row">
      <div><strong>${p.name} ${p.type}${strike}</strong>
        <span class="pill">${p.trade_type}</span>
      </div>
      <div class="muted">LTP ₹${(p.ltp||0).toFixed(2)} • Lot ${p.lot_size}</div>
      <div class="muted">TP ₹${tp} • SL ₹${sl}</div>
      <div class="muted">Why: ${p.reason || ''}</div>
      <div class="row">
        <label>Lots:</label>
        <input type="number" min="0" step="1" value="${lots}" data-sym="${p.symbol}" data-lot="${p.lot_size}"
               data-action="${p.entry_action || (p.trade_type === 'LONG' ? 'BUY' : 'SELL')}"
               data-tp="${p.tp || ''}" data-sl="${p.sl || ''}">
        <button class="btn" onclick="execOrder(this)">Execute (with TP/SL)</button>
      </div>
    </div>
  `;
}

async function execOrder(btn) {
  const input = btn.previousElementSibling;
  const lots = parseInt(input.value || '0', 10);
  const lotSize = parseInt(input.getAttribute('data-lot') || '1', 10);
  const qty = lots * lotSize;

  if (qty <= 0) {
    alert('Enter lots > 0');
    return;
  }

  const symbol = input.getAttribute('data-sym');
  const action = input.getAttribute('data-action'); // BUY/SELL
  const tp = parseFloat(input.getAttribute('data-tp') || 'NaN');
  const sl = parseFloat(input.getAttribute('data-sl') || 'NaN');

  if (!confirm(`Place ${action} ${qty} of ${symbol}?\nTP: ${isNaN(tp)?'—':tp}\nSL: ${isNaN(sl)?'—':sl}`)) return;

  try {
    const res = await fetch('/api/execute', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        symbol,
        action,
        quantity: qty,
        order_type: 'MARKET',
        with_tp_sl: true,
        tp: isNaN(tp) ? null : tp,
        sl: isNaN(sl) ? null : sl,
        product: 'NRML'
      })
    });
    const data = await res.json();
    alert(JSON.stringify(data, null, 2));
  } catch (e) {
    alert('Error: ' + e);
  }
}

async function loadScan() {
  const meta = document.getElementById('meta');
  const diag = document.getElementById('diag');
  const errors = document.getElementById('errors');

  const buckets = {
    'LONG_CE': document.getElementById('long-ce'),
    'SHORT_CE': document.getElementById('short-ce'),
    'LONG_PE': document.getElementById('long-pe'),
    'SHORT_PE': document.getElementById('short-pe'),
  };
  for (const k in buckets) buckets[k].innerHTML = '<li>Loading…</li>';

  try {
    const res = await fetch('/api/smc-status', { cache: 'no-store' });
    const data = await res.json();

    meta.textContent = `Status: ${data.status || 'ok'} | Time: ${data.ts || ''} | Budget: ₹${data.budget ?? ''}`;
    diag.textContent = JSON.stringify(data.diag || {}, null, 2);
    errors.textContent = (data.errors && data.errors.length) ? data.errors.join(' | ') : '';

    for (const k in buckets) buckets[k].innerHTML = '';

    const picks = Array.isArray(data.picks) ? data.picks : [];
    const longCE = picks.filter(p => p.type === 'CE' && p.trade_type === 'LONG').slice(0, 12);
    const shortCE = picks.filter(p => p.type === 'CE' && p.trade_type === 'SHORT').slice(0, 12);
    const longPE = picks.filter(p => p.type === 'PE' && p.trade_type === 'LONG').slice(0, 12);
    const shortPE = picks.filter(p => p.type === 'PE' && p.trade_type === 'SHORT').slice(0, 12);

    const fill = (arr, node) => {
      if (!arr.length) { node.innerHTML = '<li>No ideas</li>'; return; }
      node.innerHTML = arr.map(p => `<li>${rowHTML(p)}</li>`).join('');
    };
    fill(longCE, buckets.LONG_CE);
    fill(shortCE, buckets.SHORT_CE);
    fill(longPE, buckets.LONG_PE);
    fill(shortPE, buckets.SHORT_PE);

  } catch (e) {
    for (const k in buckets) buckets[k].innerHTML = `<li>Error: ${e}</li>`;
  }
}

window.addEventListener('load', loadScan);
