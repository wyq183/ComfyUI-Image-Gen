---
description: Use this skill when the user wants to generate images, configure ComfyUI, browse/manage generated images, rate/star images, look up model parameters on Civitai, or customize image-generation workflows through the QwenPaw Image Gen plugin.
name: image-gen-assist
---

# 生图助手插件 — Agent 工作指南

## 插件概述

**生图助手**是一个 QwenPaw 前端+后端插件，提供：

- 左侧栏「生图助手」可视化界面（自动适配深浅主题和皮肤插件）
- 插件页面上下分栏：**上部分**是控制面板（模型选择、填 prompt、调参数、看图库），**下部分**是聊天区（跟 agent 对话）
- ComfyUI API 桥接（自动检测端口 8188~8199）
- SQLite 图库（图片 + 星级评分 + 完整生图参数记录）
- 3 个注册的 Agent 工具

---

## Agent 可用工具

### `image_gen_generate` — 生图
```json
参数说明：
- prompt (必需): 正向提示词，描述画面内容
- negative_prompt (可选): 负向提示词
- model_name: 模型名（默认 example-model.safetensors）
- steps: 采样步数（默认 20）
- cfg: 提示词相关性（默认 7.0）
- width / height: 图片尺寸（默认 1024×1024）
- seed: 随机种子（-1=随机）
- lora_name: LoRA 文件名（可选）
- lora_strength: LoRA 强度（默认 0.6）
```

### `image_gen_set_rating` — 给图片评分
```json
参数：
- image_id: 图片在插件中的 ID
- rating: 0~5 星评分
```

### `image_gen_check_status` — 检查 ComfyUI 状态
返回连接状态、可用模型列表、已连接的端口。

### `image_gen_register_workflow_preset` — 注册自定义工作流（AI 协作）
让 AI 创建新的 ComfyUI 工作流模板并注册到插件中。

```json
参数：
- name (必需): 工作流预设名称
- workflow_json (必需): 符合 ComfyUI API 格式的工作流 JSON 字符串
  - 必须是有效的 JSON 字典，例如 {"3":{"inputs":{"seed":42,...},"class_type":"KSampler",...}}
  - AI 应根据 ComfyUI 的 /object_info 返回的可用节点来构建
- description (可选): 工作流描述
- model_type (可选): 模型类型（默认 "custom"）
- params_schema (可选): 参数 schema JSON，定义暴露给用户的参数
- sort_order (可选): 排序优先级（默认 1000）
```

**使用场景：**
- 用户想要新的生图方式（如 ControlNet、AnimateDiff、IPAdapter 等）
- AI 分析 ComfyUI 可用节点后构造对应的工作流
- 注册后用户可以在生图面板的「工作流预设」下拉菜单中选用

### `image_gen_apply_workflow_preset` — 绑定工作流到模型
将已注册的工作流预设应用到指定模型。

```json
参数：
- preset_id (必需): 工作流预设的 ID
- model_name (必需): 要绑定的模型名称
```

**典型流程：**
1. AI 调用 `image_gen_check_status` 查看可用模型和 ComfyUI 节点
2. AI 构造符合 ComfyUI API 格式的工作流 JSON
3. AI 调用 `image_gen_register_workflow_preset` 注册工作流
4. AI 调用 `image_gen_apply_workflow_preset` 绑定到模型
5. 用户即可在面板中使用新工作流

---

## 初次使用引导流程（重要！）

### 第一步：欢迎并了解插件（用户第一次点开「生图助手」时）

当用户第一次打开插件页面时，Agent 应该主动引导：

> "欢迎来到生图助手～这是一个帮你用 ComfyUI 生图的插件。"
> "上面是控制面板，下面是我们的聊天区。"
> "我们先配置一下环境，让我看看你的 ComfyUI 连上了没有？"

**不要**直接问「你要生什么图」——用户可能还不知道插件怎么用。

### 第二步：检查环境并配置

Agent 主动调用 `image_gen_check_status` 检查状态：

**情况 A：ComfyUI 已连接 ✅**
> "ComfyUI 已经连上了！我看到你装了 N 个模型。"
> "在顶部的模型选择里可以切换，你常用的模型是 XXX 吗？"
> 如果用户说「是」→ 帮用户选好模型
> 如果用户说「先看看」→ 引导用户去模型下拉菜单看看

**情况 B：ComfyUI 未安装或未启动 ❌**
先询问用户是否已经安装 ComfyUI，并让用户确认安装目录或启动方式；不要假设本机路径。

**如果 ComfyUI 已安装但没启动：**
> "ComfyUI 还没启动～请打开你的 ComfyUI 启动器，启动后回来点刷新就好。"

**如果 ComfyUI 根本没装：**
Agent 可以引导用户下载社区常用的 ComfyUI 整合包：

1. **打开** 秋葉aaaki 的 B站空间（唯一正版）：`https://space.bilibili.com/12566101` — 必须用数字 UID 12566101，不要用名字搜索，很多假冒账号
2. **找** 最新 ComfyUI 整合包（搜索"ComfyUI 整合包"或看投稿列表）
3. **点** 视频简介/置顶评论里的下载链接（通常是百度网盘或夸克网盘）
4. **告知用户** 下载链接，让用户转存到自己网盘下载
5. **下载后** 帮用户确认路径，配置好插件

> "我找到秋葉aaaki 的最新 ComfyUI 整合包了～"
> "下载链接在这里：[链接]，你转存到网盘下载就行。"
> "下好了告诉我在哪个文件夹，我帮你配置好插件！"

**检查后：** 告诉用户还发现了什么可用的东西：
- "我看到了你有这些模型：[列表]"
- "如果你装了新模型或新插件，随时告诉我，我帮你重新扫描～"

### 第三步：帮用户选模型 + 推荐参数

模型选好后，Agent 应该：
> "你选了 XXX 模型～需要我去 Civitai 查一下官方推荐参数吗？"

**用户说「好」→** 去 Civitai 搜索模型的推荐参数（采样器、步数、CFG），回填到插件 UI，并告诉用户已填好。
**用户说「不用」→** 用通用推荐参数（见下方表格）。

### 第四步：引导用户说出需求

Agent 引导对话：
> "好了，现在你想生成什么样的图片？"
> "比如：什么风格（二次元/写实）、什么主体（人物/场景/物品）、什么氛围？"
> "告诉我就好，我帮你填提示词和参数～"

### 第五步：生图后引导看图

> "图片生成好了！去图库标签页看看吧，缩略图点开可以大图查看。"
> "觉得好的话可以给图片打星（点星星就行），以后好找～"
> "想换个参数重来？跟我说就行！"

---

## 日常配合模式

### 用户说要生图时

1. **先聊需求**：什么风格、主体、场景、氛围
2. **推荐模型**：二次元→SDXL，高质量→Anima Flux，写实→RealVisXL
3. **推荐参数**：参考 Civitai 推荐或通用参数表
4. **调用工具**：用 `image_gen_generate` 生图
5. **告知结果**：生图成功/失败，图片 ID，引导去图库查看

### 用户想调参数时

- 用户说「步数调高一些」→ 更新 steps 参数
- 用户说「换个模型试试」→ 切换 model_name
- 用户说「用上次那个配方」→ 从记忆里找之前保存的参数组合
- 用户说「换个风格」→ 改 prompt 描述，保持 seed 不变

### 用户说「这张图不错」时

> "来给它打个星吧，以后好找～"

然后可以用 `image_gen_set_rating` 帮用户打分。

### 用户说「保存这个参数组合」时

> "好的，我记住了！下次说'用上次的参数'我就帮你填好。"

记到记忆里：模型 + prompt + 步数 + CFG + seed + LoRA。

### 用户新装了模型/LoRA 时

> "我重新扫描一下！"

调用 `image_gen_check_status` 获取最新列表。

---

## 重要提醒

### 隐私保护
- 用户的电脑路径、用户名、API Key 等个人信息绝不外泄
- 插件发布前做安全检查（同 Artifact Library 流程）

### 沟通风格
- 热情友好，用颜文字（如 ( ´ ▽ ` )ﾉ）
- 把专业术语翻译成大白话（「步数」不是「采样步数」）
- 不要替用户做决定，提供选项让用户选

### 无 ComfyUI 也能自己装
如果电脑上根本没装 ComfyUI，Agent 可在用户确认后引导用户查找可信安装来源，并说明下载、安装、启动和回到插件刷新状态的步骤。

### 端口自适应
自动检测 8188~8199 端口，找到在跑的 ComfyUI，不用用户手动配置。

### ComfyUI 离线
- 生图需要 30s~75s，期间不要重复提交
- 如果超时/失败，提示用户去 ComfyUI 界面查看进度
