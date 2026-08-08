"""
流水线定义：五个节点的唯一真相。

以前 web/app.py 的 NODES 和 cli/avctl.py 的 NODE_SEQ 各存一份，
改一处忘另一处，界面和命令行就会显示成两条不同的流水线。

scope_of() 是产物索引口径的唯一入口。这件事必须集中：
集级产物如果拿剧集 id 当 scope_id，同一部剧两集的故事档案/剧本/分镜
会在索引里互相覆盖 —— 文件落在 ep01/ ep02/ 分得开，索引却分不开，
于是界面上的分集切换是假的。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.store.artifacts import SERIES_LEVEL


@dataclass(frozen=True)
class Node:
    no: str          # 流水线编号，承载顺序信息，不是装饰
    contract: str
    skill: str

    @property
    def level(self) -> str:
        """归属层级从 SERIES_LEVEL 推导，不另存一份，避免两处打架。"""
        return "series" if self.contract in SERIES_LEVEL else "episode"

    @property
    def level_cn(self) -> str:
        return "剧集级" if self.level == "series" else "集级"


# N1 是开拍前的选题/频道圣经，不在平台内生成，所以流水线从 N2 起。
# N4/N5 并行：角色板和场景板都只依赖剧本，彼此无依赖。
NODES: tuple[Node, ...] = (
    Node("N2", "story_file", "writer"),
    Node("N3", "screenplay", "playwright"),
    Node("N4", "character_board", "casting"),
    Node("N5", "location_board", "set_dresser"),
    Node("N6", "shotlist", "storyboard"),
)


def node_for(contract: str) -> Node | None:
    return next((n for n in NODES if n.contract == contract), None)


def episode_scope_id(series_id: str, episode_no: int) -> str:
    """集级 scope_id。与 episodes.id 的既有约定保持一致。"""
    return f"{series_id}-ep{episode_no}"


def parse_scope(scope_type: str, scope_id: str) -> tuple[str, int | None]:
    """
    scope_of 的逆运算：(剧集id, 集号)。
    待办页要从产物行反查它属于哪一集，才能给出跳转链接。
    """
    if scope_type == "series":
        return scope_id, None
    m = re.match(r"^(.*)-ep(\d+)$", scope_id or "")
    if not m:
        return scope_id, None
    return m.group(1), int(m.group(2))


def scope_of(node: Node, series_id: str,
             episode_no: int | None) -> tuple[str, str]:
    """
    (scope_type, scope_id) —— 产物注册与查询都走这里。

    规则刻意与 ArtifactStore.dir_for() 对齐：集级契约在没给集号时，
    产物文件会落进 _series/，那么索引也必须记成剧集级。
    索引口径和文件落点一旦不一致，就会出现「文件在那儿但界面找不到」。
    """
    if node.level == "series" or episode_no is None:
        return ("series", series_id)
    return ("episode", episode_scope_id(series_id, episode_no))
