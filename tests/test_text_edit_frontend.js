// 逐条文案修订的前端回归（node 直接运行，无框架）：
// 用最小 DOM/fetch 桩在 vm 中加载 app/static/app.js，验证——
// - 行内「修订」打开独立编辑面板：预填当前文案，render 全量重建表格时
//   编辑框草稿不被覆盖（面板在表格之外）；
// - 保存携带 text + base_text（打开编辑时的当前文案）；成功后关闭面板；
// - 另一标签页先改过（409）：拒绝覆盖，编辑框载入服务端回传的最新文案、
//   编辑基准同步更新，提示用户；随后再次保存以最新文案为 base 才成功——
//   不会用旧 base 覆盖别人的修订；
// - 恢复导入原文走 restore 端点，同样携带 base_text、同样处理 409；
// - 空白稿被 400 拒绝时面板保持打开、不关闭不丢草稿；
// - 切换/关闭项目（render 无项目）时编辑面板一并关闭。
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const APP_JS = path.join(__dirname, "..", "app", "static", "app.js");

// ---------- 最小 DOM 桩 ----------
function makeEl(tag = "div") {
  return {
    tagName: tag,
    innerHTML: "",
    textContent: "",
    value: "",
    href: "",
    files: [],
    children: [],
    dataset: {},
    style: {},
    classList: { add() {}, remove() {}, toggle() {} },
    listeners: {},
    addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn); },
    appendChild(c) { this.children.push(c); return c; },
    reset() { this.files = []; },
    focus() {},
    closest() { return null; },
    querySelector() { return makeEl(); },
    querySelectorAll() { return []; },
  };
}

const bySelector = new Map();
const documentStub = {
  querySelector(sel) {
    if (!bySelector.has(sel)) bySelector.set(sel, makeEl());
    return bySelector.get(sel);
  },
  querySelectorAll() { return []; },
  createElement(tag) { return makeEl(tag); },
};

// ---------- fetch 桩 ----------
const pending = [];
const fetchLog = [];
function fetchStub(url, opts = {}) {
  fetchLog.push({ url, method: opts.method || "GET", opts });
  return new Promise((resolve) => pending.push({ url, opts, resolve }));
}
function popPending(substr, method) {
  for (let i = pending.length - 1; i >= 0; i--) {
    const r = pending[i];
    if (r.url.includes(substr) && (!method || (r.opts.method || "GET") === method)) {
      return pending.splice(i, 1)[0];
    }
  }
  throw new Error(`没有在途请求匹配 ${method || ""} ${substr}`);
}
function respondJson(req, body, ok = true, status = 200) {
  req.resolve({ ok, status, json: async () => body });
}
const flush = () => new Promise((r) => setImmediate(r));

// ---------- 加载 app.js ----------
const src = fs.readFileSync(APP_JS, "utf8") +
  "\n;globalThis.__app = { state, render, openTextEdit, saveTextEdit," +
  " restoreTextEdit, closeTextEdit, updateTextEditPreview," +
  " normalizeDraftText, countDraftGraphemes };";
const sandbox = {
  document: documentStub,
  fetch: fetchStub,
  FormData: class { constructor() { this.parts = []; } append(k, v) { this.parts.push([k, v]); } },
  setTimeout: () => 0,
  clearTimeout: () => {},
  console,
  Intl,
};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "app.js" });
const app = sandbox.__app;
const { state } = app;

// settingsForm 的子输入框（render 会写入当前参数值）
const settingsForm = documentStub.querySelector("#settingsForm");
settingsForm.cps_limit = makeEl("input");
settingsForm.min_gap_ms = makeEl("input");
settingsForm.tolerance_ms = makeEl("input");

// ---------- 测试工具 ----------
function cue(uid, text, extra = {}) {
  return {
    uid, cue_id: String(uid), ord: uid,
    start_ms: 0, end_ms: 1000, duration_ms: 1000,
    text, imported_text: text, edited: false,
    graphemes: [...text].length, raw_hash: "ab".repeat(32),
    cps: 1, cps_ok: true, overlap_with: null, cross_shot: false,
    proposal_start_ms: 0, proposal_end_ms: 1000,
    adopted: false, adopted_start_ms: null, adopted_end_ms: null,
    ...extra,
  };
}
function proj(id, cues, hash = "h1") {
  return {
    id, name: `P${id}`, cps_limit: 21, min_gap_ms: 40, tolerance_ms: 200,
    created_at: "", timeline_hash: hash,
    shots: [], cues,
    analysis: {
      cues, chains: [], intrinsic: [],
      stats: { cue_count: cues.length, shot_count: 0, gap_violations: 0,
               cps_violations: 0, cross_shot_count: 0, proposal_count: 0,
               proposal_moved_count: 0, conflict_cue_count: 0,
               total_shift_ms: 0 },
    },
    gap_review: null,
  };
}
const editInput = () => bySelector.get("#textEditInput");
const editPanel = () => bySelector.get("#textEditPanel");
const editHint = () => bySelector.get("#textEditHint");

function lastPutBody() {
  for (let i = fetchLog.length - 1; i >= 0; i--) {
    const r = fetchLog[i];
    if (r.url.includes("/text") && r.opts.method === "PUT") {
      return JSON.parse(r.opts.body);
    }
  }
  return null;
}

// ---------- 场景 ----------

async function test_open_prefills_and_render_keeps_draft() {
  state.project = proj(1, [cue(11, "甲"), cue(12, "乙")]);
  app.openTextEdit(11);
  assert.strictEqual(editInput().value, "甲", "打开时应预填当前文案");
  assert.strictEqual(state.textEditing.baseText, "甲");
  // 用户输入草稿后，任意 render（如参数重算回来）不得覆盖草稿
  editInput().value = "甲（草稿）";
  app.render();
  assert.strictEqual(editInput().value, "甲（草稿）",
                     "render 不得覆盖编辑框草稿");
  app.closeTextEdit();
  console.log("frontend open prefill + render keeps draft ok");
}

async function test_save_success_closes() {
  state.project = proj(1, [cue(11, "甲"), cue(12, "乙")]);
  app.openTextEdit(11);
  editInput().value = "甲（修订）";
  const p = app.saveTextEdit();
  const req = popPending("/cues/11/text", "PUT");
  const body = JSON.parse(req.opts.body);
  assert.strictEqual(body.text, "甲（修订）");
  assert.strictEqual(body.base_text, "甲", "保存须携带编辑基准 base_text");
  respondJson(req, proj(1, [cue(11, "甲（修订）",
                                 { imported_text: "甲", edited: true }),
                            cue(12, "乙")], "h2"));
  await p; await flush();
  assert.strictEqual(state.textEditing, null, "保存成功应关闭面板");
  assert.strictEqual(state.project.analysis.cues[0].text, "甲（修订）");
  console.log("frontend save success closes panel ok");
}

async function test_save_409_loads_latest_text() {
  // 核心回归：另一标签页先把「甲」改为「A稿」；本页仍基于「甲」保存 →
  // 409，编辑框必须载入最新文案「A稿」，且不得用旧 base 覆盖
  state.project = proj(1, [cue(11, "甲"), cue(12, "乙")]);
  app.openTextEdit(11);
  editInput().value = "B稿";
  const p = app.saveTextEdit();
  respondJson(popPending("/cues/11/text", "PUT"), {
    detail: { message: "该条文案已在别处被修改，请基于最新文案重新编辑",
              current_text: "A稿", edited: true },
  }, false, 409);
  await p; await flush();
  assert.ok(state.textEditing, "409 后面板应保持打开");
  assert.strictEqual(editInput().value, "A稿",
                     "409 后编辑框必须显示最新文案");
  assert.strictEqual(state.textEditing.baseText, "A稿",
                     "编辑基准须同步到最新文案");
  assert.ok(editHint().textContent.includes("最新文案"),
            "应提示已载入最新文案");
  // 详情刷新请求（refreshAfterConflict）：回最新项目
  respondJson(popPending("/api/projects/1", "GET"),
              proj(1, [cue(11, "A稿", { imported_text: "甲", edited: true }),
                       cue(12, "乙")], "h3"));
  await flush();
  // 用户在最新文案基础上继续编辑并保存：base 必须是「A稿」
  editInput().value = "B稿";
  const p2 = app.saveTextEdit();
  const body = lastPutBody();
  assert.strictEqual(body.base_text, "A稿",
                     "再次保存必须以最新文案为 base，不得覆盖他人修订");
  assert.strictEqual(body.text, "B稿");
  respondJson(popPending("/cues/11/text", "PUT"),
              proj(1, [cue(11, "B稿", { imported_text: "甲", edited: true }),
                       cue(12, "乙")], "h4"));
  await p2; await flush();
  assert.strictEqual(state.textEditing, null, "二次保存成功应关闭面板");
  console.log("frontend save 409 -> show latest text + rebase ok");
}

async function test_restore_and_409() {
  state.project = proj(1, [cue(11, "A稿", { imported_text: "甲", edited: true }),
                           cue(12, "乙")]);
  app.openTextEdit(11);
  const p = app.restoreTextEdit();
  const req = popPending("/cues/11/text/restore", "POST");
  assert.strictEqual(JSON.parse(req.opts.body).base_text, "A稿",
                     "恢复须携带编辑基准 base_text");
  respondJson(req, proj(1, [cue(11, "甲"), cue(12, "乙")], "h5"));
  await p; await flush();
  assert.strictEqual(state.textEditing, null, "恢复成功应关闭面板");
  assert.strictEqual(state.project.analysis.cues[0].text, "甲");

  // 恢复时另一标签页已先改 → 409：不得把别人的修订覆盖回原文
  state.project = proj(1, [cue(11, "B稿", { imported_text: "甲", edited: true }),
                           cue(12, "乙")]);
  app.openTextEdit(11);
  const p2 = app.restoreTextEdit();
  respondJson(popPending("/cues/11/text/restore", "POST"), {
    detail: { message: "该条文案已在别处被修改，请基于最新文案重新编辑",
              current_text: "C稿", edited: true },
  }, false, 409);
  await p2; await flush();
  assert.ok(state.textEditing, "409 后面板应保持打开");
  assert.strictEqual(editInput().value, "C稿",
                     "恢复 409 同样须显示最新文案");
  respondJson(popPending("/api/projects/1", "GET"),
              proj(1, [cue(11, "C稿", { imported_text: "甲", edited: true }),
                       cue(12, "乙")], "h6"));
  await flush();
  app.closeTextEdit();
  console.log("frontend restore + restore 409 ok");
}

async function test_blank_rejected_keeps_panel() {
  state.project = proj(1, [cue(11, "甲"), cue(12, "乙")]);
  app.openTextEdit(11);
  editInput().value = "   ";
  const p = app.saveTextEdit();
  respondJson(popPending("/cues/11/text", "PUT"),
              { detail: "文案不能为空：去标签、折叠空白后需至少保留一个可见字符" },
              false, 400);
  await p; await flush();
  assert.ok(state.textEditing, "空白稿被拒后面板应保持打开");
  assert.strictEqual(editInput().value, "   ", "被拒后草稿不丢");
  app.closeTextEdit();
  console.log("frontend blank rejected keeps panel ok");
}

async function test_panel_closed_when_project_gone() {
  state.project = proj(1, [cue(11, "甲")]);
  app.openTextEdit(11);
  assert.ok(state.textEditing);
  state.project = null;          // 删除 / 新建项目
  app.render();
  assert.strictEqual(state.textEditing, null,
                     "项目关闭后编辑面板必须一并关闭");
  console.log("frontend panel closed with project ok");
}

async function test_normalize_and_grapheme_preview() {
  // 前端预览与服务端同一套规则：去标签、折叠空白、UAX #29 字素簇
  assert.strictEqual(app.normalizeDraftText("  <i>你</i>\n\t好  "), "你 好");
  assert.strictEqual(app.normalizeDraftText("<b></b> "), "");
  assert.strictEqual(app.countDraftGraphemes("你好 👨‍👩‍👧"), 4);
  assert.strictEqual(app.countDraftGraphemes("   "), 0);
  console.log("frontend normalize + grapheme preview ok");
}

async function main() {
  await test_open_prefills_and_render_keeps_draft();
  await test_save_success_closes();
  await test_save_409_loads_latest_text();
  await test_restore_and_409();
  await test_blank_rejected_keeps_panel();
  await test_panel_closed_when_project_gone();
  await test_normalize_and_grapheme_preview();
  console.log("frontend text edit regression ok");
}

main().catch((e) => { console.error(e); process.exit(1); });
