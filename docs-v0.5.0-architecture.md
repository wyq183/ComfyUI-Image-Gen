# ComfyUI 生图助手 v0.5.0 架构重构说明

## 核心结论

v0.5.0 起，插件从「AI 通过提示词猜工作流」改为「后端确定性适配 ComfyUI」。

- 插件负责：扫描、分类、构建、校验、绑定、同步状态。
- AI 负责：解释、提示词、参数建议、引导用户安装缺失节点。
- 后端不再无条件相信 AI 传入的 `workflow_type`。

## 为什么重构

朋友测试反馈集中在四类问题：

1. 采样器、调度器、模型搜索不全面。
2. AI 配置过程中右侧悬浮窗/状态容易丢失，需要刷新。
3. 未适配 Anima 模型工作流。
4. 绑定时 Anima 被识别成 Illustrious。

根因是旧设计让大模型负责底层判断，而 SD/ComfyUI 生态高度碎片化；模型族、节点、目录、采样器不能靠提示词猜。

## 参考 SD Manager 后采用的思路

参考包体现出的成熟方向：

- 模型缓存表包含 `model_type`、`metadata_json`、`file_hash`、`rating`、`favorite`、`use_count`。
- 前端有模型类型过滤、Civitai 缓存、配方 recipe、图库 recipe 反推、树状提示词管理。
- 关键是把模型/参数/配方结构化存储，而不是只靠聊天提示词。

本插件不照搬 SD Manager UI，只吸收其工程原则：结构化资源索引 + 模型类型 + recipe/workflow 记录。

## v0.5.0 后端新增

### `backend/comfy_adapter.py`

职责：

1. 读取 ComfyUI `/object_info`。
2. 扫描模型目录：
   - checkpoints
   - loras
   - vae
   - clip
   - text_encoders
   - unet
   - diffusion_models
   - controlnet
   - upscale_models
3. 从真实节点读取 sampler/scheduler。
4. 根据文件名和目录保守分类模型。
5. 构建 SD checkpoint 工作流。
6. 构建 Anima/Qwen UNet 工作流。
7. 校验 AI 传入的 workflow_type。

## 模型分类原则

- `anima` 且位于 `unet/diffusion_models` → `anima_qwen_unet`。
- `wai/illustrious/noobai/animagine` → `sdxl_illustrious`。
- `pony/autismmix` → `sdxl_pony`。
- `realvis/juggernaut/dreamshaperxl` → `sdxl_realistic`。
- `gguf` → `gguf`。
- `nunchaku/svdq/fp4/int4` → `nunchaku`。
- 不确定 → `unknown`，不强行绑定。

## Anima 已验证工作流

基于本机技能记录：

- UNet：`diffusion_models/anima_aestheticV11.safetensors`
- 文本编码器：`text_encoders/qwen_3_06b_base.safetensors`
- VAE：`vae/qwen_image_vae.safetensors`

节点结构：

```text
UNETLoader
CLIPLoader(type=qwen_image)
VAELoader
CLIPTextEncode 正/负
EmptyLatentImage
KSampler(euler/normal, cfg=1.0, steps=25)
VAEDecode
SaveImage
```

LoRA 使用 `LoraLoaderModelOnly`，不接 CLIP 侧。

## API 变化

### `/image-gen/status`

新增：

- `all_models`：完整模型扁平列表，不只前 50 个 checkpoints。
- `resources`：按目录分组的资源。
- `samplers` / `schedulers`：从 ComfyUI object_info 读取。
- `node_capabilities`：节点能力摘要。

### `/image-gen/workflows/bind`

新增后端校验：

- Anima/Flux/Nunchaku/GGUF 不能保存为 Illustrious/SDXL checkpoint 工作流。
- 明显错误直接返回 400，并给出 corrected_type。

### `/image-gen/workflows/auto-bind`

新增自动绑定接口：

- 输入 `model_name`。
- 后端根据资源索引和模型分类生成参数 schema 并绑定。
- 不依赖 AI 猜测。

## 下一步

1. 前端模型下拉改用 `all_models`，按 kind/model_type 分组展示。
2. 增加“一键自动适配工作流”按钮调用 `/workflows/auto-bind`。
3. 右侧面板轮询 workflow version，避免需要手动刷新。
4. root 丢失时自动重挂载，避免右侧悬浮窗消失。
5. 开 ComfyUI 后跑 Anima 1-step 或低分辨率实际验证。
