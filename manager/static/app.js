// Tiny dependency-free sparkline for the Disks page.
(function () {
  const css = getComputedStyle(document.documentElement);
  const line = css.getPropertyValue("--accent").trim() || "#4c8dff";
  const dim = css.getPropertyValue("--dim").trim() || "#8a92a1";
  const grid = css.getPropertyValue("--border").trim() || "#2a2f3a";

  document.querySelectorAll("canvas.spark").forEach(async (cv) => {
    const host = cv.dataset.host, mount = cv.dataset.mount;
    let data;
    try {
      const r = await fetch(`/disks/series?host=${encodeURIComponent(host)}&mount=${encodeURIComponent(mount)}`);
      data = (await r.json()).series || [];
    } catch (e) { return; }
    if (data.length < 2) { cv.replaceWith(Object.assign(document.createElement("p"),
        { className: "dim", textContent: "Not enough history yet for a trend." })); return; }

    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.height;
    cv.width = w * dpr; cv.height = h * dpr;
    const ctx = cv.getContext("2d");
    ctx.scale(dpr, dpr);

    const pct = data.map(d => d.total_bytes ? (d.used_bytes / d.total_bytes) * 100 : 0);
    const ts = data.map(d => d.ts);
    const minP = Math.max(0, Math.min(...pct) - 3), maxP = Math.min(100, Math.max(...pct) + 3);
    const x = t => ((t - ts[0]) / (ts[ts.length - 1] - ts[0] || 1)) * (w - 44) + 4;
    const y = p => h - 16 - ((p - minP) / (maxP - minP || 1)) * (h - 26);

    ctx.strokeStyle = grid; ctx.fillStyle = dim; ctx.font = "10px system-ui"; ctx.lineWidth = 1;
    [minP, maxP].forEach(p => {
      ctx.beginPath(); ctx.moveTo(4, y(p)); ctx.lineTo(w - 40, y(p)); ctx.stroke();
      ctx.fillText(p.toFixed(0) + "%", w - 34, y(p) + 3);
    });

    ctx.strokeStyle = line; ctx.lineWidth = 1.6; ctx.beginPath();
    pct.forEach((p, i) => i ? ctx.lineTo(x(ts[i]), y(p)) : ctx.moveTo(x(ts[i]), y(p)));
    ctx.stroke();

    const span = (ts[ts.length - 1] - ts[0]) / 3600;
    ctx.fillStyle = dim;
    ctx.fillText(span >= 48 ? `${(span / 24).toFixed(0)}d` : `${span.toFixed(0)}h`, 4, h - 3);
  });
})();
