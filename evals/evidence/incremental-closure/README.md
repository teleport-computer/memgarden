# 未发布收尾变更：小样本模型核验

2026-09-29，基于0.23.0加工作树修复。仅使用仓库的7条合成样本，无真实用户数据。

- 模型：`deepseek/deepseek-v4-pro-0813`；OpenRouter，固定CoreWeave，不允许fallback；temperature=0，每案输出上限2048 tokens。
- 每案1次，7/7通过现有自动检查。provider返回的cost合计 **0.017272756 USD**，不是预估。
- 原始结果见[results.json](results.json)，含来源SHA、dirty标记和各prompt指纹；不要误认为与已发布源码相同。
- 人工逐项检查：礼物/书籍保留物品、金额和日期；寒暄与未接受建议为空；更正把27作为正确票价、72作为旧错误；Dream合并保留购买/送达日期、包装与地点；搬家补充保留城市、费用、时长和最后一箱时间。
- 这轮未见旧样本里“可能为了记录开销/避免重复送礼”的动机推测，也未见补造年份。不是全语义自动判定，不保证其他材料不出现推测。
- 礼物英文summary中的“for her birthday on May 2”仍有日期附着歧义，正文明确May 2是购买日期。不可把summary当作精确生日证据；全文读链路有独立确定性测试。
- 不属于A/B改善证明；未重复采样，不是实际DSH+模型联调，也不验证大花园语义合并质量。

运行方式（凭据只经环境注入）：

```bash
python evals/specifics.py --provider openrouter --model deepseek/deepseek-v4-pro-0813 --openrouter-provider CoreWeave --repeat 1 --max-output-tokens 2048
```
