# -*- coding: utf-8 -*-
"""ComfyUI 能力发现、模型识别与工作流构建器。

v0.5 架构原则：
- 程序负责确定性扫描 / 分类 / 验证 / 构建；
- AI 只负责解释、提示词和用户引导；
- 不再让 AI 直接猜 workflow_type 后无校验绑定。
"""
from __future__ import annotations

import time
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
UNKNOWN_TYPE = "unknown"


def fetch_object_info(api_url: str, timeout: int = 5) -> dict[str, Any]:
    r = requests.get(f"{api_url}/object_info", timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, dict) else {}


def list_comfy_models(api_url: str, kind: str, timeout: int = 5) -> list[str]:
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
    """读取 ComfyUI 资源索引和真实节点参数。"""
    object_info = {}
    try:
        object_info = fetch_object_info(api_url)
    except Exception:
        object_info = {}
    resources = {kind: list_comfy_models(api_url, kind) for kind in MODEL_KINDS}
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


def classify_model(name: str, kind: str = "") -> str:
    """确定性模型粗分类。保守原则：不确定就 unknown，不强行套模板。"""
    n = (name or "").lower()
    k = (kind or "").lower()
    if "lora" in k or k == "loras":
        return "lora"
    if k in {"vae"}:
        return "vae"
    if k in {"clip", "text_encoders"}:
        return "text_encoder"
    if "gguf" in n:
        return "gguf"
    if "nunchaku" in n or "svdq" in n or "fp4" in n or "int4" in n:
        return "nunchaku"
    # Anima Aesthetic/Qwen UNet：本机已验证，不是 Illustrious。
    if "anima" in n:
        if k in {"unet", "diffusion_models"} or "aesthetic" in n or "base" in n:
            return ANIMA_TYPE
        return "anima_unknown"
    if "flux" in n or "schnell" in n:
        return "flux"
    if k in {"unet", "diffusion_models"}:
        return "diffusion_model"
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


def validate_workflow_type_for_model(model_name: str, workflow_type: str, kind: str = "") -> tuple[bool, str, str]:
    """校验 AI 或用户传入的 workflow_type 是否明显错误。返回 ok, corrected_type, message。"""
    model_type = classify_model(model_name, kind)
    wf = (workflow_type or "").lower()
    if model_type == ANIMA_TYPE and "illustrious" in wf:
        return False, ANIMA_TYPE, "Anima/Qwen UNet 模型不能绑定为 Illustrious/SDXL checkpoint 工作流"
    if model_type in {ANIMA_TYPE, "flux", "nunchaku", "gguf", "diffusion_model"} and wf in CHECKPOINT_TYPES:
        return False, model_type, f"{model_type} 不是普通 checkpoint，不能绑定为 {wf}"
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
    return base


def build_checkpoint_workflow(params: dict[str, Any]) -> dict[str, Any]:
    seed = int(params.get("seed", -1))
    if seed < 0:
        seed = int(time.time() * 1000) % 2**32
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
    seed = int(params.get("seed", -1))
    if seed < 0:
        seed = int(time.time() * 1000) % 2**32
    unet_name = params.get("model_name") or params.get("unet_name") or "anima_aestheticV11.safetensors"
    clip_name = params.get("clip_name") or "qwen_3_06b_base.safetensors"
    vae_name = params.get("vae_name") or "qwen_image_vae.safetensors"
    workflow: dict[str, Any] = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": unet_name, "weight_dtype": params.get("weight_dtype", "default")}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip_name, "type": params.get("clip_type", "qwen_image"), "device": params.get("clip_device", "default")}},
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


def apply_lora_chain(workflow: dict[str, Any], params: dict[str, Any], model_ref: list[Any], clip_ref: list[Any] | None, start_id: int = 10, mode: str = "full") -> None:
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
    # 回填常见采样节点引用。
    if "3" in workflow and workflow["3"].get("class_type") == "KSampler":
        workflow["3"]["inputs"]["model"] = last_model
        if last_clip is not None:
            for enc in ["6", "7"]:
                if enc in workflow and workflow[enc].get("class_type") == "CLIPTextEncode":
                    workflow[enc]["inputs"]["clip"] = last_clip
    if "7" in workflow and workflow["7"].get("class_type") == "KSampler":
        workflow["7"]["inputs"]["model"] = last_model


def build_workflow_for_model(params: dict[str, Any], kind: str = "") -> tuple[dict[str, Any], str]:
    model_type = classify_model(params.get("model_name", ""), kind)
    if model_type == ANIMA_TYPE:
        return build_anima_qwen_workflow(params), model_type
    return build_checkpoint_workflow(params), model_type
