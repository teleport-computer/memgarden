"""一个不联网、不花钱的假模型 —— 失败路径测试用。

行为固定：不管问什么，都吐同一张卡。**故意不带随机性** ——
失败路径要验的是管线的边界行为，模型输出一变，失败原因就说不清了。
"""
import json
import sys

sys.stdin.read()
print(json.dumps({"cards": [{
    "action": "add",
    "bucket": "偏好与边界",
    "type": "preference",
    "summary": "喜欢喝美式咖啡",
    "content": "下午常喝美式，不加糖。",
}]}, ensure_ascii=False))
