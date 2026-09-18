"""词法排序 —— 自动想起和主动搜索共用的一把尺子。

## 为什么是 BM25、为什么分词器是插口

之前自动想起用内核的相关性打分（``scoring.relevance``），主动搜索在宿主那边另跑一套
BM25。同一个问题两条路给出不同答案，trace 里也对不上。
``evals/retrieval`` 的基线显示 BM25 的召回全面更好（recall@5 0.90 vs 0.67），
编号、短查询、线程类尤其明显，所以统一到 BM25。

数学是标准 BM25（Robertson IDF 取 ``log1p``、查询里重复的 token 只算一次、同分按
``occurred_at`` 新的在前再按 id），与一个已在线上运行的宿主实现逐项一致 ——
``tests/test_retrieval_rank.py`` 用一份冻结的参考实现对拍，给同一个分词器分数逐位相同。

分词是唯一依赖语言资源的一步。内核只依赖标准库，所以分词器由宿主注入
（例如注入 jieba）；不注入时用 :class:`DefaultTokenizer`：整段 ASCII 标识符、
按 Unicode 字母切的词、CJK 单字与相邻二字。它不承诺和 jieba 等价，质量以 ``evals/retrieval`` 的数字为准。

## 缓存只活在一次调用里

查询分析一次、每张卡分析一次，调用结束全部丢掉。**不缓存用户文本、倒排和分数**
—— 卡片随时会被改、被删、被换 owner，跨调用的缓存就是跨调用的泄漏和陈旧。

## 词法方法的边界

换说法、跨语言（「猫咪的肾脏问题」对「肾指标偏高」）拿不到，这是 BM25 的边界，
不是 bug。它也不证明答案正确，只证明用词重叠。
"""
from __future__ import annotations

import functools
import hashlib
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from . import timestamps
from .prompts.recall_fields import retrieval_cues

#: 排序算法的版本。换公式、换默认停用词表、换默认分数下限都要改它 ——
#: 宿主靠它判断两条路是不是同一把尺子、线上 trace 属于哪一版。
RANKING_VERSION = "memgarden-bm25-v2"

K1 = 1.2
B = 0.75

#: 汉字（含扩展 A、兼容区）、假名、谚文。这些文字不用空格分词，按字处理。
_CJK_CHAR = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af"
#: 标识符里的连接符：``np-4286``、``v2.3.1``、``k/d``、``foo_bar``。
_JOINER = re.compile(r"([-_./])")
#: CJK 连续段 | 字母数字连成、可由连接符串起来的词。
_SEGMENT = re.compile(rf"([{_CJK_CHAR}]+)|([^\W_{_CJK_CHAR}]+(?:[-_./][^\W_{_CJK_CHAR}]+)*)")
#: 纯语法助词。和它们相邻的二字（「的车」「咪的」「么事」）几乎都是跨词边界的噪声，
#: 不生成；助词本身的单字仍然生成（由停用词表决定查询里要不要它）。
#: 评测里去掉这些二字不改变召回，但让门槛在更宽的参数区间内稳定（见 MG-3 说明）。
_PARTICLES = frozenset("的了着吗呢吧啊呀嘛么")

#: 默认停用词：中英高频虚词、代词、疑问词，以及英文缩写残片（``what's`` → ``s``）。
#: **只从查询里去掉**。宿主传 ``stopwords=`` 覆盖，传 ``frozenset()`` 关闭。
#:
#: 刻意**不**收「喜欢」「工作」「今天」这类实词 —— 它们在记忆里是有意义的；
#: 泛词撞上带来的误召回交给覆盖率门槛处理，不靠越列越长的词表。
DEFAULT_STOPWORDS: frozenset[str] = frozenset("""
的 地 得 了 着 过 是 在 有 没 不 也 都 就 还 又 和 与 跟 及 把 被 让 给 对 从 向 为
我 你 您 他 她 它 们 这 那 哪 谁 啥 什 么 怎 吗 呢 吧 啊 呀 嘛 哦 嗯 个 些 一
什么 怎么 怎样 怎么样 为什么 哪个 哪里 哪些 哪儿 我们 你们 他们 她们 它们 自己
这个 那个 这些 那些 这样 那样 没有 有没 一个 一下 就是 还是 可以 是不 不是 的是 了吗
来着 到底 然后 后来 之前 之后 时候 现在 以前 上次 那次 这次
a an the and or but of to in on at by for with from as into about
i me my mine we our you your he him his she her it its they them their
is am are was were be been being do does did done have has had having
what whats which who whom whose when where why how
this that these those there here
can could would should will shall may might must
s t d ll m re ve
any some not no yes
""".split())

#: 门槛默认值，由 ``evals/retrieval`` 校准（210 张卡、53 条查询）：
#:
#:   默认分词器   覆盖率 0.15–0.30、强证据 1.1–2.0 之间结果完全相同
#:   jieba 分词器 覆盖率 0.25、强证据 ≤1.25 时无命中 4/5；强证据 1.5 起开始漏答案
#:
#: 取两者的交集。只有 53 条合成查询 —— **不能证明线上质量**，上线后看 trace。
DEFAULT_MIN_COVERAGE = 0.25
DEFAULT_STRONG_EVIDENCE = 1.25
#: 可选：强证据闸随查询长度放大。传 ``strong_evidence_terms=n`` 时，查询（去停用词、去重后）
#: 超过 ``n`` 个 token 就把闸乘上 √(token 数 / n)。**默认不放大**（``None``）。
#:
#: 为什么有这个开关：自动想起的查询是**最近四条对话拼起来**（含 AI 回复），几十到上百个
#: token，每个偶然撞上的泛词都给分，杂卡分数随查询变长线性上涨，固定的闸挡不住。
#: ``evals/retrieval`` 多轮窗口集（16 条，``--set multi_turn``）上，默认闸无命中 0/3、
#: 每轮平均带回 7.9 张卡；``strong_evidence_terms=8`` 时无命中 3/3、平均 1.8 张。
#:
#: 为什么默认不开：同一个放大会挡掉「长段粘贴里只有一个编号是锚点」的正确答案
#: （``tests/test_retrieval_rank.py`` 的长粘贴用例、jieba 下单句集 q33 变成返回空），
#: 且 8 这个值两侧都窄（6 或 10 都会让某一组退步）。要不要为自动想起开它是宿主的产品取舍，
#: 数字见 ``evals/retrieval/README.md``。
DEFAULT_STRONG_EVIDENCE_TERMS: int | None = None
#: 覆盖率的 IDF 至少按这么多张卡的候选池算（多出来的是「不含任何查询词」的虚拟卡）。
#: **只影响覆盖率这道闸**：分数、排序、强证据闸都照真实候选数算。候选池不小于它时没有任何变化。
#:
#: 为什么要有：覆盖率 = 命中词的 IDF / 查询全部词的 IDF，而花园里一张都没有的词拿到的是最大 IDF。
#: 池子很小时这两者差得离谱 —— 1 张卡时命中词 IDF 0.288、没命中的词 1.386，
#: 于是「我平时喝什么咖啡」在只有一张「喜欢喝美式咖啡，不加糖」的花园里覆盖率 0.12，被挡成空。
#: 新用户的花园恰恰就是这么小。按至少 F 张卡算，命中词和缺失词的 IDF 比值回到大花园里的量级。
#:
#: 取值见 ``evals/retrieval/small_pool.py``（README「小花园」）。
DEFAULT_COVERAGE_POOL_FLOOR = 20


class SearchLimitExceeded(RuntimeError):
    """候选数或文本总量超过宿主给的上限。**明确失败，不悄悄截成更小的语料。**"""

    def __init__(self, what: str = "resource") -> None:
        super().__init__(f"memory_search_resource_limit:{what}")
        self.what = what


@runtime_checkable
class Tokenizer(Protocol):
    """分词插口。``name`` 进排序版本号 —— 换分词器等于换尺子。"""

    name: str

    def tokenize(self, text: str) -> list[str]: ...


#: 拉丁字母（ASCII 字母数字 + 带重音的拉丁字母）。重音字母和 ASCII 字母同属一个词，
#: 不在它们中间切；拉丁字母和别的文字（希腊 ``μ``、西里尔字母）之间照旧切开。
_LATIN = (r"a-z0-9\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f\u1e00-\u1eff"
          r"\u2c60-\u2c7f\ua720-\ua7ff\uab30-\uab6f")


@functools.lru_cache(maxsize=1)
def _script_pattern() -> "re.Pattern[str]":
    """词内按文字（拉丁 / 其他）切段，组合附加符号跟着前一个字符走。

    附加符号字符类要扫一遍码表（约 20ms）：第一次遇到含非 ASCII 字母的词时才建，
    导入模块和纯 ASCII / CJK 文本不付这个代价。
    """
    marks = [cp for cp in (*range(0x30000), *range(0xE0100, 0xE01F0))
             if unicodedata.category(chr(cp))[0] == "M"]
    spans: list[list[int]] = []
    for cp in marks:
        if spans and cp == spans[-1][1] + 1:
            spans[-1][1] = cp
        else:
            spans.append([cp, cp])
    mark = "".join(f"{re.escape(chr(lo))}-{re.escape(chr(hi))}" for lo, hi in spans)
    return re.compile(rf"[{_LATIN}][{_LATIN}{mark}]*|[^{_LATIN}{mark}][^{_LATIN}]*|[{mark}]+")


def _leading_marks(text: str) -> str:
    """``text`` 开头连续的组合附加符号（Unicode ``M*`` 类）。"""
    end = 0
    while end < len(text) and unicodedata.category(text[end])[0] == "M":
        end += 1
    return text[:end]


def _segments(folded: str) -> list[tuple[bool, str]]:
    """（是不是词, 文本）：CJK 连续段，或由字母、数字、组合附加符号连成的词。

    组合附加符号（``é`` 分解写法里的 U+0301、印地语元音符号）在 Python 正则里不算 ``\\w``，
    正则会在它们处把词劈开。这里把「只隔着附加符号」的相邻词段拼回去，词尾的附加符号也并进词。
    词与词之间通常隔着空格或标点，只看间隔的第一个字符，几乎不花时间。不挨着词的孤立附加符号丢掉。
    """
    items: list[list] = []
    last_end = 0
    for match in _SEGMENT.finditer(folded):
        gap = folded[last_end:match.start()]
        word = match.group(2)
        marks = _leading_marks(gap) if items and items[-1][0] else ""
        if marks:
            items[-1][1] += marks
        if word is not None and marks and marks == gap:
            items[-1][1] += word
        else:
            items.append([word is not None, word if word is not None else match.group(1)])
        last_end = match.end()
    if items and items[-1][0]:
        items[-1][1] += _leading_marks(folded[last_end:])
    return [(is_word, text) for is_word, text in items]


def _word_tokens(run: str) -> list[str]:
    """一个（可能带连接符的）词 → token。

    纯 ASCII 的整段保留（``np-4286``、``v2.3.1``：编号不被切碎，也不会被当成更长编号的子串
    命中）。含非 ASCII 字符时先按连接符、再按文字（拉丁 / 其他）切段：带重音的拉丁词整词
    保留（``café``、``müller``、``résumé``），相邻的纯 ASCII 段仍按标识符拼回去
    （``café-np-4286`` → ``café`` + ``np-4286``，``μmol/l`` → ``μ`` + ``mol/l``）。
    """
    if run.isascii():
        return [run]
    script = _script_pattern()
    parts = _JOINER.split(run)
    out: list[str] = []
    for index in range(0, len(parts), 2):
        joiner = parts[index - 1] if index else ""
        for position, piece in enumerate(script.findall(parts[index])):
            if (position == 0 and joiner and out and out[-1].isascii()
                    and piece.isascii()):
                out[-1] += joiner + piece
            else:
                out.append(piece)
    return out


class DefaultTokenizer:
    """零依赖分词：NFC + casefold；词按 Unicode 字母切分；CJK 单字 + 相邻二字。

    - 词：字母、数字和组合附加符号连成的一段不拆开。纯 ASCII 的段连同 ``-_./`` 连接符
      整段保留为一个标识符；含非 ASCII 字母的词（``café``、``Müller``）同样整词保留。
    - CJK：单字让「猫」「辣」这种一字查询也能命中；二字让「体检」「医生」比两个单字各自
      撞上更有分量（二字的 IDF 通常高得多）。和语法助词相邻的二字不生成。

    ``mg-default-v1`` 先切 ASCII 标识符再处理其余文字，重音拉丁词会从 ASCII 与非 ASCII 的
    交界处劈开（``café`` → ``caf`` + ``é``，``résumé`` → ``r`` + ``sum`` + ``é``，
    ``Müller`` → ``m`` + ``ü`` + ``ller``）：碎片互相撞上（「é」出现在几乎每个法语词里），
    法语、德语查询得到不相干的命中。v2 按整词切；纯 ASCII、CJK 文本的 token 与 v1 相同
    （例外：NFC 会把 CJK 兼容表意字 U+F900–U+FAFF 归一成对应的统一表意字）。
    """

    name = "mg-default-v2"

    def tokenize(self, text: str) -> list[str]:
        out: list[str] = []
        folded = str(text or "")
        if not folded.isascii():
            folded = unicodedata.normalize("NFC", folded)
        folded = folded.casefold()
        for is_word, run in _segments(folded):
            if is_word:
                out.extend(_word_tokens(run))
                continue
            out.extend(run)
            out.extend(run[i:i + 2] for i in range(len(run) - 1)
                       if run[i] not in _PARTICLES and run[i + 1] not in _PARTICLES)
        return out


_DEFAULT = DefaultTokenizer()


@dataclass(frozen=True)
class Hit:
    id: str
    score: float
    #: 命中的查询 token（排序后）。**是用户文本的片段**，宿主落日志前自己决定要不要留。
    matched: tuple[str, ...] = ()
    #: 命中的查询 token 占查询 IDF 总量的比例（0–1）。花园里一张都没有的查询词也算进分母
    #: —— 「冰岛」没人写过，这正是「没记过」的信号。
    coverage: float = 0.0


@dataclass(frozen=True)
class RankResult:
    #: 已排序；无命中就是空列表 —— 不补位、不兜底。
    hits: list[Hit] = field(default_factory=list)
    version: str = ""
    #: 内容无关：计数和版本，没有一个字是用户内容。
    trace: dict = field(default_factory=dict)

    @property
    def ids(self) -> list[str]:
        return [hit.id for hit in self.hits]


def default_search_text(card: Mapping[str, Any]) -> str:
    """一张卡用来匹配的文本。

    宿主显式给了 ``search_text`` 就用它 —— 宿主最清楚哪些字段可搜
    （和 ``scoring.relevance`` 的约定一致）。没给就拼 summary、content、bucket、
    threads 和规范化后的 ``retrieval_cues``；完全相同的字段只算一次，
    免得 summary == content 的卡词频翻倍。
    """
    explicit = card.get("search_text")
    if isinstance(explicit, str) and explicit.strip():
        return explicit
    fields: list[Any] = [card.get(key) for key in ("summary", "content", "bucket")]
    threads = card.get("threads")
    if isinstance(threads, (list, tuple)):
        fields.extend(t for t in threads if isinstance(t, str))
    fields.extend(retrieval_cues(card.get("retrieval_cues")))
    return "\n".join(dict.fromkeys(str(v) for v in fields if v))


@dataclass(frozen=True)
class _Corpus:
    documents: int
    total_length: int
    document_frequency: Counter


def _idf(documents: int, df: int) -> float:
    return math.log1p((documents - df + 0.5) / (df + 0.5))


def _score(terms: Counter, length: int, query: Sequence[str], corpus: _Corpus,
           idf: Mapping[str, float], k1: float, b: float) -> float:
    """BM25，非负 Robertson IDF。表达式和求和顺序照抄 io，浮点结果逐位相同。"""
    average = corpus.total_length / corpus.documents
    normalizer = k1 * (1.0 - b + b * length / average)
    value = 0.0
    for term in query:  # 调用方已去重并排序
        frequency = terms.get(term, 0)
        if not frequency:
            continue
        value += idf[term] * frequency * (k1 + 1.0) / (frequency + normalizer)
    return value


def _occurred_ts(card: Mapping[str, Any]) -> float:
    valid, parsed = timestamps.sort_key(str(card.get("occurred_at") or ""))
    return parsed.timestamp() if valid else float("-inf")


def _version(tokenizer_name: str, extra: Mapping[str, Any]) -> str:
    base = f"{RANKING_VERSION}+tok:{tokenizer_name}"
    if not extra:
        return base
    digest = hashlib.sha256(repr(sorted(extra.items())).encode("utf-8")).hexdigest()[:8]
    return f"{base}+cfg:{digest}"


def rank(
    query: str,
    candidates: Sequence[Mapping[str, Any]],
    *,
    tokenizer: Tokenizer | None = None,
    limit: int | None = None,
    text_of: Callable[[Mapping[str, Any]], str] = default_search_text,
    min_score: float = 0.0,
    stopwords: Iterable[str] | None = None,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    strong_evidence: float = DEFAULT_STRONG_EVIDENCE,
    strong_evidence_terms: int | None = DEFAULT_STRONG_EVIDENCE_TERMS,
    coverage_pool_floor: int = DEFAULT_COVERAGE_POOL_FLOOR,
    k1: float = K1,
    b: float = B,
    max_cards: int | None = None,
    max_text_bytes: int | None = None,
) -> RankResult:
    """按和 ``query`` 的词法相关性给候选排序。

    ``candidates`` 必须是宿主**已经过完权限和生命周期过滤**的卡（带 ``id``）；
    这里不认识 owner、mount、归档状态。IDF 按这批候选算，所以候选池是谁决定了尺子。

    ## 什么算命中

    BM25 只要有一个词重叠就给正分 —— 「我」「什么」「喜欢」就能把不相干的卡带进来，
    查一个花园里根本没有的东西也几乎总有结果。所以打分之后还有两道闸，
    一张卡要进结果必须同时满足：

    1. 分数 > ``min_score``（默认 0，即 BM25 原义）；
    2. **覆盖率** ≥ ``min_coverage``（命中词占查询 IDF 总量的比例），
       **或者**分数 ≥ ``strong_evidence`` × 本批候选里最大可能的单词 IDF
       （「证据超过一个独有词」—— 长段粘贴时覆盖率天然很低，靠这条放行）；
       给了 ``strong_evidence_terms`` 且查询超过这么多个 token 时，这道闸再乘
       √(token 数 / terms)（默认不放大，取舍见 :data:`DEFAULT_STRONG_EVIDENCE_TERMS`）。

    覆盖率的 IDF 至少按 ``coverage_pool_floor`` 张卡算（小花园里缺失词的 IDF 不会压倒命中词，
    见 :data:`DEFAULT_COVERAGE_POOL_FLOOR`）；传 1 即按真实候选数算。

    ``stopwords`` 默认 :data:`DEFAULT_STOPWORDS`，只从**查询**里去掉（卡片侧统计不变，
    换停用词表不改变其余词的分数）。无命中返回空 ``hits``。

    要逐项复现旧的无门槛 BM25：``stopwords=frozenset(), min_coverage=0``。

    没有 ``id`` 的卡不参与打分、也不进 IDF 统计（与 :func:`select_context` 一致）——
    命中一张没有 id 的卡，宿主拿到的是一个空 id，回填不了也引用不了。

    上限（``max_cards`` / ``max_text_bytes``）超了抛 :class:`SearchLimitExceeded`，
    即使查询为空也检查 —— 资源边界不能因为输入碰巧为空就不生效。
    """
    if max_cards is not None and len(candidates) > max_cards:
        raise SearchLimitExceeded("cards")
    pool = [c for c in candidates if str(c.get("id") or "")]
    scored, rejected, version, trace = _evaluate(
        query, pool, tokenizer=tokenizer, text_of=text_of, min_score=min_score,
        stopwords=stopwords, min_coverage=min_coverage, strong_evidence=strong_evidence,
        strong_evidence_terms=strong_evidence_terms, coverage_pool_floor=coverage_pool_floor,
        k1=k1, b=b, max_cards=max_cards, max_text_bytes=max_text_bytes)
    trace = {**trace, "candidates": len(candidates), "without_id": len(candidates) - len(pool)}
    if limit is not None:
        scored = scored[:max(0, int(limit))]
    hits = [row.hit for row in scored]
    return RankResult(hits=hits, version=version, trace={**trace, "returned": len(hits)})


@dataclass(frozen=True)
class _Row:
    hit: Hit
    card: Mapping[str, Any]
    #: 没进结果的原因：``below_min_score`` / ``below_gate``；进了结果为空串。
    reason: str = ""


def _evaluate(query, candidates, *, tokenizer, text_of, min_score, stopwords, min_coverage,
              strong_evidence, strong_evidence_terms, coverage_pool_floor, k1, b, max_cards,
              max_text_bytes):
    """打分 + 过闸，返回（通过的行按序、被闸挡下的行按序、版本号、trace）。

    ``rank`` 和 ``select_context`` 共用这一份 —— 两条路是**同一把尺子**的结构保证。
    """
    tok = tokenizer or _DEFAULT
    for label, value in (("min_score", min_score), ("min_coverage", min_coverage),
                         ("strong_evidence", strong_evidence)):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{label} must be finite and non-negative")
    if strong_evidence_terms is not None and (
            isinstance(strong_evidence_terms, bool) or not isinstance(strong_evidence_terms, int)
            or strong_evidence_terms < 1):
        raise ValueError("strong_evidence_terms must be a positive int or None")
    if (isinstance(coverage_pool_floor, bool) or not isinstance(coverage_pool_floor, int)
            or coverage_pool_floor < 1):
        raise ValueError("coverage_pool_floor must be a positive int")
    if max_cards is not None and len(candidates) > max_cards:
        raise SearchLimitExceeded("cards")

    stop = frozenset(stopwords) if stopwords is not None else DEFAULT_STOPWORDS
    raw_terms = tok.tokenize(str(query or ""))
    query_terms = sorted(set(raw_terms) - stop)

    config: dict[str, Any] = {}
    if stop != DEFAULT_STOPWORDS:
        config["stopwords"] = sorted(stop)
    if min_score:
        config["min_score"] = min_score
    if (min_coverage, strong_evidence) != (DEFAULT_MIN_COVERAGE, DEFAULT_STRONG_EVIDENCE):
        config["min_coverage"], config["strong_evidence"] = min_coverage, strong_evidence
    if strong_evidence_terms != DEFAULT_STRONG_EVIDENCE_TERMS:
        config["strong_evidence_terms"] = strong_evidence_terms
    if coverage_pool_floor != DEFAULT_COVERAGE_POOL_FLOOR:
        config["coverage_pool_floor"] = coverage_pool_floor
    if (k1, b) != (K1, B):
        config["k1"], config["b"] = k1, b
    version = _version(str(getattr(tok, "name", "") or type(tok).__name__), config)
    trace: dict[str, Any] = {
        "version": version,
        "candidates": len(candidates),
        "query_tokens": len(set(raw_terms)),
        "query_stopwords": len(set(raw_terms) & stop),
    }

    documents: list[tuple[Counter, int]] = []
    total_bytes = 0
    for card in candidates:
        text = text_of(card)
        if max_text_bytes is not None:
            total_bytes += len(text.encode("utf-8"))
            if total_bytes > max_text_bytes:
                raise SearchLimitExceeded("text_bytes")
        if query_terms:
            terms = Counter(tok.tokenize(text))
            documents.append((terms, sum(terms.values())))

    empty = {"evidence_scale": 1.0, "matched": 0, "below_min_score": 0, "below_gate": 0}
    if not query_terms or not documents:
        return [], [], version, {**trace, **empty}

    frequency: Counter = Counter()
    total_length = 0
    for terms, length in documents:
        total_length += length
        frequency.update(terms.keys())
    corpus = _Corpus(len(documents), total_length, frequency)
    if not corpus.total_length:
        return [], [], version, {**trace, **empty}
    idf = {term: _idf(corpus.documents, frequency[term]) for term in query_terms}
    # 覆盖率用的 IDF：候选池按至少 coverage_pool_floor 张算。池子够大时就是 idf 本身。
    pool_size = max(corpus.documents, coverage_pool_floor)
    coverage_idf = (idf if pool_size == corpus.documents else
                    {term: _idf(pool_size, frequency[term]) for term in query_terms})
    query_mass = sum(coverage_idf.values())
    evidence_scale = 1.0
    if strong_evidence_terms is not None and len(query_terms) > strong_evidence_terms:
        evidence_scale = math.sqrt(len(query_terms) / strong_evidence_terms)
    strong_floor = strong_evidence * _idf(corpus.documents, 0) * evidence_scale

    passed: list[_Row] = []
    rejected: list[_Row] = []
    for card, (terms, length) in zip(candidates, documents):
        if not any(term in terms for term in query_terms):
            continue
        value = _score(terms, length, query_terms, corpus, idf, k1, b)
        if value <= 0:
            continue
        matched = tuple(term for term in query_terms if term in terms)
        coverage = (sum(coverage_idf[term] for term in matched) / query_mass
                    if query_mass else 0.0)
        hit = Hit(id=str(card.get("id") or ""), score=value, matched=matched,
                  coverage=round(coverage, 6))
        if value <= min_score:
            rejected.append(_Row(hit, card, "below_min_score"))
        elif coverage < min_coverage and value < strong_floor:
            rejected.append(_Row(hit, card, "below_gate"))
        else:
            passed.append(_Row(hit, card))

    def order(row: _Row):
        return (-row.hit.score, -_occurred_ts(row.card), row.hit.id)

    passed.sort(key=order)
    rejected.sort(key=order)
    return passed, rejected, version, {
        **trace,
        "evidence_scale": round(evidence_scale, 4),
        "matched": len(passed) + len(rejected),
        "below_min_score": sum(r.reason == "below_min_score" for r in rejected),
        "below_gate": sum(r.reason == "below_gate" for r in rejected),
    }

#: hybrid 融合的算法初始值（与 :mod:`memgarden.scoring.hybrid` 相同）；不是在宿主数据上
#: 验证过的最佳参数。``min_cosine`` 故意没有默认值。
DEFAULT_RRF_K = 20
DEFAULT_VECTOR_WEIGHT = 2.0
DEFAULT_LEXICAL_WEIGHT = 1.0
#: 自动想起的默认软配额：（角色或 ``recent``，最多几张）。和
#: ``scoring.relevance.select_relevant_context_memories_with_trace`` 相同。
DEFAULT_QUOTAS: tuple[tuple[str, int], ...] = (("turning_point", 3), ("recent", 2))
ROLE_TURNING_POINT = "turning_point"
#: trace 里的段名沿用旧实现（turning / recent / query），宿主的日志口径不用改。
_BUCKET_LABELS = {ROLE_TURNING_POINT: "turning", "recent": "recent"}


def select_context(
    query: str,
    candidates: Sequence[Mapping[str, Any]],
    *,
    tokenizer: Tokenizer | None = None,
    cap: int = 8,
    quotas: Sequence[tuple[str, int]] = DEFAULT_QUOTAS,
    text_of: Callable[[Mapping[str, Any]], str] = default_search_text,
    stopwords: Iterable[str] | None = None,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    strong_evidence: float = DEFAULT_STRONG_EVIDENCE,
    strong_evidence_terms: int | None = DEFAULT_STRONG_EVIDENCE_TERMS,
    coverage_pool_floor: int = DEFAULT_COVERAGE_POOL_FLOOR,
    max_cards: int | None = None,
    max_text_bytes: int | None = None,
    query_vector: Sequence[float] | None = None,
    card_vectors: Mapping[str, Sequence[float]] | None = None,
    min_cosine: float | None = None,
    vector_model: str | None = None,
    card_vector_models: Mapping[str, str] | None = None,
    vector_weight: float = DEFAULT_VECTOR_WEIGHT,
    lexical_weight: float = DEFAULT_LEXICAL_WEIGHT,
    rrf_k: int = DEFAULT_RRF_K,
) -> tuple[list[dict], dict]:
    """自动想起：这一轮该带哪几张卡进上下文。

    和 :func:`rank` **用同一把尺子**（同一个打分、同一道门槛、同一个版本号）。
    在它之上只多两件事，语义照搬 ``scoring.relevance.select_relevant_context_memories_with_trace``：

    1. **每一张都必须先过相关性门槛** —— 转折点、最近的卡也不例外，不相关的不会
       为了凑配额混进来。
    2. **软配额**：``quotas`` 依次给某个角色（按 ``occurred_at`` 新的在前）或
       ``recent``（按 ``created_at`` 新的在前）留座位；配额没用满的空位按分数补给其余合格的卡。
       总数不超过 ``cap``。

    返回 ``(卡片副本列表, trace)``，形状同旧函数：每张副本多一个 ``selection`` 字段；
    trace 里 ``selected[].bucket`` 是 turning / recent / query。trace 内容无关（id、分数、计数）。

    和旧实现有意不同的地方：

    - 打分换成 BM25 + 门槛（旧的是短语/稀有词规则 + 0.35 门槛）；
    - 返回列表**按分数排序**，段名只在 ``selection.bucket`` / trace 里标注
      （旧实现按段排列）；配额决定的是哪些卡有座位，这一点不变；
    - 分数并列时 id **升序**（与 :func:`rank` 一致，旧实现是降序）；
    - 没有 ``id`` 的卡不参与打分，也不进 IDF 统计。

    **向量（hybrid）**：宿主给了 ``query_vector`` 就多一条向量通道 —— 每张有向量的候选
    算余弦，过 ``min_cosine``（没有默认值，必须按宿主的模型和语料标定）的卡按余弦排名；
    词法通道就是上面这把 BM25 尺子过闸后的排名。两条排名用加权 RRF
    （``w_v/(k+r_v) + w_l/(k+r_l)``，见 :mod:`memgarden.scoring.hybrid`）融合，**任一通道
    合格即为候选**：换了说法、一个词都不重合的卡可以只靠向量进来；没有向量的卡只走词法。
    配额与座位规则不变，座位上的顺序按融合分。不给 ``query_vector`` 时行为与结果
    **逐字节不变**（trace 不多一个键）；给了向量但查询为空仍返回空 —— 向量不能替空轮
    偷偷注入记忆。
    """
    cap = max(0, int(cap))
    if max_cards is not None and len(candidates) > max_cards:
        raise SearchLimitExceeded("cards")
    pool = [c for c in candidates if str(c.get("id") or "")]
    passed, rejected, version, rank_trace = _evaluate(
        query, pool, tokenizer=tokenizer, text_of=text_of, min_score=0.0,
        stopwords=stopwords, min_coverage=min_coverage, strong_evidence=strong_evidence,
        strong_evidence_terms=strong_evidence_terms, coverage_pool_floor=coverage_pool_floor,
        k1=K1, b=B, max_cards=max_cards, max_text_bytes=max_text_bytes)

    hybrid = query_vector is not None
    if hybrid:
        ordered, score_of, extra_of, hybrid_trace, leftovers = _fuse_lanes(
            query, pool, passed, rejected, query_vector=query_vector,
            card_vectors=card_vectors or {}, min_cosine=min_cosine, vector_model=vector_model,
            card_vector_models=card_vector_models, vector_weight=vector_weight,
            lexical_weight=lexical_weight, rrf_k=rrf_k)
        reason = "hybrid_rrf"
    else:
        ordered = passed
        score_of = {row.hit.id: row.hit.score for row in passed}
        extra_of = {}
        hybrid_trace = {}
        leftovers = None
        reason = "bm25_match"

    chosen: list[tuple[_Row, str]] = []
    seen: set[str] = set()

    def take(rows: Sequence[_Row], quota: int, bucket: str) -> None:
        added = 0
        for row in rows:
            if len(chosen) >= cap or added >= quota:
                return
            if row.hit.id in seen:
                continue
            seen.add(row.hit.id)
            chosen.append((row, bucket))
            added += 1

    for name, quota in quotas:
        name, quota = str(name), max(0, int(quota))
        if name == "recent":
            rows = sorted(ordered, key=lambda r: (timestamps.sort_key(r.card.get("created_at")),
                                                  _neg_id(r.hit.id)), reverse=True)
        else:
            rows = sorted((r for r in ordered if name in (r.card.get("roles") or [])),
                          key=lambda r: (timestamps.sort_key(r.card.get("occurred_at")),
                                         _neg_id(r.hit.id)), reverse=True)
        take(rows, quota, _BUCKET_LABELS.get(name, name))
    take(ordered, cap, "query")
    # 配额决定**谁有座位**，座位上的顺序按分数（和 rank 同序）。旧实现按段排
    # （转折 → 最近 → 相关），最相关的卡可能排在第 6 位；评测里 MRR 0.70 → 0.89，
    # 选中的集合不变。
    position = {row.hit.id: index for index, row in enumerate(ordered)}
    chosen.sort(key=lambda pair: position[pair[0].hit.id])

    selected = []
    trace_selected = []
    for row, bucket in chosen:
        out = dict(row.card)
        out["selection"] = {
            "score": score_of[row.hit.id], "coverage": row.hit.coverage, "bucket": bucket,
            "reason": reason, "matched_units": list(row.hit.matched)[:8],
            "version": version, **extra_of.get(row.hit.id, {}),
        }
        selected.append(out)
        trace_selected.append({"id": row.hit.id, "bucket": bucket,
                               "score": round(score_of[row.hit.id], 4),
                               "coverage": row.hit.coverage, "reason": reason,
                               "selected": True, **extra_of.get(row.hit.id, {})})

    if leftovers is None:
        leftovers = [(r, "over_cap") for r in passed if r.hit.id not in seen]
        leftovers += [(r, r.reason) for r in rejected]
        leftovers.sort(key=lambda pair: (-pair[0].hit.score, pair[0].hit.id))
    else:
        leftovers = [(r, why) for r, why in leftovers if r.hit.id not in seen]
    trace = {
        **rank_trace,
        "mode": "hybrid" if hybrid else "bm25",
        "cap": cap,
        "quotas": [[str(n), int(q)] for n, q in quotas],
        "index_count": len(pool),
        "eligible": len(ordered),
        **hybrid_trace,
        "selected": trace_selected,
        "rejected_sample": [
            {"id": r.hit.id, "bucket": "rejected",
             "score": round(score_of.get(r.hit.id, r.hit.score), 4),
             "coverage": r.hit.coverage, "reason": why, "selected": False}
            for r, why in leftovers[:8]
        ],
    }
    return selected, trace


def _fuse_lanes(query, pool, passed, rejected, *, query_vector, card_vectors, min_cosine,
                vector_model, card_vector_models, vector_weight, lexical_weight, rrf_k):
    """向量通道 + 词法通道 → 加权 RRF。返回
    （融合顺序的行、id→融合分、id→额外字段、trace 片段、落选样本）。

    词法通道的输入就是 ``_evaluate`` 过闸后的 ``passed``（同一把尺子）；只被闸挡下的
    卡若靠向量进来，保留它的 BM25 分和命中词，不重打分。
    """
    from .scoring import hybrid as _hybrid  # 纯计算模块；放函数内避免导入环

    if min_cosine is None or not math.isfinite(min_cosine) or not -1 <= min_cosine <= 1:
        raise ValueError("min_cosine is required with query_vector and must be within [-1, 1]")
    for name, w in (("vector_weight", vector_weight), ("lexical_weight", lexical_weight)):
        if not math.isfinite(w) or w < 0:
            raise ValueError(f"{name} must be finite and >= 0")
    if (vector_model is None) != (card_vector_models is None):
        raise _hybrid.VectorContractError(
            "vector_model and card_vector_models must be provided together")

    by_id = {str(c["id"]): c for c in pool}
    lex_row = {row.hit.id: row for row in passed}
    gated_row = {row.hit.id: row for row in rejected}
    lex_rank = {row.hit.id: i + 1 for i, row in enumerate(passed)}

    vec_score: dict[str, float] = {}
    vector_lane = "active"
    if not str(query or "").strip():
        # 空轮：词法通道本来就空，向量也不许单独把卡塞进来。
        vector_lane = "skipped"
    else:
        qv = _hybrid._as_float_vector(query_vector, what="query_vector")
        for cid in by_id:
            raw = card_vectors.get(cid)
            if raw is None:
                continue
            if vector_model is not None and card_vector_models is not None:
                cm = card_vector_models.get(cid)
                if cm != vector_model:
                    raise _hybrid.VectorContractError(
                        f"card {cid}: vector model {cm!r} != query model {vector_model!r}")
            vec_score[cid] = _hybrid.cosine(qv, _hybrid._as_float_vector(
                raw, what=f"card_vectors[{cid}]"))
    vec_sorted = sorted(
        (cid for cid, s in vec_score.items() if s >= min_cosine),
        key=lambda cid: (-vec_score[cid], -_occurred_ts(by_id[cid]), cid))
    vec_rank = {cid: i + 1 for i, cid in enumerate(vec_sorted)}

    fused = _hybrid.rrf_fuse({"lexical": lex_rank, "vector": vec_rank},
                             {"lexical": lexical_weight, "vector": vector_weight}, k=rrf_k)

    def row_for(cid: str) -> _Row:
        if cid in lex_row:
            return lex_row[cid]
        if cid in gated_row:
            return _Row(gated_row[cid].hit, gated_row[cid].card)
        return _Row(Hit(id=cid, score=0.0), by_id[cid])

    ordered = sorted((row_for(cid) for cid in fused),
                     key=lambda r: (-fused[r.hit.id], -_occurred_ts(r.card), r.hit.id))
    extra = {cid: {"bm25": round(row_for(cid).hit.score, 4),
                   "cosine": (round(vec_score[cid], 4) if cid in vec_score else None),
                   "lanes": {"lexical": lex_rank.get(cid), "vector": vec_rank.get(cid)}}
             for cid in fused}

    leftovers = [(r, "over_cap") for r in ordered]
    leftovers += [(r, "below_cosine") for r in
                  (_Row(Hit(id=cid, score=0.0), by_id[cid]) for cid in vec_score
                   if cid not in vec_rank and cid not in fused)]
    leftovers += [(r, r.reason) for r in rejected if r.hit.id not in fused]
    leftovers.sort(key=lambda pair: (-fused.get(pair[0].hit.id, 0.0), -pair[0].hit.score,
                                     pair[0].hit.id))
    trace = {
        "vector_lane": vector_lane,
        "hybrid": {
            "rrf_k": rrf_k, "vector_weight": vector_weight, "lexical_weight": lexical_weight,
            "min_cosine": min_cosine, "vector_model": vector_model,
            "with_vector": len(vec_score), "vector_eligible": len(vec_rank),
            "lexical_eligible": len(passed), "fused": len(fused),
            "vector_only": sum(1 for cid in fused if cid not in lex_rank),
        },
    }
    return ordered, fused, extra, trace, leftovers

def _neg_id(card_id: str) -> tuple[int, ...]:
    """倒序排序里让 id **升序**的键 —— 并列时和 :func:`rank` 同一个方向。"""
    return tuple(-ord(ch) for ch in card_id) + (1,)


__all__ = [
    "RANKING_VERSION", "DEFAULT_STOPWORDS", "DEFAULT_MIN_COVERAGE", "DEFAULT_STRONG_EVIDENCE",
    "DEFAULT_STRONG_EVIDENCE_TERMS", "DEFAULT_COVERAGE_POOL_FLOOR",
    "Tokenizer", "DefaultTokenizer",
    "Hit", "RankResult", "SearchLimitExceeded", "default_search_text", "rank",
    "DEFAULT_QUOTAS", "select_context",
]
