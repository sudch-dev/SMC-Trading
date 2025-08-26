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
      <div class="muted">Entry will be placed as <b>LIMIT</b> (server picks price)</div>
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
  // … full execOrder implementation we patched earlier …
}

async function loadScan() {
  // … full loadScan implementation we patched earlier …
}

window.addEventListener('load', loadScan);
