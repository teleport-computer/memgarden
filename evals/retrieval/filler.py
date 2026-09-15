"""填充卡 —— 让花园有真实的「背景噪声」，且**不制造未标注的正确答案**。

手写卡（cards.jsonl）承载所有查询的标注；这里按固定种子生成日常流水账式的卡，
作用是：

  · 把花园撑到一两百张，让 IDF / 短语稀有度这类统计量有意义；
  · 提供泛词重叠（「周五」「昨天」「朋友」「什么」），测排序会不会被泛词拖走。

🔴 填充卡绝不能含任何查询的答案词，否则一个「没标注的正确答案」会被算成误召回。
``BANNED`` 列出查询里的关键实体/主题词，``generate()`` 生成后逐张自查，撞上就报错
—— 改模板或改查询时这条自查会先红，而不是悄悄改变基线。
"""
from __future__ import annotations

import random
import re
from datetime import datetime, timedelta, timezone

SEED = 20260915

BANNED_CJK = (
    "豆包", "猫", "膝", "睡", "咖啡", "京都", "搬家", "马拉松", "跑", "日语", "苹果",
    "表带", "书", "颜色", "车", "冰岛", "血型", "姐姐", "纹身", "体检", "事故", "值班",
    "回滚", "航班", "医生", "医院", "生日", "远距离", "处方", "降压", "迁移", "演唱会",
    "手机", "屏幕", "成绩", "抗生素", "过敏", "杭州", "星河", "小满", "胃", "尿酸",
    "腿", "肾", "项目", "记账", "胶片", "行程", "旅行", "上海", "徐汇", "住",
)
BANNED_ASCII = (
    "kyoto", "marathon", "pr", "jira", "postgres", "standup", "dave", "priya", "n2",
    "gym", "aurora", "project", "lumen", "blood", "tattoo", "tattoos", "stomach",
    "doctor", "rollback", "migration", "garmin", "olympus", "x100v", "mu5137", "pb",
)

_DAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日", "昨天", "前天"]
_FOODS = ["牛肉面", "沙拉", "寿司", "煲仔饭", "饺子", "螺蛳粉", "汉堡", "云吞面", "酸菜鱼",
          "盖浇饭", "披萨", "麻辣烫"]
_FOOD_COMMENTS = ["分量有点少", "味道一般", "排队排了二十分钟", "比上次好吃", "价格又涨了",
                  "老板送了一碗汤"]
_SHOWS = ["《漫长的季节》", "《繁花》", "《沙丘2》", "《奥本海默》", "《请回答1988》",
          "《黑镜》新一季", "《流浪地球2》", "《我的解放日志》"]
_OPINIONS = ["结尾有点仓促", "配乐很好", "节奏太慢", "看得很上头", "没有想象中好看",
             "打算再看一遍"]
_WEATHER = [("突然下暴雨", "伞被吹坏了"), ("闷热得像蒸笼", "空调开了一整天"),
            ("台风预警", "在家里没出门"), ("降温十度", "翻出了厚外套"),
            ("雾霾很重", "出门戴了口罩"), ("天气特别好", "中午去公园坐了一会儿")]
_GROCERY = ["一箱牛奶", "两斤排骨", "洗衣液", "一袋橙子", "冷冻饺子", "纸巾", "酸奶", "鸡蛋"]
_PEOPLE = ["同学阿宁", "表弟", "楼下便利店老板", "前同事 Leo", "大学室友老韩", "理发师小吴"]
_TOPICS = ["买基金的事", "最近的房价", "游戏新作", "考公", "育儿", "装修", "换发型"]
_CHORES = ["周末把阳台彻底打扫了一遍", "换了新的窗帘", "修好了漏水的水龙头", "整理了衣柜",
           "给洗衣机除垢", "把冰箱里过期的东西清掉了"]
_PODCASTS = ["《忽左忽右》", "《声东击西》", "《随机波动》", "《故事FM》"]
_PLANTS = ["龟背竹", "绿萝", "多肉", "琴叶榕"]
_EN_SHOWS = ["The Bear", "Severance", "Shogun", "Slow Horses", "Arcane"]
_EN_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_EN_OPINIONS = ["the ending felt rushed", "the soundtrack was great", "it dragged a bit",
                "he wants to rewatch it"]
_EN_FOODS = ["Thai", "Korean BBQ", "taco", "dim sum", "pho", "Lebanese"]
_EN_HOME = ["dishwasher", "bathroom fan", "door lock", "desk lamp", "kitchen tap"]


def _templates(rng: random.Random):
    d = rng.choice(_DAYS)
    f = rng.choice(_FOODS)
    yield ("饮食", f"{d}中午吃了{f}", f"{d}中午在附近吃了{f}，{rng.choice(_FOOD_COMMENTS)}。")
    s = rng.choice(_SHOWS)
    yield ("娱乐", f"看完了{s}", f"{rng.choice(_DAYS)}晚上看完了{s}，觉得{rng.choice(_OPINIONS)}。")
    w, r = rng.choice(_WEATHER)
    yield ("日常", f"{rng.choice(_DAYS)}{w}", f"{rng.choice(_DAYS)}{w}，{r}。")
    g = rng.choice(_GROCERY)
    yield ("日常", f"在超市买了{g}", f"{rng.choice(_DAYS)}下班顺路在超市买了{g}，排队结账花了很久。")
    p, t = rng.choice(_PEOPLE), rng.choice(_TOPICS)
    yield ("朋友", f"和{p}聊了{t}", f"{rng.choice(_DAYS)}和{p}聊了很久{t}，没聊出什么结论。")
    c = rng.choice(_CHORES)
    yield ("日常", c, f"{rng.choice(_DAYS)}{c}，累但是很有成就感。")
    pc = rng.choice(_PODCASTS)
    yield ("学习", f"听了播客{pc}", f"通勤路上听了一期{pc}，讲的是城市规划，挺有意思。")
    pl = rng.choice(_PLANTS)
    yield ("爱好", f"给{pl}换了盆", f"给阳台上的{pl}换了个大一点的盆，希望别养死。")
    es, ed = rng.choice(_EN_SHOWS), rng.choice(_EN_DAYS)
    yield ("Life", f"Watched {es} on {ed}", f"Watched an episode of {es} on {ed} night; {rng.choice(_EN_OPINIONS)}.")
    ef = rng.choice(_EN_FOODS)
    yield ("Food", f"Tried a new {ef} place", f"Tried a new {ef} place near the office for lunch; would go again.")
    eh = rng.choice(_EN_HOME)
    yield ("Life", f"Fixed the {eh}", f"Spent the evening fixing the {eh}; watched two tutorials first.")


def _violations(text: str) -> list[str]:
    low = text.lower()
    hits = [w for w in BANNED_CJK if w in text]
    words = set(re.findall(r"[a-z0-9]+", low))
    hits += [w for w in BANNED_ASCII if w in words]
    return hits


def generate(count: int = 140) -> list[dict]:
    rng = random.Random(SEED)
    start = datetime(2025, 6, 1, tzinfo=timezone.utc)
    out, seen = [], set()
    guard = 0
    while len(out) < count:
        guard += 1
        if guard > 200:
            raise RuntimeError("filler templates cannot produce enough distinct cards")
        for bucket, summary, content in _templates(rng):
            if len(out) >= count:
                break
            if summary in seen:
                continue
            bad = _violations(summary + " " + content)
            if bad:
                raise ValueError(f"filler card contains answer vocabulary {bad}: {summary}")
            seen.add(summary)
            when = start + timedelta(days=rng.randrange(0, 465), minutes=rng.randrange(0, 1440))
            out.append({
                "id": f"g{len(out) + 1:03d}",
                "summary": summary,
                "content": content,
                "bucket": bucket,
                "threads": [],
                "retrieval_cues": [],
                "occurred_at": when.date().isoformat(),
                "created_at": when.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            })
    return out
