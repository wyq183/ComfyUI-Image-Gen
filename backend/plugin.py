# -*- coding: utf-8 -*-
"""生图助手 — 后端路由 / ComfyUI 桥接 / Agent 工具注册"""
from __future__ import annotations
import json, os, sys, time, uuid
from pathlib import Path
from typing import Any, Optional
import requests

PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from qwenpaw.plugins.api import PluginApi
from image_store import (
    list_images, get_image, add_image, update_rating, update_notes, delete_image,
    list_presets, get_preset, add_preset, save_workflow_preset,
    save_recipe, list_recipes,
    get_config, set_config,
    list_bindings, get_binding, upsert_binding, delete_binding, DEFAULT_PARAM_SCHEMA,
    IMAGES_DIR, DB_DIR
)

PLUGIN_VERSION = "0.4.1"
PLUGIN_ID = "qwenpaw-image-gen"
ARTIFACT_LIBRARY_API_URL = os.environ.get("QWENPAW_ARTIFACT_LIBRARY_API_URL", "http://127.0.0.1:14999")

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
    "X-QwenPaw-Plugin-Version": PLUGIN_VERSION,
}

class NoCacheRoute(APIRoute):
    def get_route_handler(self):
        original_route_handler = super().get_route_handler()
        async def custom_route_handler(request: Request) -> Response:
            response: Response = await original_route_handler(request)
            for key, value in NO_CACHE_HEADERS.items():
                response.headers[key] = value
            return response
        return custom_route_handler

router = APIRouter(route_class=NoCacheRoute)

# ── 模型 ─────────────────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    prompt: str = ""
    negative_prompt: str = ""
    model_name: str = "waiIllustriousSDXL_v170.safetensors"
    workflow_id: int = 0
    steps: int = 20
    cfg: float = 7.0
    seed: int = -1
    width: int = 1024
    height: int = 1024
    lora_name: str = ""
    lora_strength: float = 0.6
    loras: list[dict[str, Any]] = Field(default_factory=list)
    sampler_name: str = "euler"
    scheduler: str = "normal"
    denoise: float = 1.0
    batch_size: int = 1

class RatingPatch(BaseModel):
    rating: int = Field(ge=0, le=5)

class NotesPatch(BaseModel):
    notes: str = ""

class ConfigPatch(BaseModel):
    value: str = ""

class WorkflowBindRequest(BaseModel):
    model_name: str
    workflow_id: str = "sdxl_basic"
    workflow_name: str = "SDXL 基础文生图"
    workflow_type: str = "sdxl_basic"
    params_schema: dict[str, Any] = Field(default_factory=dict)
    supports_lora: bool = True
    supports_negative_prompt: bool = True

class WorkflowPresetSaveRequest(BaseModel):
    name: str
    description: str = ""
    model_type: str = "custom"
    workflow_json: dict[str, Any] = Field(default_factory=dict)
    params_schema: dict[str, Any] = Field(default_factory=dict)
    sort_order: int = 1000

# ── ComfyUI 桥接 ────────────────────────────────────────────────────────────

def _comfyui_url() -> str:
    primary = get_config("comfyui_api_url", "http://127.0.0.1:8188")
    alt = get_config("comfyui_api_url_alt", "http://127.0.0.1:8189")
    # Try primary first
    try:
        r = requests.get(f"{primary}/object_info", timeout=3)
        if r.status_code < 500:
            return primary
    except Exception:
        pass
    # Fallback to alt
    try:
        r = requests.get(f"{alt}/object_info", timeout=3)
        if r.status_code < 500:
            return alt
    except Exception:
        pass
    return primary  # Return primary anyway, caller will handle errors

def _build_workflow(params: dict) -> dict:
    """Build a ComfyUI API workflow from generation parameters.
    
    For v1 we use the simple checkpoint-based SDXL workflow (waiIllustriousSDXL).
    v2 will add Anima (Flux) and other model-specific builders.
    """
    prompt_text = params.get("prompt", "")
    neg_text = params.get("negative_prompt", "")
    model_name = params.get("model_name", "waiIllustriousSDXL_v170.safetensors")
    steps = params.get("steps", 20)
    cfg = params.get("cfg", 7.0)
    seed = params.get("seed", -1)
    width = params.get("width", 1024)
    height = params.get("height", 1024)
    batch_size = params.get("batch_size", 1)
    sampler_name = params.get("sampler_name", "euler")
    scheduler = params.get("scheduler", "normal")
    denoise = params.get("denoise", 1.0)

    if seed < 0:
        seed = int(time.time() * 1000) % 2**32

    # SDXL workflow with CheckpointLoaderSimple
    workflow = {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": sampler_name,
                "scheduler": scheduler,
                "denoise": denoise,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0]
            }
        },
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {
                "ckpt_name": model_name
            }
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": width,
                "height": height,
                "batch_size": batch_size
            }
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": prompt_text,
                "clip": ["4", 1]
            }
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": neg_text,
                "clip": ["4", 1]
            }
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["3", 0],
                "vae": ["4", 2]
            }
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": "image_gen",
                "images": ["8", 0]
            }
        }
    }

    # Add LoRA chain if specified. Prefer full LoraLoader so CLIP strength is adjustable too.
    loras = params.get("loras") or []
    if not loras and params.get("lora_name"):
        loras = [{"name": params.get("lora_name"), "strength_model": params.get("lora_strength", 0.6), "strength_clip": params.get("lora_strength", 0.6), "enabled": True}]
    last_model = ["4", 0]
    last_clip = ["4", 1]
    next_id = 10
    for item in loras:
        if not item or item.get("enabled") is False:
            continue
        name = item.get("name") or item.get("lora_name")
        if not name:
            continue
        nid = str(next_id)
        next_id += 1
        workflow[nid] = {
            "class_type": "LoraLoader",
            "inputs": {
                "lora_name": name,
                "strength_model": float(item.get("strength_model", item.get("strength", 0.6))),
                "strength_clip": float(item.get("strength_clip", item.get("strength", 0.6))),
                "model": last_model,
                "clip": last_clip
            }
        }
        last_model = [nid, 0]
        last_clip = [nid, 1]
    workflow["3"]["inputs"]["model"] = last_model
    workflow["6"]["inputs"]["clip"] = last_clip
    workflow["7"]["inputs"]["clip"] = last_clip

    return workflow

def _run_comfyui(workflow: dict) -> dict:
    """Submit workflow to ComfyUI and wait for completion."""
    api_url = _comfyui_url()
    
    # Submit
    try:
        r = requests.post(f"{api_url}/prompt", json={"prompt": workflow}, timeout=30)
        r.raise_for_status()
        result = r.json()
    except requests.exceptions.ConnectionError:
        raise HTTPException(status_code=503, detail=f"无法连接到 ComfyUI ({api_url})，请确认 ComfyUI 已启动")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"提交生图任务失败: {str(e)}")

    prompt_id = result.get("prompt_id", "")
    if not prompt_id:
        raise HTTPException(status_code=500, detail="ComfyUI 未返回 prompt_id")

    # Poll for completion
    max_wait = 300  # 5 minutes max
    poll_interval = 2
    waited = 0
    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        try:
            r = requests.get(f"{api_url}/history/{prompt_id}", timeout=10)
            r.raise_for_status()
            history = r.json()
        except Exception:
            continue

        if prompt_id in history:
            node_outputs = history[prompt_id].get("outputs", {})
            # Find SaveImage node output
            images = []
            for node_id, node_out in node_outputs.items():
                if "images" in node_out:
                    for img in node_out["images"]:
                        images.append({
                            "filename": img.get("filename", ""),
                            "subfolder": img.get("subfolder", ""),
                            "type": img.get("type", "output")
                        })
            if images:
                # Download the first image
                img = images[0]
                try:
                    img_r = requests.get(
                        f"{api_url}/view",
                        params={"filename": img["filename"], "subfolder": img["subfolder"], "type": img["type"]},
                        timeout=30
                    )
                    img_r.raise_for_status()
                    
                    # Save to our managed directory
                    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
                    local_filename = f"{uuid.uuid4().hex}_{img['filename']}"
                    local_path = IMAGES_DIR / local_filename
                    with open(local_path, "wb") as f:
                        f.write(img_r.content)
                    
                    # Get image dimensions using PIL if available
                    w, h = width_from_bytes(img_r.content)
                    
                    return {
                        "success": True,
                        "image_path": str(local_path),
                        "file_name": local_filename,
                        "file_size": len(img_r.content),
                        "width": w,
                        "height": h,
                        "seed": workflow.get("3", {}).get("inputs", {}).get("seed", 0),
                        "prompt_id": prompt_id,
                        "raw_images": images
                    }
                except Exception as e:
                    return {"success": False, "error": f"下载图片失败: {str(e)}", "images_meta": images}
            
            # Check for errors
            if history[prompt_id].get("status", {}).get("completed") is False:
                error_info = history[prompt_id].get("status", {}).get("error_message", "未知错误")
                return {"success": False, "error": f"生图失败: {error_info}"}

    return {"success": False, "error": f"生图超时（{max_wait}秒），请在 ComfyUI 中查看进度"}


def _register_generated_image_to_library(result: dict, params: dict) -> None:
    """Best-effort sync generated image into Artifact Library."""
    image_path = result.get("image_path")
    if not image_path or not Path(image_path).is_file():
        return
    payload = {
        "path": image_path,
        "title": f"生图 {Path(image_path).stem}",
        "summary": (params.get("prompt") or "生图助手生成图片")[:1000],
        "project": params.get("project") or "生图图库",
        "deliverable": "生图图库",
        "artifact_type": "image",
        "tags": ["生图", "ComfyUI", str(params.get("model_name") or "")],
        "status": "delivered",
        "notes": "",
        "asset_category": "generated_image",
        "source_plugin": "qwenpaw-image-gen",
        "source_id": str(result.get("prompt_id") or ""),
        "generation_meta": {
            "prompt": params.get("prompt") or "",
            "negative_prompt": params.get("negative_prompt") or "",
            "model_name": params.get("model_name") or "",
            "lora_name": params.get("lora_name") or "",
            "loras": params.get("loras") or [],
            "steps": params.get("steps"),
            "cfg": params.get("cfg"),
            "seed": result.get("seed"),
            "width": result.get("width"),
            "height": result.get("height"),
            "prompt_id": result.get("prompt_id") or "",
            "backend": "ComfyUI",
        },
    }
    try:
        requests.post(
            f"{ARTIFACT_LIBRARY_API_URL}/artifact-library/artifacts",
            json=payload,
            timeout=8,
        )
    except Exception:
        pass

def width_from_bytes(data: bytes) -> tuple:
    """Try to get image dimensions from raw bytes without PIL."""
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(data))
        return img.width, img.height
    except Exception:
        return 0, 0

# ── API 路由 ────────────────────────────────────────────────────────────────

def _list_comfy_models(api_url: str, kind: str) -> list[str]:
    try:
        r = requests.get(f"{api_url}/api/models/{kind}", timeout=5)
        if r.status_code < 500:
            data = r.json()
            if isinstance(data, list):
                return [str(x) for x in data]
    except Exception:
        pass
    return []


@router.get("/version")
def api_version():
    return {
        "id": PLUGIN_ID,
        "version": PLUGIN_VERSION,
        "frontend_entry": "ui/index.0.4.1.js",
        "cache_busting": True,
        "api_no_cache": True,
        "features": [
            "desktop-cache-busting",
            "frontend-backend-version-check",
            "no-cache-api-headers",
            "artifact-library-integration",
        ],
    }

@router.get("/status")
def api_status():
    """检查 ComfyUI 连接状态"""
    api_url = _comfyui_url()
    try:
        r = requests.get(f"{api_url}/object_info", timeout=5)
        info = r.json() if r.status_code < 500 else {}
        models = _list_comfy_models(api_url, "checkpoints")
        loras = _list_comfy_models(api_url, "loras")
        return {
            "connected": True,
            "api_url": api_url,
            "models": models[:50] if models else [],
            "loras": loras[:100] if loras else [],
            "model_count": len(models) if models else 0,
            "lora_count": len(loras) if loras else 0,
            "object_info_loaded": bool(info)
        }
    except Exception as e:
        return {"connected": False, "api_url": api_url, "error": str(e), "models": [], "loras": []}

@router.get("/workflow-state")
def api_workflow_state(model_name: str = "", workflow_preset_id: int = 0):
    """聚合右栏所需状态：模型、LoRA、绑定工作流、动态参数 schema。"""
    status = api_status()
    bindings = list_bindings()
    presets = list_presets()
    by_model = {b.get("model_name", ""): b for b in bindings}
    selected = model_name or (status.get("models") or [""])[0]
    binding = by_model.get(selected) if selected else None
    preset = get_preset(workflow_preset_id) if workflow_preset_id else None
    schema = {}
    if preset:
        try:
            schema = json.loads(preset.get("params_schema") or "{}")
        except Exception:
            schema = DEFAULT_PARAM_SCHEMA
        binding = {
            "workflow_id": "preset:" + str(preset.get("id")),
            "workflow_name": preset.get("name") or "默认工作流",
            "workflow_type": preset.get("model_type") or "preset",
            "params_schema": preset.get("params_schema") or "{}",
            "supports_lora": 1,
            "supports_negative_prompt": 1,
            "preset_id": preset.get("id"),
        }
    elif binding:
        try:
            schema = json.loads(binding.get("params_schema") or "{}")
        except Exception:
            schema = DEFAULT_PARAM_SCHEMA
    elif presets:
        preset = presets[0]
        try:
            schema = json.loads(preset.get("params_schema") or "{}")
        except Exception:
            schema = DEFAULT_PARAM_SCHEMA
        binding = {
            "workflow_id": "preset:" + str(preset.get("id")),
            "workflow_name": preset.get("name") or "默认工作流",
            "workflow_type": preset.get("model_type") or "preset",
            "params_schema": preset.get("params_schema") or "{}",
            "supports_lora": 1,
            "supports_negative_prompt": 1,
            "preset_id": preset.get("id"),
        }
    return {
        "status": status,
        "models": [{"name": m, "has_workflow": (m in by_model) or bool(presets), "binding": by_model.get(m)} for m in status.get("models", [])],
        "loras": status.get("loras", []),
        "selected_model": selected,
        "binding": binding,
        "has_workflow": bool(binding or presets),
        "params_schema": schema,
        "workflow_presets": presets,
        "selected_preset_id": (preset or {}).get("id") if preset else 0,
    }

@router.post("/workflows/bind")
def api_bind_workflow(payload: WorkflowBindRequest):
    schema = payload.params_schema or DEFAULT_PARAM_SCHEMA
    return upsert_binding(
        model_name=payload.model_name,
        workflow_id=payload.workflow_id,
        workflow_name=payload.workflow_name,
        workflow_type=payload.workflow_type,
        params_schema=json.dumps(schema, ensure_ascii=False),
        supports_lora=1 if payload.supports_lora else 0,
        supports_negative_prompt=1 if payload.supports_negative_prompt else 0,
    )

@router.delete("/workflows/bind/{model_name}")
def api_delete_workflow_binding(model_name: str):
    delete_binding(model_name)
    return {"success": True}

@router.post("/generate")
def api_generate(params: GenerateRequest):
    """提交生图任务并等待完成"""
    workflow = _build_workflow(params.model_dump())
    result = _run_comfyui(workflow)
    if result.get("success"):
        # Auto-register to gallery
        img = add_image(
            file_path=result["image_path"],
            file_name=result["file_name"],
            file_size=result.get("file_size", 0),
            width=result.get("width", 0),
            height=result.get("height", 0),
            prompt=params.prompt,
            negative_prompt=params.negative_prompt,
            model_name=params.model_name,
            lora_name=params.lora_name,
            steps=params.steps,
            cfg=params.cfg,
            seed=result.get("seed", -1)
        )
        result["gallery_id"] = img["id"]
        _register_generated_image_to_library(result, params.model_dump())
    return result

# ── 图库路由 ────────────────────────────────────────────────────────────────

@router.get("/images")
def api_list_images(query: str = "", model_name: str = "", min_rating: int = 0):
    return {"items": list_images(query, model_name, min_rating)}

@router.get("/images/{image_id}")
def api_get_image(image_id: int):
    img = get_image(image_id)
    if not img:
        raise HTTPException(404, "图片不存在")
    return img

@router.get("/images/{image_id}/file")
def api_image_file(image_id: int):
    img = get_image(image_id)
    if not img:
        raise HTTPException(404, "图片不存在")
    path = img["file_path"]
    if not os.path.isfile(path):
        raise HTTPException(404, "原文件已不存在")
    return FileResponse(path, media_type="image/png")

@router.patch("/images/{image_id}/rating")
def api_patch_rating(image_id: int, payload: RatingPatch):
    img = get_image(image_id)
    if not img:
        raise HTTPException(404, "图片不存在")
    return update_rating(image_id, payload.rating)

@router.patch("/images/{image_id}/notes")
def api_patch_notes(image_id: int, payload: NotesPatch):
    img = get_image(image_id)
    if not img:
        raise HTTPException(404, "图片不存在")
    return update_notes(image_id, payload.notes)

@router.post("/images/{image_id}/delete")
def api_delete_image(image_id: int):
    img = get_image(image_id)
    if not img:
        raise HTTPException(404, "图片不存在")
    delete_image(image_id)
    return {"success": True}

# ── 工作流预设 ──────────────────────────────────────────────────────────────

@router.get("/presets")
def api_list_presets(model_type: str = ""):
    return {"items": list_presets(model_type)}

@router.get("/presets/{preset_id}")
def api_get_preset(preset_id: int):
    p = get_preset(preset_id)
    if not p:
        raise HTTPException(404, "预设不存在")
    return p

@router.post("/presets")
def api_save_workflow_preset(payload: WorkflowPresetSaveRequest):
    return save_workflow_preset(
        name=payload.name,
        description=payload.description,
        model_type=payload.model_type,
        workflow_json=json.dumps(payload.workflow_json or {}, ensure_ascii=False),
        params_schema=json.dumps(payload.params_schema or DEFAULT_PARAM_SCHEMA, ensure_ascii=False),
        sort_order=payload.sort_order,
    )

@router.post("/workflows/apply-preset/{preset_id}")
def api_apply_workflow_preset(preset_id: int, model_name: str = ""):
    preset = get_preset(preset_id)
    if not preset:
        raise HTTPException(404, "预设不存在")
    if not model_name:
        raise HTTPException(400, "缺少 model_name")
    return upsert_binding(
        model_name=model_name,
        workflow_id="preset:" + str(preset_id),
        workflow_name=preset.get("name") or "默认工作流",
        workflow_type=preset.get("model_type") or "preset",
        workflow_json=preset.get("workflow_json") or "{}",
        params_schema=preset.get("params_schema") or json.dumps(DEFAULT_PARAM_SCHEMA, ensure_ascii=False),
        supports_lora=1,
        supports_negative_prompt=1,
    )

# ── 配置 ─────────────────────────────────────────────────────────────────────

@router.get("/config")
def api_get_configs():
    return {
        "comfyui_api_url": get_config("comfyui_api_url"),
        "comfyui_api_url_alt": get_config("comfyui_api_url_alt"),
    }

@router.patch("/config/{key}")
def api_patch_config(key: str, payload: ConfigPatch):
    set_config(key, payload.value)
    return {"success": True}

# ── 聊天代理 ──────────────────────────────────────────────────────────────────

QWENPAW_API_URL = "http://127.0.0.1:14999"

@router.post("/recipe-draft")
def api_recipe_draft(payload: dict):
    """Receive a recipe draft from Artifact Library."""
    try:
        return {"success": True, "draft": payload}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"接收草稿失败: {exc}")

@router.post("/chat/send")
def api_chat_send(payload: dict):
    """Forward a chat message to the current QwenPaw session."""
    session_id = payload.get("session_id", "")
    text = payload.get("text", "")
    agent_id = payload.get("agent_id", "")
    if not session_id or not text:
        raise HTTPException(400, "缺少 session_id 或 text")
    
    # Try multiple known QwenPaw API patterns
    errors = []
    endpoints = [
        f"{QWENPAW_API_URL}/api/sessions/{session_id}/messages",
        f"{QWENPAW_API_URL}/api/agents/{agent_id}/sessions/{session_id}/messages",
        f"{QWENPAW_API_URL}/api/chat/send",
    ]
    for url in endpoints:
        try:
            body = {"session_id": session_id, "text": text}
            if agent_id:
                body["agent_id"] = agent_id
            r = requests.post(url, json=body, timeout=10,
                headers={"Content-Type": "application/json"})
            if r.status_code < 500:
                return {"success": True, "status": r.status_code}
        except Exception as e:
            errors.append(str(e))
            continue
    return {"success": False, "error": "无法发送消息", "details": errors}

@router.get("/chat/messages")
def api_chat_messages(session_id: str = "", agent_id: str = "", limit: int = 50):
    """Fetch messages from the current QwenPaw session."""
    if not session_id:
        return {"items": []}
    
    errors = []
    endpoints = [
        f"{QWENPAW_API_URL}/api/sessions/{session_id}/messages?limit={limit}",
        f"{QWENPAW_API_URL}/api/agents/{agent_id}/sessions/{session_id}/messages?limit={limit}",
        f"{QWENPAW_API_URL}/api/chat/messages?session_id={session_id}&limit={limit}",
    ]
    for url in endpoints:
        try:
            r = requests.get(url, timeout=10)
            if r.status_code < 500:
                data = r.json()
                items = data.get("messages") or data.get("items") or (data if isinstance(data, list) else [])
                return {"items": items if isinstance(items, list) else []}
        except Exception as e:
            errors.append(str(e))
            continue
    return {"items": [], "error": str(errors) if errors else "未找到消息接口"}

# ── 配方 ─────────────────────────────────────────────────────────────────────

@router.post("/recipes")
def api_save_recipe(payload: dict):
    return save_recipe(
        name=payload.get("name", ""),
        prompt=payload.get("prompt", ""),
        negative_prompt=payload.get("negative_prompt", ""),
        model_name=payload.get("model_name", ""),
        lora_name=payload.get("lora_name", ""),
        workflow_id=payload.get("workflow_id", 0),
        steps=payload.get("steps", 20),
        cfg=payload.get("cfg", 7.0),
        seed=payload.get("seed", -1),
        width=payload.get("width", 1024),
        height=payload.get("height", 1024)
    )

@router.get("/recipes")
def api_list_recipes():
    return {"items": list_recipes()}

# ── Agent 工具 ──────────────────────────────────────────────────────────────

def image_gen_generate(prompt: str = "", negative_prompt: str = "", model_name: str = "waiIllustriousSDXL_v170.safetensors", steps: int = 20, cfg: float = 7.0, width: int = 1024, height: int = 1024, lora_name: str = "", lora_strength: float = 0.6) -> dict:
    """使用 ComfyUI 生成图片。返回生成结果和图片在插件图库中的 ID。
    
    参数说明：
    - prompt: 正向提示词（描述你想生成的画面）
    - negative_prompt: 负向提示词（描述你不想要的元素）
    - model_name: 模型名称（如 waiIllustriousSDXL_v170.safetensors）
    - steps: 采样步数（默认20，越高越精细但越慢）
    - cfg: 提示词相关性（默认7.0，越高越贴合提示词）
    - width/height: 图片尺寸
    - lora_name: LoRA 模型名称（可选）
    - lora_strength: LoRA 强度（默认0.6）
    """
    try:
        req = GenerateRequest(
            prompt=prompt, negative_prompt=negative_prompt,
            model_name=model_name, steps=steps, cfg=cfg,
            width=width, height=height, lora_name=lora_name, lora_strength=lora_strength
        )
        result = api_generate(req)
        if result.get("success"):
            return {
                "success": True,
                "message": f"图片生成成功！已在插件图库中（ID: {result.get('gallery_id')}）",
                "gallery_id": result.get("gallery_id"),
                "image_path": result.get("image_path"),
                "seed": result.get("seed")
            }
        else:
            return {"success": False, "error": result.get("error", "生图失败")}
    except Exception as e:
        return {"success": False, "error": str(e)}

def image_gen_set_rating(image_id: int, rating: int) -> dict:
    """为生成的图片设置星级评分（0-5星）"""
    try:
        return update_rating(image_id, rating)
    except Exception as e:
        return {"success": False, "error": str(e)}

def image_gen_check_status() -> dict:
    """检查 ComfyUI 连接状态和可用模型列表"""
    try:
        return api_status()
    except Exception as e:
        return {"connected": False, "error": str(e)}

# ── 插件注册 ────────────────────────────────────────────────────────────────

class ImageGenPlugin:
    def register(self, api: PluginApi) -> None:
        api.register_http_router(router, prefix="/image-gen", tags=["image-gen"])
        
        # Register agent tools
        api.register_tool(
            tool_name="image_gen_generate",
            tool_func=image_gen_generate,
            description="使用 ComfyUI 生图。填写提示词和参数，自动生成图片并保存到插件图库。",
            icon="🎨",
            enabled=True,
            tool_type="function"
        )
        api.register_tool(
            tool_name="image_gen_set_rating",
            tool_func=image_gen_set_rating,
            description="给插件图库中的图片评分（0-5星）",
            icon="⭐",
            enabled=True,
            tool_type="function"
        )
        api.register_tool(
            tool_name="image_gen_check_status",
            tool_func=image_gen_check_status,
            description="检查 ComfyUI 的运行状态和可用模型",
            icon="🔌",
            enabled=True,
            tool_type="function"
        )
        
        # Auto-load the skill directory
        try:
            skills_dir = PLUGIN_DIR.parent / "skills"
            if skills_dir.is_dir():
                api.register_skill_provider(
                    skill_dirs=[str(skills_dir)],
                    skill_origin=__file__,
                )
        except Exception:
            pass

plugin = ImageGenPlugin()
