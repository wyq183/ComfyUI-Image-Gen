# -*- coding: utf-8 -*-
"""轻量提示词库：内置常用标签 + 从模型元数据提取触发词。

设计原则：
- 不联网、不下载大词库；内置一份精选常用标签，够用即可；
- 触发词/高频标签从本地 safetensors 的 __metadata__ 里读（ss_tag_frequency 等）；
- 只提供数据，不替用户决定提示词。
"""
from __future__ import annotations

import json
import re
from typing import Any

# 推荐负面词（画质向，通用安全）
DEFAULT_NEGATIVE = (
    "worst quality, low quality, lowres, blurry, jpeg artifacts, watermark, "
    "text, signature, bad anatomy, bad hands, extra digits, mutated, deformed"
)

# 给 agent 的提示词结构建议
PROMPT_GUIDE = (
    "提示词结构建议（英文逗号分隔，权重从左到右递减）："
    "画质词 → 主体/角色 → 外貌细节 → 服装/配饰 → 动作/姿势 → 环境/背景 → 光照 → 风格/镜头。"
    "必须包含当前模型的触发词（trigger_words）；LoRA 的触发词按需加入。"
    "负面词优先用 default_negative。不要编造模型不支持的参数。"
)

# 精选标签库（按组）。每条 {name: 英文标签, zh: 中文说明}
TAG_GROUPS: list[dict[str, Any]] = [
    {
        "id": "quality", "label": "质量词", "color": "#22c55e",
        "tags": [
            {"name": "masterpiece", "zh": "杰作"},
            {"name": "best quality", "zh": "最佳质量"},
            {"name": "high quality", "zh": "高质量"},
            {"name": "ultra detailed", "zh": "超细节"},
            {"name": "highres", "zh": "高分辨率"},
            {"name": "absurdres", "zh": "超高分辨率"},
            {"name": "sharp focus", "zh": "锐利对焦"},
            {"name": "intricate details", "zh": "精细细节"},
        ],
    },
    {
        "id": "negative", "label": "负面词", "color": "#ef4444",
        "tags": [
            {"name": "worst quality", "zh": "最差质量"},
            {"name": "low quality", "zh": "低质量"},
            {"name": "lowres", "zh": "低分辨率"},
            {"name": "blurry", "zh": "模糊"},
            {"name": "jpeg artifacts", "zh": "压缩伪影"},
            {"name": "bad anatomy", "zh": "解剖错误"},
            {"name": "bad hands", "zh": "手部错误"},
            {"name": "extra digits", "zh": "多余手指"},
            {"name": "mutated", "zh": "变异"},
            {"name": "deformed", "zh": "畸形"},
            {"name": "watermark", "zh": "水印"},
            {"name": "signature", "zh": "签名"},
            {"name": "text", "zh": "文字"},
        ],
    },
    {
        "id": "style", "label": "风格", "color": "#8b5cf6",
        "tags": [
            {"name": "anime style", "zh": "动漫风格"},
            {"name": "photorealistic", "zh": "写实照片"},
            {"name": "cinematic", "zh": "电影感"},
            {"name": "illustration", "zh": "插画"},
            {"name": "watercolor", "zh": "水彩"},
            {"name": "oil painting", "zh": "油画"},
            {"name": "pixel art", "zh": "像素画"},
            {"name": "concept art", "zh": "概念设计"},
            {"name": "line art", "zh": "线稿"},
            {"name": "chibi", "zh": "Q版"},
            {"name": "cyberpunk", "zh": "赛博朋克"},
            {"name": "steampunk", "zh": "蒸汽朋克"},
            {"name": "art nouveau", "zh": "新艺术"},
            {"name": "flat color", "zh": "平涂"},
        ],
    },
    {
        "id": "composition", "label": "构图 / 镜头", "color": "#0ea5e9",
        "tags": [
            {"name": "portrait", "zh": "肖像"},
            {"name": "upper body", "zh": "上半身"},
            {"name": "full body", "zh": "全身"},
            {"name": "close-up", "zh": "特写"},
            {"name": "wide shot", "zh": "远景"},
            {"name": "from above", "zh": "俯视"},
            {"name": "from below", "zh": "仰视"},
            {"name": "dutch angle", "zh": "斜角"},
            {"name": "depth of field", "zh": "景深"},
            {"name": "bokeh", "zh": "散景"},
            {"name": "rule of thirds", "zh": "三分法"},
            {"name": "symmetrical composition", "zh": "对称构图"},
        ],
    },
    {
        "id": "lighting", "label": "光照", "color": "#f59e0b",
        "tags": [
            {"name": "cinematic lighting", "zh": "电影光照"},
            {"name": "volumetric lighting", "zh": "体积光"},
            {"name": "rim lighting", "zh": "轮廓光"},
            {"name": "backlight", "zh": "背光"},
            {"name": "soft lighting", "zh": "柔光"},
            {"name": "moody lighting", "zh": "氛围光"},
            {"name": "studio lighting", "zh": "影棚光"},
            {"name": "golden hour", "zh": "黄金时刻"},
            {"name": "sunlight", "zh": "日光"},
            {"name": "neon lights", "zh": "霓虹灯"},
            {"name": "god rays", "zh": "丁达尔光"},
            {"name": "dramatic shadows", "zh": "戏剧阴影"},
        ],
    },
    {
        "id": "pose", "label": "姿势 / 动作", "color": "#a855f7",
        "tags": [
            {"name": "standing", "zh": "站立"},
            {"name": "sitting", "zh": "坐姿"},
            {"name": "lying", "zh": "躺姿"},
            {"name": "kneeling", "zh": "跪姿"},
            {"name": "walking", "zh": "行走"},
            {"name": "running", "zh": "奔跑"},
            {"name": "jumping", "zh": "跳跃"},
            {"name": "looking at viewer", "zh": "看向观众"},
            {"name": "looking back", "zh": "回眸"},
            {"name": "arms behind back", "zh": "双手背后"},
            {"name": "hand on hip", "zh": "手叉腰"},
            {"name": "crossed arms", "zh": "抱臂"},
            {"name": "from behind", "zh": "背面视角"},
            {"name": "profile", "zh": "侧面"},
        ],
    },
    {
        "id": "expression", "label": "表情", "color": "#f97316",
        "tags": [
            {"name": "smile", "zh": "微笑"},
            {"name": "grin", "zh": "咧嘴笑"},
            {"name": "happy", "zh": "开心"},
            {"name": "blush", "zh": "脸红"},
            {"name": "serious", "zh": "严肃"},
            {"name": "annoyed", "zh": "恼火"},
            {"name": "embarrassed", "zh": "害羞"},
            {"name": "crying", "zh": "哭泣"},
            {"name": "surprised", "zh": "惊讶"},
            {"name": "expressionless", "zh": "面无表情"},
            {"name": "half-closed eyes", "zh": "半睁眼"},
            {"name": "closed eyes", "zh": "闭眼"},
        ],
    },
    {
        "id": "clothing", "label": "服装", "color": "#ec4899",
        "tags": [
            {"name": "school uniform", "zh": "校服"},
            {"name": "maid outfit", "zh": "女仆装"},
            {"name": "kimono", "zh": "和服"},
            {"name": "casual", "zh": "休闲装"},
            {"name": "hoodie", "zh": "连帽衫"},
            {"name": "suit", "zh": "西装"},
            {"name": "dress", "zh": "连衣裙"},
            {"name": "sleeveless", "zh": "无袖"},
            {"name": "miniskirt", "zh": "迷你裙"},
            {"name": "stockings", "zh": "丝袜"},
            {"name": "thighhighs", "zh": "过膝袜"},
            {"name": "jacket", "zh": "夹克"},
            {"name": "armor", "zh": "盔甲"},
            {"name": "hanfu", "zh": "汉服"},
        ],
    },
    {
        "id": "hair", "label": "发色 / 发型", "color": "#d946ef",
        "tags": [
            {"name": "long hair", "zh": "长发"},
            {"name": "short hair", "zh": "短发"},
            {"name": "twin tails", "zh": "双马尾"},
            {"name": "ponytail", "zh": "马尾"},
            {"name": "braid", "zh": "辫子"},
            {"name": "twintails", "zh": "双马尾（变体）"},
            {"name": "bob cut", "zh": "波波头"},
            {"name": "silver hair", "zh": "银发"},
            {"name": "blonde hair", "zh": "金发"},
            {"name": "black hair", "zh": "黑发"},
            {"name": "red hair", "zh": "红发"},
            {"name": "blue hair", "zh": "蓝发"},
            {"name": "pink hair", "zh": "粉发"},
            {"name": "gradient hair", "zh": "渐变发色"},
        ],
    },
    {
        "id": "environment", "label": "环境 / 背景", "color": "#14b8a6",
        "tags": [
            {"name": "simple background", "zh": "简单背景"},
            {"name": "gradient background", "zh": "渐变背景"},
            {"name": "white background", "zh": "纯白背景"},
            {"name": "cityscape", "zh": "城市景观"},
            {"name": "city street", "zh": "城市街道"},
            {"name": "night", "zh": "夜晚"},
            {"name": "outdoors", "zh": "户外"},
            {"name": "indoors", "zh": "室内"},
            {"name": "forest", "zh": "森林"},
            {"name": "beach", "zh": "海滩"},
            {"name": "bedroom", "zh": "卧室"},
            {"name": "classroom", "zh": "教室"},
            {"name": "cherry blossoms", "zh": "樱花"},
            {"name": "starry sky", "zh": "星空"},
            {"name": "rain", "zh": "雨天"},
            {"name": "snow", "zh": "雪景"},
        ],
    },
]


def list_tag_groups() -> list[dict[str, Any]]:
    """返回标签分组（深拷贝，调用方可安全修改）。"""
    return [
        {"id": g["id"], "label": g["label"], "color": g.get("color", ""),
         "tags": [dict(t) for t in g["tags"]]}
        for g in TAG_GROUPS
    ]


def search_tags(query: str = "", limit: int = 60) -> list[dict[str, str]]:
    """按英文标签 / 中文说明模糊搜索，返回扁平列表。"""
    q = str(query or "").strip().lower()
    out: list[dict[str, str]] = []
    for group in TAG_GROUPS:
        for tag in group["tags"]:
            if q and q not in tag["name"].lower() and q not in str(tag.get("zh", "")).lower():
                continue
            out.append({"name": tag["name"], "zh": tag.get("zh", ""),
                        "group": group["id"], "group_label": group["label"]})
            if len(out) >= limit:
                return out
    return out


def _split_words(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        return [x.strip() for x in re.split(r"[,\n;]+", raw) if x.strip()]
    return []


def extract_trigger_words(metadata: dict[str, Any] | None, limit: int = 24) -> list[str]:
    """从 safetensors 元数据里提取触发词 / 高频标签（去重、按频率排序）。"""
    meta = metadata or {}
    words: list[str] = []

    def add(word: str) -> None:
        w = str(word).strip()
        if w and w not in words:
            words.append(w)

    # 1) 显式触发词字段优先
    for key in ("modelspec.trigger_phrases", "trigger_phrases", "modelspec.trigger_words", "modelspec.tags"):
        for w in _split_words(meta.get(key)):
            add(w)
            if len(words) >= limit:
                return words[:limit]

    # 2) ss_tag_frequency：跨分组累加计数后取高频
    freq = meta.get("ss_tag_frequency")
    if isinstance(freq, str):
        try:
            freq = json.loads(freq)
        except Exception:
            freq = None
    if isinstance(freq, dict):
        counts: dict[str, int] = {}
        for group in freq.values():
            if not isinstance(group, dict):
                continue
            for tag, n in group.items():
                try:
                    n = int(n)
                except Exception:
                    n = 0
                counts[str(tag)] = counts.get(str(tag), 0) + n
        for tag, _ in sorted(counts.items(), key=lambda kv: -kv[1]):
            add(tag)
            if len(words) >= limit:
                break
    return words[:limit]
