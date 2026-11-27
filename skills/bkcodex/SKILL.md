---
name: bkcodex
description: >-
  Orchestrate persistent, isolated Codex CLI background workers through bkcodex on Linux (systemd + tmux) and Windows (detached processes). Use this skill when delegating multi-step coding, refactoring, batch annotation, or verification tasks to background Codex CLI sessions with a SQLite task queue, structured result.json receipts, and automated domain validation.
---

# bkcodex Background Worker Orchestration

Use `bkcodex` when a primary agent acts as the architect/reviewer and delegates self-contained execution tasks to one or more background Codex CLI workers. Every task runs in a fresh `codex exec` session, writes artifacts and `result.json`, and passes an optional external domain validator before being marked `completed`.

## Quick Preflight

Before enqueuing work, check config and environment health:

```bash
bkcodex --config config.local.toml init
bkcodex --config config.local.toml doctor
```

Verify from `doctor` output:
- `workspace_exists: true`
- `codex_exists: true`
- Authentication is ready (`auth_file_exists: true`, or on Windows/local dev if pointing `codex.home` to an already authenticated `~/.codex` directory, verify with `<binary> login status`).
- On Linux servers, `systemctl: true` and `linger: yes` for offline `systemd --user` persistence.
- On Windows, `runtime: "windows-detached"`; `systemctl` and `tmux` are not required.

If authentication is missing in an isolated `codex.home`, instruct the user to run `bkcodex --config config.local.toml login` (device auth). Never hardcode or print credentials.

## End-to-End Orchestration Workflow

### 1. Design Clean, Non-Overlapping Tasks
- Give each task a unique ID matching `[A-Za-z0-9][A-Za-z0-9_-]{0,79}` (re-enqueuing the same ID with different prompt/payload raises `ValueError`).
- When running multiple workers (`workers.count > 1`) in parallel on the same workspace, **partition target files strictly** so two workers never edit the same file simultaneously.
- Write a focused Markdown prompt file (`task.md`) stating:
  - Concrete goal and files in scope (and files NOT to modify);
  - Exact acceptance/test commands;
  - Requirement to write `result.json` (and `CHECKPOINT.json` if blocked/partial) — `bkcodex` automatically appends the exact `run_dir` paths.
- Optionally pair it with a JSON payload (`payload.json`) consumed by your custom `[validation].command` (e.g., `expected_artifacts`, `verify_commands`).

### 2. Enqueue Tasks
Single task:
```bash
bkcodex --config config.local.toml enqueue \
  --id task-001 \
  --prompt-file tasks/task-001.md \
  --payload tasks/payload-001.json
```

Batch JSONL (`{"id": "...", "prompt_file": "...", "payload": {...}}`):
```bash
bkcodex --config config.local.toml import tasks/manifest.jsonl
```

### 3. Run Workers

Choose the execution mode based on OS and host runner:
- **Windows inside an agent tool-runner, IDE task, or CI (strongly preferred)**:
  Do **NOT** call `bkcodex start`. Many Windows subprocess runners wrap each short command in a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, which immediately terminates detached child processes when `bkcodex start` exits. Instead, launch one long-lived managed background task per worker directly:
  ```bash
  bkcodex --config config.local.toml worker --name worker-01
  ```
  Each worker drains the SQLite queue under its exclusive `msvcrt` lock, honors `STOP` / `stop --now` / `status`, and exits cleanly when the queue is empty. Streamed logs go to the host runner's task output rather than `state_dir/logs/<worker>.log`.
- **Linux (`systemd` + `tmux`) or interactive Windows terminals (PowerShell / Windows Terminal)**:
  ```bash
  bkcodex --config config.local.toml install   # required once on Linux; initializes dirs on Windows
  bkcodex --config config.local.toml start
  ```

### 4. Monitor, Review, and Recover

```bash
bkcodex --config config.local.toml status
bkcodex --config config.local.toml logs --name worker-01 --tail 80  # Linux journalctl or Windows `start` mode; in direct `worker` mode read host task output + run_dir
```

When a task finishes, always inspect its `run_dir` from `status`:
- `result.json`: `task_id`, `status` (`completed` / `blocked` / `needs_input`), `summary`, `artifacts`.
- `receipt.json`: written only after schema, artifact existence, path containment (within `state_dir` or `workspace`), and `[validation].command` all succeed.
- `validation.log` & `stderr.log` & `execution.json`: inspect first whenever a task ends up `blocked`.
- `CHECKPOINT.json`: read if a worker paused or hit timeout before resuming.

Control and recovery commands:
```bash
bkcodex --config config.local.toml stop           # pause after current task finishes
bkcodex --config config.local.toml stop --now     # immediately terminate running worker/Codex trees
bkcodex --config config.local.toml retry task-001 # re-queue a blocked / needs_input / interrupted task
bkcodex --config config.local.toml resume         # clear STOP marker and restart workers
```

## Artifact & Validation Rules

- Completed tasks (`status: "completed"`) must declare at least one existing file in `artifacts`.
- Relative artifact paths resolve against `run_dir` first, then `workspace`; every resolved artifact must remain strictly inside `state_dir` or `workspace`.
- `[validation].command` is an argv array (no shell expansion) supporting `{task}`, `{result}`, and `{run_dir}`.
- **Timeout tip**: If your validation command runs a full PyTorch/integration test suite taking >2 minutes, raise `validation.timeout_seconds` (e.g., `300`) in `config.local.toml` so valid completions are not marked `blocked` by validator timeout.

## Platform Notes & Common Pitfalls

- **Windows Job Object reaping**: Agent/IDE/CI subprocess runners commonly terminate all processes in a command's Windows Job Object as soon as that command returns. Always run `bkcodex worker --name <role>` as a managed background task in those environments instead of `bkcodex start`.
- **Windows Codex binary**: If an older global npm `codex` fails to parse newer `~/.codex/config.toml` keys (such as newer `reasoning_effort` values), point `codex.binary` to the Codex Desktop executable (`C:/Program Files/WindowsApps/OpenAI.Codex_.../app/resources/codex.exe`) or update the CLI.
- **Windows paths & encoding**: Prefer forward slashes (`C:/path/to/codex.exe`) or escaped backslashes in TOML strings. `bkcodex` sets `PYTHONUTF8=1` and reads/writes all JSON, prompts, and logs as UTF-8.
- **CLI flags**: Keep `global_args = ["--no-daemon"]` only if the installed Codex CLI supports it; otherwise set `global_args = []`.
- **Network vs. auth/quota failures**: `bkcodex` automatically retries transient network failures up to `workers.max_attempts` with shared `NETWORK_COOLDOWN.json`, and stops the worker immediately on `auth` or `quota` errors or 3 consecutive failures.
