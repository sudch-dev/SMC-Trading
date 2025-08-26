async function loadScan() {
  const buyList = document.getElementById('buy-list');
  const sellList = document.getElementById('sell-list');
  const meta = document.getElementById('meta');
  const diag = document.getElementById('diag');
  const errors = document.getElementById('errors');

  buyList.innerHTML = '<li>Loading…</li>';
  sellList.innerHTML = '';

  try {
    const res = await fetch('/api/smc-status', { cache: 'no-store' });
    const data = await res.json();

    meta.textContent = `Status: ${data.status || 'unknown'} | Time: ${data.ts || ''} | Budget: ₹${data.budget ?? ''}`;
    diag.textContent = JSON.stringify(data.diag || {}, null, 2);
    errors.textContent = (data.errors && data.errors.length) ? data.errors.join(' | ') : '';

    const picks = Array.isArray(data.picks) ? data.picks : [];
    buyList.innerHTML = '';
    sellList.innerHTML = '';

    if (picks.length === 0) {
      buyList.innerHTML = '<li>No picks returned.</li>';
      sellList.innerHTML = '<li>No picks returned.</li>';
      return;
    }

    for (const p of picks) {
      const li = document.createElement('li');
      const strike = p.strike != null ? ` ${p.strike}` : '';
      li.textContent = `${p.name} ${p.type}${strike}  @ ₹${(p.ltp || 0).toFixed(2)}  | lots=${p.suggested_lots} | ${p.reason || ''}`;
      if ((p.status || '').toLowerCase() === 'buy') {
        buyList.appendChild(li);
      } else {
        sellList.appendChild(li);
      }
    }

    if (!buyList.children.length) buyList.innerHTML = '<li>No Buy ideas</li>';
    if (!sellList.children.length) sellList.innerHTML = '<li>No Sell ideas</li>';

  } catch (e) {
    buyList.innerHTML = `<li>Error: ${e}</li>`;
  }
}

window.addEventListener('load', loadScan);
