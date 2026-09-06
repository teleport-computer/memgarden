"""历史导入：把一大批过去的材料分批蒸成记忆卡。

## 为什么不能一口气喂进去

用户交出来的可能是三年的聊天记录。一次全塞给模型有三个坏法，且都不报错：

    上下文撑爆     → 后半段被静默截断，用户看到「导入成功」，其实只读了开头
    产出几百张卡   → 之后每一轮召回都被这批淹没，而用户看不出发生了什么
    中途失败       → 前面蒸好的白费，重来一遍还要再烧一次模型钱

所以分批 + 游标 + 幂等，三样缺一不可：

    分批   每批控制在模型吃得下的量
    游标   中断之后从下一批接着跑，不是从头
    幂等   每批一个稳定的键 —— 崩溃后重放同一批不会写出第二份

## 跨批去重靠什么

不靠「记住上一批写了什么」（那需要额外状态，而状态会和库不一致）。
靠的是**每批都重新读一次库**：第 N 批做判断时，前 N-1 批写进去的卡就在
它的「已有记忆索引」里，于是模型自己会选 merge 而不是 add。

这也是为什么分批必须**串行**：并行跑的话每批看到的都是导入前的旧状态，
同一件事在不同批里各写一张，谁也不知道。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass
class ImportProgress:
    """一次导入跑到哪了。**宿主要把它存起来**，断点续跑靠它。

    ``cursor`` 是下一批的起点（字符偏移）。等于 ``total`` 就是跑完了。
    """

    cursor: int = 0
    total: int = 0
    batches_done: int = 0
    cards_written: int = 0
    #: 被跳过的批次和原因。**空结果不算失败** —— 某一批确实没什么可记是正常的。
    skipped: list[dict] = field(default_factory=list)
    #: 硬失败的批次。非空时 ``done`` 为 False，宿主应当重试或告诉用户。
    failed: list[dict] = field(default_factory=list)
    schema_version: int = 1

    @property
    def done(self) -> bool:
        return self.cursor >= self.total and not self.failed

    @property
    def percent(self) -> int:
        return 100 if not self.total else min(100, self.cursor * 100 // self.total)


def split_material(material: str, *, batch_chars: int) -> list[tuple[int, str]]:
    """把材料切成批，返回 ``(起点偏移, 这一批的文本)``。

    切在**换行**上，不切在字符中间：把一句话劈成两半送给模型，两边都读不懂
    那句话，而模型不会说「我这里少了半句」—— 它会照着残句编一个意思出来。

    找不到换行时（比如一整段没有断行）就硬切：宁可切坏一句，
    也不要让一批无限长把上下文撑爆。
    """
    text = material or ""
    if not text:
        return []
    out: list[tuple[int, str]] = []
    start = 0
    size = max(200, int(batch_chars))
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            nl = text.rfind("\n", start + size // 2, end)
            if nl > start:
                end = nl + 1
        chunk = text[start:end]
        if chunk.strip():
            out.append((start, chunk))
        start = end
    return out


def batch_key(base: str, *, offset: int, chunk: str) -> str:
    """一批的幂等键。

    含**内容摘要**而不只是序号：用户改了材料重新导入时，同一个序号对应的
    已经是另一段内容了 —— 只用序号的话第二次导入会被当成第一次的重放，
    整批静默跳过，用户看到「导入成功」而什么都没进去。
    """
    digest = hashlib.sha256(chunk.encode("utf-8")).hexdigest()[:16]
    return f"{base}:b{offset}:{digest}"
