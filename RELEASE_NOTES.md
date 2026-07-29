# Release Notes

## v0.9.3

- 修复图库切换分类时列表仍显示旧图片、响应卡顿的问题。
- 图库列表与分类读取不再隐式扫描整个 ComfyUI output；仅点击「扫描 ComfyUI」时执行全盘扫描。
- 切换分类时立即清空旧列表并显示加载状态；用请求序号防止旧请求的迟到响应覆盖新分类结果。

## v0.9.2

- 修复 ComfyUI PNG 元数据扫描只显示正向提示词的问题。
- 按 KSampler 的 `positive` / `negative` 连线准确提取正向与反向提示词；重新扫描可补齐旧图片记录。

## v0.9.1

- 图库详情新增所属分类选择器，可将已有图片实际移动到 ComfyUI output 内对应分类文件夹。
- 数据库同步更新图片分类、文件路径与文件名；目标重名时自动避免覆盖。
- 新增「全部分类」视图。


## v0.8.0

- 新增调试追踪功能：后端记录最后一次生成的完整提示词，通过 `/debug/last-generation` 端点可查看
- 新增前端调试面板：在「工作流参数」下方显示最后一次发送的提示词，方便排查提示词问题
- 新增 ComfyUI 画布同步说明：明确告知用户插件通过 API 提交工作流，不会改变 ComfyUI 画布显示
- 后端添加详细日志：记录发送到 ComfyUI 的提示词和反向提示词，便于调试
- 所有改动同时更新到 package 目录，确保打包版本与源码一致

## v0.5.0

- 架构重构：新增 `backend/comfy_adapter.py`，插件后端负责确定性 ComfyUI 能力发现、模型分类、工作流构建与绑定校验，AI 降级为提示词和解释辅助。
- `/image-gen/status` 改为完整资源扫描：返回 checkpoints、loras、vae、clip、text_encoders、unet、diffusion_models、controlnet、upscale_models，不再只截断前 50 个 checkpoint。
- 采样器和调度器改从 ComfyUI `/object_info` 中读取真实节点选项，而不是仅依赖手写枚举。
- 新增 Anima / Qwen UNet 工作流构建器：`UNETLoader + CLIPLoader(type=qwen_image) + VAELoader + KSampler`，默认 CFG 1.0、Euler/normal，LoRA 使用 `LoraLoaderModelOnly`。
- 后端 `/workflows/bind` 新增校验：Anima/Flux/Nunchaku/GGUF 等非 checkpoint 模型不能被错误保存成 Illustrious/SDXL 工作流。
- 新增 `/workflows/auto-bind`：用户选择模型后可一键自动适配工作流，减少“复制提示词让 AI 猜”的出错路径。
- 前端入口升级到 `ui/index.0.5.0.js`，无工作流状态下新增“一键自动适配”按钮。
- 新增 `docs-v0.5.0-architecture.md`，记录本次参考 SD Manager 后的职责边界和后续重构方向。

## v0.4.1

- 修复 QwenPaw Desktop / Electron WebView 可能继续加载旧版前端缓存的问题。
- 前端入口改为带版本号的 `ui/index.0.4.1.js`，升级后资源 URL 会变化，避免命中旧缓存。
- 后端新增 `/image-gen/version` 接口，用于前后端版本自检。
- 后端所有插件 API 响应加入 `Cache-Control: no-store` 等禁缓存响应头。
- 前端标题显示版本号，检测到前后端版本不一致时给出明确重启/清缓存提示。
- README 新增桌面端升级与缓存说明。


## v0.4.0

- 新增默认工作流预设：SDXL 通用、Illustrious / WAI、Pony、SD1.5、快速预览。
- 新增右侧面板工作流切换：可直接选择默认或自定义工作流，让插件开箱即可尝试生成。
- 新增“保存当前工作流”：保存当前面板参数为可复用预设，同名自动覆盖，避免重复堆积。
- 新增与 QwenPaw 产物库的本地联动：生成图片可登记为本地创作资产，便于检索、评分、备注与复用。
- 社区文案统一使用正式名称 ComfyUI，移除口语化缩写。
- 移除会修改宿主主聊天输入框或压缩宿主布局的高风险逻辑，降低 Desktop 渲染进程闪退风险。
- 补齐 requirements.txt 与 build_package.py，打包时只包含可审查运行文件，不包含 data、数据库、图片缓存或本地私有路径。

## v0.3.1-hotfix.2

- 修复社区文案中 ComfyUI 名称不正式的问题。
- 公开接口统一改为正式命名。
- 补齐基础依赖说明。

## v0.3.1-hotfix.1

- 移除写入主聊天输入框、修改宿主布局等高风险 DOM 操作。
- 改为复制提示词草稿到剪贴板，避免与 QwenPaw Desktop 前端状态冲突。
- 重写默认最小 ComfyUI 工作流提示词。

## 隐私与去个人化要求

发布前必须确认：

1. 文档、截图、默认数据不出现私人名称或真实项目名。
2. 不包含真实聊天记录、真实图片素材、真实图库数据库。
3. 不包含 API Key、账号信息或本机绝对路径。
4. 模型只显示文件名或脱敏名称，不展示本机完整路径。
5. 打包前清空 data、图库、数据库和缓存。
