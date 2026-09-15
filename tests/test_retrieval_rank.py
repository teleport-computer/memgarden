"""memgarden.retrieval.rank —— 统一排序器的数学、顺序、边界与分词。

对拍的参考实现照抄自 io ``backend/memory_bm25.py``（release/memory-overhaul @6f785b1e）。
**不 import io**：内核仓库不能依赖宿主；抄一份冻结的参考实现，才能在 io 改动时
仍然知道「移植那一刻」两边是否一致。给同一个分词器，分数必须逐位相等（用 ``==``，
不用 approx —— approx 会放过求和顺序变了这种真实差异）。
"""
from __future__ import annotations

import functools
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

#: 纯 BM25（io 旧语义）：不去停用词、不设覆盖率闸。数学类测试都走这个，
#: 免得默认门槛的调整把「公式对不对」的测试一起带红。
bm25 = functools.partial(rank, stopwords=frozenset(), min_coverage=0.0)


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
        got = bm25(query, cards, tokenizer=Whitespace(), text_of=_io_text)
        assert [(h.id, h.score) for h in got.hits] == want
        compared += bool(want)
    assert compared > 100, "对拍样本几乎全是空结果，这条测试没有牙"


# --------------------------------------------------------------------------- #
# io 测试里的样例（照搬断言）
# --------------------------------------------------------------------------- #

def test_nonnegative_idf_and_duplicate_query_tokens_count_once():
    cards = [{"id": "a", "summary": "code"}, {"id": "b", "summary": "code"}]
    one = bm25("code", cards, tokenizer=Whitespace())
    assert one.hits[0].score == pytest.approx(math.log(1.2))
    assert bm25("code code", cards, tokenizer=Whitespace()).hits == one.hits
    assert bm25("missing", cards, tokenizer=Whitespace()).hits == []


def test_noncontiguous_terms_rank_above_partial_and_card_is_not_mutated():
    rows = [{"id": "late", "summary": "coffee grinder needs repair", "score": .01},
            {"id": "early", "summary": "coffee shop", "score": 99},
            {"id": "absent", "summary": "other"}]
    result = bm25("repair coffee", rows)
    assert result.ids == ["late", "early"]
    assert rows[0]["score"] == .01
    assert result.hits[0].matched == ("coffee", "repair")


def test_identifiers_are_whole_tokens_not_substrings():
    rows = [{"id": "a", "summary": "NP-4286 CR2450"},
            {"id": "b", "summary": "NP-42860 CR24501"}]
    assert bm25("np-4286", rows).ids == ["a"]
    assert bm25("CR2450", rows).ids == ["a"]


def test_ties_do_not_depend_on_candidate_order_and_tokenless_is_empty():
    rows = [{"id": "b", "summary": "needle"}, {"id": "a", "summary": "needle"}]
    assert bm25("needle", rows).ids == ["a", "b"]
    assert bm25("needle", rows).hits == bm25("needle", rows[::-1]).hits
    assert bm25("💡!!", rows).hits == []
    assert bm25("", rows).hits == []
    assert bm25("needle", []).hits == []


def test_equal_scores_tie_by_occurred_at_then_id_with_bad_times_last():
    rows = [{"id": mid, "summary": "needle", "occurred_at": when} for mid, when in
            [("a", "2026-01-01T00:00:00Z"), ("b", "2026-02-01T00:00:00Z"),
             ("c", "bad"), ("d", "2026-02-01T08:00:00+08:00")]]
    assert bm25("needle", rows).ids == ["b", "d", "a", "c"]


def test_no_stale_state_between_calls():
    row = {"id": "a", "summary": "old value"}
    assert bm25("old", [row]).hits
    row["summary"] = "new value"
    assert bm25("old", [row]).hits == []
    assert bm25("new", []).hits == []
    assert bm25("new", [row]).hits


def test_resource_limits_fail_explicitly_even_for_tokenless_queries():
    rows = [{"id": "a", "summary": "needle"}]
    with pytest.raises(SearchLimitExceeded):
        bm25("needle", rows, max_text_bytes=3)
    with pytest.raises(SearchLimitExceeded):
        bm25("!!!", rows, max_text_bytes=3)
    with pytest.raises(SearchLimitExceeded):
        bm25("needle", rows * 3, max_cards=2)
    assert bm25("needle", rows, max_cards=1, max_text_bytes=6).ids == ["a"]


# --------------------------------------------------------------------------- #
# 本模块自己的约定
# --------------------------------------------------------------------------- #

def test_default_tokenizer_shapes():
    tok = DefaultTokenizer()
    assert tok.tokenize("NP-4286 / CR2450") == ["np-4286", "cr2450"]
    assert tok.tokenize(" !!! 🪴 \n") == []
    assert tok.tokenize("看医生") == ["看", "医", "生", "看医", "医生"]
    # 和语法助词相邻的二字不生成（「的车」「车的」是跨词噪声），助词单字照常生成
    assert tok.tokenize("我的车") == ["我", "的", "车"]
    assert tok.tokenize("尿酸480") == ["尿", "酸", "尿酸", "480"]
    assert tok.tokenize("PR #317 v2.3.1 k/d") == ["pr", "317", "v2.3.1", "k/d"]
    assert tok.tokenize("Café") == ["café"]
    assert tok.tokenize("猫") == ["猫"]


def test_single_cjk_character_query_finds_the_card():
    rows = [{"id": "spicy", "summary": "不吃辣"}, {"id": "other", "summary": "喜欢甜食"}]
    assert rank("辣", rows).ids == ["spicy"]


def test_min_score_and_limit():
    rows = [{"id": "strong", "summary": "alpha beta"}, {"id": "weak", "summary": "alpha gamma delta"},
            {"id": "none", "summary": "zeta"}]
    full = bm25("alpha beta", rows)
    assert full.ids == ["strong", "weak"]
    floor = (full.hits[0].score + full.hits[1].score) / 2
    gated = bm25("alpha beta", rows, min_score=floor)
    assert gated.ids == ["strong"]
    assert gated.trace["below_min_score"] == 1 and gated.trace["matched"] == 2
    assert bm25("alpha beta", rows, limit=1).ids == ["strong"]
    with pytest.raises(ValueError):
        bm25("alpha", rows, min_score=float("nan"))


def test_stopwords_are_removed_from_the_query_only():
    rows = [{"id": "a", "summary": "the needle"}, {"id": "b", "summary": "the hay the hay"}]
    assert bm25("the", rows, stopwords={"the"}).hits == []
    plain = bm25("needle", rows)
    with_stop = bm25("the needle", rows, stopwords={"the"})
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
    assert bm25("needle", rows).version.startswith(base + "+cfg:"), "关掉默认门槛也是换了尺子"
    assert rank("needle", rows, stopwords=retrieval.DEFAULT_STOPWORDS,
                min_coverage=retrieval.DEFAULT_MIN_COVERAGE).version == base


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
    bm25("needle card", rows, tokenizer=Counting())
    assert calls["needle card"] == 1
    assert all(calls[f"card {i} needle"] == 1 for i in range(50))
    assert sum(calls.values()) == 51


def test_default_search_text_prefers_host_projection_and_dedupes():
    assert default_search_text({"search_text": "explicit", "summary": "s"}) == "explicit"
    card = {"summary": "same", "content": "same", "bucket": "工作",
            "threads": ["t1", 3], "retrieval_cues": ["  cue  one ", "cue one", 7]}
    assert default_search_text(card) == "same\n工作\nt1\ncue one"


# --------------------------------------------------------------------------- #
# MG-3：默认停用词与门槛
# --------------------------------------------------------------------------- #

def _garden():
    return [
        {"id": "like-1", "summary": "喜欢爵士乐", "content": "最近很喜欢听爵士乐，尤其是钢琴三重奏。"},
        {"id": "like-2", "summary": "喜欢吃辣", "content": "他说自己越来越喜欢川菜。"},
        {"id": "cat", "summary": "养了一只橘猫叫豆包", "content": "豆包很黏人。"},
        {"id": "trip", "summary": "去京都的行程", "content": "第二天去伏见稻荷。"},
        {"id": "jira", "summary": "JIRA-4821 事故复盘", "content": "token refresh 失败，回滚后恢复。"},
    ] + [{"id": f"f{i}", "summary": f"周{i}吃了面", "content": "味道一般，排队很久。"} for i in range(20)]


def test_stopwords_alone_never_produce_a_hit():
    assert rank("我的是什么", _garden()).hits == []
    assert rank("what is my", _garden()).hits == []
    # 同样的查询关掉停用词，就会有卡被带进来 —— 证明上面那条不是碰巧为空
    assert bm25("我的是什么", _garden()).hits


def test_query_about_something_never_recorded_returns_nothing():
    """「冰岛」花园里没有：只靠零散单字（冰、去）撞上的卡要被挡住。"""
    garden = _garden() + [{"id": "ice", "summary": "夏天只喝冰美式"},
                          {"id": "went", "summary": "上个月去看了海"}]
    assert bm25("我有没有去过冰岛", garden).hits, "去掉门槛时单字确实会撞上"
    result = rank("我有没有去过冰岛", garden)
    assert result.hits == []
    assert result.trace["below_gate"] >= 2


@pytest.mark.xfail(strict=True, reason=(
    "已知边界：泛用实词（喜欢）在默认分词器下被单字+二字重复计数，证据分够得上强证据闸，"
    "「我喜欢什么颜色的车」仍会带回写着「喜欢」的卡（evals/retrieval q49 同一现象）。"
    "修好时这条会变成 XPASS 而报红，提醒更新门槛说明。"))
def test_generic_verb_alone_should_not_count_as_a_hit():
    assert rank("我喜欢什么颜色的车", _garden()).hits == []


def test_real_matches_survive_the_gate():
    garden = _garden()
    assert rank("豆包", garden).ids == ["cat"]
    assert rank("猫", garden).ids == ["cat"]
    assert rank("JIRA-4821", garden).ids == ["jira"]
    assert rank("我喜欢听的爵士乐", garden).ids[:1] == ["like-1"]
    assert all(0 < h.coverage <= 1 for h in rank("豆包 京都", garden).hits)


def test_long_paste_passes_on_strong_evidence_despite_low_coverage():
    garden = _garden()
    paste = ("帮我看看这段是不是跟之前那次有关：09:12 alert auth-gateway 5xx rate 3.2% "
             "login token refresh failing after gray release of JIRA-4821 follow-up patch, "
             "rollback initiated ETA 10 min, error rate back to baseline, keeping incident open")
    result = rank(paste, garden)
    assert result.ids[:1] == ["jira"]
    assert result.hits[0].coverage < retrieval.DEFAULT_MIN_COVERAGE, "这条要测的就是低覆盖率放行"


def test_gate_parameters_are_validated_and_can_be_disabled():
    garden = _garden()
    with pytest.raises(ValueError):
        rank("豆包", garden, min_coverage=-0.1)
    with pytest.raises(ValueError):
        rank("豆包", garden, strong_evidence=float("inf"))
    closed = rank("我喜欢什么颜色的车", garden, stopwords=frozenset(), min_coverage=0.0)
    assert closed.ids == bm25("我喜欢什么颜色的车", garden).ids


def test_strong_evidence_scaling_is_off_by_default_and_opt_in():
    """放大闸默认不开：长粘贴里只靠一个编号命中的答案仍然放行；打开后被挡、版本号带 cfg。"""
    garden = _garden()
    paste = ("帮我看看这段是不是跟之前那次有关：09:12 alert auth-gateway 5xx rate 3.2% "
             "login token refresh failing after gray release of JIRA-4821 follow-up patch, "
             "rollback initiated ETA 10 min, error rate back to baseline, keeping incident open")
    default = rank(paste, garden)
    assert retrieval.DEFAULT_STRONG_EVIDENCE_TERMS is None
    assert default.ids[:1] == ["jira"] and default.trace["evidence_scale"] == 1.0
    assert "+cfg:" not in default.version
    scaled = rank(paste, garden, strong_evidence_terms=8)
    assert scaled.trace["evidence_scale"] > 1
    assert scaled.ids == [], "这就是默认不开的原因：编号是唯一锚点时会被挡掉"
    assert "+cfg:" in scaled.version
    # 短查询不放大：token 数不超过 terms 时和默认完全相同。
    assert rank("豆包", garden, strong_evidence_terms=8).ids == rank("豆包", garden).ids
    assert rank("豆包", garden, strong_evidence_terms=8).trace["evidence_scale"] == 1.0


@pytest.mark.parametrize("terms", [0, -1, 1.5, "8", True])
def test_strong_evidence_terms_is_validated(terms):
    with pytest.raises(ValueError):
        rank("豆包", _garden(), strong_evidence_terms=terms)


def test_select_context_shares_the_scaled_gate_with_rank():
    garden = _garden()
    query = "今天午饭随便吃了碗面\n下午有点困想点杯奶茶\n晚上还要加班真烦\n周末想出去走走散散心"
    for terms in (None, 4):
        ranked = rank(query, garden, strong_evidence_terms=terms)
        picked, trace = retrieval.select_context(query, garden, strong_evidence_terms=terms)
        assert trace["version"] == ranked.version
        assert trace["evidence_scale"] == ranked.trace["evidence_scale"]
        assert {c["id"] for c in picked} <= set(ranked.ids)


# --------------------------------------------------------------------------- #
# 发布前复审：重音拉丁词、小花园覆盖率、没有 id 的卡
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text, tokens", [
    ("Café", ["café"]),
    ("cafe\u0301", ["café"]),                      # 分解写法（e + 组合重音）先 NFC
    ("Mon résumé est prêt", ["mon", "résumé", "est", "prêt"]),
    ("Herr Müller aus Köln", ["herr", "müller", "aus", "köln"]),
    ("Straße", ["strasse"]),
    ("café-np-4286", ["café", "np-4286"]),           # 编号仍整段保留
    ("480 μmol/L", ["480", "μ", "mol/l"]),           # 拉丁与希腊字母之间照旧切开
    ("привет-мир", ["привет", "мир"]),
    ("हिन्दी", ["हिन्दी"]),                              # 组合元音符号不劈开词
])
def test_default_tokenizer_keeps_accented_words_whole(text, tokens):
    assert DefaultTokenizer().tokenize(text) == tokens


def test_accented_fragments_do_not_produce_false_hits():
    """``mg-default-v1`` 把 café 切成 caf + é、Müller 切成 m + ü + ller：碎片互相撞上。"""
    garden = [{"id": "resume", "summary": "Mon résumé est prêt"},
              {"id": "cafe", "summary": "Rendez-vous au café"},
              {"id": "muller", "summary": "Termin mit Herrn Müller"},
              {"id": "keller", "summary": "Weinkeller aufräumen"}] + [
        {"id": f"f{i}", "summary": f"note {i} lorem ipsum"} for i in range(20)]
    assert rank("été", garden).ids == []
    assert rank("café", garden).ids == ["cafe"]
    assert rank("Müller", garden).ids == ["muller"]
    assert rank("ller", garden).ids == []


def test_tiny_garden_answer_is_not_gated_out_by_unseen_query_words():
    """1 张卡时命中词 IDF 0.288、没命中的词 1.386，覆盖率被压到 0.12 —— 新用户问什么都是空。"""
    cards = [{"id": "coffee", "summary": "喜欢喝美式咖啡，不加糖"}]
    assert rank("我平时喝什么咖啡", cards).ids == ["coffee"]
    assert rank("我平时喝什么咖啡", cards, coverage_pool_floor=1).ids == [], "关掉下限就是原来的缺陷"
    two = cards + [{"id": "run", "summary": "周末去公园跑步"}]
    assert rank("我平时喝什么咖啡", two).ids == ["coffee"]
    picked, _trace = retrieval.select_context("我平时喝什么咖啡", cards)
    assert [c["id"] for c in picked] == ["coffee"]


def test_pool_floor_only_moves_the_coverage_gate():
    cards = [{"id": "coffee", "summary": "喜欢喝美式咖啡，不加糖"},
             {"id": "run", "summary": "周末去公园跑步"}]
    floored = rank("咖啡 公园", cards)
    raw = rank("咖啡 公园", cards, coverage_pool_floor=1)
    assert [(h.id, h.score) for h in floored.hits] == [(h.id, h.score) for h in raw.hits]
    # 候选池不小于下限时逐项不变
    big = cards + [{"id": f"f{i}", "summary": f"周{i}吃了面"} for i in range(30)]
    assert rank("我平时喝什么咖啡", big).hits == rank(
        "我平时喝什么咖啡", big, coverage_pool_floor=1).hits
    # 非默认值进版本号；不合法的值当场炸
    assert "+cfg:" in raw.version and "+cfg:" not in floored.version
    for bad in (0, -1, 2.5, True, None):
        with pytest.raises(ValueError):
            rank("咖啡", cards, coverage_pool_floor=bad)


def test_rank_drops_cards_without_id():
    cards = [{"summary": "needle in a haystack"}, {"id": "", "summary": "needle again"},
             {"id": "real", "summary": "the needle"}]
    result = rank("needle", cards)
    assert result.ids == ["real"]
    assert all(hit.id for hit in result.hits)
    assert result.trace["candidates"] == 3 and result.trace["without_id"] == 2
    with pytest.raises(SearchLimitExceeded):
        rank("needle", cards, max_cards=2)


def test_orphan_combining_marks_are_dropped_and_marks_do_not_split_words():
    tok = DefaultTokenizer()
    assert tok.tokenize("́abc 中́文") == ["abc", "中", "文"]
    assert tok.tokenize("हिन्दी-भाषा") == ["हिन्दी", "भाषा"]
