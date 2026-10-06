"""镜头留白审阅的端到端回归测试。

覆盖：
- 留白计算：覆盖裁剪到镜头、重叠/相接覆盖合并、相接/重叠镜头分别审阅、
  无字幕镜头整段一项、无镜头不生成审阅项；
- 生效时间轴与 SRT 导出一致：已采纳 → 提案 → 原值；
- 结论迁移：边界未变的结论保留，变化的区间回到待审（过期行同事务清除），
  过期页面提交被 400 拒绝、不误标新留白；
- 并发交错：设置先提交时，等待写锁的过期审阅提交被拒绝；
- 导出语义不变：审阅结论不影响 JSON / SRT / ZIP 导出。
"""
import asyncio
import io
import json
import os
import sys
import threading
import zipfile

DB = "/tmp/test_gap_review.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_PATH"] = DB  # 须在导入 app 之前设置

sys.path.insert(0, "/workspace")

from fastapi import HTTPException, UploadFile  # noqa: E402

from app import database as db  # noqa: E402
from app import main as m  # noqa: E402
from app.gaps import compute_shot_gaps  # noqa: E402
from app.shots import Shot  # noqa: E402

db.init_db()

SRT = ("1\n00:00:00,100 --> 00:00:00,500\n镜头内字幕\n\n"
       "2\n00:00:00,900 --> 00:00:01,300\n跨镜头字幕\n")
SHOTS = json.dumps([{"start_ms": 0, "end_ms": 1200},
                    {"start_ms": 1200, "end_ms": 2400}])


def _upload(name: str, content: str) -> UploadFile:
    return UploadFile(io.BytesIO(content.encode("utf-8")), filename=name)


def _create(tol=200, srt=SRT, shots=SHOTS, gap=40):
    return asyncio.run(m.create_project(
        name="留白场景", cps_limit=21.0, min_gap_ms=gap, tolerance_ms=tol,
        subtitle=_upload("a.srt", srt),
        shots_file=_upload("s.json", shots) if shots else None,
    ))


def _review(pid, shot_ord, gs, ge, decision):
    return m.review_gap(pid, m.GapReviewIn(
        shot_ord=shot_ord, gap_start_ms=gs, gap_end_ms=ge,
        decision=decision))


def _gap_tuples(payload):
    return [(it["shot_ord"], it["gap_start_ms"], it["gap_end_ms"],
             it["decision"]) for it in payload["gap_review"]["items"]]


def _stored_reviews(pid):
    with db.get_conn() as conn:
        return [(r["shot_ord"], r["gap_start_ms"], r["gap_end_ms"],
                 r["decision"]) for r in db.get_gap_reviews(conn, pid)]


# ---------- 留白计算（单元） ----------

def test_unit_merge_touching_and_overlapping_coverage():
    shot = [Shot(ord=1, start_ms=0, end_ms=300)]
    # 相接覆盖合并：[0,100) 与 [100,200) 端点相贴 → 覆盖 [0,200)
    items = compute_shot_gaps(shot, [(0, 100), (100, 200)])
    assert [(g.gap_start_ms, g.gap_end_ms) for g in items] == [(200, 300)]
    # 重叠覆盖合并
    items = compute_shot_gaps(shot, [(0, 150), (100, 200)])
    assert [(g.gap_start_ms, g.gap_end_ms) for g in items] == [(200, 300)]
    # 裁剪到镜头：字幕超出镜头边界的部分不计
    items = compute_shot_gaps(shot, [(-50, 100), (250, 400)])
    assert [(g.gap_start_ms, g.gap_end_ms) for g in items] == [(100, 250)]


def test_unit_overlapping_shots_reviewed_separately():
    shots = [Shot(ord=1, start_ms=0, end_ms=1000),
             Shot(ord=2, start_ms=500, end_ms=1500)]
    items = compute_shot_gaps(shots, [(600, 900)])
    got = [(g.shot_ord, g.gap_start_ms, g.gap_end_ms) for g in items]
    assert got == [(1, 0, 600), (1, 900, 1000),
                   (2, 500, 600), (2, 900, 1500)], got


def test_unit_empty_shot_whole_segment_and_no_shots():
    # 完全无字幕覆盖的镜头：整段一项
    items = compute_shot_gaps([Shot(ord=1, start_ms=10, end_ms=50)], [])
    assert [(g.gap_start_ms, g.gap_end_ms) for g in items] == [(10, 50)]
    # 字幕恰好止于镜头起点 / 起于镜头终点：不覆盖该镜头
    items = compute_shot_gaps([Shot(ord=1, start_ms=1000, end_ms=2000)],
                              [(0, 1000), (2000, 3000)])
    assert [(g.gap_start_ms, g.gap_end_ms) for g in items] == [(1000, 2000)]
    # 无镜头：不生成审阅项
    assert compute_shot_gaps([], [(0, 100)]) == []


# ---------- 端到端：留白项与生效时间轴 ----------

def test_gap_items_follow_effective_timeline():
    proj = _create()
    gr = proj["gap_review"]
    assert gr["shot_count"] == 2 and gr["total"] == 3
    assert gr["pending_count"] == 3
    # 生效时间轴：c1 提案=原值 100–500，c2 提案 800–1200（未采纳先取提案）
    assert _gap_tuples(proj) == [
        (1, 0, 100, None), (1, 500, 800, None), (2, 1200, 2400, None)]
    # 完全无字幕的镜头 2：整段一项
    assert gr["items"][2]["duration_ms"] == 1200

    # 采纳 c2（800–1200，与提案一致）：生效时间轴不变 → 留白边界不变
    pid = proj["id"]
    cue2 = proj["analysis"]["cues"][1]
    proj = m.adopt(pid, m.AdoptIn(cue_uid=cue2["uid"],
                                  new_start_ms=800, new_end_ms=1200))
    assert _gap_tuples(proj) == [
        (1, 0, 100, None), (1, 500, 800, None), (2, 1200, 2400, None)]
    print("gap items follow effective timeline ok")
    return pid


# ---------- 结论标记与状态迁移 ----------

def test_review_mark_and_state_migration(pid):
    # 标记：未变边界 (1,0,100) 与将变边界 (1,500,800)、(2,1200,2400)
    proj = _review(pid, 1, 0, 100, "needs_subtitle")
    proj = _review(pid, 1, 500, 800, "intentional")
    proj = _review(pid, 2, 1200, 2400, "needs_subtitle")
    assert _gap_tuples(proj) == [
        (1, 0, 100, "needs_subtitle"), (1, 500, 800, "intentional"),
        (2, 1200, 2400, "needs_subtitle")]
    assert proj["gap_review"]["pending_count"] == 0

    # GET 详情一致（结论持久化）
    detail = m.get_project(pid)
    assert _gap_tuples(detail) == _gap_tuples(proj)

    # 参数重算 tol=50：c2 无提案 → 生效时间轴回退原值 900–1300。
    # (1,0,100) 边界未变 → 结论保留；其余区间边界变化 → 回到待审，
    # 过期结论在设置事务内被清除。
    proj = m.update_settings(pid, m.SettingsIn(
        cps_limit=21.0, min_gap_ms=40, tolerance_ms=50))
    assert _gap_tuples(proj) == [
        (1, 0, 100, "needs_subtitle"), (1, 500, 900, None),
        (2, 1300, 2400, None)], _gap_tuples(proj)
    assert _stored_reviews(pid) == [(1, 0, 100, "needs_subtitle")]

    # 过期页面按旧边界 (1,500,800) 提交 → 400 拒绝，不产生任何新结论
    try:
        _review(pid, 1, 500, 800, "intentional")
        raise AssertionError("过期提交应被拒绝")
    except HTTPException as exc:
        assert exc.status_code == 400, exc.status_code
    assert _gap_tuples(m.get_project(pid)) == [
        (1, 0, 100, "needs_subtitle"), (1, 500, 900, None),
        (2, 1300, 2400, None)]
    # 非法 decision 值同样 400
    try:
        _review(pid, 1, 500, 900, "bogus")
        raise AssertionError("非法 decision 应被拒绝")
    except HTTPException as exc:
        assert exc.status_code == 400
    print("review mark + state migration ok")


def test_revert_changes_boundaries_and_prunes():
    """撤销改变生效时间轴时，边界变化的留白回到待审。"""
    srt = ("1\n00:00:00,000 --> 00:00:01,000\n甲\n\n"
           "2\n00:00:01,000 --> 00:00:02,000\n乙\n")
    shots = json.dumps([{"start_ms": 0, "end_ms": 3000}])
    proj = _create(srt=srt, shots=shots, gap=40)
    pid = proj["id"]
    cues = proj["analysis"]["cues"]
    assert [(c["proposal_start_ms"], c["proposal_end_ms"]) for c in cues] == [
        (0, 1000), (1040, 2040)]
    # 生效时间轴：c1 0–1000、c2 1040–2040 → 留白 [1000,1040)、[2040,3000)
    assert _gap_tuples(proj) == [(1, 1000, 1040, None),
                                 (1, 2040, 3000, None)]

    # 采纳 c2@1040–2040 钉住位置；min_gap 收紧到 30 后提案变为 1030–2030，
    # 但采纳仍合法（间隔 40 ≥ 30）→ 生效时间轴不变，结论保留
    proj = m.adopt(pid, m.AdoptIn(cue_uid=cues[1]["uid"],
                                  new_start_ms=1040, new_end_ms=2040))
    proj = _review(pid, 1, 1000, 1040, "intentional")
    proj = m.update_settings(pid, m.SettingsIn(
        cps_limit=21.0, min_gap_ms=30, tolerance_ms=200))
    assert proj["analysis"]["cues"][1]["adopted"] is True
    assert _gap_tuples(proj) == [(1, 1000, 1040, "intentional"),
                                 (1, 2040, 3000, None)]

    # 撤销 c2：生效位置回退到新提案 1030–2030 → 留白边界变化，
    # (1,1000,1040) 的结论过期 → 回到待审并在同事务清除
    proj = m.revert_adoption(pid, cues[1]["uid"])
    assert _gap_tuples(proj) == [(1, 1000, 1030, None),
                                 (1, 2030, 3000, None)]
    assert _stored_reviews(pid) == []
    print("revert boundary change prunes stale reviews ok")


def test_clear_review_and_stale_clear_rejected():
    proj = _create()
    pid = proj["id"]
    proj = _review(pid, 1, 500, 800, "intentional")
    assert _gap_tuples(proj)[1][3] == "intentional"
    # 清除标记 → 回到待审
    proj = m.clear_gap_review(pid, shot_ord=1, gap_start_ms=500,
                              gap_end_ms=800)
    assert _gap_tuples(proj)[1][3] is None
    assert _stored_reviews(pid) == []
    # 清除不存在的区间（过期/从未存在）→ 400
    try:
        m.clear_gap_review(pid, shot_ord=1, gap_start_ms=500,
                           gap_end_ms=801)
        raise AssertionError("过期清除应被拒绝")
    except HTTPException as exc:
        assert exc.status_code == 400
    print("clear review ok")


def test_no_shots_no_review_items():
    proj = _create(shots=None)
    pid = proj["id"]
    gr = proj["gap_review"]
    assert gr["shot_count"] == 0 and gr["total"] == 0 and gr["items"] == []
    try:
        _review(pid, 1, 0, 100, "intentional")
        raise AssertionError("无镜头项目不应有可审阅区间")
    except HTTPException as exc:
        assert exc.status_code == 400
    print("no shots -> no review items ok")


# ---------- 导出语义不变 ----------

def test_exports_unaffected_by_reviews():
    proj = _create()
    pid = proj["id"]
    doc_before = json.loads(m.export(pid, fmt="json").body.decode("utf-8"))
    srt_before = m.export(pid, fmt="srt").body
    with zipfile.ZipFile(io.BytesIO(m.export(pid, fmt="zip").body)) as zf:
        zip_before = {n: zf.read(n) for n in zf.namelist()}

    _review(pid, 1, 0, 100, "needs_subtitle")
    _review(pid, 1, 500, 800, "intentional")
    _review(pid, 2, 1200, 2400, "needs_subtitle")

    # JSON 导出结构不变（不含留白审阅内容），字节级一致
    doc_after = json.loads(m.export(pid, fmt="json").body.decode("utf-8"))
    assert set(doc_after.keys()) == {
        "schema", "project", "document_hash", "cues", "conflicts", "stats"}
    assert doc_after == doc_before
    # SRT 字节级一致
    assert m.export(pid, fmt="srt").body == srt_before
    # ZIP 成员内容一致
    with zipfile.ZipFile(io.BytesIO(m.export(pid, fmt="zip").body)) as zf:
        assert {n: zf.read(n) for n in zf.namelist()} == zip_before
    print("exports unaffected by reviews ok")


# ---------- 并发：设置先提交，过期审阅提交被拒绝 ----------

def _run(fn):
    box = {}

    def worker():
        try:
            box["result"] = fn()
        except BaseException as exc:  # 含 HTTPException
            box["error"] = exc

    th = threading.Thread(target=worker)
    th.start()
    return th, box


def _join(th):
    th.join(timeout=20)
    assert not th.is_alive(), "并发事务互相等待超时"


class _Gate:
    """被调函数执行后置 entered、放行前阻塞（持锁不提交）。"""

    def __init__(self, orig):
        self.orig = orig
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, *args, **kwargs):
        out = self.orig(*args, **kwargs)
        self.entered.set()
        assert self.release.wait(timeout=10), "门控未放行"
        return out


def test_stale_review_rejected_when_settings_commit_first():
    proj = _create()
    pid = proj["id"]
    _review(pid, 1, 500, 800, "intentional")

    gate = _Gate(m.db.update_settings)
    m.db.update_settings = gate
    try:
        # A：设置收紧到 50，落参数后持锁等待
        th_a, box_a = _run(lambda: m.update_settings(
            pid, m.SettingsIn(cps_limit=21.0, min_gap_ms=40,
                              tolerance_ms=50)))
        assert gate.entered.wait(timeout=10), "设置事务未进入持锁阶段"
        # B：过期页面按旧留白 (1,500,800) 提交 → 在写锁上等待，
        #    A 提交后锁内重算，区间已不存在 → 400 拒绝
        th_b, box_b = _run(lambda: _review(pid, 1, 500, 800,
                                           "needs_subtitle"))
        threading.Event().wait(0.5)  # 确保 B 在写锁上排队
        gate.release.set()
        _join(th_a)
        _join(th_b)
    finally:
        m.db.update_settings = gate.orig

    assert "error" not in box_a, box_a.get("error")
    err = box_b.get("error")
    assert isinstance(err, HTTPException) and err.status_code == 400, err
    # 终态：tol=50 的留白全部待审；旧结论被 A 的事务清除；B 未误标新留白
    detail = m.get_project(pid)
    assert _gap_tuples(detail) == [
        (1, 0, 100, None), (1, 500, 900, None), (2, 1300, 2400, None)]
    assert _stored_reviews(pid) == []
    print("stale review rejected after settings commit ok")


def main():
    test_unit_merge_touching_and_overlapping_coverage()
    test_unit_overlapping_shots_reviewed_separately()
    test_unit_empty_shot_whole_segment_and_no_shots()
    pid = test_gap_items_follow_effective_timeline()
    test_review_mark_and_state_migration(pid)
    test_revert_changes_boundaries_and_prunes()
    test_clear_review_and_stale_clear_rejected()
    test_no_shots_no_review_items()
    test_exports_unaffected_by_reviews()
    test_stale_review_rejected_when_settings_commit_first()
    print("gap review regression ok")


if __name__ == "__main__":
    main()
