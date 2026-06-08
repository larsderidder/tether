# Script automations

Script automations let a Tether session hand each chat turn to a local command. The command receives a JSON manifest with the message text, image paths, run paths, and output locations. What happens after that is up to the script: it can call pi, run OCR, query an LLM API, process files, or just produce a deterministic report.

Adapter name: `automation`.

## Where automations live

Tether loads YAML files from these directories, in this order:

1. `<session-directory>/.tether/automations/*.yaml`
2. `~/.config/tether/automations/*.yaml`

If two files define the same `name`, the later file wins. In practice this means global automations can be overridden per project.

## YAML schema

```yaml
name: pokemon-photo-triage
metadata: optional, ignored by Tether

description: Triage Pokemon card shop photos
timeout_seconds: 180
output_markdown: "{output_md}"

steps:
  - name: triage
    run:
      cwd: /home/lars/workspace/card-tools
      command:
        - python3
        - triage_with_pi.py
        - --manifest
        - "{manifest}"
```

Fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | No | Automation name. Defaults to the YAML filename stem. |
| `description` | No | Human readable description. |
| `timeout_seconds` | No | Timeout per step. Defaults to `180`. Valid range is `1` to `900`. |
| `output_markdown` | No | Markdown file to read for the final answer. Defaults to `{output_md}`. |
| `steps` | Yes | Ordered subprocess steps. |
| `steps[].name` | No | Step name used in errors and logs. |
| `steps[].run.cwd` | No | Working directory for the step. Defaults to the session directory. |
| `steps[].run.command` | Yes | Argument vector for the command. Must be a non-empty list of strings. |

A shorthand form is also supported for one-step automations:

```yaml
name: quick-triage
cwd: /home/lars/workspace/card-tools
command:
  - python3
  - triage_with_pi.py
  - "{manifest}"
```

`shell: true` is rejected. Commands run through `asyncio.create_subprocess_exec`, so each argument is passed as its own argv item.

## Template fields

Tether renders these placeholders inside `command` and `output_markdown`:

| Placeholder | Value |
| --- | --- |
| `{manifest}` | Absolute path to `manifest.json`. |
| `{run_dir}` | Absolute path to the private run directory. |
| `{input_dir}` | Absolute path to the directory containing copied input images. |
| `{output_md}` | Absolute path where the script can write final Markdown. |
| `{output_json}` | Absolute path where the script can write optional JSON. Tether does not read it yet. |
| `{output_messages_dir}` | Absolute path where the script can write streamed Markdown messages. |

Unknown placeholders fail fast before the subprocess starts.

## Per-turn run directory

Every input turn gets its own private run directory:

```text
<TETHER_AGENT_DATA_DIR>/automation-runs/<session-id>/<automation-name>-<uuid>/
```

Tether creates:

```text
input/
manifest.json
run.log
```

The automation may create:

```text
output.md
output.json
messages/*.md
```

`run.log` contains the command line, stdout, stderr, and exit code for each step.

## Manifest schema

Tether writes `manifest.json` before the first step starts:

```json
{
  "session_id": "sess_123",
  "run_id": "pokemon-photo-triage-abc123",
  "automation": "pokemon-photo-triage",
  "text": "please triage these",
  "run_dir": "/home/lars/.local/share/tether/automation-runs/sess_123/pokemon-photo-triage-abc123",
  "input_dir": "/home/lars/.local/share/tether/automation-runs/sess_123/pokemon-photo-triage-abc123/input",
  "output_md": "/home/lars/.local/share/tether/automation-runs/sess_123/pokemon-photo-triage-abc123/output.md",
  "output_json": "/home/lars/.local/share/tether/automation-runs/sess_123/pokemon-photo-triage-abc123/output.json",
  "output_messages_dir": "/home/lars/.local/share/tether/automation-runs/sess_123/pokemon-photo-triage-abc123/messages",
  "images": [
    {
      "path": "/home/lars/.local/share/tether/automation-runs/sess_123/pokemon-photo-triage-abc123/input/001-card.png",
      "filename": "card.png",
      "mime_type": "image/png",
      "size": 12345
    }
  ],
  "steps": [
    {
      "name": "triage",
      "command": ["python3", "triage_with_pi.py", "--manifest", "{manifest}"],
      "cwd": "/home/lars/workspace/card-tools"
    }
  ]
}
```

The script should treat paths in the manifest as the source of truth. Do not reconstruct paths from the session ID or automation name.

## Calling pi from Python

Automation scripts can call pi through Tether's helper instead of setting up pi RPC themselves. The helper uses the same pi binary discovery and inherited environment as Tether, so CLI auth and local config work the same way they do for the `pi_rpc` adapter.

```python
from __future__ import annotations

import sys

from tether.automation_helpers import ask_pi_from_manifest

answer = ask_pi_from_manifest(
    sys.argv[1],
    "Triage the attached photos and return concise Markdown.",
)
print(answer)
```

The helper reads images from `manifest.json`, sends them to pi, writes the final answer to `output_md`, and returns the same text. For more control, use the async helper:

```python
from tether.automation_helpers import ask_pi

answer = await ask_pi(
    "Check this repository and summarize the risk.",
    cwd="/home/lars/workspace/project",
    output_markdown="/tmp/output.md",
)
```

This keeps workflow logic in the script, while Tether handles pi startup, image payload conversion, auth inheritance, and final Markdown writing.

## Output contract

At the end of all steps, Tether sends the final answer using this priority:

1. Read `output_markdown` if it exists and is not empty.
2. Use stdout from the last step if it is not empty.
3. Send a small completion message with the run directory.

For incremental output, create Markdown files in `output_messages_dir`:

```text
messages/001-started.md
messages/002-found-cards.md
messages/003-prices.md
```

Tether scans for `*.md` files while each step runs. Each non-empty file is sent once, sorted by filename. Use numeric prefixes if order matters.

## Input selection

If the session can see one automation, every message is sent to that automation.

If the session can see more than one automation, the first word selects the automation and the rest of the message becomes input:

```text
pokemon please triage these photos
```

You can also use the explicit prefix form:

```text
/automation:pokemon please triage these photos
```

## Queueing and failure behavior

Inputs for the same session run sequentially. If a second message arrives while a step is still running, Tether queues it and processes it after the current turn finishes.

A non-zero step exit raises `AUTOMATION_ERROR`. The error includes stderr, stdout, or the exit code, capped to 1200 characters. A timeout kills the process and raises `AUTOMATION_ERROR` as well.

## Environment and trust model

Automation subprocesses inherit the Tether server environment and run as the same OS user as Tether, so treat each automation as trusted local code with the same access as the Tether process.

The command schema intentionally avoids `shell: true`; this prevents accidental shell interpolation, but it does not make an untrusted automation safe. If the script calls another agent, reads local files, or uses network credentials, those actions happen with Tether's permissions.

## Telegram usage

Create an automation session from Telegram:

```text
/new automation /path/to/project
```

Then send text, photos, or albums in the created topic. The Telegram bridge passes images through to the automation manifest and renders Markdown outputs back into the topic.
