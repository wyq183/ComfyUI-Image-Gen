# -*- coding: utf-8 -*-
"""生图助手 — 后端路由 / ComfyUI 桥接 / Agent 工具注册"""
from __future__ import annotations
import json, os, sys, time, uuid, logging, hashlib, threading, subprocess, re
from datetime import datetime
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
    list_images, list_images_page, list_gallery_categories, list_gallery_filters, get_image, add_image, update_rating, update_notes, update_image_location, update_image_metadata, delete_image, cleanup_missing_images,
    list_presets, get_preset, add_preset, save_workflow_preset,
    save_recipe, list_recipes,
    get_config, set_config,
    list_bindings, get_binding, upsert_binding, delete_binding, DEFAULT_PARAM_SCHEMA,
    IMAGES_DIR, DB_DIR, _get_db
)
from comfy_adapter import (
    discover_resources, classify_model, validate_workflow_type_for_model,
    build_param_schema, build_workflow_for_model, resolve_runtime_assets,
    get_model_capabilities, clip_options_for_model, build_upscale_workflow, build_img2img_workflow,
    read_safetensors_architecture, read_safetensors_metadata, translate_comfy_failure,
    build_portable_package, find_missing_dependencies, PORTABLE_PNG_KEY, PORTABLE_SCHEMA, validate_workflow,
)
import prompt_library
import api_image_adapter as api_image

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
    model_name: str = "example-model-v1.safetensors"
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
    # 生成方式：txt2img 文生图 / img2img 以图生图（source_image 为 ComfyUI input 下的参考图）
    generation_mode: str = "txt2img"
    source_image: str = ""
    # 分体式模型（anima/z_image/flux/gguf/diffusion_model/nunchaku）运行资产
    clip_name: str = ""
    vae_name: str = ""
    clip_type: str = ""
    clip_device: str = ""
    weight_dtype: str = ""
    guidance: float = 3.5
    shift: float = 3.0
    clip_l_name: str = ""
    t5xxl_name: str = ""

class RatingPatch(BaseModel):
    rating: int = Field(ge=0, le=5)

class NotesPatch(BaseModel):
    notes: str = ""

class BatchImagesRequest(BaseModel):
    image_ids: list[int] = Field(default_factory=list, max_length=500)

class ConfigPatch(BaseModel):
    value: str = ""

class PanelContext(BaseModel):
    """前端面板当前状态：让 Agent 知道用户在面板里选了什么（尤其以图生图的参考图）。"""
    generation_mode: str = "txt2img"
    model_name: str = ""
    loras: list[str] = Field(default_factory=list)
    reference_image: str = ""          # 图库图片 ID 或本地绝对路径
    reference_file_name: str = ""
    reference_width: int = 0
    reference_height: int = 0
    denoise: float = 0.6
    steps: int = 0
    cfg: float = 0.0
    width: int = 0
    height: int = 0
    category: str = ""
    prompt: str = ""
    updated_at: float = 0.0

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

_comfy_root_cache: dict[str, Any] = {"value": None, "at": 0.0}
_COMFY_ROOT_TTL = 120.0

def _discover_comfyui_root() -> Path | None:
    """推导本机当前已连接 ComfyUI 的根目录，不写死任何整合包路径。

    仅查询已选 ComfyUI API 端口的监听进程；从该 Python 进程命令行的 main.py
    位置推导 ``<ComfyUI根目录>``。无法可靠识别时返回 None，绝不扫描磁盘上的
    任意目录。结果带 2 分钟缓存，避免每次请求都起 PowerShell。
    """
    now = time.time()
    if _comfy_root_cache["value"] is not None and now - _comfy_root_cache["at"] < _COMFY_ROOT_TTL:
        return _comfy_root_cache["value"]
    root: Path | None = None
    try:
        from urllib.parse import urlparse
        parsed = urlparse(_comfyui_url())
        if parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port:
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
            # 1) 命令行里的 main.py：可能是绝对路径、相对路径（如整合包从根目录启动
            #    的 `python.exe main.py`），也可能带正/反斜杠。
            match = re.search(r'(?:"([^"]*?main\.py)"|((?:[^\s"]*[\\/])?main\.py))', command_line, re.I)
            candidate = (match.group(1) or match.group(2)) if match else ""
            # 2) 命令行里的 python.exe，用于解析相对 main.py（整合包布局 <root>\python\python.exe）。
            exe_match = re.match(r'\s*"([^"]+\.exe)"|\s*([^\s"]+\.exe)', command_line, re.I)
            exe_path = (exe_match.group(1) or exe_match.group(2)) if exe_match else ""
            main_py: Path | None = None
            if candidate:
                cand = Path(candidate)
                if cand.is_absolute():
                    main_py = cand if cand.is_file() else None
                else:
                    bases: list[Path] = []
                    if exe_path:
                        exe = Path(exe_path)
                        bases = [exe.parent, exe.parent.parent]
                    for base in bases:
                        try:
                            if (base / cand).is_file():
                                main_py = base / cand
                                break
                        except OSError:
                            continue
            if main_py and main_py.is_file():
                root = main_py.parent
    except Exception:
        root = None
    _comfy_root_cache["value"] = root
    _comfy_root_cache["at"] = now
    return root

def _discover_comfyui_output_dir() -> Path | None:
    """推导 ``<ComfyUI根目录>/output``；无法可靠识别时返回 None，由 history 兜底。"""
    root = _discover_comfyui_root()
    if root:
        candidate = root / "output"
        if candidate.is_dir():
            return candidate
    return None

def _comfyui_models_root() -> Path | None:
    """推导 ``<ComfyUI根目录>/models``，用于按需读取 safetensors 架构。"""
    root = _discover_comfyui_root()
    if root:
        candidate = root / "models"
        if candidate.is_dir():
            return candidate
    return None

# ComfyUI 目录别名（unet/diffusion_models、clip/text_encoders 在不同版本下会互换）
_MODEL_KIND_DIRS: dict[str, tuple[str, ...]] = {
    "checkpoints": ("checkpoints",),
    "unet": ("unet", "diffusion_models"),
    "diffusion_models": ("diffusion_models", "unet"),
    "loras": ("loras", "lora"),
    "lora": ("loras", "lora"),
    "vae": ("vae",),
    "text_encoders": ("text_encoders", "clip"),
    "clip": ("clip", "text_encoders"),
    "controlnet": ("controlnet",),
    "upscale_models": ("upscale_models", "upscale"),
}

def _model_file_path(model_name: str, kind: str = "") -> Path | None:
    """在 ComfyUI models 目录里按 kind 别名解析模型文件的真实路径。"""
    name = str(model_name or "")
    if not name or ".." in Path(name).parts:
        return None
    root = _comfyui_models_root()
    if not root:
        return None
    subs = list(_MODEL_KIND_DIRS.get((kind or "").lower(), ()))
    for extra in ("checkpoints", "diffusion_models", "unet", "loras"):
        if extra not in subs:
            subs.append(extra)
    for sub in subs:
        try:
            candidate = root / sub / name
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None

def _model_architecture(model_name: str, kind: str = "") -> str:
    """按需读取某个模型文件的 safetensors 架构标识（带缓存）；失败返回空字符串。

    这是「尽力而为」的增强信号：读不到就回退到文件名分类，绝不影响主流程。
    """
    path = _model_file_path(model_name, kind)
    return read_safetensors_architecture(path) if path else ""

def _model_prompt_info(model_name: str, kind: str = "") -> dict:
    """汇总某个模型的提示词信息：架构、基础模型、触发词 / 高频标签（带中文翻译）。"""
    name = str(model_name or "")
    path = _model_file_path(name, kind)
    meta = read_safetensors_metadata(path) if path else {}
    translations = _load_tag_translations()
    words = prompt_library.extract_trigger_words(meta, 24)
    return {
        "name": name,
        "found": bool(path),
        "architecture": str(meta.get("_architecture") or _model_architecture(name, kind) or ""),
        "base_model": str(meta.get("modelspec.architecture") or meta.get("modelspec.base_model") or ""),
        "title": str(meta.get("modelspec.title") or ""),
        "trigger_words": [{"name": w, "zh": translations.get(w, "")} for w in words],
    }

def _classify_model(name: str, kind: str = "") -> str:
    """带真实架构信号的分类（读不到架构时与 classify_model(name, kind) 等价）。"""
    return classify_model(name, kind, _model_architecture(name, kind))

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

def _guess_output_root_for(path: Path) -> Path | None:
    """从图片路径里找最近的名为 output 的祖先目录，作为兜底输出根。"""
    try:
        for parent in path.resolve().parents:
            if parent.name.lower() == "output" and parent.is_dir():
                return parent
    except OSError:
        return None
    return None

# 各模型架构的加载节点 → 主模型名参数
_MODEL_LOADER_INPUTS = (
    ("CheckpointLoaderSimple", "ckpt_name"),
    ("CheckpointLoader", "ckpt_name"),
    ("UNETLoader", "unet_name"),
    ("UnetLoaderGGUF", "unet_name"),
    ("UnetLoaderGGUFAdvanced", "unet_name"),
    ("DiffusionModelLoader", "unet_name"),
    ("NunchakuFluxDiTLoader", "model_path"),
)
# 文本提示词节点 → 提示词字段（按优先级）
_PROMPT_INPUT_KEYS = ("text", "user_prompt", "t5xxl", "clip_l", "text_g", "text_l", "prompt")


def _node_prompt_text(inputs: dict) -> str:
    for key in _PROMPT_INPUT_KEYS:
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _resolve_conditioning_text(nodes: dict, ref, depth: int = 0) -> str:
    """沿 conditioning 链路回溯到文本节点，返回正向/负向文本。

    兼容 Lumina2（CLIPTextEncodeLumina2.user_prompt）、Flux（FluxGuidance → CLIPTextEncode）、
    SD3（CLIPTextEncodeSD3.t5xxl）等；ConditioningZeroOut 视为空（无真实负向）。
    """
    if depth > 8 or not isinstance(ref, list) or not ref:
        return ""
    node = nodes.get(str(ref[0]), nodes.get(ref[0]))
    if not isinstance(node, dict):
        return ""
    ct = str(node.get("class_type", ""))
    inputs = node.get("inputs") or {}
    if ct == "ConditioningZeroOut":
        return ""
    text = _node_prompt_text(inputs)
    if text:
        return text
    for key in ("conditioning", "conditioning_1", "positive", "text"):
        link = inputs.get(key)
        if isinstance(link, list):
            text = _resolve_conditioning_text(nodes, link, depth + 1)
            if text:
                return text
    return ""


def _read_image_meta(path: Path) -> dict:
    """从图片的 ComfyUI 元数据里读取模型 / 提示词 / 采样参数。

    支持合体 checkpoint、UNETLoader/GGUF 分体模型，以及 Lumina2（Z-Image）、
    Flux、SD3 等不同文本编码节点。
    """
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
                if not isinstance(nodes, dict):
                    return out
                loader_map = dict(_MODEL_LOADER_INPUTS)
                first_text = ""
                for node in nodes.values():
                    if not isinstance(node, dict): continue
                    ct, inp = str(node.get("class_type", "")), (node.get("inputs") or {})
                    # 主模型名（合体 checkpoint / 分体 UNet / GGUF / Nunchaku）
                    key = loader_map.get(ct)
                    if key and inp.get(key):
                        out["model_name"] = str(inp.get(key))
                    if ct.lower().startswith("lora"):
                        name = inp.get("lora_name") or inp.get("lora_name_1")
                        if name: out["lora_name"] = (out["lora_name"] + "; " if out["lora_name"] else "") + str(name)
                    if ct in ("KSampler", "KSamplerAdvanced"):
                        for k in ("steps", "cfg", "seed"):
                            if k in inp: out[k] = inp[k]
                        # KSampler.positive / .negative 是 [node_id, slot] 链接，沿链路回溯文本。
                        pos = _resolve_conditioning_text(nodes, inp.get("positive"))
                        if pos: out["prompt"] = pos
                        neg = _resolve_conditioning_text(nodes, inp.get("negative"))
                        if neg: out["negative_prompt"] = neg
                    text = _node_prompt_text(inp)
                    if text and not first_text:
                        first_text = text
                # 没有采样器连线时的兜底：取第一个文本节点作为正向提示词。
                if not out["prompt"] and first_text:
                    out["prompt"] = first_text
    except Exception:
        pass
    return out

def _scan_output_gallery() -> int:
    root = _output_dir(); added = 0
    if not root.is_dir():
        return 0
    # 1. 批量读取已有路径到 set，避免逐条 SQL
    conn = _get_db()
    existing = set()
    for row in conn.execute("SELECT file_path FROM gallery_images").fetchall():
        existing.add(str(row["file_path"]).lower())
    conn.close()
    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
    # 2. 用 os.walk 快速遍历目录
    for dirpath, _, filenames in os.walk(str(root)):
        for fname in filenames:
            ext = Path(fname).suffix.lower()
            if ext not in IMAGE_EXTS:
                continue
            fpath = str(Path(dirpath) / fname)
            try:
                st = os.stat(fpath)
            except OSError:
                continue
            # 3. 按分类归类
            try:
                rel = Path(fpath).relative_to(root).parts
                category = _safe_category(rel[0]) if len(rel) > 1 else "未分类"
            except ValueError:
                category = "未分类"
            if fpath.lower() in existing:
                # 已存在：只更新分类，不重新读元数据
                conn = _get_db()
                conn.execute("UPDATE gallery_images SET category=? WHERE file_path=?", (category, fpath))
                conn.commit(); conn.close()
                continue
            # 4. 新文件：读元数据 + 添加
            meta = _read_image_meta(Path(fpath))
            w = h = 0
            try:
                from PIL import Image
                with Image.open(fpath) as im:
                    w, h = im.size
            except Exception:
                pass
            add_image(fpath, fname, st.st_size, w, h,
                      meta.get("prompt", ""), meta.get("negative_prompt", ""),
                      meta.get("model_name", ""), meta.get("lora_name", ""),
                      steps=int(meta.get("steps") or 20), cfg=float(meta.get("cfg") or 7),
                      seed=int(meta.get("seed") or -1), category=category,
                      generated_at=datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"))
            existing.add(fpath.lower())
            added += 1
    return added

def _history_completed_ts(record: dict) -> float | None:
    """从 ComfyUI /history 记录里取真实完成时间（秒）。

    注意：status.completed 是**布尔值**（True/False），不是时间戳——早期代码把它
    当时间戳用，float(True)==1.0 会写成 1970-01-01。真实时间在
    status.messages 的 execution_success/execution_start 里（毫秒）。
    """
    if not isinstance(record, dict):
        return None
    st = record.get("status")
    if not isinstance(st, dict):
        return None
    fallback: float | None = None
    for entry in (st.get("messages") or []):
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        name, payload = entry[0], entry[1]
        if not isinstance(payload, dict):
            continue
        value = payload.get("timestamp")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if name == "execution_success":
            return float(value) / 1000.0
        if name == "execution_start" and fallback is None:
            fallback = float(value) / 1000.0
    # 兼容旧版 ComfyUI：completed 直接是秒级时间戳
    completed = st.get("completed")
    if isinstance(completed, (int, float)) and not isinstance(completed, bool) and completed > 1e9:
        return float(completed)
    return fallback

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
        for row in conn.execute("SELECT file_name, file_path FROM gallery_images").fetchall():
            existing.add(row["file_name"])
            existing.add(Path(row["file_path"]).name)
    finally: conn.close()
    # newest first; history record uses prompt id + output filename as a stable de-dup key.
    for prompt_id, record in sorted(history.items(), key=lambda kv: _history_completed_ts(kv[1]) or 0, reverse=True):
        outputs = record.get("outputs", {}) if isinstance(record, dict) else {}
        for node in outputs.values() if isinstance(outputs, dict) else []:
            for item in node.get("images", []) if isinstance(node, dict) else []:
                filename, subfolder, image_type = item.get("filename", ""), item.get("subfolder", ""), item.get("type", "output")
                key = f"history_{prompt_id}_{subfolder}_{filename}"
                if not filename or key in existing or filename in existing: continue
                try:
                    r=requests.get(f"{api_url}/view", params={"filename":filename,"subfolder":subfolder,"type":image_type}, timeout=45); r.raise_for_status()
                    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
                    local=IMAGES_DIR / (key.replace("/", "_").replace("\\", "_") + Path(filename).suffix)
                    local.write_bytes(r.content)
                    w,h=width_from_bytes(r.content)
                    category=_safe_category(Path(subfolder).name if subfolder else "未分类")
                    completed_ts = _history_completed_ts(record)
                    try:
                        gen_time = datetime.fromtimestamp(completed_ts).strftime("%Y-%m-%d %H:%M:%S") if completed_ts else None
                    except (OSError, OverflowError, ValueError):
                        gen_time = None
                    meta = _read_image_meta(local)
                    add_image(str(local), key, len(r.content), w, h,
                              meta.get("prompt", ""), meta.get("negative_prompt", ""),
                              meta.get("model_name", ""), meta.get("lora_name", ""),
                              steps=int(meta.get("steps") or 20), cfg=float(meta.get("cfg") or 7),
                              seed=int(meta.get("seed") or -1), category=category, generated_at=gen_time)
                    existing.add(key); existing.add(filename); existing.add(local.name); added += 1
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
    # 用实时资源反查模型 kind，让 classify_model 拿到的信息更准（unet/diffusion_models/checkpoints）。
    _model_name = params.get("model_name", "")
    _kind = ""
    for _m in (resources.get("models_flat") or []):
        if _m.get("name") == _model_name:
            _kind = _m.get("kind", "")
            break
    _arch = _model_architecture(_model_name, _kind)
    _mode = str(params.get("generation_mode") or "txt2img").lower()
    try:
        if _mode == "img2img":
            _source = str(params.get("source_image") or "").strip()
            if not _source:
                raise HTTPException(400, "以图生图需要先选择一张参考图（从图库选或上传）")
            params["source_image"] = _source
            workflow, _model_type = build_img2img_workflow(params, _source, _kind, _arch)
        else:
            workflow, _model_type = build_workflow_for_model(params, _kind, _arch)
    except ValueError as _e:
        raise HTTPException(400, {"code":"unsupported_model_type", "message":str(_e), "available":resources.get("resources", {})})
    params["_model_type"] = _model_type
    category = _safe_category(params.get("category", "未分类"))
    for node in workflow.values():
        if node.get("class_type") == "SaveImage":
            node.setdefault("inputs", {})["filename_prefix"] = category + "/image_gen" if category != "未分类" else "image_gen"
    return workflow

# ── 调试追踪：记录最后一次生成的完整信息 ──────────────────────────────────────
_last_generation_debug: dict = {}


def _comfyui_error_message(r) -> dict:
    """从 ComfyUI 4xx 响应提取错误，翻译成用户可读的 {error_code, error, technical_detail}。"""
    try:
        body = r.json()
    except Exception:
        return translate_comfy_failure(f"ComfyUI 返回 {r.status_code}: {r.text[:500]}")
    node_errors = body.get("node_errors") or {}
    parts = []
    for node_id, err in node_errors.items():
        errs = err.get("errors") or []
        for e in errs:
            msg = e.get("message", "")
            detail = e.get("details", "")
            parts.append(f"节点 {node_id}({err.get('class_type', '?')}): {msg} {detail}".strip())
    if parts:
        raw = "；".join(parts[:5])
    else:
        err = body.get("error") or {}
        if isinstance(err, dict) and err.get("message"):
            raw = f"{err.get('message')} {err.get('details', '')}".strip()
        else:
            raw = f"ComfyUI 返回 {r.status_code}"
    return translate_comfy_failure(raw)


def _exc_text(exc: Exception) -> str:
    """把异常转成适合展示的一行文本；HTTPException 的 dict detail 优先取用户可读的 error。"""
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        return str(detail.get("error") or detail.get("message") or detail)
    if detail is not None:
        return str(detail)
    return str(exc)


def _comfyui_prompt_queued(api_url: str, prompt_id: str) -> bool:
    """该 prompt 是否还在 ComfyUI 队列里（排队或执行中）。

    用于区分「在等 GPU 排队」和「真的卡住」：用户在 ComfyUI 里手动跑别的任务时，
    插件提交的任务会一直在 pending，按固定 300 秒判定超时会误报失败。
    """
    try:
        q = requests.get(f"{api_url}/queue", timeout=10).json()
    except Exception:
        return False
    for key in ("queue_running", "queue_pending"):
        for item in q.get(key) or []:
            if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] == prompt_id:
                return True
    return False


def _run_comfyui(workflow: dict, debug_prompt: str = "", debug_negative: str = "", portable_package: dict | None = None) -> dict:
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
    
    # Submit：把自描述包塞进 extra_pnginfo，ComfyUI 的 SaveImage 会把它写进生成图。
    payload: dict = {"prompt": workflow}
    if portable_package:
        try:
            # 传 dict：ComfyUI SaveImage 会自行 json.dumps 一次，读回时一次解码即得 dict。
            payload["extra_data"] = {"extra_pnginfo": {PORTABLE_PNG_KEY: portable_package}}
        except Exception:
            pass
    try:
        r = requests.post(f"{api_url}/prompt", json=payload, timeout=30)
        if r.status_code >= 400:
            raise HTTPException(status_code=502, detail=_comfyui_error_message(r))
        result = r.json()
    except requests.exceptions.ConnectionError:
        raise HTTPException(status_code=503, detail=f"无法连接到 ComfyUI ({api_url})，请确认 ComfyUI 已启动")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"提交生图任务失败: {str(e)}")

    prompt_id = result.get("prompt_id", "")
    if not prompt_id:
        raise HTTPException(status_code=500, detail="ComfyUI 未返回 prompt_id")

    # Poll for completion
    max_wait = 300           # 实际执行超时（单张）
    max_queue_wait = 1800    # 排队总上限：ComfyUI 在跑别的任务时不计入执行超时
    poll_interval = 2
    waited = 0
    queue_waited = 0
    conn_failures = 0        # 连续连接失败次数：ComfyUI 卡死/退出时快速失败并给出明确提示
    while True:
        time.sleep(poll_interval)
        try:
            r = requests.get(f"{api_url}/history/{prompt_id}", timeout=10)
            r.raise_for_status()
            history = r.json()
            conn_failures = 0
        except Exception:
            conn_failures += 1
            if conn_failures >= 5:
                return {"success": False, "error": "ComfyUI 无响应（连续多次连接失败，进程可能已卡死或退出），已放弃等待。请重启 ComfyUI 后重试。"}
            continue
        # 排队等 GPU 不算「卡住」：只要还在 ComfyUI 队列里就重置执行计时。
        if prompt_id not in history and _comfyui_prompt_queued(api_url, prompt_id):
            queue_waited += poll_interval
            waited = 0
            if queue_waited >= max_queue_wait:
                return {"success": False, "error": f"排队超过 {max_queue_wait // 60} 分钟仍未开始执行（ComfyUI 队列被其他任务占满），已放弃等待"}
            continue
        waited += poll_interval
        if waited >= max_wait:
            return {"success": False, "error": f"生图超时（{max_wait}秒），请在 ComfyUI 中查看进度"}

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
    # 插件路由在 QwenPaw 里挂在 /api/<prefix> 下（新版）；旧版是 /<prefix>。
    # 逐个尝试，并只接受 JSON 响应，避免被前端 SPA 的 HTML 200 误判为成功。
    base = ARTIFACT_LIBRARY_API_URL.rstrip("/")
    for url in (f"{base}/api/artifact-library/artifacts", f"{base}/artifact-library/artifacts"):
        try:
            resp = requests.post(url, json=payload, timeout=8)
            if resp.status_code < 400 and "json" in (resp.headers.get("content-type") or "").lower():
                return
        except Exception:
            continue

def width_from_bytes(data: bytes) -> tuple:
    """Try to get image dimensions from raw bytes without PIL."""
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(data))
        return img.width, img.height
    except Exception:
        return 0, 0

# ── 生图任务（UI 轮询用；线程只在插件进程内存中保留） ──────────────────────
_generation_tasks: dict[str, dict] = {}
_generation_tasks_lock = threading.RLock()

def _create_generation_task(total: int, message: str = "任务已提交，等待 ComfyUI 开始处理", source: str = "ui") -> str:
    """登记一个可见的生图任务并返回 task_id。

    UI 发起的任务与 Agent 工具发起的任务都会走这里，这样无论谁触发生图，
    插件面板都能通过 /active-task 看到进度。
    """
    task_id = uuid.uuid4().hex
    with _generation_tasks_lock:
        _generation_tasks[task_id] = {
            "id": task_id, "state": "queued", "total": total, "current": 0,
            "completed": 0, "failed": 0, "gallery_ids": [], "failures": [],
            "message": message, "created_at": time.time(), "cancel_requested": False,
            "source": source,
        }
        # 轻量清理：只保留最近 60 个任务，避免长期运行内存增长。
        if len(_generation_tasks) > 60:
            for tid in sorted(_generation_tasks, key=lambda k: _generation_tasks[k].get("created_at", 0))[:-60]:
                _generation_tasks.pop(tid, None)
    return task_id

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
        gen_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        img = add_image(file_path=img_data["image_path"], file_name=img_data["file_name"], file_size=img_data.get("file_size", 0), width=img_data.get("width", 0), height=img_data.get("height", 0), prompt=params.get("prompt", ""), negative_prompt=params.get("negative_prompt", ""), model_name=params.get("model_name", ""), lora_name=gallery_lora_name, category=_safe_category(params.get("category", "未分类")), steps=params.get("steps", 20), cfg=params.get("cfg", 7.0), seed=result.get("seed", -1), generated_at=gen_time)
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
            _wf = _build_workflow(one)
            # 把工作流 + 参数 + 依赖清单塞进 PNG，任何一张图都能复现。
            _pkg = build_portable_package(_wf, one, one.get("_model_type", ""))
            result = _run_comfyui(_wf, debug_prompt=one.get("prompt", ""), debug_negative=one.get("negative_prompt", ""), portable_package=_pkg)
            if result.get("success"):
                result = _store_generated_images(result, one)
                completed_ids.extend(result.get("all_gallery_ids") or [])
                with _generation_tasks_lock:
                    _generation_tasks[task_id].update({"completed":len(completed_ids), "gallery_ids":completed_ids[:], "message":f"已完成 {len(completed_ids)} / {total} 张"})
            else:
                failures.append(result.get("error", "未知错误"))
        except Exception as exc:
            failures.append(_exc_text(exc))
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
        info = requests.get(f"{api_url}/object_info", timeout=20).json()
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


def _upscale_options(mode: str = "fast", profile: str = "", scale: float = 0.0, denoise: float | None = None,
                     steps: int = 15, cfg: float = 0.0, sampler: str = "", scheduler: str = "",
                     seed_mode: str = "source", tile_size: int = 512, seam_fix: str = "None",
                     category: str = "未分类", prompt_mode: str = "auto", tile_padding: int = 64,
                     mask_blur: int = 16, seam_fix_denoise: float = 0.5, sharpen_alpha: float = 0.08,
                     face_detail: bool = False, face_denoise: float = 0.35, iterations: int = 2,
                     target_denoise: float = 0.0, step_mode: str = "simple") -> dict:
    """归一化放大/高清修复参数（前端、队列、Agent 都走这里）。"""
    mode = str(mode or "fast").lower()
    if mode not in {"fast", "hires", "tiled", "iterative"}:
        mode = "fast"
    if denoise is None:
        # 分块模式每个块都会重跑提示词，denoise 越低越不容易在块里长出新主体
        denoise = 0.2 if mode == "tiled" else (0.4 if mode == "iterative" else 0.35)
    pm = str(prompt_mode or "auto").lower()
    if pm not in {"auto", "quality", "full"}:
        pm = "auto"
    return {
        "mode": mode, "profile": str(profile or ""), "scale": float(scale or 0),
        "denoise": float(denoise), "steps": int(steps or 15),
        "cfg": float(cfg or 0), "sampler_name": str(sampler or ""), "scheduler": str(scheduler or ""),
        "seed_mode": str(seed_mode or "source").lower(), "tile_size": int(tile_size or 512),
        "seam_fix_mode": str(seam_fix or "None"), "category": _safe_category(category),
        "prompt_mode": pm, "tile_padding": int(tile_padding if tile_padding is not None else 64),
        "mask_blur": int(mask_blur if mask_blur is not None else 16),
        "seam_fix_denoise": float(seam_fix_denoise if seam_fix_denoise is not None else 0.5),
        "sharpen_alpha": float(sharpen_alpha if sharpen_alpha is not None else 0.08),
        "face_detail": bool(face_detail), "face_denoise": float(face_denoise if face_denoise is not None else 0.35),
        "iterations": max(1, int(iterations or 2)),
        "target_denoise": float(target_denoise or 0.0), "step_mode": str(step_mode or "simple"),
    }


def _image_actual_size(path: Path | None) -> tuple[int, int]:
    """读输入图真实像素尺寸；图库元数据可能过期，构建工作流要以文件为准。"""
    if not path:
        return 0, 0
    try:
        from PIL import Image
        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:
        return 0, 0


def _parse_lora_string(value) -> list[dict]:
    """解析图库里的 lora_name（支持带强度后缀和裸文件名两种格式）。"""
    out: list[dict] = []
    for line in str(value or "").split(";"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(.*?)\s*\(模型强度\s*([^,]+),\s*CLIP强度\s*([^)]+)\)$", line)
        if m:
            try: sm = float(m.group(2))
            except Exception: sm = 0.6
            try: sc = float(m.group(3))
            except Exception: sc = 0.6
            out.append({"name": m.group(1).strip(), "enabled": True, "strength_model": sm, "strength_clip": sc})
        else:
            out.append({"name": line, "enabled": True, "strength_model": 0.6, "strength_clip": 0.6})
    return out


_OBJECT_INFO_CACHE: dict = {}
_OBJECT_INFO_TS: float = 0.0


def _comfyui_object_info() -> dict:
    """带 120s 缓存的 ComfyUI object_info，用于探测 TiledDiffusion / ControlNet 等节点是否可用。"""
    global _OBJECT_INFO_CACHE, _OBJECT_INFO_TS
    if _OBJECT_INFO_CACHE and (time.time() - _OBJECT_INFO_TS) < 120:
        return _OBJECT_INFO_CACHE
    info = requests.get(f"{_comfyui_url()}/object_info", timeout=20).json()
    _OBJECT_INFO_CACHE = info if isinstance(info, dict) else {}
    _OBJECT_INFO_TS = time.time()
    return _OBJECT_INFO_CACHE


def _upscale_workflow(image_name: str, opts: dict, source: dict, image_path: Path | None = None) -> dict:
    """构建放大 / 高清修复工作流。

    fast  = 纯像素放大（ESRGAN，不补细节）
    hires = 放大 + VAEEncode + KSampler(低 denoise) 二次采样（补细节，需要原模型）
    tiled = UltimateSDUpscale 分块二次采样（超大图 / 显存不够）
    """
    profile = str(opts.get("profile") or "")
    models = {x["id"]: x for x in _upscale_models()}
    info = models.get(profile)
    if not info:
        raise HTTPException(400, "未在当前 ComfyUI 中检测到此放大模型，请刷新后重新选择")
    mode = opts.get("mode") or "fast"
    scale = float(opts.get("scale") or 0) or (4.0 if mode == "fast" else 2.0)
    actual_w, actual_h = _image_actual_size(image_path)
    # hires 会把整张图一次性送进 KSampler：目标分辨率过大时（8GB 卡上 SDXL 会慢到不可用）自动改用分块。
    if mode == "hires" and (actual_w or actual_h):
        _tw = int(round(actual_w * scale)); _th = int(round(actual_h * scale))
        if _tw * _th > HIRES_SAFE_PIXELS:
            mode = "tiled"
            opts["mode"] = "tiled"
            opts["auto_note"] = (f"目标 {_tw}×{_th}（{_tw * _th / 1e6:.1f}M 像素）超出整图高清修复的安全范围，"
                                 f"已自动改用分块高清修复")
    # 社区进阶能力探测：TiledDiffusion（MultiDiffusion）、Tile ControlNet 是否可用。
    _info = _comfyui_object_info()
    has_tiled_diffusion = "TiledDiffusion" in _info
    has_controlnet = "ControlNetApply" in _info and "ControlNetLoader" in _info
    tile_controlnet = ""
    if has_controlnet:
        try:
            cn_opts = ((_info.get("ControlNetLoader", {}).get("input", {}).get("required", {}) or {}).get("control_net_name") or [[]])[0]
            tile_controlnet = next((str(m) for m in cn_opts if isinstance(m, str) and ("tile" in m.lower() or "union" in m.lower())), "")
        except Exception:
            tile_controlnet = ""
    _tw2 = int(round((actual_w or 1024) * scale)); _th2 = int(round((actual_h or 1024) * scale))
    # MultiDiffusion 没有 TiledVAE 节点，整图 VAEEncode 在超大目标分辨率会爆显存，所以只在 4M 像素内启用。
    use_multidiffusion = has_tiled_diffusion and mode == "tiled" and (_tw2 * _th2) <= 4_000_000
    _eff_denoise = float(opts.get("denoise") if opts.get("denoise") is not None else (0.2 if mode == "tiled" else 0.35))
    use_controlnet = bool(tile_controlnet) and _eff_denoise >= 0.3
    build_opts = {
        "mode": mode, "upscale_model": info["model"], "scale": scale,
        "denoise": float(opts.get("denoise")) if opts.get("denoise") is not None else None,
        "steps": int(opts.get("steps") or 15),
        "cfg": float(opts.get("cfg") or 0), "sampler_name": opts.get("sampler_name") or "",
        "scheduler": opts.get("scheduler") or "", "tile_size": int(opts.get("tile_size") or 512),
        "seam_fix_mode": opts.get("seam_fix_mode") or "None",
        "prompt_mode": opts.get("prompt_mode") or "auto",
        "tile_padding": int(opts.get("tile_padding") if opts.get("tile_padding") is not None else 64),
        "mask_blur": int(opts.get("mask_blur") if opts.get("mask_blur") is not None else 16),
        "seam_fix_denoise": float(opts.get("seam_fix_denoise") if opts.get("seam_fix_denoise") is not None else 0.5),
        "sharpen_alpha": float(opts.get("sharpen_alpha") if opts.get("sharpen_alpha") is not None else 0.08),
        "face_detail": bool(opts.get("face_detail")),
        "face_denoise": float(opts.get("face_denoise") if opts.get("face_denoise") is not None else 0.35),
        "iterations": max(1, int(opts.get("iterations") or 2)),
        "target_denoise": float(opts.get("target_denoise") if opts.get("target_denoise") is not None else 0.0),
        "step_mode": opts.get("step_mode") or "simple",
        "has_tiled_diffusion": has_tiled_diffusion, "has_controlnet": has_controlnet,
        "tile_controlnet": tile_controlnet, "use_multidiffusion": use_multidiffusion,
        "use_controlnet": use_controlnet,
        "seed": -1 if opts.get("seed_mode") == "random" else int(source.get("seed") if source.get("seed") is not None else -1),
    }
    if mode == "fast":
        params = {"width": actual_w or int(source.get("width") or 1024), "height": actual_h or int(source.get("height") or 1024)}
        wf, _mt = build_upscale_workflow(params, image_name, build_opts)
    else:
        params = {
            "model_name": source.get("model_name") or "",
            "prompt": source.get("prompt") or "",
            "negative_prompt": source.get("negative_prompt") or "",
            "steps": source.get("steps") or 20, "cfg": source.get("cfg") or 7.0,
            "seed": source.get("seed") if source.get("seed") is not None else -1,
            "loras": _parse_lora_string(source.get("lora_name")),
            "width": actual_w or int(source.get("width") or 1024),
            "height": actual_h or int(source.get("height") or 1024),
        }
        resources = discover_resources(_comfyui_url())
        resolved, _warnings, errors = resolve_runtime_assets(params, resources)
        if errors:
            raise HTTPException(400, {"code": "runtime_asset_mismatch",
                                      "message": "这张图的原模型 / 资源与当前 ComfyUI 不匹配，无法做高清修复（可改用「快速放大」）",
                                      "errors": errors})
        kind = ""
        for m in (resources.get("models_flat") or []):
            if m.get("name") == resolved.get("model_name"):
                kind = m.get("kind", ""); break
        build_opts["model_kind"] = kind
        build_opts["architecture"] = _model_architecture(resolved.get("model_name"), kind)
        try:
            wf, _mt = build_upscale_workflow(resolved, image_name, build_opts)
        except ValueError as exc:
            raise HTTPException(400, {"code": "unsupported_upscale_source", "message": str(exc)})
    prefix = _safe_category(opts.get("category") or source.get("category") or "未分类")
    prefix = prefix + "/upscale" if prefix != "未分类" else "upscale"
    for node in wf.values():
        if node.get("class_type") == "SaveImage":
            node.setdefault("inputs", {})["filename_prefix"] = prefix
    return wf

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

_UPSCALE_MODE_LABELS = {"fast": "放大", "hires": "高清修复", "tiled": "分块高清修复", "iterative": "迭代放大"}

# 整图二次采样（hires）的安全像素上限：8GB 显卡上 SDXL 超过这个量级会慢到不可用。
# 参考：1920×1280 ≈ 2.5M 像素可接受；4608×2592 ≈ 11.9M 像素在 4060 上要跑很久很久。
HIRES_SAFE_PIXELS = 3_000_000


def _run_upscale_task(task_id: str, image_name: str, opts: dict, source: dict, image_path: Path | None = None) -> None:
    mode = opts.get("mode") or "fast"
    with _generation_tasks_lock:
        task=_generation_tasks.get(task_id)
        if task: task.update({"state":"running", "current":1, "message":f"正在执行{_UPSCALE_MODE_LABELS.get(mode, '放大')}…"})
    try:
        wf = _upscale_workflow(image_name, opts, source, image_path)
        mode = opts.get("mode") or mode
        note = str(opts.get("auto_note") or "")
        if note:
            with _generation_tasks_lock:
                t = _generation_tasks.get(task_id)
                if t: t.update({"upscale_mode": mode, "message": f"正在执行{_UPSCALE_MODE_LABELS.get(mode, '放大')}…（{note}）"})
        result=_run_comfyui(wf)
        if not result.get("success"):
            raise RuntimeError(result.get("error", "放大失败"))
        all_images=result.get("all_images", [result]); ids=[]
        label=next((x["label"] for x in _upscale_models() if x["id"] == opts.get("profile")), opts.get("profile") or "")
        prefix = _UPSCALE_MODE_LABELS.get(mode, "放大")
        category = _safe_category(opts.get("category") or source.get("category") or "未分类")
        for img_data in all_images:
            gen_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            img=add_image(file_path=img_data["image_path"], file_name=img_data["file_name"], file_size=img_data.get("file_size",0), width=img_data.get("width",0), height=img_data.get("height",0), prompt=source.get("prompt", ""), negative_prompt=source.get("negative_prompt", ""), model_name=f"{prefix} · " + label, lora_name=source.get("lora_name", ""), category=category, steps=source.get("steps",20), cfg=source.get("cfg",7.0), seed=source.get("seed",-1), generated_at=gen_time); ids.append(img["id"])
        with _generation_tasks_lock:
            if task_id in _generation_tasks:
                t = _generation_tasks[task_id]
                all_ids = list(t.get("gallery_ids", [])) + ids
                t.update({"state":"completed", "completed":int(t.get("completed", 0)) + len(ids), "gallery_ids":all_ids, "message":f"{prefix}完成，已保存到图库", "finished_at":time.time()})
    except Exception as exc:
        with _generation_tasks_lock:
            if task_id in _generation_tasks: _generation_tasks[task_id].update({"state":"failed", "failed":1, "failures":[_exc_text(exc)], "message":f"{_UPSCALE_MODE_LABELS.get(mode, '放大')}失败"})

def _start_upscale(image_name: str, opts: dict, source: dict, image_path: Path | None = None) -> dict:
    if opts.get("profile") not in {x["id"] for x in _upscale_models()}:
        raise HTTPException(400, "未在当前 ComfyUI 中检测到此放大模型，请刷新后重新选择")
    task_id=uuid.uuid4().hex
    mode = opts.get("mode") or "fast"
    with _generation_tasks_lock:
        _generation_tasks[task_id]={"id":task_id,"kind":"upscale","state":"queued","total":1,"current":0,"completed":0,"failed":0,"gallery_ids":[],"failures":[],"message":f"{_UPSCALE_MODE_LABELS.get(mode, '放大')}任务已提交","created_at":time.time(),"cancel_requested":False,"upscale_mode":mode}
    threading.Thread(target=_run_upscale_task,args=(task_id,image_name,opts,source,image_path),daemon=True,name=f"image-upscale-{task_id[:8]}").start()
    return {"success":True,"task_id":task_id,"total":1,"mode":mode,"profile":next((x for x in _upscale_models() if x["id"] == opts.get("profile")), {"id": opts.get("profile"), "label": opts.get("profile")})}


# ── API 路由 ────────────────────────────────────────────────────────────────

def _list_comfy_models(api_url: str, kind: str) -> list[str]:
    try:
        r = requests.get(f"{api_url}/api/models/{kind}", timeout=20)
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
        return {"connected": False, "api_url": api_url, "error": _exc_text(e), "models": [], "all_models": [], "resources": {}, "loras": []}

@router.get("/workflow-state")
def api_workflow_state(model_name: str = "", workflow_preset_id: int = 0):
    """聚合右栏所需状态：模型、LoRA、绑定工作流、动态参数 schema、模型能力与分体资产。"""
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
    # 选定模型的能力与分体资产推荐
    selected_kind = ""
    for m in full_models:
        if m.get("name") == selected:
            selected_kind = m.get("kind", "")
            break
    selected_type = _classify_model(selected, selected_kind)
    capabilities = get_model_capabilities(selected_type)
    resources_map = status.get("resources", {}) or {}
    clip_options = clip_options_for_model(selected_type, status)
    vae_options = [str(x) for x in (resources_map.get("vae", []) or [])]
    # 用与生成阶段一致的 resolve_runtime_assets 做推荐（只取结果，忽略 warning/error）
    _probe, _pw, _pe = resolve_runtime_assets({"model_name": selected}, status)
    clip_name = _probe.get("clip_name", "") or ""
    vae_name = _probe.get("vae_name", "") or ""
    # 用实时能力覆盖 binding 里可能过期的 supports_*（历史绑定可能写死 1）
    if binding:
        binding["supports_lora"] = 1 if capabilities.get("supports_lora") else 0
        binding["supports_negative_prompt"] = 1 if capabilities.get("supports_negative_prompt") else 0
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
        "capabilities": capabilities,
        "clip_options": clip_options,
        "vae_options": vae_options,
        "clip_name": clip_name,
        "vae_name": vae_name,
        "upscale_recommendation": capabilities.get("upscale_recommendation", ""),
    }

@router.post("/workflows/bind")
def api_bind_workflow(payload: WorkflowBindRequest):
    # AI 可以提议，但后端必须校验，避免 Anima/Flux 被保存成 Illustrious。
    ok, corrected_type, message = validate_workflow_type_for_model(payload.model_name, payload.workflow_type, "", _model_architecture(payload.model_name, ""))
    if not ok:
        raise HTTPException(status_code=400, detail={"error": message, "corrected_type": corrected_type})
    schema = payload.params_schema or DEFAULT_PARAM_SCHEMA
    # 能力由模型类型权威决定，AI/前端传入的 supports_* 仅作兜底，不再写死。
    caps = get_model_capabilities(corrected_type)
    return upsert_binding(
        model_name=payload.model_name,
        workflow_id=payload.workflow_id,
        workflow_name=payload.workflow_name,
        workflow_type=corrected_type,
        params_schema=json.dumps(schema, ensure_ascii=False),
        supports_lora=1 if caps.get("supports_lora") else 0,
        supports_negative_prompt=1 if caps.get("supports_negative_prompt") else 0,
    )

@router.post("/workflows/one-click-setup")
def api_one_click_setup():
    """无模型参数的一键适配：扫描当前资源并绑定一个可用主模型。"""
    resources = discover_resources(_comfyui_url())
    models = [m for m in (resources.get("models_flat") or []) if m.get("kind") in {"checkpoints", "unet", "diffusion_models"}]
    if not models:
        raise HTTPException(400, "当前 ComfyUI 未检测到可适配的主模型")
    def rank(m):
        typ=m.get("model_type", "")
        return (0 if typ == "anima_qwen_unet" else 1 if typ in {"sdxl_illustrious","sdxl_realistic","sdxl"} else 2, str(m.get("name", "")).lower())
    selected=sorted(models, key=rank)[0]
    bound=api_auto_bind_workflow(selected["name"])
    return {"success":True,"message":"已完成一键自动适配："+selected["name"],"selected_model":selected["name"],"binding":bound,"summary":{"total_models":len(models),"loras":len(resources.get("resources",{}).get("loras",[])),"samplers":len(resources.get("samplers",[]))}}

@router.post("/workflows/auto-bind")
def api_auto_bind_workflow(model_name: str = Query(...)):
    """按模型类型自动创建最小可运行绑定，不再依赖 AI 猜测。"""
    api_url = _comfyui_url()
    resources = discover_resources(api_url)
    all_models = resources.get("models_flat", [])
    found = next((m for m in all_models if m.get("name") == model_name), None)
    kind = (found or {}).get("kind", "")
    model_type = _classify_model(model_name, kind)
    schema = build_param_schema(resources.get("samplers", []), resources.get("schedulers", []), model_type)
    caps = get_model_capabilities(model_type)
    return upsert_binding(
        model_name=model_name,
        workflow_id="auto:" + model_type,
        workflow_name="自动适配：" + model_type,
        workflow_type=model_type,
        params_schema=json.dumps(schema, ensure_ascii=False),
        supports_lora=1 if caps.get("supports_lora") else 0,
        supports_negative_prompt=1 if caps.get("supports_negative_prompt") else 0,
    )

@router.post("/workflows/self-check")
def api_workflow_self_check(model_name: str = Query("")):
    """自检：不跑图，只校验「当前绑定 → 插件生成的工作流」在现在的 ComfyUI 里能不能跑通。

    校验内容：节点是否存在（自定义节点缺失）、必填参数是否齐全、模型/CLIP/VAE/LoRA 文件是否可用。
    """
    name = model_name or ""
    if not name:
        for m in (list_bindings() or []):
            name = m.get("model_name") or ""
            if name: break
    if not name:
        return {"ok": False, "errors": ["还没有任何模型绑定，先点「自动适配」"], "warnings": []}
    binding = get_binding(name) or {}
    try:
        schema = json.loads(binding.get("params_schema") or "{}")
    except Exception:
        schema = {}
    params: dict[str, Any] = {"model_name": name, "prompt": "self check", "negative_prompt": "",
                              "category": "未分类", "generation_mode": "txt2img", "width": 512, "height": 512}
    for key, spec in (schema or {}).items():
        if isinstance(spec, dict) and "default" in spec and key not in params:
            params[key] = spec["default"]
    try:
        workflow = _build_workflow(params)
    except HTTPException as exc:
        detail = exc.detail
        if not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False)
        return {"ok": False, "errors": [detail], "warnings": [], "model_name": name}
    except Exception as exc:
        return {"ok": False, "errors": [_exc_text(exc)], "warnings": [], "model_name": name}
    try:
        info = requests.get(f"{_comfyui_url()}/object_info", timeout=20).json()
    except Exception as exc:
        return {"ok": False, "errors": [f"读取 ComfyUI 节点信息失败：{exc}"], "warnings": [], "model_name": name}
    errors = validate_workflow(workflow, info)
    return {"ok": not errors, "errors": errors, "warnings": [], "model_name": name,
            "model_type": params.get("_model_type", ""), "nodes": len(workflow),
            "message": "自检通过：工作流在当前 ComfyUI 里可以跑" if not errors else "自检发现问题"}

@router.delete("/workflows/bind/{model_name}")
def api_delete_workflow_binding(model_name: str):
    delete_binding(model_name)
    return {"success": True}

@router.post("/panel/context")
def api_set_panel_context(payload: PanelContext):
    """前端面板把当前状态（生成方式 / 模型 / 参考图 / denoise 等）同步过来，供 Agent 读取。"""
    data = payload.model_dump()
    data["updated_at"] = time.time()
    _panel_context.clear()
    _panel_context.update(data)
    return {"success": True, **_panel_context}

@router.get("/panel/context")
def api_get_panel_context():
    return {"success": True, **_panel_context}

# ── 前端面板状态（供 Agent 读取参考图等）─────────────────────────────────────
_panel_context: dict = {}


@router.post("/img2img/from-gallery/{image_id}")
def api_img2img_from_gallery(image_id: int):
    """以图生图的参考图准备：把图库原图上传到 ComfyUI input，返回 LoadImage 文件名与真实尺寸。"""
    img = get_image(image_id)
    if not img: raise HTTPException(404, "图片不存在")
    path = Path(img.get("file_path", ""))
    if not path.is_file(): raise HTTPException(404, "原图片文件不存在")
    name = _upload_input_to_comfy(path.read_bytes(), path.name)
    w, h = _image_actual_size(path)
    return {"success": True, "image_name": name, "width": w or int(img.get("width") or 0),
            "height": h or int(img.get("height") or 0), "image_id": image_id, "file_name": path.name,
            "local_path": str(path)}

@router.post("/img2img/upload")
async def api_img2img_upload(image: UploadFile = File(...)):
    """以图生图的参考图准备：本地上传。"""
    data = await image.read()
    if not data: raise HTTPException(400, "没有收到图片")
    if len(data) > 40 * 1024 * 1024: raise HTTPException(400, "参考图超过 40MB")
    name = _upload_input_to_comfy(data, image.filename or "image.png")
    w = h = 0
    try:
        import io as _io
        from PIL import Image as _PILImage
        with _PILImage.open(_io.BytesIO(data)) as _im:
            w, h = int(_im.width), int(_im.height)
    except Exception:
        pass
    # 本地留一份：AI 一句话以图生图时可以用绝对路径当 reference_image。
    local_path = ""
    try:
        ref_dir = IMAGES_DIR.parent / "ref_uploads"
        ref_dir.mkdir(parents=True, exist_ok=True)
        local_file = ref_dir / f"{uuid.uuid4().hex}_{Path(image.filename or 'image.png').name}"
        local_file.write_bytes(data)
        local_path = str(local_file)
    except Exception:
        pass
    return {"success": True, "image_name": name, "width": w, "height": h,
            "file_name": image.filename or "image.png", "local_path": local_path}

@router.post("/generate")
def api_generate(params: GenerateRequest):
    """同步接口：供 Agent 工具/兼容旧前端使用。UI 应调用 /generate/async。"""
    data = params.model_dump()
    _wf = _build_workflow(data)
    _pkg = build_portable_package(_wf, data, data.get("_model_type", ""))
    result = _run_comfyui(_wf, debug_prompt=params.prompt, debug_negative=params.negative_prompt, portable_package=_pkg)
    if result.get("success"):
        _store_generated_images(result, data)
    return result

@router.post("/generate/async")
def api_generate_async(params: GenerateRequest):
    """创建后台任务。为确保批量时可逐张显示，任务会依次提交单张生成。"""
    if not params.prompt.strip():
        raise HTTPException(400, "提示词不能为空")
    data = params.model_dump()
    total = max(1, min(int(data.get("batch_size", 1) or 1), 8))
    task_id = _create_generation_task(total, source="ui")
    threading.Thread(target=_run_generation_task, args=(task_id, data), daemon=True, name=f"image-gen-{task_id[:8]}").start()
    return {"success":True, "task_id":task_id, "total":total}

@router.get("/active-task")
def api_active_task():
    """返回当前正在进行的生图/放大任务，供插件面板显示进度（含 AI 发起的生图）。"""
    with _generation_tasks_lock:
        running = [dict(t) for t in _generation_tasks.values() if t.get("state") in ("queued", "running")]
    running.sort(key=lambda t: t.get("created_at", 0), reverse=True)
    return {"task": running[0] if running else None}

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

# ── 待放大区（upscale queue）──────────────────────────────────────────────
_upscale_queue: list[dict] = []
_upscale_queue_lock = threading.Lock()
_upscale_queue_id_counter = 0

@router.get("/upscale/queue")
def api_upscale_queue_list():
    with _upscale_queue_lock:
        return {"items": list(_upscale_queue), "total": len(_upscale_queue)}

@router.post("/upscale/queue/add/gallery/{image_id}")
def api_upscale_queue_add_gallery(image_id: int):
    img = get_image(image_id)
    if not img:
        raise HTTPException(404, "图片不存在")
    path = Path(img.get("file_path", ""))
    if not path.is_file():
        raise HTTPException(404, "原文件不存在")
    global _upscale_queue_id_counter
    with _upscale_queue_lock:
        _upscale_queue_id_counter += 1
        _upscale_queue.append({
            "id": _upscale_queue_id_counter,
            "image_id": image_id,
            "file_path": str(path),
            "file_name": path.name,
            "category": img.get("category", "未分类"),
            "source": "gallery",
            "profile": ""
        })
        return {"success": True, "item_id": _upscale_queue_id_counter, "total": len(_upscale_queue)}

@router.post("/upscale/queue/add/batch")
def api_upscale_queue_add_batch(payload: BatchImagesRequest):
    ids = list(dict.fromkeys(payload.image_ids))
    if not ids:
        raise HTTPException(400, "请至少选择一张图库图片")
    if len(ids) > 50:
        raise HTTPException(400, "单次最多添加 50 张")
    added = []
    global _upscale_queue_id_counter
    with _upscale_queue_lock:
        for image_id in ids:
            img = get_image(image_id)
            if not img: continue
            path = Path(img.get("file_path", ""))
            if not path.is_file(): continue
            _upscale_queue_id_counter += 1
            _upscale_queue.append({
                "id": _upscale_queue_id_counter,
                "image_id": image_id,
                "file_path": str(path),
                "file_name": path.name,
                "category": img.get("category", "未分类"),
                "source": "gallery",
                "profile": ""
            })
            added.append(_upscale_queue_id_counter)
    return {"success": True, "added": len(added), "items": added, "total": len(_upscale_queue)}

@router.post("/upscale/queue/add/upload")
async def api_upscale_queue_add_upload(image: UploadFile = File(...), category: str = Query("未分类")):
    data = await image.read()
    if not data: raise HTTPException(400, "没有收到图片")
    if len(data) > 80 * 1024 * 1024: raise HTTPException(400, "图片超过 80MB")
    safe_name = image.filename or "image.png"
    upload_dir = IMAGES_DIR / "_upload_queue"
    upload_dir.mkdir(parents=True, exist_ok=True)
    local_path = upload_dir / safe_name
    # 避免重名覆盖
    if local_path.exists():
        stem = local_path.stem
        i = 1
        while local_path.exists():
            local_path = upload_dir / f"{stem}_{i}{local_path.suffix}"
            i += 1
    local_path.write_bytes(data)
    global _upscale_queue_id_counter
    with _upscale_queue_lock:
        _upscale_queue_id_counter += 1
        _upscale_queue.append({
            "id": _upscale_queue_id_counter,
            "image_id": None,
            "file_path": str(local_path),
            "file_name": local_path.name,
            "category": category,
            "source": "upload",
            "profile": ""
        })
        return {"success": True, "item_id": _upscale_queue_id_counter, "file_name": local_path.name, "total": len(_upscale_queue)}

@router.post("/upscale/queue/remove/{item_id}")
def api_upscale_queue_remove(item_id: int):
    with _upscale_queue_lock:
        before = len(_upscale_queue)
        _upscale_queue[:] = [x for x in _upscale_queue if x["id"] != item_id]
        removed = before - len(_upscale_queue)
        return {"success": True, "removed": removed, "total": len(_upscale_queue)}

@router.post("/upscale/queue/clear")
def api_upscale_queue_clear():
    with _upscale_queue_lock:
        _upscale_queue.clear()
    return {"success": True, "total": 0}

def _opts_from_query(options: str, profile: str = "", category: str = "") -> dict:
    """把前端传来的 options(JSON 字符串) + 兼容的 profile/category 查询参数归一化成放大参数。"""
    try:
        data = json.loads(options or "{}")
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    return _upscale_options(
        mode=data.get("mode") or "fast",
        profile=data.get("profile") or profile,
        scale=data.get("scale") or 0,
        denoise=data.get("denoise"),
        steps=data.get("steps") or 15,
        cfg=data.get("cfg") or 0,
        sampler=data.get("sampler") or "",
        scheduler=data.get("scheduler") or "",
        seed_mode=data.get("seed_mode") or "source",
        tile_size=data.get("tile_size") or 512,
        seam_fix=data.get("seam_fix") or "None",
        category=data.get("category") or category or "未分类",
        prompt_mode=data.get("prompt_mode") or "auto",
        tile_padding=data.get("tile_padding", 64),
        mask_blur=data.get("mask_blur", 16),
        seam_fix_denoise=data.get("seam_fix_denoise", 0.5),
        sharpen_alpha=data.get("sharpen_alpha", 0.08),
        face_detail=bool(data.get("face_detail", False)),
        face_denoise=data.get("face_denoise", 0.35),
        iterations=data.get("iterations", 2),
        target_denoise=data.get("target_denoise", 0.0),
        step_mode=data.get("step_mode", "simple"),
    )


@router.post("/upscale/queue/start")
def api_upscale_queue_start(options: str = Query("{}"), profile: str = Query("")):
    opts = _opts_from_query(options, profile)
    if not opts["profile"]: raise HTTPException(400, "请选择放大模型")
    if opts["profile"] not in {x["id"] for x in _upscale_models()}:
        raise HTTPException(400, "当前放大模型不可用，请刷新后重新选择")
    with _upscale_queue_lock:
        items = list(_upscale_queue)
        _upscale_queue.clear()
    if not items: raise HTTPException(400, "待放大区是空的")
    task_id = uuid.uuid4().hex
    sources = []
    for item in items:
        path = Path(item["file_path"])
        if not path.is_file(): continue
        img_info = {"file_path": str(path), "file_name": item["file_name"]}
        if item.get("image_id"):
            img_data = get_image(item["image_id"])
            if img_data: img_info = img_data
        sources.append({
            "id": item.get("image_id") or item["id"],
            "path": item["file_path"],
            "name": item["file_name"],
            "category": item["category"],
            "source": img_info
        })
    if not sources: raise HTTPException(404, "待放大区图片均不存在或原文件已丢失")
    label = _UPSCALE_MODE_LABELS.get(opts["mode"], "放大")
    with _generation_tasks_lock:
        _generation_tasks[task_id] = {
            "id": task_id, "kind": "upscale", "state": "queued",
            "total": len(sources), "current": 0, "completed": 0, "failed": 0,
            "gallery_ids": [], "failures": [],
            "message": f"待放区 {len(sources)} 张图片已提交{label}",
            "created_at": time.time(), "cancel_requested": False, "upscale_mode": opts["mode"],
        }
    threading.Thread(target=_run_upscale_batch_task, args=(task_id, sources, opts),
                     daemon=True, name=f"upscale-queue-{task_id[:8]}").start()
    return {"success": True, "task_id": task_id, "total": len(sources), "mode": opts["mode"]}

@router.get("/upscale/profiles")
def api_upscale_profiles():
    items = _upscale_models()
    return {"items":items, "detected":len(items), "message":"已从当前 ComfyUI 的 UpscaleModelLoader 自动读取" if items else "未检测到放大模型"}

@router.post("/upscale/upload")
async def api_upscale_upload(image: UploadFile = File(...), options: str = Query("{}"), profile: str = Query(""), category: str = Query("未分类")):
    data=await image.read()
    if not data: raise HTTPException(400, "没有收到图片")
    if len(data) > 80 * 1024 * 1024: raise HTTPException(400, "图片超过 80MB，暂不支持")
    opts = _opts_from_query(options, profile, category)
    if not opts["profile"]: raise HTTPException(400, "请选择放大模型")
    image_name=_upload_input_to_comfy(data, image.filename or "image.png")
    return _start_upscale(image_name, opts, {}, None)

@router.post("/upscale/gallery/{image_id}")
def api_upscale_gallery_image(image_id: int, options: str = Query("{}"), profile: str = Query(""), category: str = Query("未分类")):
    img=get_image(image_id)
    if not img: raise HTTPException(404, "图片不存在")
    path=Path(img.get("file_path", ""))
    if not path.is_file(): raise HTTPException(404, "原图片文件不存在")
    opts = _opts_from_query(options, profile, category or img.get("category", "未分类"))
    if not opts["profile"]: raise HTTPException(400, "请选择放大模型")
    image_name=_upload_input_to_comfy(path.read_bytes(), path.name)
    return _start_upscale(image_name, opts, img, path)

@router.post("/upscale/gallery/batch")
def api_upscale_gallery_batch(payload: BatchImagesRequest, options: str = Query("{}"), profile: str = Query(""), category: str = Query("")):
    ids=list(dict.fromkeys(payload.image_ids))
    if not ids: raise HTTPException(400,"请至少选择一张图库图片")
    if len(ids)>50: raise HTTPException(400,"单批最多放大 50 张图片，请分批处理")
    opts = _opts_from_query(options, profile, category)
    if not opts["profile"]: raise HTTPException(400,"请选择放大模型")
    if opts["profile"] not in {x["id"] for x in _upscale_models()}: raise HTTPException(400,"当前放大模型不可用，请刷新后重新选择")
    task_id=uuid.uuid4().hex; sources=[]
    for image_id in ids:
        img=get_image(image_id)
        if not img: continue
        path=Path(img.get("file_path", ""))
        if not path.is_file(): continue
        sources.append({"id":image_id,"path":str(path),"name":path.name,"category":opts["category"] or img.get("category","未分类"),"source":img})
    if not sources: raise HTTPException(404,"所选图片均不存在或原文件已丢失")
    label = _UPSCALE_MODE_LABELS.get(opts["mode"], "放大")
    with _generation_tasks_lock:
        _generation_tasks[task_id]={"id":task_id,"kind":"upscale","state":"queued","total":len(sources),"current":0,"completed":0,"failed":0,"gallery_ids":[],"failures":[],"message":f"批量{label}任务已提交","created_at":time.time(),"cancel_requested":False,"upscale_mode":opts["mode"]}
    threading.Thread(target=_run_upscale_batch_task,args=(task_id,sources,opts),daemon=True,name=f"image-upscale-batch-{task_id[:8]}").start()
    return {"success":True,"task_id":task_id,"total":len(sources),"mode":opts["mode"]}

def _run_upscale_batch_task(task_id: str, sources: list[dict], opts: dict) -> None:
    label = _UPSCALE_MODE_LABELS.get(opts.get("mode"), "放大")
    for index,item in enumerate(sources):
        with _generation_tasks_lock:
            task=_generation_tasks.get(task_id)
            if not task: return
            if task.get("cancel_requested"):
                task.update({"state":"cancelled","message":"已停止，未处理的图片已保留在队列"}); return
            task.update({"state":"running","current":index+1,"message":f"正在{label}第 {index+1} / {len(sources)} 张：{item['name']}"})
        try:
            image_name=_upload_input_to_comfy(Path(item["path"]).read_bytes(), item["name"])
            _run_upscale_task(task_id, image_name, opts, item["source"], Path(item["path"]))
            # 单张函数会把任务标记为 completed；批量任务必须恢复为 running，避免前端在中途误显示完成。
            with _generation_tasks_lock:
                t=_generation_tasks.get(task_id,{})
                if t.get("state") != "failed":
                    t.update({"state":"running", "message":f"已完成 {t.get('completed',0)} / {len(sources)} 张，准备下一张"})
        except Exception as exc:
            with _generation_tasks_lock:
                t=_generation_tasks.get(task_id)
                if t: t["failed"]=(t.get("failed") or 0)+1; t.setdefault("failures",[]).append(f"{item['name']}: {_exc_text(exc)}")
    with _generation_tasks_lock:
        t=_generation_tasks.get(task_id)
        if t and t.get("state") not in {"cancelled"}: t.update({"state":"completed","message":f"批量{label}完成：成功 {t.get('completed',0)} 张，失败 {t.get('failed',0)} 张","finished_at":time.time()})

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

@router.get("/gallery/filters")
def api_gallery_filters(category: str = ""):
    return list_gallery_filters(category)

def _backfill_empty_metadata(limit: int = 300) -> int:
    """给图库里元数据为空的旧记录补读一次（支持 UNETLoader/GGUF/Lumina2/Flux 等）。"""
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT id, file_path FROM gallery_images "
            "WHERE deleted=0 AND (model_name IS NULL OR model_name='') AND (prompt IS NULL OR prompt='') "
            "LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    updated = 0
    for row in rows:
        path = Path(str(row["file_path"]))
        if not path.is_file():
            continue
        meta = _read_image_meta(path)
        if not (meta.get("prompt") or meta.get("model_name")):
            continue
        conn = _get_db()
        try:
            conn.execute(
                "UPDATE gallery_images SET prompt=?, negative_prompt=?, model_name=?, lora_name=?, steps=?, cfg=?, seed=? WHERE id=?",
                (meta.get("prompt", ""), meta.get("negative_prompt", ""), meta.get("model_name", ""), meta.get("lora_name", ""),
                 int(meta.get("steps") or 20), float(meta.get("cfg") or 7), int(meta.get("seed") or -1), row["id"]),
            )
            conn.commit()
            updated += 1
        finally:
            conn.close()
    return updated

def _run_gallery_scan_task(task_id: str) -> None:
    try:
        with _generation_tasks_lock:
            _generation_tasks[task_id].update({"state":"running", "message":"正在后台扫描图库，不会阻塞界面"})
        output_root = _output_dir()
        output_added = _scan_output_gallery()
        history_added = _scan_comfy_history_gallery()
        backfilled = _backfill_empty_metadata()
        with _generation_tasks_lock:
            _generation_tasks[task_id].update({"state":"completed", "added":output_added+history_added, "output_added":output_added, "history_added":history_added, "backfilled":backfilled, "output_dir":str(output_root), "message":f"扫描完成：输出目录新增 {output_added} 张、历史记录新增 {history_added} 张、补全元数据 {backfilled} 张", "finished_at":time.time()})
    except Exception as exc:
        with _generation_tasks_lock:
            if task_id in _generation_tasks: _generation_tasks[task_id].update({"state":"failed", "message":"图库扫描失败", "failures":[_exc_text(exc)]})

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

    # 兜底：若当前推导的 output 根并不包含这张图，尝试从图片路径里的 output 目录推导，
    # 避免因 ComfyUI 启动方式（相对 main.py 等）导致 output 根识别错误而无法分类。
    try:
        src_resolved.relative_to(output_root)
    except ValueError:
        guessed = _guess_output_root_for(src_resolved)
        if guessed is not None:
            output_root = guessed.resolve()

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

@router.post("/gallery/cleanup-missing")
def api_cleanup_missing_images():
    """清理源文件已不存在（被外部删除/产物库回收）的图库记录。"""
    count = cleanup_missing_images()
    return {"success": True, "cleaned": count, "message": f"已清理 {count} 条无源文件记录"}

@router.get("/images/batch")
def api_images_batch(ids: str = Query("")):
    """按 id 批量取图：结果弹窗 / 预览不再依赖图库当前分页与分类筛选。"""
    items = []
    for token in str(ids or "").replace("，", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            image_id = int(token)
        except Exception:
            continue
        img = get_image(image_id)
        if img:
            items.append(img)
    return {"items": items, "count": len(items)}

@router.get("/images/{image_id}")
def api_get_image(image_id: int):
    img = get_image(image_id)
    if not img:
        raise HTTPException(404, "图片不存在")
    return img

def _read_portable_from_image(path: Path) -> dict | None:
    """从图片的 PNG 文本块里读回自描述生成包（找不到返回 None）。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            raw = (im.info or {}).get(PORTABLE_PNG_KEY)
    except Exception:
        return None
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except Exception:
            return None
        # 兼容某些 ComfyUI 版本对 extra_pnginfo 值再做一次 json.dumps 的双重编码。
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                return None
        if isinstance(data, dict) and str(data.get("schema", "")).startswith("qwenpaw-image-gen.portable"):
            return data
    return None

def _portable_from_record(img: dict) -> dict:
    """旧图没有内嵌包时，用图库记录重建一个「部分可复现」包。"""
    deps = []
    model_name = str(img.get("model_name") or "")
    if model_name:
        deps.append({"file_name": model_name, "model_type": ""})
    for line in str(img.get("lora_name") or "").split(";"):
        name = line.strip().split(" (")[0].strip()
        if name:
            deps.append({"file_name": name, "model_type": "loras"})
    return {
        "schema": PORTABLE_SCHEMA,
        "created_at": str(img.get("generated_at") or ""),
        "generator": {"name": "QwenPaw 生图助手", "backend": "ComfyUI"},
        "model_type": "",
        "workflow_api": {},
        "generation": {
            "prompt": img.get("prompt", ""),
            "negative_prompt": img.get("negative_prompt", ""),
            "model_name": model_name,
            "lora_name": img.get("lora_name", ""),
            "steps": img.get("steps"),
            "cfg": img.get("cfg"),
            "seed": img.get("seed"),
            "width": img.get("width"),
            "height": img.get("height"),
        },
        "dependencies": deps,
        "_reconstructed": True,
    }

@router.get("/images/{image_id}/portable")
def api_image_portable(image_id: int):
    """读取某张图的可复现信息：内嵌自描述包 + 缺失依赖清单。"""
    img = get_image(image_id)
    if not img:
        raise HTTPException(404, "图片不存在")
    path = Path(str(img.get("file_path") or ""))
    package = _read_portable_from_image(path) if path.is_file() else None
    source = "embedded" if package else "record"
    if not package:
        package = _portable_from_record(img)
    missing: list[dict[str, str]] = []
    comfy_connected = True
    try:
        missing = find_missing_dependencies(package, discover_resources(_comfyui_url()))
    except Exception:
        comfy_connected = False
    return {
        "recognized": True,
        "source": source,
        "portable": package,
        "missing_dependencies": missing,
        "comfy_connected": comfy_connected,
        "can_reproduce_now": comfy_connected and not missing,
        "message": (
            "工作流与依赖齐全，可直接复现。" if comfy_connected and not missing else
            "请先补齐缺失的模型后再复现。" if missing else
            "ComfyUI 未连接，暂时无法校验依赖。"
        ),
    }

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
    # 防呆：分体式模型（z_image / anima / flux / gguf / diffusion_model）不能套用 checkpoint 预设。
    _ok, _corrected, _msg = validate_workflow_type_for_model(
        model_name, preset.get("model_type") or "preset", "", _model_architecture(model_name, ""))
    if not _ok:
        raise HTTPException(400, {"error": _msg, "corrected_type": _corrected, "preset_model_type": preset.get("model_type") or ""})
    _caps = get_model_capabilities(_classify_model(model_name, ""))
    return upsert_binding(
        model_name=model_name,
        workflow_id="preset:" + str(preset_id),
        workflow_name=preset.get("name") or "默认工作流",
        workflow_type=preset.get("model_type") or "preset",
        workflow_json=preset.get("workflow_json") or "{}",
        params_schema=preset.get("params_schema") or json.dumps(DEFAULT_PARAM_SCHEMA, ensure_ascii=False),
        supports_lora=1 if _caps.get("supports_lora") else 0,
        supports_negative_prompt=1 if _caps.get("supports_negative_prompt") else 0,
    )

# ── 提示词库（触发词 / 标签 / 模板）─────────────────────────────────────────

def _load_prompt_templates() -> list[dict]:
    try:
        data = json.loads(get_config("prompt_templates", "[]") or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _save_prompt_templates(items: list[dict]) -> None:
    set_config("prompt_templates", json.dumps(items, ensure_ascii=False))

def _load_tag_translations() -> dict[str, str]:
    """标签中文翻译表（英文 → 中文），由 agent 或用户补充并本地保存。"""
    try:
        data = json.loads(get_config("tag_translations", "{}") or "{}")
        return {str(k): str(v) for k, v in data.items() if str(k).strip() and str(v).strip()} if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_tag_translations(mapping: dict) -> dict[str, str]:
    store = _load_tag_translations()
    for key, value in (mapping or {}).items():
        k, v = str(key).strip(), str(value).strip()
        if k and v:
            store[k] = v[:60]
    set_config("tag_translations", json.dumps(store, ensure_ascii=False))
    return store

def _apply_tag_translations(groups: list[dict], translations: dict[str, str]) -> list[dict]:
    for group in groups or []:
        for tag in group.get("tags", []):
            zh = translations.get(tag.get("name"))
            if zh:
                tag["zh"] = zh
    return groups

@router.get("/prompt-library")
def api_prompt_library(model_name: str = "", loras: str = ""):
    """提示词库：当前模型 / LoRA 的触发词 + 内置标签 + 模板 + 推荐负面词。"""
    kind = ""
    comfy_connected = False
    try:
        resources = discover_resources(_comfyui_url())
        comfy_connected = True
        for m in (resources.get("models_flat") or []):
            if m.get("name") == model_name:
                kind = m.get("kind", "")
                break
    except Exception:
        pass
    translations = _load_tag_translations()
    model_info = _model_prompt_info(model_name, kind) if model_name else {
        "name": "", "found": False, "architecture": "", "base_model": "", "title": "", "trigger_words": []}
    lora_infos = [_model_prompt_info(x.strip(), "loras") for x in str(loras or "").split(",") if x.strip()]
    # 收集还没中文翻译的触发词，交给 agent 去翻译
    untranslated: list[str] = []
    for info in [model_info] + lora_infos:
        for word in info.get("trigger_words", []):
            if isinstance(word, dict) and not word.get("zh") and word.get("name") not in untranslated:
                untranslated.append(word["name"])
    return {
        "model": model_info,
        "loras": lora_infos,
        "tag_groups": _apply_tag_translations(prompt_library.list_tag_groups(), translations),
        "default_negative": prompt_library.DEFAULT_NEGATIVE,
        "prompt_guide": prompt_library.PROMPT_GUIDE,
        "templates": _load_prompt_templates(),
        "untranslated_tags": untranslated,
        "translation_count": len(translations),
        "comfy_connected": comfy_connected,
    }

@router.post("/tag-translations")
def api_save_tag_translations(payload: dict):
    """保存标签中文翻译（agent 或前端提交 {英文: 中文}）。"""
    mapping = payload.get("translations")
    if isinstance(mapping, str):
        try:
            mapping = json.loads(mapping)
        except Exception:
            mapping = None
    if not isinstance(mapping, dict) or not mapping:
        raise HTTPException(400, "translations 必须是 {英文: 中文} 的 JSON 对象")
    store = _save_tag_translations(mapping)
    return {"success": True, "saved": len(mapping), "total": len(store)}

@router.get("/prompt-library/search")
def api_prompt_search(q: str = "", limit: int = Query(60, ge=1, le=200)):
    return {"items": prompt_library.search_tags(q, limit)}

@router.get("/prompt-templates")
def api_list_prompt_templates():
    return {"items": _load_prompt_templates()}

@router.post("/prompt-templates")
def api_save_prompt_template(payload: dict):
    name = str(payload.get("name") or "").strip()
    prompt = str(payload.get("prompt") or "").strip()
    if not name or not prompt:
        raise HTTPException(400, "模板名称和提示词不能为空")
    tid = str(payload.get("id") or "").strip() or uuid.uuid4().hex[:8]
    entry = {
        "id": tid, "name": name[:60], "prompt": prompt[:2000],
        "negative_prompt": str(payload.get("negative_prompt") or "")[:1000],
        "model_name": str(payload.get("model_name") or "")[:200],
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    items = [x for x in _load_prompt_templates() if str(x.get("id")) != tid]
    items.insert(0, entry)
    _save_prompt_templates(items[:200])
    return {"success": True, "template": entry, "items": items[:200]}

@router.delete("/prompt-templates/{template_id}")
def api_delete_prompt_template(template_id: str):
    items = [x for x in _load_prompt_templates() if str(x.get("id")) != template_id]
    _save_prompt_templates(items)
    return {"success": True, "items": items}

@router.post("/repair")
def api_repair():
    """安全恢复插件本地配置：清理失效绑定和提示词草稿，不删除图库原图。"""
    conn=_get_db()
    try:
        conn.execute("DELETE FROM workflow_bindings")
        conn.execute("DELETE FROM config WHERE key IN ('comfyui_output_dir')")
        conn.commit()
    finally: conn.close()
    return {"success":True,"message":"已恢复插件配置；图库原图和图片记录未删除，请重新扫描/自动适配"}

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
        raw=(payload.value or "").strip()
        if not raw: raise HTTPException(400, "输出目录不能为空")
        path=Path(raw).expanduser()
        if not path.is_absolute(): raise HTTPException(400, "输出目录必须使用绝对路径")
        # 只允许真实目录或其已有父目录，避免插件被误用来创建任意深层路径。
        parent=path if path.exists() else path.parent
        if not parent.is_dir(): raise HTTPException(400, "输出目录或其父目录不存在")
        if any(part in {"Windows","System32","Program Files"} for part in path.parts): raise HTTPException(400, "不允许使用系统目录作为输出目录")
        set_config(key, str(path))
    else:
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

# ── API 生图（接入第三方图像 API）────────────────────────────────────────────
# 社区版：Provider（服务端点）与模型条目完全由用户在面板配置，仅存本地数据库；
# 本模块不含任何内置站点、API Key 或个人路径。

_api_image_tasks: dict[str, dict] = {}
_api_image_tasks_lock = threading.Lock()


class ApiProviderPayload(BaseModel):
    id: str = ""
    name: str = ""
    base_url: str = ""
    api_key: str = ""
    enabled: bool = True


class ApiModelPayload(BaseModel):
    id: str = ""
    provider_id: str = ""
    model: str = ""
    label: str = ""
    protocol: str = "auto"
    capabilities: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    default_negative: str = ""
    enabled: bool = True


class ApiGeneratePayload(BaseModel):
    model_id: str = ""
    prompt: str = ""
    negative_prompt: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    ref_image_ids: list[int] = Field(default_factory=list)
    ref_local_paths: list[str] = Field(default_factory=list)
    category: str = "未分类"


def _api_image_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, api_image.ApiImageError):
        return HTTPException(400, str(exc))
    return HTTPException(500, f"API 生图失败：{exc}")


@router.get("/api-image/config")
def api_image_get_config():
    """读取 API 生图配置：服务列表（Key 已脱敏）、模型条目、参数模板。"""
    return {
        "providers": api_image.list_providers(),
        "models": api_image.list_models(),
        "templates": api_image.param_templates(),
        "protocols": list(api_image.PROTOCOLS),
        "capabilities": list(api_image.CAPABILITIES),
    }


@router.post("/api-image/providers")
def api_image_save_provider(payload: ApiProviderPayload):
    try:
        return {"success": True, "provider": api_image.upsert_provider(payload.model_dump())}
    except Exception as exc:
        raise _api_image_http_error(exc) from exc


@router.delete("/api-image/providers/{provider_id}")
def api_image_remove_provider(provider_id: str):
    try:
        return api_image.delete_provider(provider_id)
    except Exception as exc:
        raise _api_image_http_error(exc) from exc


@router.post("/api-image/providers/{provider_id}/test")
def api_image_test_provider(provider_id: str):
    try:
        return api_image.test_provider(provider_id)
    except Exception as exc:
        raise _api_image_http_error(exc) from exc


@router.post("/api-image/models")
def api_image_save_model(payload: ApiModelPayload):
    try:
        return {"success": True, "model": api_image.upsert_model(payload.model_dump())}
    except Exception as exc:
        raise _api_image_http_error(exc) from exc


@router.delete("/api-image/models/{model_id}")
def api_image_remove_model(model_id: str):
    try:
        return api_image.delete_model(model_id)
    except Exception as exc:
        raise _api_image_http_error(exc) from exc


def _run_api_image_task(task_id: str, payload: ApiGeneratePayload, refs: list[Path]) -> None:
    """后台执行 API 生图；同步协议可能耗时 1~3 分钟，不能阻塞请求。"""
    def _progress(message: str) -> None:
        with _api_image_tasks_lock:
            task = _api_image_tasks.get(task_id)
            if task:
                task["message"] = message

    def _stopped() -> bool:
        with _api_image_tasks_lock:
            task = _api_image_tasks.get(task_id)
            return bool(task and task.get("cancel_requested"))

    with _api_image_tasks_lock:
        _api_image_tasks[task_id]["state"] = "running"
        _api_image_tasks[task_id]["message"] = "提交中"
    try:
        result = api_image.generate(
            model_id=payload.model_id,
            prompt=payload.prompt,
            negative=payload.negative_prompt,
            params=payload.params,
            ref_images=refs,
            category=payload.category or "未分类",
            progress=_progress,
            should_stop=_stopped,
        )
        gallery_ids = [r.get("id") for r in result.get("images", [])]
        with _api_image_tasks_lock:
            task = _api_image_tasks[task_id]
            task.update({"state": "completed", "done": len(gallery_ids),
                         "gallery_ids": gallery_ids,
                         "message": f"完成 {len(gallery_ids)} 张"})
    except Exception as exc:
        with _api_image_tasks_lock:
            task = _api_image_tasks[task_id]
            task.update({"state": "failed", "message": str(exc), "failures": [str(exc)]})


@router.post("/api-image/generate")
def api_image_generate(payload: ApiGeneratePayload):
    """创建后台任务执行 API 生图。"""
    if not (payload.prompt or "").strip():
        raise HTTPException(400, "提示词不能为空")
    if not payload.model_id:
        raise HTTPException(400, "请先选择模型")
    refs: list[Path] = []
    for image_id in payload.ref_image_ids:
        row = get_image(int(image_id))
        if not row:
            raise HTTPException(404, f"图库里没有 ID={image_id} 的图片")
        refs.append(Path(row.get("file_path", "")))
    for raw in payload.ref_local_paths:
        if str(raw or "").strip():
            refs.append(Path(str(raw).strip()))

    task_id = "api_" + uuid.uuid4().hex[:12]
    with _api_image_tasks_lock:
        _api_image_tasks[task_id] = {
            "id": task_id, "state": "queued", "message": "排队中",
            "created_at": time.time(), "total": 1, "done": 0,
            "gallery_ids": [], "cancel_requested": False, "failures": [],
            "model_id": payload.model_id, "source": "api-image",
        }
        # 轻量清理：只保留最近 40 个任务，避免长期运行内存增长。
        if len(_api_image_tasks) > 40:
            for tid in sorted(_api_image_tasks, key=lambda k: _api_image_tasks[k].get("created_at", 0))[:-40]:
                _api_image_tasks.pop(tid, None)
    threading.Thread(target=_run_api_image_task, args=(task_id, payload, refs),
                     daemon=True, name=f"api-image-{task_id[:8]}").start()
    return {"success": True, "task_id": task_id}


@router.get("/api-image/tasks/{task_id}")
def api_image_task(task_id: str):
    with _api_image_tasks_lock:
        task = _api_image_tasks.get(task_id)
        if not task:
            raise HTTPException(404, "任务不存在或已过期")
        return dict(task)


@router.post("/api-image/tasks/{task_id}/stop")
def api_image_stop_task(task_id: str):
    with _api_image_tasks_lock:
        task = _api_image_tasks.get(task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        task["cancel_requested"] = True
        task["message"] = "已请求取消"
        return {"success": True, "message": task["message"]}

# ── Agent 工具 ──────────────────────────────────────────────────────────────

def _resolve_reference_image(reference: str) -> tuple[str, int, int]:
    """把「图库 ID 或本地绝对路径」的参考图上传到 ComfyUI input，返回 (image_name, width, height)。"""
    ref = str(reference or "").strip().strip('"').strip("'")
    if not ref:
        raise HTTPException(400, "没有提供参考图（reference_image 可以是图库图片 ID，也可以是本地图片路径）")
    if ref.isdigit():
        img = get_image(int(ref))
        if not img:
            raise HTTPException(404, f"图库里没有 ID={ref} 的图片")
        path = Path(img.get("file_path", ""))
    else:
        path = Path(ref)
    if not path or not path.is_file():
        raise HTTPException(404, f"参考图文件不存在：{ref}")
    name = _upload_input_to_comfy(path.read_bytes(), path.name)
    w, h = _image_actual_size(path)
    return name, w, h


def image_gen_generate(prompt: str = "", negative_prompt: str = "", model_name: str = "example-model.safetensors", steps: int = 20, cfg: float = 7.0, width: int = 1024, height: int = 1024, lora_name: str = "", lora_strength: float = 0.6, reference_image: str = "", denoise: float = 0.6) -> dict:
    """使用 ComfyUI 生成图片（文生图 / 以图生图）。返回生成结果和图片在插件图库中的 ID。

    参数说明：
    - prompt: 正向提示词（描述你想生成的画面）
    - negative_prompt: 负向提示词（描述你不想要的元素）
    - model_name: 模型名称（如 example-model.safetensors）
    - steps: 采样步数（默认20，越高越精细但越慢）
    - cfg: 提示词相关性（默认7.0，越高越贴合提示词）
    - width/height: 图片尺寸
    - lora_name: LoRA 模型名称（可选）
    - lora_strength: LoRA 强度（默认0.6）
    - reference_image: 以图生图的参考图。填图库图片 ID（如 "188"）或本地图片绝对路径；留空 = 普通文生图。
      用户说「照着这张图 / 用这张图改 / 以图生图」时，从图库列表或用户给的路径里取 ID 填进来。
    - denoise: 以图生图的重绘幅度（默认0.6；0.4 轻微改动，0.8 大改），只有传了 reference_image 才生效。
      传了参考图且 width/height 保持默认 1024x1024 时，会自动沿用参考图的尺寸。
    """
    try:
        source_image = ""
        if str(reference_image or "").strip():
            source_image, ref_w, ref_h = _resolve_reference_image(reference_image)
            if (int(width), int(height)) == (1024, 1024):
                width = ref_w or width
                height = ref_h or height
        req = GenerateRequest(
            prompt=prompt, negative_prompt=negative_prompt,
            model_name=model_name, steps=steps, cfg=cfg,
            width=width, height=height, lora_name=lora_name, lora_strength=lora_strength,
            generation_mode="img2img" if source_image else "txt2img",
            source_image=source_image, denoise=float(denoise or 0.6),
        )
        data = req.model_dump()
        total = max(1, min(int(data.get("batch_size", 1) or 1), 8))
        # 关键：Agent 发起的生图也登记成可见任务，插件面板通过 /active-task 显示进度。
        task_id = _create_generation_task(total, message="AI 生图任务已提交，等待 ComfyUI 开始处理", source="agent")
        _run_generation_task(task_id, data)  # 同步执行：Agent 等结果，但面板可实时看到进度
        with _generation_tasks_lock:
            task = dict(_generation_tasks.get(task_id) or {})
        gallery_ids = list(task.get("gallery_ids") or [])
        if task.get("completed") and gallery_ids:
            gid = gallery_ids[0]
            img = get_image(gid) or {}
            return {
                "success": True,
                "message": f"图片生成成功！已在插件图库中（ID: {gid}）" + ("（以图生图）" if source_image else ""),
                "gallery_id": gid,
                "image_path": img.get("file_path", ""),
                "seed": img.get("seed"),
                "task_id": task_id,
                "generation_mode": "img2img" if source_image else "txt2img",
            }
        return {"success": False, "error": (task.get("failures") or ["生图失败"])[0], "task_id": task_id}
    except HTTPException as e:
        return {"success": False, "error": e.detail if isinstance(e.detail, str) else json.dumps(e.detail, ensure_ascii=False)}
    except Exception as e:
        return {"success": False, "error": _exc_text(e)}

def image_gen_list_images(query: str = "", category: str = "", limit: int = 20) -> dict:
    """列出插件图库中的图片，用于「用某张图做参考图 / 复刻某张图」时找到图片 ID。

    参数：
    - query: 关键词（匹配提示词 / 文件名 / 备注 / 模型 / LoRA），留空 = 全部
    - category: 按分类筛选，留空 = 全部
    - limit: 返回条数（默认 20，最多 50）

    返回 items: [{id, category, model_name, width, height, created_at, prompt}]，按生成时间倒序。
    拿到 id 后传给 image_gen_generate 的 reference_image 就能做以图生图。
    """
    try:
        rows, total = list_images_page(query=query, category=category, sort="newest", limit=max(1, min(int(limit or 20), 50)))
        items = [{
            "id": r.get("id"), "category": r.get("category"), "model_name": r.get("model_name"),
            "width": r.get("width"), "height": r.get("height"), "created_at": r.get("created_at"),
            "prompt": (r.get("prompt") or "")[:200],
        } for r in rows]
        return {"success": True, "total": total, "count": len(items), "items": items,
                "hint": "把想要的图片 id 作为 reference_image 传给 image_gen_generate 即可做以图生图"}
    except Exception as e:
        return {"success": False, "error": _exc_text(e)}

def image_gen_panel_context() -> dict:
    """读取用户在插件面板里的当前设置：生成方式、主模型、LoRA、参考图、重绘幅度、尺寸、分类、提示词。

    用户说「按我面板里的设置」「就用面板里选的那张参考图」「以图生图，参考图我已经选好了」时先调用它，
    拿到 reference_image 后原样传给 image_gen_generate（不要自己猜路径）。
    generation_mode=img2img 且 reference_image 非空时，说明用户已经选好参考图，直接用它即可。
    """
    try:
        ctx = dict(_panel_context)
        ctx["success"] = True
        ctx["hint"] = ("面板当前是以图生图，参考图 reference_image=" + str(ctx.get("reference_image") or "（空）")
                       + "，denoise=" + str(ctx.get("denoise"))) if str(ctx.get("generation_mode")) == "img2img" else "面板当前是文生图"
        return ctx
    except Exception as e:
        return {"success": False, "error": _exc_text(e)}

def image_gen_set_rating(image_id: int, rating: int) -> dict:
    """为生成的图片设置星级评分（0-5星）"""
    try:
        return update_rating(image_id, rating)
    except Exception as e:
        return {"success": False, "error": _exc_text(e)}

def image_gen_check_status() -> dict:
    """检查 ComfyUI 连接状态和可用模型列表"""
    try:
        return api_status()
    except Exception as e:
        return {"connected": False, "error": _exc_text(e)}

def image_gen_prompt_helper(idea: str = "", model_name: str = "", lora_names: str = "") -> dict:
    """帮 agent 把一句话扩写成高质量生图提示词：返回模型/LoRA 触发词、推荐标签与提示词骨架。

    参数：
    - idea: 用户的一句自然语言想法（如「雨夜街头撑伞的银发少女」）
    - model_name: 目标主模型文件名（留空则用当前绑定/第一个可用模型）
    - lora_names: 逗号分隔的 LoRA 文件名（可选）

    返回 model.trigger_words（模型触发词）、loras[].trigger_words（LoRA 触发词）、
    suggested_tags（推荐标签）、default_negative（推荐负面词）、prompt_guide（结构建议）。
    拿到这些后，agent 应自己组织成英文提示词，再调用 image_gen_generate 生成。
    """
    try:
        kind = ""
        if not model_name:
            try:
                resources = discover_resources(_comfyui_url())
                for m in (resources.get("models_flat") or []):
                    if m.get("kind") in {"checkpoints", "unet", "diffusion_models"}:
                        model_name = m.get("name", "")
                        kind = m.get("kind", "")
                        break
            except Exception:
                pass
        elif model_name:
            try:
                resources = discover_resources(_comfyui_url())
                for m in (resources.get("models_flat") or []):
                    if m.get("name") == model_name:
                        kind = m.get("kind", "")
                        break
            except Exception:
                pass
        model_info = _model_prompt_info(model_name, kind) if model_name else {}
        lora_infos = [_model_prompt_info(x.strip(), "loras") for x in str(lora_names or "").split(",") if x.strip()]
        suggested = {}
        for group in prompt_library.TAG_GROUPS:
            if group["id"] in {"quality", "lighting", "composition", "style", "negative"}:
                suggested[group["id"]] = [t["name"] for t in group["tags"][:10]]
        untranslated: list[str] = []
        for info in [model_info] + lora_infos:
            for word in info.get("trigger_words", []):
                if isinstance(word, dict) and not word.get("zh") and word.get("name") not in untranslated:
                    untranslated.append(word["name"])
        return {
            "success": True,
            "idea": idea,
            "model": model_info,
            "loras": lora_infos,
            "suggested_tags": suggested,
            "untranslated_tags": untranslated,
            "default_negative": prompt_library.DEFAULT_NEGATIVE,
            "prompt_guide": prompt_library.PROMPT_GUIDE,
            "next_step": (
                "用触发词和标签把用户的一句话扩写成英文提示词，然后调用 image_gen_generate（带 model_name / prompt / negative_prompt）。"
                "如果 untranslated_tags 非空，可顺手把它们翻译成简短中文并调用 image_gen_save_tag_translations 保存，方便用户在面板里看懂。"
            ),
        }
    except Exception as exc:
        return {"success": False, "error": _exc_text(exc)}

def image_gen_save_tag_translations(translations: str) -> dict:
    """保存英文生图标签的中文翻译，供提示词库显示「英文 (中文)」。

    参数 translations：JSON 对象字符串，如 {"masterpiece": "杰作", "silver hair": "银发"}。
    翻译要简短（2-6 字），只保存英文标签对应的中文；同名会被覆盖。
    新装的模型 / LoRA 触发词通常没有内置中文，agent 可先翻译再调用本工具保存。
    """
    try:
        mapping = json.loads(translations) if isinstance(translations, str) else translations
        if not isinstance(mapping, dict) or not mapping:
            return {"success": False, "error": "translations 必须是 {英文: 中文} 的 JSON 对象"}
        store = _save_tag_translations(mapping)
        return {"success": True, "saved": len(mapping), "total": len(store)}
    except Exception as exc:
        return {"success": False, "error": _exc_text(exc)}

def image_gen_register_workflow_preset(name: str, workflow_json: str = "{}", description: str = "",
                                        model_type: str = "custom", params_schema: str = "",
                                        sort_order: int = 1000) -> dict:
    """注册一个自定义 ComfyUI 工作流预设。workflow_json 是符合 ComfyUI API 格式的工作流 JSON 字符串。"""
    try:
        import json as _json
        wf = _json.loads(workflow_json) if isinstance(workflow_json, str) else workflow_json
        ps = _json.loads(params_schema) if isinstance(params_schema, str) and params_schema else DEFAULT_PARAM_SCHEMA.copy()
        result = save_workflow_preset(
            name=name, description=description, model_type=model_type,
            workflow_json=_json.dumps(wf, ensure_ascii=False),
            params_schema=_json.dumps(ps, ensure_ascii=False),
            sort_order=sort_order
        )
        return {"success": True, "preset_id": result.get("id"), "preset": result}
    except Exception as e:
        return {"success": False, "error": _exc_text(e)}

def image_gen_apply_workflow_preset(preset_id: int, model_name: str) -> dict:
    """将一个已注册的工作流预设绑定到指定模型，使其在生图面板中生效。"""
    try:
        preset = get_preset(preset_id)
        if not preset:
            return {"success": False, "error": "预设不存在"}
        # 防呆：分体式模型不能套用 checkpoint 预设。
        _ok, _corrected, _msg = validate_workflow_type_for_model(
            model_name, preset.get("model_type") or "custom", "", _model_architecture(model_name, ""))
        if not _ok:
            return {"success": False, "error": _msg, "corrected_type": _corrected}
        _caps = get_model_capabilities(_classify_model(model_name, ""))
        result = upsert_binding(
            model_name=model_name,
            workflow_id="preset:" + str(preset_id),
            workflow_name=preset.get("name") or "自定义工作流",
            workflow_type=preset.get("model_type") or "custom",
            workflow_json=preset.get("workflow_json") or "{}",
            params_schema=preset.get("params_schema") or "",
            supports_lora=1 if _caps.get("supports_lora") else 0,
            supports_negative_prompt=1 if _caps.get("supports_negative_prompt") else 0,
        )
        return {"success": True, "binding": result}
    except Exception as e:
        return {"success": False, "error": _exc_text(e)}

# ── 插件注册 ────────────────────────────────────────────────────────────────

def _cleanup_upload_caches(max_age_days: int = 7) -> None:
    """清理参考图 / 上传队列缓存里超过指定天数的文件，避免长期累积占空间。

    只处理缓存目录，绝不动图库里的正式图片。
    """
    cutoff = time.time() - max_age_days * 86400
    for folder in (IMAGES_DIR.parent / "ref_uploads", IMAGES_DIR / "_upload_queue"):
        if not folder.is_dir():
            continue
        for item in folder.iterdir():
            if not item.is_file():
                continue
            try:
                if item.stat().st_mtime < cutoff:
                    item.unlink()
            except OSError:
                pass


class ImageGenPlugin:
    def register(self, api: PluginApi) -> None:
        api.register_http_router(router, prefix="/image-gen", tags=["image-gen"])

        # 启动时清理过期的参考图/上传缓存，避免长期累积占空间（失败不影响启动）。
        try:
            _cleanup_upload_caches()
        except Exception:
            pass
        
        # Register agent tools
        # 注意：tool_type 必须取 QwenPaw 治理系统认可的值
        # （file / network / shell / internal），否则工具会被拒绝注册、Agent 侧看不到。
        # 本插件工具都是本地操作（本地 ComfyUI / 插件图库 / 模型元数据），用 "internal"。
        api.register_tool(
            tool_name="image_gen_generate",
            tool_func=image_gen_generate,
            description=(
                "使用 ComfyUI 生图（文生图 / 以图生图）。填写提示词和参数，自动生成图片并保存到插件图库。"
                "以图生图：传 reference_image（图库图片 ID 或本地图片绝对路径）+ denoise（重绘幅度，默认 0.6）；"
                "用户说「照着这张图改 / 用这张图做参考」时，先用 image_gen_list_images 找到图片 ID 再调用。"
            ),
            icon="🎨",
            enabled=True,
            tool_type="internal"
        )
        api.register_tool(
            tool_name="image_gen_list_images",
            tool_func=image_gen_list_images,
            description=(
                "列出插件图库中的图片（ID / 分类 / 模型 / 尺寸 / 提示词片段）。"
                "用户说「用刚才那张图 / 照着图库里的 XX 改 / 以图生图」时先调用它找到图片 ID，"
                "再把 id 作为 reference_image 传给 image_gen_generate。"
            ),
            icon="🖼️",
            enabled=True,
            tool_type="internal"
        )
        api.register_tool(
            tool_name="image_gen_panel_context",
            tool_func=image_gen_panel_context,
            description=(
                "读取插件面板当前设置：生成方式（文生图/以图生图）、主模型、LoRA、参考图 reference_image、"
                "重绘幅度 denoise、尺寸、分类。用户说「按面板里的设置」「参考图我已经选好了」时先调用它，"
                "再把返回的 reference_image 传给 image_gen_generate。"
            ),
            icon="🧭",
            enabled=True,
            tool_type="internal"
        )
        api.register_tool(
            tool_name="image_gen_set_rating",
            tool_func=image_gen_set_rating,
            description="给插件图库中的图片评分（0-5星）",
            icon="⭐",
            enabled=True,
            tool_type="internal"
        )
        api.register_tool(
            tool_name="image_gen_check_status",
            tool_func=image_gen_check_status,
            description="检查 ComfyUI 的运行状态和可用模型",
            icon="🔌",
            enabled=True,
            tool_type="internal"
        )
        api.register_tool(
            tool_name="image_gen_prompt_helper",
            tool_func=image_gen_prompt_helper,
            description=(
                "把用户一句话想法扩写成高质量生图提示词前的资料查询：返回当前主模型与 LoRA 的触发词、"
                "推荐标签、推荐负面词和提示词结构建议。用户说「画个XX / 帮我生成XX」时先调用它，"
                "再用返回的触发词/标签组织英文提示词，最后调用 image_gen_generate 生成。"
            ),
            icon="💡",
            enabled=True,
            tool_type="internal"
        )
        api.register_tool(
            tool_name="image_gen_save_tag_translations",
            tool_func=image_gen_save_tag_translations,
            description=(
                "保存英文生图标签的中文翻译，让插件提示词库显示成「英文 (中文)」。"
                "参数 translations 是 JSON 对象字符串，如 {\"masterpiece\": \"杰作\"}。"
                "当 image_gen_prompt_helper 返回的 untranslated_tags 非空，或用户让你翻译标签时调用。"
            ),
            icon="🌐",
            enabled=True,
            tool_type="internal"
        )
        api.register_tool(
            tool_name="image_gen_register_workflow_preset",
            tool_func=image_gen_register_workflow_preset,
            description="注册自定义 ComfyUI 工作流预设。AI 可以用来创建新的工作流模板，参数 workflow_json 是符合 ComfyUI API 格式的工作流 JSON 字符串（必须是字典的 JSON）。",
            icon="⚙️",
            enabled=True,
            tool_type="internal"
        )
        api.register_tool(
            tool_name="image_gen_apply_workflow_preset",
            tool_func=image_gen_apply_workflow_preset,
            description="将已注册的工作流预设绑定到指定模型，使该模型使用自定义工作流生成图片。",
            icon="🔌",
            enabled=True,
            tool_type="internal"
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
