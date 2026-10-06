// 字幕节奏与镜头边界校正台 —— 原生 JS 前端
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  project: null,
  deliveryVerify: null,  // 当前展示的核验结果（绑定 project_id + timeline_hash）
  verifySeq: 0,          // 单调递增的提交序号：使迟到的旧响应失效
  verifyNotice: "",      // 无结果时核验区显示的提示（已作废 / 重新核验中）
  // 稿件快照：只有「上传并核验」提交动作才会更新它。文件框里改选了另一份
  // 稿子但没点提交时，快照仍是上一份已提交稿；自动重核与跨标签页裁决一律
  // 读取快照，绝不读取文件框当前值，避免把改选未提交稿的结论当成旧稿的。
  verifySnapshot: null,  // {file, name, projectId} 最近一次已提交的稿件
  verifyAutoInFlight: false,  // 同一生效轴上的自动重核是否在途（防重复触发）
  // 文案修订：{uid, baseText}。baseText 是打开编辑时该条的当前文案，保存 /
  // 恢复时随请求上送，服务端据此识别旧页面提交（另一标签页先改过则 409）。
  textEditing: null,
};

const VERIFY_LABEL = {
  pass: "通过",
  time_mismatch: "时间不符",
  missing: "缺失",
  new: "新增",
  pending: "待确认",
};

// ---------- 工具 ----------
function toast(msg, isErr = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("err", isErr);
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3200);
}

function fmtMs(ms) {
  if (ms == null) return "—";
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  const s = Math.floor((ms % 60000) / 1000);
  const x = ms % 1000;
  return [h, m, s].map(v => String(v).padStart(2, "0")).join(":") +
         "," + String(x).padStart(3, "0");
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body instanceof FormData ? {} : {"Content-Type": "application/json"},
    ...opts,
  });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`, detail = null;
    try {
      const body = await res.json();
      detail = body.detail ?? null;
      // detail 可能是字符串，也可能是结构化对象（如文案修订 409 冲突
      // 携带 {message, current_text, edited}，供页面展示最新文案）
      if (typeof detail === "string") msg = detail;
      else if (detail && detail.message) msg = detail.message;
    } catch (_) {}
    const err = new Error(msg);
    err.status = res.status;
    err.detail = detail;
    throw err;
  }
  return res.json();
}

// ---------- 项目列表 ----------
async function loadProjects(selectId) {
  const rows = await api("/api/projects");
  const sel = $("#projectSelect");
  sel.innerHTML = "";
  const opt0 = document.createElement("option");
  opt0.value = ""; opt0.textContent = "— 选择历史项目 —";
  sel.appendChild(opt0);
  for (const r of rows) {
    const o = document.createElement("option");
    o.value = r.id;
    o.textContent = `#${r.id} ${r.name}（${r.cue_count} 条，采纳 ${r.adopted_count}）`;
    sel.appendChild(o);
  }
}

// ---------- 渲染 ----------
const DECISION_LABEL = { intentional: "有意留白", needs_subtitle: "待补字幕" };

function renderGapReview(p) {
  const panel = $("#gapReviewPanel");
  const gr = p.gap_review;
  if (!gr || !gr.shot_count) {           // 无镜头：不生成审阅项
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  $("#gapStats").innerHTML = [
    ["留白区间", gr.total, ""],
    ["待审", gr.pending_count, gr.pending_count ? "warn" : "good"],
    ["已审", gr.total - gr.pending_count, "good"],
  ].map(([k, v, cls]) => `<span class="stat ${cls}">${k} <b>${v}</b></span>`).join("");

  const list = $("#gapList");
  list.innerHTML = "";
  const byShot = new Map();              // 相接/重叠镜头分别列出
  for (const it of gr.items) {
    const arr = byShot.get(it.shot_ord) || [];
    arr.push(it);
    byShot.set(it.shot_ord, arr);
  }
  for (const [ord, items] of byShot) {
    const box = document.createElement("div");
    box.className = "shot-gaps";
    const head = document.createElement("div");
    head.className = "shot-head";
    head.textContent = `镜头 #${ord}　${fmtMs(items[0].shot_start_ms)} → ` +
      `${fmtMs(items[0].shot_end_ms)}（${items[0].shot_start_ms} → ${items[0].shot_end_ms} ms）`;
    box.appendChild(head);
    for (const it of items) {
      const row = document.createElement("div");
      row.className = "gap-item";
      const pill = it.decision
        ? `<span class="pill ${it.decision === "intentional" ? "intentional" : "needs-sub"}">${DECISION_LABEL[it.decision]}</span>`
        : `<span class="pill proposed">待审</span>`;
      const attrs = `data-shot="${it.shot_ord}" data-start="${it.gap_start_ms}" data-end="${it.gap_end_ms}"`;
      row.innerHTML = `
        <span class="gap-time">${fmtMs(it.gap_start_ms)} → ${fmtMs(it.gap_end_ms)}<span class="muted">${it.gap_start_ms} → ${it.gap_end_ms} ms · 时长 ${it.duration_ms} ms</span></span>
        ${pill}
        <span class="row-actions">
          <button class="mini" data-gap-act="intentional" ${attrs}>有意留白</button>
          <button class="mini" data-gap-act="needs_subtitle" ${attrs}>待补字幕</button>
          ${it.decision ? `<button class="mini" data-gap-act="clear" ${attrs}>清除</button>` : ""}
        </span>`;
      box.appendChild(row);
    }
    list.appendChild(box);
  }
}

// ---------- 交付字幕核验 ----------
// 核验结论与「项目 + 生效时间轴 + 提交先后」绑定。稿件切换以**提交动作**
// （点「上传并核验」）为唯一边界：
// - 文件框里改选了乙稿但没提交，只是改了输入框的值，state.verifySnapshot
//   仍是最近一次提交的甲稿；随后本页改参数改变生效轴，或跨标签页变轴后
//   刷新详情仍需重核时，自动重核一律读取甲稿快照，绝不读取文件框当前值，
//   不会把乙稿的结论当作甲稿的。乙稿只有被提交之后才取代甲稿。
// - 清空文件框不影响快照：只要甲稿 File 仍可由本页读取，自动重核继续用甲稿；
//   稿件已不可读（如浏览器回收了句柄）时作废旧结论并提示重新上传。
// - 服务端在项目详情与核验响应中返回同一 timeline_hash（生效轴指纹）；
//   参数重算 / 采纳 / 撤销改变了生效轴后，旧结论立即作废、不得继续显示，
//   用已提交稿件快照按新生效轴自动重新核验。生效轴未变（如仅调 CPS 上限、
//   相同参数重提）或修改失败时，结论保留。
// - 响应指纹与本页详情不符时，本页详情可能已过期（另一标签页改轴）：
//   先拉取该项目最新详情再裁决——响应指纹与最新详情一致，说明已返回的
//   结论按新详情仍然有效，直接接纳并同步页面；仍不一致（等待期间生效轴
//   再次变化）才用已提交稿件快照按新轴重核。自动重核链有终止条件
//   （VERIFY_MAX_AUTO_ATTEMPTS）：详情刷新失败、快照不可用或达到上限
//   时立即停止并提示重新上传——不显示旧结论，也不无界发请求。
// - 每次提交取递增序号 verifySeq；响应或详情到达时若已有更新的提交、或
//   已切换项目（project_id 不符），迟到结果直接丢弃——只呈现当前项目
//   最近一次有效提交的结果。
const VERIFY_MAX_AUTO_ATTEMPTS = 3;  // 一条自动重核链最多发起的核验请求数

function resetDeliveryVerify() {
  state.deliveryVerify = null;
  state.verifyNotice = "";
  state.verifySnapshot = null;       // 切换 / 新建 / 删除项目：快照不跨项目
  state.verifyAutoInFlight = false;
  state.verifySeq++;                 // 使在途的旧响应全部失效
  const f = $("#deliveryVerifyForm");
  if (f) f.reset();
}

// 自动重核唯一可用的稿件来源：最近一次**已提交**且属于当前项目、且仍可
// 读取的稿件快照。文件框当前 files[0]（可能是改选后未提交的乙稿）永不参与。
function submittedVerifyFile() {
  const p = state.project;
  const snap = state.verifySnapshot;
  if (!p || !snap || snap.projectId !== p.id) return null;
  return snap.file || null;
}

function syncDeliveryVerify() {
  const p = state.project;
  const v = state.deliveryVerify;
  if (!v) return;
  if (!p || v.project_id !== p.id) {
    // 项目已切换 / 删除（正常路径已由 resetDeliveryVerify 清理，此为防御）
    state.deliveryVerify = null;
    state.verifyNotice = "";
    return;
  }
  if (v.timeline_hash === p.timeline_hash) return;   // 生效轴未变：保留
  // 生效轴已变化：旧结论立即作废，不再显示。自动重核只认最近一次已提交
  // 的稿件快照——文件框里改选未提交的乙稿不参与。
  state.deliveryVerify = null;
  const file = submittedVerifyFile();
  if (file) {
    state.verifyNotice = "生效时间轴已变化，正在用最近一次已提交的稿件重新核验…";
    scheduleAutoDeliveryVerify(file);
  } else {
    state.verifyNotice =
      "生效时间轴已变化，旧核验结论已作废，请重新上传回传稿。";
  }
}

// 触发一次自动重核（来自本页变轴或跨标签页裁决）。同一生效轴上已有自动
// 重核在途时不重复发起；手动提交不受此限制（提交是稿件切换边界，序号更大）。
function scheduleAutoDeliveryVerify(file) {
  const p = state.project;
  if (!p || !file || state.verifyAutoInFlight) return;
  state.verifyAutoInFlight = true;
  runDeliveryVerify(file, true).finally(() => { state.verifyAutoInFlight = false; });
}

function acceptDeliveryVerify(result) {
  state.verifyNotice = "";
  state.deliveryVerify = result;
  renderDeliveryVerify();
  const s = result.summary;
  toast(`核验完成：通过 ${s.pass} · 时间不符 ${s.time_mismatch} · ` +
        `缺失 ${s.missing} · 新增 ${s.new} · 待确认 ${s.pending}`);
}

async function runDeliveryVerify(file, auto, attempt = 0) {
  const p = state.project;
  if (!p || !file) return;
  const seq = ++state.verifySeq;
  const fd = new FormData();
  fd.append("delivery", file);
  try {
    const result = await api(`/api/projects/${p.id}/verify-delivery`,
                             { method: "POST", body: fd });
    // 绑定校验：只呈现当前项目最近一次有效提交的结果
    if (seq !== state.verifySeq) return;                     // 已有更新的提交
    if (!state.project || state.project.id !== p.id) return; // 已切换项目
    if (result.project_id !== p.id) return;                  // 防御
    if (result.timeline_hash === state.project.timeline_hash) {
      acceptDeliveryVerify(result);        // 指纹相符：结论对当前生效轴有效
      return;
    }
    // 指纹不符：本页详情可能已过期（另一标签页改轴），先取最新详情再裁决
    await reconcileDeliveryVerify(result, seq, attempt);
  } catch (e) {
    if (seq !== state.verifySeq) return;
    if (!state.project || state.project.id !== p.id) return;
    if (auto) {
      // 自动重核失败（含稿件快照已不可读、有界重核失败、网络错误）：
      // 作废旧结论，要求人工重传，绝不回退显示旧结论或文件框里的改选稿
      state.deliveryVerify = null;
      state.verifyNotice = `自动重新核验失败：${e.message}，旧核验结论已作废，请重新上传回传稿。`;
      renderDeliveryVerify();
    }
    toast(e.message, true);
  }
}

// 核验响应指纹与本页详情不符：本页详情可能已过期（另一标签页改参数/采纳），
// 先拉取该项目最新详情再裁决——
// - 响应指纹与最新详情一致：已返回的结论按新详情仍然有效，接纳并同步页面；
// - 仍不一致：生效轴在等待期间再次变化，用最近一次已提交的稿件快照按新轴重核。
// 终止条件：自动重核链达到 VERIFY_MAX_AUTO_ATTEMPTS 次、详情刷新失败或
// 已提交快照不可用时立即停止，作废旧结论并提示重新上传，不无界发请求。
async function reconcileDeliveryVerify(result, seq, attempt) {
  const pid = result.project_id;
  let fresh;
  try {
    fresh = await api(`/api/projects/${pid}`);
  } catch (e) {
    if (seq !== state.verifySeq) return;
    if (!state.project || state.project.id !== pid) return;
    // 详情刷新失败：无法裁决结论是否还有效，作废旧结论并停止
    state.deliveryVerify = null;
    state.verifyNotice =
      `项目详情刷新失败：${e.message}，核验结论已作废，请重新上传回传稿。`;
    renderDeliveryVerify();
    toast(e.message, true);
    return;
  }
  if (seq !== state.verifySeq) return;                     // 已有更新的提交
  if (!state.project || state.project.id !== pid) return;  // 已切换项目
  state.project = fresh;          // 页面详情同步到最新（另一标签页的改动）
  if (result.timeline_hash === fresh.timeline_hash) {
    acceptDeliveryVerify(result);   // 结论按新详情可接纳：无需重核
    render();
    loadProjects();
    return;
  }
  // 生效轴在等待期间又变化：已返回的结论不可接纳，需按最新生效轴重核。
  // 重核稿件只取最近一次已提交快照（文件框改选未提交的乙稿不参与）。
  state.deliveryVerify = null;
  const file = submittedVerifyFile();
  if (!file) {
    state.verifyNotice = "生效时间轴已变化，旧核验结论已作废，请重新上传回传稿。";
    render();
    return;
  }
  if (attempt + 1 >= VERIFY_MAX_AUTO_ATTEMPTS) {
    state.verifyNotice =
      "生效时间轴多次变化，自动重新核验已停止，请重新上传回传稿。";
    render();
    return;
  }
  state.verifyNotice = "生效时间轴已变化，正在用最近一次已提交的稿件重新核验…";
  render();
  await runDeliveryVerify(file, true, attempt + 1);
}

function renderDeliveryVerify() {
  const v = state.deliveryVerify;
  const tb = $("#verifyTable tbody");
  const stats = $("#verifyStats");
  tb.innerHTML = "";
  if (!v) {
    stats.innerHTML = "";
    $("#deliveryName").textContent = state.verifyNotice || "";
    return;
  }
  $("#deliveryName").textContent =
    `已核验：${v.delivery_name || (state.verifySnapshot && state.verifySnapshot.name) || "交付稿"}` +
    `（生效轴 ${v.reference_count} 条 / 交付 ${v.delivered_count} 条 / 同文案配对 ${v.matched_text_count} 对）`;
  const s = v.summary;
  stats.innerHTML = [
    ["通过", s.pass, "good"],
    ["时间不符", s.time_mismatch, s.time_mismatch ? "bad" : "good"],
    ["缺失", s.missing, s.missing ? "bad" : "good"],
    ["新增", s.new, s.new ? "warn" : ""],
    ["待确认", s.pending, s.pending ? "warn" : "good"],
  ].map(([k, n, cls]) => `<span class="stat ${cls}">${k} <b>${n}</b></span>`).join("");

  const cell = (ord, start, end, cls = "") => ord == null
    ? `<td class="muted">—</td>`
    : `<td class="${cls}">#${ord}<span class="muted">${start} → ${end} ms</span></td>`;

  for (const r of v.rows) {
    const tr = document.createElement("tr");
    tr.className = `verify-row verdict-${r.verdict}`;
    const pill = `<span class="pill vf-${r.verdict}">${VERIFY_LABEL[r.verdict]}</span>`;
    let body;
    if (r.kind === "match") {
      const dCls = r.verdict === "time_mismatch" || r.verdict === "pending"
        ? "vf-time" : "";
      const dTxt = (n) => (n > 0 ? "+" : "") + n;
      const deltaCls = r.delta_start_ms === 0 && r.delta_end_ms === 0
        ? "delta-zero" : "vf-time";
      const textFlag = r.text_same ? ""
        : `<div class="hash warn-text">⚠ 等优对应中文案不同，请人工核对</div>`;
      body = `
        <td>${pill}</td>
        ${cell(r.ref_ord, r.ref_start_ms, r.ref_end_ms)}
        ${cell(r.del_ord, r.del_start_ms, r.del_end_ms, dCls)}
        <td class="${deltaCls}">${dTxt(r.delta_start_ms)} / ${dTxt(r.delta_end_ms)}</td>
        <td class="text-cell">${esc(r.text)}${textFlag}</td>`;
    } else if (r.kind === "ref_only") {
      const cand = r.candidate
        ? `<div class="hash">等优候选：交付 #${cand.del_ord}
             ${cand.del_start_ms} → ${cand.del_end_ms} ms「${esc(cand.delivered_text)}」</div>`
        : "";
      body = `
        <td>${pill}</td>
        ${cell(r.ref_ord, r.ref_start_ms, r.ref_end_ms)}
        <td class="muted">—</td>
        <td class="muted">—</td>
        <td class="text-cell">${esc(r.text)}${cand}</td>`;
    } else {
      const cand = r.candidate
        ? `<div class="hash">等优候选：生效轴 #${cand.ref_ord}
             ${cand.ref_start_ms} → ${cand.ref_end_ms} ms「${esc(cand.text)}」</div>`
        : "";
      body = `
        <td>${pill}</td>
        <td class="muted">—</td>
        ${cell(r.del_ord, r.del_start_ms, r.del_end_ms)}
        <td class="muted">—</td>
        <td class="text-cell">${esc(r.delivered_text)}${cand}</td>`;
    }
    tr.innerHTML = body;
    tb.appendChild(tr);
  }
}

// ---------- 逐条文案修订 ----------
// 每行「修订」打开表格外侧的独立编辑面板（render 全量重建表格时不受影响，
// 草稿不丢）。保存 / 恢复都携带打开编辑时的当前文案作为 base_text：服务端
// 在写锁内比对，若另一标签页已先改过则 409 拒绝覆盖并回传最新文案——页面
// 把最新文案载入编辑框并提示，绝不静默覆盖。恢复导入原文即清除修订稿。
const _graphemeSegmenter = (typeof Intl !== "undefined" && Intl.Segmenter)
  ? new Intl.Segmenter(undefined, { granularity: "grapheme" }) : null;

function normalizeDraftText(s) {   // 与服务端同一套归一化：去标签、折叠空白
  return String(s).replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim();
}

function countDraftGraphemes(s) {  // UAX #29 字素簇计数（预览，以服务端为准）
  const t = normalizeDraftText(s);
  if (!_graphemeSegmenter) return null;
  return [..._graphemeSegmenter.segment(t)].length;
}

function editingCue() {
  const ed = state.textEditing;
  if (!ed || !state.project) return null;
  return state.project.analysis.cues.find(c => c.uid === ed.uid) || null;
}

function closeTextEdit() {
  state.textEditing = null;
  $("#textEditPanel").classList.add("hidden");
  $("#textEditHint").textContent = "";
}

function openTextEdit(uid) {
  const c = state.project && state.project.analysis.cues.find(x => x.uid === uid);
  if (!c) return;
  state.textEditing = { uid, baseText: c.text };
  $("#textEditInput").value = c.text;
  $("#textEditHint").textContent = "";
  $("#textEditPanel").classList.remove("hidden");
  renderTextEditInfo();
  updateTextEditPreview();
  $("#textEditInput").focus();
}

// 面板静态信息（cue_id / 原时间 / 导入原文 / 库中当前文案 / 字素 / CPS）：
// 打开时与每次 render 时按最新详情刷新；不触碰编辑框内容。
function renderTextEditInfo() {
  const c = editingCue();
  if (!c) { closeTextEdit(); return; }
  const editedNote = c.edited
    ? `<span class="pill edited">已修订</span>`
    : `<span class="pill proposed">未修订</span>`;
  $("#textEditInfo").innerHTML = `
    <div><b>cue ${esc(c.cue_id)}</b>（#${c.ord}，原时间 ${c.start_ms} → ${c.end_ms} ms，
      时长 ${c.duration_ms} ms）${editedNote}</div>
    <div class="hash">导入原文：${esc(c.imported_text)}
      （sha256:${c.raw_hash.slice(0, 16)}…）</div>
    <div class="hash">库中当前文案：${esc(c.text)}　字素 ${c.graphemes} ·
      CPS ${c.cps === Infinity ? "∞" : c.cps}</div>`;
  $("#textEditRestore").style.display = c.edited ? "" : "none";
}

function updateTextEditPreview() {
  const c = editingCue();
  const el = $("#textEditPreview");
  if (!c) { el.textContent = ""; return; }
  const draft = $("#textEditInput").value;
  const norm = normalizeDraftText(draft);
  if (!norm) {
    el.textContent = "归一化后为空：去标签、折叠空白后需至少保留一个可见字符，否则无法保存。";
    return;
  }
  const n = countDraftGraphemes(draft);
  if (n == null) { el.textContent = `归一化预览：${norm}`; return; }
  const cps = c.duration_ms > 0 ? (n * 1000 / c.duration_ms) : Infinity;
  const cpsTxt = cps === Infinity ? "∞" : (Math.round(cps * 100) / 100).toString();
  const over = state.project && cps > state.project.cps_limit ? "（超 CPS 上限）" : "";
  el.textContent = `草稿字素 ${n} · 预计 CPS ${cpsTxt}${over}（以服务端保存结果为准）`;
}

// 409：另一标签页已先改过该条。拒绝覆盖——把服务端回传的最新文案载入
// 编辑框、更新编辑基准，并刷新项目详情；用户基于最新文案重新编辑。
function handleTextEditConflict(e) {
  const d = e.detail;
  if (e.status !== 409 || !d || typeof d !== "object") return false;
  const ed = state.textEditing;
  if (ed) {
    ed.baseText = d.current_text;
    $("#textEditInput").value = d.current_text;
    $("#textEditHint").textContent =
      "该条文案已在别处被修改，编辑框已载入最新文案，请在此基础上重新编辑后再保存。";
    updateTextEditPreview();
  }
  toast("该条文案已在别处被修改，已载入最新文案", true);
  if (state.project) refreshAfterConflict(state.project.id);
  return true;
}

async function saveTextEdit() {
  const ed = state.textEditing;
  if (!ed || !state.project) return;
  const pid = state.project.id;
  try {
    state.project = await api(`/api/projects/${pid}/cues/${ed.uid}/text`, {
      method: "PUT",
      body: JSON.stringify({ text: $("#textEditInput").value,
                             base_text: ed.baseText }),
    });
    toast("文案修订已保存");
    closeTextEdit();
    render();
    loadProjects();
  } catch (e) {
    if (handleTextEditConflict(e)) return;
    toast(e.message, true);   // 空白稿 / cue 不存在等：不落库，仅提示
  }
}

async function restoreTextEdit() {
  const ed = state.textEditing;
  if (!ed || !state.project) return;
  const pid = state.project.id;
  try {
    state.project = await api(
      `/api/projects/${pid}/cues/${ed.uid}/text/restore`, {
        method: "POST",
        body: JSON.stringify({ base_text: ed.baseText }),
      });
    toast("已恢复导入原文");
    closeTextEdit();
    render();
    loadProjects();
  } catch (e) {
    if (handleTextEditConflict(e)) return;
    toast(e.message, true);
  }
}

function render() {
  syncDeliveryVerify();   // 先按最新生效轴同步核验状态（作废/自动重核）
  const p = state.project;
  if (!p) {
    closeTextEdit();
    $("#workspace").classList.add("hidden");
    $("#importPanel").classList.remove("hidden");
    renderDeliveryVerify();
    return;
  }
  $("#importPanel").classList.add("hidden");
  $("#workspace").classList.remove("hidden");
  $("#projectTitle").textContent = `#${p.id} ${p.name}`;
  const sf = $("#settingsForm");
  sf.cps_limit.value = p.cps_limit;
  sf.min_gap_ms.value = p.min_gap_ms;
  sf.tolerance_ms.value = p.tolerance_ms;

  const st = p.analysis.stats;
  $("#stats").innerHTML = [
    ["条目数", st.cue_count, ""],
    ["镜头数", st.shot_count, ""],
    ["间隔/重叠问题", st.gap_violations, st.gap_violations ? "bad" : "good"],
    ["CPS 超限", st.cps_violations, st.cps_violations ? "bad" : "good"],
    ["跨镜头条目", st.cross_shot_count, st.cross_shot_count ? "bad" : "good"],
    ["可提案条目", st.proposal_count, "good"],
    ["需移动条目", st.proposal_moved_count, "warn"],
    ["冲突条目", st.conflict_cue_count, st.conflict_cue_count ? "bad" : "good"],
    ["总移动量", st.total_shift_ms + " ms", ""],
  ].map(([k, v, cls]) => `<span class="stat ${cls}">${k} <b>${v}</b></span>`).join("");

  const conf = $("#conflicts");
  conf.innerHTML = "";
  for (const ch of p.analysis.chains) {
    const div = document.createElement("div");
    div.className = "chain";
    div.innerHTML = `<span class="tag">冲突链 ${ch.cue_ids.join(" → ")}</span>${esc(ch.reason)}`;
    conf.appendChild(div);
  }
  for (const it of p.analysis.intrinsic) {
    const div = document.createElement("div");
    div.className = "chain intrinsic";
    div.innerHTML = `<span class="tag">内在冲突 · ${esc(it.cue_id)}</span>${esc(it.reason)}`;
    conf.appendChild(div);
  }

  const tb = $("#cueTable tbody");
  tb.innerHTML = "";
  for (const c of p.analysis.cues) {
    const tr = document.createElement("tr");
    const flags = [];
    if (c.overlap_with) flags.push(`<span class="flag gap" title="${esc(c.overlap_with)}">间隔</span>`);
    if (!c.cps_ok) flags.push(`<span class="flag" title="CPS ${c.cps} 超限">CPS ${c.cps}</span>`);
    if (c.cross_shot) flags.push(`<span class="flag cross">跨镜头</span>`);

    const propTxt = c.proposal_start_ms == null ? "—（无解）"
      : `${c.proposal_start_ms} → ${c.proposal_end_ms}`;
    let deltaCls = "delta-zero", dTxt = "—";
    if (c.proposal_start_ms != null) {
      const d = c.proposal_start_ms - c.start_ms;
      dTxt = (d > 0 ? "+" : "") + d + " ms";
      deltaCls = d === 0 ? "delta-zero" : "delta-pos";
    }
    let status;
    if (c.adopted) {
      status = `<span class="pill adopted">已采纳</span>`;
    } else if (c.proposal_start_ms != null) {
      status = `<span class="pill proposed">待采纳</span>`;
    } else {
      status = `<span class="pill conflict">冲突</span>`;
    }
    const canAdopt = c.proposal_start_ms != null;
    const actionBtns = `
      <div class="row-actions">
        ${canAdopt ? `<button class="mini primary" data-act="adopt" data-uid="${c.uid}">采纳</button>` : ""}
        ${c.adopted ? `<button class="mini" data-act="revert" data-uid="${c.uid}">撤销</button>` : ""}
        <button class="mini" data-act="edit" data-uid="${c.uid}">修订</button>
      </div>`;
    const startShown = c.adopted_start_ms ?? c.start_ms;
    const endShown = c.adopted_end_ms ?? c.end_ms;
    // 文案列：当前文案（已修订取修订稿）；已修订时附徽标与导入原文对照，
    // 原文哈希始终指向导入内容。
    const textCell = c.edited
      ? `${esc(c.text)} <span class="pill edited">已修订</span>
         <div class="hash">导入原文：${esc(c.imported_text)}</div>
         <div class="hash">sha256:${c.raw_hash.slice(0, 16)}…（导入原文）</div>`
      : `${esc(c.text)}<div class="hash">sha256:${c.raw_hash.slice(0, 16)}…</div>`;
    tr.className = c.proposal_start_ms == null ? "row-bad" : "";
    tr.innerHTML = `
      <td>${c.ord}</td>
      <td>${esc(c.cue_id)}</td>
      <td class="text-cell">${textCell}</td>
      <td>${startShown} → ${endShown}</td>
      <td>${c.duration_ms}</td>
      <td>${c.graphemes}</td>
      <td class="${c.cps_ok ? "cps-ok" : "cps-bad"}">${c.cps === Infinity ? "∞" : c.cps}</td>
      <td>${flags.join("") || "—"}</td>
      <td>${propTxt}</td>
      <td class="${deltaCls}">${dTxt}</td>
      <td>${status}${actionBtns}</td>`;
    tb.appendChild(tr);
  }

  const base = `/api/projects/${p.id}/export`;
  $("#exportJson").href = base + "?fmt=json";
  $("#exportZip").href = base + "?fmt=zip";
  $("#exportSrt").href = base + "?fmt=srt";
  $("#projectSelect").value = String(p.id);
  renderGapReview(p);
  renderDeliveryVerify();
  // 编辑面板打开期间，参数重算 / 采纳 / 别处修订等引起的刷新只更新面板
  // 静态信息（库中当前文案、字素、CPS），不覆盖编辑框里的草稿。
  if (state.textEditing) renderTextEditInfo();
}

// 采纳可能因另一标签页并发改参数而被拒（旧提案失效）：重新拉取详情，
// 让表格与统计条回到服务端的最新一致状态。
async function refreshAfterConflict(pid) {
  try {
    state.project = await api(`/api/projects/${pid}`);
    render();
    loadProjects();
  } catch (_) {}
}

// ---------- 事件 ----------
$("#importForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const fd = new FormData();
  fd.append("name", f.name.value.trim() || "未命名项目");
  fd.append("cps_limit", f.cps_limit.value);
  fd.append("min_gap_ms", f.min_gap_ms.value);
  fd.append("tolerance_ms", f.tolerance_ms.value);
  fd.append("subtitle", f.subtitle.files[0]);
  if (f.shots_file.files[0]) fd.append("shots_file", f.shots_file.files[0]);
  try {
    state.project = await api("/api/projects", { method: "POST", body: fd });
    resetDeliveryVerify();
    closeTextEdit();
    toast("导入并分析完成");
    await loadProjects();
    render();
  } catch (e) { toast(e.message, true); }
});

$("#settingsForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (!state.project) return;
  const f = ev.target;
  try {
    state.project = await api(`/api/projects/${state.project.id}/settings`, {
      method: "PUT",
      body: JSON.stringify({
        cps_limit: parseFloat(f.cps_limit.value),
        min_gap_ms: parseInt(f.min_gap_ms.value, 10),
        tolerance_ms: parseInt(f.tolerance_ms.value, 10),
      }),
    });
    toast("已按新参数重新求解");
    render();
    loadProjects();
  } catch (e) { toast(e.message, true); }
});

$("#cueTable").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn || !state.project) return;
  const uid = parseInt(btn.dataset.uid, 10);
  const c = state.project.analysis.cues.find(x => x.uid === uid);
  const pid = state.project.id;
  if (btn.dataset.act === "edit") {   // 打开文案修订面板（非请求，无需 try）
    openTextEdit(uid);
    return;
  }
  try {
    if (btn.dataset.act === "adopt") {
      state.project = await api(`/api/projects/${pid}/adopt`, {
        method: "POST",
        body: JSON.stringify({
          cue_uid: uid,
          new_start_ms: c.proposal_start_ms,
          new_end_ms: c.proposal_end_ms,
        }),
      });
      toast(`已采纳 cue ${c.cue_id}`);
    } else {
      state.project = await api(`/api/projects/${pid}/adopt/${uid}`,
                                { method: "DELETE" });
      toast(`已撤销 cue ${c.cue_id}`);
    }
    render();
    loadProjects();
  } catch (e) {
    toast(e.message + "（参数可能已在别处更新，已刷新）", true);
    refreshAfterConflict(pid);
  }
});

$("#adoptAllBtn").addEventListener("click", async () => {
  if (!state.project) return;
  const todo = state.project.analysis.cues.filter(
    c => c.proposal_start_ms != null && !c.adopted);
  if (!todo.length) return toast("没有可采纳的提案");
  try {
    let p = state.project;
    for (const c of todo) {
      p = await api(`/api/projects/${p.id}/adopt`, {
        method: "POST",
        body: JSON.stringify({
          cue_uid: c.uid,
          new_start_ms: c.proposal_start_ms,
          new_end_ms: c.proposal_end_ms,
        }),
      });
    }
    state.project = p;
    toast(`已批量采纳 ${todo.length} 条`);
    render();
    loadProjects();
  } catch (e) {
    toast(e.message + "（参数可能已在别处更新，已刷新）", true);
    refreshAfterConflict(state.project.id);
  }
});

// 镜头留白审阅：标记 / 清除。过期页面（参数或采纳已在别处变化）提交的
// 结论会被服务端 400 拒绝，此时刷新到最新状态，避免误标新留白。
$("#gapList").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-gap-act]");
  if (!btn || !state.project) return;
  const pid = state.project.id;
  const shotOrd = parseInt(btn.dataset.shot, 10);
  const gapStart = parseInt(btn.dataset.start, 10);
  const gapEnd = parseInt(btn.dataset.end, 10);
  const act = btn.dataset.gapAct;
  try {
    if (act === "clear") {
      const q = `shot_ord=${shotOrd}&gap_start_ms=${gapStart}&gap_end_ms=${gapEnd}`;
      state.project = await api(`/api/projects/${pid}/gaps/review?${q}`,
                                { method: "DELETE" });
      toast("已清除审阅标记，回到待审");
    } else {
      state.project = await api(`/api/projects/${pid}/gaps/review`, {
        method: "POST",
        body: JSON.stringify({
          shot_ord: shotOrd, gap_start_ms: gapStart,
          gap_end_ms: gapEnd, decision: act,
        }),
      });
      toast(`已标记为「${DECISION_LABEL[act]}」`);
    }
    render();
  } catch (e) {
    toast(e.message + "（留白已随参数/采纳变化，已刷新）", true);
    refreshAfterConflict(pid);
  }
});

// 文案修订面板：保存 / 恢复导入原文 / 取消；输入时实时给出归一化后的
// 字素数与预计 CPS（预览，最终以服务端保存结果为准）。
$("#textEditSave").addEventListener("click", saveTextEdit);
$("#textEditRestore").addEventListener("click", restoreTextEdit);
$("#textEditCancel").addEventListener("click", closeTextEdit);
$("#textEditInput").addEventListener("input", updateTextEditPreview);

// 交付字幕核验：提交回传稿，服务端保序最优对应后返回逐行结论。
// 提交动作是稿件切换的唯一边界：无论文件框之前改选过什么，只有点下
// 「上传并核验」才把当前文件记为已提交稿件快照，此后自动重核 / 跨标签页
// 裁决都以该快照为准（再次提交乙稿后乙稿才取代甲稿）。只读核验，失败
// （空稿/解析失败）仅弹错，不影响当前项目数据。
$("#deliveryVerifyForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (!state.project) return;
  const file = ev.target.delivery.files[0];
  if (!file) return;
  // 提交即落快照：后续清空文件框或改选未提交都不会改变自动重核所用稿件
  state.verifySnapshot = {
    file,
    name: file.name || "交付稿",
    projectId: state.project.id,
  };
  state.verifyAutoInFlight = false;
  state.verifyNotice = "";
  await runDeliveryVerify(file, false);
});

$("#deleteProjectBtn").addEventListener("click", async () => {  if (!state.project) return;
  if (!confirm(`确定删除项目「${state.project.name}」？该操作不可恢复。`)) return;
  try {
    await api(`/api/projects/${state.project.id}`, { method: "DELETE" });
    state.project = null;
    resetDeliveryVerify();
    closeTextEdit();
    toast("项目已删除");
    await loadProjects();
    render();
  } catch (e) { toast(e.message, true); }
});

$("#projectSelect").addEventListener("change", async (ev) => {
  const id = ev.target.value;
  if (!id) return;
  resetDeliveryVerify();   // 先作废旧项目核验并使在途响应失效，再切换
  closeTextEdit();         // 编辑面板不跨项目保留
  try {
    state.project = await api(`/api/projects/${id}`);
    render();
  } catch (e) { toast(e.message, true); }
});

$("#newProjectBtn").addEventListener("click", () => {
  state.project = null;
  resetDeliveryVerify();
  closeTextEdit();
  $("#projectSelect").value = "";
  render();
});

// 初始化
loadProjects().catch(() => {});
