"""随包发布的 Runtime Adapter。

## 为什么 Adapter 跟着 Python 包走，而不是单独发 npm

Adapter 和这个包共用**同一套 wire 协议**。拆成两个包、两个版本号之后，
用户就能装出一个我们从没测过的组合（adapter 0.1.0 + memgarden 0.20.0），
而症状是运行时某个字段对不上 —— 不是启动失败，是某一类记忆悄悄记不进去。

放在一起，版本永远同步：``pip install memgarden`` 装到哪一版，
Adapter 就是哪一版。用户本来就要装这个 Python 包（Adapter 靠它跑
``memgarden serve``），所以这一步是零成本。

代价是 JS 那边不能用 npm 的依赖解析 —— 但这个 Adapter 零 npm 依赖，
本来也没什么可解析的。
"""
