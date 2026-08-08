"""
五份产物契约。这是整个平台的接口协议，早于所有 skill 存在。

改动本文件 = 改动全平台接口，必须同步 bump contract_version 并
检查所有生产者/消费者 skill 的输入输出契约章节。
"""

from __future__ import annotations

import re

from .base import Contract, Field

# ============================================================
# 编剧职责边界的机器化执法
# 架构里写死了「编剧不写镜头、机位、光线、时长、转场」，
# 但写在 SKILL.md 里只是君子协定。这里把它变成校验器强制执行。
# ============================================================

CAMERA_TERMS = [
    "镜头", "机位", "特写", "近景", "中景", "远景", "全景", "景别",
    "推轨", "摇臂", "俯拍", "仰拍", "航拍", "变焦", "推近", "拉远",
    "淡入", "淡出", "叠化", "转场", "切至", "闪回镜头", "长镜头", "空镜",
    "画面", "构图", "打光", "光线", "逆光", "景深", "焦点",
]


def _no_camera_terms(text_getter):
    """生成一个 check 函数：扫描列表里的文本字段，发现镜头术语就报错。"""
    def check(val) -> list[str]:
        errs = []
        for i, item in enumerate(val if isinstance(val, list) else []):
            text = text_getter(item)
            if not isinstance(text, str):
                continue
            hits = [t for t in CAMERA_TERMS if t in text]
            if hits:
                errs.append(
                    f"[{i}] 越权：出现镜头术语 {hits}。"
                    f"编剧只写场景/动作/对白，镜头由分镜skill负责"
                )
        return errs
    return check


# ============================================================
# 契约一：故事档案（作家 → 编剧、导演匹配器）
# ============================================================

STORY_FILE = Contract(
    name="story_file",
    cn_name="故事档案",
    version="1.0",
    producer="writer",
    consumers=["playwright", "director_matcher"],
    payload_fields=[
        Field("title", "str", True, "故事标题"),
        Field("logline", "str", True, "一句话故事，必须自带钩子", check=lambda v: (
            ["logline 过长（建议 80 字内），说明还没提炼干净"] if len(v) > 80 else []
        )),
        Field("genre_tags", "list", True, "题材标签，供导演匹配器使用",
              item_type="str", min_items=1),
        Field("emotional_core", "str", True, "情感内核：观众最终被什么打动"),
        Field("target_duration_sec", "int", True, "目标时长（秒）", check=lambda v: (
            ["目标时长必须为正数"] if v <= 0 else []
        )),
        Field("hook", "str", True, "开场钩子：前几秒靠什么留住观众"),
        Field("synopsis", "dict", True, "三幕梗概", fields=[
            Field("act1", "str", True, "第一幕：建置与打破日常"),
            Field("act2", "str", True, "第二幕：对抗与升级"),
            Field("act3", "str", True, "第三幕：高潮与落点"),
        ]),
        Field("characters_brief", "list", True, "人物速写", min_items=1,
              item_fields=[
                  Field("id", "str", True, "人物ID，下游全程引用"),
                  Field("name", "str", True, "人物名"),
                  Field("role", "str", True, "叙事功能：主角/对手/助力等"),
                  Field("one_line", "str", True, "一句话人设"),
              ]),
        # 作家skill的「先发散再收敛」机制留痕：候选 logline 存档，便于回看取舍
        Field("logline_candidates", "list", False, "备选 logline（发散阶段留痕）",
              item_type="str"),
        Field("why_this_one", "str", False, "为何选定这条 logline"),
    ],
)


# ============================================================
# 契约二：剧本（编剧 → 选角、场务、分镜）
# ============================================================

SCREENPLAY = Contract(
    name="screenplay",
    cn_name="剧本",
    version="1.0",
    producer="playwright",
    consumers=["casting", "set_dresser", "storyboard"],
    payload_fields=[
        Field("title", "str", True, "剧本标题"),
        Field("episode_no", "int", False, "集号，单集短片可省"),
        Field("characters", "list", True, "人物清单（选角skill的输入源）",
              min_items=1, item_fields=[
                  Field("id", "str", True, "人物ID，须与故事档案一致"),
                  Field("name", "str", True, "人物名"),
                  Field("brief", "str", True, "人设摘要"),
              ]),
        Field("locations", "list", True, "场景清单（场务skill的输入源）",
              min_items=1, item_fields=[
                  Field("id", "str", True, "场景ID"),
                  Field("name", "str", True, "场景名"),
                  Field("int_ext", "str", True, "内外景", choices=["INT", "EXT"]),
              ]),
        Field("scenes", "list", True, "分场结构", min_items=1, item_fields=[
            Field("id", "str", True, "场次ID"),
            Field("location_id", "str", True, "引用 locations.id"),
            Field("time_of_day", "str", True, "时间",
                  choices=["日", "夜", "晨", "昏", "不限"]),
            Field("summary", "str", True, "本场要办成的事（场景功能）"),
            Field("present_character_ids", "list", True, "在场人物",
                  item_type="str", min_items=1),
            # 动作-反应链：架构里对编剧skill方法论的硬要求
            Field("beats", "list", True, "动作-反应链，每拍一条",
                  min_items=1, item_fields=[
                      Field("kind", "str", True, "类型",
                            choices=["action", "dialogue", "reaction"]),
                      Field("character_id", "str", False, "谁（旁白/环境动作可空）"),
                      Field("text", "str", True, "内容"),
                  ], check=_no_camera_terms(lambda x: x.get("text", ""))),
        ]),
        # 关键句三选一机制留痕
        Field("key_line_options", "list", False, "关键句候选（开场/反转/收尾）",
              item_fields=[
                  Field("slot", "str", True, "位置", choices=["开场", "反转", "收尾"]),
                  Field("options", "list", True, "候选", item_type="str", min_items=2),
                  Field("chosen", "str", False, "用户选定的那条"),
              ]),
    ],
)


# ============================================================
# 契约三：角色板（选角 → 分镜、用户）
# ============================================================

_PROMPT_PAIR = [
    Field("zh", "str", True, "中文提示词"),
    Field("en", "str", True, "英文提示词（多数图像模型吃英文更稳）"),
]

def _board_coverage_check(id_key: str, list_key: str, cn: str, producer: str):
    """
    覆盖完整性：角色板/场景板必须覆盖剧本里的每一个人物/场景。

    为什么必须在这一关拦：漏一个角色，本关校验照样通过，
    要等到分镜引用它时才炸。那时候已经白跑了一个节点，
    而且报错指向分镜，实际错在选角 —— 排查方向都是反的。
    """
    def check(payload: dict, ctx: dict) -> list[str]:
        sp = ctx.get("screenplay")
        if not sp:
            return []
        need = {x.get("id"): x.get("name", "") for x in sp.get(list_key, [])}
        have = {x.get("id") for x in payload.get(list_key, [])}
        errs = []
        for i, nm in need.items():
            if i not in have:
                errs.append(
                    f"payload.{list_key}: 漏了剧本里的{cn} {i!r}（{nm}），"
                    f"{producer}必须覆盖剧本的全部{cn}。"
                    f"漏在这里，分镜引用时才会报错，白跑一个节点")
        for i in have - set(need):
            errs.append(
                f"payload.{list_key}: {cn} {i!r} 不在剧本里，"
                f"{producer}不得新增剧本没有的{cn}")
        return errs
    return check


CHARACTER_BOARD = Contract(
    name="character_board",
    cn_name="角色板",
    version="1.0",
    producer="casting",
    consumers=["storyboard", "user"],
    cross_check=_board_coverage_check("id", "characters", "人物", "选角"),
    payload_fields=[
        Field("characters", "list", True, "角色档案", min_items=1, item_fields=[
            Field("id", "str", True, "人物ID，须与剧本一致"),
            Field("name", "str", True, "人物名"),
            # 一致性的命门：锚定描述。分镜skill只能引用它，不许自己发明长相
            Field("anchor_description", "str", True,
                  "外貌锚定描述：脸型/鼻型/眉骨/眼型/发质等可视化特征",
                  check=lambda v: (
                      ["锚定描述过短（建议 60 字以上），不足以保证跨镜头一致性"]
                      if len(v) < 60 else []
                  )),
            Field("signature_marks", "list", True,
                  "标志性记忆点（疤痕/饰品/体态），1-2 个",
                  item_type="str", min_items=1, max_items=2),
            Field("main_look", "str", True, "主造型"),
            Field("alt_looks", "list", False, "备用造型", item_type="str"),
            Field("reference_prompts", "dict", True, "五张标准参考图提示词", fields=[
                Field("front_bust", "dict", True, "正面半身", fields=_PROMPT_PAIR),
                Field("profile", "dict", True, "侧脸", fields=_PROMPT_PAIR),
                Field("full_body", "dict", True, "全身", fields=_PROMPT_PAIR),
                Field("expression", "dict", True, "关键表情", fields=_PROMPT_PAIR),
                Field("costume_detail", "dict", True, "服装细节", fields=_PROMPT_PAIR),
            ]),
        ]),
    ],
)


# ============================================================
# 契约四：场景板（场务 → 分镜、用户）
# ============================================================

LOCATION_BOARD = Contract(
    name="location_board",
    cn_name="场景板",
    version="1.0",
    producer="set_dresser",
    consumers=["storyboard", "user"],
    cross_check=_board_coverage_check("id", "locations", "场景", "场务"),
    payload_fields=[
        Field("locations", "list", True, "场景档案", min_items=1, item_fields=[
            Field("id", "str", True, "场景ID，须与剧本一致"),
            Field("name", "str", True, "场景名"),
            Field("anchor_description", "str", True,
                  "空间锚定描述：结构/材质/陈设/色调，跨镜头复用",
                  check=lambda v: (
                      ["空间锚定描述过短（建议 60 字以上）"] if len(v) < 60 else []
                  )),
            Field("spatial_layout", "str", True, "空间关系：门窗方位、主要活动区"),
            Field("mood_tone", "str", True, "氛围基调"),
            Field("time_variants", "list", False, "同场景的时间变体（日/夜）",
                  item_type="str"),
            Field("reference_prompts", "dict", True, "场景图提示词", fields=[
                Field("wide", "dict", True, "全景", fields=_PROMPT_PAIR),
                Field("main_area", "dict", True, "主要活动区", fields=_PROMPT_PAIR),
                Field("detail", "dict", True, "氛围细节", fields=_PROMPT_PAIR),
            ]),
        ]),
    ],
)


# ============================================================
# 契约五：分镜表（分镜 → 用户）
# ============================================================

def _shotlist_cross_check(payload: dict, ctx: dict) -> list[str]:
    """
    引用完整性：分镜里出现的人物和场景，必须在角色板/场景板里真实存在。
    这是「分镜不许自己发明人物长相和场景样貌」的机器化执法。
    ctx 里期望有 character_board / location_board / screenplay 的 payload。
    """
    errs: list[str] = []
    cb = ctx.get("character_board") or {}
    lb = ctx.get("location_board") or {}
    known_chars = {c.get("id") for c in cb.get("characters", [])}
    known_locs = {l.get("id") for l in lb.get("locations", [])}
    if not known_chars and not known_locs:
        return []  # 无上下文时跳过，由引擎决定是否强制

    for si, sc in enumerate(payload.get("scenes", [])):
        for hi, shot in enumerate(sc.get("shots", [])):
            p = f"payload.scenes[{si}].shots[{hi}]"
            lid = shot.get("location_id")
            if known_locs and lid not in known_locs:
                errs.append(f"{p}.location_id: {lid!r} 不存在于场景板，"
                            f"分镜不得引用未定义场景")
            for ci, cid in enumerate(shot.get("character_ids", [])):
                if known_chars and cid not in known_chars:
                    errs.append(f"{p}.character_ids[{ci}]: {cid!r} 不存在于角色板，"
                                f"分镜不得发明人物")
    return errs


SHOTLIST = Contract(
    name="shotlist",
    cn_name="分镜表",
    version="1.0",
    producer="storyboard",
    consumers=["user"],
    cross_check=_shotlist_cross_check,
    payload_fields=[
        Field("scenes", "list", True, "按场组织的镜头", min_items=1, item_fields=[
            Field("scene_id", "str", True, "引用剧本场次ID"),
            Field("shots", "list", True, "镜头清单", min_items=1, item_fields=[
                Field("id", "str", True, "镜头ID"),
                Field("location_id", "str", True, "引用场景板ID"),
                Field("character_ids", "list", True, "引用角色板ID（空镜可为空列表）",
                      item_type="str"),
                Field("shot_size", "str", True, "景别",
                      choices=["大远景", "远景", "全景", "中景", "近景", "特写", "大特写"]),
                Field("camera_movement", "str", True, "镜头运动"),
                Field("frame_description", "str", True, "画面描述"),
                Field("duration_sec", "float", True, "时长（秒）", check=lambda v: (
                    ["单镜时长必须为正数"] if v <= 0 else []
                )),
                Field("video_prompt", "dict", True,
                      "可直接投喂视频模型的提示词", fields=_PROMPT_PAIR),
                Field("target_model", "str", False,
                      "目标视频模型（不同模型吃的提示词方言不同）"),
            ]),
        ]),
    ],
)


# ============================================================
# 注册表
# ============================================================

REGISTRY: dict[str, Contract] = {
    c.name: c for c in [
        STORY_FILE, SCREENPLAY, CHARACTER_BOARD, LOCATION_BOARD, SHOTLIST
    ]
}

# 流水线顺序（M0 只登记，M2 之后引擎按它调度）
PIPELINE_ORDER = [
    "story_file", "screenplay",
    ["character_board", "location_board"],   # 并行
    "shotlist",
]


def get(name: str) -> Contract:
    if name not in REGISTRY:
        raise KeyError(f"未知契约 {name!r}，已注册: {list(REGISTRY)}")
    return REGISTRY[name]
