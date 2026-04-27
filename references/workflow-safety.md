# Workflow Safety And Cost Control

This document summarizes the current safety rules for the independent picture-book image generation skill. The goal is to make image generation predictable, avoid hidden retries, and prevent unfinished background processes from continuing to consume credits.

## Current Behavior

The skill is fully standalone and does not depend on the original RedInk application.

The full workflow is:

1. Generate a children-picture-book outline from `topic`.
2. Parse the outline into canonical `pages`.
3. Optionally generate titles, packaging copy, and tags.
4. Generate images cover-first.
5. Persist task state and image files under the configured task directory.
6. Use `--only-missing` to continue unfinished pages without regenerating existing images.

## Image Generation Rules

- Default image generation is serial.
- `high_concurrency: false` and `max_workers: 1` mean one page is generated at a time.
- Concurrency is used only when `high_concurrency: true` and `max_workers > 1`.
- There is no automatic retry logic.
- Failed pages are recorded in `failed`; they are not automatically retried.
- Existing image files are treated as completed pages after directory scanning.
- `task_state.json` is reconciled with actual image files whenever task state is read or image generation starts.

## Timeout Calculation

Image generation uses two config fields:

```yaml
page_timeout_seconds: 120
scan_interval_seconds: 60
```

`page_timeout_seconds` is the maximum wait time for one image request.

The total image-stage timeout is:

```text
pages_to_generate * page_timeout_seconds
```

For a full run, `pages_to_generate` is the number of pages in the current generated outline.

For `generate-images --only-missing`, `pages_to_generate` is the number of missing image files on disk. It is not the total story page count.

Example:

```text
16-page story with 2 missing pages
pages_to_generate = 2
page_timeout_seconds = 120
total_timeout_seconds = 240
```

## Directory Scanning

During image generation, the task directory is scanned every `scan_interval_seconds`.

Each scan reports:

- `target_indices`: pages targeted by the current run
- `completed_indices`: targeted pages that already have image files
- `missing_indices`: targeted pages without image files
- `failed_indices`: targeted pages with recorded errors
- `completed_count`
- `missing_count`

The final `finish` event returns the same progress fields.

## Timeout Result

When total time is reached, the skill stops sending new image requests and returns a partial result.

The final result may include:

```json
{
  "status": "timeout",
  "missing_indices": [8, 9],
  "continue_command": "generate-images --config <config> --input <task_state.json> --only-missing"
}
```

Use the missing indices as the basis for continuing generation.

## Continue Missing Pages

Use this command to generate only missing images:

```powershell
python skills/independent-image-generation/scripts/image_workflow_cli.py generate-images --config .\workflow_config.yaml --input .\tasks\task_id\task_state.json --only-missing
```

This mode:

- Scans the task directory first
- Builds the page list only from missing image files
- Computes total timeout from the missing page count
- Does not regenerate pages whose image files already exist

## Task Locking

The skill creates `.task.lock` inside the task directory while a task is running.

The lock prevents another command from running the same `task_id` at the same time.

If a process is killed and leaves a stale lock, the next command checks the stored PID. If the PID no longer exists, the stale lock is removed.

## Process Exit Handling

The CLI installs shutdown handlers and a parent-process watchdog.

The intended behavior is:

- When the CLI receives an interrupt signal, it marks cancellation and stops before starting additional pages.
- If the parent process disappears, the CLI exits instead of continuing as a hidden background process.
- A request already sent to the image provider cannot always be cancelled by the remote service, but the CLI will not start new pages after cancellation or timeout.

## Cost-Control Checklist

Before running a real image task:

1. Confirm `high_concurrency: false`.
2. Confirm `max_workers: 1`.
3. Confirm `page_timeout_seconds` is acceptable for the provider.
4. Confirm `scan_interval_seconds` is acceptable for progress reporting.
5. Use a new `task_id` for a new story.
6. Use `--only-missing` only for continuing an existing unfinished task.

## Important Limitations

The skill can prevent new local requests after timeout or interruption, but it cannot force a remote provider to refund or cancel a request that was already accepted by the provider.

For cost-sensitive usage, keep serial generation enabled and set a conservative `page_timeout_seconds`.
