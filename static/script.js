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

  if (!confirm(`Place ${action} ${qty} of ${symbol}?\nTP: ${isNaN(tp)?'—':tp}\nSL: ${isNaN(sl)?'—':sl}\n(Entry will be LIMIT; server picks price)`)) return;

  // optional UX: prevent double-clicks
  btn.disabled = true; btn.textContent = 'Placing…';

  try {
    const res = await fetch('/api/execute', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        symbol,
        action,
        quantity: qty,
        order_type: 'LIMIT',      // ← IMPORTANT: always LIMIT now
        // price: omit → backend computes from best bid/ask or ltp fallback
        with_tp_sl: true,
        tp: isNaN(tp) ? null : tp,
        sl: isNaN(sl) ? null : sl,
        product: 'NRML'
      })
    });
    const data = await res.json();

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
    btn.disabled = false; btn.textContent = 'Execute (with TP/SL)';
  }
}
