# -*- coding: utf-8 -*-
"""生图助手 — 图片与配置存储层（SQLite）"""
from __future__ import annotations
import json, os, time, shutil, sqlite3
from pathlib import Path
from typing import Any, Optional

DB_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DB_DIR / "image_gen.db"
IMAGES_DIR = DB_DIR / "images"

def _get_db() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    """初始化数据库表结构"""
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS workflow_presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            model_type TEXT NOT NULL DEFAULT 'sdxl',
            workflow_json TEXT NOT NULL DEFAULT '{}',
            params_schema TEXT NOT NULL DEFAULT '{}',
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS gallery_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_size INTEGER DEFAULT 0,
            width INTEGER DEFAULT 0,
            height INTEGER DEFAULT 0,
            prompt TEXT DEFAULT '',
            negative_prompt TEXT DEFAULT '',
            model_name TEXT DEFAULT '',
            lora_name TEXT DEFAULT '',
            workflow_id INTEGER DEFAULT 0,
            steps INTEGER DEFAULT 20,
            cfg REAL DEFAULT 7.0,
            seed INTEGER DEFAULT -1,
            rating INTEGER DEFAULT 0,
            notes TEXT DEFAULT '',
            deleted INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            generated_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS generation_recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            prompt TEXT DEFAULT '',
            negative_prompt TEXT DEFAULT '',
            model_name TEXT DEFAULT '',
            lora_name TEXT DEFAULT '',
            workflow_id INTEGER DEFAULT 0,
            steps INTEGER DEFAULT 20,
            cfg REAL DEFAULT 7.0,
            seed INTEGER DEFAULT -1,
            width INTEGER DEFAULT 1024,
            height INTEGER DEFAULT 1024,
            recipe_json TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS workflow_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_name TEXT NOT NULL UNIQUE,
            workflow_id TEXT NOT NULL DEFAULT '',
            workflow_name TEXT NOT NULL DEFAULT '',
            workflow_type TEXT NOT NULL DEFAULT 'sdxl_basic',
            workflow_json TEXT NOT NULL DEFAULT '{}',
            params_schema TEXT NOT NULL DEFAULT '{}',
            supports_lora INTEGER DEFAULT 1,
            supports_negative_prompt INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    conn.commit()
    # 默认配置
    defaults = {
        "comfyui_api_url": "http://127.0.0.1:8188",
        "comfyui_api_url_alt": "http://127.0.0.1:8189",
        "output_dir": str(IMAGES_DIR),
    }
    for k, v in defaults.items():
        conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()

# ── 工作流预设 ──────────────────────────────────────────────────────────────

def list_presets(model_type: str = "") -> list[dict]:
    conn = _get_db()
    if model_type:
        rows = conn.execute("SELECT * FROM workflow_presets WHERE model_type=? ORDER BY sort_order", (model_type,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM workflow_presets ORDER BY sort_order").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_preset(preset_id: int) -> Optional[dict]:
    conn = _get_db()
    row = conn.execute("SELECT * FROM workflow_presets WHERE id=?", (preset_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def add_preset(name: str, description: str, model_type: str, workflow_json: str, params_schema: str, sort_order: int = 0) -> dict:
    conn = _get_db()
    cur = conn.execute(
        "INSERT INTO workflow_presets (name,description,model_type,workflow_json,params_schema,sort_order) VALUES (?,?,?,?,?,?)",
        (name, description, model_type, workflow_json, params_schema, sort_order)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM workflow_presets WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    return dict(row)

def save_workflow_preset(name: str, description: str = "", model_type: str = "custom", workflow_json: str = "{}", params_schema: str = "", sort_order: int = 1000) -> dict:
    """保存一个用户工作流预设。名称相同则覆盖，避免重复堆积。"""
    if not params_schema:
        params_schema = json.dumps(DEFAULT_PARAM_SCHEMA, ensure_ascii=False)
    conn = _get_db()
    row = conn.execute("SELECT id FROM workflow_presets WHERE name=?", (name,)).fetchone()
    if row:
        conn.execute(
            "UPDATE workflow_presets SET description=?, model_type=?, workflow_json=?, params_schema=?, sort_order=?, updated_at=datetime('now','localtime') WHERE id=?",
            (description, model_type, workflow_json, params_schema, sort_order, row["id"])
        )
        preset_id = row["id"]
    else:
        cur = conn.execute(
            "INSERT INTO workflow_presets (name,description,model_type,workflow_json,params_schema,sort_order) VALUES (?,?,?,?,?,?)",
            (name, description, model_type, workflow_json, params_schema, sort_order)
        )
        preset_id = cur.lastrowid
    conn.commit()
    out = conn.execute("SELECT * FROM workflow_presets WHERE id=?", (preset_id,)).fetchone()
    conn.close()
    return dict(out)

# ── 模型工作流绑定 ───────────────────────────────────────────────────────────

DEFAULT_PARAM_SCHEMA = {
    "steps": {"type": "number", "label": "采样步数", "min": 1, "max": 80, "step": 1, "default": 20},
    "cfg": {"type": "number", "label": "CFG", "min": 1, "max": 20, "step": 0.5, "default": 7},
    "sampler_name": {"type": "select", "label": "采样器", "default": "euler", "options": ["euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_3m_sde", "dpmpp_sde", "ddim"]},
    "scheduler": {"type": "select", "label": "调度器", "default": "normal", "options": ["normal", "karras", "exponential", "sgm_uniform", "simple", "ddim_uniform"]},
    "width": {"type": "number", "label": "宽度", "min": 256, "max": 2048, "step": 64, "default": 1024},
    "height": {"type": "number", "label": "高度", "min": 256, "max": 2048, "step": 64, "default": 1024},
    "seed": {"type": "number", "label": "Seed", "min": -1, "max": 2147483647, "step": 1, "default": -1},
    "batch_size": {"type": "number", "label": "批量", "min": 1, "max": 8, "step": 1, "default": 1},
    "denoise": {"type": "number", "label": "重绘幅度", "min": 0, "max": 1, "step": 0.05, "default": 1}
}

DEFAULT_WORKFLOW_PRESETS = [
    ("SDXL 通用文生图", "适合 JuggernautXL、RealVisXL、DreamShaperXL、普通 SDXL checkpoint。", "sdxl", {**DEFAULT_PARAM_SCHEMA, "steps": {**DEFAULT_PARAM_SCHEMA["steps"], "default": 24}, "cfg": {**DEFAULT_PARAM_SCHEMA["cfg"], "default": 6.5}, "sampler_name": {**DEFAULT_PARAM_SCHEMA["sampler_name"], "default": "dpmpp_2m"}, "scheduler": {**DEFAULT_PARAM_SCHEMA["scheduler"], "default": "karras"}}, 10),
    ("Illustrious / WAI 二次元", "适合 WAI Illustrious、NoobAI、Animagine 等 SDXL 二次元模型。", "illustrious", {**DEFAULT_PARAM_SCHEMA, "steps": {**DEFAULT_PARAM_SCHEMA["steps"], "default": 28}, "cfg": {**DEFAULT_PARAM_SCHEMA["cfg"], "default": 5.5}, "sampler_name": {**DEFAULT_PARAM_SCHEMA["sampler_name"], "default": "euler"}, "scheduler": {**DEFAULT_PARAM_SCHEMA["scheduler"], "default": "normal"}}, 20),
    ("Pony / AutismMix 通用", "适合 Pony Diffusion XL 体系、AutismMix、相关 furry / 角色模型。", "pony", {**DEFAULT_PARAM_SCHEMA, "steps": {**DEFAULT_PARAM_SCHEMA["steps"], "default": 30}, "cfg": {**DEFAULT_PARAM_SCHEMA["cfg"], "default": 6.0}, "sampler_name": {**DEFAULT_PARAM_SCHEMA["sampler_name"], "default": "dpmpp_2m"}, "scheduler": {**DEFAULT_PARAM_SCHEMA["scheduler"], "default": "karras"}}, 30),
    ("SD1.5 通用文生图", "适合 Anything、Counterfeit、MajicMix、老版二次元/写实 SD1.5 checkpoint。", "sd15", {**DEFAULT_PARAM_SCHEMA, "width": {**DEFAULT_PARAM_SCHEMA["width"], "default": 512, "max": 1024}, "height": {**DEFAULT_PARAM_SCHEMA["height"], "default": 768, "max": 1024}, "steps": {**DEFAULT_PARAM_SCHEMA["steps"], "default": 25}, "cfg": {**DEFAULT_PARAM_SCHEMA["cfg"], "default": 7.0}, "sampler_name": {**DEFAULT_PARAM_SCHEMA["sampler_name"], "default": "dpmpp_2m"}, "scheduler": {**DEFAULT_PARAM_SCHEMA["scheduler"], "default": "karras"}}, 40),
    ("快速预览 / 低步数", "用于快速看构图和提示词方向，质量不是最终版。", "preview", {**DEFAULT_PARAM_SCHEMA, "steps": {**DEFAULT_PARAM_SCHEMA["steps"], "default": 12}, "cfg": {**DEFAULT_PARAM_SCHEMA["cfg"], "default": 5.0}, "sampler_name": {**DEFAULT_PARAM_SCHEMA["sampler_name"], "default": "euler"}, "scheduler": {**DEFAULT_PARAM_SCHEMA["scheduler"], "default": "normal"}}, 50),
]

def ensure_default_presets() -> None:
    conn = _get_db()
    for name, description, model_type, schema, sort_order in DEFAULT_WORKFLOW_PRESETS:
        exists = conn.execute("SELECT id FROM workflow_presets WHERE name=?", (name,)).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO workflow_presets (name,description,model_type,workflow_json,params_schema,sort_order) VALUES (?,?,?,?,?,?)",
            (name, description, model_type, "{}", json.dumps(schema, ensure_ascii=False), sort_order)
        )
    conn.commit()
    conn.close()

def list_bindings() -> list[dict]:
    conn = _get_db()
    rows = conn.execute("SELECT * FROM workflow_bindings ORDER BY model_name").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_binding(model_name: str) -> Optional[dict]:
    conn = _get_db()
    row = conn.execute("SELECT * FROM workflow_bindings WHERE model_name=?", (model_name,)).fetchone()
    conn.close()
    return dict(row) if row else None

def upsert_binding(model_name: str, workflow_id: str = "sdxl_basic", workflow_name: str = "SDXL 基础文生图", workflow_type: str = "sdxl_basic", workflow_json: str = "{}", params_schema: str = "", supports_lora: int = 1, supports_negative_prompt: int = 1) -> dict:
    if not params_schema:
        params_schema = json.dumps(DEFAULT_PARAM_SCHEMA, ensure_ascii=False)
    conn = _get_db()
    conn.execute(
        """INSERT INTO workflow_bindings (model_name,workflow_id,workflow_name,workflow_type,workflow_json,params_schema,supports_lora,supports_negative_prompt)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(model_name) DO UPDATE SET workflow_id=excluded.workflow_id, workflow_name=excluded.workflow_name,
           workflow_type=excluded.workflow_type, workflow_json=excluded.workflow_json, params_schema=excluded.params_schema,
           supports_lora=excluded.supports_lora, supports_negative_prompt=excluded.supports_negative_prompt,
           updated_at=datetime('now','localtime')""",
        (model_name, workflow_id, workflow_name, workflow_type, workflow_json, params_schema, supports_lora, supports_negative_prompt)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM workflow_bindings WHERE model_name=?", (model_name,)).fetchone()
    conn.close()
    return dict(row)

def delete_binding(model_name: str) -> bool:
    conn = _get_db()
    conn.execute("DELETE FROM workflow_bindings WHERE model_name=?", (model_name,))
    conn.commit()
    conn.close()
    return True

# ── 图库 ────────────────────────────────────────────────────────────────────

def list_images(query: str = "", model_name: str = "", min_rating: int = 0, include_deleted: bool = False) -> list[dict]:
    conn = _get_db()
    sql = "SELECT * FROM gallery_images WHERE 1=1"
    params = []
    if not include_deleted:
        sql += " AND deleted=0"
    if query:
        sql += " AND (prompt LIKE ? OR file_name LIKE ? OR notes LIKE ?)"
        params.extend([f"%{query}%", f"%{query}%", f"%{query}%"])
    if model_name:
        sql += " AND model_name=?"
        params.append(model_name)
    if min_rating > 0:
        sql += " AND rating>=?"
        params.append(min_rating)
    sql += " ORDER BY created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_image(image_id: int) -> Optional[dict]:
    conn = _get_db()
    row = conn.execute("SELECT * FROM gallery_images WHERE id=?", (image_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def add_image(file_path: str, file_name: str, file_size: int = 0, width: int = 0, height: int = 0,
              prompt: str = "", negative_prompt: str = "", model_name: str = "", lora_name: str = "",
              workflow_id: int = 0, steps: int = 20, cfg: float = 7.0, seed: int = -1) -> dict:
    conn = _get_db()
    cur = conn.execute(
        """INSERT INTO gallery_images (file_path,file_name,file_size,width,height,
           prompt,negative_prompt,model_name,lora_name,workflow_id,steps,cfg,seed)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (file_path, file_name, file_size, width, height, prompt, negative_prompt,
         model_name, lora_name, workflow_id, steps, cfg, seed)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM gallery_images WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    return dict(row)

def update_rating(image_id: int, rating: int) -> dict:
    rating = max(0, min(5, rating))
    conn = _get_db()
    conn.execute("UPDATE gallery_images SET rating=? WHERE id=?", (rating, image_id))
    conn.commit()
    row = conn.execute("SELECT * FROM gallery_images WHERE id=?", (image_id,)).fetchone()
    conn.close()
    return dict(row)

def update_notes(image_id: int, notes: str) -> dict:
    conn = _get_db()
    conn.execute("UPDATE gallery_images SET notes=? WHERE id=?", (notes, image_id))
    conn.commit()
    row = conn.execute("SELECT * FROM gallery_images WHERE id=?", (image_id,)).fetchone()
    conn.close()
    return dict(row)

def delete_image(image_id: int) -> bool:
    conn = _get_db()
    conn.execute("UPDATE gallery_images SET deleted=1 WHERE id=?", (image_id,))
    conn.commit()
    conn.close()
    return True

# ── 生图配方 ────────────────────────────────────────────────────────────────

def save_recipe(name: str, prompt: str, negative_prompt: str, model_name: str, lora_name: str,
                workflow_id: int, steps: int, cfg: float, seed: int, width: int, height: int) -> dict:
    conn = _get_db()
    recipe = {"prompt": prompt, "negative_prompt": negative_prompt, "model_name": model_name,
              "lora_name": lora_name, "workflow_id": workflow_id, "steps": steps, "cfg": cfg,
              "seed": seed, "width": width, "height": height}
    cur = conn.execute(
        "INSERT INTO generation_recipes (name,prompt,negative_prompt,model_name,lora_name,workflow_id,steps,cfg,seed,width,height,recipe_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, prompt, negative_prompt, model_name, lora_name, workflow_id, steps, cfg, seed, width, height, json.dumps(recipe))
    )
    conn.commit()
    row = conn.execute("SELECT * FROM generation_recipes WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    return dict(row)

def list_recipes() -> list[dict]:
    conn = _get_db()
    rows = conn.execute("SELECT * FROM generation_recipes ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ── 配置 ─────────────────────────────────────────────────────────────────────

def get_config(key: str, default: str = "") -> str:
    conn = _get_db()
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default

def set_config(key: str, value: str):
    conn = _get_db()
    conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

# ── 初始化 ───────────────────────────────────────────────────────────────────

init_db()
ensure_default_presets()
