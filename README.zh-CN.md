# 独立儿童绘本生成 Skill

[English](README.md)

这是一个可独立安装到 OpenClaw/Codex 的儿童绘本生成 skill。它可以从用户输入的主题开始，完成绘本大纲生成、页面解析、标题和简介文案生成、封面优先生图、多页图片落盘、任务状态持久化、超时控制、目录扫描和缺页续跑。



## 能做什么

完整流程如下：

1. `topic` -> 儿童绘本大纲
2. 大纲 -> 解析为 `pages`
3. 大纲 -> 标题、简介文案、标签
4. pages -> 封面优先的多页图片生成
5. 生成结果 -> 写入 `tasks/<task_id>/`
6. 未完成任务 -> 使用 `--only-missing` 只补缺失页

它针对额度敏感的图片生成场景做了约束：

- 默认串行生成，一次只生成一张图。
- 没有任何自动重试逻辑。
- 单张图片默认等待时间是 120 秒。
- 目录扫描默认每 60 秒执行一次。
- 总等待时间 = `本次需要生成的页数 * page_timeout_seconds`。
- 补页时，本次需要生成的页数只等于缺失图片数量，不等于绘本总页数。

## 仓库结构

可以把本目录作为 GitHub 仓库根目录上传，也可以放在更大仓库的 `skills/independent-image-generation/` 目录下。

必须包含这些文件：

```text
independent-image-generation/
├── SKILL.md
├── README.md
├── README.zh-CN.md
├── requirements.txt
├── agents/
│   └── openai.yaml
├── references/
│   ├── config.example.yaml
│   ├── content-prompt.txt
│   ├── outline-prompt.txt
│   ├── prompt-full.txt
│   ├── prompt-short.txt
│   └── workflow-safety.md
└── scripts/
    └── image_workflow_cli.py
```



## 使用 OpenClaw 安装

使用 OpenClaw 的 skill 安装流程从 GitHub 仓库安装。不同 OpenClaw 版本命令可能略有差异，先查看当前版本支持的命令：

```powershell
openclaw skills --help
```

然后使用你的 GitHub 仓库地址安装：

```text
https://github.com/4a5s5/picture_book_creation
```

仓库根目录必须有 `SKILL.md`，或者仓库内有一个清晰的 skill 子目录并包含 `SKILL.md`。

安装后检查 OpenClaw 是否识别到 skill：

```powershell
openclaw skills list
```

skill 名称是：

```text
independent-image-generation
```

如果你的 OpenClaw 版本暂不支持直接从 GitHub 安装 skill，可以手动把本目录放到 OpenClaw/Codex 使用的 skills 目录，然后重启或刷新 OpenClaw 的 skill 索引。

## 安装 Python 依赖

在 skill 根目录执行：

```powershell
python -m pip install -r requirements.txt
```

基础依赖：

- `requests`
- `PyYAML`
- `Pillow`

如果要使用 Google Gemini provider，还需要：

```powershell
python -m pip install google-genai
```

## 创建配置文件

在 skill 根目录执行：

```powershell
python scripts/image_workflow_cli.py init-config --output .\workflow_config.yaml
```

然后编辑 `workflow_config.yaml`，填入你的文本模型和图片模型 key。


图片生成的重要配置：

```yaml
short_prompt: false
high_concurrency: false
max_workers: 1
page_timeout_seconds: 120
scan_interval_seconds: 60
```

默认行为是串行逐页生成。

## 准备输入

创建 `payload.json`：

```json
{
  "task_id": "bear-emotion-picture-book-16p",
  "topic": "生成一个关于小熊学会情绪管理的儿童绘本故事，包括搭积木倒了以后如何处理生气、被奶奶批评后如何表达不舒服、说错话以后如何诚实道歉",
  "page_count": 16
}
```

可选字段：

```json
{
  "style": "水彩手绘风格",
  "user_images": ["C:/path/to/reference.png"]
}
```

如果不写 `style`，提示词会要求文本模型根据故事内容自动选择适合的儿童绘本风格。

## 运行完整流程

在 skill 根目录执行：

```powershell
$env:PYTHONUTF8='1'
python scripts/image_workflow_cli.py run --config .\workflow_config.yaml --input .\payload.json --compact
```

命令会输出 JSON lines。主要事件包括：

- `outline_complete`
- `content_complete`
- `generation_window`
- `progress`
- `complete`
- `scan`
- `timeout`
- `finish`

`generation_window` 表示本次图片阶段的额度控制窗口：

```json
{
  "target_count": 16,
  "page_timeout_seconds": 120,
  "scan_interval_seconds": 60,
  "total_timeout_seconds": 1920
}
```

这里的含义是：本次要生成 16 页，每页最多等待 120 秒，总等待时间是 1920 秒。

## 继续补缺页

如果任务超时，或者只有部分图片缺失，使用：

```powershell
python scripts/image_workflow_cli.py generate-images --config .\workflow_config.yaml --input .\tasks\<task_id>\task_state.json --only-missing --compact
```

这个模式会：

- 先扫描任务目录
- 找出缺失图片文件
- 只生成缺失页
- 按缺页数量计算总等待时间
- 不重新生成已经存在的图片

示例：

```text
16 页绘本，只缺第 2 页和第 14 页
pages_to_generate = 2
page_timeout_seconds = 120
total_timeout_seconds = 240
```

## 查看任务状态

```powershell
python scripts/image_workflow_cli.py task-state --task-id bear-emotion-picture-book-16p --config .\workflow_config.yaml --compact
```

重点查看：

- `generated`
- `failed`
- `files`
- `pages`
- `task_dir`

## 手动重试单页

没有自动重试。如果你明确想重试某一页，需要准备该页 JSON 文件，然后执行：

```powershell
python scripts/image_workflow_cli.py retry --config .\workflow_config.yaml --task-id <task_id> --page .\page-2.json --compact
```

这是手动操作，会对该页发送一次新的图片请求。

## 安全与额度控制

完整规则见 [references/workflow-safety.md](references/workflow-safety.md)。

关键规则：

- 保持 `high_concurrency: false`，额度消耗更可控。
- 保持 `max_workers: 1`，除非你明确想并发请求。
- 未完成任务使用 `--only-missing`。
- 新故事使用新的 `task_id`。
- 不要提交本地配置和生成结果。

## 常见问题

如果 OpenClaw 找不到 skill：

- 确认仓库根目录存在 `SKILL.md`。
- 确认 skill 名称是 `independent-image-generation`。
- 刷新或重启 OpenClaw 的 skill 索引。
- 使用 `openclaw skills list` 查看可用 skills。

如果 Python 缺少依赖：

```powershell
python -m pip install -r requirements.txt
```

如果 Windows 控制台输出中文时报编码错误：

```powershell
$env:PYTHONUTF8='1'
```

如果任务看起来卡住：

```powershell
python scripts/image_workflow_cli.py task-state --task-id <task_id> --config .\workflow_config.yaml --compact
```

然后使用 `--only-missing` 继续补缺页。
