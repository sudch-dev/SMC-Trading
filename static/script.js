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
    const res = await fetch('/api/smc-status', {
      // some Safari builds get grumpy with 'no-store'
      cache: 'reload'      // or just remove the cache option
    });

    // Robust parse: prefer JSON; otherwise read text and throw a readable error
    const ct = (res.headers.get('content-type') || '').toLowerCase();
    let data;
    if (ct.includes('application/json')) {
      data = await res.json();
    } else {
      const text = await res.text();
      throw new Error(`Non-JSON response (${res.status}) ${text.slice(0, 200)}`);
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
    // Show the *actual* reason instead of Safari's vague SyntaxError
    for (const k in buckets) {
      buckets[k].innerHTML = `<li>Error: ${String(e).replace(/</g,'&lt;')}</li>`;
    }
    errors.textContent = String(e);
  }
}

window.addEventListener('load', loadScan);
