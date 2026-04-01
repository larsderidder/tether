# Checkmark Reaction Shortcut Dev Plan

## Goal

Allow an authorized Slack or Discord user to create and start a new Tether chat by:

1. posting a top-level control-channel message
2. adding a green checkmark reaction to that message

## Assumed Message Contract

- Only top-level messages in the bound control channel are eligible.
- The first line must reuse the existing `!new` argument contract, for example:

```text
!new codex /worktrees/tether
Fix the failing Discord bridge tests and keep the control channel bootstrap intact.
```

- The first line selects the adapter and directory.
- The remaining non-empty body becomes the initial prompt sent to the created session.
- Empty prompt bodies are rejected with a platform-native error reply.
- Only authorized users may trigger the shortcut.
- Each platform message id may create at most one session; duplicate reaction events must no-op.

## Tether Implementation Slices

1. Add bridge settings for the shortcut.
   - `TETHER_BRIDGE_REACTION_NEW_SESSION_ENABLED`
   - `TETHER_BRIDGE_REACTION_NEW_SESSION_EMOJI` with default `✅`

2. Add a small shared helper in Tether-local bridge code.
   - validate control-channel scope
   - validate reaction emoji
   - parse the first line with the existing `_parse_new_args(...)` flow
   - extract the prompt body
   - deduplicate by message id

3. Extend the local Discord bridge wrapper.
   - handle reaction-add events only in the configured control channel
   - reject thread reactions
   - require the reacting user to be authorized
   - create the session with `platform="discord"`
   - send the initial prompt via the existing input/start callback path
   - reply with the created thread link

4. Replace the Slack compatibility shim with a Tether-local subclass.
   - keep upstream Slack behavior as the base
   - add `reaction_added` handling for the configured channel
   - mirror the same parsing, authorization, dedupe, and create-plus-start flow
   - reply in channel with the created thread reference

5. Keep `tether-autolaunchd` narrow.
   - no feature logic belongs in the daemon
   - it only needs to preserve the future bridge env knobs when launching managed Tether
   - Discord machine-channel bootstrap must continue to work with the shortcut enabled

## Test Plan

- Passing parser-contract tests:
  - top-level messages still require an explicit directory when only an agent is provided
  - thread-derived context can still reuse the base session directory and adapter rules

- Planned disabled behavior tests:
  - Discord checkmark reaction in the bound control channel creates and starts a session
  - Slack checkmark reaction in the bound control channel creates and starts a session
  - non-checkmark reactions, thread reactions, unauthorized reactions, and duplicate events are ignored
