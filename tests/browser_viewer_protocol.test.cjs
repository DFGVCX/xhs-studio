"use strict";

// Run: node --test tests/browser_viewer_protocol.test.cjs
// Isolated frontend protocol checks. The opt-in Python companion exercises real
// browsers, the same-origin iframe, production WebSocket route, and native CDP.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {test} = require("node:test");
const source = fs.readFileSync(path.join(__dirname, "../static/browser.js"), "utf8");

function fixture(options = {}) {
  let document;
  class Element {
    constructor(id) {
      Object.assign(this, {id, listeners: {}, classList: {toggle() {}}, elements: [], value: "", style: {},
        clientWidth: 900, clientHeight: 500, width: 1280, height: 720, hidden: false, children: {}});
    }
    addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
    dispatch(type, event = {}) {
      event.preventDefault ||= function () { this.defaultPrevented = true; };
      for (const callback of this.listeners[type] || []) callback(event);
      return event;
    }
    getContext() { return this.context ||= {draws: [], drawImage(...args) { this.draws.push(args); }}; }
    getBoundingClientRect() { return {left: 100, top: 80, width: 800, height: 400}; }
    setAttribute() {} replaceChildren() {} append() {}
    querySelector(selector) { return this.children[selector] ||= new Element(selector); }
    focus() { document.activeElement = this; }
    blur() { document.activeElement = null; this.dispatch("blur"); }
    setPointerCapture(id) { this.capture = id; }
    hasPointerCapture(id) { return this.capture === id; }
    releasePointerCapture() { this.capture = null; }
  }
  const elements = {};
  document = {hidden: false, activeElement: null,
    getElementById(id) { return elements[id] ||= new Element(id); },
    createElement(tag) { return new Element(tag); }, addEventListener() {}};
  const instances = [];
  class Socket {
    static OPEN = 1;
    constructor() { Object.assign(this, {listeners: {}, readyState: 1, bufferedAmount: 0, sent: []}); instances.push(this); }
    addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
    dispatch(type, event = {}) { for (const callback of this.listeners[type] || []) callback(event); }
    send(text) { this.sent.push(JSON.parse(text)); }
    close() {}
  }
  const timers = new Map();
  const storage = options.storage || new Map();
  const requests = [];
  let timerId = 0;
  const window = {addEventListener() {}};
  window.parent = window;
  const ready = {type: "state", status: "ready", browser_open: true, manual_enabled: true, stream_available: true,
    browser: {url: "https://www.xiaohongshu.com/", title: "小红书"}};
  const context = {document, window, WebSocket: Socket,
    localStorage: {
      getItem(key) { if (options.storageDisabled) throw new Error("Storage unavailable"); return storage.get(key) ?? null; },
      setItem(key, value) { if (options.storageDisabled) throw new Error("Storage unavailable"); storage.set(key, value); },
    },
    location: {origin: "http://127.0.0.1:8765", protocol: "http:", host: "127.0.0.1:8765"},
    setTimeout(callback) { timers.set(++timerId, callback); return timerId; },
    clearTimeout(id) { timers.delete(id); }, setInterval() { return 1; }, clearInterval() {},
    ResizeObserver: class {observe() {} disconnect() {}}, AbortController, Blob, URL, performance,
    createImageBitmap: async () => ({width: options.bitmapWidth || 1280, height: options.bitmapHeight || 720, close() {}}),
    fetch: async (url, request) => { requests.push({url, body: request?.body ? JSON.parse(request.body) : undefined}); return {ok: true, json: async () => url === "/api/config" ? {browser: "auto"} : ready}; },
  };
  vm.runInNewContext(source, context, {filename: "browser.js"});
  const socket = instances[0];
  socket.dispatch("open");
  socket.dispatch("message", {data: JSON.stringify(ready)});
  socket.sent = [];
  return {socket, elements, ready, storage, requests, input: elements["remote-input"], canvas: elements["browser-canvas"], quality: elements["stream-quality"],
    state(next) { socket.dispatch("message", {data: JSON.stringify({...ready, ...next})}); },
    flushTimers() { const queued = [...timers]; for (const [id, callback] of queued) if (timers.delete(id)) callback(); },
    actions(type) { return socket.sent.filter((packet) => !type || packet.action.type === type).map((packet) => packet.action); },
  };
}

test("Chinese IME composition commits once despite a following insertion event", () => {
  const f = fixture();
  f.input.dispatch("compositionstart");
  f.input.value = "中文输入";
  f.input.dispatch("input", {isComposing: true, data: "中文输入"});
  f.input.dispatch("compositionend", {data: "中文输入"});
  f.input.dispatch("beforeinput", {inputType: "insertText", data: "中文输入"});
  assert.deepEqual(f.actions("text").map((action) => action.text), ["中文输入"]);
});

test("direct text and explicit native paste preserve characters, including chunked emoji", () => {
  const f = fixture();
  f.input.dispatch("beforeinput", {inputType: "insertText", data: "a"});
  const clipboard = "粘贴🙂".repeat(650);
  f.input.dispatch("paste", {clipboardData: {getData: () => clipboard}});
  const values = f.actions("text").map((action) => action.text);
  assert.equal(values.shift(), "a");
  assert.equal(values.join(""), clipboard);
  assert.ok(values.every((text) => text.length <= 2000));
});

test("pointer drag preserves edges and clamps release outside the displayed image", () => {
  const f = fixture();
  f.canvas.dispatch("pointerdown", {pointerId: 1, button: 0, buttons: 1, clientX: 500, clientY: 280, detail: 1});
  f.canvas.dispatch("pointermove", {pointerId: 1, buttons: 1, clientX: 600, clientY: 300});
  f.canvas.dispatch("pointerup", {pointerId: 1, buttons: 0, clientX: 1200, clientY: 500});
  assert.deepEqual(f.actions("pointer").map((action) => action.event), ["down", "move", "up"]);
  assert.equal(f.actions("pointer")[0].x, 0.5);
  assert.equal(f.actions("pointer")[0].y, 0.5);
  assert.equal(f.actions("pointer")[2].x, 1);
  assert.equal(f.actions("pointer")[2].y, 1);
});

test("modifier key edges reach the remote browser, but Ctrl+V uses the local paste event", () => {
  const f = fixture();
  f.input.dispatch("keydown", {key: "Control", code: "ControlLeft", ctrlKey: true});
  f.input.dispatch("keydown", {key: "a", code: "KeyA", ctrlKey: true});
  f.input.dispatch("keyup", {key: "a", code: "KeyA", ctrlKey: true});
  f.input.dispatch("keyup", {key: "Control", code: "ControlLeft", ctrlKey: false});
  assert.deepEqual(f.actions("key").map(({event, key, modifiers}) => [event, key, modifiers]),
    [["down", "Control", 2], ["down", "a", 2], ["up", "a", 2], ["up", "Control", 0]]);
  f.socket.sent = [];
  f.input.dispatch("keydown", {key: "v", code: "KeyV", ctrlKey: true});
  assert.equal(f.socket.sent.length, 0);
});

test("binary JPEG rendering fits the image while resize requests use the available viewport", async () => {
  const f = fixture();
  f.socket.dispatch("message", {data: new ArrayBuffer(3)});
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(f.canvas.hidden, false);
  assert.equal(f.canvas.style.width, "888px");
  assert.equal(f.canvas.style.height, "500px");
  f.flushTimers();
  assert.deepEqual(f.actions("resize").map(({width, height, quality}) => [width, height, quality]), [[900, 500, "high"]]);
  f.state({});
  f.flushTimers();
  assert.equal(f.actions("resize").length, 1, "unchanged metadata cannot cause a resize loop");
});

test("running automation blocks input; blur releases held pointer and keyboard state", () => {
  const f = fixture();
  f.input.dispatch("keydown", {key: "Shift", code: "ShiftLeft", shiftKey: true});
  f.input.dispatch("blur");
  assert.equal(f.actions().at(-1).type, "release");
  f.state({status: "running", manual_enabled: false});
  f.socket.sent = [];
  f.canvas.dispatch("pointerdown", {pointerId: 2, button: 0, buttons: 1, clientX: 500, clientY: 280, detail: 1});
  f.input.dispatch("beforeinput", {inputType: "insertText", data: "blocked"});
  assert.equal(f.socket.sent.length, 0);
});

test("high quality is the default; switching quality resends resize and persists across viewer loads", () => {
  const f = fixture();
  assert.equal(f.quality.value, "high");
  f.flushTimers();
  assert.equal(f.actions("resize")[0].quality, "high");
  f.quality.value = "smooth";
  f.quality.dispatch("change");
  f.flushTimers();
  assert.deepEqual(f.actions("resize").map(({quality}) => quality), ["high", "smooth"]);
  assert.equal(f.storage.get("xhs.viewer.quality"), "smooth");
  const reloaded = fixture({storage: f.storage});
  reloaded.flushTimers();
  assert.equal(reloaded.quality.value, "smooth");
  assert.equal(reloaded.actions("resize")[0].quality, "smooth");
  f.input.dispatch("beforeinput", {inputType: "insertText", data: "切换后仍可输入"});
  assert.equal(f.actions("text")[0].text, "切换后仍可输入");
});

test("disabled storage does not block quality changes, and automation cannot change viewport quality", () => {
  const f = fixture({storageDisabled: true});
  f.quality.value = "smooth";
  f.quality.dispatch("change");
  f.flushTimers();
  assert.equal(f.actions("resize")[0].quality, "smooth");
  f.state({status: "running", manual_enabled: false});
  f.socket.sent = [];
  assert.equal(f.quality.disabled, true);
  f.quality.value = "high";
  f.quality.dispatch("change");
  f.flushTimers();
  assert.equal(f.quality.value, "smooth");
  assert.equal(f.actions("resize").length, 0);
});

test("2x bitmaps retain every source pixel without shaders or changing pointer coordinates", async () => {
  const f = fixture({bitmapWidth: 1800, bitmapHeight: 1000});
  f.socket.dispatch("message", {data: new ArrayBuffer(3)});
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(f.canvas.width, 1800);
  assert.equal(f.canvas.height, 1000);
  assert.equal(f.canvas.style.width, "900px");
  assert.equal(f.canvas.style.height, "500px");
  const drawing = f.canvas.context;
  assert.equal(drawing.filter, "none");
  assert.equal(drawing.imageSmoothingQuality, "high");
  assert.equal(drawing.draws[0].length, 3, "The source bitmap is drawn 1:1, without an intermediate resize");
  f.canvas.dispatch("pointerdown", {pointerId: 1, button: 0, buttons: 1, clientX: 500, clientY: 280, detail: 1});
  assert.equal(f.actions("pointer")[0].x, 0.5);
  assert.equal(f.actions("pointer")[0].y, 0.5);
});

test("the address bar accepts arbitrary web pages, custom ports and local development addresses", async () => {
  const cases = new Map([
    ["https://example.org/path?q=中文#section", "https://example.org/path?q=%E4%B8%AD%E6%96%87#section"],
    ["example.com", "https://example.com/"],
    ["example.com:8443/path", "https://example.com:8443/path"],
    ["http://intranet:8080/", "http://intranet:8080/"],
    ["localhost:3000", "http://localhost:3000/"],
    ["127.0.0.1:5000/index", "http://127.0.0.1:5000/index"],
    ["[::1]:3000", "http://[::1]:3000/"],
  ]);
  for (const [address, expected] of cases) {
    const f = fixture();
    f.elements.address.value = address;
    f.elements.navigation.dispatch("submit");
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(f.requests.find(({url}) => url === "/api/browser/action")?.body.url, expected, address);
  }
});

test("the address bar rejects privileged schemes, credentials, empty hosts and invalid ports", async () => {
  const cases = ["javascript:alert(1)", "javascript:123", "file:///C:/Windows", "data:text/html,hello", "chrome://settings", "edge://settings", "ftp://example.com", "https:example.com", "https:///", "https://", "https:////example.com", "//example.com", "https://user:password@example.com", "https://@example.com", "https://example.com:65536", "example.com:0", "example.com:", "https://example.com:abc", "https://example.com\\path", "https://example.com/with space", "https://example.com/\npath", "\nhttps://example.com"];
  for (const address of cases) {
    const f = fixture();
    f.elements.address.value = address;
    f.elements.navigation.dispatch("submit");
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(f.requests.some(({url}) => url === "/api/browser/action"), false, address);
    assert.ok(f.elements["viewer-toast"].textContent, `A clear error should explain rejection: ${address}`);
  }
});
