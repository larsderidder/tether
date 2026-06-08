---
description: Attach this Claude Code session to Tether
argument-hint: "[auto|none|telegram|slack|discord]"
allowed-tools: ["Bash(tether attach-current:*)"]
---

Attach this Claude Code session to Tether.

Use the supplied argument as the bridge choice. If no argument was supplied, use `auto`.

```!
bridge="$ARGUMENTS"
if [ -z "$bridge" ]; then
  bridge="auto"
fi
if [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
  tether attach-current --runner-type claude_code --directory "$PWD" --external-id "$CLAUDE_CODE_SESSION_ID" --bridge "$bridge"
else
  tether attach-current --runner-type claude_code --directory "$PWD" --bridge "$bridge"
fi
```

Report the resulting Tether session ID and platform. Do not make any other changes.
