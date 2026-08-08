"""
导航结构。

分三区，因为这三件事的性质不同 —— 使用频率、心智模式、改错的代价都不一样：

  制作  在做的活。有先后、有状态、有卡点，是一条动线。
  资产  攒下来的东西。Skill、导演、题材、契约，改一次影响之后所有产出。
  系统  配置与自检。低频，出问题才来。

原先四个入口（剧集 / Skill / 导演 / 密钥设置）把三者平铺在一层，
既看不出哪些是一类，也没有「我现在该干什么」的落脚点。

定义在这里而不是写死在模板里：二级导航要按当前区渲染，
两处各写一份迟早对不上。
"""

from __future__ import annotations

ZONES = (
    {
        "id": "make", "label": "制作", "home": "series.inbox",
        "items": (("待办", "series.inbox"), ("剧集", "series.index")),
    },
    {
        "id": "assets", "label": "资产", "home": "skills.index",
        "items": (("Skill", "skills.index"), ("导演", "directors.index"),
                  ("契约", "assets.contracts_index")),
    },
    {
        "id": "system", "label": "系统", "home": "settings.index",
        "items": (("密钥", "settings.index"),),
    },
)


def zone_of(zone_id: str) -> dict | None:
    return next((z for z in ZONES if z["id"] == zone_id), None)
