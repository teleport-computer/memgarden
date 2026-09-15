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

## 两种接法，一个状态机

    MountedGarden.import_history   内核调模型、内核写 Store
    GardenComponent.import_session 宿主调模型、宿主写库（io 这类自带 provider、
                                   加密和执行器的宿主）

两者都走 :class:`ImportSession`：切批、续传校验、提示词、解析、重问、跨批去重、
张数上限、进度推进是**同一份代码**。各写一份的话，两条路会悄悄产出不同的卡。

宿主驱动的用法::

    session = garden.import_session(request, progress=saved, existing_cards=cards)
    while (batch := session.next_batch()) is not None:
        while (prompt := batch.next_prompt()) is not None:
            reply, truncated = my_provider(prompt)
            batch.feed(reply, truncated=truncated)
        outcome = batch.result()
        if outcome.error:
            save(session.commit(outcome))      # 记下失败，游标不动
            break
        ids = my_write(outcome.mutations, idempotency_key=outcome.idempotency_key)
        save(session.commit(outcome, record_ids=ids))

## 跨批去重靠什么

不靠「记住上一批写了什么」的额外状态（那会和库不一致），靠的是**每批写卡时
都能看见已有的卡**：第 N 批写卡时，前 N-1 批写进去的卡在它的「已有记忆索引」里，
模型于是会选 merge 而不是 add。

    MountedGarden   每批重新读一次库
    宿主驱动        宿主开会话时给一次 existing_cards；之后每次 commit 时会话把
                    刚写进去的卡（带宿主给的真实 id）登记进索引。进程重启后宿主
                    重新读库再开会话，写过的卡自然都在里面。

索引不再按重要度取前 60：大导入里后面的批次会看不到前面写的卡（它们的重要度
不一定排得进前 60），只能又写一张。改成「按这一批的文字挑相关的旧卡」，
再留四分之一名额给最重要的卡。

这也是为什么分批必须**串行**：并行跑的话每批看到的都是导入前的旧状态，
同一件事在不同批里各写一张，谁也不知道。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

#: 按 ``material`` 切批时一批多少字。够模型一次读完，也够小到中断时不心疼。
IMPORT_BATCH_CHARS = 6000

#: 两段式的候选总数硬上限。候选存在进度对象里（宿主要持久化），不设上限的话
#: 一份几百万字的材料会让进度对象膨胀到几 MB，每批都要整份重写一遍。
#: 超出的候选丢弃并记进 ``skipped``，不静默。
MAX_CANDIDATES = 4000

STRATEGIES = ("single_pass", "two_pass")


@dataclass
class ImportProgress:
    """一次导入跑到哪了。**宿主要把它存起来**，断点续跑靠它。

    ``cursor`` 是下一批的起点（字符偏移；预切批次时是各批文字长度的累计）。
    等于 ``total`` 就是材料读完了。

    ⚠️ 两段式（``strategy="two_pass"``）的 ``candidates`` **含用户内容**（候选事实
    和原话证据）。宿主要按记忆正文的等级保存这个对象（加密、同样的保留期）。
    单段式的进度只有偏移、计数和摘要，不含内容。
    """

    cursor: int = 0
    total: int = 0
    #: 整份输入材料的摘要。断点只能用于同一份材料；否则旧 cursor 会让新材料
    #: 的开头被静默跳过。
    source_digest: str = ""
    #: 材料 + scope + mount + locale + policy + 名称 + 幂等前缀 + 批次规则的
    #: 摘要。相同正文换一套导入语义也不能沿用旧 cursor。
    import_fingerprint: str = ""
    batches_done: int = 0
    cards_written: int = 0
    #: 被跳过的批次和原因。**空结果不算失败** —— 某一批确实没什么可记是正常的。
    skipped: list[dict] = field(default_factory=list)
    #: 硬失败的批次。非空时 ``done`` 为 False，宿主应当重试或告诉用户。
    failed: list[dict] = field(default_factory=list)
    schema_version: int = 1
    #: ``single_pass`` / ``two_pass``。
    strategy: str = "single_pass"
    #: 计入 ``max_total_cards`` 的写卡数（add + supersede）。
    cards_added: int = 0
    #: 两段式抽出的候选（**含用户内容**），写卡阶段按 ``candidates_cursor`` 消费。
    candidates: list[dict] = field(default_factory=list)
    candidates_cursor: int = 0

    @property
    def done(self) -> bool:
        return (self.cursor >= self.total
                and self.candidates_cursor >= len(self.candidates)
                and not self.failed)

    @property
    def percent(self) -> int:
        if self.strategy != "two_pass":
            return 100 if not self.total else min(100, self.cursor * 100 // self.total)
        # 两段式：读材料占 70%，写卡占 30%。写卡阶段有多少批要等读完才知道。
        read = 100 if not self.total else min(100, self.cursor * 100 // self.total)
        if read < 100:
            return read * 70 // 100
        if not self.candidates:
            return 100
        write = min(100, self.candidates_cursor * 100 // len(self.candidates))
        return 70 + write * 30 // 100


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


# --------------------------------------------------------------------------- #
# 已有记忆索引：按这一批的文字挑相关的旧卡
# --------------------------------------------------------------------------- #

_ASCII_WORD = re.compile(r"[a-z0-9][a-z0-9_+#.-]*")


def _is_cjk(ch: str) -> bool:
    return "㐀" <= ch <= "鿿" or "豈" <= ch <= "﫿"


def _index_tokens(text: str) -> set[str]:
    """ASCII 词 + 汉字二元组。只用来挑候选旧卡，不是检索排序器。"""
    lowered = str(text or "").casefold()
    tokens = {w for w in _ASCII_WORD.findall(lowered) if len(w) >= 2}
    run: list[str] = []
    for ch in lowered + " ":
        if _is_cjk(ch):
            run.append(ch)
            continue
        if len(run) == 1:
            tokens.add(run[0])
        else:
            tokens.update(run[i] + run[i + 1] for i in range(len(run) - 1))
        run = []
    return tokens


def _importance(card: Mapping) -> float:
    try:
        return float(card.get("importance") or 0)
    except (TypeError, ValueError):
        return 0.0


def _card_index_text(card: Mapping) -> str:
    threads = card.get("threads") if isinstance(card.get("threads"), list) else []
    cues = card.get("retrieval_cues") if isinstance(card.get("retrieval_cues"), list) else []
    return " ".join([
        str(card.get("summary") or ""), str(card.get("content") or ""),
        str(card.get("bucket") or ""),
        " ".join(str(t) for t in threads), " ".join(str(c) for c in cues),
    ])


#: 宿主可注入的挑卡器：``ranker(batch_text, cards) -> 按相关性排好的卡 id``。
#: 有更好的检索（BM25、向量）的宿主传进来；不传用内置的词面重叠。
IndexRanker = Callable[[str, Sequence[Mapping]], Sequence[str]]


def select_index_cards(
    cards: Sequence[Mapping], text: str, *, limit: int = 60,
    ranker: IndexRanker | None = None,
) -> list[dict]:
    """给这一批材料挑进「已有记忆索引」的旧卡。

    卡不多于 ``limit`` 时全给。多了就按和这一批文字的相关性挑，**留四分之一名额
    给重要度最高的卡** —— 核心事实（名字、关系、边界）常常和某一批的字面不重合，
    但模型写卡时仍需要知道它们已经在了。
    """
    usable = [dict(c) for c in cards
              if str(c.get("id") or "").strip() and str(c.get("summary") or "").strip()]
    limit = max(0, int(limit))
    if len(usable) <= limit:
        return usable
    if not limit:
        return []
    by_importance = sorted(usable, key=lambda c: (-_importance(c), str(c.get("id"))))
    budget = limit - limit // 4
    picked: list[dict] = []
    if ranker is not None:
        by_id = {str(c["id"]): c for c in usable}
        seen: set[str] = set()
        for rid in ranker(text, usable):
            rid = str(rid)
            if rid in by_id and rid not in seen:
                seen.add(rid)
                picked.append(by_id[rid])
            if len(picked) >= budget:
                break
    else:
        query = _index_tokens(text)
        scored = []
        for card in usable:
            tokens = _index_tokens(_card_index_text(card))
            overlap = len(query & tokens)
            if overlap:
                scored.append((overlap / math.sqrt(len(tokens)), card))
        scored.sort(key=lambda sc: (-sc[0], -_importance(sc[1]), str(sc[1].get("id"))))
        picked = [card for _score, card in scored[:budget]]
    taken = {str(c["id"]) for c in picked}
    for card in by_importance:
        if len(picked) >= limit:
            break
        if str(card["id"]) not in taken:
            picked.append(card)
            taken.add(str(card["id"]))
    return picked


def converge_bucket(bucket: str, known_buckets: Sequence[str], *, locale: str = "") -> str:
    """把桶名收敛到已有的写法上。

    模型按提示词复用已有桶名，但偶尔会写成 ``work`` / ``Work `` / ``健康/Health``。
    一批一批导入时，这种小差别会裂成两个桶，检索时互相看不见 —— 而且每一步都
    「成功」。这里只做**确定性**的收敛：大小写/空白一致的并到已有写法；
    ``中文/English`` 这种双语串按花园语言取一半。近义词（「工作」vs「职业」）
    不在这里猜。
    """
    from .prompts.buckets import COMMON_BUCKETS_V1

    name = str(bucket or "").strip()
    if not name:
        return name
    common = {zh for zh, _en in COMMON_BUCKETS_V1} | {en for _zh, en in COMMON_BUCKETS_V1}
    listed = [p.strip() for p in re.split(r"[、，,]", name) if p.strip()]
    if len(listed) > 1 and all(p in common for p in listed):
        # 真模型实测（2026-09-15，DeepSeek）：提示词里通用桶清单是「工作、目标与成长、…」
        # 一行，模型会把前两个连着抄成一个桶名「工作、目标与成长」。只在每一段都是
        # 通用桶时取第一个；自定义桶名里带顿号的不动。
        name = listed[0]
    if "/" in name:
        parts = [p.strip() for p in name.split("/") if p.strip()]
        pairs = {(zh, en) for zh, en in COMMON_BUCKETS_V1}
        if len(parts) == 2:
            a, b = parts
            pair = (a, b) if (a, b) in pairs else (b, a) if (b, a) in pairs else None
            if pair:
                name = pair[1] if str(locale).strip() == "en" else pair[0]
    folded = " ".join(name.split()).casefold()
    for existing in known_buckets:
        clean = str(existing or "").strip()
        if clean and " ".join(clean.split()).casefold() == folded:
            return clean
    return name


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #

@dataclass
class ImportBatchResult:
    """一批跑完的结果。**还没写库** —— 宿主写完再交给 :meth:`ImportSession.commit`。"""

    #: ``cards``（单段式写卡）/ ``candidates``（两段式抽候选）/ ``write``（两段式写卡）
    stage: str
    #: 这一批在材料里的起点（``write`` 阶段是候选下标）。commit 用它确认没交错批。
    offset: int
    end: int
    #: 写库用的幂等键。同一批重放必须用同一个键。
    idempotency_key: str
    mutations: list[dict] = field(default_factory=list)
    cards: list[dict] = field(default_factory=list)
    #: 两段式第一段的产出（含用户内容）。
    candidates: list[dict] = field(default_factory=list)
    #: 非 None = 这批失败了，游标不能动。
    error: str | None = None
    retried: int = 0
    #: 内容无关的观测量。
    trace: dict = field(default_factory=dict)

    @property
    def nothing_worth_keeping(self) -> bool:
        return self.error is None and not self.mutations and not self.candidates


class _CandidatePlan:
    """两段式第一段的状态机。和落卡同构：问 → 喂 → 必要时重问一次 → 取结果。"""

    def __init__(self, owner: Any, prompt: str, *, locale: str) -> None:
        self.owner = owner
        self.prompt = prompt
        self.rejected: str | None = None if str(locale or "").strip() else "locale_required"
        self.candidates: list[dict] = []
        self.err: str | None = None
        self.retried = 0
        self.calls = 0
        self._stage = "first"

    def next_prompt(self) -> str | None:
        from .contracts import Step
        from .prompts.history_import import build_import_candidates_retry_prompt
        from .text.card_text import build_truncation_retry_prompt

        if self.rejected is not None:
            return None
        if self._stage == "first":
            self.owner._step(Step(kind="prompt_built", purpose="import_candidates",
                                  attempt=0, detail={"prompt_chars": len(self.prompt)},
                                  prompt=self.prompt))
            return self.prompt
        if self._stage == "format_retry":
            return build_import_candidates_retry_prompt(self.prompt, self.err or "")
        if self._stage == "truncation_retry":
            return build_truncation_retry_prompt(self.prompt)
        return None

    def feed(self, raw: Any, *, truncated: bool = False) -> None:
        from .component import _reply_text
        from .contracts import Step
        from .prompts.history_import import parse_import_candidates
        from .text.card_text import is_retryable_parse_error

        self.calls += 1
        text = _reply_text(raw)
        self.owner._step(Step(kind="model_called", purpose="import_candidates",
                              attempt=self.calls,
                              detail={"reply_chars": len(text), "stage": self._stage,
                                      "truncated": truncated}, reply=text))
        if (truncated and self._stage != "truncation_retry"
                and self.retried < self.owner._max_retries):
            self._stage = "truncation_retry"
            self.retried += 1
            return
        strict = self._stage == "first"
        cands, err = parse_import_candidates(text, strict=strict, signals=self.owner._signals)
        if self._stage == "format_retry":
            self.retried += 1
        self.candidates, self.err = cands, err
        self.owner._step(Step(kind="parsed", purpose="import_candidates",
                              attempt=self.calls,
                              detail={"candidates": len(cands), "error": err}))
        if err and is_retryable_parse_error(err, raw=text) and self.retried < self.owner._max_retries:
            self._stage = "format_retry"
            return
        self._stage = "done"


class ImportBatch:
    """会话发出来的一批。用法和 :class:`memgarden.CaptureSession` 一样。"""

    def __init__(self, session: "ImportSession", *, stage: str, offset: int, end: int,
                 idempotency_key: str, request: Any = None, prompt: str = "") -> None:
        self._session = session
        self.stage = stage
        self.offset = offset
        self.end = end
        self.idempotency_key = idempotency_key
        #: 写卡批次对应的 ``CaptureRequest``（候选批次为 None）。
        self.request = request
        if stage == "candidates":
            self._plan: Any = _CandidatePlan(session._owner, prompt,
                                             locale=session.request.locale)
        else:
            from .component import _CapturePlan

            self._plan = _CapturePlan(session._owner, request)

    def next_prompt(self) -> str | None:
        if self._plan.rejected is not None:
            return None
        return self._plan.next_prompt()

    def feed(self, reply: Any, *, truncated: bool = False) -> None:
        self._plan.feed(reply, truncated=truncated)

    def result(self) -> ImportBatchResult:
        base = dict(stage=self.stage, offset=self.offset, end=self.end,
                    idempotency_key=self.idempotency_key)
        if self.stage == "candidates":
            plan = self._plan
            if plan.rejected is not None:
                return ImportBatchResult(**base, error=plan.rejected)
            trace = {"model_calls": plan.calls, "candidates": len(plan.candidates)}
            if plan.err:
                return ImportBatchResult(**base, error=plan.err, retried=plan.retried,
                                         trace=trace)
            return ImportBatchResult(**base, candidates=list(plan.candidates),
                                     retried=plan.retried, trace=trace)
        captured = (self._plan.rejected if self._plan.rejected is not None
                    else self._plan.finish())
        if captured.error:
            return ImportBatchResult(**base, error=captured.error,
                                     retried=captured.retried, trace=dict(captured.trace))
        cards, mutations = self._session._finish_cards(
            list(captured.cards), list(captured.mutations))
        trace = {**dict(captured.trace), "stage": self.stage}
        return ImportBatchResult(**base, mutations=mutations, cards=cards,
                                 retried=captured.retried, trace=trace)


class ImportSession:
    """一次历史导入。**同一个对象可以跨很多批**，进度随 :meth:`commit` 推进。

    不要自己构造；用 :meth:`memgarden.GardenComponent.import_session`。
    """

    def __init__(
        self,
        owner: Any,
        request: Any,
        *,
        progress: ImportProgress | None = None,
        existing_cards: Sequence[Mapping] | None = None,
        binding: tuple[str, str] = ("", ""),
        ranker: IndexRanker | None = None,
        index_limit: int = 60,
    ) -> None:
        from .policies import get_policy
        from .timestamps import normalize

        self._owner = owner
        self.request = request
        self._ranker = ranker
        self._index_limit = index_limit
        strategy = str(getattr(request, "strategy", "") or "single_pass").strip()
        if strategy not in STRATEGIES:
            raise ValueError(f"未知的导入 strategy {strategy!r}；可用：{', '.join(STRATEGIES)}")
        self.strategy = strategy
        self.policy = getattr(request, "policy", None) or "history_import"
        get_policy(self.policy)  # 拼错的档位当场炸，不要等到第一批。
        fallback = str(getattr(request, "fallback_occurred_at", "") or "").strip()
        self._fallback_date = normalize(fallback) if fallback else ""
        if fallback and not self._fallback_date:
            raise ValueError("fallback_occurred_at 不是合法的 ISO 日期")
        max_total = getattr(request, "max_total_cards", None)
        if max_total is not None and int(max_total) < 0:
            raise ValueError("max_total_cards 不能为负")
        self._max_total = None if max_total is None else int(max_total)
        self._batch_card_limit = max(1, int(getattr(request, "max_cards", 50) or 50))
        self._batch_chars = int(getattr(request, "batch_chars", None) or IMPORT_BATCH_CHARS)
        if self._batch_chars < 200:
            raise ValueError("batch_chars 至少为 200")
        self._write_group = int(getattr(request, "write_batch_candidates", 40) or 40)
        if self._write_group < 1:
            raise ValueError("write_batch_candidates 至少为 1")

        self._batches, total, source_digest = self._plan_batches()
        fingerprint = self._fingerprint(binding, source_digest)
        prog = progress or ImportProgress(total=total, source_digest=source_digest,
                                          import_fingerprint=fingerprint,
                                          strategy=strategy)
        _check_resume(prog, total=total, source_digest=source_digest,
                      fingerprint=fingerprint)
        valid = {0, total}
        valid.update(off + len(chunk) for off, chunk, _hint in self._batches)
        if prog.cursor not in valid:
            raise ValueError(
                "history import progress.cursor 不是合法批次边界；请使用服务"
                "上次原样返回的 progress")
        if prog.candidates_cursor < 0 or prog.candidates_cursor > len(prog.candidates):
            raise ValueError("history import progress.candidates_cursor 超出范围")
        prog.source_digest = source_digest
        prog.import_fingerprint = fingerprint
        prog.total = total
        prog.strategy = strategy
        self.progress = prog

        key_prefix = str(getattr(request, "idempotency_key", "") or "import")
        self._base_key = f"{key_prefix}:{fingerprint[:16]}"
        self._known: dict[str, dict] = {}
        self._set_known(existing_cards or ())

        # 全空白输入不会生成 batch，但它已经被完整消费；cursor 必须走到末尾，
        # 否则 progress.done 永远为 False，宿主会无限重试。
        if not self._batches:
            if total and not prog.skipped:
                prog.skipped.append({"offset": 0, "reason": "blank_material"})
            prog.failed.clear()
            prog.cursor = total

    # -- 对外 ----------------------------------------------------------- #

    @property
    def done(self) -> bool:
        return self.progress.done

    def next_batch(self, *, existing_cards: Sequence[Mapping] | None = None) -> ImportBatch | None:
        """游标处的下一批；``None`` = 没有可跑的了（跑完，或整次上限已满）。

        没 commit 之前反复调用，拿到的是**同一批**（重新生成提示词）——
        解析失败、写库冲突后重试就这么做。``existing_cards`` 给了就替换索引
        （宿主重新读过库时传进来）。
        """
        if existing_cards is not None:
            self._set_known(existing_cards)
        prog = self.progress
        self._consume_trailing()
        if (self._max_total is not None and prog.cards_added >= self._max_total
                and not self._fully_consumed()):
            # 整次导入的上限满了：剩下的批次不再调模型，如实记下来。
            prog.skipped.append({"offset": prog.cursor, "reason": "max_total_cards",
                                 "cap": self._max_total})
            prog.cursor = prog.total
            prog.candidates_cursor = len(prog.candidates)
            return None
        pending = self._pending_material_batch()
        if pending is not None:
            offset, chunk, hint = pending
            if self.strategy == "single_pass":
                window = f"[{hint}]\n{chunk}" if hint else chunk
                request = self._capture_request(
                    window, batch_key(self._base_key, offset=offset, chunk=chunk))
                return ImportBatch(self, stage="cards", offset=offset,
                                   end=offset + len(chunk),
                                   idempotency_key=request.idempotency_key,
                                   request=request)
            return ImportBatch(
                self, stage="candidates", offset=offset, end=offset + len(chunk),
                idempotency_key=f"{self._base_key}:m{offset}:{_digest(chunk)}",
                prompt=self._candidates_prompt(chunk, hint))
        start = prog.candidates_cursor
        if start < len(prog.candidates):
            from .prompts.history_import import render_candidate_digest

            group = prog.candidates[start:start + self._write_group]
            digest_text = render_candidate_digest(group)
            request = self._capture_request(
                digest_text, f"{self._base_key}:w{start}:{_digest(digest_text)}")
            return ImportBatch(self, stage="write", offset=start, end=start + len(group),
                               idempotency_key=request.idempotency_key, request=request)
        return None

    def commit(self, outcome: ImportBatchResult, *,
               record_ids: Sequence[str] = ()) -> ImportProgress:
        """宿主写完这一批之后调用。返回（同一个）进度对象，宿主把它存起来。

        ``outcome.error`` 非空时只记失败，游标不动。写卡批次的 ``record_ids``
        必须和 ``mutations`` 一一对应（宿主写库后拿到的真实 id）—— 会话要把
        刚写进去的卡登记进后面批次的索引，id 对不上就等于让模型去 merge 一张
        宿主库里不存在的卡。
        """
        ids = [str(x or "") for x in record_ids]
        if outcome.stage != "candidates" and not outcome.error and len(ids) != len(outcome.mutations):
            raise ValueError(
                f"record_ids 数量 ({len(ids)}) 和 mutations 数量 "
                f"({len(outcome.mutations)}) 不一致")
        return self._advance(outcome, ids, register=True)

    def fail(self, outcome: ImportBatchResult, error: str) -> ImportProgress:
        """判断成功但宿主写库失败：记成这一批失败，游标不动。"""
        return self._advance(replace(outcome, error=str(error or "write_failed"),
                                     mutations=[], cards=[], candidates=[]),
                             [], register=False)

    def estimate(self) -> dict:
        """还剩多少活。给宿主估时间和花费用，**不含已有记忆索引的字数**。"""
        prog = self.progress
        pending = [(o, c, h) for o, c, h in self._batches if o >= prog.cursor]
        try:
            if self.strategy == "single_pass":
                overhead = len(_capture_prompt(replace(
                    self._capture_request("", ""), cards="", buckets="", threads="")))
            else:
                overhead = len(self._candidates_prompt("", ""))
        except Exception:  # noqa: BLE001 — 估算不能让导入失败（比如 locale 缺失）
            overhead = 0
        left = len(prog.candidates) - prog.candidates_cursor
        return {
            "strategy": self.strategy,
            "batches_total": len(self._batches),
            "batches_remaining": len(pending),
            "prompt_chars_remaining": sum(overhead + len(c) for _o, c, _h in pending),
            "write_batches_remaining": (math.ceil(left / self._write_group)
                                        if self.strategy == "two_pass" else 0),
            # 两段式读完材料之前，写卡要几批还不知道。
            "write_batches_known": self.strategy != "two_pass" or not pending,
        }

    # -- 内部 ----------------------------------------------------------- #

    def _set_known(self, cards: Sequence[Mapping]) -> None:
        self._known = {}
        for card in cards:
            rid = str(card.get("id") or "").strip()
            if rid:
                self._known[rid] = dict(card)

    def _plan_batches(self) -> tuple[list[tuple[int, str, str]], int, str]:
        from .timestamps import normalize

        req = self.request
        material = str(getattr(req, "material", "") or "")
        given = tuple(getattr(req, "batches", ()) or ())
        if given and material:
            raise ValueError("material 和 batches 只能给一个")
        if not given:
            digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
            return ([(off, chunk, "") for off, chunk in
                     split_material(material, batch_chars=self._batch_chars)],
                    len(material), digest)
        planned: list[tuple[int, str, str]] = []
        canonical = []
        offset = 0
        for i, item in enumerate(given):
            if not isinstance(item, Mapping) or not isinstance(item.get("text"), str):
                raise ValueError(f"batches[{i}] 必须是带 text 字符串的对象")
            text = item["text"]
            label = " ".join(str(item.get("label") or "").split())[:120]
            dates = []
            for key in ("occurred_from", "occurred_to"):
                raw = str(item.get(key) or "").strip()
                value = normalize(raw) if raw else ""
                if raw and not value:
                    raise ValueError(f"batches[{i}].{key} 不是合法的 ISO 日期")
                dates.append(value)
            hint = " · ".join(p for p in (
                label, " → ".join(d for d in dates if d)) if p)
            canonical.append([text, label, dates[0], dates[1]])
            if text.strip():
                planned.append((offset, text, hint))
            offset += len(text)
        digest = hashlib.sha256(json.dumps(
            ["batches-v1", canonical], ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        return planned, offset, digest

    def _fingerprint(self, binding: tuple[str, str], source_digest: str) -> str:
        req = self.request
        payload: list[Any] = [
            "history-import-v1", source_digest, binding[0], binding[1],
            str(getattr(req, "mount", "") or ""),
            str(getattr(req, "locale", "") or ""), self.policy,
            str(getattr(req, "material_kind", "") or ""),
            str(getattr(req, "ai_name", "") or ""),
            str(getattr(req, "user_name", "") or ""),
            str(getattr(req, "idempotency_key", "") or ""),
            self._batch_card_limit, self._batch_chars,
        ]
        # 新增的语义只在偏离默认值时进指纹：老进度在默认请求上照常能续传。
        extras: dict[str, Any] = {}
        if self.strategy != "single_pass":
            extras["strategy"] = self.strategy
            extras["write_batch_candidates"] = self._write_group
        if self._max_total is not None:
            extras["max_total_cards"] = self._max_total
        if self._fallback_date:
            extras["fallback_occurred_at"] = self._fallback_date
        if getattr(req, "naming_rule", None) is not None:
            extras["naming_rule"] = str(req.naming_rule)
        if str(getattr(req, "identity", "") or ""):
            extras["identity"] = str(req.identity)
        if getattr(req, "batches", ()):
            extras["batches"] = True
        if extras:
            payload.append(extras)
        return hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()

    def _pending_material_batch(self) -> tuple[int, str, str] | None:
        for batch in self._batches:
            if batch[0] >= self.progress.cursor:
                return batch
        return None

    def _fully_consumed(self) -> bool:
        prog = self.progress
        return (prog.cursor >= prog.total
                and prog.candidates_cursor >= len(prog.candidates))

    def _consume_trailing(self) -> None:
        # split_material 故意不把纯空白批次送给模型。最后一批有效内容之后若
        # 还有尾随空白，需要把它也标成已消费；否则下一次没有 batch 可跑，
        # cursor 却永远小于 total。
        prog = self.progress
        if not prog.failed and self._pending_material_batch() is None:
            prog.cursor = prog.total

    def _capture_request(self, window: str, key: str) -> Any:
        from .contracts import CaptureRequest
        from .rendering import render_buckets, render_card_index, render_threads

        req = self.request
        remaining = (None if self._max_total is None
                     else max(0, self._max_total - self.progress.cards_added))
        cap = self._batch_card_limit if remaining is None else min(
            self._batch_card_limit, remaining)
        known = list(self._known.values())
        index = select_index_cards(known, window, limit=self._index_limit,
                                   ranker=self._ranker)
        return CaptureRequest(
            window=window,
            actor=req.actor, mount=req.mount, locale=req.locale,
            ai_name=req.ai_name, user_name=req.user_name,
            naming_rule=getattr(req, "naming_rule", None),
            identity=str(getattr(req, "identity", "") or ""),
            cards=render_card_index(index, limit=len(index)) if index else "",
            buckets=render_buckets(known),
            threads=render_threads(known),
            policy=self.policy, material_kind=str(req.material_kind or ""),
            source="history_import",
            max_cards=cap,
            idempotency_key=key,
        )

    def _candidates_prompt(self, chunk: str, hint: str) -> str:
        from .prompts.history_import import build_import_candidates_prompt

        req = self.request
        return build_import_candidates_prompt(
            window=chunk, locale=req.locale, policy=self.policy,
            ai_name=req.ai_name, user_name=req.user_name,
            naming_rule=getattr(req, "naming_rule", None),
            material_kind=str(req.material_kind or ""), time_hint=hint)

    def _finish_cards(self, cards: list[dict], mutations: list[dict]) -> tuple[list[dict], list[dict]]:
        """导入专属的收尾：兜底日期、桶名收敛。卡和 mutation 里的卡同步改。"""
        known_buckets = [str(c.get("bucket") or "") for c in self._known.values()]
        locale = str(self.request.locale or "")
        out_cards, out_mutations = [], []
        for card, mutation in zip(cards, mutations):
            card = dict(card)
            mutation = {**mutation, "card": dict(mutation.get("card") or {})}
            bucket = converge_bucket(card.get("bucket") or "", known_buckets, locale=locale)
            if bucket and bucket not in known_buckets:
                known_buckets.append(bucket)
            for target in (card, mutation["card"]):
                if bucket:
                    target["bucket"] = bucket
                if self._fallback_date and not str(target.get("occurred_at") or "").strip():
                    target["occurred_at"] = self._fallback_date
            out_cards.append(card)
            out_mutations.append(mutation)
        return out_cards, out_mutations

    def _expected(self) -> tuple[str, int] | None:
        pending = self._pending_material_batch()
        if pending is not None:
            return ("cards" if self.strategy == "single_pass" else "candidates", pending[0])
        if self.progress.candidates_cursor < len(self.progress.candidates):
            return ("write", self.progress.candidates_cursor)
        return None

    def _advance(self, outcome: ImportBatchResult, ids: list[str], *,
                 register: bool) -> ImportProgress:
        from .prompts.history_import import candidate_key

        prog = self.progress
        if self._expected() != (outcome.stage, outcome.offset):
            raise ValueError(
                "这一批不是会话当前待提交的批次（重复提交或跳批）；"
                "请用 next_batch() 重新取")
        # 正在重试这一批时先移除它的旧失败记录。成功后 failed 应为空；
        # 只 append 的话一次临时失败后即使续传成功也永远 done=False。
        prog.failed[:] = [
            f for f in prog.failed
            if not (int(f.get("offset", -1)) == outcome.offset
                    and str(f.get("stage") or outcome.stage) == outcome.stage)]
        if outcome.error:
            # 🔴 失败就**停在这里**，游标不动。继续往下跑的话，后面几批
            # 看不到这一批本该写进去的卡，会把同一件事再记一遍；
            # 而游标推过去了，这一批永远不会被重试。
            row = {"offset": outcome.offset, "error": outcome.error}
            if outcome.stage != "cards":
                row["stage"] = outcome.stage
            prog.failed.append(row)
            return prog

        if outcome.stage == "candidates":
            seen = {candidate_key(c) for c in prog.candidates}
            fresh, dropped = [], 0
            for cand in outcome.candidates:
                key = candidate_key(cand)
                if not key or key in seen:
                    continue
                if len(prog.candidates) + len(fresh) >= MAX_CANDIDATES:
                    dropped += 1
                    continue
                seen.add(key)
                fresh.append({**cand, "batch_offset": outcome.offset})
            prog.candidates.extend(fresh)
            if dropped:
                prog.skipped.append({"offset": outcome.offset, "reason": "candidate_cap",
                                     "dropped": dropped, "cap": MAX_CANDIDATES})
            elif not fresh:
                prog.skipped.append({"offset": outcome.offset, "stage": "candidates",
                                     "reason": "nothing_worth_keeping"})
            prog.cursor = outcome.end
            prog.batches_done += 1
            self._consume_trailing()
            return prog

        if register:
            for mutation, rid in zip(outcome.mutations, ids):
                if str(mutation.get("op")) == "supersede":
                    self._known.pop(str(mutation.get("target_id") or ""), None)
                if rid:
                    self._known[rid] = {**dict(mutation.get("card") or {}), "id": rid}
        written = [i for i in ids if i]
        prog.cards_written += len(written)
        # 宿主驱动：按判断出的写卡指令计；Store 路径：按真正写进去的 id 计。
        prog.cards_added += len(outcome.mutations) if register else len(written)
        if not outcome.mutations and not written:
            row = {"offset": outcome.offset, "reason": "nothing_worth_keeping"}
            if outcome.stage != "cards":
                row["stage"] = outcome.stage
            prog.skipped.append(row)
        if outcome.stage == "cards":
            prog.cursor = outcome.end
        else:
            prog.candidates_cursor = outcome.end
        prog.batches_done += 1
        self._consume_trailing()
        return prog


def _check_resume(prog: ImportProgress, *, total: int, source_digest: str,
                  fingerprint: str) -> None:
    if prog.cursor < 0 or prog.cursor > total:
        raise ValueError("history import progress.cursor 超出材料范围")
    if prog.source_digest and prog.source_digest != source_digest:
        raise ValueError(
            "history import progress 属于另一份材料；请从新进度开始导入")
    if not prog.source_digest and prog.cursor:
        raise ValueError(
            "旧 history import progress 没有 source_digest，无法证明它属于"
            "当前材料；请从头重启（稳定 batch 幂等键会防止重复落卡）")
    if prog.import_fingerprint and prog.import_fingerprint != fingerprint:
        raise ValueError(
            "history import progress 的 scope/mount/locale/policy/来源/幂等/批次规则"
            "与当前请求不同；不能安全续传")
    if not prog.import_fingerprint and prog.cursor:
        raise ValueError(
            "旧 history import progress 没有 import_fingerprint，无法验证"
            "当前导入语义；请从头重启")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _capture_prompt(request: Any) -> str:
    from .prompts.capture import build_capture_prompt

    return build_capture_prompt(
        ai_name=request.ai_name, user_name=request.user_name,
        naming_rule=request.naming_rule, buckets=request.buckets,
        threads=request.threads, identity=request.identity, window=request.window,
        cards=request.cards, policy=request.policy, locale=request.locale,
        material_kind=request.material_kind)


__all__ = [
    "IMPORT_BATCH_CHARS",
    "MAX_CANDIDATES",
    "STRATEGIES",
    "ImportBatch",
    "ImportBatchResult",
    "ImportProgress",
    "ImportSession",
    "batch_key",
    "converge_bucket",
    "select_index_cards",
    "split_material",
]
