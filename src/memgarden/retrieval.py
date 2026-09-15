"""词法排序 —— 自动想起和主动搜索共用的一把尺子。

## 为什么是 BM25、为什么分词器是插口

宿主 io 线上跑着两套排序：自动想起用内核的相关性打分（``scoring.relevance``），
主动搜索用 io 自己的 BM25 + jieba。同一个问题两条路给出不同答案，trace 里也对不上。
``evals/retrieval`` 的基线显示 BM25 的召回全面更好（recall@5 0.90 vs 0.67），
编号、短查询、线程类尤其明显，所以统一到 BM25。

数学逐行移植自 io ``backend/memory_bm25.py``（Robertson IDF 取 ``log1p``、查询里
重复的 token 只算一次、同分按 ``occurred_at`` 新的在前再按 id）。**给同一个分词器，
分数与排序逐项相同** —— ``tests/test_retrieval_rank.py`` 用一份照抄的参考实现对拍。

分词是唯一依赖语言资源的一步。内核只依赖标准库，所以分词器由宿主注入
（io 注入 jieba）；不注入时用 :class:`DefaultTokenizer`：整段 ASCII 标识符 +
CJK 单字与相邻二字。它不承诺和 jieba 等价，质量以 ``evals/retrieval`` 的数字为准。

## 缓存只活在一次调用里

查询分析一次、每张卡分析一次，调用结束全部丢掉。**不缓存用户文本、倒排和分数**
—— 卡片随时会被改、被删、被换 owner，跨调用的缓存就是跨调用的泄漏和陈旧。

## 词法方法的边界

换说法、跨语言（「猫咪的肾脏问题」对「肾指标偏高」）拿不到，这是 BM25 的边界，
不是 bug。它也不证明答案正确，只证明用词重叠。
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from . import timestamps
from .prompts.recall_fields import retrieval_cues

#: 排序算法的版本。换公式、换默认停用词表、换默认分数下限都要改它 ——
#: 宿主靠它判断两条路是不是同一把尺子、线上 trace 属于哪一版。
RANKING_VERSION = "memgarden-bm25-v1"

K1 = 1.2
B = 0.75

#: 整段 ASCII 标识符：``np-4286``、``v2.3.1``、``k/d``、``x100v``。沿用 io 的写法，
#: 编号不被切成碎片，也不会被当成更长编号的子串命中。
_ASCII_TOKEN = re.compile(r"([a-z0-9]+(?:[-_./][a-z0-9]+)*)")
#: 汉字（含扩展 A、兼容区）、假名、谚文。这些文字不用空格分词，按字处理。
_CJK_CHAR = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af"
_SEGMENT = re.compile(rf"([{_CJK_CHAR}]+)|([^\W_{_CJK_CHAR}]+)")


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


class DefaultTokenizer:
    """零依赖分词：casefold；整段 ASCII 标识符；CJK 单字 + 相邻二字；其余文字按词。

    单字让「猫」「辣」这种一字查询也能命中；二字让「体检」「医生」比两个单字各自
    撞上更有分量（二字的 IDF 通常高得多）。
    """

    name = "mg-default-v1"

    def tokenize(self, text: str) -> list[str]:
        out: list[str] = []
        for index, part in enumerate(_ASCII_TOKEN.split(str(text or "").casefold())):
            if not part:
                continue
            if index % 2:
                out.append(part)
                continue
            for match in _SEGMENT.finditer(part):
                run, word = match.group(1), match.group(2)
                if word:
                    out.append(word)
                    continue
                out.extend(run)
                out.extend(run[i:i + 2] for i in range(len(run) - 1))
        return out


_DEFAULT = DefaultTokenizer()


@dataclass(frozen=True)
class Hit:
    id: str
    score: float
    #: 命中的查询 token（排序后）。**是用户文本的片段**，宿主落日志前自己决定要不要留。
    matched: tuple[str, ...] = ()


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
    k1: float = K1,
    b: float = B,
    max_cards: int | None = None,
    max_text_bytes: int | None = None,
) -> RankResult:
    """按和 ``query`` 的词法相关性给候选排序。

    ``candidates`` 必须是宿主**已经过完权限和生命周期过滤**的卡（带 ``id``）；
    这里不认识 owner、mount、归档状态。IDF 按这批候选算，所以候选池是谁决定了尺子。

    只有分数 > ``min_score`` 的卡进结果；无命中返回空 ``hits``。``stopwords`` 只从
    **查询**里去掉（卡片侧统计不变，所以换停用词表不改变其余词的分数）。

    上限（``max_cards`` / ``max_text_bytes``）超了抛 :class:`SearchLimitExceeded`，
    即使查询为空也检查 —— 资源边界不能因为输入碰巧为空就不生效。
    """
    tok = tokenizer or _DEFAULT
    if not math.isfinite(min_score) or min_score < 0:
        raise ValueError("min_score must be finite and non-negative")
    if max_cards is not None and len(candidates) > max_cards:
        raise SearchLimitExceeded("cards")

    stop = frozenset(stopwords) if stopwords is not None else frozenset()
    raw_terms = tok.tokenize(str(query or ""))
    query_terms = sorted(set(raw_terms) - stop)

    config: dict[str, Any] = {}
    if stop:
        config["stopwords"] = sorted(stop)
    if min_score:
        config["min_score"] = min_score
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

    if not query_terms or not documents:
        return RankResult(hits=[], version=version,
                          trace={**trace, "matched": 0, "below_min_score": 0, "returned": 0})

    frequency: Counter = Counter()
    total_length = 0
    for terms, length in documents:
        total_length += length
        frequency.update(terms.keys())
    corpus = _Corpus(len(documents), total_length, frequency)
    if not corpus.total_length:
        return RankResult(hits=[], version=version,
                          trace={**trace, "matched": 0, "below_min_score": 0, "returned": 0})
    idf = {term: _idf(corpus.documents, frequency[term]) for term in query_terms}

    scored = []
    below = 0
    for card, (terms, length) in zip(candidates, documents):
        if not any(term in terms for term in query_terms):
            continue
        value = _score(terms, length, query_terms, corpus, idf, k1, b)
        if value <= 0:
            continue
        if value <= min_score:
            below += 1
            continue
        matched = tuple(term for term in query_terms if term in terms)
        scored.append((value, card, matched))

    scored.sort(key=lambda row: (-row[0], -_occurred_ts(row[1]), str(row[1].get("id") or "")))
    matched_count = len(scored) + below
    if limit is not None:
        scored = scored[:max(0, int(limit))]
    hits = [Hit(id=str(card.get("id") or ""), score=value, matched=matched)
            for value, card, matched in scored]
    return RankResult(hits=hits, version=version,
                      trace={**trace, "matched": matched_count, "below_min_score": below,
                             "returned": len(hits)})


__all__ = [
    "RANKING_VERSION", "Tokenizer", "DefaultTokenizer",
    "Hit", "RankResult", "SearchLimitExceeded", "default_search_text", "rank",
]
