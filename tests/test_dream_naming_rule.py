"""Dream 提示词的称呼规则由宿主决定，和 Capture / 导入同一个语义。

## 为什么

``MaintenanceRequest`` 以前没有 ``naming_rule``：Capture 和历史导入用的是宿主
（io）自己的规则（不许叫「用户」、不许写「TA」），Dream 却总是退回内核的默认
规则。同一个人，白天落卡按宿主的规矩叫，夜里整理时换一把尺子重写卡片。
"""
from __future__ import annotations

import json

import pytest

from memgarden import GardenComponent, MaintenanceRequest
from memgarden.naming import naming_rule as default_naming_rule

HOST_RULE = "宿主规则：称呼阿青时只用「阿青」，不写「用户」，不写「TA」。"


class Recorder:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        self.prompts.append(prompt)
        return json.dumps({"consolidations": []})


def _cards(n: int = 15) -> list[dict]:
    return [{"id": f"card{i:04d}", "summary": f"摘要{i}", "content": f"正文{i}"}
            for i in range(n)]


@pytest.mark.parametrize("locale", ["zh-Hans", "en"])
def test_a_host_rule_reaches_the_dream_prompt_verbatim(locale):
    model = Recorder()
    GardenComponent(model=model).run_maintenance(MaintenanceRequest(
        cards=_cards(), locale=locale, user_name="阿青", naming_rule=HOST_RULE))
    prompt = model.prompts[0]
    assert HOST_RULE in prompt
    assert default_naming_rule("阿青", locale=locale) not in prompt


@pytest.mark.parametrize("locale", ["zh-Hans", "en"])
def test_omitting_the_rule_keeps_the_default(locale):
    model = Recorder()
    GardenComponent(model=model).run_maintenance(MaintenanceRequest(
        cards=_cards(), locale=locale, user_name="阿青"))
    assert MaintenanceRequest().naming_rule is None
    assert default_naming_rule("阿青", locale=locale) in model.prompts[0]
    assert HOST_RULE not in model.prompts[0]


def test_session_and_built_in_loop_use_the_same_rule():
    request = MaintenanceRequest(cards=_cards(), locale="zh-Hans",
                                 user_name="阿青", naming_rule=HOST_RULE)
    built = Recorder()
    GardenComponent(model=built).run_maintenance(request)
    session = GardenComponent(model=Recorder()).maintenance_session(request)
    assert session.next_prompt() == built.prompts[0]
