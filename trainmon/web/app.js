(function () {
  "use strict";

  var state = {
    runs: {},
    initialized: false,
    selected: null,
    alerts: false,
    stopped: false,
    lastStatus: {},
    audioCtx: null,
    sse: null,
    fallbackTimer: null,
    detailTimer: null,
    logTimer: null
  };

  function $(id) { return document.getElementById(id); }

  function escapeHtml(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined || isNaN(seconds)) return "-";
    seconds = Math.max(0, Math.floor(seconds));
    var h = Math.floor(seconds / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    var s = seconds % 60;
    if (h) return h + "h " + (m < 10 ? "0" : "") + m + "m";
    if (m) return m + "m " + (s < 10 ? "0" : "") + s + "s";
    return s + "s";
  }

  function elapsedSeconds(run) {
    var end = run.ended_at || Date.now() / 1000;
    var start = run.started_at || end;
    return Math.max(0, end - start);
  }

  function fmtClock(ts) {
    if (!ts) return "";
    try {
      return new Date(ts * 1000).toLocaleTimeString([], {
        hour: "2-digit", minute: "2-digit"
      });
    } catch (e) {
      return new Date(ts * 1000).toLocaleTimeString();
    }
  }

  function fmtEta(run) {
    if (run.status === "completed") return "已完成";
    if (run.status === "failed" || run.status === "lost") return statusLabel(run.status);
    if (run.status === "stalled") return "可能停滞";
    if (run.eta_seconds === null || run.eta_seconds === undefined) return "估算中";
    return fmtDuration(run.eta_seconds);
  }

  function confidenceText(value) {
    if (value === "high") return "高";
    if (value === "medium") return "中";
    return "低";
  }

  function finishText(run) {
    if (!run.eta_finish_at) return "";
    return "预计 " + fmtClock(run.eta_finish_at) + " 完成";
  }

  function etaRangeText(run, compact) {
    if (run.status === "completed") return "用时 " + fmtDuration(elapsedSeconds(run));
    if (run.status === "failed" || run.status === "lost") return statusLabel(run.status);
    if (run.status === "stalled") return "最后更新 " + relTime(run.last_update);
    if (run.eta_seconds === null || run.eta_seconds === undefined) return "等待更多数据";
    var low = run.eta_low;
    var high = run.eta_high;
    if (low !== null && low !== undefined && high !== null && high !== undefined && high > low) {
      if (compact && (high - low < 20 || high < low * 1.2)) return "";
      return "区间 " + fmtDuration(low) + " – " + fmtDuration(high);
    }
    if (low !== null && low !== undefined && (high === null || high === undefined)) {
      return "至少 " + fmtDuration(low);
    }
    return "";
  }

  function etaLineHtml(run) {
    if (run.status === "completed") {
      return '<div class="eta-line"><span class="eta-done">已完成</span>' +
        '<span class="eta-hint">用时 ' + fmtDuration(elapsedSeconds(run)) + '</span></div>';
    }
    if (run.status === "failed" || run.status === "lost") {
      return '<div class="eta-line"><span class="eta-bad">' + statusLabel(run.status) + '</span>' +
        '<span class="eta-hint">用时 ' + fmtDuration(elapsedSeconds(run)) + '</span></div>';
    }
    if (run.status === "stalled") {
      return '<div class="eta-line"><span class="eta-warn">可能卡住</span>' +
        '<span class="eta-hint">最后更新 ' + relTime(run.last_update) + '</span></div>';
    }
    if (run.eta_seconds === null || run.eta_seconds === undefined) {
      var canEstimate = run.total_steps ||
        (run.percent !== null && run.percent !== undefined) ||
        (run.total_epochs && run.epoch);
      return '<div class="eta-line"><span class="eta-hint">' +
        (canEstimate ? "正在估算剩余时间…" : "日志中没有总步数/总 epoch，无法估算剩余时间") +
        '</span></div>';
    }
    var hint = [finishText(run), etaRangeText(run, true)].filter(Boolean).join(" · ");
    return '<div class="eta-line">' +
      '<span class="eta-label">剩余</span>' +
      '<strong class="eta-value">' + fmtDuration(run.eta_seconds) + '</strong>' +
      (hint ? '<span class="eta-hint">' + hint + '</span>' : '') +
      '<span class="eta-conf">' + confidenceText(run.confidence) + '置信度</span>' +
      '</div>';
  }

  function fmtNum(value, digits) {
    if (value === null || value === undefined || value === "" || isNaN(value)) return "-";
    return Number(value).toFixed(digits === undefined ? 4 : digits);
  }

  function fmtTime(ts) {
    if (!ts) return "-";
    var d = new Date(ts * 1000);
    return d.toLocaleString();
  }

  function relTime(ts) {
    if (!ts) return "-";
    var diff = Math.max(0, Date.now() / 1000 - ts);
    if (diff < 60) return Math.floor(diff) + " 秒前";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
    return Math.floor(diff / 3600) + " 小时前";
  }

  function statusLabel(status) {
    var map = {
      running: "运行中",
      waiting: "等待中",
      stalled: "可能卡住",
      completed: "已完成",
      failed: "失败",
      lost: "进程丢失",
      terminated: "已终止"
    };
    return map[status] || status || "-";
  }

  function isTerminal(status) {
    return status === "completed" || status === "failed" || status === "lost" || status === "terminated";
  }

  function setConn(online, text) {
    var el = $("conn");
    el.className = "conn " + (online ? "online" : "offline");
    $("connText").textContent = text;
  }

  function fetchRuns() {
    if (state.stopped) return Promise.resolve(null);
    return fetch("/api/runs", { cache: "no-store" })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (data && data.runs) {
          setConn(true, "监控服务在线");
          applyRuns(data.runs);
        }
        return data;
      })
      .catch(function () {
        if (!state.stopped) setConn(false, "监控服务离线");
      });
  }

  function applyRuns(runs) {
    var next = {};
    runs.forEach(function (run) { next[run.run_id] = run; });
    if (state.initialized) {
      runs.forEach(function (run) {
        var old = state.lastStatus[run.run_id];
        if (old && old !== run.status && isTerminal(run.status)) {
          notifyRun(run);
        }
      });
    }
    state.runs = next;
    runs.forEach(function (run) { state.lastStatus[run.run_id] = run.status; });
    state.initialized = true;
    render();
  }

  function render() {
    var runs = Object.keys(state.runs).map(function (key) { return state.runs[key]; });
    var order = { running: 0, waiting: 0, stalled: 1, completed: 2, failed: 3, lost: 4, terminated: 5 };
    runs.sort(function (a, b) {
      var oa = order[a.status] === undefined ? 9 : order[a.status];
      var ob = order[b.status] === undefined ? 9 : order[b.status];
      if (oa !== ob) return oa - ob;
      return (b.started_at || 0) - (a.started_at || 0);
    });

    var running = 0, completed = 0, failed = 0;
    runs.forEach(function (run) {
      if (run.status === "running" || run.status === "waiting" || run.status === "stalled") running += 1;
      else if (run.status === "completed") completed += 1;
      else failed += 1;
    });
    $("countRunning").textContent = running;
    $("countCompleted").textContent = completed;
    $("countFailed").textContent = failed;
    $("countTotal").textContent = runs.length;
    document.title = running ? "(" + running + ") TrainMon" : "TrainMon";

    var container = $("runs");
    container.innerHTML = runs.map(renderCard).join("");
    $("empty").hidden = runs.length > 0;
    container.hidden = runs.length === 0;
  }

  function renderCard(run) {
    var pct = run.progress === null || run.progress === undefined
      ? 0 : Math.max(0, Math.min(100, run.progress * 100));
    var stepText = (run.step === null || run.step === undefined ? "-" : run.step) +
      " / " + (run.total_steps === null || run.total_steps === undefined ? "-" : run.total_steps);
    var last = run.error && isTerminal(run.status) ? run.error : (run.last_line || "等待日志输出…");
    var lastClass = run.error && isTerminal(run.status) ? "last-line error-line" : "last-line";
    var sub = escapeHtml(run.host || "") + (run.pid ? " · PID " + run.pid : "");
    return [
      '<article class="run-card status-' + escapeHtml(run.status) + '" data-id="' + escapeHtml(run.run_id) + '">',
      '  <div class="card-head">',
      '    <div><div class="card-name">' + escapeHtml(run.name) + '</div>',
      '    <div class="card-sub">' + sub + '</div></div>',
      '    <span class="pill ' + escapeHtml(run.status) + '">' + statusLabel(run.status) + '</span>',
      '  </div>',
      '  <div class="progress-row"><span class="pct">' + pct.toFixed(1) + '%</span>',
      '  <span class="steps">' + stepText + '</span></div>',
      '  <div class="bar"><i style="width:' + pct.toFixed(2) + '%"></i></div>',
      etaLineHtml(run),
      '  <div class="card-grid">',
      '    <div class="metric"><div class="k">速度</div><div class="v">' + (run.rate ? fmtNum(run.rate, 2) + "/s" : "-") + '</div></div>',
      '    <div class="metric"><div class="k">Loss</div><div class="v">' + fmtNum(run.loss) + '</div></div>',
      '    <div class="metric"><div class="k">已运行</div><div class="v">' + fmtDuration(elapsedSeconds(run)) + '</div></div>',
      '  </div>',
      '  <div class="' + lastClass + '" title="' + escapeHtml(run.last_line || "") + '">' + escapeHtml(last) + '</div>',
      '  <div class="card-sub" style="margin-top:6px">更新于 ' + relTime(run.last_update) + '</div>',
      '</article>'
    ].join("");
  }

  function openDetail(runId) {
    state.selected = runId;
    $("detail").hidden = false;
    document.body.style.overflow = "hidden";
    fetchDetail();
    fetchLog();
    if (state.detailTimer) clearInterval(state.detailTimer);
    if (state.logTimer) clearInterval(state.logTimer);
    state.detailTimer = setInterval(fetchDetail, 2000);
    state.logTimer = setInterval(fetchLog, 3000);
  }

  function closeDetail() {
    state.selected = null;
    $("detail").hidden = true;
    document.body.style.overflow = "";
    if (state.detailTimer) clearInterval(state.detailTimer);
    if (state.logTimer) clearInterval(state.logTimer);
    state.detailTimer = null;
    state.logTimer = null;
  }

  function fetchDetail() {
    if (!state.selected) return;
    fetch("/api/runs/" + encodeURIComponent(state.selected), { cache: "no-store" })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (data && data.run_id) renderDetail(data);
      })
      .catch(function () {});
  }

  function fetchLog() {
    if (!state.selected) return;
    fetch("/api/runs/" + encodeURIComponent(state.selected) + "/log?max_bytes=65536", { cache: "no-store" })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (!data || data.text === undefined) return;
        var pre = $("logTail");
        pre.textContent = data.text || "(没有日志)";
        if ($("autoScroll").checked) pre.scrollTop = pre.scrollHeight;
      })
      .catch(function () {});
  }

  function renderDetail(run) {
    $("detailName").textContent = run.name || run.run_id;
    $("detailMeta").textContent = (run.host || "") + " · PID " + (run.pid || "-") +
      " · 启动于 " + fmtTime(run.started_at);
    var pill = $("detailStatus");
    pill.className = "pill " + run.status;
    pill.textContent = statusLabel(run.status);
    $("downloadLog").href = "/api/runs/" + encodeURIComponent(run.run_id) + "/download";

    var elapsed = elapsedSeconds(run);
    var banner = $("etaBanner");
    banner.hidden = false;
    banner.className = "eta-banner " + run.status;
    var bigLabel = $("etaBigLabel");
    var big = $("etaBig");
    var sub = $("etaSub");
    if (run.status === "completed") {
      bigLabel.textContent = "已完成";
      big.textContent = "用时 " + fmtDuration(elapsed);
      sub.textContent = "结束于 " + fmtTime(run.ended_at);
    } else if (run.status === "failed" || run.status === "lost") {
      bigLabel.textContent = "状态";
      big.textContent = statusLabel(run.status);
      sub.textContent = run.error || ("用时 " + fmtDuration(elapsed));
    } else if (run.status === "stalled") {
      bigLabel.textContent = "当前状态";
      big.textContent = "可能卡住";
      sub.textContent = "最后更新 " + relTime(run.last_update) +
        (run.eta_seconds ? " · 此前预计剩余 " + fmtDuration(run.eta_seconds) : "");
    } else if (run.eta_seconds === null || run.eta_seconds === undefined) {
      bigLabel.textContent = "剩余时间";
      big.textContent = "估算中";
      var canEstimate = run.total_steps ||
        (run.percent !== null && run.percent !== undefined) ||
        (run.total_epochs && run.epoch);
      sub.textContent = canEstimate
        ? "等待更多进度数据（通常几秒内出现）"
        : "日志中没有总步数/总 epoch，无法估算剩余时间";
    } else {
      bigLabel.textContent = "剩余时间";
      big.textContent = fmtDuration(run.eta_seconds);
      var parts = [finishText(run), etaRangeText(run),
        confidenceText(run.confidence) + "置信度"].filter(Boolean);
      sub.textContent = parts.join(" · ");
    }

    var stats = [
      ["进度", (run.step === null || run.step === undefined ? "-" : run.step) + " / " +
        (run.total_steps === null || run.total_steps === undefined ? "-" : run.total_steps)],
      ["ETA", fmtEta(run)],
      ["预计完成", run.status === "completed"
        ? fmtClock(run.ended_at)
        : (run.eta_finish_at ? fmtClock(run.eta_finish_at) : "-")],
      ["剩余区间", (run.status === "running" || run.status === "waiting" ||
        run.status === "stalled") ? (etaRangeText(run) || "-") : "-"],
      ["已运行", fmtDuration(elapsed)],
      ["速度", run.rate ? fmtNum(run.rate, 2) + " step/s" : "-"],
      ["Loss", fmtNum(run.loss)],
      ["LR", run.lr === null || run.lr === undefined ? "-" : Number(run.lr).toExponential(2)],
      ["Epoch", run.epoch === null || run.epoch === undefined ? "-"
        : (Math.abs(run.epoch - Math.round(run.epoch)) < 1e-6
          ? String(Math.round(run.epoch)) : Number(run.epoch).toFixed(2)) +
          (run.total_epochs ? " / " + run.total_epochs : "")],
      ["置信度", confidenceText(run.confidence)]
    ];
    if (run.ended_at) stats.push(["结束时间", fmtTime(run.ended_at)]);
    if (run.exit_code !== null && run.exit_code !== undefined) stats.push(["退出码", String(run.exit_code)]);
    $("detailStats").innerHTML = stats.map(function (item) {
      return '<div class="stat"><div class="k">' + escapeHtml(item[0]) + '</div><div class="v">' +
        escapeHtml(item[1]) + '</div></div>';
    }).join("");

    var errorBox = $("detailError");
    if (run.error) {
      errorBox.hidden = false;
      errorBox.textContent = run.error;
    } else {
      errorBox.hidden = true;
      errorBox.textContent = "";
    }

    var samples = (run.samples || []).filter(function (s) {
      return s && s.loss !== null && s.loss !== undefined && !isNaN(s.loss);
    });
    $("chartHint").textContent = samples.length ? (samples.length + " 个采样点") : "暂无 loss 数据";
    drawChart($("lossChart"), samples);
  }

  function drawChart(canvas, samples) {
    var rect = canvas.getBoundingClientRect();
    var dpr = window.devicePixelRatio || 1;
    var width = Math.max(200, rect.width);
    var height = Math.max(160, rect.height || 240);
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    var pad = { left: 44, right: 14, top: 14, bottom: 24 };
    var plotW = width - pad.left - pad.right;
    var plotH = height - pad.top - pad.bottom;
    ctx.strokeStyle = "rgba(143,161,184,0.18)";
    ctx.lineWidth = 1;
    ctx.font = "10px ui-monospace, Menlo, monospace";
    ctx.fillStyle = "#8fa1b8";

    if (!samples.length) {
      ctx.textAlign = "center";
      ctx.fillText("暂无 loss 数据", width / 2, height / 2);
      return;
    }

    if (samples.length > 400) {
      var stride = Math.ceil(samples.length / 400);
      var reduced = [];
      for (var i = 0; i < samples.length; i += stride) reduced.push(samples[i]);
      if (reduced[reduced.length - 1] !== samples[samples.length - 1]) {
        reduced.push(samples[samples.length - 1]);
      }
      samples = reduced;
    }

    var minY = Infinity, maxY = -Infinity, minX = Infinity, maxX = -Infinity;
    samples.forEach(function (s) {
      var y = Number(s.loss);
      var x = Number(s.step !== undefined && s.step !== null ? s.step : 0);
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
    });
    if (!isFinite(minY) || !isFinite(maxY)) return;
    if (maxY - minY < 1e-9) { maxY += 1; minY -= 1; }
    var yPad = (maxY - minY) * 0.08;
    minY -= yPad; maxY += yPad;
    if (maxX === minX) maxX = minX + 1;

    for (var g = 0; g <= 4; g++) {
      var gy = pad.top + plotH * g / 4;
      ctx.beginPath();
      ctx.moveTo(pad.left, gy);
      ctx.lineTo(pad.left + plotW, gy);
      ctx.stroke();
      var label = maxY - (maxY - minY) * g / 4;
      ctx.textAlign = "right";
      ctx.fillText(label.toFixed(3), pad.left - 6, gy + 3);
    }
    ctx.textAlign = "center";
    ctx.fillText(String(minX), pad.left, height - 6);
    ctx.fillText(String(maxX), pad.left + plotW, height - 6);

    function px(x) { return pad.left + (x - minX) / (maxX - minX) * plotW; }
    function py(y) { return pad.top + (maxY - y) / (maxY - minY) * plotH; }

    ctx.beginPath();
    samples.forEach(function (s, idx) {
      var x = px(Number(s.step !== undefined && s.step !== null ? s.step : idx));
      var y = py(Number(s.loss));
      if (idx === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.lineWidth = 1.8;
    ctx.strokeStyle = "#5ec8ff";
    ctx.stroke();

    var last = samples[samples.length - 1];
    var lx = px(Number(last.step !== undefined && last.step !== null ? last.step : samples.length - 1));
    var ly = py(Number(last.loss));
    ctx.beginPath();
    ctx.arc(lx, ly, 3.2, 0, Math.PI * 2);
    ctx.fillStyle = "#9ddcff";
    ctx.fill();
  }

  function notifyRun(run) {
    var title = run.status === "completed" ? "训练完成" :
      (run.status === "failed" ? "训练失败" : (run.status === "stalled" ? "训练可能卡住" : "训练进程丢失"));
    var body = (run.name || run.run_id) + " · " + statusLabel(run.status);
    toast(title, body, run.status);
    if (!state.alerts) return;
    beep(run.status === "completed");
    if ("Notification" in window && Notification.permission === "granted") {
      try {
        new Notification(title, { body: body, tag: run.run_id });
      } catch (e) {}
    }
  }

  function toast(title, body, status) {
    var box = document.createElement("div");
    box.className = "toast " + (status || "");
    var text = document.createElement("div");
    text.className = "txt";
    var strong = document.createElement("b");
    strong.textContent = title;
    text.appendChild(strong);
    text.appendChild(document.createTextNode(body));
    var close = document.createElement("button");
    close.type = "button";
    close.textContent = "×";
    close.setAttribute("aria-label", "关闭");
    close.onclick = function () { box.remove(); };
    box.appendChild(text);
    box.appendChild(close);
    $("toasts").appendChild(box);
    if (!isTerminal(status) && status !== "stalled") {
      setTimeout(function () { box.remove(); }, 7000);
    }
  }

  function ensureAudio() {
    if (state.audioCtx) return state.audioCtx;
    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    try { state.audioCtx = new Ctx(); } catch (e) { return null; }
    return state.audioCtx;
  }

  function beep(success) {
    var ctx = ensureAudio();
    if (!ctx) return;
    try { ctx.resume(); } catch (e) {}
    var times = success ? [0, 0.18] : [0, 0.22];
    var freqs = success ? [880, 1174] : [330, 220];
    times.forEach(function (offset, idx) {
      var osc = ctx.createOscillator();
      var gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = freqs[idx];
      gain.gain.setValueAtTime(0.0001, ctx.currentTime + offset);
      gain.gain.exponentialRampToValueAtTime(0.18, ctx.currentTime + offset + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + offset + 0.16);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(ctx.currentTime + offset);
      osc.stop(ctx.currentTime + offset + 0.18);
    });
  }

  function enableAlerts() {
    ensureAudio();
    state.alerts = true;
    try { localStorage.setItem("trainmon.alerts", "1"); } catch (e) {}
    if ("Notification" in window && Notification.permission === "default") {
      Notification.requestPermission();
    }
    updateAlertButton();
    beep(true);
  }

  function updateAlertButton() {
    var btn = $("alertsBtn");
    btn.textContent = state.alerts ? "提醒已启用" : "启用提醒";
    btn.className = "btn ghost" + (state.alerts ? " active" : "");
  }

  function stopAgent() {
    if (state.stopped) return;
    var message = "停止监控服务？\n\n" +
      "只停止远端日志采集和面板服务，不会终止训练进程。\n" +
      "停止后需要重新运行 python3 -m trainmon agent start 才能继续查看。\n\n" +
      "agent 常驻约 17MB 内存、0.2% CPU。\n" +
      "注意：如果服务器按实例时长计费，停止 agent 本身不会停止计费，仍需关机或释放实例。";
    if (!window.confirm(message)) return;
    $("stopAgentBtn").disabled = true;
    if ($("stopAgentDetail")) $("stopAgentDetail").disabled = true;
    fetch("/api/agent/stop", {
      method: "POST",
      headers: { "X-TrainMon-Confirm": "stop" },
      cache: "no-store"
    }).then(function (res) {
      return res.json();
    }).then(function (data) {
      if (data && data.ok) {
        showStopped();
      } else {
        throw new Error((data && data.error) || "stop failed");
      }
    }).catch(function () {
      // The agent may exit before the response is fully flushed.
      showStopped();
      toast("停止监控", "请求已发送；如果连接断开，说明监控服务已经停止。", "stalled");
    });
  }

  function showStopped() {
    state.stopped = true;
    if (state.sse) {
      try { state.sse.close(); } catch (e) {}
      state.sse = null;
    }
    stopFallback();
    setConn(false, "监控已停止");
    $("stoppedBanner").hidden = false;
    $("stopAgentBtn").disabled = true;
    if ($("stopAgentDetail")) $("stopAgentDetail").disabled = true;
    closeDetail();
  }

  function connectSSE() {
    if (state.stopped) return;
    if (state.sse) {
      try { state.sse.close(); } catch (e) {}
    }
    if (!("EventSource" in window)) { startFallback(); return; }
    var source = new EventSource("/api/events");
    state.sse = source;
    source.onopen = function () {
      setConn(true, "实时连接");
      stopFallback();
    };
    source.onmessage = function (event) {
      try {
        var payload = JSON.parse(event.data);
        if (payload && payload.runs) applyRuns(payload.runs);
      } catch (e) {}
    };
    source.onerror = function () {
      setConn(false, "连接中断，重试中");
      startFallback();
    };
  }

  function startFallback() {
    if (state.stopped || state.fallbackTimer) return;
    state.fallbackTimer = setInterval(fetchRuns, 3000);
  }

  function stopFallback() {
    if (!state.fallbackTimer) return;
    clearInterval(state.fallbackTimer);
    state.fallbackTimer = null;
  }

  function init() {
    try {
      state.alerts = localStorage.getItem("trainmon.alerts") === "1";
    } catch (e) {}
    updateAlertButton();
    $("alertsBtn").onclick = enableAlerts;
    $("stopAgentBtn").onclick = stopAgent;
    if ($("stopAgentDetail")) $("stopAgentDetail").onclick = stopAgent;
    $("detailClose").onclick = closeDetail;
    $("detail").addEventListener("click", function (event) {
      if (event.target === $("detail")) closeDetail();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") closeDetail();
    });
    $("runs").addEventListener("click", function (event) {
      var card = event.target.closest ? event.target.closest(".run-card") : null;
      if (card && card.dataset.id) openDetail(card.dataset.id);
    });

    fetchRuns().then(function () {
      var params = new URLSearchParams(window.location.search);
      var run = params.get("run");
      if (run) openDetail(run);
    });
    connectSSE();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
