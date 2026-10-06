"""交付字幕核验的端到端回归测试。

覆盖：
- 通过：回传稿与生效时间轴起止毫秒完全一致（允许重编号、文案重复）；
- 时间不符：同文案但时间不同（只在起止毫秒均相同时判通过）；
- 缺失 / 新增：插删行；拆并行保持一一对应、不自动合并；
- 待确认：等优对应（同文案对数与偏差和都并列）会改变某行结论时，
  相关行列为待确认，不强判缺失 / 时间不符 / 新增；
- 保序对应不按 cue_id / 行号、不按贪心就近配对；
- 生效时间轴：已采纳 → 提案 → 原值；
- timeline_hash 绑定：参数重算/采纳/撤销改变生效轴则指纹必变，未改变
  （相同参数重提、仅调 CPS、采纳值即提案值）或修改失败则不变；核验响应
  与项目详情携带同一指纹，供前端作废旧结论；
- 空稿 / 解析失败 400；核验不改动采纳、留白结论与 JSON/SRT/ZIP 导出。
"""
import asyncio
import io
import json
import os
import sys
import zipfile

DB = "/tmp/test_delivery_verify.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_PATH"] = DB  # 须在导入 app 之前设置

sys.path.insert(0, "/workspace")

from fastapi import HTTPException, UploadFile  # noqa: E402

from app import main as m  # noqa: E402
from app import verification as vfy  # noqa: E402

m.db.init_db()

SRT = ("1\n00:00:00,000 --> 00:00:01,000\n甲\n\n"
       "2\n00:00:01,000 --> 00:00:02,000\n乙\n\n"
       "3\n00:00:02,000 --> 00:00:03,000\n丙\n")


def _upload(name: str, content: str) -> UploadFile:
    return UploadFile(io.BytesIO(content.encode("utf-8")), filename=name)


def _create(srt=SRT, shots=None, **kw):
    return asyncio.run(m.create_project(
        name="核验场景", cps_limit=kw.get("cps_limit", 21.0),
        min_gap_ms=kw.get("min_gap_ms", 40),
        tolerance_ms=kw.get("tolerance_ms", 200),
        subtitle=_upload("a.srt", srt),
        shots_file=_upload("s.json", shots) if shots else None,
    ))


def _verify(pid, content, name="delivered.srt"):
    return asyncio.run(
        m.verify_delivery(pid, _upload(name, content)))


def _rows(v):
    return [(r["kind"], r["verdict"]) for r in v["rows"]]


# ---------- 单元：保序最优对应（不经过 HTTP/DB） ----------

def _lines(spec):
    return [vfy.TimelineLine(ord=i + 1, cue_id=f"r{i + 1}",
                             text=t, start_ms=s, end_ms=e)
            for i, (t, s, e) in enumerate(spec)]


def _dlines(spec):
    return [vfy.TimelineLine(ord=i + 1, text=t, start_ms=s, end_ms=e)
            for i, (t, s, e) in enumerate(spec)]


def test_unit_all_pass_with_renumber_and_duplicate_text():
    refs = _lines([("同", 0, 1000), ("同", 1000, 2000), ("丙", 2000, 3000)])
    # 重编号（VTT 用文案当 cue id 风格，序号全换）+ 前两行文案重复
    deliv = _dlines([("同", 0, 1000), ("同", 1000, 2000), ("丙", 2000, 3000)])
    v = vfy.verify_delivery(refs, deliv)
    assert v["matched_text_count"] == 3
    assert v["summary"] == {"pass": 3, "time_mismatch": 0, "missing": 0,
                            "new": 0, "pending": 0}, v["summary"]
    print("unit all-pass (renumber + duplicate) ok")


def test_unit_time_mismatch_only_when_exact_ms():
    refs = _lines([("甲", 0, 1000)])
    v = vfy.verify_delivery(refs, _dlines([("甲", 1, 1000)]))
    assert v["summary"]["time_mismatch"] == 1 and v["summary"]["pass"] == 0
    v = vfy.verify_delivery(refs, _dlines([("甲", 0, 999)]))
    assert v["summary"]["time_mismatch"] == 1
    print("unit time mismatch requires exact both ms ok")


def test_unit_missing_new_and_split_not_merged():
    refs = _lines([("甲", 0, 1000), ("乙", 1000, 2000)])
    # 交付稿删掉甲、把乙拆成两行（时间相接）：一一对应，不并条。
    # 乙与两个拆分子项等距（偏差均 500）→ 两种等优对应互换
    # 「时间不符 ↔ 新增」，涉及行全部待确认，不强判；甲稳定缺失。
    deliv = _dlines([("乙", 1000, 1500), ("乙", 1500, 2000)])
    v = vfy.verify_delivery(refs, deliv)
    assert v["summary"] == {"pass": 0, "time_mismatch": 0, "missing": 1,
                            "new": 0, "pending": 2}, v["summary"]
    kinds = sorted((r["kind"], r["verdict"]) for r in v["rows"])
    assert kinds == [("del_only", "pending"), ("match", "pending"),
                     ("ref_only", "missing")], kinds
    print("unit missing + split not merged (tie -> pending) ok")


def test_unit_tie_between_missing_and_mismatch_is_pending():
    # 非等优：交付行与 r1 等时、与 r2 偏差 2000，两种配对同文案对数相同
    # 但偏差不同 → 唯一最优配 r1（通过），r2 稳定缺失，不进入待确认。
    refs = _lines([("同", 0, 1000), ("同", 1000, 2000)])
    deliv = _dlines([("同", 0, 1000)])
    v = vfy.verify_delivery(refs, deliv)
    assert v["summary"] == {"pass": 1, "time_mismatch": 0, "missing": 1,
                            "new": 0, "pending": 0}, v["summary"]

    # 真正等优：交付行与两条生效行等距（偏差均为 1000），同文案对数都是 1。
    deliv = _dlines([("同", 500, 1500)])
    v = vfy.verify_delivery(refs, deliv)
    # 配 r1：r2 缺失；配 r2：r1 缺失。两行在不同等优对应下
    # 「时间不符 ↔ 缺失」切换；规范路径的配对行（待确认）+ 另一条待确认行
    assert v["summary"] == {"pass": 0, "time_mismatch": 0, "missing": 0,
                            "new": 0, "pending": 2}, v["summary"]
    kinds = {(r["ref_ord"], r["kind"], r["verdict"]) for r in v["rows"]
             if r["kind"] == "ref_only"}
    # 规范路径只能选一种对应，另一条以 ref_only+待确认 呈现
    assert len(kinds) == 1 and next(iter(kinds))[2] == "pending"
    match = next(r for r in v["rows"] if r["kind"] == "match")
    assert match["verdict"] == "pending"
    print("unit tie -> pending, no forced missing ok")


def test_unit_tie_between_new_and_mismatch_is_pending():
    # 非等优：只有 d1 同文案 → 唯一最优配 d1，文案不同的 d2 稳定新增
    refs = _lines([("同", 500, 1500)])
    deliv = _dlines([("同", 0, 1000), ("不同", 2000, 3000)])
    v = vfy.verify_delivery(refs, deliv)
    assert v["summary"] == {"pass": 0, "time_mismatch": 1, "missing": 0,
                            "new": 1, "pending": 0}, v["summary"]

    # 真正等优：两条交付文案相同且与 r 等距（偏差均 1000）。
    deliv = _dlines([("同", 0, 1000), ("同", 1000, 2000)])
    # 配 d1：偏差 1000，d2 新增；配 d2：偏差 1000，d1 新增（均 1 对同文案）。
    # r 与“未配上的交付行”在两种对应间「时间不符 ↔ 新增」互换：
    # 规范路径的配对待确认行 + 另一条待确认新增行。
    v = vfy.verify_delivery(refs, deliv)
    assert v["summary"] == {"pass": 0, "time_mismatch": 0, "missing": 0,
                            "new": 0, "pending": 2}, v["summary"]
    print("unit tie -> pending, no forced new ok")


def test_unit_not_greedy_order_preserving():
    # 贪心就近/逐行配对会把 r1-d1 配上（偏差 100），造成 r2 缺失、d2 新增；
    # 全局保序最优应配 r1-d2（同文案，偏差 0）与 r2-d3（同文案，偏差 0）。
    refs = _lines([("甲", 1000, 2000), ("乙", 2000, 3000)])
    deliv = _dlines([("杂", 1100, 1900), ("甲", 1000, 2000),
                     ("乙", 2000, 3000)])
    v = vfy.verify_delivery(refs, deliv)
    assert v["matched_text_count"] == 2
    assert v["summary"]["new"] == 1
    assert v["summary"]["missing"] == 0
    assert v["summary"]["time_mismatch"] == 0
    assert v["summary"]["pass"] == 2, v["summary"]
    print("unit order-preserving global optimum (non-greedy) ok")


# ---------- 端到端 ----------

def test_e2e_pass_renumbered_vtt():
    proj = _create()
    pid = proj["id"]
    # 生效时间轴：三行无提案（间隔均为 0 但 min_gap=40 会有提案，因此
    # 这里直接验证“生效时间”取提案/原值的逻辑——先不传镜头、最小间隔 0）
    proj = m.update_settings(pid, m.SettingsIn(
        cps_limit=21.0, min_gap_ms=0, tolerance_ms=200))
    cues = proj["analysis"]["cues"]
    assert [c["proposal_start_ms"] for c in cues] == [0, 1000, 2000]
    # 回传 WebVTT：序号打乱重编号，时间与生效轴一致
    vtt = ("WEBVTT\n\n"
           "99\n00:00:00.000 --> 00:00:01.000\n甲\n\n"
           "7\n00:00:01.000 --> 00:00:02.000\n乙\n\n"
           "42\n00:00:02.000 --> 00:00:03.000\n丙\n")
    v = _verify(pid, vtt, "back.vtt")
    assert v["reference_count"] == 3 and v["delivered_count"] == 3
    assert v["summary"]["pass"] == 3 and v["summary"]["time_mismatch"] == 0
    assert v["summary"]["missing"] == v["summary"]["new"] == 0
    print("e2e all-pass renumbered VTT ok")
    return pid


def test_e2e_mismatch_missing_new():
    proj = _create()
    pid = proj["id"]
    proj = m.update_settings(pid, m.SettingsIn(
        cps_limit=21.0, min_gap_ms=0, tolerance_ms=200))
    # 删了「乙」；「甲」时间偏 100ms；末尾多一条交付方新文案
    srt = ("1\n00:00:00,100 --> 00:00:01,100\n甲\n\n"
           "2\n00:00:02,000 --> 00:00:03,000\n丙\n\n"
           "3\n00:00:03,000 --> 00:00:04,000\n丁\n")
    v = _verify(pid, srt)
    s = v["summary"]
    assert s["pass"] == 1 and s["time_mismatch"] == 1
    assert s["missing"] == 1 and s["new"] == 1 and s["pending"] == 0
    by = {}
    for r in v["rows"]:
        if r["kind"] == "match":
            if r["text"] == "甲":
                assert r["delta_start_ms"] == 100 and r["delta_end_ms"] == 100
            by[r["text"]] = r["verdict"]
        elif r["kind"] == "ref_only":
            by[r["text"]] = r["verdict"]
        else:
            by[r["delivered_text"]] = r["verdict"]
    assert by == {"甲": "time_mismatch", "乙": "missing",
                  "丙": "pass", "丁": "new"}, by
    print("e2e mismatch/missing/new ok")
    return pid


def test_e2e_effective_timeline_adopted_first(pid):
    # 生效时间轴须取「已采纳 → 提案 → 原值」。构造有提案的场景：
    # 相接两条，min_gap=40 → 提案 0–1000 / 1040–2040。
    srt = ("1\n00:00:00,000 --> 00:00:01,000\n甲\n\n"
           "2\n00:00:01,000 --> 00:00:02,000\n乙\n")
    proj = _create(srt=srt)
    pid2 = proj["id"]
    cues = proj["analysis"]["cues"]
    assert [c["proposal_start_ms"] for c in cues] == [0, 1040]

    # 交付稿按【提案】时间回传（均未采纳）：应全部通过（未采纳先取提案）
    ok_by_proposal = ("1\n00:00:00,000 --> 00:00:01,000\n甲\n\n"
                      "2\n00:00:01,040 --> 00:00:02,040\n乙\n")
    v = _verify(pid2, ok_by_proposal)
    assert v["summary"]["pass"] == 2, v["summary"]

    # 按原值回传第二条：与生效轴（提案 1040）不符 → 时间不符
    by_original = ("1\n00:00:00,000 --> 00:00:01,000\n甲\n\n"
                   "2\n00:00:01,000 --> 00:00:02,000\n乙\n")
    v = _verify(pid2, by_original)
    assert v["summary"]["time_mismatch"] == 1 and v["summary"]["pass"] == 1

    # 采纳第二条到 1100–2100？采纳值必须等于提案 → 采纳提案 1040。
    # 再把 min_gap 调成 100 会撤销；这里直接采纳后生效轴仍是提案位置，
    # 主要验证“已采纳优先”：改参数让提案变化但采纳仍合法时以采纳为准。
    proj = m.adopt(pid2, m.AdoptIn(cue_uid=cues[1]["uid"],
                                   new_start_ms=1040, new_end_ms=2040))
    assert proj["analysis"]["cues"][1]["adopted"]
    # min_gap 30：未采纳时提案会变成 1030，但第二条已采纳 1040 仍合法 →
    # 生效轴第二条 = 采纳值 1040
    proj = m.update_settings(pid2, m.SettingsIn(
        cps_limit=21.0, min_gap_ms=30, tolerance_ms=200))
    c2 = proj["analysis"]["cues"][1]
    assert c2["adopted"] and c2["adopted_start_ms"] == 1040
    assert c2["proposal_start_ms"] == 1030
    v = _verify(pid2, ok_by_proposal)
    assert v["summary"]["pass"] == 2, v["summary"]
    print("e2e effective timeline (adopted > proposal > original) ok")


def test_e2e_empty_and_parse_errors():
    proj = _create()
    pid = proj["id"]
    for bad in ("", "   \n  \n", "WEBVTT\n\nNOTE 只有注释块\n"):
        try:
            _verify(pid, bad)
            raise AssertionError("空稿应报错")
        except HTTPException as exc:
            assert exc.status_code == 400, exc.status_code
    # 解析失败（无任何有效时间轴行）
    try:
        _verify(pid, "这不是字幕\n只是两行文字\n")
        raise AssertionError("解析失败应报错")
    except HTTPException as exc:
        assert exc.status_code == 400
    # 项目不存在
    try:
        _verify(999999, "1\n00:00:00,000 --> 00:00:01,000\n甲\n")
        raise AssertionError("不存在的项目应 404")
    except HTTPException as exc:
        assert exc.status_code == 404
    print("e2e empty / parse failure / 404 ok")


def test_e2e_timeline_hash_binding():
    """生效轴指纹 timeline_hash：核验结论与「项目 + 生效轴」绑定。

    参数重算 / 采纳 / 撤销改变了生效轴 → hash 必变（前端据此作废旧结论）；
    未改变生效轴（相同参数重提、仅调 CPS、采纳值即提案值）或修改失败 →
    hash 不变（前端保留有效结论）。核验响应携带与详情一致的 hash。
    场景即 README 用例：相接两条 0–1000 / 1000–2000，最小间隔 40ms 时
    回传稿按 1040ms 提案核验通过；改为 100ms 后提案变为 1100ms。
    """
    srt = ("1\n00:00:00,000 --> 00:00:01,000\n甲\n\n"
           "2\n00:00:01,000 --> 00:00:02,000\n乙\n")
    proj = _create(srt=srt)  # min_gap=40 → 提案 0–1000 / 1040–2040
    pid = proj["id"]
    h1 = proj["timeline_hash"]
    assert [c["proposal_start_ms"] for c in proj["analysis"]["cues"]] == [0, 1040]

    # 回传稿按 1040 提案：全部通过；响应 hash / project_id 与详情一致
    deliv = ("1\n00:00:00,000 --> 00:00:01,000\n甲\n\n"
             "2\n00:00:01,040 --> 00:00:02,040\n乙\n")
    v = _verify(pid, deliv)
    assert v["summary"]["pass"] == 2
    assert v["project_id"] == pid and v["timeline_hash"] == h1
    assert set(v["summary"].keys()) == {"pass", "time_mismatch", "missing",
                                        "new", "pending"}

    # 最小间隔 40 → 100：提案变 1100，生效轴指纹必须变化；旧回传稿不再通过
    proj = m.update_settings(pid, m.SettingsIn(
        cps_limit=21.0, min_gap_ms=100, tolerance_ms=200))
    h2 = proj["timeline_hash"]
    assert h2 != h1
    assert proj["analysis"]["cues"][1]["proposal_start_ms"] == 1100
    v = _verify(pid, deliv)
    assert v["timeline_hash"] == h2
    assert v["summary"]["pass"] == 1 and v["summary"]["time_mismatch"] == 1
    row = next(r for r in v["rows"] if r["kind"] == "match" and r["text"] == "乙")
    # Δ = 交付 − 生效轴 = 1040 − 1100
    assert row["delta_start_ms"] == -60 and row["delta_end_ms"] == -60

    # 相同参数重复提交：生效轴未变 → hash 不变（前端保留结论）
    proj = m.update_settings(pid, m.SettingsIn(
        cps_limit=21.0, min_gap_ms=100, tolerance_ms=200))
    assert proj["timeline_hash"] == h2
    # 仅调 CPS 上限：不改变时间轴 → hash 不变
    proj = m.update_settings(pid, m.SettingsIn(
        cps_limit=10.0, min_gap_ms=100, tolerance_ms=200))
    assert proj["timeline_hash"] == h2

    # 非法参数被拒绝：修改失败 → hash 不变
    try:
        m.update_settings(pid, m.SettingsIn(
            cps_limit=0, min_gap_ms=100, tolerance_ms=200))
        raise AssertionError("非法参数应 400")
    except HTTPException as exc:
        assert exc.status_code == 400
    assert m.get_project(pid)["timeline_hash"] == h2

    # 采纳值即提案值：生效轴不变（未采纳本就回退提案）→ hash 不变
    cues = proj["analysis"]["cues"]
    proj = m.adopt(pid, m.AdoptIn(cue_uid=cues[1]["uid"],
                                  new_start_ms=1100, new_end_ms=2100))
    assert proj["timeline_hash"] == h2
    # min_gap 100 → 30：提案变 1030，采纳 1100 仍合法 → 生效轴仍取采纳值
    # 1100，与 h2 的生效位置相同 → hash 不变，旧结论依然有效
    proj = m.update_settings(pid, m.SettingsIn(
        cps_limit=10.0, min_gap_ms=30, tolerance_ms=200))
    assert proj["timeline_hash"] == h2
    c2 = proj["analysis"]["cues"][1]
    assert c2["adopted"] and c2["adopted_start_ms"] == 1100
    assert c2["proposal_start_ms"] == 1030
    # 撤销采纳：生效轴从采纳值 1100 回落提案 1030 → hash 必变
    proj = m.revert_adoption(pid, cues[1]["uid"])
    h3 = proj["timeline_hash"]
    assert h3 != h2
    v = _verify(pid, deliv)
    assert v["timeline_hash"] == h3 and v["summary"]["time_mismatch"] == 1
    # 重新采纳到当前提案 1030（采纳值即提案值，hash 不变），随后收紧
    # min_gap 使采纳被重算撤销：1030 与新提案 1050 都改变生效轴 → hash 再变
    proj = m.adopt(pid, m.AdoptIn(cue_uid=cues[1]["uid"],
                                  new_start_ms=1030, new_end_ms=2030))
    assert proj["timeline_hash"] == h3
    proj = m.update_settings(pid, m.SettingsIn(
        cps_limit=10.0, min_gap_ms=50, tolerance_ms=200))
    c2 = proj["analysis"]["cues"][1]
    assert not c2["adopted"] and c2["proposal_start_ms"] == 1050
    h4 = proj["timeline_hash"]
    assert h4 not in (h2, h3)
    v = _verify(pid, deliv)
    assert v["timeline_hash"] == h4
    print("e2e timeline_hash binding (stale invalidation) ok")


def test_e2e_verification_changes_nothing():
    proj = _create()
    pid = proj["id"]
    proj = m.update_settings(pid, m.SettingsIn(
        cps_limit=21.0, min_gap_ms=40, tolerance_ms=200))
    # 先制造一条采纳与一条留白结论
    cues = proj["analysis"]["cues"]
    if cues[1]["proposal_start_ms"] is not None:
        m.adopt(pid, m.AdoptIn(cue_uid=cues[1]["uid"],
                               new_start_ms=cues[1]["proposal_start_ms"],
                               new_end_ms=cues[1]["proposal_end_ms"]))
    srt_shots = json.dumps([{"start_ms": 0, "end_ms": 10000}])
    p2 = _create(shots=srt_shots)
    pid2 = p2["id"]
    c2 = p2["analysis"]["cues"]
    m.adopt(pid2, m.AdoptIn(cue_uid=c2[0]["uid"],
                            new_start_ms=c2[0]["proposal_start_ms"],
                            new_end_ms=c2[0]["proposal_end_ms"]))
    gr = m.get_project(pid2)["gap_review"]["items"]
    if gr:
        m.review_gap(pid2, m.GapReviewIn(
            shot_ord=gr[0]["shot_ord"], gap_start_ms=gr[0]["gap_start_ms"],
            gap_end_ms=gr[0]["gap_end_ms"], decision="intentional"))

    before = m.get_project(pid2)
    doc_before = m.export(pid2, fmt="json").body
    srt_before = m.export(pid2, fmt="srt").body
    with zipfile.ZipFile(io.BytesIO(m.export(pid2, fmt="zip").body)) as zf:
        zip_before = {n: zf.read(n) for n in zf.namelist()}

    # 各种核验（通过 / 不符 / 缺失新增 / 待确认）都不应落库
    _verify(pid2, "1\n00:00:00,000 --> 00:00:01,000\n甲\n")
    _verify(pid2, "9\n00:00:05,000 --> 00:00:06,000\n陌生文案\n")
    _verify(pid2, "1\n00:00:00,500 --> 00:00:01,500\n同\n\n"
                  "2\n00:00:01,500 --> 00:00:02,500\n同\n")

    after = m.get_project(pid2)
    assert after == before
    assert m.export(pid2, fmt="json").body == doc_before
    assert m.export(pid2, fmt="srt").body == srt_before
    with zipfile.ZipFile(io.BytesIO(m.export(pid2, fmt="zip").body)) as zf:
        assert {n: zf.read(n) for n in zf.namelist()} == zip_before
    print("e2e verification does not touch adoption/gaps/exports ok")


def main():
    test_unit_all_pass_with_renumber_and_duplicate_text()
    test_unit_time_mismatch_only_when_exact_ms()
    test_unit_missing_new_and_split_not_merged()
    test_unit_tie_between_missing_and_mismatch_is_pending()
    test_unit_tie_between_new_and_mismatch_is_pending()
    test_unit_not_greedy_order_preserving()
    pid = test_e2e_pass_renumbered_vtt()
    test_e2e_mismatch_missing_new()
    test_e2e_effective_timeline_adopted_first(pid)
    test_e2e_empty_and_parse_errors()
    test_e2e_timeline_hash_binding()
    test_e2e_verification_changes_nothing()
    print("delivery verify regression ok")


if __name__ == "__main__":
    main()
