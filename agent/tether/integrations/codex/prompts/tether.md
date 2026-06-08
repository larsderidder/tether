---
description: Attach this Codex session to Tether
argument-hint: "[auto|none|telegram|slack|discord]"
---

Attach this Codex session to Tether.

Use the supplied argument as the bridge choice. If no argument was supplied, use `auto`.

Argument: $ARGUMENTS

Run one shell command:

```bash
bridge="$ARGUMENTS"
if [ -z "$bridge" ]; then
  bridge="auto"
fi
tether attach-current --runner-type codex --directory "$PWD" --bridge "$bridge"
```

Report the resulting Tether session ID and platform. Do not make any other changes.
