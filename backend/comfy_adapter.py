# -*- coding: utf-8 -*-
"""ComfyUI 能力发现、模型识别与工作流构建器。

v0.5 架构原则：
- 程序负责确定性扫描 / 分类 / 验证 / 构建；
- AI 只负责解释、提示词和用户引导；
- 不再让 AI 直接猜 workflow_type 后无校验绑定。

v1.2 目标：
- 适配 ComfyUI 里所有常见主模型类型，不再只有 checkpoint / anima / z-image 三种；
- 每个模型类型都有对应的能力描述（get_model_capabilities）与独立工作流构建器；
- 未知类型明确报错，绝不 fallback 回 checkpoint 工作流。
"""
from __future__ import annotations

import json
import struct
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

MODEL_KINDS = [
    "checkpoints",
    "loras",
    "vae",
    "clip",
    "text_encoders",
    "unet",
    "diffusion_models",
    "controlnet",
    "upscale_models",
]

CHECKPOINT_TYPES = {"sdxl", "sdxl_illustrious", "sdxl_pony", "sd15", "sdxl_realistic"}
ANIMA_TYPE = "anima_qwen_unet"
Z_IMAGE_TYPE = "z_image"
FLUX_TYPE = "flux"
NUNCHAKU_TYPE = "nunchaku"
GGUF_TYPE = "gguf"
DIFFUSION_MODEL_TYPE = "diffusion_model"
UNKNOWN_TYPE = "unknown"

# 需要「分体式」加载（UNet + CLIP + VAE 分开）的主模型类型。
SPLIT_MODEL_TYPES = {ANIMA_TYPE, Z_IMAGE_TYPE, FLUX_TYPE, NUNCHAKU_TYPE, GGUF_TYPE, DIFFUSION_MODEL_TYPE}


def fetch_object_info(api_url: str, timeout: int = 20) -> dict[str, Any]:
    r = requests.get(f"{api_url}/object_info", timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, dict) else {}


def list_comfy_models(api_url: str, kind: str, timeout: int = 20) -> list[str]:
    try:
        r = requests.get(f"{api_url}/api/models/{kind}", timeout=timeout)
        if r.status_code < 500:
            data = r.json()
            if isinstance(data, list):
                return [str(x) for x in data]
    except Exception:
        pass
    return []


def discover_resources(api_url: str) -> dict[str, Any]:
    """读取 ComfyUI 资源索引和真实节点参数。
       如果 ComfyUI 不可达，直接抛出异常让调用方知道连接状态。
    """
    object_info = fetch_object_info(api_url)  # 失败时 raise，不自吞
    resources = {kind: list_comfy_models(api_url, kind) for kind in MODEL_KINDS}
    # GGUF 文本编码器不在 /api/models/text_encoders 里，从 CLIPLoaderGGUF 节点补扫。
    gguf_clips = extract_gguf_text_encoders(object_info)
    if gguf_clips:
        te = list(resources.get("text_encoders", []) or [])
        for x in gguf_clips:
            if x not in te:
                te.append(x)
        resources["text_encoders"] = te
    samplers, schedulers = extract_sampler_scheduler_options(object_info)
    return {
        "object_info_loaded": bool(object_info),
        "nodes": sorted(object_info.keys()),
        "resources": resources,
        "models_flat": flatten_model_resources(resources),
        "samplers": samplers,
        "schedulers": schedulers,
        "node_capabilities": summarize_node_capabilities(object_info),
    }


def flatten_model_resources(resources: dict[str, list[str]]) -> list[dict[str, str]]:
    out = []
    for kind, items in (resources or {}).items():
        for name in items or []:
            out.append({"name": name, "kind": kind, "model_type": classify_model(name, kind)})
    return out


def summarize_node_capabilities(object_info: dict[str, Any]) -> dict[str, bool]:
    keys = set(object_info or {})
    return {
        "checkpoint_workflow": {"CheckpointLoaderSimple", "CLIPTextEncode", "EmptyLatentImage", "KSampler", "VAEDecode", "SaveImage"}.issubset(keys),
        "anima_qwen_workflow": {"UNETLoader", "CLIPLoader", "VAELoader", "CLIPTextEncode", "EmptyLatentImage", "KSampler", "VAEDecode", "SaveImage"}.issubset(keys),
        "z_image_workflow": {"UNETLoader", "CLIPLoaderGGUF", "VAELoader", "CLIPTextEncodeLumina2", "ConditioningZeroOut", "EmptySD3LatentImage", "ModelSamplingAuraFlow", "KSampler", "VAEDecode", "SaveImage"}.issubset(keys),
        "flux_workflow": {"UNETLoader", "DualCLIPLoader", "VAELoader", "CLIPTextEncode", "FluxGuidance", "EmptySD3LatentImage", "KSampler", "VAEDecode", "SaveImage"}.issubset(keys),
        "gguf_workflow": {"UnetLoaderGGUF", "CLIPLoaderGGUF", "VAELoader", "CLIPTextEncode", "EmptyLatentImage", "KSampler", "VAEDecode", "SaveImage"}.issubset(keys),
        "nunchaku_workflow": {"NunchakuFluxDiTLoader", "NunchakuTextEncoderLoaderV2", "VAELoader", "CLIPTextEncode", "FluxGuidance", "EmptySD3LatentImage", "KSampler", "VAEDecode", "SaveImage"}.issubset(keys),
        "lora_full": "LoraLoader" in keys,
        "lora_model_only": "LoraLoaderModelOnly" in keys,
        "anima_lllite": "AnimaLLLiteApply" in keys,
        "nunchaku_flux": any(k.lower().startswith("nunchaku") for k in keys),
    }


def _options_from_required(required: dict[str, Any], key: str) -> list[str]:
    raw = required.get(key)
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, list):
            return [str(x) for x in first]
    return []


def extract_sampler_scheduler_options(object_info: dict[str, Any]) -> tuple[list[str], list[str]]:
    samplers: list[str] = []
    schedulers: list[str] = []
    for node_name in ["KSampler", "KSamplerAdvanced", "BasicScheduler"]:
        required = (((object_info or {}).get(node_name) or {}).get("input") or {}).get("required") or {}
        for x in _options_from_required(required, "sampler_name"):
            if x not in samplers:
                samplers.append(x)
        for x in _options_from_required(required, "scheduler"):
            if x not in schedulers:
                schedulers.append(x)
    return samplers, schedulers


def extract_gguf_text_encoders(object_info: dict[str, Any]) -> list[str]:
    """从 CLIPLoaderGGUF / DualCLIPLoaderGGUF 等节点的 clip_name 读取 GGUF 文本编码器。

    ComfyUI 的 /api/models/text_encoders 只返回 safetensors/ckpt，不返回 .gguf，
    而 Z-Image-Turbo 的 Lumina2 编码器是 GGUF，必须从这些节点的真实选项里补扫。
    """
    out: list[str] = []
    for node_name in ["CLIPLoaderGGUF", "DualCLIPLoaderGGUF", "TripleCLIPLoaderGGUF", "QuadrupleCLIPLoaderGGUF"]:
        node = (object_info or {}).get(node_name) or {}
        required = (node.get("input") or {}).get("required") or {}
        for key in ["clip_name", "clip_name1", "clip_name2", "clip_name3", "clip_name4"]:
            for x in _options_from_required(required, key):
                if x.lower().endswith(".gguf") and x not in out:
                    out.append(x)
    return out


# ── safetensors 架构识别（只读文件头，带缓存；移植自 SD Manager 的确定性规则）──
# 缓存值：(文件头 __metadata__ 字典, 张量名列表)。架构与元数据共用一次 IO。
_HEADER_CACHE: dict[tuple[str, int, int], tuple[dict[str, Any], list[str]]] = {}
_HEADER_CACHE_LOCK = threading.Lock()
_HEADER_CACHE_MAX = 512
_MAX_SAFETENSORS_HEADER = 64 * 1024 * 1024


def detect_architecture_from_tensor_keys(tensor_keys: list[str] | None) -> str:
    """从 safetensors 张量名识别架构，返回稳定标识。

    识别结果：qwen_image / flux / sd3 / checkpoint / diffusion_model /
    lora / vae / clip_vision / text_encoder，未知返回空字符串。
    很多模型文件 __metadata__ 为空，张量名是最可靠的架构信号。
    """
    keys = tensor_keys or []
    if not keys:
        return ""

    def has(fragment: str) -> bool:
        return any(fragment in k for k in keys)

    def has_any(*fragments: str) -> bool:
        return any(has(f) for f in fragments)

    # Qwen-Image / Anima：带 llm_adapter 的扩散主干。
    # ComfyUI 重打包格式是 model.diffusion_model.llm_adapter.*，diffusers 格式是 net.llm_adapter.*。
    if has("llm_adapter"):
        return "qwen_image"
    # Lumina2 / Z-Image-Turbo：cap_embedder / context_refiner / noise_refiner 是其独有结构。
    if has_any("cap_embedder", "context_refiner", "noise_refiner", "cap_pad_token"):
        return "lumina2"
    # Flux：double/single blocks 结构。
    if has_any("double_blocks", "single_blocks"):
        return "flux"
    # SD3 / SD3.5 MMDiT。
    if has("joint_blocks"):
        return "sd3"
    has_unet = has_any("model.diffusion_model", "diffusion_model")
    has_cond = has_any("cond_stage_model", "conditioner")
    has_vae = has_any("first_stage_model", "vae.", "encoder.", "decoder.")
    if has_unet and has_cond and has_vae:
        return "checkpoint"
    if has_unet:
        return "diffusion_model"
    if has_any("lora_unet_", "lora_te_", "lora_down", "lora_up"):
        return "lora"
    if has("quant_conv") or (has("encoder.") and has("decoder.")):
        return "vae"
    if has("vision_model"):
        return "clip_vision"
    if has_any("text_model", "transformer.", "text.", "model.layers."):
        return "text_encoder"
    return ""


def _read_safetensors_header(path: str | Path) -> tuple[dict[str, Any], list[str]]:
    """只读 safetensors 文件头，返回 (__metadata__, 张量名列表)。

    结果按 (路径, mtime, 大小) 缓存，避免重复 IO；只读 header 长度 + header JSON，
    不读权重；任何失败都返回 ({}, [])。
    """
    try:
        p = Path(path)
        st = p.stat()
        key = (str(p).lower(), int(st.st_mtime), int(st.st_size))
    except OSError:
        return {}, []
    with _HEADER_CACHE_LOCK:
        cached = _HEADER_CACHE.get(key)
    if cached is not None:
        return cached
    meta: dict[str, Any] = {}
    keys: list[str] = []
    try:
        with open(p, "rb") as f:
            raw = f.read(8)
            if len(raw) == 8:
                header_size = struct.unpack("<Q", raw)[0]
                if 0 < header_size <= _MAX_SAFETENSORS_HEADER:
                    header = json.loads(f.read(header_size))
                    if isinstance(header, dict):
                        raw_meta = header.get("__metadata__")
                        meta = raw_meta if isinstance(raw_meta, dict) else {}
                        keys = [k for k in header.keys() if k != "__metadata__"]
    except Exception:
        meta, keys = {}, []
    with _HEADER_CACHE_LOCK:
        _HEADER_CACHE[key] = (meta, keys)
        if len(_HEADER_CACHE) > _HEADER_CACHE_MAX:
            for k in list(_HEADER_CACHE)[:_HEADER_CACHE_MAX // 2]:
                _HEADER_CACHE.pop(k, None)
    return meta, keys


def read_safetensors_architecture(path: str | Path) -> str:
    """只读 safetensors 文件头判架构；失败返回空字符串。"""
    _, keys = _read_safetensors_header(path)
    return detect_architecture_from_tensor_keys(keys)


def read_safetensors_metadata(path: str | Path) -> dict[str, Any]:
    """读取 safetensors 文件头的 __metadata__（触发词 / 标签频率 / 基础架构等）。

    返回字典额外带 `_architecture`（张量名推断的架构）；失败返回 {}。
    """
    meta, keys = _read_safetensors_header(path)
    out = dict(meta or {})
    arch = detect_architecture_from_tensor_keys(keys)
    if arch:
        out["_architecture"] = arch
    return out


def _classify_by_name(name: str, kind: str = "") -> str:
    """按文件名 / ComfyUI 目录类别做保守分类（不读文件）。"""
    n = (name or "").lower()
    k = (kind or "").lower()
    if "lora" in k or k == "loras":
        return "lora"
    if k in {"vae"}:
        return "vae"
    if k in {"clip", "text_encoders"}:
        return "text_encoder"
    if "gguf" in n:
        return GGUF_TYPE
    if "nunchaku" in n or "svdq" in n or "fp4" in n or "int4" in n:
        return NUNCHAKU_TYPE
    # Z-Image-Turbo（Lumina2 FP8/GGUF）：本机已验证，走独立工作流，不是 checkpoint。
    if "z-image" in n or "z_image" in n or "zimage" in n:
        return Z_IMAGE_TYPE
    # Anima Aesthetic/Qwen UNet：本机已验证，不是 Illustrious。
    # 注意 "animagine"（Illustrious 系 checkpoint）含 "anima" 子串，必须排除。
    if "anima" in n and "animagine" not in n:
        if k in {"unet", "diffusion_models"} or "aesthetic" in n or "base" in n:
            return ANIMA_TYPE
        return "anima_unknown"
    if "flux" in n or "schnell" in n:
        return FLUX_TYPE
    if k in {"unet", "diffusion_models"}:
        return DIFFUSION_MODEL_TYPE
    if "pony" in n or "autismmix" in n:
        return "sdxl_pony"
    if "illustrious" in n or "wai" in n or "noobai" in n or "animagine" in n:
        return "sdxl_illustrious"
    if "realvis" in n or "juggernaut" in n or "dreamshaperxl" in n or "epicrealism" in n:
        return "sdxl_realistic"
    if "sdxl" in n or "xl" in n:
        return "sdxl"
    if k == "checkpoints":
        return "sd15"
    return UNKNOWN_TYPE


def classify_model(name: str, kind: str = "", architecture: str = "") -> str:
    """确定性模型分类。保守原则：不确定就 unknown，不强行套模板。

    ``architecture`` 是可选的真实架构信号（来自 safetensors 张量名），只在
    它能给出更可靠结论时覆盖文件名猜测；不传时行为与旧版完全一致。
    """
    base = _classify_by_name(name, kind)
    arch = (architecture or "").lower()
    if not arch:
        return base
    if arch == "qwen_image":
        return ANIMA_TYPE
    if arch == "lumina2":
        return Z_IMAGE_TYPE
    if arch == "flux":
        return FLUX_TYPE
    if arch == "sd3":
        # SD3/SD3.5 是分体式 MMDiT；先用通用分体工作流，clip_type 由构建器决定。
        return DIFFUSION_MODEL_TYPE
    if arch == "diffusion_model":
        # 只有文件名给不出更具体类型时才降级为通用分体。
        if base in CHECKPOINT_TYPES or base == UNKNOWN_TYPE:
            return DIFFUSION_MODEL_TYPE
        return base
    if arch == "checkpoint":
        # 合体 checkpoint：若文件名误判成分体/未知，回到 checkpoint 分支。
        if base in {DIFFUSION_MODEL_TYPE, UNKNOWN_TYPE}:
            return "sd15" if (kind or "").lower() == "checkpoints" else base
        return base
    return base


def get_model_capabilities(model_type: str) -> dict[str, Any]:
    """返回某模型类型的能力描述，供后端写绑定、前端自适应 UI。

    字段：
    - supports_negative_prompt: 是否支持真实负向提示词
    - supports_lora: 是否支持 LoRA（当前自动构建器是否会注入 LoRA 链）
    - needs_clip / needs_vae: 是否为分体式（需要单独选择文本编码器 / VAE）
    - clip_mode: "single"（一个文本编码器）/ "dual"（双编码器，如 flux）/ "none"
    - upscale_recommendation: 放大建议文案
    """
    base = {
        "supports_negative_prompt": True,
        "supports_lora": True,
        "needs_clip": False,
        "needs_vae": False,
        "clip_mode": "none",
        "upscale_recommendation": "",
        "workflow_builder": None,
    }
    if model_type in CHECKPOINT_TYPES:
        base.update({
            "supports_negative_prompt": True,
            "supports_lora": True,
            "needs_clip": False,
            "needs_vae": False,
            "clip_mode": "none",
            "upscale_recommendation": "SDXL/SD1.5 推荐 RealESRGAN_x4plus / 4x-UltraSharp 放大",
            "workflow_builder": "checkpoint",
        })
    elif model_type in (ANIMA_TYPE, "anima_unknown"):
        base.update({
            "supports_negative_prompt": True,
            "supports_lora": True,
            "needs_clip": True,
            "needs_vae": True,
            "clip_mode": "single",
            "upscale_recommendation": "Anima/Qwen 建议图库放大（RealESRGAN anime_6B）",
            "workflow_builder": "anima",
        })
    elif model_type == Z_IMAGE_TYPE:
        base.update({
            "supports_negative_prompt": False,
            "supports_lora": False,
            "needs_clip": True,
            "needs_vae": True,
            "clip_mode": "single",
            "upscale_recommendation": "Z-Image-Turbo 无负向提示词；放大建议 RealESRGAN",
            "workflow_builder": "z_image",
        })
    elif model_type == FLUX_TYPE:
        base.update({
            "supports_negative_prompt": False,
            "supports_lora": True,
            "needs_clip": True,
            "needs_vae": True,
            "clip_mode": "dual",
            "upscale_recommendation": "Flux 建议 4x-UltraSharp 或高分辨率图生图放大",
            "workflow_builder": "flux",
        })
    elif model_type == NUNCHAKU_TYPE:
        base.update({
            "supports_negative_prompt": False,
            "supports_lora": False,
            "needs_clip": True,
            "needs_vae": True,
            "clip_mode": "dual",
            "upscale_recommendation": "Nunchaku 量化模型建议 4x 放大（LoRA 需专用加载器，暂不自动注入）",
            "workflow_builder": "nunchaku",
        })
    elif model_type == GGUF_TYPE:
        base.update({
            "supports_negative_prompt": True,
            "supports_lora": True,
            "needs_clip": True,
            "needs_vae": True,
            "clip_mode": "single",
            "upscale_recommendation": "GGUF 量化模型建议 RealESRGAN 放大",
            "workflow_builder": "gguf",
        })
    elif model_type == DIFFUSION_MODEL_TYPE:
        base.update({
            "supports_negative_prompt": True,
            "supports_lora": True,
            "needs_clip": True,
            "needs_vae": True,
            "clip_mode": "single",
            "upscale_recommendation": "分体式 DiT 模型建议 RealESRGAN / 4x 放大",
            "workflow_builder": "diffusion_model",
        })
    else:
        # unknown / lora / vae / text_encoder 等不可直接作为主模型生图的类型：保守默认，
        # 但 workflow_builder 保持 None，由 build_workflow_for_model 明确拒绝。
        base.update({
            "supports_negative_prompt": True,
            "supports_lora": True,
            "needs_clip": False,
            "needs_vae": False,
            "clip_mode": "none",
            "upscale_recommendation": "",
            "workflow_builder": None,
        })
    return base


def clip_options_for_model(model_type: str, resources: dict[str, Any]) -> list[str]:
    """返回某模型类型可选的文本编码器列表（去重）。

    CLIPLoader（safetensors/ckpt）与 CLIPLoaderGGUF（.gguf）互斥：
    - Anima / 通用 diffusion_model 走 CLIPLoader，排除 .gguf；
    - z_image / gguf 走 CLIPLoaderGGUF，保留 .gguf。
    """
    res = (resources or {}).get("resources", {}) or {}
    seen = set()
    opts: list[str] = []
    for x in (res.get("text_encoders", []) or []) + (res.get("clip", []) or []):
        x = str(x)
        if x not in seen:
            seen.add(x)
            opts.append(x)
    if model_type in (ANIMA_TYPE, "anima_unknown", DIFFUSION_MODEL_TYPE):
        opts = [x for x in opts if not x.lower().endswith(".gguf")]
    return opts


# ── 采样器别名归一化（A1111/CivitAI 展示名 → ComfyUI KSampler 枚举）──────────
_SAMPLER_ALIASES = {
    "euler": "euler",
    "euler a": "euler_ancestral",
    "euler ancestral": "euler_ancestral",
    "euler_ancestral": "euler_ancestral",
    "heun": "heun",
    "dpm2": "dpm_2",
    "dpm 2": "dpm_2",
    "dpm_2": "dpm_2",
    "dpm2 a": "dpm_2_ancestral",
    "dpm 2 a": "dpm_2_ancestral",
    "dpm_2_ancestral": "dpm_2_ancestral",
    "lms": "lms",
    "dpm fast": "dpm_fast",
    "dpm_fast": "dpm_fast",
    "dpm adaptive": "dpm_adaptive",
    "dpm_adaptive": "dpm_adaptive",
    "dpmpp sde": "dpmpp_sde",
    "dpm++ sde": "dpmpp_sde",
    "dpmpp_sde": "dpmpp_sde",
    "dpmpp 2s a": "dpmpp_2s_ancestral",
    "dpm++ 2s a": "dpmpp_2s_ancestral",
    "dpmpp_2s_ancestral": "dpmpp_2s_ancestral",
    "dpmpp 2m": "dpmpp_2m",
    "dpm++ 2m": "dpmpp_2m",
    "dpmpp_2m": "dpmpp_2m",
    "dpmpp 2m sde": "dpmpp_2m_sde",
    "dpm++ 2m sde": "dpmpp_2m_sde",
    "dpmpp_2m_sde": "dpmpp_2m_sde",
    "dpmpp 3m sde": "dpmpp_3m_sde",
    "dpm++ 3m sde": "dpmpp_3m_sde",
    "dpmpp_3m_sde": "dpmpp_3m_sde",
    "unipc": "uni_pc",
    "uni_pc": "uni_pc",
    "ddim": "ddim",
    "ddpm": "ddpm",
    "deis": "deis",
    "ipndm": "ipndm",
    "ipndm_v": "ipndm_v",
    "res_multistep": "res_multistep",
}


def normalize_sampler(sampler: str, scheduler: str = "normal") -> tuple[str, str]:
    """把 A1111/CivitAI 的采样器展示名映射成 ComfyUI 枚举，并拆出 Karras 调度器。

    未知名称原样返回小写，让 ComfyUI 的「value not in list」错误暴露出来，
    而不是被静默替换成别的采样器。
    """
    s = str(sampler or "").strip().lower()
    sch = str(scheduler or "normal").strip().lower()
    if s.endswith(" karras"):
        s = s[: -len(" karras")].strip()
        if sch == "normal":
            sch = "karras"
    return _SAMPLER_ALIASES.get(s, s), sch


# ── ComfyUI 失败信息翻译（把原始报错变成用户能懂的提示）──────────────────────
def translate_comfy_failure(detail: Any) -> dict[str, str]:
    """把 ComfyUI 的原始错误翻译成 {error_code, error, technical_detail}。"""
    raw = str(detail or "")
    lowered = raw.lower()
    if any(t in lowered for t in ("size mismatch", "shape mismatch", "tensor", "architecture", "cannot be multiplied", "mat1 and mat2")):
        return {
            "error_code": "model_architecture_mismatch",
            "error": "模型组件可能不兼容：请检查主模型与 LoRA / VAE / CLIP 的架构是否匹配，或重新绑定工作流。",
            "technical_detail": raw,
        }
    if "out of memory" in lowered or "cuda oom" in lowered:
        return {
            "error_code": "out_of_memory",
            "error": "显存不足：请降低分辨率 / 批量，或关闭其他占用显存的程序后重试。",
            "technical_detail": raw,
        }
    if "not in list" in lowered or "not found" in lowered or "no such file" in lowered:
        return {
            "error_code": "model_or_asset_missing",
            "error": "模型或资源文件缺失 / 名称不匹配：请点「刷新」重新扫描 ComfyUI 资源后重试。",
            "technical_detail": raw,
        }
    return {
        "error_code": "comfy_request_failed",
        "error": "ComfyUI 未能接受本次生成请求。",
        "technical_detail": raw,
    }


# ── 自描述生成包（写进 PNG，任何图都能复现）──────────────────────────────────
PORTABLE_SCHEMA = "qwenpaw-image-gen.portable/v1"
PORTABLE_PNG_KEY = "qwenpaw_image_gen"
_PORTABLE_MODEL_INPUTS = {
    "ckpt_name": "checkpoints",
    "lora_name": "loras",
    "vae_name": "vae",
    "unet_name": "unet",
    "clip_name": "text_encoders",
    "clip_name1": "text_encoders",
    "clip_name2": "text_encoders",
    "control_net_name": "controlnet",
}


def workflow_model_dependencies(workflow: dict[str, Any] | None) -> list[dict[str, str]]:
    """收集工作流里用到的模型文件引用（只记录文件名与类型，不含本机路径）。"""
    seen: set[tuple[str, str]] = set()
    deps: list[dict[str, str]] = []
    for node in (workflow or {}).values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs") or {}
        for key, model_type in _PORTABLE_MODEL_INPUTS.items():
            value = inputs.get(key)
            if isinstance(value, str) and value and (value, model_type) not in seen:
                seen.add((value, model_type))
                deps.append({"file_name": value, "model_type": model_type})
    return deps


def build_portable_package(workflow: dict[str, Any], params: dict[str, Any] | None = None, model_type: str = "") -> dict[str, Any]:
    """构建自描述生成包：工作流 + 生成参数 + 模型依赖清单（不含本机路径）。"""
    p = params or {}
    generation = {k: p.get(k) for k in (
        "prompt", "negative_prompt", "model_name", "loras", "lora_name",
        "steps", "cfg", "seed", "width", "height", "sampler_name", "scheduler",
        "denoise", "clip_name", "vae_name", "clip_l_name", "t5xxl_name", "category",
    ) if p.get(k) is not None}
    return {
        "schema": PORTABLE_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "generator": {"name": "QwenPaw 生图助手", "backend": "ComfyUI"},
        "model_type": model_type or "",
        "workflow_api": workflow,
        "generation": generation,
        "dependencies": workflow_model_dependencies(workflow),
    }


def find_missing_dependencies(package: dict[str, Any] | None, resources: dict[str, Any] | None) -> list[dict[str, str]]:
    """把自描述包里的模型依赖与当前 ComfyUI 实时资源比对，返回缺失项。"""
    by_kind = ((resources or {}).get("resources") or {}) if isinstance(resources, dict) else {}
    available: set[str] = set()
    for items in by_kind.values():
        for x in items or []:
            available.add(str(x))
    missing: list[dict[str, str]] = []
    for dep in ((package or {}).get("dependencies") or []):
        if not isinstance(dep, dict):
            continue
        name = str(dep.get("file_name") or "")
        if name and name not in available:
            missing.append({"file_name": name, "model_type": str(dep.get("model_type") or "")})
    return missing


# 预设里常见的「checkpoint 专用」类型名（用户自建预设的 model_type 是自由文本）
_CHECKPOINT_WF_TOKENS = ("pony", "sdxl", "illustrious", "sd15", "sd1.5", "checkpoint", "realistic")


def _is_checkpoint_workflow_type(workflow_type: str) -> bool:
    wf = (workflow_type or "").lower()
    return wf in CHECKPOINT_TYPES or any(t in wf for t in _CHECKPOINT_WF_TOKENS)


def validate_workflow_type_for_model(model_name: str, workflow_type: str, kind: str = "", architecture: str = "") -> tuple[bool, str, str]:
    """校验 AI 或用户传入的 workflow_type 是否明显错误。返回 ok, corrected_type, message。"""
    model_type = classify_model(model_name, kind, architecture)
    wf = (workflow_type or "").lower()
    if model_type == ANIMA_TYPE and "illustrious" in wf:
        return False, ANIMA_TYPE, "Anima/Qwen UNet 模型不能绑定为 Illustrious/SDXL checkpoint 工作流"
    if model_type in {ANIMA_TYPE, Z_IMAGE_TYPE, FLUX_TYPE, NUNCHAKU_TYPE, GGUF_TYPE, DIFFUSION_MODEL_TYPE} and _is_checkpoint_workflow_type(wf):
        return False, model_type, f"{model_type} 不是普通 checkpoint，不能绑定为「{workflow_type or wf}」工作流；请改用自动适配或对应类型的工作流。"
    return True, workflow_type or model_type, "ok"


def build_param_schema(samplers: list[str] | None = None, schedulers: list[str] | None = None, model_type: str = "sdxl") -> dict[str, Any]:
    samplers = samplers or []
    schedulers = schedulers or []
    base = {
        "steps": {"type": "number", "label": "采样步数", "min": 1, "max": 80, "step": 1, "default": 20},
        "cfg": {"type": "number", "label": "CFG", "min": 0, "max": 20, "step": 0.5, "default": 7},
        "sampler_name": {"type": "select", "label": "采样器", "default": "euler", "options": samplers},
        "scheduler": {"type": "select", "label": "调度器", "default": "normal", "options": schedulers},
        "width": {"type": "number", "label": "宽度", "min": 256, "max": 2048, "step": 64, "default": 1024},
        "height": {"type": "number", "label": "高度", "min": 256, "max": 2048, "step": 64, "default": 1024},
        "seed": {"type": "number", "label": "Seed", "min": -1, "max": 2147483647, "step": 1, "default": -1},
        "batch_size": {"type": "number", "label": "批量", "min": 1, "max": 8, "step": 1, "default": 1},
        "denoise": {"type": "number", "label": "重绘幅度", "min": 0, "max": 1, "step": 0.05, "default": 1},
    }
    if model_type == ANIMA_TYPE:
        base["steps"]["default"] = 25
        base["cfg"]["default"] = 1.0
        base["cfg"]["label"] = "CFG（Anima 推荐 1.0）"
        base["sampler_name"]["default"] = "euler" if "euler" in samplers else (samplers[0] if samplers else "euler")
        base["scheduler"]["default"] = "normal" if "normal" in schedulers else (schedulers[0] if schedulers else "normal")
    if model_type == Z_IMAGE_TYPE:
        base["steps"]["default"] = 25
        base["cfg"]["default"] = 1.0
        base["cfg"]["label"] = "CFG（Z-Image-Turbo 推荐 1.0）"
        base["sampler_name"]["default"] = "res_multistep" if "res_multistep" in samplers else (samplers[0] if samplers else "res_multistep")
        base["scheduler"]["default"] = "sgm_uniform" if "sgm_uniform" in schedulers else (schedulers[0] if schedulers else "sgm_uniform")
    if model_type in (FLUX_TYPE, NUNCHAKU_TYPE):
        base["steps"]["default"] = 20
        base["cfg"]["default"] = 1.0
        base["cfg"]["label"] = "CFG（Flux/Nunchaku 推荐 1.0）"
        base["sampler_name"]["default"] = "euler" if "euler" in samplers else (samplers[0] if samplers else "euler")
        base["scheduler"]["default"] = "simple" if "simple" in schedulers else (schedulers[0] if schedulers else "simple")
    return base


def _resolve_seed(params: dict[str, Any]) -> int:
    seed = int(params.get("seed", -1) or -1)
    if seed < 0:
        seed = int(time.time() * 1000) % 2**32
    return seed


def _sampler_inputs(params: dict[str, Any], model: list[Any], positive: list[Any], negative: list[Any], latent: list[Any], default_steps: int = 20, default_cfg: float = 7.0, default_scheduler: str = "normal") -> dict[str, Any]:
    """构造 KSampler 节点 inputs，保持与现有 checkpoint 构建器一致的字段顺序。"""
    return {
        "seed": _resolve_seed(params),
        "steps": int(params.get("steps", default_steps)),
        "cfg": float(params.get("cfg", default_cfg)),
        "sampler_name": params.get("sampler_name", "euler"),
        "scheduler": params.get("scheduler", default_scheduler),
        "denoise": float(params.get("denoise", 1.0)),
        "model": model,
        "positive": positive,
        "negative": negative,
        "latent_image": latent,
    }


def build_checkpoint_workflow(params: dict[str, Any]) -> dict[str, Any]:
    seed = _resolve_seed(params)
    model_name = params.get("model_name", "")
    prompt_text = params.get("prompt", "")
    neg_text = params.get("negative_prompt", "")
    workflow: dict[str, Any] = {
        "3": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": int(params.get("steps", 20)), "cfg": float(params.get("cfg", 7.0)), "sampler_name": params.get("sampler_name", "euler"), "scheduler": params.get("scheduler", "normal"), "denoise": float(params.get("denoise", 1.0)), "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": model_name}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": int(params.get("width", 1024)), "height": int(params.get("height", 1024)), "batch_size": int(params.get("batch_size", 1))}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt_text, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": neg_text, "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "image_gen", "images": ["8", 0]}},
    }
    apply_lora_chain(workflow, params, model_ref=["4", 0], clip_ref=["4", 1], start_id=10, mode="full")
    return workflow


def build_anima_qwen_workflow(params: dict[str, Any]) -> dict[str, Any]:
    """本机已验证 Anima Aesthetic V1.1 / Qwen Image 结构。"""
    seed = _resolve_seed(params)
    unet_name = params.get("model_name") or params.get("unet_name") or "example-unet.safetensors"
    clip_name = params.get("clip_name") or "example-clip.safetensors"
    vae_name = params.get("vae_name") or "example-vae.safetensors"
    workflow: dict[str, Any] = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": unet_name, "weight_dtype": (params.get("weight_dtype") or "default")}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip_name, "type": (params.get("clip_type") or "qwen_image"), "device": (params.get("clip_device") or "default")}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae_name}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": params.get("prompt", ""), "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": params.get("negative_prompt", ""), "clip": ["2", 0]}},
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": int(params.get("width", 1024)), "height": int(params.get("height", 1024)), "batch_size": int(params.get("batch_size", 1))}},
        "7": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": int(params.get("steps", 25)), "cfg": float(params.get("cfg", 1.0)), "sampler_name": params.get("sampler_name", "euler"), "scheduler": params.get("scheduler", "normal"), "denoise": float(params.get("denoise", 1.0)), "model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "image_gen_anima", "images": ["8", 0]}},
    }
    apply_lora_chain(workflow, params, model_ref=["1", 0], clip_ref=None, start_id=10, mode="model_only")
    return workflow


def build_z_image_turbo_workflow(params: dict[str, Any]) -> dict[str, Any]:
    """Z-Image-Turbo（Lumina2）本机已验证结构。

    DiT 用 FP8 safetensors（UNETLoader + fp8_e4m3fn），文本编码器用 GGUF
    （CLIPLoaderGGUF + lumina2），VAE 用 ae.safetensors。Lumina2 没有真实负向
    提示词，用 ConditioningZeroOut 占位；潜空间用 EmptySD3LatentImage，采样前
    用 ModelSamplingAuraFlow(shift=3) 修正 shift。
    """
    seed = _resolve_seed(params)
    unet_name = params.get("model_name") or params.get("unet_name") or "z-image-turbo-fp8-e4m3fn.safetensors"
    clip_name = params.get("clip_name") or "Qwen_3_4b-imatrix-IQ4_XS.gguf"
    vae_name = params.get("vae_name") or "ae.safetensors"
    prompt = params.get("prompt", "")
    workflow: dict[str, Any] = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": unet_name, "weight_dtype": (params.get("weight_dtype") or "fp8_e4m3fn")}},
        "2": {"class_type": "CLIPLoaderGGUF", "inputs": {"clip_name": clip_name, "type": (params.get("clip_type") or "lumina2")}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae_name}},
        "4": {"class_type": "CLIPTextEncodeLumina2", "inputs": {"system_prompt": (params.get("system_prompt") or "superior"), "user_prompt": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
        "6": {"class_type": "EmptySD3LatentImage", "inputs": {"width": int(params.get("width", 1024)), "height": int(params.get("height", 1024)), "batch_size": int(params.get("batch_size", 1))}},
        "7": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": float(params.get("shift", 3.0))}},
        "8": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": int(params.get("steps", 25)), "cfg": float(params.get("cfg", 1.0)), "sampler_name": params.get("sampler_name", "res_multistep"), "scheduler": params.get("scheduler", "sgm_uniform"), "denoise": float(params.get("denoise", 1.0)), "model": ["7", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"filename_prefix": "image_gen_zimage", "images": ["9", 0]}},
    }
    return workflow


def build_flux_workflow(params: dict[str, Any]) -> dict[str, Any]:
    """Flux.1 最小可运行结构（classic 模板）。

    UNETLoader + DualCLIPLoader(t5xxl + clip_l) + VAELoader，正向 CLIPTextEncode 后
    接 FluxGuidance，负向恒为空字符串（Flux 没有真实负向提示词），cfg 固定 1.0。
    """
    unet_name = params.get("model_name") or params.get("unet_name") or "flux1-dev.safetensors"
    t5xxl_name = params.get("t5xxl_name") or params.get("clip_name1") or "t5xxl_fp16.safetensors"
    clip_l_name = params.get("clip_l_name") or params.get("clip_name2") or "clip_l.safetensors"
    vae_name = params.get("vae_name") or "ae.safetensors"
    prompt = params.get("prompt", "")
    guidance = float(params.get("guidance", 3.5))
    workflow: dict[str, Any] = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": unet_name, "weight_dtype": (params.get("weight_dtype") or "default")}},
        "2": {"class_type": "DualCLIPLoader", "inputs": {"clip_name1": t5xxl_name, "clip_name2": clip_l_name, "type": (params.get("clip_type") or "flux")}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae_name}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}},
        "6": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["4", 0], "guidance": guidance}},
        "7": {"class_type": "EmptySD3LatentImage", "inputs": {"width": int(params.get("width", 1024)), "height": int(params.get("height", 1024)), "batch_size": int(params.get("batch_size", 1))}},
        "8": {"class_type": "KSampler", "inputs": _sampler_inputs(params, ["1", 0], ["6", 0], ["5", 0], ["7", 0], default_steps=20, default_cfg=1.0, default_scheduler="simple")},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"filename_prefix": "image_gen_flux", "images": ["9", 0]}},
    }
    apply_lora_chain(workflow, params, model_ref=["1", 0], clip_ref=None, start_id=11, mode="model_only", model_consumers=["8"])
    return workflow


def build_nunchaku_workflow(params: dict[str, Any]) -> dict[str, Any]:
    """Nunchaku（SVDQuant）Flux 量化模型最小结构。

    使用 ComfyUI-nunchaku 的 NunchakuFluxDiTLoader + NunchakuTextEncoderLoaderV2，
    其余与 Flux classic 模板一致。该路径依赖 nunchaku 量化模型与加载节点已安装；
    未安装时 ComfyUI 会在提交时明确报节点/模型缺失，而不是被误塞进 checkpoint。
    """
    model_path = params.get("model_name") or params.get("model_path") or "svdq-int4-flux.1-dev"
    t5xxl_name = params.get("t5xxl_name") or params.get("text_encoder1") or "t5xxl_fp16.safetensors"
    clip_l_name = params.get("clip_l_name") or params.get("text_encoder2") or "clip_l.safetensors"
    vae_name = params.get("vae_name") or "ae.safetensors"
    prompt = params.get("prompt", "")
    guidance = float(params.get("guidance", 3.5))
    workflow: dict[str, Any] = {
        "1": {"class_type": "NunchakuFluxDiTLoader", "inputs": {
            "model_path": model_path,
            "cache_threshold": float(params.get("cache_threshold", 0.0)),
            "attention": (params.get("attention") or "nunchaku-fp16"),
            "cpu_offload": (params.get("cpu_offload") or "auto"),
            "device_id": int(params.get("device_id", 0)),
            "data_type": (params.get("data_type") or "bfloat16"),
        }},
        "2": {"class_type": "NunchakuTextEncoderLoaderV2", "inputs": {
            "model_type": "flux.1",
            "text_encoder1": t5xxl_name,
            "text_encoder2": clip_l_name,
            "t5_min_length": int(params.get("t5_min_length", 512)),
        }},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae_name}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}},
        "6": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["4", 0], "guidance": guidance}},
        "7": {"class_type": "EmptySD3LatentImage", "inputs": {"width": int(params.get("width", 1024)), "height": int(params.get("height", 1024)), "batch_size": int(params.get("batch_size", 1))}},
        "8": {"class_type": "KSampler", "inputs": _sampler_inputs(params, ["1", 0], ["6", 0], ["5", 0], ["7", 0], default_steps=20, default_cfg=1.0, default_scheduler="simple")},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"filename_prefix": "image_gen_nunchaku", "images": ["9", 0]}},
    }
    return workflow


def build_gguf_workflow(params: dict[str, Any]) -> dict[str, Any]:
    """GGUF 量化 UNet 模型最小结构：UnetLoaderGGUF + CLIPLoaderGGUF + VAELoader。"""
    unet_name = params.get("model_name") or params.get("unet_name") or "example-unet-Q4_K_S.gguf"
    clip_name = params.get("clip_name") or "example-clip.safetensors"
    vae_name = params.get("vae_name") or "example-vae.safetensors"
    prompt = params.get("prompt", "")
    neg_text = params.get("negative_prompt", "")
    workflow: dict[str, Any] = {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": unet_name}},
        "2": {"class_type": "CLIPLoaderGGUF", "inputs": {"clip_name": clip_name, "type": (params.get("clip_type") or "stable_diffusion")}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae_name}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": neg_text, "clip": ["2", 0]}},
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": int(params.get("width", 1024)), "height": int(params.get("height", 1024)), "batch_size": int(params.get("batch_size", 1))}},
        "7": {"class_type": "KSampler", "inputs": _sampler_inputs(params, ["1", 0], ["4", 0], ["5", 0], ["6", 0])},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "image_gen_gguf", "images": ["8", 0]}},
    }
    apply_lora_chain(workflow, params, model_ref=["1", 0], clip_ref=None, start_id=10, mode="model_only")
    return workflow


def build_diffusion_model_workflow(params: dict[str, Any]) -> dict[str, Any]:
    """通用分体式扩散模型（unet/diffusion_models 目录下的 safetensors）。

    与 Anima 同构，但 clip_type 默认 stable_diffusion，不预设 Anima 专属类型。
    """
    unet_name = params.get("model_name") or params.get("unet_name") or "example-unet.safetensors"
    clip_name = params.get("clip_name") or "example-clip.safetensors"
    vae_name = params.get("vae_name") or "example-vae.safetensors"
    prompt = params.get("prompt", "")
    neg_text = params.get("negative_prompt", "")
    workflow: dict[str, Any] = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": unet_name, "weight_dtype": (params.get("weight_dtype") or "default")}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip_name, "type": (params.get("clip_type") or "stable_diffusion"), "device": (params.get("clip_device") or "default")}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae_name}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": neg_text, "clip": ["2", 0]}},
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": int(params.get("width", 1024)), "height": int(params.get("height", 1024)), "batch_size": int(params.get("batch_size", 1))}},
        "7": {"class_type": "KSampler", "inputs": _sampler_inputs(params, ["1", 0], ["4", 0], ["5", 0], ["6", 0])},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "image_gen_diffusion", "images": ["8", 0]}},
    }
    apply_lora_chain(workflow, params, model_ref=["1", 0], clip_ref=None, start_id=10, mode="model_only")
    return workflow


def _add_model_loaders(workflow: dict[str, Any], start_id: int, model_type: str, params: dict[str, Any]) -> tuple[list[Any], list[Any], list[Any], int, str]:
    """按模型类型写入加载器节点，返回 (model_ref, clip_ref, vae_ref, next_id, prompt_mode)。"""
    nid = start_id

    def take() -> str:
        nonlocal nid
        s = str(nid)
        nid += 1
        return s

    if model_type in CHECKPOINT_TYPES:
        cid = take()
        workflow[cid] = {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": params.get("model_name", "")}}
        return [cid, 0], [cid, 1], [cid, 2], nid, "plain"
    if model_type == Z_IMAGE_TYPE:
        u, c, v = take(), take(), take()
        workflow[u] = {"class_type": "UNETLoader", "inputs": {"unet_name": params.get("model_name", ""), "weight_dtype": (params.get("weight_dtype") or "fp8_e4m3fn")}}
        workflow[c] = {"class_type": "CLIPLoaderGGUF", "inputs": {"clip_name": params.get("clip_name") or "Qwen_3_4b-imatrix-IQ4_XS.gguf", "type": (params.get("clip_type") or "lumina2")}}
        workflow[v] = {"class_type": "VAELoader", "inputs": {"vae_name": params.get("vae_name") or "ae.safetensors"}}
        return [u, 0], [c, 0], [v, 0], nid, "lumina2"
    if model_type in (FLUX_TYPE, NUNCHAKU_TYPE):
        u, c, v = take(), take(), take()
        if model_type == FLUX_TYPE:
            workflow[u] = {"class_type": "UNETLoader", "inputs": {"unet_name": params.get("model_name", ""), "weight_dtype": (params.get("weight_dtype") or "default")}}
            workflow[c] = {"class_type": "DualCLIPLoader", "inputs": {"clip_name1": params.get("t5xxl_name") or params.get("clip_name1") or "t5xxl_fp16.safetensors", "clip_name2": params.get("clip_l_name") or params.get("clip_name2") or "clip_l.safetensors", "type": (params.get("clip_type") or "flux")}}
        else:
            workflow[u] = {"class_type": "NunchakuFluxDiTLoader", "inputs": {"model_path": params.get("model_name", ""), "cache_threshold": float(params.get("cache_threshold", 0.0)), "attention": (params.get("attention") or "nunchaku-fp16"), "cpu_offload": (params.get("cpu_offload") or "auto"), "device_id": int(params.get("device_id", 0)), "data_type": (params.get("data_type") or "bfloat16")}}
            workflow[c] = {"class_type": "NunchakuTextEncoderLoaderV2", "inputs": {"model_type": "flux.1", "text_encoder1": params.get("t5xxl_name") or "t5xxl_fp16.safetensors", "text_encoder2": params.get("clip_l_name") or "clip_l.safetensors", "t5_min_length": 512}}
        workflow[v] = {"class_type": "VAELoader", "inputs": {"vae_name": params.get("vae_name") or "ae.safetensors"}}
        return [u, 0], [c, 0], [v, 0], nid, "flux"
    if model_type == GGUF_TYPE:
        u, c, v = take(), take(), take()
        workflow[u] = {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": params.get("model_name", "")}}
        workflow[c] = {"class_type": "CLIPLoaderGGUF", "inputs": {"clip_name": params.get("clip_name") or "example-clip.safetensors", "type": (params.get("clip_type") or "stable_diffusion")}}
        workflow[v] = {"class_type": "VAELoader", "inputs": {"vae_name": params.get("vae_name") or "example-vae.safetensors"}}
        return [u, 0], [c, 0], [v, 0], nid, "plain"
    if model_type in (ANIMA_TYPE, "anima_unknown", DIFFUSION_MODEL_TYPE):
        u, c, v = take(), take(), take()
        default_clip_type = "qwen_image" if model_type in (ANIMA_TYPE, "anima_unknown") else "stable_diffusion"
        workflow[u] = {"class_type": "UNETLoader", "inputs": {"unet_name": params.get("model_name", ""), "weight_dtype": (params.get("weight_dtype") or "default")}}
        workflow[c] = {"class_type": "CLIPLoader", "inputs": {"clip_name": params.get("clip_name") or "example-clip.safetensors", "type": (params.get("clip_type") or default_clip_type), "device": (params.get("clip_device") or "default")}}
        workflow[v] = {"class_type": "VAELoader", "inputs": {"vae_name": params.get("vae_name") or "example-vae.safetensors"}}
        return [u, 0], [c, 0], [v, 0], nid, "plain"
    raise ValueError(
        f"无法为模型「{params.get('model_name', '')}」构建放大/高清修复工作流：未识别的模型类型 {model_type!r}。"
        "如果这张图是放大产物或模型信息缺失，请改用「快速放大」。"
    )


UPSCALE_QUALITY_PROMPT = (
    "masterpiece, best quality, ultra detailed, highly detailed, sharp focus, "
    "clean lineart, intricate details, high resolution"
)


def build_upscale_workflow(params: dict[str, Any], image_name: str, opts: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    """构建放大工作流。四种模式：

    - fast：UpscaleModelLoader + ImageUpscaleWithModel（纯像素放大，不补细节）
    - hires：像素放大 → VAEEncode → KSampler(低 denoise) → VAEDecode（高清修复，补细节）
    - tiled：UltimateSDUpscale 分块二次采样（超大图 / 显存不够）
    - iterative：IterativeImageUpscale 分多步渐进放大（2x→4x 逐级补细节）

    返回 (workflow, model_type)。fast 模式不需要原模型，其余需要。
    """
    o = opts or {}
    mode = str(o.get("mode") or "fast").lower()
    # 放大提示词策略：
    #   auto    —— 分块模式低幅度沿用原图提示词（保留 LoRA/角色触发词，人物才不会变），高幅度用画质词（防块里长新主体）
    #   quality —— 一律用画质词
    #   full    —— 一律沿用原图提示词
    # 背景：分块模式会把提示词在每个分块上各执行一遍：denoise 高时角色标签会让空背景块"长出"新角色；
    # 但完全换成画质词后，角色 LoRA 的触发词也没了，低幅度下人物形象会漂移。所以按 denoise 分段。
    prompt_strategy = str(o.get("prompt_mode") or "auto").lower()
    if prompt_strategy not in {"auto", "quality", "full"}:
        prompt_strategy = "auto"
    raw_denoise = o.get("denoise")
    denoise = float(raw_denoise if raw_denoise is not None else (0.2 if mode == "tiled" else (0.4 if mode == "iterative" else 0.35)))
    denoise = max(0.0, min(1.0, denoise))
    use_quality_prompt = prompt_strategy == "quality" or (prompt_strategy == "auto" and mode == "tiled" and denoise > 0.25)
    profile = str(o.get("upscale_model") or "")
    if not profile:
        raise ValueError("缺少放大模型")
    scale = float(o.get("scale") or (4.0 if mode == "fast" else 2.0))
    scale = max(1.0, min(4.0, scale))
    src_w = int(params.get("width") or 1024)
    src_h = int(params.get("height") or 1024)
    target_w = max(16, int(round(src_w * scale)))
    target_h = max(16, int(round(src_h * scale)))
    workflow: dict[str, Any] = {}

    if mode == "fast":
        workflow["1"] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
        workflow["2"] = {"class_type": "UpscaleModelLoader", "inputs": {"model_name": profile}}
        workflow["3"] = {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}}
        last = ["3", 0]
        if abs(scale - 4.0) > 0.01:
            workflow["4"] = {"class_type": "ImageScale", "inputs": {"image": ["3", 0], "upscale_method": "lanczos", "width": target_w, "height": target_h, "crop": "disabled"}}
            last = ["4", 0]
        workflow["9"] = {"class_type": "SaveImage", "inputs": {"filename_prefix": "upscale", "images": last}}
        return workflow, "fast"

    # hires / tiled 都需要原模型 + 文本条件 + VAE
    model_name = params.get("model_name") or ""
    model_type = classify_model(model_name, o.get("model_kind", ""), o.get("architecture", ""))
    model_ref, clip_ref, vae_ref, nid, prompt_mode = _add_model_loaders(workflow, 10, model_type, params)
    prompt_text = UPSCALE_QUALITY_PROMPT if use_quality_prompt else params.get("prompt", "")
    neg_text = params.get("negative_prompt", "")
    clip_consumers: list[str] | None = None
    if prompt_mode == "lumina2":
        pos_id = str(nid); nid += 1
        workflow[pos_id] = {"class_type": "CLIPTextEncodeLumina2", "inputs": {"system_prompt": (params.get("system_prompt") or "superior"), "user_prompt": prompt_text, "clip": clip_ref}}
        zero_id = str(nid); nid += 1
        workflow[zero_id] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": [pos_id, 0]}}
        positive_ref, negative_ref = [pos_id, 0], [zero_id, 0]
    elif prompt_mode == "flux":
        pos_id = str(nid); nid += 1
        neg_id = str(nid); nid += 1
        workflow[pos_id] = {"class_type": "CLIPTextEncode", "inputs": {"text": prompt_text, "clip": clip_ref}}
        workflow[neg_id] = {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": clip_ref}}
        guide_id = str(nid); nid += 1
        workflow[guide_id] = {"class_type": "FluxGuidance", "inputs": {"conditioning": [pos_id, 0], "guidance": float(o.get("guidance") or params.get("guidance") or 3.5)}}
        positive_ref, negative_ref = [guide_id, 0], [neg_id, 0]
    else:
        pos_id = str(nid); nid += 1
        neg_id = str(nid); nid += 1
        workflow[pos_id] = {"class_type": "CLIPTextEncode", "inputs": {"text": prompt_text, "clip": clip_ref}}
        workflow[neg_id] = {"class_type": "CLIPTextEncode", "inputs": {"text": neg_text, "clip": clip_ref}}
        positive_ref, negative_ref = [pos_id, 0], [neg_id, 0]
        clip_consumers = [pos_id, neg_id]

    seed = int(o.get("seed", -1) or -1)
    if seed < 0:
        seed = int(params.get("seed", -1) or -1)
    if seed < 0:
        seed = int(time.time() * 1000) % 2**32
    steps = max(1, int(o.get("steps") or 15))
    cfg = float(o.get("cfg") or 0) or float(params.get("cfg") or 7.0)
    sampler_name = o.get("sampler_name") or params.get("sampler_name") or "euler"
    scheduler = o.get("scheduler") or params.get("scheduler") or "normal"

    workflow["1"] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
    workflow["2"] = {"class_type": "UpscaleModelLoader", "inputs": {"model_name": profile}}

    # 可选：Tile ControlNet（denoise >= 0.3 时自动启用，社区推荐强度 0.9，抑制分块幻觉 + 接缝）
    tile_cn = str(o.get("tile_controlnet") or "")
    if tile_cn and bool(o.get("has_controlnet")) and bool(o.get("use_controlnet")) and denoise >= 0.3:
        cn_loader_id = str(nid); nid += 1
        workflow[cn_loader_id] = {"class_type": "ControlNetLoader", "inputs": {"control_net_name": tile_cn}}
        cn_apply_id = str(nid); nid += 1
        workflow[cn_apply_id] = {"class_type": "ControlNetApply", "inputs": {
            "conditioning": positive_ref, "control_net": [cn_loader_id, 0],
            "image": ["1", 0], "strength": float(o.get("controlnet_strength") or 0.9)}}
        positive_ref = [cn_apply_id, 0]

    last_ref: list[Any]
    model_consumers: list[str]

    if mode == "tiled" and bool(o.get("use_multidiffusion")):
        # MultiDiffusion 分块：全局 latent 融合，比 UltimateSDUpscale 更不容易在块里长新主体。
        tile = int(o.get("tile_size") or 768)
        overlap = int(o.get("tile_overlap") or 64)
        batch = max(1, int(o.get("tile_batch_size") or 4))
        workflow["3"] = {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}}
        workflow["4"] = {"class_type": "ImageScale", "inputs": {"image": ["3", 0], "upscale_method": "lanczos", "width": target_w, "height": target_h, "crop": "disabled"}}
        td_id = str(nid); nid += 1
        workflow[td_id] = {"class_type": "TiledDiffusion", "inputs": {
            "model": model_ref, "method": "MultiDiffusion",
            "tile_width": tile, "tile_height": tile, "tile_overlap": overlap, "tile_batch_size": batch}}
        enc_id = str(nid); nid += 1
        workflow[enc_id] = {"class_type": "VAEEncode", "inputs": {"pixels": ["4", 0], "vae": vae_ref}}
        ks_id = str(nid); nid += 1
        workflow[ks_id] = {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": steps, "cfg": cfg, "sampler_name": sampler_name, "scheduler": scheduler,
            "denoise": denoise, "model": [td_id, 0], "positive": positive_ref, "negative": negative_ref, "latent_image": [enc_id, 0]}}
        dec_id = str(nid); nid += 1
        workflow[dec_id] = {"class_type": "VAEDecode", "inputs": {"samples": [ks_id, 0], "vae": vae_ref}}
        last_ref = [dec_id, 0]
        model_consumers = [td_id]
    elif mode == "tiled":
        tile = int(o.get("tile_size") or 512)
        usd_id = str(nid); nid += 1
        workflow[usd_id] = {"class_type": "UltimateSDUpscale", "inputs": {
            "image": ["1", 0], "model": model_ref, "positive": positive_ref, "negative": negative_ref, "vae": vae_ref,
            "upscale_model": ["2", 0], "upscale_by": scale, "seed": seed, "steps": steps, "cfg": cfg,
            "sampler_name": sampler_name, "scheduler": scheduler,
            "denoise": denoise,
            "mode_type": "Linear", "tile_width": tile, "tile_height": tile,
            "mask_blur": int(o.get("mask_blur") if o.get("mask_blur") is not None else 16),
            "tile_padding": int(o.get("tile_padding") if o.get("tile_padding") is not None else 64),
            "seam_fix_mode": (o.get("seam_fix_mode") or "None"),
            "seam_fix_denoise": float(o.get("seam_fix_denoise") if o.get("seam_fix_denoise") is not None else 0.5),
            "seam_fix_width": 64, "seam_fix_mask_blur": 8, "seam_fix_padding": 16,
            "force_uniform_tiles": True, "tiled_decode": False,
        }}
        last_ref = [usd_id, 0]
        model_consumers = [usd_id]
    elif mode == "iterative":
        iterations = max(1, int(o.get("iterations") or 2))
        target_denoise = float(o.get("target_denoise") if o.get("target_denoise") is not None else max(0.25, denoise - 0.15))
        target_denoise = max(0.05, min(1.0, target_denoise))
        hook_id = str(nid); nid += 1
        workflow[hook_id] = {"class_type": "DenoiseScheduleHookProvider", "inputs": {"schedule_for_iteration": "simple", "target_denoise": target_denoise}}
        prov_id = str(nid); nid += 1
        workflow[prov_id] = {"class_type": "PixelKSampleUpscalerProvider", "inputs": {
            "scale_method": "lanczos", "model": model_ref, "vae": vae_ref,
            "seed": seed, "steps": steps, "cfg": cfg,
            "sampler_name": sampler_name, "scheduler": scheduler,
            "positive": positive_ref, "negative": negative_ref,
            "denoise": denoise, "use_tiled_vae": False, "tile_size": int(o.get("tile_size") or 512),
            "upscale_model_opt": ["2", 0], "pk_hook_opt": [hook_id, 0],
        }}
        it_id = str(nid); nid += 1
        workflow[it_id] = {"class_type": "IterativeImageUpscale", "inputs": {
            "pixels": ["1", 0], "upscale_factor": scale, "steps": iterations,
            "temp_prefix": "", "upscaler": [prov_id, 0], "vae": vae_ref,
            "step_mode": (o.get("step_mode") or "simple"),
        }}
        last_ref = [it_id, 0]
        model_consumers = [prov_id]
    else:
        # hires：像素放大 → VAEEncode → KSampler → VAEDecode
        workflow["3"] = {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}}
        workflow["4"] = {"class_type": "ImageScale", "inputs": {"image": ["3", 0], "upscale_method": "lanczos", "width": target_w, "height": target_h, "crop": "disabled"}}
        enc_id = str(nid); nid += 1
        workflow[enc_id] = {"class_type": "VAEEncode", "inputs": {"pixels": ["4", 0], "vae": vae_ref}}
        ks_id = str(nid); nid += 1
        workflow[ks_id] = {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": steps, "cfg": cfg, "sampler_name": sampler_name, "scheduler": scheduler,
            "denoise": denoise, "model": model_ref, "positive": positive_ref,
            "negative": negative_ref, "latent_image": [enc_id, 0],
        }}
        dec_id = str(nid); nid += 1
        workflow[dec_id] = {"class_type": "VAEDecode", "inputs": {"samples": [ks_id, 0], "vae": vae_ref}}
        last_ref = [dec_id, 0]
        model_consumers = [ks_id]

    # 尾部一：很轻的 unsharp（VAE 往返会让线稿发软；纯卷积，不会长出新内容）
    sharpen_alpha = float(o.get("sharpen_alpha") if o.get("sharpen_alpha") is not None else 0.08)
    if sharpen_alpha > 0:
        sh_id = str(nid); nid += 1
        workflow[sh_id] = {"class_type": "ImageSharpen", "inputs": {
            "image": last_ref, "sharpen_radius": int(o.get("sharpen_radius") or 1),
            "sigma": float(o.get("sharpen_sigma") or 1.0), "alpha": sharpen_alpha}}
        last_ref = [sh_id, 0]

    # 尾部二：可选脸部细化（FaceDetailer，需要 Impact-Pack 的 UltralyticsDetectorProvider + face_yolov8m）
    if bool(o.get("face_detail")):
        last_ref, nid, fd_model_id, fd_clip_ids = _append_face_detailer(
            workflow, nid, last_ref, model_ref, clip_ref, vae_ref, positive_ref, negative_ref, params, prompt_mode, o)
        model_consumers = model_consumers + [fd_model_id]
        if clip_consumers is not None:
            clip_consumers = clip_consumers + fd_clip_ids

    save_id = str(nid); nid += 1
    workflow[save_id] = {"class_type": "SaveImage", "inputs": {"filename_prefix": "upscale", "images": last_ref}}
    apply_lora_chain(workflow, params, model_ref=model_ref, clip_ref=clip_ref if clip_consumers else None,
                     start_id=200, mode="full" if clip_consumers else "model_only",
                     model_consumers=model_consumers, clip_consumers=clip_consumers)
    return workflow, model_type


def _append_face_detailer(workflow: dict[str, Any], nid: int, image_ref: list[Any], model_ref: list[Any],
                          clip_ref: list[Any], vae_ref: list[Any], positive_ref: list[Any], negative_ref: list[Any],
                          params: dict[str, Any], prompt_mode: str, o: dict[str, Any]) -> tuple[list[Any], int, str, list[str]]:
    """在放大结果后追加 FaceDetailer 脸部细化，返回 (新 image_ref, 新 nid, FaceDetailer 节点 id, 新增的 clip 消费者 id)。"""
    face_prompt = str(o.get("face_prompt") or "face, detailed face, detailed eyes, sharp focus")
    face_neg = str(o.get("face_negative") or params.get("negative_prompt") or "lowres, blurry, bad anatomy, deformed")
    fp, fn, nid, fc_clip_ids = _add_conditioning(workflow, nid, prompt_mode, params, clip_ref, face_prompt, face_neg)

    det_id = str(nid); nid += 1
    workflow[det_id] = {"class_type": "UltralyticsDetectorProvider",
                        "inputs": {"model_name": (o.get("face_detector") or "bbox/face_yolov8m.pt")}}

    face_seed = int(o.get("face_seed", -1) or -1)
    if face_seed < 0:
        face_seed = int(o.get("seed", -1) or -1)
    if face_seed < 0:
        face_seed = int(time.time() * 1000) % 2**32
    fd_id = str(nid); nid += 1
    workflow[fd_id] = {"class_type": "FaceDetailer", "inputs": {
        "image": image_ref, "model": model_ref, "clip": clip_ref, "vae": vae_ref,
        "guide_size": float(o.get("face_guide_size") or 512), "guide_size_for": True,
        "max_size": float(o.get("face_max_size") or 1536),
        "seed": face_seed, "steps": max(1, int(params.get("steps") or 20)),
        "cfg": float(o.get("cfg") or 0) or float(params.get("cfg") or 7.0),
        "sampler_name": o.get("sampler_name") or params.get("sampler_name") or "euler",
        "scheduler": o.get("scheduler") or params.get("scheduler") or "normal",
        "positive": fp, "negative": fn,
        "denoise": float(o.get("face_denoise") if o.get("face_denoise") is not None else 0.35),
        "feather": int(o.get("face_feather") or 5), "noise_mask": True, "force_inpaint": True,
        "bbox_threshold": 0.5, "bbox_dilation": int(o.get("face_dilation") or 10), "bbox_crop_factor": 3.0,
        "sam_detection_hint": "none", "sam_dilation": 0, "sam_threshold": 0.93, "sam_bbox_expansion": 0,
        "sam_mask_hint_threshold": 0.7, "sam_mask_hint_use_negative": "False", "drop_size": 10,
        "bbox_detector": [det_id, 0], "wildcard": "", "cycle": 1,
    }}
    return [fd_id, 0], nid, fd_id, (fc_clip_ids or [])


def validate_workflow(workflow: dict[str, Any], object_info: dict[str, Any]) -> list[str]:
    """不跑图，只校验工作流在当前 ComfyUI 里能不能跑：节点是否存在、必填参数是否齐全、模型文件是否可用。

    这是「自动适配」的自检环节：适配完不用真的生成一张图，也能立刻知道绑定有没有问题。
    """
    errors: list[str] = []
    for nid, node in (workflow or {}).items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        spec = (object_info or {}).get(class_type)
        if not spec:
            errors.append(f"节点 {nid}（{class_type}）在当前 ComfyUI 中不存在，可能缺少对应自定义节点")
            continue
        node_input = spec.get("input", {}) or {}
        required = node_input.get("required", {}) or {}
        optional = node_input.get("optional", {}) or {}
        given = node.get("inputs", {}) or {}
        for name in required:
            if name not in given:
                errors.append(f"节点 {nid}（{class_type}）缺少必填参数 {name}")
        for name, value in given.items():
            if not isinstance(value, str) or name in {"text", "system_prompt", "user_prompt", "filename_prefix"}:
                continue
            spec_value = required.get(name) or optional.get(name)
            options = None
            if isinstance(spec_value, list) and spec_value:
                first = spec_value[0]
                if isinstance(first, list):
                    options = first
                elif first == "COMBO" and len(spec_value) > 1 and isinstance(spec_value[1], dict):
                    options = spec_value[1].get("options")
            if options and value and value not in options:
                errors.append(f"节点 {nid}（{class_type}）的 {name}「{value}」不在当前 ComfyUI 的可用列表中")
    return errors


def _add_conditioning(workflow: dict[str, Any], nid: int, prompt_mode: str, params: dict[str, Any],
                      clip_ref: list[Any], prompt_text: str, neg_text: str) -> tuple[list[Any], list[Any], int, list[str] | None]:
    """写入文本条件节点，返回 (positive_ref, negative_ref, next_id, clip_consumers)。"""
    clip_consumers: list[str] | None = None
    if prompt_mode == "lumina2":
        pos_id = str(nid); nid += 1
        workflow[pos_id] = {"class_type": "CLIPTextEncodeLumina2", "inputs": {"system_prompt": (params.get("system_prompt") or "superior"), "user_prompt": prompt_text, "clip": clip_ref}}
        zero_id = str(nid); nid += 1
        workflow[zero_id] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": [pos_id, 0]}}
        positive_ref, negative_ref = [pos_id, 0], [zero_id, 0]
    elif prompt_mode == "flux":
        pos_id = str(nid); nid += 1
        neg_id = str(nid); nid += 1
        workflow[pos_id] = {"class_type": "CLIPTextEncode", "inputs": {"text": prompt_text, "clip": clip_ref}}
        workflow[neg_id] = {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": clip_ref}}
        guide_id = str(nid); nid += 1
        workflow[guide_id] = {"class_type": "FluxGuidance", "inputs": {"conditioning": [pos_id, 0], "guidance": float(params.get("guidance") or 3.5)}}
        positive_ref, negative_ref = [guide_id, 0], [neg_id, 0]
    else:
        pos_id = str(nid); nid += 1
        neg_id = str(nid); nid += 1
        workflow[pos_id] = {"class_type": "CLIPTextEncode", "inputs": {"text": prompt_text, "clip": clip_ref}}
        workflow[neg_id] = {"class_type": "CLIPTextEncode", "inputs": {"text": neg_text, "clip": clip_ref}}
        positive_ref, negative_ref = [pos_id, 0], [neg_id, 0]
        clip_consumers = [pos_id, neg_id]
    return positive_ref, negative_ref, nid, clip_consumers


def build_img2img_workflow(params: dict[str, Any], image_name: str, kind: str = "", architecture: str = "") -> tuple[dict[str, Any], str]:
    """以图生图：LoadImage → ImageScale(目标尺寸) → VAEEncode → KSampler(denoise) → VAEDecode → SaveImage。

    denoise 决定「像原图」还是「像提示词」：0.4 轻微改动、0.6 常用、0.8 大改。
    与文生图共用同一套模型 / CLIP / VAE 解析与 LoRA 链，所以所有模型类型都支持。
    """
    workflow: dict[str, Any] = {}
    model_name = params.get("model_name") or ""
    model_type = classify_model(model_name, kind, architecture)
    raw_denoise = params.get("denoise")
    denoise = float(0.6 if raw_denoise is None else raw_denoise)
    denoise = max(0.0, min(1.0, denoise))
    model_ref, clip_ref, vae_ref, nid, prompt_mode = _add_model_loaders(workflow, 10, model_type, params)
    positive_ref, negative_ref, nid, clip_consumers = _add_conditioning(
        workflow, nid, prompt_mode, params, clip_ref,
        params.get("prompt", ""), params.get("negative_prompt", ""))

    workflow["1"] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
    target_w = max(16, int(params.get("width") or 1024))
    target_h = max(16, int(params.get("height") or 1024))
    scale_id = str(nid); nid += 1
    workflow[scale_id] = {"class_type": "ImageScale", "inputs": {"image": ["1", 0], "upscale_method": "lanczos", "width": target_w, "height": target_h, "crop": "disabled"}}
    enc_id = str(nid); nid += 1
    workflow[enc_id] = {"class_type": "VAEEncode", "inputs": {"pixels": [scale_id, 0], "vae": vae_ref}}

    seed = int(params["seed"]) if params.get("seed") is not None else -1
    if seed < 0:
        seed = int(time.time() * 1000) % 2**32
    ks_id = str(nid); nid += 1
    workflow[ks_id] = {"class_type": "KSampler", "inputs": {
        "seed": seed, "steps": max(1, int(params.get("steps") or 20)),
        "cfg": float(params.get("cfg") if params.get("cfg") is not None else 7.0),
        "sampler_name": params.get("sampler_name") or "euler",
        "scheduler": params.get("scheduler") or "normal", "denoise": denoise,
        "model": model_ref, "positive": positive_ref, "negative": negative_ref,
        "latent_image": [enc_id, 0],
    }}
    dec_id = str(nid); nid += 1
    workflow[dec_id] = {"class_type": "VAEDecode", "inputs": {"samples": [ks_id, 0], "vae": vae_ref}}
    save_id = str(nid); nid += 1
    workflow[save_id] = {"class_type": "SaveImage", "inputs": {"filename_prefix": "image_gen", "images": [dec_id, 0]}}
    apply_lora_chain(workflow, params, model_ref=model_ref, clip_ref=clip_ref if clip_consumers else None,
                     start_id=200, mode="full" if clip_consumers else "model_only",
                     model_consumers=[ks_id], clip_consumers=clip_consumers)
    return workflow, model_type


def apply_lora_chain(workflow: dict[str, Any], params: dict[str, Any], model_ref: list[Any], clip_ref: list[Any] | None, start_id: int = 10, mode: str = "full", model_consumers: list[str] | None = None, clip_consumers: list[str] | None = None) -> list[Any]:
    """把 LoRA 链插入工作流，并回填模型 / CLIP 消费者引用。

    - model_consumers：需要接 LoRA 链输出的节点 id（默认所有 KSampler）。
    - clip_consumers：需要接 LoRA 链 clip 输出的节点 id（默认所有 CLIPTextEncode；仅 full 模式）。
    """
    loras = params.get("loras") or []
    if not loras and params.get("lora_name"):
        loras = [{"name": params.get("lora_name"), "strength_model": params.get("lora_strength", 0.6), "strength_clip": params.get("lora_strength", 0.6), "enabled": True}]
    last_model = model_ref
    last_clip = clip_ref
    next_id = start_id
    for item in loras:
        if not item or item.get("enabled") is False:
            continue
        name = item.get("name") or item.get("lora_name")
        if not name:
            continue
        nid = str(next_id)
        next_id += 1
        if mode == "model_only" or last_clip is None:
            workflow[nid] = {"class_type": "LoraLoaderModelOnly", "inputs": {"lora_name": name, "strength_model": float(item.get("strength_model", item.get("strength", 0.6))), "model": last_model}}
            last_model = [nid, 0]
        else:
            workflow[nid] = {"class_type": "LoraLoader", "inputs": {"lora_name": name, "strength_model": float(item.get("strength_model", item.get("strength", 0.6))), "strength_clip": float(item.get("strength_clip", item.get("strength", 0.6))), "model": last_model, "clip": last_clip}}
            last_model = [nid, 0]
            last_clip = [nid, 1]
    # 回填模型消费者（默认所有 KSampler）
    if model_consumers is None:
        model_consumers = [nid for nid, node in workflow.items() if node.get("class_type") == "KSampler"]
    for nid in model_consumers:
        node = workflow.get(nid)
        if node and "model" in (node.get("inputs") or {}):
            node["inputs"]["model"] = last_model
    # 回填 CLIP 消费者（仅 full 模式且存在 clip 引用）
    if last_clip is not None:
        if clip_consumers is None:
            clip_consumers = [nid for nid, node in workflow.items() if node.get("class_type") == "CLIPTextEncode"]
        for nid in clip_consumers:
            node = workflow.get(nid)
            if node and "clip" in (node.get("inputs") or {}):
                node["inputs"]["clip"] = last_clip
    return last_model


def _pick_asset(out: dict[str, Any], out_key: str, requested: str, candidates: list[str], warnings: list[str], errors: list[str], label: str, prefer_tokens: tuple[str, ...] = ()) -> str:
    """单选自动替换 / 多选报错 / 无候选报错 的通用资产校准。"""
    requested = str(requested or "")
    if requested in candidates:
        return requested
    preferred = [x for x in candidates if any(t in x.lower() for t in prefer_tokens)]
    choices = preferred or candidates
    if len(choices) == 1:
        out[out_key] = choices[0]
        if requested:
            warnings.append(f"已将不存在的 {label} {requested} 替换为本机资源 {choices[0]}")
        return choices[0]
    if not choices:
        errors.append(f"本机未检测到可用 {label}，无法构建工作流")
    else:
        errors.append(f"{label} {requested or '（空）'} 不存在；检测到多个候选，请重新选择：{', '.join(choices)}")
    return requested


def resolve_runtime_assets(params: dict[str, Any], resources: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """在提交工作流前，把绑定中的资源名校准到当前 ComfyUI 实时资源。

    不把另一台机器的工作流文件名直接提交给 ComfyUI：同类型只有一个候选时
    自动替换；多个候选时返回错误，让前端/用户明确选择；没有候选则拒绝提交。
    返回 (校准后的参数, warnings, errors)。
    """
    out = dict(params or {})
    warnings: list[str] = []
    errors: list[str] = []
    by_kind = (resources or {}).get("resources", {}) or {}
    def names(*kinds: str) -> list[str]:
        result=[]
        for kind in kinds:
            for value in by_kind.get(kind, []) or []:
                value=str(value)
                if value not in result: result.append(value)
        return result
    model_name=str(out.get("model_name") or "")
    model_kind=""
    for kind in ("unet", "diffusion_models", "checkpoints"):
        if model_name in names(kind): model_kind=kind; break
    model_type=classify_model(model_name, model_kind)
    # 旧绑定的 Anima 模型名可能来自别的整合包；按当前资源重新找同类模型。
    if model_type in {ANIMA_TYPE, "anima_unknown"} or "anima" in model_name.lower():
        model_candidates=[x for x in names("unet", "diffusion_models") if classify_model(x, "diffusion_models") == ANIMA_TYPE or "anima" in x.lower()]
        if model_name not in model_candidates:
            if len(model_candidates)==1:
                warnings.append(f"已将不存在的 Anima 模型 {model_name or '（空）'} 替换为本机资源 {model_candidates[0]}")
                out["model_name"]=model_candidates[0]
            elif not model_candidates:
                errors.append(f"本机未检测到可用 Anima/UNet 模型；工作流需要 {model_name or 'Anima 模型'}")
            else:
                errors.append(f"Anima 模型 {model_name or '（空）'} 不存在；检测到多个候选，请重新选择：{', '.join(model_candidates)}")
        # Anima/Qwen 用 CLIPLoader（safetensors），排除 GGUF（那是 Z-Image/Lumina2 的编码器）。
        clip_candidates=[x for x in names("text_encoders", "clip") if not x.lower().endswith(".gguf")]
        _pick_asset(out, "clip_name", out.get("clip_name"), clip_candidates, warnings, errors, "CLIP/text encoder", ("qwen", "anima", "t5"))
        vae_candidates=names("vae")
        _pick_asset(out, "vae_name", out.get("vae_name"), vae_candidates, warnings, errors, "VAE", ("qwen", "anima"))
    elif model_type == Z_IMAGE_TYPE or "z-image" in model_name.lower() or "z_image" in model_name.lower() or "zimage" in model_name.lower():
        # Z-Image-Turbo（Lumina2）：DiT 校准 diffusion_models/unet，CLIP 优先 GGUF 的 Qwen 4b，VAE 优先 ae。
        model_candidates = [x for x in names("diffusion_models", "unet") if "z-image" in x.lower() or "z_image" in x.lower() or "zimage" in x.lower()]
        if model_name not in model_candidates:
            if len(model_candidates) == 1:
                warnings.append(f"已将不存在的 Z-Image-Turbo 模型 {model_name or '（空）'} 替换为本机资源 {model_candidates[0]}")
                out["model_name"] = model_candidates[0]
            elif not model_candidates:
                errors.append(f"本机未检测到可用 Z-Image-Turbo DiT 模型；工作流需要 {model_name or 'Z-Image-Turbo 模型'}")
            else:
                errors.append(f"Z-Image-Turbo 模型 {model_name or '（空）'} 不存在；检测到多个候选，请重新选择：{', '.join(model_candidates)}")
        clip_candidates = names("text_encoders", "clip")
        requested_clip = str(out.get("clip_name") or "")
        if requested_clip not in clip_candidates:
            # Lumina2 文本编码器优先 GGUF 的 Qwen 3 4b，其次任意 qwen。
            preferred = [x for x in clip_candidates if x.lower().endswith(".gguf") and "qwen" in x.lower() and ("4b" in x.lower() or "3_4b" in x.lower())]
            fallback = [x for x in clip_candidates if "qwen" in x.lower()]
            candidates = preferred or fallback or clip_candidates
            if len(candidates) == 1:
                out["clip_name"] = candidates[0]
                warnings.append(f"已将不存在的 CLIP {requested_clip or '（空）'} 替换为本机资源 {candidates[0]}")
            elif not candidates:
                errors.append("本机未检测到可用 CLIP/text encoder，无法构建 Z-Image-Turbo 工作流")
            else:
                errors.append(f"CLIP {requested_clip or '（空）'} 不存在；检测到多个候选，请重新选择：{', '.join(candidates)}")
        vae_candidates = names("vae")
        requested_vae = str(out.get("vae_name") or "")
        if requested_vae not in vae_candidates:
            preferred = [x for x in vae_candidates if x.lower() == "ae.safetensors" or ("ae" in x.lower() and "zimage" not in x.lower() and "sdxl" not in x.lower() and "qwen" not in x.lower())]
            candidates = preferred or vae_candidates
            if len(candidates) == 1:
                out["vae_name"] = candidates[0]
                warnings.append(f"已将不存在的 VAE {requested_vae or '（空）'} 替换为本机资源 {candidates[0]}")
            elif not candidates:
                errors.append("本机未检测到可用 VAE，无法构建 Z-Image-Turbo 工作流")
            else:
                errors.append(f"VAE {requested_vae or '（空）'} 不存在；检测到多个候选，请重新选择：{', '.join(candidates)}")
    elif model_type in {FLUX_TYPE, NUNCHAKU_TYPE, GGUF_TYPE, DIFFUSION_MODEL_TYPE}:
        # 分体式扩散模型通用校准：模型 → diffusion_models/unet；CLIP → text_encoders/clip；VAE → vae。
        model_candidates = names("diffusion_models", "unet")
        _pick_asset(out, "model_name", model_name, model_candidates, warnings, errors, "扩散模型")
        te_candidates = names("text_encoders", "clip")
        vae_candidates = names("vae")
        if model_type in {FLUX_TYPE, NUNCHAKU_TYPE}:
            # 双文本编码器：t5xxl + clip_l，尽量互斥选择。
            t5_pick = _pick_asset(out, "t5xxl_name", out.get("t5xxl_name") or out.get("clip_name1"), te_candidates, warnings, errors, "T5 文本编码器", ("t5", "xxl"))
            clip_l_candidates = [x for x in te_candidates if x != t5_pick] or te_candidates
            _pick_asset(out, "clip_l_name", out.get("clip_l_name") or out.get("clip_name2"), clip_l_candidates, warnings, errors, "CLIP-L 文本编码器", ("clip_l", "vit"))
        else:
            _pick_asset(out, "clip_name", out.get("clip_name"), te_candidates, warnings, errors, "CLIP/text encoder")
        _pick_asset(out, "vae_name", out.get("vae_name"), vae_candidates, warnings, errors, "VAE")
    # 采样器/调度器也必须来自当前节点，避免跨版本枚举失效。
    # 先把 A1111/CivitAI 展示名（"DPM++ 2M Karras" 等）归一化成 ComfyUI 枚举。
    if out.get("sampler_name") or out.get("scheduler"):
        _ns, _nsch = normalize_sampler(out.get("sampler_name", ""), out.get("scheduler", "normal"))
        if _ns and _ns != out.get("sampler_name"):
            out["sampler_name"] = _ns
        if _nsch:
            out["scheduler"] = _nsch
    samplers=list((resources or {}).get("samplers", []) or [])
    schedulers=list((resources or {}).get("schedulers", []) or [])
    if samplers and str(out.get("sampler_name") or "") not in samplers:
        old=str(out.get("sampler_name") or "")
        out["sampler_name"]="euler" if "euler" in samplers else samplers[0]
        warnings.append(f"已将不可用采样器 {old} 替换为 {out['sampler_name']}")
    if schedulers and str(out.get("scheduler") or "") not in schedulers:
        old=str(out.get("scheduler") or "")
        out["scheduler"]="normal" if "normal" in schedulers else schedulers[0]
        warnings.append(f"已将不可用调度器 {old} 替换为 {out['scheduler']}")
    out["_asset_warnings"]=warnings
    return out, warnings, errors


def build_workflow_for_model(params: dict[str, Any], kind: str = "", architecture: str = "") -> tuple[dict[str, Any], str]:
    """按模型类型选择正确的构建器。每种已知类型都有独立分支，未知类型明确报错。"""
    model_name = params.get("model_name", "")
    model_type = classify_model(model_name, kind, architecture)
    if model_type in CHECKPOINT_TYPES:
        return build_checkpoint_workflow(params), model_type
    if model_type in (ANIMA_TYPE, "anima_unknown"):
        return build_anima_qwen_workflow(params), model_type
    if model_type == Z_IMAGE_TYPE:
        return build_z_image_turbo_workflow(params), model_type
    if model_type == FLUX_TYPE:
        return build_flux_workflow(params), model_type
    if model_type == NUNCHAKU_TYPE:
        return build_nunchaku_workflow(params), model_type
    if model_type == GGUF_TYPE:
        return build_gguf_workflow(params), model_type
    if model_type == DIFFUSION_MODEL_TYPE:
        return build_diffusion_model_workflow(params), model_type
    raise ValueError(
        f"无法为模型「{model_name}」自动构建工作流：模型类型 {model_type!r} 尚未支持。"
        "请确认 ComfyUI 中已安装对应加载节点，或为该模型手动绑定工作流预设。"
    )
