from __future__ import annotations
import json
import shutil
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parent
package = root / "package"
if package.exists():
    shutil.rmtree(package)
(package / "backend" / "workflows").mkdir(parents=True)
(package / "ui").mkdir(parents=True)
(package / "skills" / "image-gen-assist").mkdir(parents=True)

# Package only reviewable runtime files. Do not ship data/, database, images,
# local cache, development logs, or private workspace paths.
for source, target in [
    (root / "README.md", package / "README.md"),
    (root / "RELEASE_NOTES.md", package / "RELEASE_NOTES.md"),
    (root / "requirements.txt", package / "requirements.txt"),
    (root / "backend" / "plugin.py", package / "backend" / "plugin.py"),
    (root / "backend" / "image_store.py", package / "backend" / "image_store.py"),
    (root / "backend" / "comfy_adapter.py", package / "backend" / "comfy_adapter.py"),
    (root / "backend" / "workflows" / "__init__.py", package / "backend" / "workflows" / "__init__.py"),
    (root / "ui" / "index.js", package / "ui" / "index.js"),
    (root / "skills" / "image-gen-assist" / "SKILL.md", package / "skills" / "image-gen-assist" / "SKILL.md"),
]:
    shutil.copy2(source, target)

for ui_file in sorted((root / "ui").glob("*.js")):
    target = package / "ui" / ui_file.name
    if not target.exists():
        shutil.copy2(ui_file, target)

manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))

# 清理旧版前端文件：只保留 index.js 和 index.{当前版本}.js
_version = manifest.get("version", "0.0.0")
for f in (package / "ui").glob("index.*.js"):
    if f.name != f"index.{_version}.js":
        f.unlink()
        print(f"[build] 清理旧前端: {f.name}")

# 自动同步 frontend 入口：确保 entry.frontend 指向 index.{version}.js
_expected_frontend = f"ui/index.{_version}.js"
_actual_frontend = manifest.get("entry", {}).get("frontend", "")
if _actual_frontend != _expected_frontend:
    print(f"[build] ⚠️ 修正 frontend 入口: {_actual_frontend} → {_expected_frontend}")
    manifest["entry"]["frontend"] = _expected_frontend

(package / "plugin.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)

zip_path = root / f"qwenpaw-image-gen-{manifest['version']}.zip"
if zip_path.exists():
    zip_path.unlink()
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
    for path in sorted(package.rglob("*")):
        if path.is_file():
            zf.write(path, path.relative_to(package).as_posix())
print(zip_path)
print(zip_path.stat().st_size)
print("\n".join(str(p.relative_to(package)) for p in sorted(package.rglob("*")) if p.is_file()))
