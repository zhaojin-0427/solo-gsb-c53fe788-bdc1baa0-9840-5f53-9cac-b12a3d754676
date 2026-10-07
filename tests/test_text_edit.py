"""逐条文案修订与恢复导入原文的端到端回归测试。

覆盖：
- 保存归一化：去标签、折叠空白、UAX #29 字素簇计数（emoji 组合序列按 1）；
  归一化后为空（空白稿 / 纯标签）→ 400 且库中无任何写入；
- 单调修订版本 text_version：导入为 0，仅当前文案真正改变的已提交写事务
  +1；归一化后同文案保存、未修订时幂等恢复、空白 / 不存在 / 409 失败事务
  一律不推进；详情 / 保存 / 恢复 / 409 响应携带同一基准；
- 旧页覆盖核心场景：甲打开 X(v0)，乙先保存 Y(v1) 再恢复 X(v2)，甲仍以
  base_text=X/base_version=0 保存 Z → 409（仅比对文字会漏放），detail 回传
  最新文案与 v2；甲基于最新基准重编辑才写入 v3；
- 导入原文与修订稿的区分：详情/分析/导出中 text 为当前文案，imported_text
  与 raw_hash 始终指向导入内容，edited 标记随修订/恢复切换；
- CPS 告警同步：修订改变字素数后 cps / cps_ok 与统计条按新文案重算；
- 旧页面提交识别：base_version 不符（另一标签页先改过）→ 409 拒绝覆盖，
  detail 携带最新文案与最新版本，库中保持先提交者的值；恢复同样校验；
- 恢复导入原文：清除修订稿（未修订时幂等且不推进版本），字素/CPS 复原；
  修订稿与导入原文一致时保存等同恢复并推进版本；
- timeline_hash：修订改变生效轴指纹（旧交付结论过期），恢复后回到原指纹
  （指纹只由文案 / 时间决定，不直接含版本号）；
- 联动：JSON / SRT / ZIP 导出用当前文案，cue_id、原时间、采纳与镜头留白
  结论不变；核验 refs 用当前文案；
- 原子性：非法保存（空白 / 不存在）后版本、详情与三种导出字节级不变；
- 旧库迁移：迁移前无修订列的旧库迁移后仍可修订，版本从 0 推进。
"""
import asyncio
import hashlib
import io
import json
import os
import sqlite3
import sys
import zipfile

DB = "/tmp/test_text_edit.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_PATH"] = DB  # 须在导入 app 之前设置

sys.path.insert(0, "/workspace")

from fastapi import HTTPException, UploadFile  # noqa: E402

from app import database as db  # noqa: E402
from app import main as m  # noqa: E402

db.init_db()

SRT = ("1\n00:00:00,000 --> 00:00:01,000\n甲\n\n"
       "2\n00:00:01,000 --> 00:00:02,000\n乙\n\n"
       "3\n00:00:02,000 --> 00:00:03,000\n丙\n")
SHOTS = json.dumps([{"start_ms": 0, "end_ms": 10000}])


def _upload(name: str, content: str) -> UploadFile:
    return UploadFile(io.BytesIO(content.encode("utf-8")), filename=name)


def _create(srt=SRT, shots=SHOTS, **kw):
    return asyncio.run(m.create_project(
        name="文案修订场景", cps_limit=kw.get("cps_limit", 21.0),
        min_gap_ms=kw.get("min_gap_ms", 0),
        tolerance_ms=kw.get("tolerance_ms", 200),
        subtitle=_upload("a.srt", srt),
        shots_file=_upload("s.json", shots) if shots else None,
    ))


def _cue(proj, i=0):
    return proj["analysis"]["cues"][i]


def _current(pid, uid):
    """库内（重算详情里）目标 cue 的当前文案与版本——编辑基准的权威来源。"""
    c = next(c for c in m.get_project(pid)["analysis"]["cues"]
             if c["uid"] == uid)
    return c["text"], c["text_version"]


def _edit(pid, uid, text, base=None, version=None):
    # base / version 缺省时取库中当前基准，模拟页面「打开面板时记下基准」。
    if base is None or version is None:
        cur_text, cur_ver = _current(pid, uid)
        if base is None:
            base = cur_text
        if version is None:
            version = cur_ver
    return m.edit_cue_text(
        pid, uid,
        m.TextEditIn(text=text, base_text=base, base_version=version),
    )


def _restore(pid, uid, base=None, version=None):
    if base is None or version is None:
        cur_text, cur_ver = _current(pid, uid)
        if base is None:
            base = cur_text
        if version is None:
            version = cur_ver
    return m.restore_cue_text(
        pid, uid,
        m.TextRestoreIn(base_text=base, base_version=version),
    )


def _expect_409(fn, current_text=None, current_version=None):
    try:
        fn()
        raise AssertionError("过期提交应 409")
    except HTTPException as exc:
        assert exc.status_code == 409, exc.status_code
        assert "最新文案" in exc.detail["message"]
        if current_text is not None:
            assert exc.detail["current_text"] == current_text
        if current_version is not None:
            assert exc.detail["current_version"] == current_version, exc.detail
        return exc.detail


def _export_json(pid):
    return json.loads(m.export(pid, fmt="json").body.decode("utf-8"))


# ---------- 保存归一化与详情联动 ----------

def test_edit_normalizes_and_updates_detail():
    proj = _create()
    pid = proj["id"]
    c = _cue(proj, 0)
    assert c["text"] == "甲" and c["edited"] is False
    assert c["imported_text"] == "甲" and c["graphemes"] == 1
    assert c["text_version"] == 0                  # 导入版本为 0
    assert all(x["text_version"] == 0 for x in _cue_list(proj))
    # 详情顶层 cues 列表同样携带版本
    row = next(r for r in proj["cues"] if r["uid"] == c["uid"])
    assert row["text_version"] == 0

    # 含标签、多余空白、emoji 组合序列（👨‍👩‍👧 为 1 个字素簇）
    proj = _edit(pid, c["uid"], "  <i>你好</i>\n\t世界 👨‍👩‍👧  ", "甲", 0)
    c = _cue(proj, 0)
    assert c["text"] == "你好 世界 👨‍👩‍👧", c["text"]
    assert c["edited"] is True
    assert c["text_version"] == 1                  # 真正改变 → 版本 +1
    assert _cue(proj, 1)["text_version"] == 0      # 他条不动
    assert c["imported_text"] == "甲"              # 导入原文不变
    assert c["graphemes"] == 7                     # 你好/空格/世界/空格/emoji
    assert c["raw_hash"] == hashlib.sha256("甲".encode()).hexdigest()
    # 时长 1000ms、7 字素 → CPS 7.0
    assert c["cps"] == 7.0 and c["cps_ok"] is True
    # 详情 cues 列表同步
    row = next(r for r in proj["cues"] if r["uid"] == c["uid"])
    assert row["text"] == "你好 世界 👨‍👩‍👧" and row["edited"] is True
    assert row["imported_text"] == "甲" and row["graphemes"] == 7
    assert row["text_version"] == 1
    # GET 详情一致（持久化）
    again = m.get_project(pid)
    assert _cue(again, 0)["text"] == "你好 世界 👨‍👩‍👧"
    assert _cue(again, 0)["text_version"] == 1
    print("edit normalize + detail sync + version ok")
    return pid


def _cue_list(proj):
    return proj["analysis"]["cues"]


def test_blank_and_missing_rejected_without_partial_write():
    proj = _create()
    pid = proj["id"]
    uid = _cue(proj, 0)["uid"]
    before = m.get_project(pid)
    doc_before = m.export(pid, fmt="json").body
    srt_before = m.export(pid, fmt="srt").body
    with zipfile.ZipFile(io.BytesIO(m.export(pid, fmt="zip").body)) as zf:
        zip_before = {n: zf.read(n) for n in zf.namelist()}

    # 空白稿：空串 / 纯空白 / 纯标签 → 400
    for blank in ("", "   \n\t  ", "<i><b>\n</b></i>", "<v 张三> </v>"):
        try:
            _edit(pid, uid, blank, "甲", 0)
            raise AssertionError(f"空白稿应被拒绝：{blank!r}")
        except HTTPException as exc:
            assert exc.status_code == 400, exc.status_code
    # 不存在的 cue → 400；不存在的项目 → 404
    try:
        _edit(pid, 999999, "新文案", "甲", 0)
        raise AssertionError("不存在的 cue 应 400")
    except HTTPException as exc:
        assert exc.status_code == 400
    try:
        m.edit_cue_text(999999, uid,
                        m.TextEditIn(text="新文案", base_text="甲",
                                     base_version=0))
        raise AssertionError("不存在的项目应 404")
    except HTTPException as exc:
        assert exc.status_code == 404
    try:
        _restore(pid, 999999, "甲", 0)
        raise AssertionError("不存在的 cue 恢复应 400")
    except HTTPException as exc:
        assert exc.status_code == 400

    # 全部失败：版本、详情与三种导出字节级不变（无部分写入、版本不推进）
    after = m.get_project(pid)
    assert after == before
    assert [c["text_version"] for c in after["analysis"]["cues"]] == [0, 0, 0]
    assert m.export(pid, fmt="json").body == doc_before
    assert m.export(pid, fmt="srt").body == srt_before
    with zipfile.ZipFile(io.BytesIO(m.export(pid, fmt="zip").body)) as zf:
        assert {n: zf.read(n) for n in zf.namelist()} == zip_before
    print("blank / missing rejected, no partial write, version untouched ok")


def test_stale_page_rejected_with_latest_text():
    proj = _create()
    pid = proj["id"]
    uid = _cue(proj, 0)["uid"]

    # 标签页 A 先改：甲 v0 → A稿 v1
    proj = _edit(pid, uid, "A稿", "甲", 0)
    assert _cue(proj, 0)["text"] == "A稿"
    assert _cue(proj, 0)["text_version"] == 1
    # 标签页 B 仍基于「甲 v0」提交 → 409，detail 携带最新文案与 v1，
    # 库中不被覆盖、版本不推进
    detail = _expect_409(lambda: _edit(pid, uid, "B稿", "甲", 0),
                         current_text="A稿", current_version=1)
    assert detail["edited"] is True
    assert _cue(m.get_project(pid), 0)["text"] == "A稿"
    assert _current(pid, uid) == ("A稿", 1)
    # B 拿到最新文案后以其为 base（A稿 v1）再提交 → 成功，v2
    proj = _edit(pid, uid, "B稿", "A稿", 1)
    assert _cue(proj, 0)["text"] == "B稿"
    assert _cue(proj, 0)["text_version"] == 2

    # 恢复同样识别旧页面：另一标签页先把 B稿 v2 改为 C稿 v3
    proj = _edit(pid, uid, "C稿", "B稿", 2)
    assert _cue(proj, 0)["text_version"] == 3
    _expect_409(lambda: _restore(pid, uid, "B稿", 2),
                current_text="C稿", current_version=3)
    assert _cue(m.get_project(pid), 0)["text"] == "C稿"   # 未被覆盖回原文
    assert _current(pid, uid) == ("C稿", 3)
    print("stale page 409 + latest text/version ok")


def test_save_then_restore_stale_page_must_409():
    """核心缺陷回归：仅比对当前文字会漏拒的旧页。

    甲打开 X(v0)；乙先保存 Y(v1) 再恢复 X(v2)——文字已回到 X，旧实现
    比对 base_text=X 相等而接受甲的 Z，覆盖乙的恢复。版本裁决下甲必须
    收到 409（current_text=X、current_version=2），库中保持 X/v2；甲
    基于最新基准成功重编辑后才写入 Z(v3)。
    """
    proj = _create()
    pid = proj["id"]
    uid = _cue(proj, 0)["uid"]
    assert _current(pid, uid) == ("甲", 0)

    # 乙：保存 Y，再恢复 X
    proj = _edit(pid, uid, "Y稿", "甲", 0)
    assert _cue(proj, 0)["text_version"] == 1
    proj = _restore(pid, uid, "Y稿", 1)
    assert _cue(proj, 0)["text"] == "甲"
    assert _cue(proj, 0)["edited"] is False
    assert _cue(proj, 0)["text_version"] == 2     # 恢复也推进版本
    assert _current(pid, uid) == ("甲", 2)

    # 甲仍以打开时的 甲 v0 保存 Z → 必须 409（文字相同也不行）
    _expect_409(lambda: _edit(pid, uid, "Z稿", "甲", 0),
                current_text="甲", current_version=2)
    # 库中保持恢复后的 X，版本停在 2
    assert _current(pid, uid) == ("甲", 2)
    assert _cue(m.get_project(pid), 0)["edited"] is False
    # 甲的过期恢复同样 409
    _expect_409(lambda: _restore(pid, uid, "甲", 0),
                current_text="甲", current_version=2)
    assert _current(pid, uid) == ("甲", 2)

    # 甲按 409 回传的最新文案 / 版本重新形成基准后重编辑 → 成功 v3
    proj = _edit(pid, uid, "Z稿", "甲", 2)
    c = _cue(proj, 0)
    assert c["text"] == "Z稿" and c["edited"] is True
    assert c["text_version"] == 3
    print("save->restore then stale page 409, re-edit succeeds ok")


def test_same_text_save_and_idempotent_restore_do_not_bump():
    """裁决：归一化后同文案保存、未修订时幂等恢复都不推进版本。

    既不漏拒旧页（版本不符即便文字相同也 409），也不误拒有效编辑
    （同一基准上把文案原样重存 / 重复恢复应 200 幂等）。
    """
    proj = _create()
    pid = proj["id"]
    uid = _cue(proj, 0)["uid"]

    # 未修订 cue：归一化后 == 当前（导入）文案的保存 → 幂等，版本不推进
    proj = _edit(pid, uid, " 甲 ", "甲", 0)
    c = _cue(proj, 0)
    assert c["text"] == "甲" and c["edited"] is False
    assert c["text_version"] == 0
    # 未修订时恢复：幂等成功，版本不推进
    proj = _restore(pid, uid, "甲", 0)
    assert _cue(proj, 0)["text_version"] == 0

    # 已修订 cue：保存与修订稿归一化后相同的文案 → 幂等，保留修订标记，
    # 版本不推进
    proj = _edit(pid, uid, "修订稿", "甲", 0)
    assert _cue(proj, 0)["text_version"] == 1
    proj = _edit(pid, uid, " 修订稿\n", "修订稿", 1)
    c = _cue(proj, 0)
    assert c["text"] == "修订稿" and c["edited"] is True
    assert c["text_version"] == 1
    # 同基准重复恢复请求不会凭空发生（恢复会变文案），但保存稿归一化后
    # 等于当前修订稿时再来一次仍幂等
    proj = _edit(pid, uid, "修订稿", "修订稿", 1)
    assert _cue(proj, 0)["text_version"] == 1

    # 但「文字相同 + 版本过期」仍是旧页 → 409，不能因幂等而漏拒
    _expect_409(lambda: _edit(pid, uid, "甲", "甲", 0),
                current_text="修订稿", current_version=1)
    print("same-text save / idempotent restore no bump, stale still 409 ok")


def test_restore_round_trip_and_idempotent():
    proj = _create()
    pid = proj["id"]
    uid = _cue(proj, 0)["uid"]
    h0 = proj["timeline_hash"]

    proj = _edit(pid, uid, "修订稿", "甲", 0)
    assert _cue(proj, 0)["edited"] is True
    assert _cue(proj, 0)["text_version"] == 1
    h1 = proj["timeline_hash"]
    assert h1 != h0                       # 修订改变生效轴指纹 → 旧核验结论过期

    # 恢复导入原文：当前文案、字素、CPS、指纹全部回到导入状态，版本 +1
    proj = _restore(pid, uid, "修订稿", 1)
    c = _cue(proj, 0)
    assert c["edited"] is False and c["text"] == "甲" and c["graphemes"] == 1
    assert c["text_version"] == 2
    assert proj["timeline_hash"] == h0    # 文案回到原值 → 指纹回到原值
    # 未修订时恢复：幂等成功，状态 / 版本都不变
    proj = _restore(pid, uid, "甲", 2)
    assert _cue(proj, 0)["edited"] is False
    assert _cue(proj, 0)["text_version"] == 2
    assert proj["timeline_hash"] == h0

    # 修订稿与导入原文一致：保存等同恢复，不留修订标记；当前文案改变 →
    # 版本 +1
    proj = _edit(pid, uid, "另一个稿", "甲", 2)
    assert _cue(proj, 0)["edited"] is True
    assert _cue(proj, 0)["text_version"] == 3
    proj = _edit(pid, uid, " 甲 ", "另一个稿", 3)   # 归一化后 == 导入原文
    c = _cue(proj, 0)
    assert c["edited"] is False and c["text"] == "甲"
    assert c["text_version"] == 4
    assert proj["timeline_hash"] == h0
    print("restore round-trip + idempotent ok")


def test_cps_alert_follows_edit():
    # 10 字文案压进 400ms → CPS 25 > 21 超限；修订为 4 字 → CPS 10 告警解除
    srt = "1\n00:00:00,000 --> 00:00:00,400\n一二三四五六七八九十\n"
    proj = _create(srt=srt, shots=None)
    pid = proj["id"]
    c = _cue(proj, 0)
    assert c["graphemes"] == 10 and c["cps"] == 25.0 and c["cps_ok"] is False
    assert proj["analysis"]["stats"]["cps_violations"] == 1

    proj = _edit(pid, c["uid"], "一二三四")
    c = _cue(proj, 0)
    assert c["graphemes"] == 4 and c["cps"] == 10.0 and c["cps_ok"] is True
    assert proj["analysis"]["stats"]["cps_violations"] == 0
    print("cps alert follows edit ok")


def test_exports_and_invariants():
    proj = _create()
    pid = proj["id"]
    cues = proj["analysis"]["cues"]
    # 先制造采纳与留白结论
    proj = m.adopt(pid, m.AdoptIn(cue_uid=cues[0]["uid"],
                                  new_start_ms=cues[0]["proposal_start_ms"],
                                  new_end_ms=cues[0]["proposal_end_ms"]))
    gaps = proj["gap_review"]["items"]
    assert gaps, "含镜头项目应有留白项"
    proj = m.review_gap(pid, m.GapReviewIn(
        shot_ord=gaps[0]["shot_ord"], gap_start_ms=gaps[0]["gap_start_ms"],
        gap_end_ms=gaps[0]["gap_end_ms"], decision="intentional"))

    # 修订第二条：乙 → 乙（修订）
    uid2 = cues[1]["uid"]
    proj = _edit(pid, uid2, "乙（修订）", "乙", 0)
    assert _cue(proj, 1)["text_version"] == 1

    # JSON 导出：text 为修订稿，raw_text_hash 仍指向导入内容，含区分字段；
    # 导出语义保持不变——版本号不进入导出（document_hash 口径不变）
    doc = _export_json(pid)
    ec = next(c for c in doc["cues"] if c["cue_id"] == "2")
    assert ec["text"] == "乙（修订）"
    assert ec["edited"] is True and ec["imported_text"] == "乙"
    assert ec["raw_text_hash"] == hashlib.sha256("乙".encode()).hexdigest()
    assert ec["graphemes"] == 5
    assert "text_version" not in ec
    # cue_id、原时间不变；第一条采纳不变
    assert ec["cue_id"] == "2"
    assert ec["original"] == {"start_ms": 1000, "end_ms": 2000}
    c1 = next(c for c in doc["cues"] if c["cue_id"] == "1")
    assert c1["status"] == "corrected" and c1["corrected"] is not None
    assert c1["edited"] is False and c1["imported_text"] == "甲"

    # SRT 用修订文案；ZIP 内 SRT 与独立 SRT 一致
    srt_body = m.export(pid, fmt="srt").body.decode("utf-8")
    assert "乙（修订）" in srt_body and "\n乙\n" not in srt_body
    with zipfile.ZipFile(io.BytesIO(m.export(pid, fmt="zip").body)) as zf:
        assert zf.read("corrected.srt").decode("utf-8") == srt_body
        zdoc = json.loads(zf.read("correction.json").decode("utf-8"))
    assert zdoc == doc

    # 采纳与留白结论保持不变；他条版本不推进
    detail = m.get_project(pid)
    assert _cue(detail, 0)["adopted"] is True
    assert _cue(detail, 0)["text_version"] == 0
    item = next(it for it in detail["gap_review"]["items"]
                if (it["shot_ord"], it["gap_start_ms"], it["gap_end_ms"])
                == (gaps[0]["shot_ord"], gaps[0]["gap_start_ms"],
                    gaps[0]["gap_end_ms"]))
    assert item["decision"] == "intentional"
    print("exports sync + invariants ok")


def test_verify_uses_current_text():
    proj = _create()
    pid = proj["id"]
    uid = _cue(proj, 0)["uid"]
    h0 = proj["timeline_hash"]
    proj = _edit(pid, uid, "甲修订", "甲", 0)
    assert proj["timeline_hash"] != h0    # 旧交付结论按指纹机制判过期

    # 交付稿用修订文案 → 通过；用导入原文 → 缺失 + 新增
    v = asyncio.run(m.verify_delivery(pid, _upload(
        "d.srt",
        "1\n00:00:00,000 --> 00:00:01,000\n甲修订\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n乙\n\n"
        "3\n00:00:02,000 --> 00:00:03,000\n丙\n")))
    assert v["summary"]["pass"] == 3, v["summary"]
    assert v["timeline_hash"] == proj["timeline_hash"]
    v = asyncio.run(m.verify_delivery(pid, _upload("d.srt", SRT)))
    s = v["summary"]
    # 「甲修订」与交付稿「甲」等时但文案不同：配上（同文案对数不增）与
    # 双双跳过（缺失+新增）等优 → 该行待确认，不强判
    assert s["pending"] == 1 and s["pass"] == 2, s
    row = next(r for r in v["rows"] if r["verdict"] == "pending")
    assert row["text"] == "甲修订"        # 生效轴 refs 用当前文案
    print("verify uses current text ok")


def test_migration_keeps_existing_rows():
    # 新建库：修订列为 NULL、版本为 0 → 未修订
    proj = _create()
    pid = proj["id"]
    with db.get_conn() as conn:
        rows = list(db.get_cues(conn, pid))
        assert all(r["edited_text"] is None for r in rows)
        assert all(db.current_text(r) == r["text"] for r in rows)
        assert all(r["text_version"] == 0 for r in rows)
    print("migration default NULL/version 0 ok")


def test_migration_from_legacy_db_still_editable():
    """真实旧库（迁移前无修订列 / 版本列）：init_db 迁移后行可读、
    版本默认 0，且仍可正常修订并推进到 1、再恢复到 2。"""
    legacy = "/tmp/test_text_edit_legacy.db"
    if os.path.exists(legacy):
        os.remove(legacy)
    raw = sqlite3.connect(legacy)
    try:
        # 旧版 schema：cues 无 edited_text / edited_graphemes / text_version
        raw.executescript(
            """
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                cps_limit REAL NOT NULL, min_gap_ms INTEGER NOT NULL,
                tolerance_ms INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE cues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL, ord INTEGER NOT NULL,
                cue_id TEXT NOT NULL, start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL, text TEXT NOT NULL,
                raw_text TEXT NOT NULL, graphemes INTEGER NOT NULL,
                raw_hash TEXT NOT NULL
            );
            """
        )
        raw.execute(
            "INSERT INTO projects (id,name,cps_limit,min_gap_ms,tolerance_ms)"
            " VALUES (1,'旧库',21.0,0,200)")
        raw.execute(
            "INSERT INTO cues (id,project_id,ord,cue_id,start_ms,end_ms,"
            "text,raw_text,graphemes,raw_hash) VALUES "
            "(10,1,1,'1',0,1000,'旧','旧',1,?)",
            (hashlib.sha256("旧".encode()).hexdigest(),))
        raw.commit()
    finally:
        raw.close()

    orig_path = db.DB_PATH
    db.DB_PATH = legacy
    try:
        db.init_db()
        with db.get_conn() as conn:
            cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(cues)")}
            assert {"edited_text", "edited_graphemes",
                    "text_version"} <= cols
            row = db.get_cue(conn, 1, 10)
            assert db.current_text(row) == "旧"
            assert row["text_version"] == 0
        # 旧库迁移后仍可修订：以 v0 为基准成功，版本推进到 1
        proj = m.edit_cue_text(1, 10, m.TextEditIn(
            text="新文案", base_text="旧", base_version=0))
        c = _cue(proj, 0)
        assert c["text"] == "新文案" and c["edited"] is True
        assert c["text_version"] == 1
        # 旧基准（v0）立即失效 → 409
        _expect_409(lambda: m.restore_cue_text(
            1, 10, m.TextRestoreIn(base_text="旧", base_version=0)),
            current_text="新文案", current_version=1)
        # 恢复 → v2
        proj = m.restore_cue_text(1, 10, m.TextRestoreIn(
            base_text="新文案", base_version=1))
        assert _cue(proj, 0)["text"] == "旧"
        assert _cue(proj, 0)["text_version"] == 2
    finally:
        db.DB_PATH = orig_path
    print("legacy db migration: still editable, version advances ok")


# ---------- 并发：两个标签页同基准同时保存 ----------

def test_failed_transaction_does_not_bump_version():
    """版本 +1 的 UPDATE 之后、事务提交之前若任一步抛错，整体回滚：
    库中文案与 text_version 都停留在提交前的值，不出现「版本空进」。"""
    proj = _create()
    pid = proj["id"]
    uid = _cue(proj, 0)["uid"]
    orig_recompute = m._recompute
    calls = {"n": 0}

    def boom(conn, pid_arg):
        calls["n"] += 1
        result = orig_recompute(conn, pid_arg)
        raise RuntimeError("模拟落库后重算失败")

    m._recompute = boom
    try:
        try:
            _edit(pid, uid, "新稿", "甲", 0)
            raise AssertionError("事务内失败应向上抛出")
        except RuntimeError as exc:
            assert "模拟落库后重算失败" in str(exc)
    finally:
        m._recompute = orig_recompute
    assert calls["n"] == 1
    # 整体回滚：文案未变、版本仍为 0、未留修订标记
    assert _current(pid, uid) == ("甲", 0)
    detail = m.get_project(pid)
    c = _cue(detail, 0)
    assert c["text"] == "甲" and c["edited"] is False
    # 失败后同一基准仍可正常保存
    proj = _edit(pid, uid, "新稿", "甲", 0)
    assert _cue(proj, 0)["text_version"] == 1
    print("failed transaction: version rolled back ok")


def _run(fn):
    import threading
    box = {}

    def worker():
        try:
            box["result"] = fn()
        except BaseException as exc:  # 含 HTTPException
            box["error"] = exc

    th = threading.Thread(target=worker)
    th.start()
    return th, box


def test_concurrent_edits_second_rejected():
    """A、B 两标签页都以「甲 v0」为基准同时保存：先提交者落库（v1），
    后到者在写锁上等待，锁内重读发现版本已推进 → 409 拒绝覆盖并回传
    最新文案与 v1，库中始终保持先提交者的值。"""
    import threading
    proj = _create()
    pid = proj["id"]
    uid = _cue(proj, 0)["uid"]

    entered = threading.Event()
    release = threading.Event()
    orig = db.update_cue_text

    def gated(*args, **kwargs):     # A 落库前持锁等待，B 在写锁上排队
        out = orig(*args, **kwargs)
        entered.set()
        assert release.wait(timeout=10), "门控未放行"
        return out

    db.update_cue_text = gated
    try:
        th_a, box_a = _run(lambda: _edit(pid, uid, "A稿", "甲", 0))
        assert entered.wait(timeout=10), "A 未进入持锁阶段"
        th_b, box_b = _run(lambda: _edit(pid, uid, "B稿", "甲", 0))
        threading.Event().wait(0.5)   # 确保 B 在写锁上排队
        release.set()
        th_a.join(timeout=20)
        th_b.join(timeout=20)
    finally:
        db.update_cue_text = orig

    assert "error" not in box_a, box_a.get("error")
    assert _cue(box_a["result"], 0)["text_version"] == 1
    err = box_b.get("error")
    assert isinstance(err, HTTPException) and err.status_code == 409, err
    assert err.detail["current_text"] == "A稿"
    assert err.detail["current_version"] == 1
    # 终态：只有 A 的修订，B 未覆盖，版本只 +1
    assert _cue(m.get_project(pid), 0)["text"] == "A稿"
    assert _current(pid, uid) == ("A稿", 1)
    print("concurrent edits: second rejected with latest text/version ok")


def main():
    test_edit_normalizes_and_updates_detail()
    test_blank_and_missing_rejected_without_partial_write()
    test_stale_page_rejected_with_latest_text()
    test_save_then_restore_stale_page_must_409()
    test_same_text_save_and_idempotent_restore_do_not_bump()
    test_restore_round_trip_and_idempotent()
    test_cps_alert_follows_edit()
    test_exports_and_invariants()
    test_verify_uses_current_text()
    test_migration_keeps_existing_rows()
    test_migration_from_legacy_db_still_editable()
    test_failed_transaction_does_not_bump_version()
    test_concurrent_edits_second_rejected()
    print("text edit regression ok")


if __name__ == "__main__":
    main()
