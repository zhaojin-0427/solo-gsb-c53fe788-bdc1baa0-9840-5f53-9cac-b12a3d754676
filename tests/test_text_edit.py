"""逐条文案修订与恢复导入原文的端到端回归测试。

覆盖：
- 保存归一化：去标签、折叠空白、UAX #29 字素簇计数（emoji 组合序列按 1）；
  归一化后为空（空白稿 / 纯标签）→ 400 且库中无任何写入；
- 导入原文与修订稿的区分：详情/分析/导出中 text 为当前文案，imported_text
  与 raw_hash 始终指向导入内容，edited 标记随修订/恢复切换；
- CPS 告警同步：修订改变字素数后 cps / cps_ok 与统计条按新文案重算；
- 旧页面提交识别：base_text 不符（另一标签页先改过）→ 409 拒绝覆盖，
  detail 携带最新文案，库中保持先提交者的值；恢复同样校验；
- 恢复导入原文：清除修订稿（未修订时幂等），字素/CPS 复原；修订稿与导入
  原文一致时保存等同恢复；
- timeline_hash：修订改变生效轴指纹（旧交付结论过期），恢复后回到原指纹；
- 联动：JSON / SRT / ZIP 导出用当前文案，cue_id、原时间、采纳与镜头留白
  结论不变；核验 refs 用当前文案；
- 原子性：非法保存（空白 / 不存在）后详情与三种导出字节级不变。
"""
import asyncio
import hashlib
import io
import json
import os
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


def _edit(pid, uid, text, base):
    return m.edit_cue_text(pid, uid, m.TextEditIn(text=text, base_text=base))


def _restore(pid, uid, base):
    return m.restore_cue_text(pid, uid, m.TextRestoreIn(base_text=base))


def _export_json(pid):
    return json.loads(m.export(pid, fmt="json").body.decode("utf-8"))


# ---------- 保存归一化与详情联动 ----------

def test_edit_normalizes_and_updates_detail():
    proj = _create()
    pid = proj["id"]
    c = _cue(proj, 0)
    assert c["text"] == "甲" and c["edited"] is False
    assert c["imported_text"] == "甲" and c["graphemes"] == 1

    # 含标签、多余空白、emoji 组合序列（👨‍👩‍👧 为 1 个字素簇）
    proj = _edit(pid, c["uid"], "  <i>你好</i>\n\t世界 👨‍👩‍👧  ", "甲")
    c = _cue(proj, 0)
    assert c["text"] == "你好 世界 👨‍👩‍👧", c["text"]
    assert c["edited"] is True
    assert c["imported_text"] == "甲"           # 导入原文不变
    assert c["graphemes"] == 7                  # 你好/空格/世界/空格/emoji
    assert c["raw_hash"] == hashlib.sha256("甲".encode()).hexdigest()
    # 时长 1000ms、7 字素 → CPS 7.0
    assert c["cps"] == 7.0 and c["cps_ok"] is True
    # 详情 cues 列表同步
    row = next(r for r in proj["cues"] if r["uid"] == c["uid"])
    assert row["text"] == "你好 世界 👨‍👩‍👧" and row["edited"] is True
    assert row["imported_text"] == "甲" and row["graphemes"] == 7
    # GET 详情一致（持久化）
    again = m.get_project(pid)
    assert _cue(again, 0)["text"] == "你好 世界 👨‍👩‍👧"
    print("edit normalize + detail sync ok")
    return pid


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
            _edit(pid, uid, blank, "甲")
            raise AssertionError(f"空白稿应被拒绝：{blank!r}")
        except HTTPException as exc:
            assert exc.status_code == 400, exc.status_code
    # 不存在的 cue → 400；不存在的项目 → 404
    try:
        _edit(pid, 999999, "新文案", "甲")
        raise AssertionError("不存在的 cue 应 400")
    except HTTPException as exc:
        assert exc.status_code == 400
    try:
        _edit(999999, uid, "新文案", "甲")
        raise AssertionError("不存在的项目应 404")
    except HTTPException as exc:
        assert exc.status_code == 404
    try:
        _restore(pid, 999999, "甲")
        raise AssertionError("不存在的 cue 恢复应 400")
    except HTTPException as exc:
        assert exc.status_code == 400

    # 全部失败：详情与三种导出字节级不变（无部分写入）
    assert m.get_project(pid) == before
    assert m.export(pid, fmt="json").body == doc_before
    assert m.export(pid, fmt="srt").body == srt_before
    with zipfile.ZipFile(io.BytesIO(m.export(pid, fmt="zip").body)) as zf:
        assert {n: zf.read(n) for n in zf.namelist()} == zip_before
    print("blank / missing rejected, no partial write ok")


def test_stale_page_rejected_with_latest_text():
    proj = _create()
    pid = proj["id"]
    uid = _cue(proj, 0)["uid"]

    # 标签页 A 先改：甲 → A稿
    proj = _edit(pid, uid, "A稿", "甲")
    assert _cue(proj, 0)["text"] == "A稿"
    # 标签页 B 仍基于「甲」提交 → 409，detail 携带最新文案，库中不被覆盖
    try:
        _edit(pid, uid, "B稿", "甲")
        raise AssertionError("过期页面提交应 409")
    except HTTPException as exc:
        assert exc.status_code == 409, exc.status_code
        assert exc.detail["current_text"] == "A稿"
        assert exc.detail["edited"] is True
        assert "最新文案" in exc.detail["message"]
    assert _cue(m.get_project(pid), 0)["text"] == "A稿"
    # B 拿到最新文案后以其为 base 再提交 → 成功
    proj = _edit(pid, uid, "B稿", "A稿")
    assert _cue(proj, 0)["text"] == "B稿"

    # 恢复同样识别旧页面：另一标签页先把 B稿 改为 C稿
    proj = _edit(pid, uid, "C稿", "B稿")
    try:
        _restore(pid, uid, "B稿")   # 过期 base
        raise AssertionError("过期恢复应 409")
    except HTTPException as exc:
        assert exc.status_code == 409
        assert exc.detail["current_text"] == "C稿"
    assert _cue(m.get_project(pid), 0)["text"] == "C稿"   # 未被覆盖回原文
    print("stale page 409 + latest text ok")


def test_restore_round_trip_and_idempotent():
    proj = _create()
    pid = proj["id"]
    uid = _cue(proj, 0)["uid"]
    h0 = proj["timeline_hash"]

    proj = _edit(pid, uid, "修订稿", "甲")
    assert _cue(proj, 0)["edited"] is True
    h1 = proj["timeline_hash"]
    assert h1 != h0                       # 修订改变生效轴指纹 → 旧核验结论过期

    # 恢复导入原文：当前文案、字素、CPS、指纹全部回到导入状态
    proj = _restore(pid, uid, "修订稿")
    c = _cue(proj, 0)
    assert c["edited"] is False and c["text"] == "甲" and c["graphemes"] == 1
    assert proj["timeline_hash"] == h0
    # 未修订时恢复：幂等成功，状态不变
    proj = _restore(pid, uid, "甲")
    assert _cue(proj, 0)["edited"] is False
    assert proj["timeline_hash"] == h0

    # 修订稿与导入原文一致：保存等同恢复，不留修订标记
    proj = _edit(pid, uid, "另一个稿", "甲")
    assert _cue(proj, 0)["edited"] is True
    proj = _edit(pid, uid, " 甲 ", "另一个稿")   # 归一化后 == 导入原文
    c = _cue(proj, 0)
    assert c["edited"] is False and c["text"] == "甲"
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

    proj = _edit(pid, c["uid"], "一二三四", c["text"])
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
    proj = _edit(pid, uid2, "乙（修订）", "乙")

    # JSON 导出：text 为修订稿，raw_text_hash 仍指向导入内容，含区分字段
    doc = _export_json(pid)
    ec = next(c for c in doc["cues"] if c["cue_id"] == "2")
    assert ec["text"] == "乙（修订）"
    assert ec["edited"] is True and ec["imported_text"] == "乙"
    assert ec["raw_text_hash"] == hashlib.sha256("乙".encode()).hexdigest()
    assert ec["graphemes"] == 5
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

    # 采纳与留白结论保持不变
    detail = m.get_project(pid)
    assert _cue(detail, 0)["adopted"] is True
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
    proj = _edit(pid, uid, "甲修订", "甲")
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
    # 模拟旧库：建表（含新列）后插入的项目修订列为 NULL → 未修订
    proj = _create()
    pid = proj["id"]
    with db.get_conn() as conn:
        rows = list(db.get_cues(conn, pid))
        assert all(r["edited_text"] is None for r in rows)
        assert all(db.current_text(r) == r["text"] for r in rows)
    print("migration default NULL ok")


# ---------- 并发：两个标签页同基准同时保存 ----------

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
    """A、B 两标签页都以「甲」为基准同时保存：先提交者落库，后到者在写锁
    上等待，锁内重读发现当前文案已变 → 409 拒绝覆盖并回传最新文案，
    库中始终保持先提交者的值。"""
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
        th_a, box_a = _run(lambda: _edit(pid, uid, "A稿", "甲"))
        assert entered.wait(timeout=10), "A 未进入持锁阶段"
        th_b, box_b = _run(lambda: _edit(pid, uid, "B稿", "甲"))
        threading.Event().wait(0.5)   # 确保 B 在写锁上排队
        release.set()
        th_a.join(timeout=20)
        th_b.join(timeout=20)
    finally:
        db.update_cue_text = orig

    assert "error" not in box_a, box_a.get("error")
    err = box_b.get("error")
    assert isinstance(err, HTTPException) and err.status_code == 409, err
    assert err.detail["current_text"] == "A稿"
    # 终态：只有 A 的修订，B 未覆盖
    assert _cue(m.get_project(pid), 0)["text"] == "A稿"
    print("concurrent edits: second rejected with latest text ok")


def main():
    test_edit_normalizes_and_updates_detail()
    test_blank_and_missing_rejected_without_partial_write()
    test_stale_page_rejected_with_latest_text()
    test_restore_round_trip_and_idempotent()
    test_cps_alert_follows_edit()
    test_exports_and_invariants()
    test_verify_uses_current_text()
    test_migration_keeps_existing_rows()
    test_concurrent_edits_second_rejected()
    print("text edit regression ok")


if __name__ == "__main__":
    main()
