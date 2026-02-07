"""Runner adapter that uses the Claude Agent SDK (local OAuth via Claude CLI)."""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import structlog

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ThinkingBlock,
    HookMatcher,
)

from tether.prompts import SYSTEM_PROMPT
from tether.runner.base import RunnerEvents
from tether.store import store

logger = structlog.get_logger(__name__)

HEARTBEAT_INTERVAL = 5.0
PERMISSION_TIMEOUT = 300.0  # 5 minutes timeout for permission requests


class ClaudeLocalRunner:
    """Runner that uses the Claude Agent SDK with local OAuth authentication.

    This runner spawns the Claude Code binary which handles authentication
    using OAuth tokens stored in ~/.claude/.credentials.json.
    No ANTHROPIC_API_KEY required.
    """

    runner_type: str = "claude-local"

    def __init__(self, events: RunnerEvents) -> None:
        self._events = events
        # Map our session_id -> SDK session_id for resume
        self._sdk_sessions: dict[str, str] = {}
        # Active query tasks per session
        self._tasks: dict[str, asyncio.Task] = {}
        # Store permission mode per session for follow-up inputs
        self._permission_modes: dict[str, str] = {}
        # Queue follow-up inputs while a query is running
        self._pending_inputs: dict[str, list[str]] = {}

    async def start(self, session_id: str, prompt: str, approval_choice: int) -> None:
        """Start a Claude conversation session.

        Args:
            session_id: Internal session identifier.
            prompt: Initial prompt to send.
            approval_choice: Approval policy (0=interactive, 1=acceptEdits, 2=bypassPermissions).
        """
        # Map approval_choice to permission_mode and store for follow-up inputs
        permission_mode = self._map_permission_mode(approval_choice)
        self._permission_modes[session_id] = permission_mode

        logger.info(
            "Starting claude_local session",
            session_id=session_id,
            approval_choice=approval_choice,
            permission_mode=permission_mode,
        )
        store.clear_stop_requested(session_id)

        # Get session to determine working directory
        session = store.get_session(session_id)
        cwd = session.directory if session and session.directory else None

        # Check for pre-attached external session ID (from attach flow)
        resume = store.get_runner_session_id(session_id)
        if resume:
            # Pre-populate internal cache for subsequent send_input calls
            self._sdk_sessions[session_id] = resume
            logger.info(
                "Starting with attached session",
                session_id=session_id,
                external_session_id=resume,
            )

        # Start the query task
        task = asyncio.create_task(
            self._run_query(session_id, prompt, cwd, permission_mode, resume=resume)
        )
        self._tasks[session_id] = task

    async def send_input(self, session_id: str, text: str) -> None:
        """Send follow-up input to the conversation.

        Args:
            session_id: Internal session identifier.
            text: Follow-up input text.
        """
        if not text.strip():
            return

        # Get SDK session_id for resume - check internal cache first, then store
        # (store may have pre-attached external session ID from attach flow)
        sdk_session_id = self._sdk_sessions.get(session_id)
        if not sdk_session_id:
            sdk_session_id = store.get_runner_session_id(session_id)
            if sdk_session_id:
                # Cache it for subsequent calls
                self._sdk_sessions[session_id] = sdk_session_id
                logger.info(
                    "Using attached session for send_input",
                    session_id=session_id,
                    external_session_id=sdk_session_id,
                )

        # Get session to determine working directory
        session = store.get_session(session_id)
        cwd = session.directory if session and session.directory else None

        # Check if there's an active task
        existing_task = self._tasks.get(session_id)
        if existing_task and not existing_task.done():
            logger.warning(
                "send_input called while query still running; queueing input",
                session_id=session_id,
            )
            self._pending_inputs.setdefault(session_id, []).append(text)
            return

        store.clear_stop_requested(session_id)

        # Use cached permission mode from start(), falling back to the
        # persisted approval_mode on the session (survives agent restarts).
        permission_mode = self._permission_modes.get(session_id)
        if not permission_mode:
            approval_mode = session.approval_mode if session else None
            permission_mode = self._map_permission_mode(approval_mode or 0)

        # Start new query with resume
        task = asyncio.create_task(
            self._run_query(
                session_id,
                text,
                cwd,
                permission_mode=permission_mode,
                resume=sdk_session_id,
            )
        )
        self._tasks[session_id] = task

    async def stop(self, session_id: str) -> int | None:
        """Stop the Claude session.

        Args:
            session_id: Internal session identifier.

        Returns:
            Exit code (always 0 for clean stop).
        """
        store.request_stop(session_id)

        # Cancel any pending permission requests
        store.clear_pending_permissions(session_id)

        task = self._tasks.get(session_id)
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        self._tasks.pop(session_id, None)
        store.clear_stop_requested(session_id)

        return 0

    def _map_permission_mode(self, approval_choice: int) -> str:
        """Map approval_choice to SDK permission_mode."""
        # 0 = suggest (default), 1 = auto-approve edits, 2 = full auto
        if approval_choice == 2:
            return "bypassPermissions"
        elif approval_choice == 1:
            return "acceptEdits"
        else:
            return "default"

    def update_permission_mode(self, session_id: str, approval_choice: int) -> None:
        """Update permission mode for an active session.

        Args:
            session_id: Internal session identifier.
            approval_choice: Approval policy (0=interactive, 1=acceptEdits, 2=bypassPermissions).
        """
        permission_mode = self._map_permission_mode(approval_choice)
        self._permission_modes[session_id] = permission_mode
        logger.info(
            "Updated permission mode",
            session_id=session_id,
            approval_choice=approval_choice,
            permission_mode=permission_mode,
        )

    def _make_pre_tool_use_hook(self, session_id: str):
        """Create a PreToolUse hook callback for permission handling.

        Uses the hooks system instead of can_use_tool because hooks have
        proper bidirectional communication support in the SDK.

        Args:
            session_id: Internal session identifier.

        Returns:
            An async hook callback function.
        """

        async def pre_tool_use_hook(hook_input, tool_use_id, context):
            """Handle PreToolUse hook to request permission from user.

            Args:
                hook_input: Hook input with tool_name and tool_input.
                tool_use_id: ID of the tool use.
                context: Hook context.

            Returns:
                Hook output with permissionDecision.
            """
            tool_name = hook_input.get("tool_name", "unknown")
            tool_input = hook_input.get("tool_input", {})

            logger.info(
                "PreToolUse hook invoked",
                session_id=session_id,
                tool_name=tool_name,
                tool_use_id=tool_use_id,
            )

            request_id = f"perm_{uuid.uuid4().hex[:12]}"

            # Create a future to wait for the user's response
            loop = asyncio.get_running_loop()
            future: asyncio.Future = loop.create_future()

            # Store the pending permission request
            store.add_pending_permission(
                session_id, request_id, tool_name, tool_input, future
            )

            try:
                # Emit the permission request event to the UI
                await self._events.on_permission_request(
                    session_id,
                    request_id=request_id,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    suggestions=None,
                )

                # Wait for the user's response with timeout
                result = await asyncio.wait_for(future, timeout=PERMISSION_TIMEOUT)

                logger.info(
                    "Permission response received",
                    session_id=session_id,
                    request_id=request_id,
                    behavior=result.get("behavior"),
                )

                # Return hook output with permission decision
                if result.get("behavior") == "allow":
                    return {
                        "continue_": True,
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "allow",
                        },
                    }
                else:
                    return {
                        "continue_": True,
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": result.get(
                                "message", "User denied permission"
                            ),
                        },
                    }

            except asyncio.TimeoutError:
                logger.warning(
                    "Permission request timed out",
                    session_id=session_id,
                    request_id=request_id,
                )
                # Clean up the pending request
                store.resolve_pending_permission(
                    session_id, request_id, {"behavior": "deny", "message": "Timeout"}
                )
                # Notify UI to dismiss the dialog
                await self._events.on_permission_resolved(
                    session_id,
                    request_id=request_id,
                    resolved_by="timeout",
                    allowed=False,
                    message="Permission request timed out after 5 minutes",
                )
                return {
                    "continue_": True,
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "Permission request timed out",
                    },
                }

            except asyncio.CancelledError:
                logger.info(
                    "Permission request cancelled",
                    session_id=session_id,
                    request_id=request_id,
                )
                # Notify UI to dismiss the dialog
                await self._events.on_permission_resolved(
                    session_id,
                    request_id=request_id,
                    resolved_by="cancelled",
                    allowed=False,
                    message="Session was interrupted",
                )
                raise

        return pre_tool_use_hook

    async def _run_query(
        self,
        session_id: str,
        prompt: str,
        cwd: str | None,
        permission_mode: str,
        resume: str | None,
    ) -> None:
        """Run the Agent SDK query and emit events.

        Args:
            session_id: Internal session identifier.
            prompt: User prompt.
            cwd: Working directory for the agent.
            permission_mode: SDK permission mode.
            resume: SDK session ID to resume, if any.
        """
        start_time = time.monotonic()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(session_id, start_time)
        )

        try:
            # Create stderr handler to log CLI output for debugging
            def stderr_handler(line: str) -> None:
                logger.debug("CLI stderr", session_id=session_id, line=line)

            # Set up PreToolUse hook for permission handling if not in bypass mode
            # We use hooks instead of can_use_tool because hooks have proper
            # bidirectional communication support in the SDK
            hooks = None
            if permission_mode != "bypassPermissions":
                pre_tool_hook = self._make_pre_tool_use_hook(session_id)
                hooks = {"PreToolUse": [HookMatcher(hooks=[pre_tool_hook])]}

            options = ClaudeAgentOptions(
                cwd=Path(cwd) if cwd else None,
                permission_mode=permission_mode,
                resume=resume,
                # Only continue conversation when resuming an existing session
                continue_conversation=resume is not None,
                # Don't load external settings that might override permission_mode
                setting_sources=[],
                # Custom system prompt for application-wide instructions
                system_prompt=SYSTEM_PROMPT,
                # Enable stderr logging for debugging
                stderr=stderr_handler,
                # PreToolUse hooks for permission handling
                hooks=hooks,
            )

            logger.info(
                "Starting query",
                session_id=session_id,
                cwd=cwd,
                permission_mode=permission_mode,
                resume=resume,
                continue_conversation=options.continue_conversation,
                has_hooks=hooks is not None,
                options_permission_mode=options.permission_mode,
            )

            # When using hooks, we need streaming input to keep stdin open for the
            # control protocol. The generator yields the prompt and then waits on
            # an event until the query completes, preventing stdin from closing.
            query_done = asyncio.Event()

            async def prompt_stream():
                yield {"type": "user", "message": {"role": "user", "content": prompt}}
                # Keep generator alive until query completes - this keeps stdin open
                # for hook responses via the control protocol
                if hooks:
                    await query_done.wait()

            try:
                async for message in query(prompt=prompt_stream(), options=options):
                    # Check for stop request
                    if store.is_stop_requested(session_id):
                        break

                    logger.info("Received message", session_id=session_id, message_type=type(message).__name__)
                    await self._handle_message(session_id, message)

                    # ResultMessage indicates completion; allow stdin to close
                    if isinstance(message, ResultMessage):
                        query_done.set()
                        break
            finally:
                # Signal the generator to finish so it can be garbage collected
                query_done.set()

        except asyncio.CancelledError:
            logger.info("Claude query cancelled", session_id=session_id)
        except Exception as e:
            logger.exception("Claude query failed", session_id=session_id)
            await self._events.on_error(session_id, "CLAUDE_LOCAL_ERROR", str(e))
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

            # Emit final heartbeat
            elapsed = time.monotonic() - start_time
            await self._events.on_heartbeat(session_id, elapsed, done=True)

            # If inputs are queued, start the next query immediately
            pending = self._pending_inputs.get(session_id)
            if pending and not store.is_stop_requested(session_id):
                next_input = pending.pop(0)
                if not pending:
                    self._pending_inputs.pop(session_id, None)

                # Reuse the permission_mode from this query's scope — it was
                # already resolved at the top of _run_query's caller.

                task = asyncio.create_task(
                    self._run_query(
                        session_id,
                        next_input,
                        cwd,
                        permission_mode=permission_mode,
                        resume=self._sdk_sessions.get(session_id),
                    )
                )
                self._tasks[session_id] = task
                return

            # Signal completion
            if store.is_stop_requested(session_id):
                await self._events.on_exit(session_id, 0)
            else:
                await self._events.on_awaiting_input(session_id)

    async def _handle_message(self, session_id: str, message) -> None:
        """Process a message from the Agent SDK.

        Args:
            session_id: Internal session identifier.
            message: SDK message object.
        """
        if isinstance(message, SystemMessage):
            await self._handle_system_message(session_id, message)
        elif isinstance(message, AssistantMessage):
            await self._handle_assistant_message(session_id, message)
        elif isinstance(message, ResultMessage):
            await self._handle_result_message(session_id, message)
        # UserMessage typically echoes our input, skip it

    async def _handle_system_message(
        self, session_id: str, message: SystemMessage
    ) -> None:
        """Handle SystemMessage (init, etc.)."""
        if message.subtype == "init":
            data = message.data
            # Store SDK session ID for resume
            sdk_session_id = data.get("session_id")

            # Check if we expected a specific session (from attach or prior run)
            expected_session_id = self._sdk_sessions.get(session_id)
            if expected_session_id and sdk_session_id:
                if expected_session_id != sdk_session_id:
                    # SDK created a new session instead of resuming the bound one.
                    # The original session is likely gone — accept the new one
                    # and update our binding so we stay consistent going forward.
                    logger.warning(
                        "SDK returned different session ID than expected — "
                        "rebinding to new session",
                        session_id=session_id,
                        expected=expected_session_id,
                        actual=sdk_session_id,
                    )
                    store.clear_runner_session_id(session_id)
                else:
                    logger.info(
                        "SDK resumed expected session",
                        session_id=session_id,
                        sdk_session_id=sdk_session_id,
                    )

            if sdk_session_id:
                self._sdk_sessions[session_id] = sdk_session_id
                # Persist to store so sync can find it
                store.set_runner_session_id(session_id, sdk_session_id)

            # Emit header
            model = data.get("model", "claude")
            version = data.get("claude_code_version", "")
            title = f"Claude Code{f' {version}' if version else ''}"
            await self._events.on_header(
                session_id,
                title=title,
                model=model,
                provider="Anthropic (OAuth)",
            )

    async def _handle_assistant_message(
        self, session_id: str, message: AssistantMessage
    ) -> None:
        """Handle AssistantMessage (text, tool use, etc.)."""
        if message.error:
            await self._events.on_error(
                session_id, f"CLAUDE_{message.error.upper()}", message.error
            )
            return

        # Analyze content to determine which text blocks are intermediate vs final
        # If there are tool uses, all text is intermediate reasoning
        # If no tool uses, only the last text block is final
        text_blocks = [b for b in message.content if isinstance(b, TextBlock)]
        has_tool_use = any(isinstance(b, ToolUseBlock) for b in message.content)
        last_text_index = len(text_blocks) - 1

        text_index = 0
        for block in message.content:
            if isinstance(block, TextBlock):
                # Text is final only if: no tool uses AND this is the last text block
                is_final_text = not has_tool_use and text_index == last_text_index
                await self._events.on_output(
                    session_id,
                    "combined",
                    block.text,
                    kind="final" if is_final_text else "step",
                    is_final=is_final_text,
                )
                text_index += 1
            elif isinstance(block, ToolUseBlock):
                # Emit tool invocation as step
                tool_info = f"[tool: {block.name}]"
                await self._events.on_output(
                    session_id,
                    "combined",
                    f"{tool_info}\n",
                    kind="step",
                    is_final=False,
                )
            elif isinstance(block, ToolResultBlock):
                # Emit truncated tool result
                content = block.content
                if isinstance(content, str):
                    truncated = content[:500] + "..." if len(content) > 500 else content
                    prefix = "[error] " if block.is_error else "[result] "
                    await self._events.on_output(
                        session_id,
                        "combined",
                        f"{prefix}{truncated}\n",
                        kind="step",
                        is_final=False,
                    )
            elif isinstance(block, ThinkingBlock):
                # Emit thinking output
                if block.thinking:
                    await self._events.on_output(
                        session_id,
                        "combined",
                        f"[thinking] {block.thinking}\n",
                        kind="step",
                        is_final=False,
                    )

    async def _handle_result_message(
        self, session_id: str, message: ResultMessage
    ) -> None:
        """Handle ResultMessage (completion with usage stats)."""
        # Emit usage metadata
        if message.usage:
            input_tokens = message.usage.get("input_tokens", 0)
            output_tokens = message.usage.get("output_tokens", 0)
            await self._events.on_metadata(
                session_id,
                "tokens",
                {"input": input_tokens, "output": output_tokens},
                f"input: {input_tokens}, output: {output_tokens}",
            )

        if message.total_cost_usd is not None:
            await self._events.on_metadata(
                session_id,
                "cost",
                message.total_cost_usd,
                f"${message.total_cost_usd:.4f}",
            )

        # Check for errors
        if message.is_error:
            await self._events.on_error(
                session_id,
                "CLAUDE_RESULT_ERROR",
                message.result or "Unknown error",
            )

    async def _heartbeat_loop(self, session_id: str, start_time: float) -> None:
        """Emit periodic heartbeats while the query is active.

        Args:
            session_id: Internal session identifier.
            start_time: Monotonic timestamp when query started.
        """
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                elapsed = time.monotonic() - start_time
                await self._events.on_heartbeat(session_id, elapsed, done=False)
        except asyncio.CancelledError:
            pass
