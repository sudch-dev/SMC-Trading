
async function fetchSMC() {
    const res = await fetch('/api/smc-status');
    const data = await res.json();
    const buyList = document.getElementById('buy-list');
    const sellList = document.getElementById('sell-list');
    buyList.innerHTML = '';
    sellList.innerHTML = '';
    for (let [symbol, info] of Object.entries(data)) {
        const li = document.createElement('li');
        li.innerHTML = `<b>${symbol}</b> – ₹${info.price} (Zone: ${info.zone[0]}–${info.zone[1]})<br>
        EMA20: ${info.ema20} | EMA50: ${info.ema50} | RSI: ${info.rsi} | Trend: ${info.trend}`;
        if (info.status.includes("Buy")) {
            buyList.appendChild(li);
        } else {
            sellList.appendChild(li);
        }
    }
}
setInterval(fetchSMC, 60000);
fetchSMC();
