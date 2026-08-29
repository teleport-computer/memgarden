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
    BrowseItem,
    CuratedWriteRequest,
    ExportRequest,
    ExportResult,
    ImportRequest,
    PromoteRequest,
    Step,
    to_browse_item,
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
from .prompts.dream import (
    build_dream_prompt,
    build_dream_retry_prompt,
    parse_dream_consolidations,
)
from .text.card_text import build_truncation_retry_prompt
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
    #: 用户明说要记的一件事（不做「值不值得记」的判断）。
    curated_write: bool = True
    #: 导出这个人的全部记忆。
    export: bool = True
    #: private → shared 的显式提升。
    promote: bool = True
    #: 历史导入**专用的判断尺子**。
    #:
    #: ⚠️ 现在是 ``False``：``policies`` 里有 ``history_import`` 档，但
    #: ``build_capture_prompt`` 只实现了 ``conversation_capture`` 的模板结构。
    #: ``import_history()`` 仍可用，只是会用日常聊天那把（偏保守）的尺子。
    #:
    #: 声明成 False 而不是假装支持 —— 后者的表现是「导入成功但几乎没记住
    #: 什么」，用户和宿主都查不出原因。
    history_import: bool = False
    #: 逻辑可见范围。第一阶段只保证 agent-private —— 声明清楚，
    #: 别让宿主以为写进 shared 会生效。
    mounts: tuple[str, ...] = ("agent-private",)
    schema_version: int = 1

    def as_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


def _is_truncated(reply) -> bool:
    """模型回复带不带「被截断」的标记。

    内核**自己判断不了**截断 —— 那要看 provider 的 finish_reason，只有宿主有。
    所以约定一个可选的信封：模型端口可以返回一个带 ``truncated`` 的对象
    （或 dict），内核认这个标记；返回纯字符串就当没截断。

    这样宿主不必为了报一个 bool 而改整套调用链。
    """
    if isinstance(reply, dict):
        return bool(reply.get("truncated"))
    return bool(getattr(reply, "truncated", False))


class _CapturePlan:
    """落卡的状态机 —— **同步和异步共用这一份**。

    为什么要抽出来：``capture`` 和 ``acapture`` 只有「怎么等模型回复」不同，
    判断逻辑完全一样。各写一份的话，改了一边忘了另一边，同步和异步就会
    悄悄产出不同的结果 —— 而这种分岔只有在真模型上才看得见。

    用法：反复问 ``next_prompt()``，拿到 ``None`` 就 ``finish()``；
    拿到提示词就调模型，把回复 ``feed()`` 回来。
    """

    def __init__(self, owner: "GardenComponent", request: CaptureRequest) -> None:
        self.owner = owner
        self.request = request
        self.rejected: CaptureResult | None = None
        self.prompt = ""
        self.cards: list[dict] = []
        self.err: str | None = None
        self.retried = 0
        self.calls = 0
        self._stage = "first"

        if not str(request.locale or "").strip():
            # 不给默认值是刻意的：默认成某种语言，等于把一套分类法硬塞给使用者，
            # 而他不会知道自己的库里为什么长出了中文桶。
            self.rejected = CaptureResult(error="locale_required")
            return

        self.prompt = build_capture_prompt(
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

    def next_prompt(self) -> str | None:
        """下一次该问模型什么；``None`` = 问完了。"""
        if self._stage == "first":
            self.owner._step(Step(
                kind="prompt_built", purpose="capture", attempt=0,
                detail={"prompt_chars": len(self.prompt),
                        "window_chars": len(self.request.window or ""),
                        "locale": self.request.locale},
                prompt=self.prompt,
            ))
            return self.prompt
        if self._stage == "format_retry":
            return build_capture_retry_prompt(self.prompt, self.err or "")
        if self._stage == "semantic_retry":
            return build_capture_semantic_retry_prompt(
                self.prompt, capture_semantic_retry_reasons(self.cards)
            )
        if self._stage == "truncation_retry":
            # 换一版**更简短**的提示词 —— 原样重问多半还是会被截在同一个位置。
            return build_truncation_retry_prompt(self.prompt)
        return None

    def feed(self, raw: str, *, truncated: bool = False) -> None:
        """把模型这次的回复喂回来，并决定下一步。

        ``truncated`` 由**宿主**判断 —— 回复有没有被 provider 截断，只有拿到
        原始响应元数据（finish_reason / usage）的那一层看得见，内核看不到。
        这是刻意的分工：**宿主判断，内核决定怎么办**（换一版更简短的提示词重问）。

        判错的代价不对称：漏判会把半截 JSON 当成解析失败、整个窗口的落卡清零；
        误判只是多花一次调用。所以宿主宁可宽一点。
        """
        self.calls += 1
        if truncated and self._stage != "truncation_retry" and self.retried < self.owner._max_retries:
            self.owner._step(Step(
                kind="retrying", purpose="capture", attempt=self.calls,
                detail={"why": "output_truncated", "kind": "truncation"},
            ))
            self._stage = "truncation_retry"
            self.retried += 1
            return
        self.owner._step(Step(
            kind="model_called", purpose="capture", attempt=self.calls,
            detail={"reply_chars": len(str(raw or "")), "stage": self._stage,
                    "truncated": truncated},
            reply=raw if isinstance(raw, str) else None,
        ))
        raw = raw if isinstance(raw, str) else (
            raw.get("text", "") if isinstance(raw, dict) else str(getattr(raw, "text", raw))
        )
        strict = self._stage == "first"
        cards, err = parse_capture_cards(
            raw, strict=strict, policy=self.request.policy, signals=self.owner._signals
        )

        if self._stage == "semantic_retry":
            # 语义重问失败要**报失败**，不能退回上一版。
            #
            # 上一版正是「要覆盖旧卡但没说覆盖哪张」那种卡 —— 保留它等于
            # 把一条执行不了的指令写出去：宿主会拿着空的 target_id 去 supersede，
            # 结果要么静默无效、要么覆盖错东西。
            #
            # 这条是照宿主 io 的托管路径对齐的（它返回
            # ``semantic_validation_failed_after_retry`）。组件原本的写法更宽松，
            # 逐条比对两条路时发现的 —— 宽松在这儿是错的，
            # 因为「写出一条执行不了的指令」比「这轮没落卡」严重得多。
            self.retried += 1
            self._stage = "done"
            if err:
                self.err = str(err)
                return
            if capture_semantic_retry_reasons(cards):
                self.err = "semantic_validation_failed_after_retry"
                self.cards = []
                return
            self.cards, self.err = cards, None
            return

        self.cards, self.err = cards, err
        if self._stage == "format_retry":
            self.retried += 1

        self.owner._step(Step(
            kind="parsed", purpose="capture", attempt=self.calls,
            detail={"cards": len(cards), "error": err},
        ))

        if err:
            if is_retryable_parse_error(err) and self.retried < self.owner._max_retries:
                self._stage = "format_retry"
                self.owner._step(Step(
                    kind="retrying", purpose="capture", attempt=self.calls,
                    detail={"why": err, "kind": "format"},
                ))
            else:
                self._stage = "done"
            return

        reasons = capture_semantic_retry_reasons(cards)
        if reasons and self.retried < self.owner._max_retries:
            self._stage = "semantic_retry"
            self.owner._step(Step(
                kind="retrying", purpose="capture", attempt=self.calls,
                detail={"why": "semantic", "reasons": len(reasons)},
            ))
        else:
            self._stage = "done"

    def finish(self) -> CaptureResult:
        trace = self.owner._trace(self.request, self.calls, cards=len(self.cards))
        self.owner._step(Step(
            kind="done", purpose="capture", attempt=self.calls,
            detail={"cards": len(self.cards), "retried": self.retried, "error": self.err},
        ))
        if self.err:
            return CaptureResult(error=self.err, retried=self.retried, trace=trace)
        return CaptureResult(
            mutations=[self.owner._to_mutation(c, self.request) for c in self.cards],
            cards=list(self.cards),
            retried=self.retried,
            trace=trace,
        )


class _MaintenancePlan:
    """整理的状态机 —— 和落卡同构：反复问「下一步问什么」，喂回复，取结果。

    比落卡简单：整理只有格式重问一档（没有语义闸），但**截断这一档必须有** ——
    整理的提示词把整个花园的卡都塞进去了，是所有 lane 里最容易被截的。
    """

    def __init__(self, owner: "GardenComponent", request: MaintenanceRequest) -> None:
        self.owner = owner
        self.request = request
        self.rejected: MaintenanceResult | None = None
        self.prompt = ""
        self.consolidations: list[dict] = []
        self.err: str | None = None
        self.retried = 0
        self.calls = 0
        self._stage = "first"

        snapshot = dream_snapshot(
            available_cards=request.cards,
            all_cards=request.all_cards or request.cards,
        )
        self.snapshot = snapshot
        verdict = needs_dream(
            snapshot,
            DreamLedger(last_seed_card_count=request.last_seed_card_count,
                        last_signature=request.last_signature),
            min_new_cards=owner._min_new_cards,
        )
        self.verdict = verdict
        if not verdict.needed or request.dry_run:
            self.rejected = MaintenanceResult(
                needed=verdict.needed,
                trace={"reason": verdict.reason, "new_cards": verdict.new_cards,
                       "signature": snapshot.signature,
                       "seed_card_count": snapshot.seed_card_count},
            )
            return

        rendered = "\n".join(
            f"- [{c.get('id')}] {c.get('summary','')}" for c in request.cards
        )
        self.prompt = build_dream_prompt(
            ai_name=request.ai_name, user_name=request.user_name,
            cards=rendered, recent_conversations=request.recent_conversations,
            locale=request.locale,
        )

    def next_prompt(self) -> str | None:
        if self._stage == "first":
            self.owner._step(Step(
                kind="prompt_built", purpose="dream", attempt=0,
                detail={"prompt_chars": len(self.prompt),
                        "cards": len(self.request.cards)},
                prompt=self.prompt,
            ))
            return self.prompt
        if self._stage == "format_retry":
            return build_dream_retry_prompt(self.prompt, self.err or "")
        if self._stage == "truncation_retry":
            return build_truncation_retry_prompt(self.prompt)
        return None

    def feed(self, raw, *, truncated: bool = False) -> None:
        self.calls += 1
        text = raw if isinstance(raw, str) else str(raw)
        self.owner._step(Step(
            kind="model_called", purpose="dream", attempt=self.calls,
            detail={"reply_chars": len(text), "stage": self._stage,
                    "truncated": truncated},
            reply=text,
        ))
        if truncated and self._stage != "truncation_retry" and self.retried < self.owner._max_retries:
            self._stage = "truncation_retry"
            self.retried += 1
            self.owner._step(Step(kind="retrying", purpose="dream",
                                  attempt=self.calls,
                                  detail={"why": "output_truncated",
                                          "kind": "truncation"}))
            return

        strict = self._stage == "first"
        cons, _questions, err = parse_dream_consolidations(
            text, strict=strict, signals=self.owner._signals,
            known_ids=frozenset(self.request.known_ids),
        )
        self.consolidations, self.err = cons, err
        if self._stage == "format_retry":
            self.retried += 1
        self.owner._step(Step(kind="parsed", purpose="dream", attempt=self.calls,
                              detail={"consolidations": len(cons), "error": err}))
        if err and is_retryable_parse_error(err) and self.retried < self.owner._max_retries:
            self._stage = "format_retry"
            self.owner._step(Step(kind="retrying", purpose="dream",
                                  attempt=self.calls,
                                  detail={"why": err, "kind": "format"}))
            return
        self._stage = "done"

    def finish(self) -> MaintenanceResult:
        trace = {"reason": self.verdict.reason, "new_cards": self.verdict.new_cards,
                 "consolidations": len(self.consolidations),
                 "signature": self.snapshot.signature,
                 "seed_card_count": self.snapshot.seed_card_count}
        self.owner._step(Step(kind="done", purpose="dream", attempt=self.calls,
                              detail={"consolidations": len(self.consolidations),
                                      "retried": self.retried, "error": self.err}))
        if self.err:
            return MaintenanceResult(needed=True, error=self.err, trace=trace)
        return MaintenanceResult(
            needed=True,
            mutations=[dict(c, mount=self.request.mount) for c in self.consolidations],
            trace=trace,
        )


class MaintenanceSession:
    """由宿主驱动的整理会话。用法和 :class:`CaptureSession` 一样。"""

    def __init__(self, plan: "_MaintenancePlan") -> None:
        self._plan = plan

    def next_prompt(self) -> str | None:
        if self._plan.rejected is not None:
            return None
        return self._plan.next_prompt()

    def feed(self, reply, *, truncated: bool = False) -> None:
        self._plan.feed(reply, truncated=truncated)

    def result(self) -> MaintenanceResult:
        if self._plan.rejected is not None:
            return self._plan.rejected
        return self._plan.finish()


class CaptureSession:
    """由**宿主驱动**的落卡会话 —— 给自带 provider 机制的宿主用。

    ## 为什么需要这个

    ``capture()`` 和 ``acapture()`` 自带循环：组件问模型、拿回复、决定重问。
    对多数接入方这是最省事的。

    但有的宿主自己管着一整套 provider 机制 —— 截断检测（要看 finish_reason）、
    用量统计、失败分类、退避、轨迹。宿主 io 的托管 worker 就是这样。
    让组件把 provider 调用抢过去，等于要求宿主**放弃这些能力**，
    那是净退步，这层门面就不值得接。

    所以给第二种形状：**组件只回答「下一步该问什么」和「拿到回复之后怎么办」，
    provider 那一步仍归宿主。**

        session = garden.capture_session(request)
        while (prompt := session.next_prompt()) is not None:
            reply, truncated = my_provider.call(prompt)      # 宿主自己的机制
            session.feed(reply, truncated=truncated)
        result = session.result()

    两种形状**共用同一个状态机**，判断逻辑是同一份 —— 各写一份的话，
    自带循环和宿主驱动会悄悄产出不同的卡。
    """

    def __init__(self, plan: "_CapturePlan") -> None:
        self._plan = plan

    def next_prompt(self) -> str | None:
        """下一次该问模型什么；``None`` = 问完了，可以取结果。"""
        if self._plan.rejected is not None:
            return None
        return self._plan.next_prompt()

    def feed(self, reply: str, *, truncated: bool = False) -> None:
        """把这次的模型回复交回来。

        ``truncated`` 由宿主判断 —— 回复有没有被 provider 截断，
        只有拿到原始响应元数据的那一层看得见。
        """
        self._plan.feed(reply, truncated=truncated)

    def result(self) -> CaptureResult:
        if self._plan.rejected is not None:
            return self._plan.rejected
        return self._plan.finish()


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
        on_step=None,
    ) -> None:
        #: 每一步都回调一次。宿主用它记轨迹 —— 不给就什么都不记。
        #: 收进组件的编排不能让宿主的可观测性净退步，这是那条的落点。
        self._on_step = on_step
        self._model = model
        self._policy = selection_policy
        self._signals = signals
        self._clock = clock or SystemClock()
        self._min_new_cards = min_new_cards_for_maintenance
        self._max_retries = max(0, max_capture_retries)

    def _step(self, step: Step) -> None:
        """汇报一步。回调抛异常不许影响主流程 —— 记轨迹失败不该让落卡失败。"""
        if self._on_step is None:
            return
        try:
            self._on_step(step)
        except Exception:  # noqa: BLE001
            pass

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
        plan = _CapturePlan(self, request)
        if plan.rejected is not None:
            return plan.rejected
        while True:
            ask = plan.next_prompt()
            if ask is None:
                return plan.finish()
            reply = self._model.complete(ask, purpose="capture")
            plan.feed(reply, truncated=_is_truncated(reply))

    async def acapture(self, request: CaptureRequest) -> CaptureResult:
        """:meth:`capture` 的异步版 —— 逐步等价，**判断逻辑是同一份**。

        存在的理由不是接口对称：真实 Runtime 的模型调用几乎都是 async，
        一次 capture 要等几秒，同步阻塞会卡住整个事件循环。
        只有同步版的话，异步宿主（比如 io 的托管 worker）根本接不上。

        要求注入的 ``model`` 有 ``async def complete``。
        """
        plan = _CapturePlan(self, request)
        if plan.rejected is not None:
            return plan.rejected
        while True:
            ask = plan.next_prompt()
            if ask is None:
                return plan.finish()
            reply = await self._model.complete(ask, purpose="capture")
            plan.feed(reply, truncated=_is_truncated(reply))

    def maintenance_session(self, request: MaintenanceRequest) -> MaintenanceSession:
        """开一个由宿主驱动的整理会话。见 :class:`CaptureSession` 的理由，
        两者同构 —— 自带 provider 机制的宿主两条 lane 用同一种形状接入。"""
        return MaintenanceSession(_MaintenancePlan(self, request))

    def capture_session(self, request: CaptureRequest) -> CaptureSession:
        """开一个由宿主驱动的落卡会话。见 :class:`CaptureSession`。

        自带 provider 机制的宿主用这个；其余用 :meth:`capture` /
        :meth:`acapture` 更省事。三者共用同一个状态机。
        """
        return CaptureSession(_CapturePlan(self, request))

    # -- 另外两种写入来源 ------------------------------------------------ #

    def import_history(self, request: ImportRequest) -> CaptureResult:
        """历史导入：用户主动交出的一批过去材料。

        走同一条落卡链路，**但该换一把尺子** —— 这是他自己给的东西，宁可多记；
        自动落卡那把「克制」的尺子在这里是错的。

        ## ⚠️ 当前状态：尺子有，模板没有

        ``policies`` 里有 ``history_import`` 档（判据写好了），但
        ``build_capture_prompt`` 只实现了 ``conversation_capture`` 的模板结构，
        其余档位的动作偏好/日期/输出 schema **尚未策略化**。

        所以这条路现在**只能用日常聊天那把尺子**，判断会偏保守 ——
        用户交出三年记录，可能只蒸出很少几张。

        这件事写在 ``capabilities().history_import`` 里（值是 ``False``），
        不靠调用方读源码发现。**宁可明说不支持，也不要悄悄用错的尺子** ——
        后者的表现是「导入成功但几乎没记住什么」，而且没有任何错误可查。

        ``max_cards`` 仍然必要：三年的聊天记录一次能蒸出几百张，
        之后的召回会被这批淹没，而用户看不出发生了什么。
        """
        policy = request.policy
        if policy and policy != "conversation_capture":
            # 明确拒绝，而不是退回默认档假装做了。
            return CaptureResult(
                error=f"policy_not_supported_by_prompt_template:{policy}",
                trace={"supported": ["conversation_capture"]},
            )
        result = self.capture(CaptureRequest(
            window=request.material,
            actor=request.actor, mount=request.mount, locale=request.locale,
            ai_name=request.ai_name, user_name=request.user_name,
            policy=None,   # 见上：其余档位的模板结构尚未策略化
            idempotency_key=request.idempotency_key,
        ))
        if len(result.mutations) > request.max_cards:
            kept = request.max_cards
            result.trace = {**result.trace, "capped_from": len(result.mutations),
                            "cap": kept}
            # 截断要**说出来**。悄悄丢掉一半，用户看到的是「导入成功」，
            # 实际少了一半，且没有任何痕迹。
            result.mutations = result.mutations[:kept]
            result.cards = result.cards[:kept]
        return result

    def write_one(self, request: CuratedWriteRequest) -> CaptureResult:
        """用户明说要记的一件事。

        ⚠️ **这条路不做「值不值得记」的判断。** 他说了「记一下我不吃辣」，
        我们的活是把它记好，不是评估该不该记。拿克制那把尺子来量，
        模型会判「这不值得」然后什么都不发生 —— 用户以为记住了，其实没有。

        所以这里**不调模型做筛选**，直接成卡；只有归类和清洗还走内核。
        """
        text = str(request.text or "").strip()
        if not text:
            return CaptureResult(error="empty_text")
        from .text.card_text import card_text_rejection
        from .prompts.buckets import normalize_bucket_language

        summary = text if len(text) <= 40 else text[:38] + "…"
        bucket = normalize_bucket_language(request.bucket, text) if request.bucket else ""
        rejection = card_text_rejection(summary=summary, content=text,
                                        signals=self._signals)
        if rejection:
            return CaptureResult(error=f"invalid_card_content:{rejection}")
        card = {"action": "add", "summary": summary, "content": text,
                "bucket": bucket, "threads": [], "source": "curated"}
        return CaptureResult(
            mutations=[self._to_mutation(card, CaptureRequest(
                window="", locale=request.locale, mount=request.mount,
                idempotency_key=request.idempotency_key))],
            cards=[card],
            trace={"source": "curated", "chars": len(text), "mount": request.mount},
        )

    # -- 导出 / 提升 ------------------------------------------------------ #

    def export(self, request: ExportRequest, records: list[dict]) -> ExportResult:
        """把这个人的记忆整理成可交付的形状。

        **记录由宿主给** —— 内核不读库、也不知道谁有权看哪个 mount。
        这里只负责整理成一致的形状和给出计数（供宿主核对完整性）。

        默认含已归档、已被取代的：「你删掉的那条我还留着」和
        「你看不到自己删过什么」都是不该有的状态。
        """
        out = []
        counts = {"total": 0, "archived": 0, "superseded": 0}
        for r in records:
            lifecycle = str(r.get("lifecycle") or "active")
            archived = lifecycle == "archived" or bool(r.get("archived"))
            superseded = bool(r.get("superseded_by"))
            if not request.include_archived and (archived or superseded):
                continue
            counts["total"] += 1
            counts["archived"] += int(archived)
            counts["superseded"] += int(superseded)
            out.append(r)
        return ExportResult(records=out, counts=counts)

    def promote(self, request: PromoteRequest) -> ToolResult:
        """把一条私有记忆提升到共享范围 —— **必须是显式操作**。

        内核不自行授予权限：``authorized`` 由宿主的权限校验填。
        没有它就拒绝，而不是「大概可以吧」。
        """
        if not request.authorized:
            return ToolResult(ok=False, error="not_authorized_by_host")
        if not request.record_id:
            return ToolResult(ok=False, error="record_id_required")
        return ToolResult(
            content="ok",
            mutations=[{
                "op": "update", "record_id": request.record_id,
                "changes": {"mount": request.to_mount},
                "mount": request.to_mount,
                "reason": request.reason or "promoted_by_user",
            }],
        )

    def browse(self, records: list[dict]) -> list[BrowseItem]:
        """投影成通用记忆列表项 —— 供 Runtime 的跨组件 Browse 页面用。

        Garden 自己的界面照常用 bucket/threads；这层只是让**别家组件**
        也能进同一个列表。
        """
        return [to_browse_item(r) for r in records]

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
            raw, signals=self._signals, known_ids=frozenset(request.known_ids),
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
