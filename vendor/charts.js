/* charts.js — 轻量本地图表库（离线可用）
 * 替代 Chart.js CDN，兼容本系统用到的子集：
 * bar（分组/堆叠/横向）、line（含填充）、混合 bar+line 双轴、radar、doughnut
 * 接口对齐 Chart.js 4：new Chart(ctx, config)、chart.destroy()、responsive
 */
(function (global) {
  'use strict';

  var registry = [];
  var PALETTE = ['#4a90d9', '#f39c12', '#2ecc71', '#e67e22', '#9b59b6', '#7f8c8d', '#e74c3c', '#1abc9c'];

  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function num(v) { return isNum(v) ? v : 0; }
  function fmt(v) { return String(Math.round(v * 10) / 10); }
  function color(c, i) { return c || PALETTE[i % PALETTE.length]; }

  function dataRange(datasets, axisId) {
    var min = Infinity, max = -Infinity;
    datasets.forEach(function (ds) {
      if ((ds.yAxisID || 'y') !== axisId) return;
      (ds.data || []).forEach(function (v) {
        if (!isNum(v)) return;
        if (v < min) min = v;
        if (v > max) max = v;
      });
    });
    if (min === Infinity) { min = 0; max = 1; }
    if (min === max) { min = Math.min(0, min); max = min + 1; }
    return { min: min, max: max };
  }

  function axisRange(datasets, axisId, axisCfg) {
    var r = dataRange(datasets, axisId);
    var cfg = axisCfg || {};
    var stacked = !!(cfg.stacked);
    if (stacked) {
      var sum = 0;
      datasets.forEach(function (ds) {
        if ((ds.yAxisID || 'y') !== axisId) return;
        var mx = 0;
        (ds.data || []).forEach(function (v) { if (isNum(v)) mx = Math.max(mx, num(v)); });
        sum += mx;
      });
      r = { min: 0, max: Math.max(sum, 1) };
    }
    if (isNum(cfg.min)) r.min = cfg.min;
    if (isNum(cfg.max)) r.max = cfg.max;
    if (cfg.beginAtZero && r.min > 0) r.min = 0;
    return r;
  }

  function niceMax(v) {
    if (v <= 0) return 1;
    var mag = Math.pow(10, Math.floor(Math.log10(v)));
    var n = v / mag;
    var step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
    return step * mag;
  }

  function ticks(r, count) {
    var hi = niceMax(r.max);
    var out = [];
    for (var i = 0; i <= count; i++) out.push(Math.round((hi * i / count) * 10) / 10);
    return out;
  }

  function measureCtx(ctx, text, font) {
    ctx.save();
    ctx.font = font;
    var w = ctx.measureText(String(text)).width;
    ctx.restore();
    return w;
  }

  function canvasSize(canvas) {
    var parent = canvas.parentElement;
    var w = parent ? parent.clientWidth : 600;
    if (w < 120) w = 600;
    var h = Math.round(w * 0.42);
    var cs = global.getComputedStyle ? global.getComputedStyle(canvas) : null;
    if (cs && cs.maxHeight && cs.maxHeight !== 'none') {
      var cap = parseFloat(cs.maxHeight);
      if (cap > 0) h = Math.min(h, cap);
    }
    return { w: w, h: h };
  }

  function setupCanvas(canvas, padding) {
    var s = canvasSize(canvas);
    var dpr = global.devicePixelRatio || 1;
    canvas.style.width = s.w + 'px';
    canvas.style.height = s.h + 'px';
    if (canvas.width !== Math.round(s.w * dpr) || canvas.height !== Math.round(s.h * dpr)) {
      canvas.width = Math.round(s.w * dpr);
      canvas.height = Math.round(s.h * dpr);
    }
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, s.w, s.h);
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, s.w, s.h);
    var pad = {
      top: 18, right: 16, bottom: 34, left: padding || 44
    };
    return { ctx: ctx, w: s.w, h: s.h, pad: pad };
  }

  function drawLegend(ctx, w, h, pad, labels, display, fontSize) {
    if (display === false || !labels || !labels.length) return;
    ctx.font = (fontSize || 11) + 'px sans-serif';
    ctx.textBaseline = 'middle';
    var y = h - pad.bottom / 2;
    var x = pad.left;
    labels.forEach(function (lb, i) {
      var text = lb.label !== undefined ? lb.label : lb;
      var bw = measureCtx(ctx, text, ctx.font) + 18;
      if (x + bw > w - pad.right && x > pad.left) { x = pad.left; y += 16; }
      ctx.fillStyle = lb.color;
      ctx.fillRect(x, y - 4, 10, 8);
      ctx.fillStyle = '#555';
      ctx.textAlign = 'left';
      ctx.fillText(text, x + 14, y);
      x += bw + 8;
    });
  }

  function drawNoData(ctx, w, h) {
    ctx.fillStyle = '#999';
    ctx.font = '13px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('暂无数据', w / 2, h / 2);
  }

  function drawBar(cfg) {
    var ctx = cfg.ctx, w = cfg.w, h = cfg.h, pad = cfg.pad;
    var opts = cfg.options || {};
    var labels = cfg.labels || [];
    var datasets = cfg.datasets || [];
    var horizontal = !!(opts.indexAxis === 'y');
    var xStacked = !!(opts.scales && opts.scales.x && opts.scales.x.stacked);
    var yStacked = !!(opts.scales && opts.scales.y && opts.scales.y.stacked);

    var plotW = w - pad.left - pad.right;
    var plotH = h - pad.top - pad.bottom;

    if (!labels.length || !datasets.length) { drawNoData(ctx, w, h); return; }

    var rY = axisRange(datasets, 'y', opts.scales && opts.scales.y);
    var rY1 = axisRange(datasets, 'y1', opts.scales && opts.scales.y1);
    var yTicks = ticks(rY, 4);
    var y1Ticks = ticks(rY1, 4);

    var n = labels.length;
    var slot = plotW / n;
    var groupW = slot * 0.7;

    function yPos(v, r) {
      return pad.top + plotH - (v - r.min) / (r.max - r.min) * plotH;
    }

    // 网格 + 左轴刻度
    ctx.strokeStyle = '#eee';
    ctx.fillStyle = '#888';
    ctx.font = '10px sans-serif';
    yTicks.forEach(function (t) {
      var yy = yPos(t, rY);
      ctx.beginPath();
      ctx.moveTo(pad.left, yy);
      ctx.lineTo(w - pad.right, yy);
      ctx.stroke();
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      ctx.fillText(fmt(t), pad.left - 6, yy);
    });
    // 右轴刻度（如有数据集使用 y1）
    var hasY1 = datasets.some(function (d) { return (d.yAxisID || 'y') === 'y1'; });
    if (hasY1) {
      ctx.strokeStyle = '#f2f2f2';
      ctx.fillStyle = '#b8860b';
      y1Ticks.forEach(function (t) {
        var yy = yPos(t, rY1);
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText(fmt(t), w - pad.right + 5, yy);
      });
    }
    // x 轴标签
    ctx.fillStyle = '#666';
    ctx.font = '10px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    var skip = Math.max(1, Math.ceil(n / 14));
    for (var li = 0; li < n; li += skip) {
      var lx = pad.left + slot * li + slot / 2;
      ctx.fillText(String(labels[li]), lx, h - pad.bottom + 6);
    }
    // 轴标题
    drawAxisTitles(ctx, w, h, pad, opts);

    var hasLineDs = datasets.some(function (d) { return (d.type || 'bar') === 'line'; });

    // 柱
    datasets.forEach(function (ds, di) {
      if ((ds.type || 'bar') === 'line') return;
      var axis = (ds.yAxisID || 'y') === 'y1' ? 'y1' : 'y';
      var r = axis === 'y1' ? rY1 : rY;
      var per = datasets.filter(function (d) { return (d.type || 'bar') === 'bar'; }).length || 1;
      var bw = hasLineDs ? slot * 0.55 : groupW / per;
      (ds.data || []).forEach(function (v, i) {
        var val = num(v);
        var x0 = pad.left + slot * i + (slot - groupW) / 2;
        var bx;
        if (horizontal) {
          var bh = slot * 0.6;
          var by0 = pad.top + slot * i + (slot - bh) / 2;
          var len = val / r.max * plotW;
          ctx.fillStyle = color(ds.backgroundColor, di);
          ctx.fillRect(pad.left, by0, len, bh);
          ctx.fillStyle = '#555';
          ctx.textAlign = 'left';
          ctx.textBaseline = 'middle';
          ctx.fillText(fmt(val), pad.left + len + 4, by0 + bh / 2);
          return;
        }
        if (xStacked && yStacked) {
          bx = x0;
        } else if (xStacked) {
          var accum = 0;
          for (var pdi = 0; pdi < di; pdi++) {
            var pds = datasets[pdi];
            if ((pds.type || 'bar') !== 'bar') continue;
            accum += num((pds.data || [])[i]);
          }
          var y1p = yPos(accum + val, r);
          var y0p = yPos(accum, r);
          ctx.fillStyle = color(ds.backgroundColor, di);
          ctx.fillRect(x0, Math.min(y0p, y1p), groupW, Math.abs(y0p - y1p));
          return;
        } else {
          bx = x0 + bw * di;
        }
        var yv = yPos(val, r);
        var yb = yPos(0, r);
        ctx.fillStyle = color(ds.backgroundColor, di);
        ctx.fillRect(bx, Math.min(yv, yb), bw, Math.max(Math.abs(yv - yb), 1));
      });
    });

    // 折线
    datasets.forEach(function (ds, di) {
      if ((ds.type || 'bar') !== 'line') return;
      var axis = (ds.yAxisID || 'y') === 'y1' ? 'y1' : 'y';
      var r = axis === 'y1' ? rY1 : rY;
      var pts = [];
      (ds.data || []).forEach(function (v, i) {
        pts.push({ x: pad.left + slot * i + slot / 2, y: yPos(num(v), r), v: v });
      });
      var lc = color(ds.borderColor, di);
      if (ds.fill) {
        ctx.beginPath();
        ctx.moveTo(pts[0] ? pts[0].x : pad.left, yPos(0, r));
        pts.forEach(function (p) { ctx.lineTo(p.x, p.y); });
        var last = pts[pts.length - 1];
        ctx.lineTo(last ? last.x : pad.left + plotW, yPos(0, r));
        ctx.closePath();
        ctx.fillStyle = ds.backgroundColor || 'rgba(46,204,113,.1)';
        ctx.fill();
      }
      ctx.beginPath();
      pts.forEach(function (p, i) { if (i === 0) ctx.moveTo(p.x, p.y); else ctx.lineTo(p.x, p.y); });
      ctx.strokeStyle = lc;
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.fillStyle = lc;
      pts.forEach(function (p) {
        ctx.beginPath();
        ctx.arc(p.x, p.y, 2.5, 0, Math.PI * 2);
        ctx.fill();
      });
    });

    drawLegend(ctx, w, h, pad,
      datasets.map(function (ds, i) {
        return { label: ds.label || '', color: color(ds.backgroundColor, i) };
      }),
      opts.plugins && opts.plugins.legend ? opts.plugins.legend.display : true,
      opts.plugins && opts.plugins.legend && opts.plugins.legend.labels ? opts.plugins.legend.labels.font.size : 11);
  }

  function drawLine(cfg) {
    drawBar(cfg); // 纯 line 复用 bar 的网格与折线逻辑
  }

  function drawAxisTitles(ctx, w, h, pad, opts) {
    var sc = opts.scales || {};
    ctx.fillStyle = '#888';
    ctx.font = '11px sans-serif';
    if (sc.x && sc.x.title && sc.x.title.display !== false && sc.x.title.text) {
      ctx.textAlign = 'center';
      ctx.textBaseline = 'bottom';
      ctx.fillText(String(sc.x.title.text), pad.left + (w - pad.left - pad.right) / 2, h - 2);
    }
    if (sc.y && sc.y.title && sc.y.title.display !== false && sc.y.title.text) {
      ctx.save();
      ctx.translate(12, pad.top + (h - pad.top - pad.bottom) / 2);
      ctx.rotate(-Math.PI / 2);
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillText(String(sc.y.title.text), 0, 0);
      ctx.restore();
    }
  }

  function drawRadar(cfg) {
    var ctx = cfg.ctx, w = cfg.w, h = cfg.h, pad = cfg.pad;
    var labels = cfg.labels || [];
    var ds = (cfg.datasets && cfg.datasets[0]) || { data: [] };
    var data = ds.data || [];
    var n = labels.length || data.length;
    if (!n) { drawNoData(ctx, w, h); return; }

    var cx = w / 2, cy = h / 2;
    var R = Math.min(w, h) / 2 - 44;
    var opts = cfg.options || {};
    var max = 1;
    if (opts.scales && opts.scales.r && isNum(opts.scales.r.max)) max = opts.scales.r.max;
    else max = Math.max.apply(null, data.concat([1]));

    ctx.strokeStyle = '#e0e0e0';
    ctx.lineWidth = 1;
    for (var ring = 1; ring <= 4; ring++) {
      var rr = R * ring / 4;
      ctx.beginPath();
      for (var i = 0; i <= n; i++) {
        var ang = -Math.PI / 2 + (i % n) * 2 * Math.PI / n;
        var x = cx + rr * Math.cos(ang), y = cy + rr * Math.sin(ang);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
    ctx.strokeStyle = '#ccc';
    for (var a2 = 0; a2 < n; a2++) {
      var ang2 = -Math.PI / 2 + a2 * 2 * Math.PI / n;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(cx + R * Math.cos(ang2), cy + R * Math.sin(ang2));
      ctx.stroke();
      ctx.fillStyle = '#666';
      ctx.font = '11px sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(String(labels[a2] || ''), cx + (R + 22) * Math.cos(ang2), cy + (R + 22) * Math.sin(ang2));
    }

    ctx.beginPath();
    for (var p2 = 0; p2 < n; p2++) {
      var val = Math.min(num(data[p2] || 0), max);
      var rr2 = val / max * R;
      var ang3 = -Math.PI / 2 + p2 * 2 * Math.PI / n;
      var px = cx + rr2 * Math.cos(ang3), py = cy + rr2 * Math.sin(ang3);
      if (p2 === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.closePath();
    ctx.fillStyle = ds.backgroundColor || 'rgba(74,144,217,.2)';
    ctx.fill();
    ctx.strokeStyle = ds.borderColor || '#4a90d9';
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.fillStyle = ds.pointBackgroundColor || ds.borderColor || '#4a90d9';
    for (var p3 = 0; p3 < n; p3++) {
      var v3 = Math.min(num(data[p3] || 0), max);
      var rr3 = v3 / max * R;
      var ang4 = -Math.PI / 2 + p3 * 2 * Math.PI / n;
      ctx.beginPath();
      ctx.arc(cx + rr3 * Math.cos(ang4), cy + rr3 * Math.sin(ang4), 3, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function drawDoughnut(cfg) {
    var ctx = cfg.ctx, w = cfg.w, h = cfg.h, pad = cfg.pad;
    var ds = (cfg.datasets && cfg.datasets[0]) || { data: [] };
    var labels = cfg.labels || [];
    var data = ds.data || [];
    var total = 0;
    data.forEach(function (v) { total += num(v); });
    var legendEntries = labels.map(function (lb, i) {
      return { label: lb, color: (ds.backgroundColor && ds.backgroundColor[i]) || PALETTE[i % PALETTE.length] };
    });

    if (total <= 0) { drawNoData(ctx, w, h); return; }

    var cx = w / 2, cy = (h - pad.bottom) / 2;
    var R = Math.min(w, h) / 2 - 40;
    var inner = R * 0.58;
    var start = -Math.PI / 2;
    data.forEach(function (v, i) {
      var frac = num(v) / total;
      var end = start + frac * Math.PI * 2;
      ctx.beginPath();
      ctx.arc(cx, cy, R, start, end);
      ctx.arc(cx, cy, inner, end, start, true);
      ctx.closePath();
      ctx.fillStyle = legendEntries[i].color;
      ctx.fill();
      if (frac > 0.06) {
        ctx.fillStyle = '#fff';
        ctx.font = '11px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        var mid = (start + end) / 2;
        ctx.fillText(fmt(v), cx + (R + inner) / 2 * Math.cos(mid), cy + (R + inner) / 2 * Math.sin(mid));
      }
      start = end;
    });
    var opts = cfg.options || {};
    drawLegend(ctx, w, h, pad, legendEntries,
      !(opts.plugins && opts.plugins.legend && opts.plugins.legend.display === false),
      opts.plugins && opts.plugins.legend && opts.plugins.legend.labels ? opts.plugins.legend.labels.font.size : 11);
  }

  var DRAWERS = {
    bar: drawBar,
    line: drawLine,
    radar: drawRadar,
    doughnut: drawDoughnut,
    pie: drawDoughnut
  };

  function Chart(ctx, config) {
    this.ctx = ctx;
    this.config = config || {};
    this.canvas = ctx.canvas;
    this._destroyed = false;
    this._onResize = this.draw.bind(this);
    if (global.addEventListener) global.addEventListener('resize', this._onResize);
    registry.push(this);
    this.draw();
  }

  Chart.prototype.draw = function () {
    if (this._destroyed) return;
    if (this.canvas && !this.canvas.isConnected) return;
    try {
      var conf = this.config;
      var type = conf.type || 'bar';
      var env = setupCanvas(this.canvas, type === 'radar' ? 50 : 44);
      var drawer = DRAWERS[type] || DRAWERS.bar;
      drawer({
        ctx: env.ctx, w: env.w, h: env.h, pad: env.pad,
        labels: (conf.data && conf.data.labels) || [],
        datasets: (conf.data && conf.data.datasets) || [],
        options: conf.options || {}
      });
    } catch (e) {
      if (global.console) console.warn('charts.js draw error:', e);
    }
  };

  Chart.prototype.destroy = function () {
    if (this._destroyed) return;
    this._destroyed = true;
    if (global.removeEventListener) global.removeEventListener('resize', this._onResize);
    var i = registry.indexOf(this);
    if (i >= 0) registry.splice(i, 1);
    if (this.canvas) {
      var c = this.canvas.getContext('2d');
      c && c.clearRect(0, 0, this.canvas.width, this.canvas.height);
    }
  };

  Chart.registry = registry;
  global.Chart = Chart;
})(typeof window !== 'undefined' ? window : this);
