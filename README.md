# Independent Picture Book Generation Skill

[中文文档](README.zh-CN.md)

Standalone OpenClaw/Codex skill for generating children-picture-book image workflows from a user topic. It includes text prompt generation, outline parsing, story packaging text, cover-first image generation, task state persistence, timeout control, directory scanning, and missing-page continuation.

This skill does not depend on RedInk application code.

## What It Does

The workflow is:

1. `topic` -> children-picture-book outline
2. outline -> parsed `pages`
3. outline -> titles, copywriting, tags
4. pages -> cover-first image generation
5. generated files -> `tasks/<task_id>/`
6. unfinished task -> continue with `--only-missing`

It is designed for cost-sensitive image generation:

- Default image generation is serial.
- No automatic retry logic is used.
- Per-page timeout defaults to 120 seconds.
- Directory scan interval defaults to 60 seconds.
- Total image-stage timeout is `pages_to_generate * page_timeout_seconds`.
- For `--only-missing`, `pages_to_generate` is the number of missing image files, not the full story page count.

## Repository Layout

Upload this folder as a GitHub repository root, or keep it under a `skills/independent-image-generation/` path in a larger repository.

Required files:

```text
independent-image-generation/
├── SKILL.md
├── README.md
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

Do not upload local runtime files:

- `workflow_config.yaml`
- `tasks/`
- `.codex_*.json`
- `.codex_*.yaml`
- `.retry_pages_*/`

These are ignored by `.gitignore`.

## Install With OpenClaw

Use OpenClaw's skill installation flow for a GitHub skill repository. In current OpenClaw builds, inspect the exact command with:

```powershell
openclaw skills --help
```

Then install from your GitHub repository URL. The repository must include `SKILL.md` at the skill root, or a clearly nested skill folder containing `SKILL.md`.

After installation, confirm OpenClaw can see the skill:

```powershell
openclaw skills list
```

The skill name is:

```text
independent-image-generation
```

If your OpenClaw version does not support direct GitHub skill installation, manually place this folder in the OpenClaw/Codex skills directory used by your environment, then restart or refresh OpenClaw's skill index.

## Natural-Language Triggering

After installation, OpenClaw should be able to select this skill from natural-language picture-book requests because `SKILL.md` explicitly describes those triggers.

Example prompts:

```text
制作一个关于恐龙的儿童绘本
生成一本小熊情绪管理绘本，16页
做一个关于小朋友学会分享玩具的儿童绘本
create a dinosaur picture book for kids
make a 12-page bedtime storybook about the moon
继续补齐上次儿童绘本缺失的图片页
```

When the request includes a topic but does not specify page count or style, the skill lets the outline prompt decide a suitable story length and illustration style.

OpenClaw agents must still execute the bundled CLI. They should not hand-write the story or reuse old task output as a fallback. A successful run must include `outline_complete`, `content_complete`, `generation_window`, and `finish` with `success: true`.

## Install Python Dependencies

The CLI script needs Python and these core packages:

```powershell
python -m pip install -r requirements.txt
```

Core dependencies:

- `requests`
- `PyYAML`
- `Pillow`

Optional dependency for Google Gemini providers:

```powershell
python -m pip install google-genai
```

## Create A Config

Generate a local config file:

```powershell
python scripts/image_workflow_cli.py init-config --output .\workflow_config.yaml
```

Edit `workflow_config.yaml` and fill in your provider keys.

Do not commit `workflow_config.yaml` to GitHub.

Important image settings:

```yaml
task_lock_stale_seconds: 300
short_prompt: false
high_concurrency: false
max_workers: 1
page_timeout_seconds: 120
scan_interval_seconds: 60
```

Default behavior is serial, one image at a time.

## Prepare Input

Create `payload.json`:

```json
{
  "task_id": "bear-emotion-picture-book-16p",
  "topic": "生成一个关于小熊学会情绪管理的儿童绘本故事，包括搭积木倒了以后如何处理生气、被奶奶批评后如何表达不舒服、说错话以后如何诚实道歉",
  "page_count": 16
}
```

Optional fields:

```json
{
  "style": "水彩手绘风格",
  "user_images": ["C:/path/to/reference.png"]
}
```

If `style` is omitted, the prompt asks the text model to choose a fitting children-picture-book style.

## Run Full Workflow

From the skill root:

```powershell
$env:PYTHONUTF8='1'
python scripts/image_workflow_cli.py run --config .\workflow_config.yaml --input .\payload.json --compact
```

For OpenClaw natural-language use, the simpler direct command is:

```powershell
$env:PYTHONUTF8='1'
python scripts/image_workflow_cli.py run-topic --config .\workflow_config.yaml --topic "制作一个关于恐龙的儿童绘本" --page-count 12 --task-id dinosaur-picture-book-12p --compact
```

Output is JSON lines. Important events:

- `outline_complete`
- `content_complete`
- `generation_window`
- `progress`
- `complete`
- `scan`
- `timeout`
- `finish`

If the command exits non-zero, use the JSON error output or `tasks/<task_id>/task_error.json` as the final failure report. Do not package old files or manually written text as a substitute for a failed workflow.

Do not run the workflow with a 5-second or similarly short external timeout. Use at least `page_count * page_timeout_seconds + 600` seconds, or leave the command running until the CLI emits `finish`.

The `generation_window` event shows the cost-control window:

```json
{
  "target_count": 16,
  "page_timeout_seconds": 120,
  "scan_interval_seconds": 60,
  "total_timeout_seconds": 1920
}
```

## Continue Missing Pages

If generation times out or only some files are missing, continue with:

```powershell
python scripts/image_workflow_cli.py generate-images --config .\workflow_config.yaml --input .\tasks\<task_id>\task_state.json --only-missing --compact
```

This mode:

- Scans the task directory first
- Finds missing image files
- Generates only missing pages
- Computes timeout from missing page count only
- Does not regenerate existing images

Example:

```text
16-page story, pages 2 and 14 missing
pages_to_generate = 2
page_timeout_seconds = 120
total_timeout_seconds = 240
```

## Inspect Task State

```powershell
python scripts/image_workflow_cli.py task-state --task-id bear-emotion-picture-book-16p --config .\workflow_config.yaml --compact
```

Look for:

- `generated`
- `failed`
- `files`
- `pages`
- `task_dir`

If a failed run leaves only `.task.lock`, inspect it with:

```powershell
python scripts/image_workflow_cli.py diagnose-task --task-id <task_id> --config .\workflow_config.yaml --compact
```

Remove a stale lock only when `lock_pid_alive` is `false`:

```powershell
python scripts/image_workflow_cli.py cleanup-lock --task-id <task_id> --config .\workflow_config.yaml --compact
```

## Manual Single-Page Retry

There is no automatic retry. If you intentionally want to retry one page, prepare a page JSON file and run:

```powershell
python scripts/image_workflow_cli.py retry --config .\workflow_config.yaml --task-id <task_id> --page .\page-2.json --compact
```

This is a manual action and will send a new image request for that page.

## Safety Rules

See [references/workflow-safety.md](references/workflow-safety.md) for the full cost-control behavior.

Key rules:

- Keep `high_concurrency: false` for predictable credit usage.
- Keep `max_workers: 1` unless you intentionally want parallel requests.
- Use `--only-missing` for unfinished tasks.
- Use a new `task_id` for a new story.
- Do not commit local config or generated tasks.

## Troubleshooting

If OpenClaw cannot find the skill:

- Confirm `SKILL.md` exists at the installed skill root.
- Confirm the skill folder name is `independent-image-generation`.
- Refresh or restart OpenClaw's skill index.
- Run `openclaw skills list` to inspect available skills.

If Python cannot import dependencies:

```powershell
python -m pip install -r requirements.txt
```

If Windows console output fails on Chinese text:

```powershell
$env:PYTHONUTF8='1'
```

If a task appears stuck:

```powershell
python scripts/image_workflow_cli.py task-state --task-id <task_id> --config .\workflow_config.yaml --compact
```

Then continue missing pages with `--only-missing`.
