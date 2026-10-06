"""交付字幕核验：回传稿与项目生效时间轴的保序最优对应。

回传的交付稿（SRT / WebVTT，复用 ``subtitles.parse_subtitle`` 解析）可能被
合作方**重编号、插删行、重复文案、拆并行**，因此不能按 cue_id 或行号配对。
在「生效时间轴条目 → 交付稿条目」两条保序序列之间求**一一对应**
（不交叉、不并条；拆并行不会被自动合并），择优按字典序：

  1. **同文案匹配对数最多**（可见文本经同一套去标签/空白折叠归一化）；
  2. 并列时，匹配对的起止时间偏差绝对值之和最小
     —— Σ (|Δstart_ms| + |Δend_ms|)。

结论：

- 匹配对的起止毫秒**完全相同** → ``pass``（通过）；否则 ``time_mismatch``；
- 生效轴上未匹配的条目 → ``missing``（缺失）；交付稿未匹配条目 → ``new``
  （新增）；
- 若存在多个**等优对应**（同文案对数与偏差和都相同），且某行在不同等优
  对应下的结论不同，该行记 ``pending``（待确认），不强判缺失 / 时间不符 /
  新增。

实现为前向 + 后向两张序列对齐 DP（LCS 式三种转移：配 / 跳生效条目 /
跳交付条目），值为 ``(同文案对数, -偏差和)`` 字典序最大；再用两张表判定
每条匹配边、每个“跳过”动作是否能出现在某条最优路径上，从而得到各行结论
在全部等优对应下是否稳定。O(n·m) 时间/空间。
"""
from __future__ import annotations

from dataclasses import dataclass

PASS = "pass"                    # 起止毫秒均相同
TIME_MISMATCH = "time_mismatch"  # 匹配但起止毫秒有差异
MISSING = "missing"              # 生效轴有、交付稿没有
NEW = "new"                      # 交付稿有、生效轴没有
PENDING = "pending"              # 等优对应会改变该行结论

VERDICTS = (PASS, TIME_MISMATCH, MISSING, NEW, PENDING)


@dataclass(frozen=True)
class TimelineLine:
    """核验序列中的一行；交付稿行 cue_id 为 None。"""
    ord: int
    text: str
    start_ms: int
    end_ms: int
    cue_id: str | None = None


def _add(a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int]:
    return (a[0] + b[0], a[1] + b[1])


def verify_delivery(
    refs: list[TimelineLine],
    delivered: list[TimelineLine],
) -> dict:
    """对生效时间轴 refs 与交付稿 delivered 做保序核验。

    返回 {reference_count, delivered_count, summary, rows}；rows 按
    “生效轴顺序优先、交付稿顺序随后”的一条规范最优路径排列（能配则配），
    每行恰好一个结论：match / ref_only / del_only。
    """
    n, m = len(refs), len(delivered)

    def same(i: int, j: int) -> bool:
        return refs[i].text == delivered[j].text

    def dev(i: int, j: int) -> int:
        return (abs(refs[i].start_ms - delivered[j].start_ms)
                + abs(refs[i].end_ms - delivered[j].end_ms))

    def edge_val(i: int, j: int) -> tuple[int, int]:
        return (1 if same(i, j) else 0, -dev(i, j))

    # ---------- 前向 DP：f[i][j] = refs[:i] × delivered[:j] 的最优值 ----------
    f = [[None] * (m + 1) for _ in range(n + 1)]
    f[0][0] = (0, 0)
    for i in range(n + 1):
        for j in range(m + 1):
            if i == 0 and j == 0:
                continue
            best = None
            if i:  # 生效条目 i-1 不配对（缺失）
                best = f[i - 1][j]
            if j:  # 交付条目 j-1 不配对（新增）
                cand = f[i][j - 1]
                if best is None or cand > best:
                    best = cand
            if i and j:  # 配对 (i-1, j-1)
                sc, nd = f[i - 1][j - 1]
                k, dv = edge_val(i - 1, j - 1)
                cand = (sc + k, nd + dv)
                if best is None or cand > best:
                    best = cand
            f[i][j] = best

    # ---------- 后向 DP：b[i][j] = refs[i:] × delivered[j:] 的最优值 ----------
    b = [[None] * (m + 1) for _ in range(n + 1)]
    b[n][m] = (0, 0)
    for i in range(n, -1, -1):
        for j in range(m, -1, -1):
            if i == n and j == m:
                continue
            best = None
            if i < n:
                best = b[i + 1][j]
            if j < m:
                cand = b[i][j + 1]
                if best is None or cand > best:
                    best = cand
            if i < n and j < m:
                sc, nd = b[i + 1][j + 1]
                k, dv = edge_val(i, j)
                cand = (sc + k, nd + dv)
                if best is None or cand > best:
                    best = cand
            b[i][j] = best

    opt = f[n][m]

    # 边 (i,j) 是否落在某条最优路径上 → 记录其偏差；同时收集各端点的候选边
    partners_r: list[set[int]] = [set() for _ in range(n)]
    partners_d: list[set[int]] = [set() for _ in range(m)]
    edge_dev: dict[tuple[int, int], int] = {}
    for i in range(n):
        for j in range(m):
            if _add(_add(f[i][j], edge_val(i, j)), b[i + 1][j + 1]) == opt:
                partners_r[i].add(j)
                partners_d[j].add(i)
                edge_dev[(i, j)] = dev(i, j)

    # 生效条目 i 可在某条最优路径上被跳过（结论缺失）
    r_skippable = [
        any(_add(f[i][j], b[i + 1][j]) == opt for j in range(m + 1))
        for i in range(n)
    ]
    # 交付条目 j 可在某条最优路径上被跳过（结论新增）
    d_skippable = [
        any(_add(f[i][j], b[i][j + 1]) == opt for i in range(n + 1))
        for j in range(m)
    ]

    def _edge_verdict(i: int, j: int) -> str:
        return PASS if edge_dev[(i, j)] == 0 else TIME_MISMATCH

    def _stable_verdict(i: int, skippable: bool, partners: set[int],
                        side: str) -> str:
        """该行在所有等优对应下结论是否稳定。"""
        if skippable:
            # 既可配对又可缺失/新增 → 结论必变；只能跳过时就是缺失/新增
            return PENDING if partners else (MISSING if side == "r" else NEW)
        verdicts = {_edge_verdict(i, j) if side == "r"
                    else _edge_verdict(j, i) for j in partners}
        return next(iter(verdicts)) if len(verdicts) == 1 else PENDING

    r_verdict = [
        _stable_verdict(i, r_skippable[i], partners_r[i], "r")
        for i in range(n)
    ]
    d_verdict = [
        _stable_verdict(j, d_skippable[j], partners_d[j], "d")
        for j in range(m)
    ]

    def _ref_payload(i: int) -> dict:
        r = refs[i]
        return {"ref_ord": r.ord, "cue_id": r.cue_id, "text": r.text,
                "ref_start_ms": r.start_ms, "ref_end_ms": r.end_ms}

    def _del_payload(j: int) -> dict:
        d = delivered[j]
        return {"del_ord": d.ord, "delivered_text": d.text,
                "del_start_ms": d.start_ms, "del_end_ms": d.end_ms}

    def _best_ref_candidate(j: int) -> dict:
        i = min(partners_d[j], key=lambda x: (dev(x, j), x))
        return _ref_payload(i)

    def _best_del_candidate(i: int) -> dict:
        j = min(partners_r[i], key=lambda x: (dev(i, x), x))
        return _del_payload(j)

    # ---------- 沿一条规范最优路径生成逐行结论（能配优先于留白） ----------
    rows: list[dict] = []
    i = j = 0
    while i < n or j < m:
        if i < n and j < m and (i, j) in edge_dev:
            rv, dv = r_verdict[i], d_verdict[j]
            verdict = PENDING if (PENDING in (rv, dv) or rv != dv) else rv
            row = {"kind": "match", "verdict": verdict,
                   "text_same": same(i, j),
                   "delta_start_ms": delivered[j].start_ms - refs[i].start_ms,
                   "delta_end_ms": delivered[j].end_ms - refs[i].end_ms}
            row.update(_ref_payload(i))
            row.update(_del_payload(j))
            rows.append(row)
            i += 1
            j += 1
        elif i < n and _add(f[i][j], b[i + 1][j]) == opt:
            row = {"kind": "ref_only", "verdict": r_verdict[i]}
            row.update(_ref_payload(i))
            # 等优对应里存在可配交付行（该行因此待确认）：给出偏差最小的
            # 候选，便于人工确认，不自动下结论。
            row["candidate"] = (_best_del_candidate(i)
                                if partners_r[i] else None)
            rows.append(row)
            i += 1
        else:
            row = {"kind": "del_only", "verdict": d_verdict[j]}
            row.update(_del_payload(j))
            row["candidate"] = (_best_ref_candidate(j)
                                if partners_d[j] else None)
            rows.append(row)
            j += 1

    summary = {v: 0 for v in VERDICTS}
    for row in rows:
        summary[row["verdict"]] += 1
    return {
        "reference_count": n,
        "delivered_count": m,
        "matched_text_count": opt[0],
        "summary": summary,
        "rows": rows,
    }
