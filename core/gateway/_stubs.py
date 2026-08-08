"""
dry-run 占位产物。字段齐全，能通过契约校验，但内容明显是占位的。
用途：无 API key 时验收引擎逻辑；改流水线代码时不必烧 token。
"""

STUBS = {
    "proposal": {
        "verdict": "有系统性问题",
        "evidence_summary": "【占位】dry-run 建议：反馈集中在情感落点与开场钩子两处。",
        "suggestions": [{
            "section": "质量红线",
            "kind": "add",
            "current": "",
            "proposed": "8. 【占位】新增红线条目（dry-run）",
            "why": "【占位】依据多次修改",
            "confidence": "medium",
        }],
        "not_suggested": ["【占位】样本不足的问题"],
    },
    "story_file": {
        "title": "【占位】故事标题",
        "logline": "【占位】一句话故事，用于 dry-run 跑通流程",
        "genre_tags": ["占位题材"],
        "emotional_core": "【占位】情感内核",
        "target_duration_sec": 180,
        "hook": "【占位】开场钩子",
        "synopsis": {
            "act1": "【占位】第一幕",
            "act2": "【占位】第二幕",
            "act3": "【占位】第三幕",
        },
        "characters_brief": [
            {"id": "c1", "name": "【占位】主角", "role": "主角",
             "one_line": "【占位】一句话人设"},
        ],
        "logline_candidates": ["【占位】候选一", "【占位】候选二"],
        "why_this_one": "【占位】选定理由",
    },
    "screenplay": {
        "title": "【占位】剧本标题",
        "episode_no": 1,
        "characters": [{"id": "c1", "name": "【占位】主角", "brief": "【占位】人设"}],
        "locations": [{"id": "l1", "name": "【占位】场景", "int_ext": "INT"}],
        "scenes": [{
            "id": "s1",
            "location_id": "l1",
            "time_of_day": "日",
            "summary": "【占位】本场功能",
            "present_character_ids": ["c1"],
            "beats": [
                {"kind": "action", "character_id": "c1", "text": "【占位】动作描述"},
                {"kind": "dialogue", "character_id": "c1", "text": "【占位】台词"},
            ],
        }],
    },
    "character_board": {
        "characters": [{
            "id": "c1",
            "name": "【占位】主角",
            "anchor_description": (
                "【占位】外貌锚定描述占位内容。真实产出需覆盖脸型骨相、眼型鼻型"
                "嘴型眉形、发质发型、肤色肤质、年龄的具体表现、体态等可测量特征，"
                "禁止使用帅气清秀这类评价性词汇。本占位文本长度刻意留出余量，"
                "以免擦线触发校验。"
            ),
            "signature_marks": ["【占位】标志性记忆点"],
            "main_look": "【占位】主造型",
            "alt_looks": [],
            "reference_prompts": {
                k: {"zh": f"【占位】{k} 中文提示词", "en": f"placeholder {k} prompt"}
                for k in ["front_bust", "profile", "full_body",
                          "expression", "costume_detail"]
            },
        }],
    },
    "location_board": {
        "locations": [{
            "id": "l1",
            "name": "【占位】场景",
            "anchor_description": (
                "【占位】空间锚定描述占位内容。真实产出需覆盖空间类型与尺度、"
                "建筑结构与材质、门窗开口方位、固定陈设各自靠哪面墙、色调基调、"
                "以及三到五处具体的使用痕迹。方位必须写死，否则每次生成布局都会"
                "重排。本占位文本长度刻意留出余量。"
            ),
            "spatial_layout": "【占位】空间关系",
            "mood_tone": "【占位】氛围基调",
            "time_variants": ["日", "夜"],
            "reference_prompts": {
                k: {"zh": f"【占位】{k} 中文提示词", "en": f"placeholder {k} prompt"}
                for k in ["wide", "main_area", "detail"]
            },
        }],
    },
    "shotlist": {
        "scenes": [{
            "scene_id": "s1",
            "shots": [{
                "id": "sh1",
                "location_id": "l1",
                "character_ids": ["c1"],
                "shot_size": "中景",
                "camera_movement": "固定",
                "frame_description": "【占位】画面描述",
                "duration_sec": 3.0,
                "video_prompt": {"zh": "【占位】视频提示词",
                                 "en": "placeholder video prompt"},
                "target_model": "placeholder",
            }],
        }],
    },
}
