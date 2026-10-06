"""镜头留白（未被字幕覆盖的区间）计算与审阅项身份。

留白定义：对每个镜头 [shot_start, shot_end)，把生效时间轴上的字幕区间
裁剪到镜头内，合并重叠或相接（端点相贴）的覆盖段后取补集，得到的半开
毫秒区间 [gap_start, gap_end) 即为一条留白审阅项。相接或重叠的镜头各自
独立计算、分别审阅（不合并镜头）；镜头内完全没有任何字幕覆盖时，整段
[shot_start, shot_end) 自然成为唯一一条留白项。

生效时间轴与 SRT 导出共用同一条规则：已采纳取采纳值，未采纳先取提案，
无提案再取原值（由调用方逐条算好后传入）。

审阅项身份 = (shot_ord, shot_start, shot_end, gap_start, gap_end)。
镜头在导入后不可变，因此同一镜头内边界未变的留白在采纳/撤销/参数重算后
仍匹配同一条已存结论（保留）；边界发生变化的区间匹配不到旧结论，回到
待审。过期页面按旧边界提交的结论在服务端写锁内重算校验，匹配不到当前
留白即拒绝，不会误标到变化后的新留白上。
"""
from __future__ import annotations

from dataclasses import dataclass

from .shots import Shot

# 审阅结论取值
INTENTIONAL = "intentional"        # 有意留白
NEEDS_SUBTITLE = "needs_subtitle"  # 待补字幕
DECISIONS = (INTENTIONAL, NEEDS_SUBTITLE)


@dataclass
class GapItem:
    shot_ord: int
    shot_start_ms: int
    shot_end_ms: int
    gap_start_ms: int
    gap_end_ms: int
    decision: str | None = None    # None=待审；intentional / needs_subtitle


def _merge_covered(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠或相接（前段终点 == 后段起点）的半开区间。"""
    merged: list[list[int]] = []
    for s, e in sorted(intervals):
        if s >= e:
            continue
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def compute_shot_gaps(
    shots: list[Shot],
    effective: list[tuple[int, int]],
) -> list[GapItem]:
    """逐镜头计算未被字幕覆盖的半开留白区间（镜头之间不合并）。

    shots：按 ord 排序的镜头；effective：生效时间轴上各 cue 的
    (start_ms, end_ms)（顺序无关，裁剪后统一排序合并）。
    """
    items: list[GapItem] = []
    for shot in shots:
        s0, e0 = shot.start_ms, shot.end_ms
        clipped = [
            (max(cs, s0), min(ce, e0)) for cs, ce in effective
            if max(cs, s0) < min(ce, e0)
        ]
        covered = _merge_covered(clipped)
        gaps: list[tuple[int, int]] = []
        cur = s0
        for cs, ce in covered:
            if cur < cs:
                gaps.append((cur, cs))
            cur = max(cur, ce)
        if cur < e0:
            gaps.append((cur, e0))
        # 完全无覆盖时上面的循环自然得到整段 [s0, e0) 一条
        for gs, ge in gaps:
            items.append(GapItem(shot.ord, s0, e0, gs, ge))
    return items
