"""FastAPI 入口：项目导入、分析、采纳、逐条文案修订、镜头留白审阅、
交付字幕核验与导出。"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import database as db
from . import verification as vfy
from .analysis import build_analysis, revalidate_adoptions
from .gaps import DECISIONS, compute_shot_gaps
from .shots import Shot, parse_shots
from .subtitles import ParsedCue, count_graphemes, parse_subtitle, visible_text

app = FastAPI(title="字幕节奏与镜头边界校正台", version="1.0")

# 交付字幕核验：保序对应 O(n·m)，两侧条目数上限
VERIFY_MAX_CUES = 3000


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# ---------- 数据模型 ----------

class SettingsIn(BaseModel):
    cps_limit: float = 21.0
    min_gap_ms: int = 40
    tolerance_ms: int = 200


class AdoptIn(BaseModel):
    cue_uid: int
    new_start_ms: int
    new_end_ms: int


class GapReviewIn(BaseModel):
    shot_ord: int
    gap_start_ms: int
    gap_end_ms: int
    decision: str


class TextEditIn(BaseModel):
    """逐条文案修订。base_text 为页面打开编辑时看到的当前文案：服务端在
    写锁内比对，不一致即另一标签页已先改过 → 409 拒绝覆盖并回传最新文案。"""
    text: str
    base_text: str


class TextRestoreIn(BaseModel):
    base_text: str


# ---------- 序列化 ----------

def _cue_row(row) -> dict:
    edited = row["edited_text"] is not None
    return {
        "uid": row["id"],
        "cue_id": row["cue_id"],
        "ord": row["ord"],
        "start_ms": row["start_ms"],
        "end_ms": row["end_ms"],
        "text": db.current_text(row),
        "imported_text": row["text"],
        "edited": edited,
        "graphemes": db.current_graphemes(row),
        "raw_hash": row["raw_hash"],
        "adopted_start_ms": row["new_start_ms"],
        "adopted_end_ms": row["new_end_ms"],
        "adopted": row["new_start_ms"] is not None,
    }


def _parsed_from_rows(cue_db_rows) -> list[ParsedCue]:
    """由库中字幕行构造分析输入：文案与字素数取当前值（已修订取修订稿），
    原文哈希始终指向导入内容。"""
    return [
        ParsedCue(
            cue_id=r["cue_id"], ord=r["ord"],
            start_ms=r["start_ms"], end_ms=r["end_ms"],
            raw_text=db.current_text(r), graphemes=db.current_graphemes(r),
            raw_hash=r["raw_hash"],
        ) for r in cue_db_rows
    ]


def _load_inputs(conn, pid: int) -> tuple:
    """读取库中项目、字幕与镜头，构造 build_analysis 的输入。"""
    prow = db.get_project(conn, pid)
    if prow is None:
        raise HTTPException(404, "项目不存在")
    cue_db_rows = list(db.get_cues(conn, pid))
    shot_rows = list(db.get_shots(conn, pid))
    parsed = _parsed_from_rows(cue_db_rows)
    shots = [Shot(ord=s["ord"], start_ms=s["start_ms"], end_ms=s["end_ms"])
             for s in shot_rows]
    return prow, cue_db_rows, shots, parsed


def _effective_report_times(rep) -> tuple[int, int]:
    """分析报表中一条 cue 的生效时间：与导出 SRT 的有效时间轴一致——
    已采纳取采纳值，未采纳先取提案，无提案再取原值。"""
    if rep.adopted_start_ms is not None:
        return rep.adopted_start_ms, rep.adopted_end_ms
    if rep.proposal_start_ms is not None:
        return rep.proposal_start_ms, rep.proposal_end_ms
    return rep.start_ms, rep.end_ms


def _timeline_hash(analysis) -> str:
    """生效时间轴指纹：覆盖核验输入 refs 的全部字段（ord / cue_id / 当前
    文案 / 生效起止毫秒）。参数重算、采纳、撤销、文案修订 / 恢复只要改变了
    生效轴（含当前文案），哈希必变；不改变生效轴的操作（仅调 CPS 上限、
    相同参数重提、修改失败回滚）哈希保持不变。项目详情与 verify-delivery
    响应携带同一指纹，前端据此判定已展示的核验结论是否过期。"""
    h = hashlib.sha256()
    for rep in analysis.cues:
        s, e = _effective_report_times(rep)
        h.update(f"{rep.ord}\x1f{rep.cue_id}\x1f{rep.text}\x1f{s}\x1f{e}\x1e"
                 .encode("utf-8"))
    return h.hexdigest()


def _gap_review_payload(conn, pid: int, shots: list[Shot], analysis) -> dict:
    """镜头留白审阅：按与 SRT 导出相同的生效时间轴逐镜头计算未被字幕
    覆盖的半开区间，并叠加库中已保存的审阅结论。结论按
    (镜头 ord + 镜头边界 + 留白起止) 精确匹配：边界未变的结论保留；
    采纳/撤销/参数重算导致边界变化的区间匹配不到旧结论 → 待审。
    无镜头时不生成任何审阅项。"""
    effective = [_effective_report_times(rep) for rep in analysis.cues]
    items = compute_shot_gaps(shots, effective)
    saved = {}
    for r in db.get_gap_reviews(conn, pid):
        saved[(r["shot_ord"], r["shot_start_ms"], r["shot_end_ms"],
               r["gap_start_ms"], r["gap_end_ms"])] = r["decision"]
    payload_items = []
    for g in items:
        decision = saved.get((g.shot_ord, g.shot_start_ms, g.shot_end_ms,
                              g.gap_start_ms, g.gap_end_ms))
        payload_items.append({
            "shot_ord": g.shot_ord,
            "shot_start_ms": g.shot_start_ms,
            "shot_end_ms": g.shot_end_ms,
            "gap_start_ms": g.gap_start_ms,
            "gap_end_ms": g.gap_end_ms,
            "duration_ms": g.gap_end_ms - g.gap_start_ms,
            "decision": decision,
        })
    return {
        "shot_count": len(shots),
        "total": len(payload_items),
        "pending_count": sum(1 for it in payload_items
                             if it["decision"] is None),
        "items": payload_items,
    }


def _prune_gap_reviews(conn, pid: int, payload: dict) -> None:
    """写路径（参数重算 / 采纳 / 撤销）在最终 _recompute 之后调用：删除
    不再匹配任何当前留白项的过期结论，使边界变化的区间回到待审。"""
    valid = {(it["shot_ord"], it["gap_start_ms"], it["gap_end_ms"])
             for it in payload["gap_review"]["items"]}
    db.delete_stale_gap_reviews(conn, pid, valid)


def _recompute_from(conn, pid: int, prow, cue_db_rows, shots, parsed) -> dict:
    analysis = build_analysis(
        parsed, shots, prow["cps_limit"],
        prow["min_gap_ms"], prow["tolerance_ms"],
    )
    raw_by_cid = {r["cue_id"]: r for r in cue_db_rows}
    for rep in analysis.cues:
        dbr = raw_by_cid[rep.cue_id]
        rep.uid = dbr["id"]
        rep.adopted = dbr["new_start_ms"] is not None
        rep.adopted_start_ms = dbr["new_start_ms"]
        rep.adopted_end_ms = dbr["new_end_ms"]
        rep.edited = dbr["edited_text"] is not None
        rep.imported_text = dbr["text"]
    return {
        "id": prow["id"],
        "name": prow["name"],
        "cps_limit": prow["cps_limit"],
        "min_gap_ms": prow["min_gap_ms"],
        "tolerance_ms": prow["tolerance_ms"],
        "created_at": prow["created_at"],
        "timeline_hash": _timeline_hash(analysis),
        "shots": [{"ord": s.ord, "start_ms": s.start_ms,
                   "end_ms": s.end_ms} for s in shots],
        "cues": [_cue_row(r) for r in cue_db_rows],
        "analysis": {
            "cues": [vars(c) for c in analysis.cues],
            "chains": analysis.chains,
            "intrinsic": analysis.intrinsic,
            "stats": analysis.stats,
        },
        "gap_review": _gap_review_payload(conn, pid, shots, analysis),
    }


def _recompute(conn, pid: int) -> dict:
    prow, cue_db_rows, shots, parsed = _load_inputs(conn, pid)
    return _recompute_from(conn, pid, prow, cue_db_rows, shots, parsed)


# ---------- 路由 ----------

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/projects")
def list_projects():
    with db.get_conn() as conn:
        rows = db.list_projects(conn)
        return [{"id": r["id"], "name": r["name"],
                 "created_at": r["created_at"],
                 "cps_limit": r["cps_limit"],
                 "min_gap_ms": r["min_gap_ms"],
                 "tolerance_ms": r["tolerance_ms"],
                 "cue_count": r["cue_count"],
                 "shot_count": r["shot_count"],
                 "adopted_count": r["adopted_count"]} for r in rows]


@app.post("/api/projects")
async def create_project(
    name: str = Form("未命名项目"),
    cps_limit: float = Form(21.0),
    min_gap_ms: int = Form(40),
    tolerance_ms: int = Form(200),
    subtitle: UploadFile | None = File(None),
    shots_file: UploadFile | None = File(None),
):
    if subtitle is None:
        raise HTTPException(400, "必须提供字幕文件（.srt/.vtt）")
    if cps_limit <= 0 or min_gap_ms < 0 or tolerance_ms < 0:
        raise HTTPException(400, "参数必须为正数/非负")
    sub_bytes = await subtitle.read()
    try:
        parsed = parse_subtitle(sub_bytes, subtitle.filename or "")
    except Exception as exc:
        raise HTTPException(400, f"字幕解析失败：{exc}")
    if not parsed:
        raise HTTPException(400, "字幕文件中未解析出任何条目")

    shots = []
    if shots_file is not None and shots_file.filename:
        raw = await shots_file.read()
        try:
            shots = parse_shots(raw)
        except Exception as exc:
            raise HTTPException(400, f"镜头 JSON 解析失败：{exc}")

    cues_payload = [{
        "cue_id": c.cue_id, "ord": c.ord,
        "start_ms": c.start_ms, "end_ms": c.end_ms,
        "text": visible_text(c.raw_text),
        "raw_text": c.raw_text,
        "graphemes": c.graphemes, "raw_hash": c.raw_hash,
    } for c in parsed]
    shots_payload = [{"ord": s.ord, "start_ms": s.start_ms,
                      "end_ms": s.end_ms} for s in shots]
    with db.get_conn(write=True) as conn:
        pid = db.create_project(conn, name, cps_limit, min_gap_ms,
                                tolerance_ms, cues_payload, shots_payload)
        return _recompute(conn, pid)


@app.get("/api/projects/{pid}")
def get_project(pid: int):
    with db.get_conn() as conn:
        return _recompute(conn, pid)


@app.put("/api/projects/{pid}/settings")
def update_settings(pid: int, body: SettingsIn):
    if body.cps_limit <= 0 or body.min_gap_ms < 0 or body.tolerance_ms < 0:
        raise HTTPException(400, "参数非法")
    # 先取 RESERVED 写锁再读状态：与并发的 POST /adopt 互斥。若采纳
    # 事务先提交，这里锁内读到的就是含新采纳的最新状态，revalidate
    # 会撤销新参数下失效者；若本事务先拿到锁，采纳请求会阻塞到本事务
    # 提交，随后按新参数重读并拒绝失效旧提案（见 adopt）。
    with db.get_conn(write=True) as conn:
        prow = db.get_project(conn, pid)
        if prow is None:
            raise HTTPException(404, "项目不存在")
        cue_rows = list(db.get_cues(conn, pid))
        shot_rows = list(db.get_shots(conn, pid))
        shots = [Shot(ord=s["ord"], start_ms=s["start_ms"], end_ms=s["end_ms"])
                 for s in shot_rows]
        parsed = _parsed_from_rows(cue_rows)
        # 同一写事务内：用新参数求提案（与详情/采纳校验完全同一套
        # build_analysis 规则）。失败则由 get_conn 整体回滚，参数与
        # 采纳状态都不会被改动。
        analysis = build_analysis(
            parsed, shots, body.cps_limit,
            body.min_gap_ms, body.tolerance_ms,
        )
        proposals = {
            rep.cue_id: (
                (rep.proposal_start_ms, rep.proposal_end_ms)
                if rep.proposal_start_ms is not None else None
            ) for rep in analysis.cues
        }
        # 落新参数；随后按「已采纳取采纳值，未采纳先取新提案、无提案取
        # 原值」的生效位置重核验每条采纳，撤销失效者。重算若失败，
        # 下面任一步异常都会触发整事务回滚，不留半更新。
        db.update_settings(conn, pid, body.cps_limit,
                           body.min_gap_ms, body.tolerance_ms)
        shot_ranges = [(s["start_ms"], s["end_ms"]) for s in shot_rows]
        for uid in revalidate_adoptions(
            cue_rows, shot_ranges, body.tolerance_ms, body.min_gap_ms,
            proposals,
        ):
            db.delete_adoption(conn, pid, uid)
        payload = _recompute(conn, pid)
        _prune_gap_reviews(conn, pid, payload)
        return payload


@app.post("/api/projects/{pid}/adopt")
def adopt(pid: int, body: AdoptIn):
    # 先取写锁再读取：若并发的 PUT settings 先提交，本请求会在 BEGIN
    # IMMEDIATE 处等待，锁内重读时拿到的已是新参数，随后 _recompute
    # 用新参数求提案——旧提案位置在此被判失效而拒绝（400），不会把按
    # 旧参数算出的采纳值与新参数并存落库。
    with db.get_conn(write=True) as conn:
        if db.get_project(conn, pid) is None:
            raise HTTPException(404, "项目不存在")
        cue_rows = list(db.get_cues(conn, pid))
        target = next((r for r in cue_rows if r["id"] == body.cue_uid), None)
        if target is None:
            raise HTTPException(400, "cue 不存在")
        orig_dur = target["end_ms"] - target["start_ms"]
        if body.new_end_ms - body.new_start_ms != orig_dur:
            raise HTTPException(400, "校正仅允许平移：时长必须与原时长完全一致")
        # 必须落在当前（锁内最新）参数下的提案位置（重新计算并比对）
        payload = _recompute(conn, pid)
        cand = next((c for c in payload["analysis"]["cues"]
                     if c["uid"] == body.cue_uid), None)
        if cand is None or cand["proposal_start_ms"] is None:
            raise HTTPException(400, "该 cue 在当前参数下无可采纳提案")
        if (body.new_start_ms != cand["proposal_start_ms"]
                or body.new_end_ms != cand["proposal_end_ms"]):
            raise HTTPException(400, "采纳值必须等于系统给出的提案时间")
        db.upsert_adoption(conn, pid, body.cue_uid,
                           body.new_start_ms, body.new_end_ms)
        payload = _recompute(conn, pid)
        _prune_gap_reviews(conn, pid, payload)
        return payload


@app.delete("/api/projects/{pid}/adopt/{cue_uid}")
def revert_adoption(pid: int, cue_uid: int):
    with db.get_conn(write=True) as conn:
        db.delete_adoption(conn, pid, cue_uid)
        payload = _recompute(conn, pid)
        _prune_gap_reviews(conn, pid, payload)
        return payload


# ---------- 逐条文案修订 ----------

def _check_text_base(conn, pid: int, cue_uid: int, base_text: str):
    """写锁内读取目标 cue 并校验 base_text（旧页面提交识别）。

    另一标签页已先改过该条文案时，当前文案与页面编辑基准不一致 → 409
    拒绝覆盖，detail 携带最新文案供页面展示；cue 不存在 → 400。
    """
    row = db.get_cue(conn, pid, cue_uid)
    if row is None:
        raise HTTPException(400, "cue 不存在")
    current = db.current_text(row)
    if current != base_text:
        raise HTTPException(status_code=409, detail={
            "message": "该条文案已在别处被修改，请基于最新文案重新编辑",
            "current_text": current,
            "edited": row["edited_text"] is not None,
        })
    return row


@app.put("/api/projects/{pid}/cues/{cue_uid}/text")
def edit_cue_text(pid: int, cue_uid: int, body: TextEditIn):
    """保存一条文案修订。

    归一化沿用导入同一套规则（去标签、折叠空白、UAX #29 字素簇计数），
    归一化后为空 → 400 不写库；cue 不存在 → 400；base 不符 → 409。
    写路径与参数更新 / 采纳共用同一事务边界：先取写锁再读最新状态，
    任一步失败整体回滚，不留半更新。修订只改当前文案：cue_id、原时间、
    采纳与镜头留白结论不变，原文哈希仍指向导入内容。
    """
    new_text = visible_text(body.text)
    if not new_text:
        raise HTTPException(
            400, "文案不能为空：去标签、折叠空白后需至少保留一个可见字符")
    new_graphemes = count_graphemes(body.text)
    with db.get_conn(write=True) as conn:
        if db.get_project(conn, pid) is None:
            raise HTTPException(404, "项目不存在")
        row = _check_text_base(conn, pid, cue_uid, body.base_text)
        if new_text == row["text"]:
            # 修订稿与导入原文一致：等同恢复导入原文，不留修订标记
            db.clear_cue_text_edit(conn, pid, cue_uid)
        else:
            db.update_cue_text(conn, pid, cue_uid, new_text, new_graphemes)
        return _recompute(conn, pid)


@app.post("/api/projects/{pid}/cues/{cue_uid}/text/restore")
def restore_cue_text(pid: int, cue_uid: int, body: TextRestoreIn):
    """恢复导入原文：清除该条修订稿（未修订时幂等成功）。

    与修订同一套旧页面识别：base_text 不符 → 409 并回传最新文案，
    不会把另一标签页的新修订覆盖回导入原文。
    """
    with db.get_conn(write=True) as conn:
        if db.get_project(conn, pid) is None:
            raise HTTPException(404, "项目不存在")
        _check_text_base(conn, pid, cue_uid, body.base_text)
        db.clear_cue_text_edit(conn, pid, cue_uid)
        return _recompute(conn, pid)


@app.delete("/api/projects/{pid}")
def remove_project(pid: int):
    with db.get_conn(write=True) as conn:
        db.delete_project(conn, pid)
    return {"ok": True}


# ---------- 镜头留白审阅 ----------

def _find_gap_item(payload: dict, shot_ord: int,
                   gap_start_ms: int, gap_end_ms: int) -> dict | None:
    return next((it for it in payload["gap_review"]["items"]
                 if it["shot_ord"] == shot_ord
                 and it["gap_start_ms"] == gap_start_ms
                 and it["gap_end_ms"] == gap_end_ms), None)


@app.post("/api/projects/{pid}/gaps/review")
def review_gap(pid: int, body: GapReviewIn):
    if body.decision not in DECISIONS:
        raise HTTPException(
            400, "decision 仅支持 intentional（有意留白）/ "
                 "needs_subtitle（待补字幕）")
    # 与采纳同理：先取写锁，锁内按最新参数与采纳状态重算留白。过期页面
    # 提交的区间匹配不到当前留白 → 400 拒绝，不会把结论误标到变化后的
    # 新留白上；匹配到即按审阅项身份（镜头 ord + 边界 + 留白起止）落库。
    with db.get_conn(write=True) as conn:
        if db.get_project(conn, pid) is None:
            raise HTTPException(404, "项目不存在")
        payload = _recompute(conn, pid)
        item = _find_gap_item(payload, body.shot_ord,
                              body.gap_start_ms, body.gap_end_ms)
        if item is None:
            raise HTTPException(
                400, "该留白区间已随参数或采纳状态变化，请刷新后重试")
        db.upsert_gap_review(conn, pid, body.shot_ord,
                             item["shot_start_ms"], item["shot_end_ms"],
                             body.gap_start_ms, body.gap_end_ms,
                             body.decision)
        return _recompute(conn, pid)


@app.delete("/api/projects/{pid}/gaps/review")
def clear_gap_review(pid: int, shot_ord: int,
                     gap_start_ms: int, gap_end_ms: int):
    # 清除标记（回到待审）。同样先取写锁并校验留白仍存在，过期页面的
    # 清除请求一律 400 拒绝。
    with db.get_conn(write=True) as conn:
        if db.get_project(conn, pid) is None:
            raise HTTPException(404, "项目不存在")
        payload = _recompute(conn, pid)
        if _find_gap_item(payload, shot_ord, gap_start_ms,
                          gap_end_ms) is None:
            raise HTTPException(
                400, "该留白区间已随参数或采纳状态变化，请刷新后重试")
        db.delete_gap_review(conn, pid, shot_ord, gap_start_ms, gap_end_ms)
        return _recompute(conn, pid)


# ---------- 导出 ----------

def _fmt_ms(ms: int) -> str:
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, mss = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{mss:03d}"


def _build_srt(cues: list[dict], key_start="proposal_start_ms",
               key_end="proposal_end_ms") -> str:
    out = []
    for i, c in enumerate(cues, start=1):
        s, e = c[key_start], c[key_end]
        if s is None:
            s, e = c["start_ms"], c["end_ms"]
        out.append(f"{i}\n{_fmt_ms(s)} --> {_fmt_ms(e)}\n{c['text']}\n")
    return "\n".join(out)


def _effective_times(c: dict) -> tuple[int, int]:
    """导出时间轴上一条 cue 的生效时间：已采纳取采纳值；未采纳先取提案，
    无提案取原值。独立 SRT 与 ZIP 内 SRT 共用同一有效时间轴。"""
    s, e = c["adopted_start_ms"], c["adopted_end_ms"]
    if s is None:
        s, e = c["proposal_start_ms"], c["proposal_end_ms"]
    if s is None:
        s, e = c["start_ms"], c["end_ms"]
    return s, e


@app.get("/api/projects/{pid}/export")
def export(pid: int, fmt: str = "json"):
    with db.get_conn() as conn:
        data = _recompute(conn, pid)
    if fmt == "srt":
        srt_cues = [{
            "proposal_start_ms": s, "proposal_end_ms": e, "text": c["text"],
        } for c in data["analysis"]["cues"]
            for s, e in [_effective_times(c)]]
        body = _build_srt(srt_cues)
        return Response(content=body, media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition":
                                 "attachment; filename=corrected.srt"})
    if fmt not in ("json", "zip"):
        raise HTTPException(400, "fmt 仅支持 json / srt / zip")

    export_doc = {
        "schema": "subtitle-correction-export/1.0",
        "project": {
            "id": data["id"],
            "name": data["name"],
            "cps_limit": data["cps_limit"],
            "min_gap_ms": data["min_gap_ms"],
            "tolerance_ms": data["tolerance_ms"],
        },
        "document_hash": "",
        "cues": [{
            "cue_id": c["cue_id"],
            "raw_text_hash": c["raw_hash"],
            "graphemes": c["graphemes"],
            "original": {"start_ms": c["start_ms"], "end_ms": c["end_ms"]},
            "proposal": (
                {"start_ms": c["proposal_start_ms"],
                 "end_ms": c["proposal_end_ms"]}
                if c["proposal_start_ms"] is not None else None
            ),
            "corrected": (
                {"start_ms": c["adopted_start_ms"],
                 "end_ms": c["adopted_end_ms"]}
                if c["adopted"] else None
            ),
            "status": ("corrected" if c["adopted"]
                       else ("proposed" if c["proposal_start_ms"] is not None
                             else "conflict")),
            "text": c["text"],
            "edited": c["edited"],
            "imported_text": c["imported_text"],
        } for c in data["analysis"]["cues"]],
        "conflicts": {
            "chains": data["analysis"]["chains"],
            "intrinsic": data["analysis"]["intrinsic"],
        },
        "stats": data["analysis"]["stats"],
    }
    raw = json.dumps(export_doc, ensure_ascii=False, indent=2, sort_keys=True)
    doc_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    export_doc["document_hash"] = doc_hash
    raw = json.dumps(export_doc, ensure_ascii=False, indent=2, sort_keys=True)

    if fmt == "json":
        return Response(
            content=raw, media_type="application/json; charset=utf-8",
            headers={"Content-Disposition":
                     "attachment; filename=correction.json"})

    # zip：JSON + SRT，SRT 与独立 SRT 导出共用同一有效时间轴
    # （采纳值，未采纳者回退提案再回退原值）
    srt_cues = []
    for c in data["analysis"]["cues"]:
        s, e = _effective_times(c)
        srt_cues.append({"proposal_start_ms": s, "proposal_end_ms": e,
                         "text": c["text"]})
    srt_text = _build_srt(srt_cues)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("correction.json", raw)
        zf.writestr("corrected.srt", srt_text)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition":
                 "attachment; filename=correction.zip"})


# ---------- 交付字幕核验 ----------

@app.post("/api/projects/{pid}/verify-delivery")
async def verify_delivery(pid: int, delivery: UploadFile = File(...)):
    """上传回传的交付稿（SRT / WebVTT），与项目当前生效时间轴对照核验。

    只读操作：复用 parse_subtitle 解析回传稿，生效时间轴与 SRT 导出共用
    同一规则（已采纳 → 提案 → 原值）；保序最优对应、等优歧义待确认的全部
    规则见 app.verification。不改动采纳、留白结论与任何导出。空稿或解析
    失败返回 400，核验不落库。响应携带 project_id 与 timeline_hash
    （与项目详情同源的生效轴指纹），调用方据此把结论绑定到「该项目、
    该生效轴」：生效轴变化后旧结论即过期，不得继续展示。
    """
    sub_bytes = await delivery.read()
    try:
        parsed = parse_subtitle(sub_bytes, delivery.filename or "")
    except Exception as exc:
        raise HTTPException(400, f"交付字幕解析失败：{exc}")
    if not parsed:
        raise HTTPException(400, "交付字幕文件中未解析出任何条目")

    with db.get_conn() as conn:
        data = _recompute(conn, pid)
    refs = [
        vfy.TimelineLine(
            ord=c["ord"], cue_id=c["cue_id"], text=c["text"],
            start_ms=s, end_ms=e,
        ) for c in data["analysis"]["cues"]
        for s, e in [_effective_times(c)]
    ]
    # 保序对应为 O(生效条目 × 交付条目)：限制两侧规模避免误传超大稿导致
    # 请求长时间占用；真实交付稿（千条以内）均在秒级完成。
    if len(parsed) > VERIFY_MAX_CUES or len(refs) > VERIFY_MAX_CUES:
        raise HTTPException(
            400, f"核验条目数过多（>{VERIFY_MAX_CUES}），请拆分为更小的稿件")
    delivered = [
        vfy.TimelineLine(
            ord=i, cue_id=None, text=visible_text(c.raw_text),
            start_ms=c.start_ms, end_ms=c.end_ms,
        ) for i, c in enumerate(parsed, start=1)
    ]
    return {"project_id": data["id"], "project_name": data["name"],
            "delivery_name": delivery.filename or "",
            "timeline_hash": data["timeline_hash"],
            **vfy.verify_delivery(refs, delivered)}


# ---------- 静态前端 ----------

_STATIC = Path(__file__).parent / "static"
if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/")
    def index():
        return Response(content=(_STATIC / "index.html").read_text(encoding="utf-8"),
                        media_type="text/html; charset=utf-8")
