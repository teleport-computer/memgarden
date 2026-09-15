"""memgarden.retrieval.rank —— 统一排序器的数学、顺序、边界与分词。

对拍的参考实现照抄自 io ``backend/memory_bm25.py``（release/memory-overhaul @6f785b1e）。
**不 import io**：内核仓库不能依赖宿主；抄一份冻结的参考实现，才能在 io 改动时
仍然知道「移植那一刻」两边是否一致。给同一个分词器，分数必须逐位相等（用 ``==``，
不用 approx —— approx 会放过求和顺序变了这种真实差异）。
"""
from __future__ import annotations

import math
import pathlib
import random
import sys
from collections import Counter

import pytest

_SRC_PATH = str(pathlib.Path(__file__).resolve().parent.parent / "src")
if _SRC_PATH not in sys.path:
    sys.path.insert(0, _SRC_PATH)

from memgarden import retrieval, timestamps  # noqa: E402
from memgarden.retrieval import (  # noqa: E402
    DefaultTokenizer,
    SearchLimitExceeded,
    default_search_text,
    rank,
)


# --------------------------------------------------------------------------- #
# 参考实现（io memory_bm25.py 的 corpus_stats / score / rank，分词器换成注入）
# --------------------------------------------------------------------------- #

def _ref_rank(items, query, tokenize, text_of):
    k1, b = 1.2, 0.75
    query_terms = tokenize(query)
    documents = []
    for item in items:
        text = text_of(item)
        if query_terms:
            documents.append(Counter(tokenize(text)))
    if not query_terms:
        return []
    count, length_total, frequencies = 0, 0, Counter()
    for terms in documents:
        count += 1
        length_total += sum(terms.values())
        frequencies.update(terms.keys())

    def score(terms):
        if not count or not length_total:
            return 0.0
        length = sum(terms.values())
        average = length_total / count
        normalizer = k1 * (1.0 - b + b * length / average)
        value = 0.0
        for term in sorted(set(query_terms)):
            frequency = terms.get(term, 0)
            if not frequency:
                continue
            df = frequencies[term]
            idf = math.log1p((count - df + 0.5) / (df + 0.5))
            value += idf * frequency * (k1 + 1.0) / (frequency + normalizer)
        return value

    def occurred(item):
        valid, parsed = timestamps.sort_key(str(item.get("occurred_at") or ""))
        return parsed.timestamp() if valid else float("-inf")

    scored = [(item, score(terms)) for item, terms in zip(items, documents)]
    return sorted((p for p in scored if p[1] > 0),
                  key=lambda p: (-p[1], -occurred(p[0]), str(p[0].get("id") or "")))


class Whitespace:
    name = "ws"

    def tokenize(self, text):
        return [t for t in str(text).casefold().split() if t]


def _io_text(item):
    fields = [item.get(k) for k in ("summary", "content", "bucket")]
    fields.extend(item.get("threads") or [])
    return "\n".join(dict.fromkeys(str(v) for v in fields if v))


def test_matches_reference_bm25_exactly_on_random_corpora():
    rng = random.Random(20260915)
    vocab = [f"w{i}" for i in range(40)] + ["needle", "np-4286", "咖啡"]
    times = ["2026-01-01", "2026-02-01T00:00:00Z", "2026-02-01T08:00:00+08:00", "bad", ""]
    compared = 0
    for _ in range(200):
        cards = [{
            "id": f"c{rng.randrange(10_000)}-{i}",
            "summary": " ".join(rng.choices(vocab, k=rng.randint(0, 12))),
            "content": " ".join(rng.choices(vocab, k=rng.randint(0, 30))),
            "bucket": rng.choice(["", "w1", "工作"]),
            "threads": rng.sample(vocab, rng.randint(0, 2)),
            "occurred_at": rng.choice(times),
        } for i in range(rng.randint(0, 40))]
        query = " ".join(rng.choices(vocab, k=rng.randint(0, 5)))
        want = [(c["id"], s) for c, s in _ref_rank(cards, query, Whitespace().tokenize, _io_text)]
        got = rank(query, cards, tokenizer=Whitespace(), text_of=_io_text)
        assert [(h.id, h.score) for h in got.hits] == want
        compared += bool(want)
    assert compared > 100, "对拍样本几乎全是空结果，这条测试没有牙"


# --------------------------------------------------------------------------- #
# io 测试里的样例（照搬断言）
# --------------------------------------------------------------------------- #

def test_nonnegative_idf_and_duplicate_query_tokens_count_once():
    cards = [{"id": "a", "summary": "code"}, {"id": "b", "summary": "code"}]
    one = rank("code", cards, tokenizer=Whitespace())
    assert one.hits[0].score == pytest.approx(math.log(1.2))
    assert rank("code code", cards, tokenizer=Whitespace()).hits == one.hits
    assert rank("missing", cards, tokenizer=Whitespace()).hits == []


def test_noncontiguous_terms_rank_above_partial_and_card_is_not_mutated():
    rows = [{"id": "late", "summary": "coffee grinder needs repair", "score": .01},
            {"id": "early", "summary": "coffee shop", "score": 99},
            {"id": "absent", "summary": "other"}]
    result = rank("repair coffee", rows)
    assert result.ids == ["late", "early"]
    assert rows[0]["score"] == .01
    assert result.hits[0].matched == ("coffee", "repair")


def test_identifiers_are_whole_tokens_not_substrings():
    rows = [{"id": "a", "summary": "NP-4286 CR2450"},
            {"id": "b", "summary": "NP-42860 CR24501"}]
    assert rank("np-4286", rows).ids == ["a"]
    assert rank("CR2450", rows).ids == ["a"]


def test_ties_do_not_depend_on_candidate_order_and_tokenless_is_empty():
    rows = [{"id": "b", "summary": "needle"}, {"id": "a", "summary": "needle"}]
    assert rank("needle", rows).ids == ["a", "b"]
    assert rank("needle", rows).hits == rank("needle", rows[::-1]).hits
    assert rank("💡!!", rows).hits == []
    assert rank("", rows).hits == []
    assert rank("needle", []).hits == []


def test_equal_scores_tie_by_occurred_at_then_id_with_bad_times_last():
    rows = [{"id": mid, "summary": "needle", "occurred_at": when} for mid, when in
            [("a", "2026-01-01T00:00:00Z"), ("b", "2026-02-01T00:00:00Z"),
             ("c", "bad"), ("d", "2026-02-01T08:00:00+08:00")]]
    assert rank("needle", rows).ids == ["b", "d", "a", "c"]


def test_no_stale_state_between_calls():
    row = {"id": "a", "summary": "old value"}
    assert rank("old", [row]).hits
    row["summary"] = "new value"
    assert rank("old", [row]).hits == []
    assert rank("new", []).hits == []
    assert rank("new", [row]).hits


def test_resource_limits_fail_explicitly_even_for_tokenless_queries():
    rows = [{"id": "a", "summary": "needle"}]
    with pytest.raises(SearchLimitExceeded):
        rank("needle", rows, max_text_bytes=3)
    with pytest.raises(SearchLimitExceeded):
        rank("!!!", rows, max_text_bytes=3)
    with pytest.raises(SearchLimitExceeded):
        rank("needle", rows * 3, max_cards=2)
    assert rank("needle", rows, max_cards=1, max_text_bytes=6).ids == ["a"]


# --------------------------------------------------------------------------- #
# 本模块自己的约定
# --------------------------------------------------------------------------- #

def test_default_tokenizer_shapes():
    tok = DefaultTokenizer()
    assert tok.tokenize("NP-4286 / CR2450") == ["np-4286", "cr2450"]
    assert tok.tokenize(" !!! 🪴 \n") == []
    assert tok.tokenize("看医生") == ["看", "医", "生", "看医", "医生"]
    assert tok.tokenize("尿酸480") == ["尿", "酸", "尿酸", "480"]
    assert tok.tokenize("PR #317 v2.3.1 k/d") == ["pr", "317", "v2.3.1", "k/d"]
    assert tok.tokenize("Café") == ["caf", "é"]
    assert tok.tokenize("猫") == ["猫"]


def test_single_cjk_character_query_finds_the_card():
    rows = [{"id": "spicy", "summary": "不吃辣"}, {"id": "other", "summary": "喜欢甜食"}]
    assert rank("辣", rows).ids == ["spicy"]


def test_min_score_and_limit():
    rows = [{"id": "strong", "summary": "alpha beta"}, {"id": "weak", "summary": "alpha gamma delta"},
            {"id": "none", "summary": "zeta"}]
    full = rank("alpha beta", rows)
    assert full.ids == ["strong", "weak"]
    floor = (full.hits[0].score + full.hits[1].score) / 2
    gated = rank("alpha beta", rows, min_score=floor)
    assert gated.ids == ["strong"]
    assert gated.trace["below_min_score"] == 1 and gated.trace["matched"] == 2
    assert rank("alpha beta", rows, limit=1).ids == ["strong"]
    with pytest.raises(ValueError):
        rank("alpha", rows, min_score=float("nan"))


def test_stopwords_are_removed_from_the_query_only():
    rows = [{"id": "a", "summary": "the needle"}, {"id": "b", "summary": "the hay the hay"}]
    assert rank("the", rows, stopwords={"the"}).hits == []
    plain = rank("needle", rows)
    with_stop = rank("the needle", rows, stopwords={"the"})
    # 卡片侧统计不变：去掉查询里的停用词后，剩下的词分数和单独查它完全相同
    assert [(h.id, h.score) for h in with_stop.hits] == [(h.id, h.score) for h in plain.hits]


def test_version_names_the_tokenizer_and_changes_with_config():
    rows = [{"id": "a", "summary": "needle"}]
    base = rank("needle", rows).version
    assert base == f"{retrieval.RANKING_VERSION}+tok:{DefaultTokenizer.name}"
    assert rank("needle", rows, tokenizer=Whitespace()).version.endswith("+tok:ws")
    tuned = rank("needle", rows, min_score=0.5).version
    assert tuned.startswith(base + "+cfg:") and tuned != base
    assert rank("needle", rows, min_score=0.5).version == tuned, "版本号必须确定"


def test_trace_is_content_free():
    secret = "绝密病历编号 AB-99"
    result = rank(secret, [{"id": "a", "summary": secret}])
    assert result.hits
    flat = repr(result.trace)
    for token in DefaultTokenizer().tokenize(secret):
        assert token not in flat.replace(result.version, ""), token


def test_each_card_and_the_query_are_tokenized_exactly_once():
    calls = Counter()

    class Counting(DefaultTokenizer):
        name = "counting"

        def tokenize(self, text):
            calls[text] += 1
            return super().tokenize(text)

    rows = [{"id": str(i), "summary": f"card {i} needle"} for i in range(50)]
    rank("needle card", rows, tokenizer=Counting())
    assert calls["needle card"] == 1
    assert all(calls[f"card {i} needle"] == 1 for i in range(50))
    assert sum(calls.values()) == 51


def test_default_search_text_prefers_host_projection_and_dedupes():
    assert default_search_text({"search_text": "explicit", "summary": "s"}) == "explicit"
    card = {"summary": "same", "content": "same", "bucket": "工作",
            "threads": ["t1", 3], "retrieval_cues": ["  cue  one ", "cue one", 7]}
    assert default_search_text(card) == "same\n工作\nt1\ncue one"
