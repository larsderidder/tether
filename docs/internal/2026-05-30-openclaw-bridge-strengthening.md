# OpenClaw bridge strengthening notes

OpenClaw has a much more mature bridge layer than Tether. The useful patterns are mostly around media safety, delivery reliability, and preserving context from platform-specific message shapes.

## High-value patterns to port

### 1. Central media runtime instead of per-bridge downloads

OpenClaw routes bridge media through shared helpers, not ad hoc bridge code.

References:
- `/home/lars/workspace/openclaw/extensions/discord/src/monitor/message-media.ts`
- `/home/lars/workspace/openclaw/extensions/telegram/src/bot/delivery.resolve-media.ts`
- `/home/lars/workspace/openclaw/src/media/load-options.ts`
- `/home/lars/workspace/openclaw/src/media/web-media.ts`

Useful pieces:
- Download timeouts with idle and total caps.
- Max byte limits before storing or forwarding.
- MIME sniffing and fallback content type handling.
- SSRF policy per platform, Discord CDN allowlist, Telegram API host allowlist.
- Safe local file handling for Bot API local paths.
- One normalized payload for downstream agents, `MediaPath`, `MediaPaths`, `MediaTypes`, etc.

Tether currently has a small image helper. Next step would be a `bridge_media.py` runtime that supports images first, then documents and audio later.

### 2. Discord inbound hydration

OpenClaw hydrates partial Discord gateway messages via REST when text, mentions, or attachments may be incomplete.

Reference:
- `/home/lars/workspace/openclaw/extensions/discord/src/monitor/message-handler.hydration.ts`

Why it matters:
- Gateway payloads can miss mention metadata or full message shape.
- Attachment and forwarded-message handling is more reliable if the message is re-fetched when suspicious.

Tether opportunity:
- Before routing Discord thread input, fetch the message if it has attachments, mentions, forwarded snapshots, or empty text with media.

### 3. Forwarded and referenced media support

OpenClaw pulls media from direct attachments, forwarded messages, stickers, and referenced replies.

Reference:
- `/home/lars/workspace/openclaw/extensions/discord/src/monitor/message-media.ts`

Tether opportunity:
- Add support for Discord replied-to image references, not just images attached to the current message.
- For Telegram, support documents that are images, not only `photo` messages.

### 4. Telegram media group debounce

OpenClaw buffers Telegram albums so several photos become one agent turn.

Reference:
- `/home/lars/workspace/openclaw/extensions/telegram/src/bot-handlers.runtime.ts`

Useful pieces:
- Debounce album entries.
- Merge media into a single dispatch.
- Send a user-facing warning when only some images were fetched.

Tether opportunity:
- Add album handling for Telegram with `media_group_id`, capped at the same image limit.

### 5. Delivery retry wrappers

OpenClaw wraps outbound Discord delivery with retry handling for 429 and 5xx responses, including `Retry-After`.

Reference:
- `/home/lars/workspace/openclaw/extensions/discord/src/delivery-retry.ts`

Tether opportunity:
- Wrap Discord and Telegram sends in small retry helpers.
- Respect platform rate-limit headers where available.
- Keep failure notices short and user-facing.

### 6. Outbound media access policy

OpenClaw scopes local media reads to agent roots and workspace roots, then passes that policy to bridge senders.

References:
- `/home/lars/workspace/openclaw/src/media/read-capability.ts`
- `/home/lars/workspace/openclaw/src/media/local-roots.ts`

Tether already constrains final-output attachments to the session directory. It could still benefit from a shared attachment sender that handles MIME, image vs document choice, and consistent platform limits.

### 7. Bot loop protection and dedupe

OpenClaw has logic for bot-to-bot loop suppression and dispatch dedupe before sending input to agents.

Reference:
- `/home/lars/workspace/openclaw/extensions/discord/src/monitor/message-handler.process.ts`

Tether opportunity:
- Add a lightweight per-platform recent-message dedupe cache.
- Ignore messages from known bot accounts beyond the platform bot itself.

## Recommended Tether roadmap

1. Add `agent/tether/bridges/media.py`, centralize image validation, platform downloads, and future document/audio support.
2. Add Discord hydration for media messages before `_collect_message_images`.
3. Add Telegram document-as-image and media group support.
4. Add bridge send retry helpers for Discord and Telegram.
5. Move Telegram outbound attachment sending and Discord outbound attachment sending behind one `BridgeAttachmentSender` abstraction.
6. Add tests for Discord replied-to media, Telegram album partial failures, rate-limit retries, and MIME spoofing.

## Security notes

Keep these controls when porting ideas:
- Byte sniffing must win over platform MIME headers and filenames.
- Downloads need max bytes, idle timeout, total timeout, and platform host allowlists.
- Local file output must stay under the session directory unless explicitly configured otherwise.
- Media failures should not drop the whole user message when text can still be routed.
