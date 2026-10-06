// 交付字幕核验的前端绑定回归（node 直接运行，无框架）：
// 用最小 DOM/fetch 桩在 vm 中加载 app/static/app.js，验证——
// - 稿件切换以「提交动作」为唯一边界：提交甲稿后只在文件框改选乙稿而不提交，
//   本页改参数变轴或跨标签页变轴后刷新详情仍需重核时，自动重核必须读取
//   最近一次已提交的甲稿快照，绝不读取文件框当前的乙稿；乙稿只有提交后才
//   取代甲稿；迟到响应不得覆盖更新的提交或新项目；
// - 清空文件框但甲稿快照仍可读时，自动重核继续用甲稿；
// - 稿件快照不可读、详情刷新失败或有界重核失败时作废旧结论并提示重传；
// - 核验结果与「项目 + 生效轴 timeline_hash + 提交序号」绑定；
// - 参数重算/采纳/撤销改变生效轴（hash 变化）后：旧结论立即作废，
//   用已提交稿件快照按新生效轴重新核验，无快照时提示重新上传；
// - 生效轴未变（hash 相同）时结论保留，不发多余请求；
// - 跨标签页改轴（本页详情过期）：响应指纹不符时先拉取最新项目详情——
//   结论按新详情有效则直接接纳（不再重核、不循环请求），仍不符才用
//   已提交稿件快照按新轴重核；自动重核有终止条件，详情刷新失败 /
//   快照不可用 / 达到上限时停止并提示重新上传，不显示旧结论；
// - 再次提交、详情刷新或切换项目交错时，迟到的响应被丢弃，只呈现当前
//   项目最近一次有效提交的结果；响应到达即过期（等待期间生效轴被改）
//   时不展示。
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
    classList: { add() {}, remove() {}, toggle() {} },
    listeners: {},
    addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn); },
    appendChild(c) { this.children.push(c); return c; },
    reset() { this.files = []; if (this.delivery) this.delivery.files = []; },
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

// ---------- fetch 桩：响应时机完全由测试控制 ----------
const pending = [];   // 在途请求 {url, opts, resolve}
const fetchLog = [];
function fetchStub(url, opts = {}) {
  fetchLog.push({ url, method: opts.method || "GET", opts });
  return new Promise((resolve) => pending.push({ url, opts, resolve }));
}
function popPending(substr) {
  for (let i = pending.length - 1; i >= 0; i--) {
    if (pending[i].url.includes(substr)) return pending.splice(i, 1)[0];
  }
  throw new Error(`没有在途请求匹配 ${substr}`);
}
function popPendingExact(url) {   // 精确匹配（区分详情 GET 与列表/核验请求）
  for (let i = pending.length - 1; i >= 0; i--) {
    if (pending[i].url === url) return pending.splice(i, 1)[0];
  }
  throw new Error(`没有在途请求匹配 ${url}`);
}
function respondJson(req, body, ok = true, status = 200) {
  req.resolve({ ok, status, json: async () => body });
}
const flush = () => new Promise((r) => setImmediate(r));

class FormDataStub {
  constructor() { this.parts = []; }
  append(k, v) { this.parts.push([k, v]); }
}

// ---------- 在 vm 中加载 app.js，并暴露词法绑定的 state 与函数 ----------
const src = fs.readFileSync(APP_JS, "utf8") +
  "\n;globalThis.__app = { state, render, renderDeliveryVerify," +
  " syncDeliveryVerify, resetDeliveryVerify, runDeliveryVerify," +
  " submittedVerifyFile, scheduleAutoDeliveryVerify };";
const sandbox = {
  document: documentStub,
  fetch: fetchStub,
  FormData: FormDataStub,
  setTimeout: () => 0,
  clearTimeout: () => {},
  console,
};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "app.js" });
const app = sandbox.__app;
const { state } = app;

// ---------- 测试工具 ----------
function proj(id, hash) {
  return {
    id, name: `P${id}`, cps_limit: 21, min_gap_ms: 40, tolerance_ms: 200,
    created_at: "", timeline_hash: hash,
    shots: [], cues: [],
    analysis: {
      cues: [], chains: [], intrinsic: [],
      stats: { cue_count: 0, shot_count: 0, gap_violations: 0,
               cps_violations: 0, cross_shot_count: 0, proposal_count: 0,
               proposal_moved_count: 0, conflict_cue_count: 0,
               total_shift_ms: 0 },
    },
    gap_review: null,
  };
}
function verifyResult(pid, hash, pass) {
  return { project_id: pid, project_name: `P${pid}`, delivery_name: "d.srt",
           timeline_hash: hash, reference_count: 2, delivered_count: 2,
           matched_text_count: 2,
           summary: { pass, time_mismatch: 0, missing: 0, new: 0,
                      pending: 0 },
           rows: [] };
}
function verifyPostCount() {
  return fetchLog.filter((r) => r.url.includes("/verify-delivery")).length;
}

const deliveryForm = documentStub.querySelector("#deliveryVerifyForm");
deliveryForm.delivery = makeEl("input");
const settingsForm = documentStub.querySelector("#settingsForm");
settingsForm.cps_limit = makeEl("input");
settingsForm.min_gap_ms = makeEl("input");
settingsForm.tolerance_ms = makeEl("input");

function clearVerifyState() {
  state.deliveryVerify = null;
  state.verifyNotice = "";
  state.verifySnapshot = null;
  state.verifyAutoInFlight = false;
  state.verifySeq++;
}

// 模拟提交动作（点击「上传并核验」）：提交是稿件切换边界——只有这里会
// 写入已提交稿件快照。runDeliveryVerify 本身不改快照，与生产代码一致。
function submitDelivery(file) {
  state.verifySnapshot = { file, name: String(file), projectId: state.project.id };
  state.verifyAutoInFlight = false;
  state.verifyNotice = "";
  return app.runDeliveryVerify(file, false);
}

// 取最近一次核验请求实际携带的文件（FormData 的 delivery 部分），
// 用于断言自动重核用的是哪一份稿。
function lastPostedFile() {
  for (let i = fetchLog.length - 1; i >= 0; i--) {
    const r = fetchLog[i];
    if (r.url.includes("/verify-delivery") && r.opts && r.opts.body) {
      const part = r.opts.body.parts.find(([k]) => k === "delivery");
      return part ? part[1] : null;
    }
  }
  return null;
}

// ---------- 场景 ----------

async function test_manual_upload_binds_result() {
  clearVerifyState();
  state.project = proj(1, "h1");
  deliveryForm.delivery.files = ["fileA"];
  const p = submitDelivery("fileA");
  assert.strictEqual(verifyPostCount(), 1, "应发出一次核验请求");
  respondJson(popPending("verify-delivery"), verifyResult(1, "h1", 2));
  await p; await flush();
  assert.ok(state.deliveryVerify, "结果应展示");
  assert.strictEqual(state.deliveryVerify.timeline_hash, "h1");
  assert.strictEqual(state.deliveryVerify.project_id, 1);
  assert.strictEqual(app.submittedVerifyFile(), "fileA",
                     "提交后应记住已提交稿件快照");
  console.log("frontend manual upload binds project+timeline ok");
}

async function test_timeline_change_auto_reverify() {
  // 参数重算改变生效轴（h1 → h2）：旧结论立即作废，
  // 自动用最近一次已提交的稿件按新生效轴重新核验
  state.project = proj(1, "h2");
  app.render();
  assert.strictEqual(state.deliveryVerify, null, "旧结论必须立即作废");
  assert.ok(state.verifyNotice.includes("重新核验"), "应提示正在重核");
  assert.strictEqual(verifyPostCount(), 2, "应自动发起重新核验");
  assert.strictEqual(lastPostedFile(), "fileA",
                     "自动重核必须使用已提交稿件快照");
  respondJson(popPending("verify-delivery"), verifyResult(1, "h2", 1));
  await flush();
  assert.ok(state.deliveryVerify, "自动重核结果应展示");
  assert.strictEqual(state.deliveryVerify.timeline_hash, "h2");
  assert.strictEqual(state.deliveryVerify.summary.pass, 1);
  console.log("frontend timeline change -> invalidate + auto re-verify ok");
}

async function test_timeline_unchanged_keeps_result() {
  // 生效轴未变（如仅调 CPS 上限 / 相同参数重提）：结论保留，不发请求
  state.project = proj(1, "h2");
  const before = verifyPostCount();
  app.render();
  assert.strictEqual(verifyPostCount(), before, "不应发起新核验");
  assert.ok(state.deliveryVerify, "有效结论应保留");
  assert.strictEqual(state.deliveryVerify.timeline_hash, "h2");
  console.log("frontend unchanged timeline keeps result ok");
}

async function test_concurrent_uploads_last_wins() {
  // 连续两次提交：第一次的响应迟到，不得覆盖第二次提交的结果
  clearVerifyState();
  state.project = proj(1, "h2");
  deliveryForm.delivery.files = ["fileB"];
  const p1 = submitDelivery("fileB");
  const req1 = popPending("verify-delivery");
  deliveryForm.delivery.files = ["fileB2"];
  const p2 = submitDelivery("fileB2");
  const req2 = popPending("verify-delivery");
  respondJson(req2, verifyResult(1, "h2", 5));   // 第二次先返回
  await p2; await flush();
  assert.strictEqual(state.deliveryVerify.summary.pass, 5);
  assert.strictEqual(app.submittedVerifyFile(), "fileB2",
                     "快照应为最近一次提交的稿件");
  respondJson(req1, verifyResult(1, "h2", 99));  // 第一次迟到
  await p1; await flush();
  assert.strictEqual(state.deliveryVerify.summary.pass, 5,
                     "迟到响应不得覆盖最近一次提交的结果");
  console.log("frontend concurrent uploads: last response wins ok");
}

async function test_switch_project_discards_inflight() {
  // 提交未完成时切换项目：迟到响应不得落到新项目
  clearVerifyState();
  state.project = proj(1, "h2");
  deliveryForm.delivery.files = ["fileC"];
  const p = submitDelivery("fileC");
  const req = popPending("verify-delivery");
  app.resetDeliveryVerify();          // 切换项目的前置清理（在途失效）
  state.project = proj(2, "k1");
  respondJson(req, verifyResult(1, "h2", 7));
  await p; await flush();
  assert.strictEqual(state.deliveryVerify, null,
                     "切换项目后迟到响应必须丢弃");
  console.log("frontend switch project discards in-flight response ok");
}

async function test_clear_input_keeps_submitted_snapshot() {
  // 核心回归（改选未提交 / 清空文件框）：提交甲稿后**清空文件框**，
  // 甲稿快照仍可读取 → 变轴后自动重核继续用甲稿，而不是提示重传。
  clearVerifyState();
  state.project = proj(2, "k1");
  deliveryForm.delivery.files = ["fileA-submitted"];
  const p = submitDelivery("fileA-submitted");
  respondJson(popPending("verify-delivery"), verifyResult(2, "k1", 3));
  await p; await flush();
  assert.ok(state.deliveryVerify);
  deliveryForm.delivery.files = [];          // 用户清空了文件框
  assert.strictEqual(app.submittedVerifyFile(), "fileA-submitted",
                     "清空文件框不影响已提交稿件快照");
  state.project = proj(2, "k2");
  app.render();
  assert.strictEqual(state.deliveryVerify, null, "旧结论必须立即作废");
  assert.strictEqual(lastPostedFile(), "fileA-submitted",
                     "清空文件框后仍须用甲稿快照自动重核");
  respondJson(popPending("verify-delivery"), verifyResult(2, "k2", 2));
  await flush();
  assert.ok(state.deliveryVerify, "甲稿快照的重核结果应展示");
  assert.strictEqual(state.deliveryVerify.timeline_hash, "k2");
  console.log("frontend clear input keeps submitted snapshot ok");
}

async function test_reselect_without_submit_ignored_on_reverify() {
  // 核心回归（不得把乙的结论当作甲的）：提交甲稿后只在文件框改选乙稿
  // 而**不提交**，随后本页改参数改变生效轴——自动重核必须携带甲稿，
  // 不得读取文件框当前的乙稿。
  clearVerifyState();
  state.project = proj(3, "m1");
  deliveryForm.delivery.files = ["manuscript-A"];
  const p = submitDelivery("manuscript-A");
  respondJson(popPending("verify-delivery"), verifyResult(3, "m1", 2));
  await p; await flush();
  assert.strictEqual(app.submittedVerifyFile(), "manuscript-A");
  deliveryForm.delivery.files = ["manuscript-B"];  // 只改选，不提交
  state.project = proj(3, "m2");                   // 本页改参数变轴
  app.render();
  assert.strictEqual(state.deliveryVerify, null, "旧结论必须立即作废");
  assert.strictEqual(lastPostedFile(), "manuscript-A",
                     "改选未提交的乙稿不得参与自动重核，必须仍用甲稿");
  assert.ok(state.verifyNotice.includes("重新核验"));
  // 响应按甲稿在新轴上的结论（与乙稿会给出的结论不同）
  const r = verifyResult(3, "m2", 0);
  r.summary = { pass: 0, time_mismatch: 2, missing: 0, new: 0, pending: 0 };
  respondJson(popPending("verify-delivery"), r);
  await flush();
  assert.ok(state.deliveryVerify, "应展示甲稿（而非乙稿）的重核结论");
  assert.strictEqual(state.deliveryVerify.summary.time_mismatch, 2);
  assert.strictEqual(app.submittedVerifyFile(), "manuscript-A",
                     "乙稿未提交，不得取代甲稿快照");
  console.log("frontend reselect without submit ignored on re-verify ok");
}

async function test_submit_b_replaces_a_then_reverify_uses_b() {
  // 接上一情境：乙稿只有在**提交之后**才可取代甲稿——提交乙稿后变轴，
  // 自动重核必须携带乙稿。
  state.project = proj(3, "m3");
  deliveryForm.delivery.files = ["manuscript-B"];  // 此时文件框已是乙稿
  const p = submitDelivery("manuscript-B");        // 提交：切换边界
  assert.strictEqual(app.submittedVerifyFile(), "manuscript-B",
                     "提交后乙稿取代甲稿");
  respondJson(popPending("verify-delivery"), verifyResult(3, "m3", 2));
  await p; await flush();
  state.project = proj(3, "m4");
  app.render();
  assert.strictEqual(lastPostedFile(), "manuscript-B",
                     "提交乙稿后自动重核应使用乙稿");
  respondJson(popPending("verify-delivery"), verifyResult(3, "m4", 1));
  await flush();
  assert.strictEqual(state.deliveryVerify.timeline_hash, "m4");
  console.log("frontend submit B replaces A; re-verify uses B ok");
}

async function test_unreadable_snapshot_asks_reupload() {
  // 已提交稿件快照已不可读（如浏览器回收文件句柄）：自动重核请求无法
  // 携带该文件而失败 → 作废旧结论并提示重新上传，不回退文件框改选稿。
  clearVerifyState();
  state.project = proj(2, "k1");
  deliveryForm.delivery.files = ["fileGone"];
  const p = submitDelivery("fileGone");
  respondJson(popPending("verify-delivery"), verifyResult(2, "k1", 2));
  await p; await flush();
  assert.ok(state.deliveryVerify);
  // 文件框改选了乙稿但未提交；甲稿快照同时已不可读
  deliveryForm.delivery.files = ["manuscript-B"];
  state.verifySnapshot.file = { unreadable: true };
  state.project = proj(2, "k2");
  app.render();
  respondJson(popPending("verify-delivery"),
              { detail: "回传稿无法读取" }, false, 400);
  await flush();
  assert.strictEqual(state.deliveryVerify, null, "不可读时不得保留旧结论");
  assert.ok(state.verifyNotice.includes("重新上传"), "应提示重新上传");
  console.log("frontend unreadable snapshot -> ask re-upload ok");
}

async function test_stale_on_arrival_reverified() {
  // 响应等待期间另一标签页改了参数（本地已刷新到 k3）：
  // 按旧轴 k2 算出的结果到达即过期 → 先拉取最新详情（仍为 k3）裁决，
  // 结论不可接纳 → 自动用已提交稿件快照按新轴重核，不展示过期结果
  clearVerifyState();
  state.project = proj(2, "k2");
  deliveryForm.delivery.files = ["fileE"];
  const p = submitDelivery("fileE");
  const req = popPending("verify-delivery");
  state.project = proj(2, "k3");      // 生效轴在等待期间变化
  const before = verifyPostCount();
  respondJson(req, verifyResult(2, "k2", 4));
  await flush();
  respondJson(popPendingExact("/api/projects/2"), proj(2, "k3"));  // 详情仍 k3
  await flush();
  assert.strictEqual(verifyPostCount(), before + 1,
                     "到达即过期应自动按新生效轴重核");
  assert.strictEqual(lastPostedFile(), "fileE",
                     "跨标签页变轴后的重核仍须用已提交稿件快照");
  assert.ok(!state.deliveryVerify ||
            state.deliveryVerify.timeline_hash === "k3",
            "过期结果不得展示");
  respondJson(popPending("verify-delivery"), verifyResult(2, "k3", 4));
  await p; await flush();
  assert.strictEqual(state.deliveryVerify.timeline_hash, "k3");
  console.log("frontend stale-on-arrival discarded + re-verified ok");
}

async function test_stale_reverify_ignores_unsubmitted_reselect() {
  // 跨标签页变轴、详情刷新后仍需重核的路径上，文件框改选了乙稿但未提交：
  // 重核仍必须携带甲稿快照，绝不能读取文件框当前的乙稿。
  clearVerifyState();
  state.project = proj(2, "k2");
  deliveryForm.delivery.files = ["manuscript-A"];
  const p = submitDelivery("manuscript-A");
  const req = popPending("verify-delivery");
  state.project = proj(2, "k3");                 // 等待期间跨标签页变轴
  deliveryForm.delivery.files = ["manuscript-B"];  // 同时只改选、不提交
  respondJson(req, verifyResult(2, "k2", 4));
  await flush();
  respondJson(popPendingExact("/api/projects/2"), proj(2, "k4"));  // 又变轴
  await flush();
  assert.strictEqual(lastPostedFile(), "manuscript-A",
                     "裁决后重核必须用已提交的甲稿，而非文件框的乙稿");
  respondJson(popPending("verify-delivery"), verifyResult(2, "k4", 4));
  await p; await flush();
  assert.strictEqual(state.deliveryVerify.timeline_hash, "k4");
  console.log("frontend stale re-verify ignores unsubmitted reselect ok");
}

async function test_auto_reverify_failure_asks_reupload() {
  // 自动重核失败（如回传稿解析失败）：作废旧结论，提示人工重传
  clearVerifyState();
  state.project = proj(2, "k3");
  deliveryForm.delivery.files = ["fileF"];
  const p = submitDelivery("fileF");
  respondJson(popPending("verify-delivery"), verifyResult(2, "k3", 2));
  await p; await flush();
  assert.ok(state.deliveryVerify);
  state.project = proj(2, "k4");
  app.render();                        // 触发自动重核
  respondJson(popPending("verify-delivery"),
              { detail: "交付字幕解析失败：bad" }, false, 400);
  await flush();
  assert.strictEqual(state.deliveryVerify, null, "失败后不得保留旧结论");
  assert.ok(state.verifyNotice.includes("重新上传"), "应提示重新上传");
  console.log("frontend auto re-verify failure -> ask re-upload ok");
}

async function test_cross_tab_change_accepted_after_detail_refresh() {
  // 核心回归（循环请求修复）：另一标签页改了最小间隔，本页详情仍旧（c1）。
  // 核验响应携带新指纹 c2——旧实现会判过期并无界自动重核且永无结论；
  // 修复后应先拉取最新详情（c2），结论按新详情可接纳：直接展示、
  // 页面详情同步到最新，且不再发起任何重核请求。
  clearVerifyState();
  state.project = proj(7, "c1");               // 本页详情过期
  deliveryForm.delivery.files = ["fileX"];
  const base = verifyPostCount();
  const p = submitDelivery("fileX");
  respondJson(popPending("verify-delivery"), verifyResult(7, "c2", 4));
  await flush();
  respondJson(popPendingExact("/api/projects/7"), proj(7, "c2"));  // 最新详情
  await p; await flush();
  assert.strictEqual(verifyPostCount(), base + 1,
                     "结论可按新详情接纳时不得重复发起核验（无循环请求）");
  assert.ok(state.deliveryVerify, "按新详情有效的结论应展示");
  assert.strictEqual(state.deliveryVerify.timeline_hash, "c2");
  assert.strictEqual(state.deliveryVerify.summary.pass, 4);
  assert.strictEqual(state.project.timeline_hash, "c2",
                     "页面详情应同步到最新");
  console.log("frontend cross-tab change accepted after detail refresh ok");
}

async function test_repeated_timeline_changes_stop_reverify() {
  // 多次变轴：每次响应到达时生效轴都已再次变化（详情也一路变化），
  // 自动重核必须有终止条件——达到上限后停止、作废旧结论并提示重传，
  // 不无界发请求。
  clearVerifyState();
  state.project = proj(4, "r1");
  deliveryForm.delivery.files = ["fileR"];
  const base = verifyPostCount();
  const p = submitDelivery("fileR");          // 第 1 次核验
  respondJson(popPending("verify-delivery"), verifyResult(4, "r2", 1));
  await flush();
  respondJson(popPendingExact("/api/projects/4"), proj(4, "r3"));
  await flush();                                          // 仍不符 → 第 2 次
  respondJson(popPending("verify-delivery"), verifyResult(4, "r4", 1));
  await flush();
  respondJson(popPendingExact("/api/projects/4"), proj(4, "r5"));
  await flush();                                          // 仍不符 → 第 3 次
  respondJson(popPending("verify-delivery"), verifyResult(4, "r6", 1));
  await flush();
  respondJson(popPendingExact("/api/projects/4"), proj(4, "r7"));
  await p; await flush();                                 // 达到上限 → 停止
  assert.strictEqual(verifyPostCount(), base + 3,
                     "自动重核链必须有终止条件（最多 3 次核验请求）");
  assert.strictEqual(state.deliveryVerify, null,
                     "达到上限后不得显示旧结论");
  assert.ok(state.verifyNotice.includes("重新上传"), "应提示重新上传");
  console.log("frontend repeated timeline changes -> bounded stop ok");
}

async function test_detail_refresh_failure_stops() {
  // 详情刷新失败（如项目已被删除/网络错误）：停止并提示重传，
  // 不显示旧结论，也不再发核验请求
  clearVerifyState();
  state.project = proj(5, "f1");
  deliveryForm.delivery.files = ["fileF"];
  const base = verifyPostCount();
  const p = submitDelivery("fileF");
  respondJson(popPending("verify-delivery"), verifyResult(5, "f2", 1));
  await flush();
  respondJson(popPendingExact("/api/projects/5"),
              { detail: "项目不存在" }, false, 404);
  await p; await flush();
  assert.strictEqual(verifyPostCount(), base + 1,
                     "详情刷新失败不得继续重核");
  assert.strictEqual(state.deliveryVerify, null, "不得显示旧结论");
  assert.ok(state.verifyNotice.includes("重新上传"), "应提示重新上传");
  console.log("frontend detail refresh failure -> stop + ask re-upload ok");
}

async function test_no_snapshot_during_reconcile_stops() {
  // 等待详情期间项目切换语义下快照被清空（或已无已提交稿）：生效轴仍
  // 不符时不得自动重核，提示重传。文件框清空本身不清快照——这里直接
  // 模拟「没有已提交快照」的情形。
  clearVerifyState();
  state.project = proj(6, "n1");
  deliveryForm.delivery.files = ["fileN"];
  const base = verifyPostCount();
  const p = submitDelivery("fileN");
  respondJson(popPending("verify-delivery"), verifyResult(6, "n2", 1));
  await flush();
  state.verifySnapshot = null;               // 已提交稿件不可用（非文件框清空）
  respondJson(popPendingExact("/api/projects/6"), proj(6, "n3"));
  await p; await flush();
  assert.strictEqual(verifyPostCount(), base + 1,
                     "无已提交快照不得继续重核");
  assert.strictEqual(state.deliveryVerify, null, "不得显示旧结论");
  assert.ok(state.verifyNotice.includes("重新上传"), "应提示重新上传");
  console.log("frontend no snapshot during reconcile -> stop + ask re-upload ok");
}

async function test_reconcile_discards_after_project_switch() {
  // 详情刷新在途时切换项目：迟到的详情响应不得落到新项目
  clearVerifyState();
  state.project = proj(8, "s1");
  deliveryForm.delivery.files = ["fileS"];
  const p = submitDelivery("fileS");
  respondJson(popPending("verify-delivery"), verifyResult(8, "s2", 1));
  await flush();
  const getReq = popPendingExact("/api/projects/8");
  app.resetDeliveryVerify();                  // 切换项目的前置清理（在途失效）
  state.project = proj(9, "t1");
  respondJson(getReq, proj(8, "s3"));         // 旧项目的详情迟到
  await p; await flush();
  assert.strictEqual(state.deliveryVerify, null,
                     "切换项目后迟到详情必须丢弃");
  assert.strictEqual(state.project.id, 9);
  assert.strictEqual(state.project.timeline_hash, "t1",
                     "新项目详情不得被旧响应覆盖");
  console.log("frontend reconcile discarded after project switch ok");
}

async function test_reconcile_discards_when_newer_submit() {
  // 详情刷新在途时再次提交：旧链一律丢弃，只呈现最近一次提交的结果
  clearVerifyState();
  state.project = proj(10, "u1");
  deliveryForm.delivery.files = ["fileU"];
  const p1 = submitDelivery("fileU");
  respondJson(popPending("verify-delivery"), verifyResult(10, "u2", 1));
  await flush();
  const getReq = popPendingExact("/api/projects/10");
  deliveryForm.delivery.files = ["fileU2"];
  const p2 = submitDelivery("fileU2");        // 再次提交（更新序号 + 换快照）
  respondJson(getReq, proj(10, "u3"));        // 旧链的详情迟到
  await p1; await flush();
  assert.strictEqual(state.deliveryVerify, null, "旧链不得展示结论");
  // 第二次提交的响应 u3 与本地 u1 不符 → 再经一次详情同步后接纳
  respondJson(popPending("verify-delivery"), verifyResult(10, "u3", 6));
  await flush();
  respondJson(popPendingExact("/api/projects/10"), proj(10, "u3"));
  await p2; await flush();
  assert.ok(state.deliveryVerify, "最近一次提交的结果应展示");
  assert.strictEqual(state.deliveryVerify.summary.pass, 6);
  assert.strictEqual(state.deliveryVerify.timeline_hash, "u3");
  console.log("frontend reconcile discarded when newer submit ok");
}

async function main() {
  await test_manual_upload_binds_result();
  await test_timeline_change_auto_reverify();
  await test_timeline_unchanged_keeps_result();
  await test_concurrent_uploads_last_wins();
  await test_switch_project_discards_inflight();
  await test_clear_input_keeps_submitted_snapshot();
  await test_reselect_without_submit_ignored_on_reverify();
  await test_submit_b_replaces_a_then_reverify_uses_b();
  await test_unreadable_snapshot_asks_reupload();
  await test_stale_on_arrival_reverified();
  await test_stale_reverify_ignores_unsubmitted_reselect();
  await test_auto_reverify_failure_asks_reupload();
  await test_cross_tab_change_accepted_after_detail_refresh();
  await test_repeated_timeline_changes_stop_reverify();
  await test_detail_refresh_failure_stops();
  await test_no_snapshot_during_reconcile_stops();
  await test_reconcile_discards_after_project_switch();
  await test_reconcile_discards_when_newer_submit();
  console.log("frontend delivery verify binding regression ok");
}

main().catch((e) => { console.error(e); process.exit(1); });
