"""GardenComponent —— 一次调用完成一件事，宿主不必知道内部有哪些零件。

## 为什么要有这一层

在这之前，「落一次卡」是这样的：宿主要自己知道 ``build_capture_prompt`` 之后
该调谁、返回值怎么解析、过闸要不要传 signals、桶名要不要归一化、
模型吐脏了怎么重问。这份**拼装说明书从来没写下来过**，只存在于宿主 io 的代码里
（散在 23 个文件、15 个子模块、约 30 个符号）。

后果有两个，都不是理论上的：

    对外   别人装了这个包，得翻源码重新发明一遍那份说明书
    对内   Garden 内部改个函数名，宿主就编译不过

这一层把那份说明书收进来。**判断逻辑一行没改，只是搬了个位置。**

## 边界：判断在里面，执行在外面

    在这里     什么值得记 · 写成几张 · 归哪个桶 · 挑哪几张 · 该不该整理 · 怎么合
    不在这里   调模型（key 在宿主）· 加解密 · 写库 · 定时器 · 权限 · UI

所以 ``capture()`` 返回的是**「该这么改」的指令**，不是「已经写好了」。
宿主拿到之后自己落库 —— 它可能还要加密、还要过自己的权限闸，
那些内核既不知道也不该知道。

## 明文

全链路明文。宿主有加密的话，在自己的 adapter 里解完再喂进来。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import (
    Actor,
    CaptureRequest,
    CaptureResult,
    ContextRequest,
    ContextResult,
    MaintenanceRequest,
    MaintenanceResult,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from .dreaming import DreamLedger, dream_snapshot, needs_dream
from .ports import ClockPort, ModelPort, SystemClock
from .prompts.capture import (
    build_capture_prompt,
    build_capture_retry_prompt,
    build_capture_semantic_retry_prompt,
    capture_semantic_retry_reasons,
    parse_capture_cards,
)
from .prompts.dream import build_dream_prompt, parse_dream_consolidations
from .selection import Chain
from .text.card_text import is_retryable_parse_error
from .text.leak_signals import GENERIC_SIGNALS, LeakSignals


@dataclass(frozen=True)
class GardenCapabilities:
    """这个组件会做什么。宿主据此决定接哪几条线、跳过哪几条。

    **有声明和没声明的差别是实打实的**：没有声明时，宿主只能把「Garden 一定会做
    夜里整理」写死在自己代码里；接一个不做整理的组件时，它会傻等，等不到也不报错。
    """

    capture: bool = True
    turn_context: bool = True
    maintenance: bool = True
    model_tools: bool = True
    #: 逻辑可见范围。第一阶段只保证 agent-private —— 声明清楚，
    #: 别让宿主以为写进 shared 会生效。
    mounts: tuple[str, ...] = ("agent-private",)
    schema_version: int = 1

    def as_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


class GardenComponent:
    """完整的 Memory Garden 能力，注入式依赖。

        garden = GardenComponent(model=my_model, selection_policy=my_policy)
        result = garden.capture(CaptureRequest(window=..., locale="zh-Hans"))
        my_store.apply(result.mutations)     # ← 落库是宿主的事

    ``model`` 是唯一必需的注入：判断需要问模型，而 key 在宿主手里。
    """

    def __init__(
        self,
        *,
        model: ModelPort,
        selection_policy: Chain | None = None,
        signals: LeakSignals = GENERIC_SIGNALS,
        clock: ClockPort | None = None,
        min_new_cards_for_maintenance: int = 10,
        max_capture_retries: int = 1,
    ) -> None:
        self._model = model
        self._policy = selection_policy
        self._signals = signals
        self._clock = clock or SystemClock()
        self._min_new_cards = min_new_cards_for_maintenance
        self._max_retries = max(0, max_capture_retries)

    # -- 声明 ------------------------------------------------------------ #

    def capabilities(self) -> GardenCapabilities:
        return GardenCapabilities(turn_context=self._policy is not None)

    # -- 落卡 ------------------------------------------------------------ #

    def capture(self, request: CaptureRequest) -> CaptureResult:
        """这段对话里有什么值得记。返回**改动指令**，不写库。

        内部就是宿主以前手写的那几步，一步没少：
        拼提示词 → 问模型 → 解析 → 格式不对重问 → 语义不对再重问。

        **两种「没有产出」必须分得开**：真的没什么值得记（正常，游标该推进）
        和解析彻底失败（异常，游标不能动，否则这批对话永远不会再被看一眼）。
        """
        if not str(request.locale or "").strip():
            # 不给默认值是刻意的：默认成某种语言，等于把一套分类法硬塞给使用者，
            # 而他不会知道自己的库里为什么长出了中文桶。
            return CaptureResult(error="locale_required")

        prompt = build_capture_prompt(
            ai_name=request.ai_name,
            user_name=request.user_name,
            naming_rule=request.naming_rule,
            buckets=request.buckets,
            threads=request.threads,
            identity=request.identity,
            window=request.window,
            cards=request.cards,
            policy=request.policy,
            locale=request.locale,
        )
        calls = 0
        raw = self._model.complete(prompt, purpose="capture")
        calls += 1
        cards, err = parse_capture_cards(raw, policy=request.policy, signals=self._signals)

        retried = 0
        while err and is_retryable_parse_error(err) and retried < self._max_retries:
            raw = self._model.complete(
                build_capture_retry_prompt(prompt, err), purpose="capture"
            )
            calls += 1
            retried += 1
            cards, err = parse_capture_cards(
                raw, strict=False, policy=request.policy, signals=self._signals
            )

        if err:
            return CaptureResult(
                error=err, retried=retried,
                trace=self._trace(request, calls, cards=0),
            )

        # 语义重问：模型说要覆盖旧卡却没给 target_id 这类，本地就能证明是错的。
        reasons = capture_semantic_retry_reasons(cards)
        if reasons and retried < self._max_retries:
            raw = self._model.complete(
                build_capture_semantic_retry_prompt(prompt, reasons), purpose="capture"
            )
            calls += 1
            retried += 1
            retried_cards, retry_err = parse_capture_cards(
                raw, strict=False, policy=request.policy, signals=self._signals
            )
            if not retry_err and not capture_semantic_retry_reasons(retried_cards):
                cards = retried_cards

        return CaptureResult(
            mutations=[self._to_mutation(c, request) for c in cards],
            retried=retried,
            trace=self._trace(request, calls, cards=len(cards)),
        )

    # -- 想起来 ---------------------------------------------------------- #

    def build_context(self, request: ContextRequest) -> ContextResult:
        """这一轮该想起哪几张。

        候选由宿主给 —— 生命周期过滤和权限过滤都在宿主那边做完了。
        内核不认识你的 ``is_archived``，也没资格替你判断谁能看什么。
        """
        if self._policy is None:
            return ContextResult(trace={"skipped": "no_selection_policy"})

        result = self._policy.select(
            request.candidates, request.query, limit=request.limit
        )
        by_id = {str(c.get("id") or ""): c for c in request.candidates}
        ids, blocks, by_stage = [], [], {}
        for pick in result.picks:
            card = by_id.get(pick.card_id) or {}
            ids.append(pick.card_id)
            by_stage[pick.card_id] = pick.stage
            blocks.append({
                "type": "memory",
                "record_ref": pick.card_id,
                # 摘要算不出来的卡这里会是空串。**宿主的入口过滤要在这之前做完** ——
                # 2026-08-14 的事故正是这类卡带着空摘要进了流程，然后被整批丢弃，
                # 用户问「我有一只狗吗」答记忆里没有。
                "text": str(card.get("summary") or ""),
                "mount": request.mounts[0] if request.mounts else "",
                "stage": pick.stage,
            })
        return ContextResult(
            record_ids=ids,
            blocks=blocks,
            # 「为什么想不起来」只有这条线索。没有它，用户说「它忘了我的狗」时
            # 查不出是没召回、还是根本没落库。
            trace={
                "candidates": len(request.candidates),
                "selected": len(ids),
                "by_stage": by_stage,
                "diagnostics": [dict(d) for d in result.diagnostics],
            },
        )

    # -- 整理 ------------------------------------------------------------ #

    def run_maintenance(self, request: MaintenanceRequest) -> MaintenanceResult:
        """该不该整理；要整理的话怎么合。

        ``dry_run=True`` 只回答前半句 —— 宿主的调度器用它决定要不要排这个活，
        不必为了问一句就烧一次模型调用。
        """
        snapshot = dream_snapshot(
            available_cards=request.cards,
            all_cards=request.all_cards or request.cards,
        )
        verdict = needs_dream(
            snapshot,
            DreamLedger(
                last_seed_card_count=request.last_seed_card_count,
                last_signature=request.last_signature,
            ),
            min_new_cards=self._min_new_cards,
        )
        if not verdict.needed or request.dry_run:
            return MaintenanceResult(
                needed=verdict.needed,
                trace={"reason": verdict.reason, "new_cards": verdict.new_cards,
                       # 整理完宿主要把这两个存回自己的账本，否则下次判断不出增量。
                       "signature": snapshot.signature,
                       "seed_card_count": snapshot.seed_card_count},
            )

        rendered = "\n".join(
            f"- [{c.get('id')}] {c.get('summary','')}" for c in request.cards
        )
        prompt = build_dream_prompt(
            ai_name=request.ai_name,
            user_name=request.user_name,
            cards=rendered,
            recent_conversations=request.recent_conversations,
            locale=request.locale,
        )
        raw = self._model.complete(prompt, purpose="dream")
        consolidations, _questions, err = parse_dream_consolidations(
            raw, signals=self._signals
        )
        if err:
            return MaintenanceResult(needed=True, error=err,
                                     trace={"reason": verdict.reason})
        return MaintenanceResult(
            needed=True,
            mutations=[dict(c, mount=request.mount) for c in consolidations],
            trace={"reason": verdict.reason, "new_cards": verdict.new_cards,
                   "consolidations": len(consolidations),
                   "signature": snapshot.signature,
                   "seed_card_count": snapshot.seed_card_count},
        )

    # -- 给模型的工具 ----------------------------------------------------- #

    def tools(self) -> list[ToolDefinition]:
        """模型能主动调的工具。**和 capture 不是一回事** ——
        capture 是后台自动跑的判断，这些是模型自己决定要不要调。"""
        return [
            ToolDefinition(
                name="memory_search",
                description="Search the user's durable memories by keyword or topic.",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            ),
            ToolDefinition(
                name="memory_write",
                description="Record one durable fact the user asked to remember.",
                parameters={
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "content": {"type": "string"},
                        "bucket": {"type": "string"},
                    },
                    "required": ["summary", "content"],
                },
            ),
        ]

    def invoke_tool(self, call: ToolCall) -> ToolResult:
        """执行工具。写类工具同样只返回**指令**，落库仍归宿主。"""
        if call.name == "memory_write":
            args = call.arguments or {}
            summary = str(args.get("summary") or "").strip()
            content = str(args.get("content") or "").strip()
            if not summary or not content:
                return ToolResult(ok=False, error="summary_and_content_required")
            return ToolResult(
                content="ok",
                mutations=[{
                    "op": "add",
                    "mount": call.mounts[0] if call.mounts else "agent-private",
                    "card": {"summary": summary, "content": content,
                             "bucket": str(args.get("bucket") or "")},
                }],
            )
        if call.name == "memory_search":
            # 搜索要读库，而库在宿主手里 —— 内核给不出结果，只能明说。
            return ToolResult(ok=False, error="search_requires_host_store")
        return ToolResult(ok=False, error=f"unknown_tool:{call.name}")

    # -- 内部 ------------------------------------------------------------ #

    def _to_mutation(self, card: dict, request: CaptureRequest) -> dict:
        action = str(card.get("action") or "add").strip().lower()
        op = "supersede" if action in {"merge", "supersede"} else "add"
        out = {
            "op": op,
            "mount": request.mount,
            "card": {k: v for k, v in card.items()
                     if k not in {"action", "target_id"}},
        }
        if op == "supersede":
            out["target_id"] = str(card.get("target_id") or "")
        if request.idempotency_key:
            out["idempotency_key"] = request.idempotency_key
        return out

    def _trace(self, request: CaptureRequest, calls: int, *, cards: int) -> dict:
        """观测量全部**内容无关** —— 长度、计数、语言标签，没有一个字是用户内容。"""
        return {
            "window_chars": len(request.window or ""),
            "model_calls": calls,
            "cards": cards,
            "locale": request.locale,
            "mount": request.mount,
        }


__all__ = ["GardenComponent", "GardenCapabilities"]
