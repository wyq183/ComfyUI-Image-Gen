# ComfyUI 生图助手

面向 QwenPaw 的本地 ComfyUI 生图助手插件。插件用于连接本机 ComfyUI、选择工作流、生成图片、查看结果并记录可复用的生成参数。

v0.5.0 起，插件改为“确定性适配”架构：后端负责扫描 ComfyUI 资源、读取真实节点参数、分类模型、构建工作流和绑定校验；AI 只负责解释、提示词与参数建议，避免让 AI 猜 Anima / Illustrious / Flux 等底层工作流类型。

## 产物库联动建议（社区发布版 / 隐私安全版）

生图助手可以与 QwenPaw 产物库联动，将生成结果自动归档为可检索、可管理、可复用的创作资产。

- **生图助手**：负责连接 ComfyUI、配置工作流、生成图片、保存生成参数。
- **产物库**：负责长期归档、分类管理、项目整理、评分筛选和复用记录。

### 生成完成后自动登记到产物库

生成图片后可自动归档到 QwenPaw 产物库，便于后续按项目、模型、标签和评分进行管理。可保存的基础元数据包括图片路径、生成时间、模型、提示词、负向提示词、LoRA、采样参数、尺寸、Seed 和可选 workflow 信息。

### 产物库中显示生成参数

社区版只显示模型文件名或脱敏后的模型名称，不展示本机完整路径。

| 字段 | 内容 |
|---|---|
| 来源 | ComfyUI 生图助手 |
| 后端 | ComfyUI |
| 模型 | 用户本地模型名称 |
| LoRA | 可选 |
| Seed | 生成种子 |
| Steps | 采样步数 |
| CFG | 提示词引导系数 |
| Sampler | 采样器 |
| Scheduler | 调度器 |
| 尺寸 | 宽 × 高 |

### 从产物库复用生成参数

用户可以从产物库中复用历史生成参数，将提示词、模型、采样参数发送回生图助手继续调整。

### 推荐资产类型

```json
{
  "artifact_type": "generated_image",
  "source_plugin": "qwenpaw-image-gen",
  "backend": "ComfyUI"
}
```

## 隐私说明

本插件默认在本地运行。与产物库联动时，生成图片及其参数仅登记到用户本机的 QwenPaw 产物库中。

插件不会主动上传以下信息：

- 本地文件路径
- API Key
- 用户账号信息
- 聊天记录
- 私人项目名称
- 图片内容
- ComfyUI 安装目录
- 本地模型完整路径

如果用户手动将产物打包、分享或发布，请自行检查其中是否包含私人路径、真实项目名或其他敏感信息。

## 通用示例

```json
{
  "title": "ComfyUI 生成图片",
  "project": "AI 图像生成项目",
  "tags": ["ComfyUI", "generated-image", "QwenPaw"],
  "metadata": {
    "model_name": "example-model.safetensors",
    "prompt": "example prompt",
    "negative_prompt": "example negative prompt",
    "steps": 28,
    "cfg": 7,
    "seed": 123456
  }
}
```


## 升级与桌面端缓存说明

v0.4.1 起，插件前端入口改为带版本号的资源文件（例如 `ui/index.0.4.1.js`），并在后端 API 中加入禁缓存响应头。这样可以避免 QwenPaw Desktop / Electron WebView 在升级插件后继续加载旧版前端脚本。

建议升级步骤：

1. 安装新版插件包。
2. 完全退出并重新打开 QwenPaw Desktop。
3. 打开生图助手，标题区域应显示当前版本号。

如果升级后仍显示旧界面，说明桌面端 WebView 仍保留旧缓存。可退出 QwenPaw Desktop 后清理：

- `%LOCALAPPDATA%\io.agentscope.qwenpaw.desktop\EBWebView\Default\Cache`
- `%LOCALAPPDATA%\io.agentscope.qwenpaw.desktop\EBWebView\Default\Code Cache`

本插件不会自动删除用户缓存，只提供版本化资源与版本自检提示，避免误删其他桌面端数据。
