---
name: independent-image-generation
description: Create children picture books from natural-language requests such as "制作一个关于恐龙的儿童绘本", "生成一本小熊情绪管理绘本", "make a dinosaur picture book for kids", or "create a 16-page bedtime storybook". Runs a standalone end-to-end workflow from user topic to outline, page parsing, story packaging text, cover-first image generation, local task snapshots, timeout control, and missing-page continuation. Use whenever Codex/OpenClaw is asked to create, generate, make, continue, retry, or inspect a children's picture book or multi-page illustrated story, without relying on host application code.
---

# Independent Picture Book Generation

## Overview

Use this skill when the full workflow must be fully self-contained. The skill owns its own text and image provider adapters, prompt templates, page parsing, task-state persistence, thumbnail generation, and CLI entrypoint.

Run [scripts/image_workflow_cli.py](./scripts/image_workflow_cli.py) to initialize config, generate picture-book outlines, generate titles and parent-facing copy, generate images, retry pages, regenerate pages, or inspect task output.

## Quick Start

1. Create a workflow config file.

```powershell
python skills/independent-image-generation/scripts/image_workflow_cli.py init-config --output .\workflow_config.yaml
```

2. Prepare a payload file.

```json
{
  "task_id": "task_demo",
  "topic": "生成一个关于小熊学会分享的儿童绘本故事",
  "page_count": 14,
  "style": "水彩拼贴风格",
  "user_images": [
    "C:/work/ref/bear-reference.png"
  ]
}
```

3. Run the full workflow.

```powershell
python skills/independent-image-generation/scripts/image_workflow_cli.py run --config .\workflow_config.yaml --input .\payload.json
```

The command emits one JSON event per line, generates outline pages first, optionally generates titles and copy, then writes image outputs under `tasks/task_id/` unless you override the output root.

## Workflow Rules

- Generate the outline before images. The parsed `pages` become the canonical page list for image generation unless you manually override them.
- Treat the workflow as children-picture-book generation, not social-post generation. Prompts, page structure, and image direction should serve story continuity, readability, and child-safe presentation.
- Generate the cover first. If no page is marked as `cover`, treat the first page as the cover.
- Store the raw outline text and derived `pages` together so later retry and regenerate commands still have the original context.
- Reuse the generated cover as a style reference for later pages when the selected provider supports reference images.
- If the user provides `page_count`, use it as the primary page-count target. If not provided, let the outline model choose a suitable length for the story.
- If the user does not provide `style`, let the prompts choose a fitting children's-illustration style from the story theme, emotion, and setting.
- Respect `short_prompt` and `high_concurrency` from the config instead of hardcoding one behavior.
- Persist task state to disk so retry and regenerate work even in a fresh process.
- Reconcile `task_state.json` with images on disk when reading task state, so interrupted or repeated runs do not leave stale failure markers.
- Use `page_timeout_seconds` as the per-image request timeout and compute the image-stage timeout as `pages_to_generate * page_timeout_seconds`. For `--only-missing`, `pages_to_generate` is the number of missing image files, not the full story page count.
- Scan the task directory every `scan_interval_seconds` during image generation and report completed, missing, and failed page indices. Do not automatically retry failed pages.
- Save both full images and thumbnails for every successful page.

## Commands

```powershell
python skills/independent-image-generation/scripts/image_workflow_cli.py init-config --output .\workflow_config.yaml
python skills/independent-image-generation/scripts/image_workflow_cli.py config --config .\workflow_config.yaml
python skills/independent-image-generation/scripts/image_workflow_cli.py generate-outline --config .\workflow_config.yaml --input .\payload.json
python skills/independent-image-generation/scripts/image_workflow_cli.py generate-content --config .\workflow_config.yaml --input .\outline_payload.json
python skills/independent-image-generation/scripts/image_workflow_cli.py generate-images --config .\workflow_config.yaml --input .\pages_payload.json
python skills/independent-image-generation/scripts/image_workflow_cli.py generate-images --config .\workflow_config.yaml --input .\tasks\task_demo\task_state.json --only-missing
python skills/independent-image-generation/scripts/image_workflow_cli.py run --config .\workflow_config.yaml --input .\payload.json
python skills/independent-image-generation/scripts/image_workflow_cli.py retry --config .\workflow_config.yaml --task-id task_demo --page .\page-1.json
python skills/independent-image-generation/scripts/image_workflow_cli.py regenerate --config .\workflow_config.yaml --task-id task_demo --page .\page-1.json
python skills/independent-image-generation/scripts/image_workflow_cli.py task-state --task-id task_demo --config .\workflow_config.yaml
```

## Resources

- [README.md](./README.md): GitHub installation and usage guide
- [requirements.txt](./requirements.txt): core Python dependencies for the CLI
- [scripts/image_workflow_cli.py](./scripts/image_workflow_cli.py): standalone CLI and workflow engine
- [references/config.example.yaml](./references/config.example.yaml): example workflow config
- [references/outline-prompt.txt](./references/outline-prompt.txt): topic-to-outline prompt template
- [references/content-prompt.txt](./references/content-prompt.txt): outline-to-story-packaging prompt template
- [references/prompt-full.txt](./references/prompt-full.txt): full image prompt template
- [references/prompt-short.txt](./references/prompt-short.txt): short image prompt template
- [references/workflow-safety.md](./references/workflow-safety.md): timeout, task-locking, no-retry, and cost-control rules
