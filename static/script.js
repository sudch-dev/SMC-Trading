/* ============ SMC Dashboard Frontend ============ */
/* Renders picks, places LIMIT entries, and passes LONG/SHORT intent to server. */

function rowHTML(p) {
  const strike = p.strike != null ? ` ${p.strike}` : '';
  const lots = p.suggested_lots ?? 0;
  const tp = p.tp != null ? p.tp.toFixed(2) : '—';
  const sl = p.sl != null ? p.sl.toFixed(2) : '—';

  return `
    <div class="row">
      <div><strong>${p.name} ${p.type}${strike}</strong>
        <span class="pill">${p.trade_type}</span>
      </div>
      <div class="muted">LTP ₹${(p.ltp||0).toFixed(2)} • Lot ${p.lot_size}</div>
      <div class="muted">TP ₹${tp} • SL ₹${sl}</div>
      <div class="muted">Why: ${p.reason ? p.reason.replace(/</g,'&lt;') : ''}</div>
      <div class="muted">Entry will be placed as <b>LIMIT</b> (server picks price)</div>
      <div class="row">
        <label>Lots:</label>
        <input type="number" min="0" step="1" value="${lots}"
               data-sym="${p.symbol}"
               data-lot="${p.lot_size}"
               data-action="${p.entry_action || (p.trade_type === 'LONG' ? 'BUY' : 'SELL')}"
               data-type="${p.type}"                   <!-- CE / PE -->
               data-trade_type="${p.trade_type}"       <!-- LONG / SHORT -->
               data-tp="${p.tp ?? ''}" data-sl="${p.sl ?? ''}">
        <button class="btn" onclick="execOrder(this)">${p.trade_type === 'LONG' ? 'BUY' : 'SELL'} (with TP/SL)</button>
      </div>
    </div>
  `;
}

async function execOrder(btn) {
  const input = btn.previousElementSibling;
  const lots = parseInt(input.value || '0', 10);
  const lotSize = parseInt(input.getAttribute('data-lot') || '1', 10);
  const qty = lots * lotSize;

  if (!Number.isFinite(qty) || qty <= 0) {
    alert('Enter lots > 0');
    return;
  }

  const symbol = input.getAttribute('data-sym');
  const actionFallback = input.getAttribute('data-action');  // legacy
  const optType = input.getAttribute('data-type');           // CE / PE
  const tradeType = input.getAttribute('data-trade_type');   // LONG / SHORT
  const tp = parseFloat(input.getAttribute('data-tp') || 'NaN');
  const sl = parseFloat(input.getAttribute('data-sl') || 'NaN');

  const verb = (tradeType === 'LONG') ? 'BUY' : 'SELL';
  if (!confirm(
    `Place ${verb} ${qty} of ${symbol} [${tradeType} ${optType}]?\n` +
    `TP: ${isNaN(tp)?'—':tp}\nSL: ${isNaN(sl)?'—':sl}\n(Entry LIMIT; server picks price)`
  )) return;

  btn.disabled = true; const oldText = btn.textContent; btn.textContent = 'Placing…';

  try {
    const res = await fetch('/api/execute', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        symbol,
        action: actionFallback,   // server will override if trade_type provided
        type: optType,            // CE / PE
        trade_type: tradeType,    // LONG / SHORT  <-- prevents action drift
        quantity: qty,
        order_type: 'LIMIT',      // always LIMIT now
        with_tp_sl: true,
        tp: isNaN(tp) ? null : tp,
        sl: isNaN(sl) ? null : sl,
        product: 'NRML'
      })
    });

    let data;
    const ct = (res.headers.get('content-type') || '').toLowerCase();
    if (ct.includes('application/json')) {
      data = await res.json();
    } else {
      const text = await res.text();
      throw new Error(`Non-JSON response (${res.status}): ${text.slice(0, 200)}`);
    }

    if (data.status === 'ok') {
      const lines = [
        `Entry OK`,
        `Order: ${data.entry_order_id}`,
        `Used LIMIT: ₹${data.used_limit_price ?? '—'}`
      ];
      if (data.tp_order_id) lines.push(`TP: ${data.tp_order_id} @ ₹${data.tp_price}`);
      if (data.sl_order_id) lines.push(`SL: ${data.sl_order_id} trig ₹${data.sl_trigger} / lim ₹${data.sl_price}`);
      if (data.tp_error)   lines.push(`TP error: ${data.tp_error}`);
      if (data.sl_error)   lines.push(`SL error: ${data.sl_error}`);
      alert(lines.join('\n'));
    } else {
      alert(`Error: ${data.error || JSON.stringify(data)}`);
    }
  } catch (e) {
    alert('Error: ' + e);
  } finally {
    btn.disabled = false; btn.textContent = oldText;
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
    const res = await fetch('/api/smc-status', { cache: 'reload' });
    let data;
    const ct = (res.headers.get('content-type') || '').toLowerCase();
    if (ct.includes('application/json')) {
      data = await res.json();
    } else {
      const text = await res.text();
      throw new Error(`Non-JSON response (${res.status}): ${text.slice(0, 200)}`);
    }

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
    for (const k in buckets) {
      buckets[k].innerHTML = `<li>Error: ${String(e).replace(/</g,'&lt;')}</li>`;
    }
    errors.textContent = String(e);
  }
}

window.addEventListener('load', loadScan);
