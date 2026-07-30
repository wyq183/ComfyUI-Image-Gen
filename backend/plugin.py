# -*- coding: utf-8 -*-
"""生图助手 — 后端路由 / ComfyUI 桥接 / Agent 工具注册"""
from __future__ import annotations
import json, os, sys, time, uuid, logging, hashlib, threading, subprocess, re
from pathlib import Path
from typing import Any, Optional
import ctypes
import requests

log = logging.getLogger("qwenpaw-image-gen")

PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from fastapi import APIRouter, HTTPException, Query, Request, Response, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from qwenpaw.plugins.api import PluginApi
from image_store import (
    list_images, list_images_page, list_gallery_categories, get_image, add_image, update_rating, update_notes, update_image_location, update_image_metadata, delete_image,
    list_presets, get_preset, add_preset, save_workflow_preset,
    save_recipe, list_recipes,
    get_config, set_config,
    list_bindings, get_binding, upsert_binding, delete_binding, DEFAULT_PARAM_SCHEMA,
    IMAGES_DIR, DB_DIR, _get_db
)
from comfy_adapter import (
    discover_resources, classify_model, validate_workflow_type_for_model,
    build_param_schema, build_workflow_for_model, resolve_runtime_assets,
)

# 从 plugin.json 读取版本（唯一定义源）
try:
    _pj_path = PLUGIN_DIR.parent / "plugin.json"
    if not _pj_path.exists():
        _pj_path = PLUGIN_DIR / "plugin.json"  # fallback
    _plugin_meta = json.loads(_pj_path.read_text(encoding="utf-8"))
    PLUGIN_VERSION = _plugin_meta.get("version", "0.0.0")
except Exception as _e:
    PLUGIN_VERSION = "0.0.0"
    import traceback; traceback.print_exc()
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
    category: str = '未分类'

class RatingPatch(BaseModel):
    rating: int = Field(ge=0, le=5)

class NotesPatch(BaseModel):
    notes: str = ""

class BatchImagesRequest(BaseModel):
    image_ids: list[int] = Field(default_factory=list, max_length=500)

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

def _safe_category(value: str) -> str:
    value = (value or "未分类").strip()
    if not value or value in {".", ".."}:
        return "未分类"
    value = "".join(c for c in value if c not in '<>:/\\|?*"')
    return value[:40] or "未分类"

def _discover_comfyui_output_dir() -> Path | None:
    """推导本机当前已连接 ComfyUI 的 output 目录，不写死任何整合包路径。

    仅查询已选 ComfyUI API 端口的监听进程；从该 Python 进程命令行的 main.py
    位置推导 ``<ComfyUI根目录>/output``。无法可靠识别时返回 None，由 history
    兜底，绝不扫描磁盘上的任意目录。
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse(_comfyui_url())
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or not parsed.port:
            return None
        # $PID 是 PowerShell 的只读自动变量，必须使用自定义变量名。
        ps_script = (
            "$procId=(Get-NetTCPConnection -LocalPort "
            f"{parsed.port} -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 "
            "-ExpandProperty OwningProcess); if($procId){Get-CimInstance Win32_Process "
            "-Filter ('ProcessId='+$procId) | Select-Object -ExpandProperty CommandLine}"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace"
        )
        command_line = result.stdout.strip()
        # ComfyUI official and packaged launchers both pass an absolute main.py path.
        match = re.search(r'"([^\"]+\\main\.py)"|([^\s\"]+\\main\.py)', command_line, re.I)
        main_py = Path((match.group(1) or match.group(2))) if match else None
        if main_py and main_py.is_file():
            candidate = main_py.parent / "output"
            if candidate.is_dir():
                return candidate
    except Exception:
        pass
    return None

def _output_dir() -> Path:
    """获取 ComfyUI 输出目录：用户设置优先，其次自动发现正在运行的 ComfyUI。"""
    # 兼容旧版数据库曾使用的 output_dir 键；新键优先。
    configured = (get_config("comfyui_output_dir", "").strip() or get_config("output_dir", "").strip())
    # 旧版默认 output_dir 可能指向插件缓存目录，不能阻止自动发现真实 output。
    if configured:
        path = Path(configured)
        if path.is_dir() and path.resolve() != IMAGES_DIR.resolve(): return path
    discovered = _discover_comfyui_output_dir()
    if discovered: return discovered
    # 最后回退插件私有目录。这个目录只保存插件自己生成/下载的文件。
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    return IMAGES_DIR

def _read_image_meta(path: Path) -> dict:
    out = {"prompt": "", "negative_prompt": "", "model_name": "", "lora_name": "", "steps": 20, "cfg": 7.0, "seed": -1}
    try:
        from PIL import Image
        with Image.open(path) as im:
            info = dict(im.info or {})
            raw = info.get("prompt") or info.get("workflow")
            if isinstance(raw, str):
                try: raw = json.loads(raw)
                except Exception: raw = None
            if isinstance(raw, dict):
                nodes = raw.get("prompt", raw)
                for node in nodes.values() if isinstance(nodes, dict) else []:
                    if not isinstance(node, dict): continue
                    ct, inp = node.get("class_type", ""), node.get("inputs", {})
                    if ct == "CheckpointLoaderSimple": out["model_name"] = inp.get("ckpt_name", "")
                    if ct.lower().startswith("lora"):
                        name = inp.get("lora_name", "")
                        if name: out["lora_name"] = (out["lora_name"] + "; " if out["lora_name"] else "") + str(name)
                    if ct == "KSampler":
                        for k in ("steps", "cfg", "seed"):
                            if k in inp: out[k] = inp[k]
                        # ComfyUI API workflow uses [node_id, slot_index] links:
                        # KSampler.positive -> 正向，KSampler.negative -> 反向。
                        for slot, key in (("positive", "prompt"), ("negative", "negative_prompt")):
                            link = inp.get(slot)
                            if isinstance(link, list) and link:
                                source = nodes.get(str(link[0]), nodes.get(link[0], {})) if isinstance(nodes, dict) else {}
                                source_text = (source.get("inputs", {}) if isinstance(source, dict) else {}).get("text", "")
                                if source_text:
                                    out[key] = str(source_text)
                    if ct == "CLIPTextEncode":
                        # 兼容没有 KSampler 连线的旧/简化工作流：第一个文本节点作为正向。
                        text = inp.get("text", "")
                        if text and not out["prompt"]: out["prompt"] = str(text)
    except Exception:
        pass
    return out

def _scan_output_gallery() -> int:
    root = _output_dir(); added = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}: continue
        try: st = path.stat()
        except OSError: continue
        conn = _get_db()
        exists = conn.execute("SELECT id FROM gallery_images WHERE file_path=?", (str(path),)).fetchone()
        conn.close()
        meta = _read_image_meta(path)
        # output 根目录图片为未分类；首层子目录就是分类。
        try:
            relative_parts = path.relative_to(root).parts
            category = _safe_category(relative_parts[0]) if len(relative_parts) > 1 else "未分类"
        except ValueError:
            category = "未分类"
        if exists:
            # 重扫时同步纠正旧记录分类，避免历史记录永远停留在未分类。
            conn = _get_db()
            conn.execute("UPDATE gallery_images SET category=? WHERE id=?", (category, exists["id"]))
            conn.commit(); conn.close()
            # 旧图库记录也要在重新扫描时补齐 PNG 元数据，尤其是反向提示词。
            if meta.get("negative_prompt") or meta.get("prompt"):
                update_image_metadata(exists["id"], meta["prompt"], meta["negative_prompt"], meta["model_name"], meta["lora_name"], int(meta["steps"] or 20), float(meta["cfg"] or 7), int(meta["seed"] or -1))
            continue
        w=h=0
        try:
            from PIL import Image
            with Image.open(path) as im: w,h=im.size
        except Exception: pass
        add_image(str(path), path.name, st.st_size, w, h, meta["prompt"], meta["negative_prompt"], meta["model_name"], meta["lora_name"], steps=int(meta["steps"] or 20), cfg=float(meta["cfg"] or 7), seed=int(meta["seed"] or -1), category=category)
        added += 1
    return added

def _scan_comfy_history_gallery() -> int:
    """从 ComfyUI /history 导入已完成输出。

    这是跨整合包的兜底：ComfyUI API 会返回每张输出的 filename/subfolder，
    插件通过 /view 下载并保存为自己的可管理副本，因此不需要猜测用户的
    ComfyUI 安装路径或 output 文件夹位置。
    """
    api_url = _comfyui_url(); added = 0
    try:
        history = requests.get(f"{api_url}/history", timeout=20).json()
    except Exception:
        return 0
    if not isinstance(history, dict): return 0
    existing = set()
    conn = _get_db()
    try:
        for row in conn.execute("SELECT file_name FROM gallery_images").fetchall(): existing.add(row["file_name"])
    finally: conn.close()
    # newest first; history record uses prompt id + output filename as a stable de-dup key.
    for prompt_id, record in sorted(history.items(), key=lambda kv: str(kv[1].get("status", {}).get("completed", "")), reverse=True):
        outputs = record.get("outputs", {}) if isinstance(record, dict) else {}
        for node in outputs.values() if isinstance(outputs, dict) else []:
            for item in node.get("images", []) if isinstance(node, dict) else []:
                filename, subfolder, image_type = item.get("filename", ""), item.get("subfolder", ""), item.get("type", "output")
                key = f"history_{prompt_id}_{subfolder}_{filename}"
                if not filename or key in existing: continue
                try:
                    r=requests.get(f"{api_url}/view", params={"filename":filename,"subfolder":subfolder,"type":image_type}, timeout=45); r.raise_for_status()
                    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
                    local=IMAGES_DIR / (key.replace("/", "_").replace("\\", "_") + Path(filename).suffix)
                    local.write_bytes(r.content)
                    w,h=width_from_bytes(r.content)
                    category=_safe_category(Path(subfolder).name if subfolder else "未分类")
                    add_image(str(local), key, len(r.content), w,h, model_name="", category=category)
                    existing.add(key); added += 1
                except Exception: continue
    return added

def _build_workflow(params: dict) -> dict:
    """Build a ComfyUI API workflow from generation parameters.

    v0.5: no longer assumes every model is a CheckpointLoaderSimple SDXL model.
    The deterministic adapter classifies the selected model and chooses a builder.
    """
    # 1.0.1：每次提交前按当前 ComfyUI 实时资源校准旧绑定，禁止跨机器模型名直接下发。
    api_url = _comfyui_url()
    resources = discover_resources(api_url)
    resolved, warnings, errors = resolve_runtime_assets(params, resources)
    if errors:
        raise HTTPException(400, {"code":"runtime_asset_mismatch", "message":"当前 ComfyUI 资源与工作流不匹配", "errors":errors, "warnings":warnings, "available":resources.get("resources", {})})
    params.clear(); params.update(resolved)
    workflow, _model_type = build_workflow_for_model(params)
    category = _safe_category(params.get("category", "未分类"))
    for node in workflow.values():
        if node.get("class_type") == "SaveImage":
            node.setdefault("inputs", {})["filename_prefix"] = category + "/image_gen" if category != "未分类" else "image_gen"
    return workflow

# ── 调试追踪：记录最后一次生成的完整信息 ──────────────────────────────────────
_last_generation_debug: dict = {}

def _run_comfyui(workflow: dict, debug_prompt: str = "", debug_negative: str = "") -> dict:
    """Submit workflow to ComfyUI and wait for completion."""
    global _last_generation_debug
    api_url = _comfyui_url()
    
    # 记录调试信息：发送前的完整提示词
    _last_generation_debug = {
        "timestamp": time.time(),
        "prompt_sent": debug_prompt,
        "negative_sent": debug_negative,
        "workflow_keys": list(workflow.keys()),
        "comfyui_url": api_url,
    }
    print(f"[DEBUG] 发送到ComfyUI的提示词: {debug_prompt[:200]}{'...' if len(debug_prompt) > 200 else ''}")
    print(f"[DEBUG] 反向提示词: {debug_negative[:100]}{'...' if len(debug_negative) > 100 else ''}")
    
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
            # Find SaveImage node output - 下载所有图片
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
                # 下载所有图片（支持批量生成）
                downloaded_images = []
                for img in images:
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
                        
                        downloaded_images.append({
                            "image_path": str(local_path),
                            "file_name": local_filename,
                            "file_size": len(img_r.content),
                            "width": w,
                            "height": h,
                        })
                    except Exception as e:
                        print(f"[WARNING] 下载图片失败 {img['filename']}: {e}")
                        continue
                
                if downloaded_images:
                    # 返回第一张图片的信息（兼容旧接口）
                    first_img = downloaded_images[0]
                    return {
                        "success": True,
                        "image_path": first_img["image_path"],
                        "file_name": first_img["file_name"],
                        "file_size": first_img["file_size"],
                        "width": first_img["width"],
                        "height": first_img["height"],
                        "seed": workflow.get("3", {}).get("inputs", {}).get("seed", 0),
                        "prompt_id": prompt_id,
                        "raw_images": images,
                        "all_images": downloaded_images  # 新增：返回所有下载的图片
                    }
                else:
                    return {"success": False, "error": "所有图片下载失败", "images_meta": images}
            
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

# ── 异步生图任务（UI 轮询用；线程只在插件进程内存中保留） ────────────────
_generation_tasks: dict[str, dict] = {}
_generation_tasks_lock = threading.Lock()

def _store_generated_images(result: dict, params: dict) -> dict:
    """将一次 ComfyUI 成功结果写入图库，返回 gallery ids。"""
    all_images = result.get("all_images", [result])
    gallery_ids = []
    enabled_loras = [x for x in (params.get("loras") or []) if x and x.get("enabled") is not False and (x.get("name") or x.get("lora_name"))]
    gallery_lora_name = "; ".join(
        f"{x.get('name') or x.get('lora_name')} (模型强度 {x.get('strength_model', x.get('strength', 0.6))}, CLIP强度 {x.get('strength_clip', x.get('strength', 0.6))})"
        for x in enabled_loras
    ) or params.get("lora_name", "")
    for img_data in all_images:
        img = add_image(file_path=img_data["image_path"], file_name=img_data["file_name"], file_size=img_data.get("file_size", 0), width=img_data.get("width", 0), height=img_data.get("height", 0), prompt=params.get("prompt", ""), negative_prompt=params.get("negative_prompt", ""), model_name=params.get("model_name", ""), lora_name=gallery_lora_name, category=_safe_category(params.get("category", "未分类")), steps=params.get("steps", 20), cfg=params.get("cfg", 7.0), seed=result.get("seed", -1))
        gallery_ids.append(img["id"])
    result["gallery_id"] = gallery_ids[0] if gallery_ids else None
    result["all_gallery_ids"] = gallery_ids
    _register_generated_image_to_library(result, params)
    return result

def _run_generation_task(task_id: str, params: dict) -> None:
    total = max(1, min(int(params.get("batch_size", 1) or 1), 8))
    completed_ids, failures = [], []
    for index in range(total):
        with _generation_tasks_lock:
            task = _generation_tasks.get(task_id)
            if not task or task.get("cancel_requested"):
                if task: task.update({"state":"cancelled", "message":"已停止等待；未提交的图片不会继续生成"})
                return
            task.update({"state":"running", "current":index + 1, "message":f"正在生成第 {index + 1} / {total} 张"})
        one = dict(params); one["batch_size"] = 1
        try:
            result = _run_comfyui(_build_workflow(one), debug_prompt=one.get("prompt", ""), debug_negative=one.get("negative_prompt", ""))
            if result.get("success"):
                result = _store_generated_images(result, one)
                completed_ids.extend(result.get("all_gallery_ids") or [])
                with _generation_tasks_lock:
                    _generation_tasks[task_id].update({"completed":len(completed_ids), "gallery_ids":completed_ids[:], "message":f"已完成 {len(completed_ids)} / {total} 张"})
            else:
                failures.append(result.get("error", "未知错误"))
        except Exception as exc:
            failures.append(str(exc))
        with _generation_tasks_lock:
            if task_id in _generation_tasks:
                _generation_tasks[task_id]["failed"] = len(failures)
    with _generation_tasks_lock:
        task = _generation_tasks.get(task_id)
        if task:
            task.update({"state":"completed" if completed_ids else "failed", "completed":len(completed_ids), "gallery_ids":completed_ids, "failures":failures, "message":f"已完成 {len(completed_ids)} / {total} 张" if completed_ids else "本次生成没有成功图片", "finished_at":time.time()})


# ── RealESRGAN 放大任务 ──────────────────────────────────────────────────────
def _upscale_models() -> list[dict]:
    """从当前 ComfyUI 的真实 UpscaleModelLoader 参数读取放大模型，绝不写死本机文件名。"""
    api_url = _comfyui_url()
    names: list[str] = []
    try:
        info = requests.get(f"{api_url}/object_info", timeout=8).json()
        node = info.get("UpscaleModelLoader", {})
        required = node.get("input", {}).get("required", {})
        raw = required.get("model_name", [])
        # ComfyUI versions return either [["a.pth", ...]] or
        # ["COMBO", {"options": ["a.pth", ...]}]. Support both.
        if raw and isinstance(raw[0], list):
            names = list(raw[0])
        elif len(raw) > 1 and isinstance(raw[1], dict):
            names = list(raw[1].get("options") or raw[1].get("choices") or [])
        else:
            names = []
    except Exception:
        return []
    out=[]
    for name in names:
        text=str(name); low=text.lower()
        if any(x in low for x in ("anime", "anime6b", "realesr-anime")): group="anime"; recommendation="二次元 / 插画推荐"
        elif any(x in low for x in ("x4plus", "realesrgan", "esrgan", "remacri", "ultrasharp")): group="general"; recommendation="通用 / 写实推荐"
        else: group="other"; recommendation="已检测到的放大模型"
        out.append({"id":text,"model":text,"label":text,"group":group,"recommendation":recommendation})
    return out


def _upscale_workflow(image_name: str, profile: str, category: str) -> dict:
    models = {x["id"]: x for x in _upscale_models()}
    info = models.get(profile)
    if not info:
        raise HTTPException(400, "未在当前 ComfyUI 中检测到此放大模型，请刷新后重新选择")
    prefix = _safe_category(category) + "/upscale" if _safe_category(category) != "未分类" else "upscale"
    return {
        "1": {"class_type":"LoadImage", "inputs":{"image":image_name}},
        "2": {"class_type":"UpscaleModelLoader", "inputs":{"model_name":info["model"]}},
        "3": {"class_type":"ImageUpscaleWithModel", "inputs":{"upscale_model":["2",0], "image":["1",0]}},
        "4": {"class_type":"SaveImage", "inputs":{"filename_prefix":prefix, "images":["3",0]}},
    }

def _upload_input_to_comfy(data: bytes, filename: str) -> str:
    api_url = _comfyui_url()
    safe_name = Path(filename or "image.png").name
    if Path(safe_name).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(400, "仅支持 PNG、JPG、JPEG、WEBP 图片")
    try:
        r = requests.post(f"{api_url}/upload/image", files={"image": (safe_name, data)}, data={"overwrite":"false"}, timeout=60)
        r.raise_for_status(); result=r.json()
    except requests.exceptions.ConnectionError:
        raise HTTPException(503, "无法连接到 ComfyUI，请先启动它")
    except Exception as exc:
        raise HTTPException(500, f"上传图片到 ComfyUI 失败：{exc}")
    name = result.get("name") or safe_name
    subfolder = result.get("subfolder") or ""
    return (subfolder.strip("/\\") + "/" if subfolder else "") + name

def _run_upscale_task(task_id: str, image_name: str, profile: str, category: str, source: dict) -> None:
    with _generation_tasks_lock:
        task=_generation_tasks.get(task_id)
        if task: task.update({"state":"running", "current":1, "message":"正在使用 RealESRGAN 放大图片"})
    try:
        result=_run_comfyui(_upscale_workflow(image_name, profile, category))
        if not result.get("success"):
            raise RuntimeError(result.get("error", "放大失败"))
        all_images=result.get("all_images", [result]); ids=[]; label=next((x["label"] for x in _upscale_models() if x["id"] == profile), profile)
        for img_data in all_images:
            img=add_image(file_path=img_data["image_path"], file_name=img_data["file_name"], file_size=img_data.get("file_size",0), width=img_data.get("width",0), height=img_data.get("height",0), prompt=source.get("prompt", ""), negative_prompt=source.get("negative_prompt", ""), model_name="放大 · " + label, lora_name=source.get("lora_name", ""), category=_safe_category(category), steps=source.get("steps",20), cfg=source.get("cfg",7.0), seed=source.get("seed",-1)); ids.append(img["id"])
        with _generation_tasks_lock:
            if task_id in _generation_tasks:
                t = _generation_tasks[task_id]
                all_ids = list(t.get("gallery_ids", [])) + ids
                t.update({"state":"completed", "completed":int(t.get("completed", 0)) + len(ids), "gallery_ids":all_ids, "message":"放大完成，已保存到图库", "finished_at":time.time()})
    except Exception as exc:
        with _generation_tasks_lock:
            if task_id in _generation_tasks: _generation_tasks[task_id].update({"state":"failed", "failed":1, "failures":[str(exc)], "message":"放大失败"})

def _start_upscale(image_name: str, profile: str, category: str, source: dict) -> dict:
    if profile not in {x["id"] for x in _upscale_models()}: raise HTTPException(400, "未在当前 ComfyUI 中检测到此放大模型，请刷新后重新选择")
    task_id=uuid.uuid4().hex
    with _generation_tasks_lock:
        _generation_tasks[task_id]={"id":task_id,"kind":"upscale","state":"queued","total":1,"current":0,"completed":0,"failed":0,"gallery_ids":[],"failures":[],"message":"放大任务已提交","created_at":time.time(),"cancel_requested":False}
    threading.Thread(target=_run_upscale_task,args=(task_id,image_name,profile,category,source),daemon=True,name=f"image-upscale-{task_id[:8]}").start()
    return {"success":True,"task_id":task_id,"total":1,"profile":next((x for x in _upscale_models() if x["id"] == profile), {"id": profile, "label": profile})}


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
        "frontend_entry": _plugin_meta.get("entry", {}).get("frontend", ""),
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
    """检查 ComfyUI 连接状态，并返回完整资源索引。"""
    api_url = _comfyui_url()
    try:
        resources = discover_resources(api_url)
        checkpoints = resources.get("resources", {}).get("checkpoints", [])
        loras = resources.get("resources", {}).get("loras", [])
        # 兼容旧前端：models 仍返回 checkpoints；新增 all_models/resources 给 v0.5 使用。
        return {
            "connected": True,
            "api_url": api_url,
            "models": checkpoints or [],
            "all_models": resources.get("models_flat", []),
            "resources": resources.get("resources", {}),
            "loras": loras or [],
            "model_count": len(checkpoints or []),
            "all_model_count": len(resources.get("models_flat", [])),
            "lora_count": len(loras or []),
            "object_info_loaded": bool(resources.get("object_info_loaded")),
            "samplers": resources.get("samplers", []),
            "schedulers": resources.get("schedulers", []),
            "node_capabilities": resources.get("node_capabilities", {}),
        }
    except Exception as e:
        return {"connected": False, "api_url": api_url, "error": str(e), "models": [], "all_models": [], "resources": {}, "loras": []}

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
    full_models = status.get("all_models") or [{"name": m, "kind": "checkpoints", "model_type": classify_model(m, "checkpoints")} for m in status.get("models", [])]
    primary_kinds = {"checkpoints", "unet", "diffusion_models"}
    selectable_models = [m for m in full_models if m.get("kind") in primary_kinds]
    # 用 ComfyUI 实时扫描的采样器/调度器覆盖 schema 里的旧选项
    fresh_samplers = status.get("samplers", [])
    fresh_schedulers = status.get("schedulers", [])
    if fresh_samplers and schema.get("sampler_name", {}).get("type") == "select":
        schema["sampler_name"]["options"] = fresh_samplers
    if fresh_schedulers and schema.get("scheduler", {}).get("type") == "select":
        schema["scheduler"]["options"] = fresh_schedulers
    return {
        "status": status,
        "models": [
            {**m, "has_workflow": (m.get("name") in by_model) or bool(presets), "binding": by_model.get(m.get("name"))}
            for m in selectable_models
        ],
        "all_models": full_models,
        "loras": status.get("loras", []),
        "samplers": status.get("samplers", []),
        "schedulers": status.get("schedulers", []),
        "selected_model": selected,
        "binding": binding,
        "has_workflow": bool(binding or presets),
        "params_schema": schema,
        "workflow_presets": presets,
        "selected_preset_id": (preset or {}).get("id") if preset else 0,
    }

@router.post("/workflows/bind")
def api_bind_workflow(payload: WorkflowBindRequest):
    # AI 可以提议，但后端必须校验，避免 Anima/Flux 被保存成 Illustrious。
    ok, corrected_type, message = validate_workflow_type_for_model(payload.model_name, payload.workflow_type)
    if not ok:
        raise HTTPException(status_code=400, detail={"error": message, "corrected_type": corrected_type})
    schema = payload.params_schema or DEFAULT_PARAM_SCHEMA
    return upsert_binding(
        model_name=payload.model_name,
        workflow_id=payload.workflow_id,
        workflow_name=payload.workflow_name,
        workflow_type=corrected_type,
        params_schema=json.dumps(schema, ensure_ascii=False),
        supports_lora=1 if payload.supports_lora else 0,
        supports_negative_prompt=1 if payload.supports_negative_prompt else 0,
    )

@router.post("/workflows/auto-bind")
def api_auto_bind_workflow(model_name: str = Query(...)):
    """按模型类型自动创建最小可运行绑定，不再依赖 AI 猜测。"""
    api_url = _comfyui_url()
    resources = discover_resources(api_url)
    all_models = resources.get("models_flat", [])
    found = next((m for m in all_models if m.get("name") == model_name), None)
    kind = (found or {}).get("kind", "")
    model_type = classify_model(model_name, kind)
    schema = build_param_schema(resources.get("samplers", []), resources.get("schedulers", []), model_type)
    return upsert_binding(
        model_name=model_name,
        workflow_id="auto:" + model_type,
        workflow_name="自动适配：" + model_type,
        workflow_type=model_type,
        params_schema=json.dumps(schema, ensure_ascii=False),
        supports_lora=1,
        supports_negative_prompt=1,
    )

@router.delete("/workflows/bind/{model_name}")
def api_delete_workflow_binding(model_name: str):
    delete_binding(model_name)
    return {"success": True}

@router.post("/generate")
def api_generate(params: GenerateRequest):
    """同步接口：供 Agent 工具/兼容旧前端使用。UI 应调用 /generate/async。"""
    data = params.model_dump()
    result = _run_comfyui(_build_workflow(data), debug_prompt=params.prompt, debug_negative=params.negative_prompt)
    if result.get("success"):
        _store_generated_images(result, data)
    return result

@router.post("/generate/async")
def api_generate_async(params: GenerateRequest):
    """创建后台任务。为确保批量时可逐张显示，任务会依次提交单张生成。"""
    if not params.prompt.strip():
        raise HTTPException(400, "提示词不能为空")
    task_id = uuid.uuid4().hex
    data = params.model_dump()
    total = max(1, min(int(data.get("batch_size", 1) or 1), 8))
    with _generation_tasks_lock:
        _generation_tasks[task_id] = {"id":task_id, "state":"queued", "total":total, "current":0, "completed":0, "failed":0, "gallery_ids":[], "failures":[], "message":"任务已提交，等待 ComfyUI 开始处理", "created_at":time.time(), "cancel_requested":False}
    threading.Thread(target=_run_generation_task, args=(task_id, data), daemon=True, name=f"image-gen-{task_id[:8]}").start()
    return {"success":True, "task_id":task_id, "total":total}

@router.get("/tasks/{task_id}")
def api_generation_task(task_id: str):
    with _generation_tasks_lock:
        task = _generation_tasks.get(task_id)
        if not task: raise HTTPException(404, "生成任务不存在或已过期")
        return dict(task)

@router.post("/tasks/{task_id}/stop")
def api_stop_generation_task(task_id: str):
    with _generation_tasks_lock:
        task = _generation_tasks.get(task_id)
        if not task: raise HTTPException(404, "生成任务不存在")
        task["cancel_requested"] = True
        task["message"] = "将在当前图片结束后停止等待"
        return {"success":True, "message":task["message"]}

@router.get("/upscale/profiles")
def api_upscale_profiles():
    items = _upscale_models()
    return {"items":items, "detected":len(items), "message":"已从当前 ComfyUI 的 UpscaleModelLoader 自动读取" if items else "未检测到放大模型"}

@router.post("/upscale/upload")
async def api_upscale_upload(image: UploadFile = File(...), profile: str = Query("anime_6b"), category: str = Query("未分类")):
    data=await image.read()
    if not data: raise HTTPException(400, "没有收到图片")
    if len(data) > 80 * 1024 * 1024: raise HTTPException(400, "图片超过 80MB，暂不支持")
    image_name=_upload_input_to_comfy(data, image.filename or "image.png")
    return _start_upscale(image_name, profile, category, {})

@router.post("/upscale/gallery/{image_id}")
def api_upscale_gallery_image(image_id: int, profile: str = Query("anime_6b"), category: str = Query("未分类")):
    img=get_image(image_id)
    if not img: raise HTTPException(404, "图片不存在")
    path=Path(img.get("file_path", ""))
    if not path.is_file(): raise HTTPException(404, "原图片文件不存在")
    image_name=_upload_input_to_comfy(path.read_bytes(), path.name)
    return _start_upscale(image_name, profile, category or img.get("category", "未分类"), img)

@router.post("/upscale/gallery/batch")
def api_upscale_gallery_batch(payload: BatchImagesRequest, profile: str = Query(""), category: str = Query("")):
    ids=list(dict.fromkeys(payload.image_ids))
    if not ids: raise HTTPException(400,"请至少选择一张图库图片")
    if len(ids)>50: raise HTTPException(400,"单批最多放大 50 张图片，请分批处理")
    if not profile: raise HTTPException(400,"请选择放大模型")
    if profile not in {x["id"] for x in _upscale_models()}: raise HTTPException(400,"当前放大模型不可用，请刷新后重新选择")
    task_id=uuid.uuid4().hex; sources=[]
    for image_id in ids:
        img=get_image(image_id)
        if not img: continue
        path=Path(img.get("file_path", ""))
        if not path.is_file(): continue
        sources.append({"id":image_id,"path":str(path),"name":path.name,"category":category or img.get("category","未分类"),"source":img})
    if not sources: raise HTTPException(404,"所选图片均不存在或原文件已丢失")
    with _generation_tasks_lock:
        _generation_tasks[task_id]={"id":task_id,"kind":"upscale","state":"queued","total":len(sources),"current":0,"completed":0,"failed":0,"gallery_ids":[],"failures":[],"message":"批量放大任务已提交","created_at":time.time(),"cancel_requested":False}
    threading.Thread(target=_run_upscale_batch_task,args=(task_id,sources,profile),daemon=True,name=f"image-upscale-batch-{task_id[:8]}").start()
    return {"success":True,"task_id":task_id,"total":len(sources)}

def _run_upscale_batch_task(task_id: str, sources: list[dict], profile: str) -> None:
    for index,item in enumerate(sources):
        with _generation_tasks_lock:
            task=_generation_tasks.get(task_id)
            if not task: return
            if task.get("cancel_requested"):
                task.update({"state":"cancelled","message":"已停止，未处理的图片已保留在队列"}); return
            task.update({"state":"running","current":index+1,"message":f"正在放大第 {index+1} / {len(sources)} 张：{item['name']}"})
        try:
            image_name=_upload_input_to_comfy(Path(item["path"]).read_bytes(), item["name"])
            _run_upscale_task(task_id,image_name,profile,item["category"],item["source"])
            with _generation_tasks_lock:
                t=_generation_tasks.get(task_id,{})
                if t.get("state")=="failed": t["state"]="running"
        except Exception as exc:
            with _generation_tasks_lock:
                t=_generation_tasks.get(task_id)
                if t: t["failed"]=(t.get("failed") or 0)+1; t.setdefault("failures",[]).append(f"{item['name']}: {exc}")
    with _generation_tasks_lock:
        t=_generation_tasks.get(task_id)
        if t and t.get("state") not in {"cancelled"}: t.update({"state":"completed","message":f"批量放大完成：成功 {t.get('completed',0)} 张，失败 {t.get('failed',0)} 张","finished_at":time.time()})

@router.get("/debug/last-generation")
def api_debug_last_generation():
    """返回最后一次生成的调试信息，用于排查提示词问题"""
    return {
        "last_generation": _last_generation_debug,
        "workflow_state": {
            "comfyui_url": _comfyui_url(),
            "current_time": time.time(),
        }
    }

# ── 图库路由 ────────────────────────────────────────────────────────────────

@router.get("/images")
def api_list_images(query: str = "", model_name: str = "", lora_name: str = "", min_rating: int = 0, category: str = "", sort: str = "newest", limit: int = Query(60, ge=1, le=200), offset: int = Query(0, ge=0)):
    # 大图库安全模式：数据库分页，前端只拿当前窗口，避免上百 GB 图片拖死进程。
    items, total = list_images_page(query, model_name, lora_name, min_rating, category, sort, limit, offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset, "has_more": offset + len(items) < total}

def _run_gallery_scan_task(task_id: str) -> None:
    try:
        with _generation_tasks_lock:
            _generation_tasks[task_id].update({"state":"running", "message":"正在后台扫描图库，不会阻塞界面"})
        output_root = _output_dir()
        output_added = _scan_output_gallery()
        history_added = _scan_comfy_history_gallery()
        with _generation_tasks_lock:
            _generation_tasks[task_id].update({"state":"completed", "added":output_added+history_added, "output_added":output_added, "history_added":history_added, "output_dir":str(output_root), "message":f"扫描完成：输出目录新增 {output_added} 张、历史记录新增 {history_added} 张", "finished_at":time.time()})
    except Exception as exc:
        with _generation_tasks_lock:
            if task_id in _generation_tasks: _generation_tasks[task_id].update({"state":"failed", "message":"图库扫描失败", "failures":[str(exc)]})

@router.post("/gallery/scan")
def api_gallery_scan():
    # 超大图库扫描改为后台任务，避免同步递归扫描卡住 QwenPaw 主界面。
    task_id=uuid.uuid4().hex
    with _generation_tasks_lock:
        _generation_tasks[task_id]={"id":task_id,"kind":"gallery_scan","state":"queued","message":"图库扫描已排队","created_at":time.time()}
    threading.Thread(target=_run_gallery_scan_task,args=(task_id,),daemon=True,name=f"gallery-scan-{task_id[:8]}").start()
    return {"success":True,"task_id":task_id,"message":"图库扫描已转入后台，可继续使用其他功能"}

@router.get("/gallery/categories")
def api_gallery_categories():
    # 同上：分类列表只读取目录，不在每次前端渲染时触发全量扫描。
    root = _output_dir(); cats = set(list_gallery_categories()) | {"未分类"}
    try: cats.update(p.name for p in root.iterdir() if p.is_dir())
    except OSError: pass
    return {"categories": sorted(cats)}

@router.post("/gallery/categories/create")
def api_gallery_category_create(name: str = Query("")):
    category = _safe_category(name)
    if category == "未分类" and (name or "").strip() != "未分类":
        raise HTTPException(400, "分类名称无效")
    (_output_dir() / category).mkdir(parents=True, exist_ok=True)
    return {"success": True, "category": category}

@router.post("/images/{image_id}/category")
def api_move_image_category(image_id: int, category: str = Query("未分类")):
    img = get_image(image_id)
    if not img:
        raise HTTPException(404, "图片不存在")
    category = _safe_category(category)
    src = Path(img["file_path"])
    if not src.is_file():
        raise HTTPException(404, "原文件已不存在")
    output_root = _output_dir().resolve()
    cache_root = IMAGES_DIR.resolve()
    src_resolved = src.resolve()

    # 插件旧版生成时会先下载一份 UUID_前缀的缓存图到 data/images，
    # 而真实 ComfyUI 原图仍在 output。整理时优先找到原图，避免只移动缓存副本。
    try:
        src_resolved.relative_to(cache_root)
        is_cache_image = True
    except ValueError:
        is_cache_image = False
    if is_cache_image:
        raw_name = src.name.split("_", 1)[1] if "_" in src.name else src.name
        candidates = [x for x in output_root.rglob(raw_name) if x.is_file()]
        if candidates:
            # 同名文件若有多个，优先 output 根目录中的原始未分类图，再按最新修改时间兜底。
            candidates.sort(key=lambda x: (x.parent != output_root, -x.stat().st_mtime))
            src = candidates[0]
            src_resolved = src.resolve()

    # 允许两类受插件管理的图片：ComfyUI output 原图，以及插件自身 data/images 缓存图。
    try:
        src_resolved.relative_to(output_root)
        managed = True
    except ValueError:
        try:
            src_resolved.relative_to(cache_root)
            managed = True
        except ValueError:
            managed = False
    if not managed:
        raise HTTPException(400, "这张图片不在 ComfyUI output 或插件图库目录中，不能自动移动")

    target_dir = output_root if category == "未分类" else output_root / category
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / (src.name.split("_", 1)[1] if is_cache_image and src_resolved.is_relative_to(cache_root) and "_" in src.name else src.name)
    if target.resolve() != src.resolve():
        if target.exists():
            target = target_dir / (src.stem + "_" + uuid.uuid4().hex[:6] + src.suffix)
        src.replace(target)
    return update_image_location(image_id, str(target), target.name, category)

@router.post("/gallery/batch/category")
def api_batch_move_images(payload: BatchImagesRequest, category: str = Query("未分类")):
    ids = list(dict.fromkeys(payload.image_ids))
    if not ids:
        raise HTTPException(400, "请至少选择一张图片")
    moved, failed = 0, []
    for image_id in ids:
        try:
            api_move_image_category(image_id, category)
            moved += 1
        except HTTPException as exc:
            failed.append({"id": image_id, "error": str(exc.detail)})
    if failed and not moved:
        raise HTTPException(400, {"error": "没有图片完成移动", "failed": failed})
    return {"success": True, "moved": moved, "failed": failed}

def _move_to_recycle_bin(path: Path) -> None:
    """Move a file to the Windows Recycle Bin instead of permanently deleting it."""
    if not path.exists():
        return
    # SHFileOperation requires a double-NUL terminated path.
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p), ("pTo", ctypes.c_wchar_p), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_bool), ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]
    FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT = 3, 0x40, 0x10, 0x4
    operation = SHFILEOPSTRUCTW(None, FO_DELETE, str(path) + "\0\0", None, FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT, False, None, None)
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise OSError("移动到回收站失败")

@router.post("/gallery/batch/delete")
def api_batch_delete_images(payload: BatchImagesRequest):
    ids = list(dict.fromkeys(payload.image_ids))
    if not ids:
        raise HTTPException(400, "请至少选择一张图片")
    deleted, failed = 0, []
    for image_id in ids:
        img = get_image(image_id)
        if not img:
            failed.append({"id": image_id, "error": "图片不存在"}); continue
        try:
            path = Path(img["file_path"])
            # Only physical ComfyUI-output files are recycled; managed cache files are also local and safe to recycle.
            if path.exists():
                _move_to_recycle_bin(path)
            delete_image(image_id)
            deleted += 1
        except Exception as exc:
            failed.append({"id": image_id, "error": str(exc)})
    if failed and not deleted:
        raise HTTPException(400, {"error": "没有图片完成删除", "failed": failed})
    return {"success": True, "deleted": deleted, "failed": failed}

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
        "comfyui_output_dir": get_config("comfyui_output_dir", str(_output_dir())),
    }

@router.patch("/config/{key}")
def api_patch_config(key: str, payload: ConfigPatch):
    if key not in {"comfyui_api_url", "comfyui_api_url_alt", "comfyui_output_dir"}:
        raise HTTPException(400, "不允许修改此配置")
    if key == "comfyui_output_dir":
        Path(payload.value).mkdir(parents=True, exist_ok=True)
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
