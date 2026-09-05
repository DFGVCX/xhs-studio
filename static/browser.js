"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const canvas = $("browser-canvas");
  const viewport = $("viewport");
  const input = $("remote-input");
  const qualitySelect = $("stream-quality");
  const qualityStorageKey = "xhs.viewer.quality";
  const context = canvas.getContext("2d", {alpha: false});
  const manualStates = new Set(["idle", "ready", "paused", "waiting_login", "stopped", "completed", "error"]);
  let state = {status: "idle", browser_open: false, manual_enabled: false, browser: {}};
  let socket;
  let connected = false;
  let reconnectDelay = 350;
  let reconnectTimer;
  let destroyed = false;
  let latestFrame = null;
  let decoding = false;
  let hasFrame = false;
  let busy = false;
  let takingOver = false;
  let browserChoice = "auto";
  let lastMetadataAt = 0;
  let stateRequest = null;
  let resizeTimer;
  let lastRequestedSize = "";
  let quality = "high";
  try {
    const savedQuality = localStorage.getItem(qualityStorageKey);
    if (["high", "smooth"].includes(savedQuality)) quality = savedQuality;
  } catch { /* The viewer remains usable when local storage is unavailable. */ }
  qualitySelect.value = quality;
  let toastTimer;
  let pointerId = null;
  let pointerButton = "left";
  let pointerClickCount = 1;
  let previousDown = {time: 0, x: 0, y: 0, count: 0};
  let pendingMove = null;
  let moveTimer;
  let pendingWheel = null;
  let wheelTimer;
  let composing = false;
  let lastComposition = {text: "", time: 0};
  const pressedKeys = new Set();

  async function api(path, body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 25000);
    try {
      const response = await fetch(path, {
        method: body === undefined ? "GET" : "POST",
        headers: body === undefined ? {} : {"Content-Type": "application/json"},
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store", signal: controller.signal,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : data.message || "操作未完成，请稍后重试");
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("浏览器响应超时，请查看下方运行日志");
      throw error;
    } finally { clearTimeout(timeout); }
  }

  function toast(message, error = false) {
    clearTimeout(toastTimer);
    $("viewer-toast").textContent = message;
    $("viewer-toast").classList.toggle("error", error);
    $("viewer-toast").hidden = false;
    toastTimer = setTimeout(() => { $("viewer-toast").hidden = true; }, error ? 6000 : 3500);
  }

  function interactive() { return connected && state.browser_open && state.manual_enabled && !busy; }

  function renderState() {
    const manual = interactive();
    const opening = state.status === "opening" || (busy && !state.browser_open);
    const info = state.browser || {};
    const blankPage = state.browser_open && info.url === "about:blank";
    $("mode-badge").textContent = !connected ? "正在连接" : manual ? "可直接操作" : state.status === "running" ? "自动化运行中" : opening ? "启动中" : state.browser_open ? "等待画面" : "浏览器未打开";
    $("mode-badge").className = `mode-badge${manual ? " manual" : state.status === "running" ? " automated" : ""}`;
    $("connection-dot").className = `connection-dot ${connected ? "connected" : "disconnected"}`;
    $("connection-label").textContent = connected ? "实时连接" : hasFrame ? "连接中断 · 重连中" : "正在连接画面";
    $("interaction-hint").textContent = manual ? "点击后直接输入 · 支持滚动、拖动与粘贴" : state.status === "running" ? "正在自动操作 · 接管后可手动操作" : opening ? "正在启动浏览器，请稍候" : "登录与操作都在当前页面完成";
    for (const element of $("navigation").elements) element.disabled = !manual;
    qualitySelect.disabled = busy || ["opening", "running", "stopping"].includes(state.status);
    canvas.classList.toggle("interactive", manual);
    canvas.setAttribute("aria-disabled", String(!manual));
    $("takeover").hidden = !connected || !state.browser_open || state.status !== "running" || !hasFrame;
    $("takeover-button").disabled = takingOver;
    $("takeover-button").querySelector("span").textContent = takingOver ? "正在暂停…" : "接管操作";
    $("empty-state").hidden = hasFrame && state.browser_open && !blankPage;
    $("empty-state").classList.toggle("loading", opening || (state.browser_open && !hasFrame && !blankPage));
    $("empty-title").textContent = opening ? "正在打开交互浏览器" : blankPage ? "从这里去任何网页" : state.browser_open ? "正在连接浏览器画面" : "网页，就在这里";
    $("empty-description").replaceChildren();
    const lines = opening ? ["浏览器正在启动，画面即将出现在这里", "第一次启动可能需要稍等片刻"] : state.browser_open ? ["输入任意 HTTP/HTTPS 地址开始浏览", "启动采集后会自动进入小红书"] : ["可访问任意网页，直接点击、输入、滚动", "自动采集默认进入小红书"];
    $("empty-description").append(lines[0], document.createElement("br"), lines[1]);
    $("open-browser").hidden = opening || state.browser_open;
    $("open-browser").disabled = busy;
    const unavailable = state.browser_open && state.stream_available === false;
    $("frame-notice").hidden = !hasFrame || (connected && !unavailable);
    $("frame-notice-text").textContent = !connected ? "实时连接中断，正在重连 · 当前显示最后一帧" : "画面正在恢复 · 当前显示最后一帧";
    $("page-title").textContent = info.title || "交互浏览器";
    if (document.activeElement !== $("address")) $("address").value = info.url && info.url !== "about:blank" ? info.url : "";
  }

  function receiveState(next) {
    const previouslyManual = interactive();
    const wasOpen = state.browser_open;
    state = {...state, ...next, browser: {...state.browser, ...(typeof next.browser === "object" ? next.browser : {})}};
    if (typeof next.manual_enabled !== "boolean") state.manual_enabled = Boolean(state.browser_open && manualStates.has(state.status));
    if (!state.browser_open && wasOpen) {
      latestFrame = null;
      hasFrame = false;
      canvas.hidden = true;
      $("frame-size").textContent = "";
      lastRequestedSize = "";
    }
    if (previouslyManual && !interactive()) releaseInputs();
    if (takingOver && (state.manual_enabled || state.status !== "running")) takingOver = false;
    renderState();
    if (interactive()) scheduleViewportResize();
    if (window.parent !== window) window.parent.postMessage({type: "browser-state", status: state.status, browser_open: state.browser_open, manual_enabled: state.manual_enabled, browser: state.browser, connected}, location.origin);
  }

  async function refreshState() {
    if (stateRequest) return stateRequest;
    stateRequest = api("/api/state").then(receiveState).catch(() => {}).finally(() => { stateRequest = null; });
    return stateRequest;
  }

  function fitCanvas() {
    if (!hasFrame || !canvas.width || !canvas.height) return;
    const scale = Math.min(viewport.clientWidth / canvas.width, viewport.clientHeight / canvas.height);
    canvas.style.width = `${Math.max(1, Math.floor(canvas.width * scale))}px`;
    canvas.style.height = `${Math.max(1, Math.floor(canvas.height * scale))}px`;
  }

  function scheduleViewportResize() {
    if (resizeTimer) return;
    resizeTimer = setTimeout(() => {
      resizeTimer = null;
      if (!interactive()) return;
      const width = Math.max(320, Math.min(1920, Math.round(viewport.clientWidth)));
      const height = Math.max(240, Math.min(1200, Math.round(viewport.clientHeight)));
      const size = `${width}x${height}:${quality}`;
      if (lastRequestedSize !== size && sendInput({type: "resize", width, height, quality})) lastRequestedSize = size;
    }, 150);
  }

  async function drawLatestFrame() {
    if (decoding) return;
    decoding = true;
    try {
      while (latestFrame && !destroyed) {
        const frame = latestFrame;
        latestFrame = null;
        let bitmap;
        try {
          bitmap = await createImageBitmap(frame instanceof Blob ? frame : new Blob([frame], {type: "image/jpeg"}));
          if (latestFrame || !state.browser_open) continue;
          if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) { canvas.width = bitmap.width; canvas.height = bitmap.height; }
          // Preserve the full decoded backing resolution. drawImage is 1:1;
          // only the fitted CSS box scales down the 2× high-quality bitmap.
          context.imageSmoothingEnabled = true;
          context.imageSmoothingQuality = "high";
          context.filter = "none";
          context.drawImage(bitmap, 0, 0);
          const firstFrame = !hasFrame;
          hasFrame = true;
          canvas.hidden = false;
          $("frame-size").textContent = `${bitmap.width} × ${bitmap.height}`;
          fitCanvas();
          if (firstFrame) renderState();
        } catch (error) {
          if (!hasFrame) $("connection-label").textContent = "画面解码中，正在重试";
        } finally { bitmap?.close(); }
      }
    } finally { decoding = false; }
  }

  function connect() {
    clearTimeout(reconnectTimer);
    if (destroyed) return;
    socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/browser/stream`);
    socket.binaryType = "arraybuffer";
    socket.addEventListener("open", () => { connected = true; reconnectDelay = 350; lastRequestedSize = ""; renderState(); refreshState(); });
    socket.addEventListener("message", (event) => {
      if (typeof event.data !== "string") { latestFrame = event.data; drawLatestFrame(); return; }
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      if (message.type === "state") { lastMetadataAt = Date.now(); receiveState(message.state || message); }
      if (message.type === "error") toast(message.message || "浏览器操作未完成", true);
    });
    socket.addEventListener("close", () => {
      connected = false;
      releaseInputs();
      renderState();
      if (!destroyed) { reconnectTimer = setTimeout(connect, reconnectDelay); reconnectDelay = Math.min(3000, reconnectDelay * 1.7); }
    });
    socket.addEventListener("error", () => { $("connection-label").textContent = "正在恢复实时连接"; });
  }

  function sendInput(action, force = false) {
    if ((!force && !interactive()) || socket?.readyState !== WebSocket.OPEN) return false;
    // Drop mouse motion while the transport is backed up; never drop button edges.
    if (action.type === "pointer" && action.event === "move" && socket.bufferedAmount > 65536) return false;
    socket.send(JSON.stringify({type: "input", action}));
    return true;
  }

  function point(event) {
    const rect = canvas.getBoundingClientRect();
    return {x: Math.max(0, Math.min(1, (event.clientX - rect.left) / Math.max(1, rect.width))), y: Math.max(0, Math.min(1, (event.clientY - rect.top) / Math.max(1, rect.height)))};
  }

  function focusRemote(event) {
    const rect = viewport.getBoundingClientRect();
    input.style.left = `${Math.max(0, Math.min(rect.width - 3, event.clientX - rect.left))}px`;
    input.style.top = `${Math.max(0, Math.min(rect.height - 22, event.clientY - rect.top))}px`;
    input.focus({preventScroll: true});
  }

  function flushMove() { clearTimeout(moveTimer); moveTimer = null; if (pendingMove) { sendInput(pendingMove); pendingMove = null; } }
  function releaseInputs() {
    clearTimeout(moveTimer); clearTimeout(wheelTimer);
    moveTimer = wheelTimer = null;
    pendingMove = pendingWheel = null;
    if (pointerId !== null && canvas.hasPointerCapture(pointerId)) canvas.releasePointerCapture(pointerId);
    pointerId = null;
    pressedKeys.clear(); composing = false; input.value = "";
    sendInput({type: "release"}, true);
  }

  canvas.addEventListener("pointerdown", (event) => {
    if (!interactive() || (pointerId !== null && pointerId !== event.pointerId)) return;
    event.preventDefault();
    flushMove();
    pointerId = event.pointerId;
    pointerButton = ["left", "middle", "right"][event.button] || "left";
    const closeToPrevious = performance.now() - previousDown.time < 450 && Math.hypot(event.clientX - previousDown.x, event.clientY - previousDown.y) < 6;
    pointerClickCount = Math.min(2, event.detail || (closeToPrevious && previousDown.count === 1 ? 2 : 1));
    previousDown = {time: performance.now(), x: event.clientX, y: event.clientY, count: pointerClickCount};
    canvas.setPointerCapture(event.pointerId);
    focusRemote(event);
    sendInput({type: "pointer", event: "down", ...point(event), button: pointerButton, buttons: event.buttons || 1, click_count: pointerClickCount});
  });

  canvas.addEventListener("pointermove", (event) => {
    if (!interactive() || (pointerId !== null && pointerId !== event.pointerId)) return;
    pendingMove = {type: "pointer", event: "move", ...point(event), button: pointerId !== null ? pointerButton : "left", buttons: event.buttons, click_count: 1};
    if (!moveTimer) moveTimer = setTimeout(flushMove, 33);
  });

  canvas.addEventListener("pointerup", (event) => {
    if (pointerId !== event.pointerId) return;
    event.preventDefault();
    flushMove();
    sendInput({type: "pointer", event: "up", ...point(event), button: pointerButton, buttons: event.buttons, click_count: pointerClickCount});
    pointerId = null;
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointercancel", releaseInputs);
  canvas.addEventListener("lostpointercapture", () => { if (pointerId !== null) releaseInputs(); });
  canvas.addEventListener("contextmenu", (event) => event.preventDefault());
  canvas.addEventListener("dragstart", (event) => event.preventDefault());
  canvas.addEventListener("wheel", (event) => {
    if (!interactive()) return;
    event.preventDefault();
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? canvas.clientHeight : 1;
    const pos = point(event);
    if (!pendingWheel) pendingWheel = {type: "wheel", delta_x: 0, delta_y: 0, ...pos};
    pendingWheel.delta_x = Math.max(-3000, Math.min(3000, pendingWheel.delta_x + event.deltaX * unit));
    pendingWheel.delta_y = Math.max(-3000, Math.min(3000, pendingWheel.delta_y + event.deltaY * unit));
    Object.assign(pendingWheel, pos);
    if (!wheelTimer) wheelTimer = setTimeout(() => { sendInput(pendingWheel); pendingWheel = null; wheelTimer = null; }, 33);
  }, {passive: false});

  function modifiers(event) { return (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) | (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0); }
  function sendText(text) {
    if (!text) return;
    const characters = Array.from(text);
    for (let offset = 0; offset < characters.length; offset += 1000) sendInput({type: "text", text: characters.slice(offset, offset + 1000).join("")});
  }
  function tapKey(key, code = key) { sendInput({type: "key", event: "down", key, code, modifiers: 0}); sendInput({type: "key", event: "up", key, code, modifiers: 0}); }
  function repeatedComposition(text) { return text && text === lastComposition.text && performance.now() - lastComposition.time < 100; }

  for (const target of [canvas, input]) {
    target.addEventListener("keydown", (event) => {
      if (!interactive()) return;
      if (composing || event.isComposing || event.keyCode === 229 || ["Process", "Dead", "Unidentified"].includes(event.key)) return;
      const command = (event.ctrlKey || event.metaKey) && !event.altKey;
      if (command && event.key.toLowerCase() === "v") return; // Let the explicit local paste event supply text.
      if (command && ["c", "x"].includes(event.key.toLowerCase())) {
        event.preventDefault();
        toast("画面暂不支持复制或剪切；可从下方采集结果复制内容");
        return;
      }
      if (event.key.length === 1 && !command && !event.altKey) {
        if (target === canvas) input.focus({preventScroll: true});
        return; // beforeinput/compositionend owns printable text, including Chinese IME.
      }
      event.preventDefault();
      pressedKeys.add(event.code || event.key);
      sendInput({type: "key", event: "down", key: event.key, code: event.code, modifiers: modifiers(event)});
    });
    target.addEventListener("keyup", (event) => {
      if (!pressedKeys.has(event.code || event.key)) return;
      event.preventDefault();
      pressedKeys.delete(event.code || event.key);
      sendInput({type: "key", event: "up", key: event.key, code: event.code, modifiers: modifiers(event)});
    });
  }

  input.addEventListener("compositionstart", () => { composing = true; });
  input.addEventListener("compositionend", (event) => {
    composing = false;
    const text = event.data || input.value;
    sendText(text);
    lastComposition = {text, time: performance.now()};
    input.value = "";
  });
  input.addEventListener("beforeinput", (event) => {
    if (!interactive()) { event.preventDefault(); return; }
    if (composing || event.isComposing || event.inputType === "insertCompositionText") return;
    event.preventDefault();
    if (event.inputType === "deleteContentBackward") tapKey("Backspace");
    else if (event.inputType === "deleteContentForward") tapKey("Delete");
    else if (["insertLineBreak", "insertParagraph"].includes(event.inputType)) tapKey("Enter");
    else if (event.data && !repeatedComposition(event.data)) sendText(event.data);
    input.value = "";
  });
  input.addEventListener("input", (event) => {
    if (composing || event.isComposing) return;
    const text = event.data || input.value;
    if (!repeatedComposition(text)) sendText(text);
    input.value = "";
  });
  input.addEventListener("paste", (event) => {
    event.preventDefault();
    if (interactive()) { sendText(event.clipboardData?.getData("text/plain") || ""); input.value = ""; }
  });
  input.addEventListener("blur", releaseInputs);
  canvas.addEventListener("focus", () => { if (interactive()) input.focus({preventScroll: true}); });
  window.addEventListener("blur", releaseInputs);
  document.addEventListener("visibilitychange", () => { if (document.hidden) releaseInputs(); });

  async function navigate(type, url) {
    if (!interactive()) return;
    releaseInputs();
    busy = true; renderState();
    try { await api("/api/browser/action", {type, ...(url ? {url} : {})}); }
    catch (error) { toast(error.message, true); }
    finally { busy = false; renderState(); refreshState(); }
  }
  for (const type of ["back", "forward", "refresh", "home"]) $(`nav-${type}`).addEventListener("click", () => navigate(type));
  $("navigation").addEventListener("submit", (event) => {
    event.preventDefault();
    const rawValue = $("address").value;
    const value = rawValue.trim();
    if (!value) return;
    let url;
    try {
      if (value.length > 4096 || /[\u0000-\u001f\u007f\\]/.test(rawValue) || value.includes(" ") || value.startsWith("//")) throw new Error("请输入完整的 http/https 地址或域名，地址不能包含空格、控制字符或反斜杠");
      const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(value);
      const authority = value.split(/[/?#]/, 1)[0];
      const bareHostPort = /^(?:localhost|(?:[a-z0-9-]+\.)+[a-z0-9-]+|\[[0-9a-f:]+\]):\d+$/i.test(authority);
      if (scheme && !/^https?$/i.test(scheme[1]) && !bareHostPort) throw new Error("仅支持 http:// 或 https:// 网页地址，不支持文件、脚本或浏览器内部协议");
      if (scheme && /^https?$/i.test(scheme[1]) && !/^https?:\/\//i.test(value)) throw new Error("请使用完整的 http:// 或 https:// 网页地址");
      const loopback = /^(?:localhost|127(?:\.\d+){3}|\[::1\])(?::\d+)?(?:[/?#]|$)/i.test(value);
      const candidate = /^https?:\/\//i.test(value) ? value : `${loopback ? "http" : "https"}://${value}`;
      const candidateAuthority = candidate.split("//", 2)[1].split(/[/?#]/, 1)[0];
      if (!candidateAuthority) throw new Error("网页地址缺少域名或主机名");
      if (candidateAuthority.includes("@") || candidateAuthority.endsWith(":")) throw new Error("网页地址不能包含账号密码，端口需为 1–65535");
      url = new URL(candidate);
      if (!["https:", "http:"].includes(url.protocol) || !url.hostname || url.username || url.password || (url.port && Number(url.port) < 1)) throw new Error("请输入有效网页地址，端口需为 1–65535");
    } catch (error) { toast(error instanceof TypeError ? "请输入有效网页地址，端口需为 1–65535" : error.message, true); return; }
    $("address").blur();
    navigate("navigate", url.href);
  });
  $("address").addEventListener("keydown", (event) => { if (event.key === "Escape") { $("address").blur(); renderState(); } });
  qualitySelect.addEventListener("change", () => {
    if (qualitySelect.disabled) { qualitySelect.value = quality; return; }
    quality = qualitySelect.value === "smooth" ? "smooth" : "high";
    qualitySelect.value = quality;
    try { localStorage.setItem(qualityStorageKey, quality); } catch { /* Session preference still applies. */ }
    lastRequestedSize = "";
    scheduleViewportResize();
  });
  $("open-browser").addEventListener("click", async () => {
    if (busy || state.browser_open) return;
    busy = true; renderState();
    try { await api("/api/browser/open", {headless: true, browser: browserChoice}); }
    catch (error) { toast(error.message, true); }
    finally { busy = false; await refreshState(); renderState(); }
  });
  $("takeover-button").addEventListener("click", async () => {
    if (takingOver || state.status !== "running") return;
    takingOver = true; renderState();
    try { await api("/api/jobs/pause", {}); await refreshState(); }
    catch (error) { takingOver = false; toast(error.message, true); renderState(); }
  });
  const resizeObserver = new ResizeObserver(() => { fitCanvas(); scheduleViewportResize(); });
  resizeObserver.observe(viewport);
  const pollTimer = setInterval(() => { if (!connected || Date.now() - lastMetadataAt > 3000) refreshState(); }, 2000);
  window.addEventListener("pagehide", () => { destroyed = true; releaseInputs(); clearTimeout(reconnectTimer); clearTimeout(resizeTimer); clearInterval(pollTimer); resizeObserver.disconnect(); socket?.close(); });
  api("/api/config").then((config) => { if (["auto", "chrome", "edge"].includes(config.browser)) browserChoice = config.browser; }).catch(() => {});
  renderState();
  refreshState();
  connect();
})();
