"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const form = $("config-form");
  const boolFields = ["download_images", "skip_existing", "headless", "direct_connection"];
  const numberFields = ["search_seconds", "max_notes", "interval_seconds", "page_timeout", "retries"];
  const textFields = ["keyword", "browser", "naming"];
  const activeStates = new Set(["opening", "running", "paused", "waiting_login", "stopping"]);
  const statusLabels = {idle:"等待开始", opening:"正在连接浏览器", ready:"浏览器已就绪", running:"采集中", paused:"已接管 · 采集暂停", waiting_login:"等待登录或页面处理", stopping:"正在停止", stopped:"已停止", completed:"采集完成", error:"需要处理异常"};
  let config = null;
  let state = null;
  let mode = "search";
  let currentTab = "settings";
  let connected = false;
  let busy = false;
  let timer;
  let toastTimer;
  let pendingState = null;
  let resultKey = "";
  let logKey = "";
  let exportKey = "";

  async function api(path, method = "GET", body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(path, {method, cache:"no-store", signal:controller.signal,
        headers:body === undefined ? {} : {"Content-Type":"application/json"},
        body:body === undefined ? undefined : JSON.stringify(body)});
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        const detail = data?.detail;
        throw new Error(Array.isArray(detail) ? detail.map((item) => `${item.loc?.slice(1).join(".")}：${item.msg}`).join("；") : detail || `请求失败 (${response.status})`);
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("服务响应超时，请稍后重试。");
      if (error instanceof TypeError) throw new Error("无法连接本地服务，请确认启动程序正在运行。");
      throw error;
    } finally { clearTimeout(timeout); }
  }

  function redact(value) {
    return String(value || "").replace(/((?:xsec_token|cookie|authorization)[=:]\s*)[^\s&]+/gi, "$1[已隐藏]");
  }

  function toast(message, error = false) {
    clearTimeout(toastTimer);
    $("toast-message").textContent = redact(message);
    $("toast").classList.toggle("error", error);
    $("toast").hidden = false;
    toastTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 8000 : 3500);
  }

  function selectMode(value) {
    mode = value === "urls" ? "urls" : "search";
    document.querySelectorAll("[data-mode]").forEach((button) => {
      button.classList.toggle("selected", button.dataset.mode === mode);
      button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
    });
    $("keyword-label").textContent = mode === "search" ? "搜索关键词" : "资料库名称";
    $("keyword-help").textContent = mode === "search" ? "输入你想收集的主题" : "用于保存本批内容的文件夹名称";
    $("urls-group").hidden = mode !== "urls";
    $("urls").required = mode === "urls";
    $("keyword").required = true;
    $("search-time-group").hidden = mode !== "search";
    controls();
  }

  function populate(value) {
    config = structuredClone(value);
    for (const name of boolFields) $(name).checked = Boolean(value[name]);
    for (const name of [...numberFields, ...textFields]) $(name).value = value[name] ?? "";
    $("urls").value = (value.urls || []).join("\n");
    selectMode(value.mode);
    $("form-error").hidden = true;
  }

  function readConfig() {
    const value = {mode};
    for (const name of boolFields) value[name] = $(name).checked;
    for (const name of numberFields) value[name] = Number($(name).value);
    for (const name of textFields) value[name] = $(name).value.trim();
    value.urls = $("urls").value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    return value;
  }

  function controls() {
    const active = activeStates.has(state?.status);
    const disabled = !connected || busy || !config;
    const locked = disabled || active;
    for (const element of form.elements) element.disabled = locked;
    for (const button of document.querySelectorAll("[data-mode]")) button.disabled = locked;
    $("search_seconds").disabled = locked || mode !== "search";
    $("urls").disabled = locked || mode !== "urls";
    $("save-config").disabled = locked;
    $("reset-config").disabled = locked;
    $("start-job").disabled = disabled || active;
    $("open-browser").hidden = Boolean(state?.browser_open);
    $("open-browser").disabled = disabled || active;
    $("close-browser").hidden = !state?.browser_open;
    $("close-browser").disabled = disabled || active;
    const resume = ["paused", "waiting_login"].includes(state?.status);
    $("pause-label").textContent = state?.status === "waiting_login" ? "已处理，继续" : resume ? "继续采集" : "暂停采集";
    $("pause-symbol").textContent = resume ? "▷" : "Ⅱ";
    $("pause-job").disabled = disabled || !["running", "paused", "waiting_login"].includes(state?.status);
    $("stop-job").disabled = disabled || !["opening", "running", "paused", "waiting_login"].includes(state?.status);
    $("retry-job").hidden = !(state?.retryable_count > 0) || active;
    $("retry-job").disabled = disabled || active;
    $("takeover-browser").hidden = !state?.browser_open || state?.status !== "running";
    $("takeover-browser").disabled = disabled || state?.status !== "running";
    $("takeover-browser").textContent = "接管操作";
    const sizeLocked = ["opening", "running", "stopping"].includes(state?.status);
    if (sizeLocked && heightDrag) finishHeightDrag({pointerId: heightDrag.id});
    $("browser-height-handle").disabled = sizeLocked;
    $("reset-browser-height").disabled = sizeLocked;
    $("browser-height-handle").title = sizeLocked ? "暂停采集后可调整高度" : "向下拖动加高；也可按上下方向键调整";
  }

  function switchTab(name, focus = false) {
    if (!["settings", "status", "results", "logs"].includes(name)) return;
    currentTab = name;
    document.querySelectorAll("[data-workspace-tab]").forEach((button) => {
      const selected = button.dataset.workspaceTab === name;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
      $(`${button.dataset.workspaceTab}-panel`).hidden = !selected;
    });
    if (name === "logs") $("log-indicator").classList.remove("new");
    if (focus) $(`${name}-tab`).focus();
  }

  function elapsed(value) {
    const total = Math.floor(Math.max(0, value || 0));
    return [Math.floor(total / 3600), Math.floor(total / 60) % 60, total % 60].map((part) => String(part).padStart(2,"0")).join(":");
  }

  function timeText(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString("zh-CN", {hour12:false});
  }

  function safeURL(value, local = false) {
    try {
      const url = new URL(value, location.origin);
      return ["http:","https:"].includes(url.protocol) && (!local || url.origin === location.origin) ? url.href : null;
    } catch { return null; }
  }

  function results(records) {
    const signature = JSON.stringify(records);
    if (signature === resultKey) return;
    resultKey = signature;
    $("result-total").textContent = String(records.length);
    $("results-empty").hidden = records.length > 0;
    const fragment = document.createDocumentFragment();
    for (const record of [...records].reverse()) {
      const row = document.createElement("tr");
      const nameCell = document.createElement("td");
      const name = document.createElement("strong");
      name.className = "note-title";
      name.textContent = record.title || record.note_id || "未获取标题";
      const meta = document.createElement("span");
      meta.className = "note-meta";
      meta.textContent = [record.author || "未知作者", record.published_at].filter(Boolean).join(" · ");
      nameCell.append(name, meta);
      const type = document.createElement("td");
      type.textContent = {image:"图文",normal:"图文",video:"视频",text:"文字"}[record.type] || "—";
      const images = document.createElement("td");
      images.textContent = String(record.image_count || 0);
      const resultCell = document.createElement("td");
      const badge = document.createElement("span");
      const resultStatus = ["success","failed","partial","skipped"].includes(record.status) ? record.status : "success";
      badge.className = `result-badge ${resultStatus}`;
      badge.textContent = {success:"已保存", failed:"失败", partial:"待补采", skipped:"已跳过"}[resultStatus];
      resultCell.append(badge);
      const linkCell = document.createElement("td");
      const url = safeURL(record.url);
      if (url) {
        const link = document.createElement("a");
        link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
        link.className = "result-link"; link.textContent = "↗";
        link.setAttribute("aria-label", "查看原文"); link.title = "查看原文";
        linkCell.append(link);
      }
      row.append(nameCell, type, images, resultCell, linkCell);
      fragment.append(row);
    }
    $("results-body").replaceChildren(fragment);
  }

  function logs(entries) {
    const signature = JSON.stringify(entries);
    if (signature === logKey) return;
    if (logKey && entries.length && currentTab !== "logs") $("log-indicator").classList.add("new");
    logKey = signature;
    $("logs-empty").hidden = entries.length > 0;
    const container = $("logs-list");
    const follow = container.scrollHeight - container.scrollTop - container.clientHeight < 80;
    const fragment = document.createDocumentFragment();
    for (const entry of entries) {
      const row = document.createElement("div");
      const severity = ["warning","error","success"].includes(entry.level) ? entry.level : "info";
      row.className = `log-row ${severity}`;
      const time = document.createElement("time");
      time.className = "log-time"; time.textContent = timeText(entry.time);
      const level = document.createElement("span");
      level.className = "log-level";
      level.textContent = {warning:"提示", error:"错误", success:"成功", info:"信息"}[severity];
      const message = document.createElement("span");
      message.className = "log-message"; message.textContent = redact(entry.message);
      row.append(time, level, message); fragment.append(row);
    }
    container.replaceChildren(fragment);
    if (follow) container.scrollTop = container.scrollHeight;
  }

  function exports(files) {
    const signature = JSON.stringify(files);
    if (signature === exportKey) return;
    exportKey = signature;
    const fragment = document.createDocumentFragment();
    for (const file of files) {
      const url = safeURL(file.url, true);
      if (!url) continue;
      const anchor = document.createElement("a");
      anchor.href = url; anchor.download = file.name; anchor.textContent = `↓ ${file.name}`;
      fragment.append(anchor);
    }
    $("export-links").replaceChildren(fragment);
  }

  function render(next) {
    state = next;
    $("state-title").textContent = statusLabels[next.status] || "等待开始";
    $("state-label").className = `state-label ${Object.hasOwn(statusLabels,next.status) ? next.status : "idle"}`;
    $("state-message").textContent = redact(next.message);
    $("elapsed").textContent = elapsed(next.elapsed_seconds);
    for (const name of ["discovered","success","images","failed","skipped"]) $(`count-${name}`).textContent = String(next.counts?.[name] || 0);
    $("job-error").hidden = !next.error;
    $("job-error").textContent = redact(next.error);
    $("current-note").textContent = next.current_note || "暂无正在处理的笔记";
    $("browser-url").textContent = next.browser?.url || "";
    $("browser-engine").textContent = (next.browser?.browser || "BROWSER").toUpperCase();
    const networkMode = next.browser?.network_mode;
    $("network-mode").hidden = !next.browser_open;
    $("network-mode").className = `network-label ${networkMode === "direct" ? "direct" : "system"}`;
    $("network-mode").textContent = networkMode === "direct" ? "直连网络" : "系统代理";
    $("network-mode").title = networkMode === "direct" ? "已忽略系统 HTTP/HTTPS 代理；VPN、TUN 或路由器出口仍可能生效" : "当前浏览器跟随系统代理设置";
    $("live-label").className = `live-label${next.browser_open ? " online" : ""}`;
    $("live-label").lastChild.textContent = next.browser_open ? "已连接" : "未连接";
    $("frame-time").textContent = next.frame_at ? `页面同步 ${timeText(next.frame_at)}` : "浏览器在本机运行";
    $("interaction-hint").textContent = next.status === "running" ? "自动采集中 · 点击接管操作可暂停并操控网页" : "直接点击网页操作 · 支持键盘、中文输入与拖动";
    const total = Number(next.counts?.discovered) || 0;
    const processed = Number(next.counts?.processed) || 0;
    const percent = total ? Math.min(100, processed / total * 100) : 0;
    $("progress-fill").style.width = `${percent}%`;
    $("progress-track").setAttribute("aria-valuenow", String(Math.round(percent)));
    $("progress-label").textContent = total ? `${processed} / ${total} 篇` : next.status === "running" ? "发现内容中" : "未开始";
    const phaseIndex = {idle:-1,browser:0,prepare:0,search:1,collect:2,export:3,done:3};
    let step = phaseIndex[next.phase] ?? -1;
    if (next.status === "completed") step = 4;
    document.querySelectorAll(".steps li").forEach((element,index) => {
      const done = index < step || (next.status === "ready" && index === 0);
      const current = index === step && activeStates.has(next.status) && next.status !== "stopping";
      element.classList.toggle("active", current);
      element.classList.toggle("done", done);
      element.querySelector(".step-marker").textContent = done ? "✓" : String(index+1);
      element.querySelector(".step-result").textContent = current ? (["paused","waiting_login"].includes(next.status) ? "等待中" : "进行中") : "";
    });
    results(next.results || []); logs(next.logs || []); exports(next.exports || []);
    controls();
  }

  async function fetchState() {
    if (!pendingState) pendingState = api("/api/state").finally(() => { pendingState = null; });
    return pendingState;
  }

  function connection(value) {
    connected = value;
    $("connection").className = `connection ${value ? "connected" : "disconnected"}`;
    $("connection-label").textContent = value ? "本地服务已连接" : "本地服务已断开";
    controls();
  }

  async function poll() {
    clearTimeout(timer);
    try {
      const next = await fetchState();
      connection(true);
      if (!config) populate(await api("/api/config"));
      render(next);
    } catch (error) {
      connection(false);
      if (!config) { $("form-error").hidden = false; $("form-error").textContent = error.message; }
    } finally { timer = setTimeout(poll, connected ? 1000 : 2000); }
  }

  async function command(path, body, message) {
    if (busy) return;
    busy = true; controls();
    try {
      const result = await api(path, path === "/api/config" ? "PUT" : "POST", body);
      if (path === "/api/config") populate(result);
      if (path === "/api/jobs/start") config = structuredClone(body);
      if (message) toast(message);
      if (pendingState) await pendingState.catch(() => {});
      render(await fetchState());
    } catch (error) {
      toast(error.message, true);
      if (["/api/config","/api/jobs/start"].includes(path)) {
        $("form-error").textContent = error.message; $("form-error").hidden = false; switchTab("settings");
      }
    } finally { busy = false; controls(); }
  }

  for (const button of document.querySelectorAll("[data-mode]")) button.addEventListener("click", () => selectMode(button.dataset.mode));
  for (const button of document.querySelectorAll("[data-workspace-tab]")) {
    button.addEventListener("click", () => switchTab(button.dataset.workspaceTab));
    button.addEventListener("keydown", (event) => {
      const names = ["settings","status","results","logs"];
      if (!["ArrowLeft","ArrowRight","Home","End"].includes(event.key)) return;
      event.preventDefault();
      const index = names.indexOf(currentTab);
      switchTab(event.key === "Home" ? names[0] : event.key === "End" ? names[3] : names[(index + (event.key === "ArrowRight" ? 1 : 3)) % 4], true);
    });
  }
  form.addEventListener("submit", (event) => { event.preventDefault(); if (form.reportValidity()) command("/api/jobs/start", readConfig(), "正在进入小红书首页并检查登录"); });
  $("start-job").addEventListener("click", () => { if (!form.checkValidity()) switchTab("settings"); });
  $("save-config").addEventListener("click", () => { if (form.reportValidity()) command("/api/config", readConfig(), "参数已保存"); });
  $("reset-config").addEventListener("click", () => { if (config) { populate(config); toast("已还原到最近保存的参数"); } });
  $("open-browser").addEventListener("click", () => command("/api/browser/open", {headless:$("headless").checked, browser:$("browser").value, direct_connection:$("direct_connection").checked}, "正在连接页内浏览器"));
  $("close-browser").addEventListener("click", () => command("/api/browser/close"));
  $("takeover-browser").addEventListener("click", () => command("/api/jobs/pause", undefined, "正在暂停采集，随后即可直接操作网页"));
  $("pause-job").addEventListener("click", () => command(`/api/jobs/${["paused","waiting_login"].includes(state?.status) ? "resume" : "pause"}`));
  $("stop-job").addEventListener("click", () => command("/api/jobs/stop"));
  $("retry-job").addEventListener("click", () => command("/api/jobs/retry"));
  $("fullscreen").addEventListener("click", async () => {
    try { if (document.fullscreenElement) await document.exitFullscreen(); else await $("browser-section").requestFullscreen(); }
    catch { toast("当前环境不支持全屏，可使用浏览器缩放扩大画面。", true); }
  });
  $("dismiss-toast").addEventListener("click", () => { $("toast").hidden = true; });

  // Local display preference only. Keep the width untouched and let the inner
  // browser's ResizeObserver request an exact viewport once manual mode permits.
  const heightHandle = $("browser-height-handle");
  const browserWrap = $("browser-embed-wrap");
  const heightStorageKey = "xhs.viewer.height";
  let heightDrag = null;
  let heightPreference = null;
  function setBrowserHeight(value, save = false) {
    const height = Math.round(Math.max(420, Math.min(1300, value)));
    heightPreference = height;
    document.documentElement.style.setProperty("--browser-height", `${height}px`);
    heightHandle.setAttribute("aria-valuenow", String(height));
    $("browser-height-value").textContent = `${height}px`;
    if (save) { try { localStorage.setItem(heightStorageKey, String(height)); } catch {} }
  }
  try {
    const saved = Number(localStorage.getItem(heightStorageKey));
    if (saved >= 420 && saved <= 1300) setBrowserHeight(saved);
  } catch {}
  heightHandle.addEventListener("pointerdown", (event) => {
    if (heightHandle.disabled || event.button !== 0) return;
    event.preventDefault();
    heightDrag = {id:event.pointerId, y:event.clientY, height:browserWrap.getBoundingClientRect().height};
    heightHandle.setPointerCapture(event.pointerId);
    $("browser-section").classList.add("resizing");
  });
  heightHandle.addEventListener("pointermove", (event) => {
    if (!heightHandle.disabled && heightDrag?.id === event.pointerId) setBrowserHeight(heightDrag.height + event.clientY - heightDrag.y);
  });
  function finishHeightDrag(event) {
    if (!heightDrag || heightDrag.id !== event.pointerId) return;
    heightDrag = null;
    $("browser-section").classList.remove("resizing");
    if (heightHandle.hasPointerCapture(event.pointerId)) heightHandle.releasePointerCapture(event.pointerId);
    if (heightPreference !== null) setBrowserHeight(heightPreference, true);
  }
  heightHandle.addEventListener("pointerup", finishHeightDrag);
  heightHandle.addEventListener("pointercancel", finishHeightDrag);
  heightHandle.addEventListener("lostpointercapture", finishHeightDrag);
  heightHandle.addEventListener("keydown", (event) => {
    if (heightHandle.disabled || !["ArrowUp","ArrowDown","Home","End"].includes(event.key)) return;
    event.preventDefault();
    const current = browserWrap.getBoundingClientRect().height;
    setBrowserHeight(event.key === "Home" ? 420 : event.key === "End" ? 1300 : current + (event.key === "ArrowDown" ? 40 : -40), true);
  });
  $("reset-browser-height").addEventListener("click", () => {
    heightPreference = null;
    document.documentElement.style.removeProperty("--browser-height");
    try { localStorage.removeItem(heightStorageKey); } catch {}
    $("browser-height-value").textContent = "自动";
    heightHandle.setAttribute("aria-valuenow", String(Math.round(browserWrap.getBoundingClientRect().height)));
  });
  const heightObserver = new ResizeObserver(() => {
    if (heightPreference === null) heightHandle.setAttribute("aria-valuenow", String(Math.round(browserWrap.getBoundingClientRect().height)));
  });
  heightObserver.observe(browserWrap);
  window.addEventListener("pagehide", () => heightObserver.disconnect());
  switchTab("settings"); controls(); poll();
})();
