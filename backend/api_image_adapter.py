# -*- coding: utf-8 -*-
"""API 生图适配层 —— 接入任意 OpenAI 兼容 / 异步媒体协议的图像生成服务。

社区版设计约束（重要）：
- 本模块**不含任何内置站点、API Key、个人路径或厂商绑定**。
- Provider（服务端点）与模型条目全部由用户在插件面板中配置，仅存于本地插件数据库。
- 参数形态用「模板」描述，用户可自由增删改；模板只描述参数结构，不绑定具体厂商。

支持两套协议：
- ``openai``：OpenAI 兼容同步协议
  - 文生图 ``POST {base}/images/generations``
  - 图生图 ``POST {base}/images/edits``（multipart 上传本地参考图）
- ``media``：异步媒体协议
  - 创建任务 ``POST {base}/media/generate``
  - 轮询状态 ``GET  {base}/media/status?task_id=...``

统一入口 ``generate()`` 会按模型条目的协议设置调用，并在 ``auto`` 模式下自动回退。
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from image_store import IMAGES_DIR, add_image, get_config, set_config

# ── 配置 ────────────────────────────────────────────────────────────────────

CONFIG_KEY = "api_image_config"
DEFAULT_TIMEOUT = 300          # 同步协议：出图常见 1~3 分钟，读超时给足
DEFAULT_POLL_INTERVAL = 4      # 异步协议：轮询间隔（秒）
DEFAULT_POLL_TIMEOUT = 900     # 异步协议：最长等待

PROTOCOLS = ("auto", "openai", "media")
CAPABILITIES = (
    "txt2img",      # 文生图
    "img2img",      # 图生图 / 图像编辑
    "multi_ref",    # 多张参考图
    "transparent",  # 透明背景
    "group",        # 组图（一次出多张）
)


class ApiImageError(Exception):
    """API 生图的可读错误。message 面向用户，可直接展示。"""


# ── 参数形态模板（通用，不绑定厂商）─────────────────────────────────────────
#
# 每个模板描述「一类 API 的参数结构」，用户在面板里一键添加后再填自己的模型名。
# 这样社区用户接任何中转站都能快速起步，而不是被写死的模型清单限制。

PARAM_TEMPLATES: list[dict[str, Any]] = [
    {
        "key": "basic_txt2img",
        "label": "基础文生图",
        "description": "只有尺寸和数量，最通用的同步接口形态。",
        "protocol": "openai",
        "capabilities": ["txt2img"],
        "params": {
            "size": {"type": "enum", "label": "尺寸", "default": "1024x1024",
                     "options": ["1024x1024", "1536x1024", "1024x1536"]},
            "n": {"type": "int", "label": "张数", "default": 1, "min": 1, "max": 4},
        },
    },
    {
        "key": "quality_txt2img",
        "label": "文生图 + 质量档",
        "description": "在基础形态上增加画质档位。",
        "protocol": "openai",
        "capabilities": ["txt2img"],
        "params": {
            "size": {"type": "enum", "label": "尺寸", "default": "1024x1024",
                     "options": ["1024x1024", "1536x1024", "1024x1536"]},
            "quality": {"type": "enum", "label": "画质", "default": "auto",
                        "options": ["auto", "low", "medium", "high"]},
            "n": {"type": "int", "label": "张数", "default": 1, "min": 1, "max": 4},
        },
    },
    {
        "key": "ratio_resolution",
        "label": "文生图 + 比例 + 分辨率",
        "description": "比例与清晰度分开控制的异步形态。",
        "protocol": "media",
        "capabilities": ["txt2img"],
        "params": {
            "aspect_ratio": {"type": "enum", "label": "比例", "default": "1:1",
                             "options": ["auto", "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"]},
            "resolution": {"type": "enum", "label": "分辨率", "default": "1K",
                           "options": ["auto", "1K", "2K", "4K"]},
            "n": {"type": "int", "label": "张数", "default": 1, "min": 1, "max": 4},
        },
    },
    {
        "key": "transparent_bg",
        "label": "文生图 + 透明底",
        "description": "支持输出透明背景 PNG。",
        "protocol": "media",
        "capabilities": ["txt2img", "transparent"],
        "params": {
            "aspect_ratio": {"type": "enum", "label": "比例", "default": "1:1",
                             "options": ["auto", "1:1", "16:9", "9:16", "4:3", "3:4"]},
            "resolution": {"type": "enum", "label": "分辨率", "default": "1K",
                           "options": ["auto", "1K", "2K", "4K"]},
            "quality": {"type": "enum", "label": "画质", "default": "auto",
                        "options": ["auto", "low", "medium", "high"]},
            "background": {"type": "enum", "label": "背景", "default": "opaque",
                           "options": ["opaque", "transparent", "auto"]},
            "n": {"type": "int", "label": "张数", "default": 1, "min": 1, "max": 4},
        },
    },
    {
        "key": "multi_reference",
        "label": "图生图 + 多参考图",
        "description": "可上传多张参考图做融合 / 编辑。",
        "protocol": "media",
        "capabilities": ["txt2img", "img2img", "multi_ref"],
        "params": {
            "aspect_ratio": {"type": "enum", "label": "比例", "default": "1:1",
                             "options": ["auto", "1:1", "16:9", "9:16", "4:3", "3:4"]},
            "resolution": {"type": "enum", "label": "分辨率", "default": "1K",
                           "options": ["auto", "1K", "2K", "4K"]},
            "max_refs": {"type": "int", "label": "参考图上限", "default": 4, "min": 1, "max": 16},
            "n": {"type": "int", "label": "张数", "default": 1, "min": 1, "max": 4},
        },
    },
    {
        "key": "openai_edit",
        "label": "图生图编辑（OpenAI 协议）",
        "description": "同步接口上传参考图做编辑。",
        "protocol": "openai",
        "capabilities": ["img2img", "multi_ref"],
        "params": {
            "size": {"type": "enum", "label": "尺寸", "default": "1024x1024",
                     "options": ["auto", "1024x1024", "1536x1024", "1024x1536"]},
            "n": {"type": "int", "label": "张数", "default": 1, "min": 1, "max": 4},
        },
    },
]


def param_templates() -> list[dict[str, Any]]:
    """返回内置参数形态模板（只读副本）。"""
    return json.loads(json.dumps(PARAM_TEMPLATES, ensure_ascii=False))


# ── 配置读写 ────────────────────────────────────────────────────────────────

def _default_config() -> dict[str, Any]:
    return {"providers": [], "models": []}


def load_config() -> dict[str, Any]:
    raw = get_config(CONFIG_KEY, "")
    if not raw:
        return _default_config()
    try:
        cfg = json.loads(raw)
    except (ValueError, TypeError):
        return _default_config()
    if not isinstance(cfg, dict):
        return _default_config()
    cfg.setdefault("providers", [])
    cfg.setdefault("models", [])
    if not isinstance(cfg["providers"], list):
        cfg["providers"] = []
    if not isinstance(cfg["models"], list):
        cfg["models"] = []
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    set_config(CONFIG_KEY, json.dumps(cfg, ensure_ascii=False))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _clean(value: Any, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def _normalize_base_url(raw: str) -> str:
    """把用户填的地址规范成不带尾部斜杠的 base URL。"""
    url = _clean(raw, 500).rstrip("/")
    if not url:
        raise ApiImageError("服务地址不能为空")
    if not re.match(r"^https?://", url, re.I):
        raise ApiImageError("服务地址必须以 http:// 或 https:// 开头")
    return url


# ── Provider CRUD ───────────────────────────────────────────────────────────

def _mask_provider(item: dict[str, Any]) -> dict[str, Any]:
    """对外脱敏：不回传明文 Key，只给「是否已配置」与尾部片段供辨认。"""
    out = dict(item)
    key = out.get("api_key") or ""
    out["api_key_set"] = bool(key)
    out["api_key_hint"] = ("…" + key[-4:]) if len(key) >= 8 else ""
    out.pop("api_key", None)
    return out


def list_providers(include_secret: bool = False) -> list[dict[str, Any]]:
    out = []
    for p in load_config()["providers"]:
        out.append(dict(p) if include_secret else _mask_provider(p))
    return out


def upsert_provider(payload: dict[str, Any]) -> dict[str, Any]:
    cfg = load_config()
    pid = _clean(payload.get("id"), 64)
    name = _clean(payload.get("name"), 80) or "未命名服务"
    base_url = _normalize_base_url(payload.get("base_url", ""))
    api_key = _clean(payload.get("api_key"), 400)
    enabled = bool(payload.get("enabled", True))

    if pid:
        target = next((p for p in cfg["providers"] if p.get("id") == pid), None)
        if target is None:
            raise ApiImageError("找不到要更新的服务")
        target.update({"name": name, "base_url": base_url, "enabled": enabled})
        if api_key:                      # 留空表示不修改已保存的 Key
            target["api_key"] = api_key
        saved = target
    else:
        saved = {"id": _new_id("p"), "name": name, "base_url": base_url,
                 "api_key": api_key, "enabled": enabled}
        cfg["providers"].append(saved)

    save_config(cfg)
    return _mask_provider(saved)


def delete_provider(pid: str) -> dict[str, Any]:
    cfg = load_config()
    before = len(cfg["providers"])
    cfg["providers"] = [p for p in cfg["providers"] if p.get("id") != pid]
    if len(cfg["providers"]) == before:
        raise ApiImageError("找不到要删除的服务")
    # 同时移除挂在它下面的模型条目，避免留下孤儿配置。
    removed_models = [m for m in cfg["models"] if m.get("provider_id") == pid]
    cfg["models"] = [m for m in cfg["models"] if m.get("provider_id") != pid]
    save_config(cfg)
    return {"deleted": True, "removed_models": len(removed_models)}


def _find_provider(pid: str) -> dict[str, Any]:
    for p in load_config()["providers"]:
        if p.get("id") == pid:
            return p
    raise ApiImageError("找不到该服务，请先在设置里添加")


# ── 模型条目 CRUD ───────────────────────────────────────────────────────────

def list_models() -> list[dict[str, Any]]:
    cfg = load_config()
    providers = {p.get("id"): p.get("name") for p in cfg["providers"]}
    out = []
    for m in cfg["models"]:
        item = dict(m)
        item["provider_name"] = providers.get(m.get("provider_id"), "(已删除的服务)")
        out.append(item)
    return out


def upsert_model(payload: dict[str, Any]) -> dict[str, Any]:
    cfg = load_config()
    mid = _clean(payload.get("id"), 64)
    provider_id = _clean(payload.get("provider_id"), 64)
    if not any(p.get("id") == provider_id for p in cfg["providers"]):
        raise ApiImageError("请选择一个有效的服务")

    model_name = _clean(payload.get("model"), 200)
    if not model_name:
        raise ApiImageError("模型名不能为空（填服务方文档里的 model 值）")

    protocol = _clean(payload.get("protocol"), 20) or "auto"
    if protocol not in PROTOCOLS:
        raise ApiImageError(f"协议只能是 {'/'.join(PROTOCOLS)}")

    caps = [c for c in (payload.get("capabilities") or []) if c in CAPABILITIES]
    params = payload.get("params")
    if not isinstance(params, dict):
        params = {}

    saved_payload = {
        "provider_id": provider_id,
        "model": model_name,
        "label": _clean(payload.get("label"), 80) or model_name,
        "protocol": protocol,
        "capabilities": caps,
        "params": params,
        "default_negative": _clean(payload.get("default_negative"), 2000),
        "enabled": bool(payload.get("enabled", True)),
    }

    if mid:
        target = next((m for m in cfg["models"] if m.get("id") == mid), None)
        if target is None:
            raise ApiImageError("找不到要更新的模型")
        target.update(saved_payload)
        saved = target
    else:
        saved = {"id": _new_id("m"), **saved_payload}
        cfg["models"].append(saved)

    save_config(cfg)
    return dict(saved)


def delete_model(mid: str) -> dict[str, Any]:
    cfg = load_config()
    before = len(cfg["models"])
    cfg["models"] = [m for m in cfg["models"] if m.get("id") != mid]
    if len(cfg["models"]) == before:
        raise ApiImageError("找不到要删除的模型")
    save_config(cfg)
    return {"deleted": True}


def _find_model(mid: str) -> dict[str, Any]:
    for m in load_config()["models"]:
        if m.get("id") == mid:
            if not m.get("enabled", True):
                raise ApiImageError("该模型已停用")
            return m
    raise ApiImageError("找不到该模型，请先在设置里添加")


# ── HTTP 工具 ───────────────────────────────────────────────────────────────

def _auth_headers(provider: dict[str, Any]) -> dict[str, str]:
    headers = {"User-Agent": "QwenPaw-ImageGen/1.0"}
    key = provider.get("api_key") or ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")[:600]
    except Exception:
        return ""


def _normalize_error(status: int, body: str) -> str:
    """把服务方的错误体整理成一句人能看懂的中文提示。"""
    detail = ""
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict):
                detail = str(err.get("message") or err.get("code") or "")
            elif isinstance(err, str):
                detail = err
            if not detail:
                detail = str(parsed.get("message") or parsed.get("msg") or "")
    except (ValueError, TypeError):
        detail = body[:300]

    if status == 401:
        return "鉴权失败：请检查 API Key 是否填写正确（401）" + (f"｜服务方提示：{detail}" if detail else "")
    if status == 403:
        return "无权访问该模型：请确认账号是否有此模型权限（403）" + (f"｜{detail}" if detail else "")
    if status == 404:
        return "接口地址不存在：请检查服务地址是否填写正确（404）" + (f"｜{detail}" if detail else "")
    if status == 429:
        return "请求过于频繁或额度已用尽（429）" + (f"｜{detail}" if detail else "")
    if status == 400:
        return "请求被拒绝：参数或模型名可能不被该服务支持（400）" + (f"｜{detail}" if detail else "")
    if 500 <= status < 600:
        return f"服务方暂时不可用（{status}）" + (f"｜{detail}" if detail else "")
    return f"请求失败（{status}）" + (f"｜{detail}" if detail else "")


def _normalize_network_error(reason: Any) -> str:
    """把底层网络异常翻译成用户能看懂、且能据此行动的提示。"""
    text = str(reason)
    low = text.lower()
    if "ssl" in low or "certificate" in low or "tls" in low:
        return ("HTTPS 连接被中断（SSL 握手失败）。常见原因：系统代理/VPN 未正确转发该域名、"
                "网络抖动，或服务方临时不可用。可先关闭代理直连重试，或稍后再试；"
                f"（原始信息：{text[:160]}）")
    if "timed out" in low or "timeout" in low:
        return f"连接超时：网络不通或服务方响应过慢，请检查网络后重试（原始信息：{text[:120]}）"
    if "getaddrinfo" in low or "name or service not known" in low or "nodename" in low:
        return "域名解析失败：请检查服务地址是否填写正确（原始信息：" + text[:120] + "）"
    if "refused" in low:
        return "连接被拒绝：请确认服务地址与端口是否正确（原始信息：" + text[:120] + "）"
    if "reset" in low or "eof" in low or "disconnected" in low:
        return ("连接被对端中断：可能是网络不稳定或服务方限流，建议稍后重试"
                f"（原始信息：{text[:120]}）")
    return f"无法连接服务地址：{text[:200]}"


def _post_json(url: str, provider: dict[str, Any], payload: dict[str, Any],
               timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    headers = _auth_headers(provider)
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise ApiImageError(_normalize_error(exc.code, _read_error_body(exc))) from exc
    except urllib.error.URLError as exc:
        raise ApiImageError(_normalize_network_error(exc.reason)) from exc
    except TimeoutError as exc:
        raise ApiImageError("请求超时：服务方长时间未返回，请稍后重试或改用异步协议") from exc


def _get_json(url: str, provider: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=_auth_headers(provider))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise ApiImageError(_normalize_error(exc.code, _read_error_body(exc))) from exc
    except urllib.error.URLError as exc:
        raise ApiImageError(f"无法连接服务地址：{exc.reason}") from exc


def _multipart(fields: dict[str, str], files: list[tuple[str, Path]]) -> tuple[bytes, str]:
    """构造 multipart/form-data 请求体。"""
    boundary = "----QwenPawImageGen" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    for name, path in files:
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n".encode("utf-8")
        )
        parts.append(path.read_bytes())
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _post_multipart(url: str, provider: dict[str, Any], fields: dict[str, str],
                    files: list[tuple[str, Path]], timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    body, ctype = _multipart(fields, files)
    headers = _auth_headers(provider)
    headers["Content-Type"] = ctype
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise ApiImageError(_normalize_error(exc.code, _read_error_body(exc))) from exc
    except urllib.error.URLError as exc:
        raise ApiImageError(f"无法连接服务地址：{exc.reason}") from exc
    except TimeoutError as exc:
        raise ApiImageError("请求超时：服务方长时间未返回，请稍后重试") from exc


def _download(url: str, provider: dict[str, Any], timeout: int = 180) -> bytes:
    req = urllib.request.Request(url, headers=_auth_headers(provider))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise ApiImageError(f"结果图片下载失败（{exc.code}）") from exc
    except urllib.error.URLError as exc:
        raise ApiImageError(f"结果图片下载失败：{exc.reason}") from exc


def _decode_b64(raw: str) -> bytes:
    text = raw.strip()
    if text.startswith("data:"):
        text = text.split(",", 1)[-1]
    try:
        return base64.b64decode(text)
    except Exception as exc:
        raise ApiImageError("服务返回的图片数据无法解码") from exc


def _extract_images(body: dict[str, Any], provider: Optional[dict[str, Any]] = None) -> list[bytes]:
    """从同步协议响应里取出图片字节。"""
    data = body.get("data")
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data:
        raise ApiImageError("服务未返回图片数据，请检查模型名与参数是否匹配")
    images: list[bytes] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("b64_json"):
            images.append(_decode_b64(str(item["b64_json"])))
        elif item.get("url"):
            images.append(_download(str(item["url"]), provider or {}))
    if not images:
        raise ApiImageError("服务返回的结构里没有可用的图片字段（既无 b64_json 也无 url）")
    return images


# ── 参数整理 ────────────────────────────────────────────────────────────────

def _clean_params(params: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    """按模型条目的 params schema 过滤并补默认值。"""
    schema = model.get("params") if isinstance(model.get("params"), dict) else {}
    out: dict[str, Any] = {}
    for key, spec in schema.items():
        if not isinstance(spec, dict):
            continue
        value = params.get(key, spec.get("default"))
        if value is None or value == "":
            continue
        kind = spec.get("type")
        try:
            if kind == "int":
                value = int(value)
                lo, hi = spec.get("min"), spec.get("max")
                if lo is not None:
                    value = max(int(lo), value)
                if hi is not None:
                    value = min(int(hi), value)
            elif kind == "float":
                value = float(value)
        except (TypeError, ValueError):
            continue
        out[key] = value
    return out


def _positive_prompt(prompt: str, negative: str) -> str:
    """部分服务不支持独立负向字段时，把负向合并进正向提示词。"""
    p = (prompt or "").strip()
    n = (negative or "").strip()
    if not n:
        return p
    return f"{p}\n\nAvoid the following: {n}" if p else f"Avoid the following: {n}"


# ── 协议 A：OpenAI 兼容同步 ─────────────────────────────────────────────────

def generate_openai(provider: dict[str, Any], model: dict[str, Any], prompt: str,
                    negative: str, params: dict[str, Any],
                    ref_images: list[Path]) -> list[bytes]:
    base = (provider.get("base_url") or "").rstrip("/")
    model_name = model.get("model") or ""
    cleaned = _clean_params(params, model)
    n = int(cleaned.pop("n", 1) or 1)
    n = max(1, min(n, 8))
    cleaned.pop("max_refs", None)
    background = cleaned.pop("background", None)
    if background == "transparent":
        # 同步协议没有统一的透明底字段，用提示词兜底表达。
        prompt = (prompt or "") + "\n\nTransparent background, PNG output."

    if ref_images:
        fields: dict[str, str] = {"model": model_name, "prompt": _positive_prompt(prompt, negative)}
        for key, value in cleaned.items():
            if key == "size" and value == "auto":
                continue
            fields[key] = str(value)
        if n > 1:
            fields["n"] = str(n)
        files = [("image[]", p) for p in ref_images[:16]]
        body = _post_multipart(f"{base}/images/edits", provider, fields, files)
    else:
        payload: dict[str, Any] = {"model": model_name,
                                   "prompt": _positive_prompt(prompt, negative)}
        for key, value in cleaned.items():
            if key == "size" and value == "auto":
                continue
            payload[key] = value
        if n > 1:
            payload["n"] = n
        body = _post_json(f"{base}/images/generations", provider, payload)

    return _extract_images(body, provider)


# ── 协议 B：异步媒体 ────────────────────────────────────────────────────────

def generate_media(provider: dict[str, Any], model: dict[str, Any], prompt: str,
                   negative: str, params: dict[str, Any], ref_images: list[Path],
                   progress: Optional[Callable[[str], None]] = None,
                   should_stop: Optional[Callable[[], bool]] = None) -> list[bytes]:
    base = (provider.get("base_url") or "").rstrip("/")
    model_name = model.get("model") or ""
    cleaned = _clean_params(params, model)
    n = int(cleaned.pop("n", 1) or 1)
    n = max(1, min(n, 8))
    cleaned.pop("max_refs", None)

    media_params: dict[str, Any] = {k: v for k, v in cleaned.items()}
    if ref_images:
        media_params["images"] = [_file_to_data_uri(p) for p in ref_images[:16]]

    payload: dict[str, Any] = {
        "model": model_name,
        "prompt": _positive_prompt(prompt, negative),
        "params": media_params,
    }

    def _run_one() -> bytes:
        created = _post_json(f"{base}/media/generate", provider, payload, timeout=120)
        data = created.get("data") if isinstance(created.get("data"), dict) else created
        task_id = data.get("task_id") if isinstance(data, dict) else None
        if not task_id:
            # 有些实现会直接同步返回结果。
            try:
                return _extract_images(created, provider)[0]
            except ApiImageError:
                raise ApiImageError("服务未返回任务号，无法查询进度")

        deadline = time.time() + DEFAULT_POLL_TIMEOUT
        last_state = ""
        while time.time() < deadline:
            if should_stop and should_stop():
                raise ApiImageError("已取消")
            time.sleep(DEFAULT_POLL_INTERVAL)
            status = _get_json(
                f"{base}/media/status?task_id={urllib.parse.quote(str(task_id))}", provider)
            state = str(status.get("state") or "")
            if progress and state != last_state:
                last_state = state
                pct = status.get("progress")
                progress(f"{state} {pct or ''}".strip())
            if status.get("is_final"):
                if state == "success" and status.get("result_url"):
                    return _download(str(status["result_url"]), provider)
                detail = status.get("error") or status.get("status") or "任务失败"
                raise ApiImageError(f"生成失败：{detail}")
        raise ApiImageError("等待超时：任务长时间未完成，请稍后在服务方后台查看")

    if n <= 1:
        return [_run_one()]

    # 多数异步服务一次只出一张，需要多张时并发多路请求。
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(n, 4)) as pool:
        futures = [pool.submit(_run_one) for _ in range(n)]
        out: list[bytes] = []
        errors: list[str] = []
        for fut in futures:
            try:
                out.append(fut.result())
            except Exception as exc:
                errors.append(str(exc))
    if not out:
        raise ApiImageError(errors[0] if errors else "生成失败")
    return out


def _file_to_data_uri(path: Path, max_side: int = 2048) -> str:
    """把参考图转成 data URI。

    过大的图先等比压缩再编码，避免请求体过大被服务方拒绝或拖慢上传；
    压缩失败时回退为原始字节，保证功能不中断。
    """
    try:
        from PIL import Image
        import io as _io
        with Image.open(path) as im:
            width, height = im.size
            longest = max(width, height)
            if longest > max_side:
                scale = max_side / float(longest)
                im = im.resize((max(1, int(width * scale)), max(1, int(height * scale))),
                               Image.LANCZOS)
            buf = _io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=88, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        ctype = mimetypes.guess_type(path.name)[0] or "image/png"
        return f"data:{ctype};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


# ── 统一入口 ────────────────────────────────────────────────────────────────

def _is_unsupported_endpoint(message: str) -> bool:
    """判断服务方是否明确表示「该模型不走这个端点」。"""
    return "不支持" in message and ("端点" in message or "endpoint" in message.lower())


def _should_fallback_to_media(message: str) -> bool:
    """判断「自动」模式下是否应改用异步协议重试。

    两种情形值得回退：
    1. 服务方明确说该模型不走同步端点；
    2. 同步协议是长连接（一次请求挂到出图，可能 1~3 分钟），容易被中间网络
       设备掐断或服务方提前断开，此时改用「短请求 + 轮询」的异步协议往往能成功。
       鉴权类、参数类错误不回退，避免无意义的重复请求。
    """
    if _is_unsupported_endpoint(message):
        return True
    if any(k in message for k in ("鉴权", "无权", "参数", "请求被拒绝", "域名解析", "连接被拒绝")):
        return False
    return any(k in message for k in ("SSL", "HTTPS 连接被中断", "连接超时", "连接被对端中断", "无法连接"))


def test_provider(pid: str) -> dict[str, Any]:
    """测试服务连通性：尝试读取模型列表，读不到也不视为失败。"""
    provider = _find_provider(pid)
    base = (provider.get("base_url") or "").rstrip("/")
    try:
        body = _get_json(f"{base}/models", provider, timeout=30)
        items = body.get("data") if isinstance(body, dict) else None
        count = len(items) if isinstance(items, list) else 0
        return {"ok": True, "message": f"连接成功，服务方返回 {count} 个模型" if count else "连接成功",
                "model_count": count}
    except ApiImageError as exc:
        return {"ok": False, "message": str(exc), "model_count": 0}


def generate(model_id: str, prompt: str, negative: str = "",
             params: Optional[dict[str, Any]] = None,
             ref_images: Optional[list[Path]] = None,
             category: str = "未分类",
             project: str = "",
             progress: Optional[Callable[[str], None]] = None,
             should_stop: Optional[Callable[[], bool]] = None) -> dict[str, Any]:
    """按模型条目执行一次 API 生图，并把结果写入图库。"""
    model = _find_model(model_id)
    provider = _find_provider(model.get("provider_id") or "")
    if not provider.get("enabled", True):
        raise ApiImageError("该服务已停用")

    text = (prompt or "").strip()
    if not text:
        raise ApiImageError("请先填写提示词")

    params = params if isinstance(params, dict) else {}
    refs = [Path(p) for p in (ref_images or [])]
    for p in refs:
        if not p.is_file():
            raise ApiImageError(f"参考图不存在：{p.name}")

    protocol = model.get("protocol") or "auto"
    images: list[bytes] = []

    if protocol in ("openai", "auto"):
        try:
            if progress:
                progress("提交请求" if protocol == "openai" else "尝试同步协议")
            images = generate_openai(provider, model, text, negative, params, refs)
        except ApiImageError as exc:
            if protocol == "openai" or not _should_fallback_to_media(str(exc)):
                raise
            if progress:
                progress("同步协议未成功，改用异步协议重试")
            images = []

    if not images:
        if progress and protocol == "media":
            progress("提交任务")
        images = generate_media(provider, model, text, negative, params, refs,
                                progress=progress, should_stop=should_stop)

    if not images:
        raise ApiImageError("服务未返回任何图片")

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    saved: list[dict[str, Any]] = []
    from PIL import Image
    import io as _io

    for idx, blob in enumerate(images, 1):
        name = f"api_{stamp}_{uuid.uuid4().hex[:6]}_{idx}.png"
        path = IMAGES_DIR / name
        width = height = 0
        try:
            with Image.open(_io.BytesIO(blob)) as im:
                width, height = im.size
                if im.format and im.format.upper() != "PNG":
                    im.convert("RGB").save(path, "PNG")
                else:
                    path.write_bytes(blob)
        except Exception:
            path.write_bytes(blob)

        row = add_image(
            file_path=str(path), file_name=name, file_size=path.stat().st_size,
            width=width, height=height,
            prompt=text, negative_prompt=negative,
            model_name=model.get("label") or model.get("model") or "",
            lora_name="", workflow_id=0, steps=0, cfg=0.0, seed=-1,
            category=category or "未分类",
        )
        saved.append(row)

    return {
        "success": True,
        "count": len(saved),
        "images": saved,
        "model_label": model.get("label") or model.get("model"),
        "provider_name": provider.get("name"),
        "project": project,
    }
