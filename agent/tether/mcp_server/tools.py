"""MCP tool definitions and execution.

This module wraps the internal API endpoints as MCP tools, allowing
agents like Claude Code to interact with Tether.

The module contains two groups of tools:

1. Agent self-registration tools (original): for an agent to register itself
   with Tether and receive/send messages from/to a human supervisor.
   Tools: create_session, send_output, request_approval, check_input.

2. Remote agent execution tools (new): for an orchestrating agent to create
   and control other agent sessions running on this Tether server.
   Tools: run_agent, get_session_status, get_session_output, send_followup,
   get_diff, stop_session.
"""

import asyncio

import httpx

from tether.settings import settings


def get_tool_definitions() -> list[dict]:
    """Get MCP tool definitions.

    Returns:
        List of tool definition dicts in MCP format.
    """
    return [
        {
            "name": "create_session",
            "description": "Create a new Tether session for an external agent",
            "input_schema": {
                "type": "object",
                "properties": {
                    "agent_name": {
                        "type": "string",
                        "description": "Display name for the agent",
                    },
                    "agent_type": {
                        "type": "string",
                        "description": "Type of agent (e.g., 'claude_code', 'custom')",
                    },
                    "session_name": {
                        "type": "string",
                        "description": "Name for the session",
                    },
                    "platform": {
                        "type": "string",
                        "description": "Messaging platform (default: 'telegram')",
                        "default": "telegram",
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Optional workspace directory",
                    },
                },
                "required": ["agent_name", "agent_type", "session_name"],
            },
        },
        {
            "name": "send_output",
            "description": "Send output text to a Tether session",
            "input_schema": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Tether session ID",
                    },
                    "text": {
                        "type": "string",
                        "description": "Output text to send",
                    },
                },
                "required": ["session_id", "text"],
            },
        },
        {
            "name": "request_approval",
            "description": "Request approval from a human via Tether",
            "input_schema": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Tether session ID",
                    },
                    "title": {
                        "type": "string",
                        "description": "Approval request title",
                    },
                    "description": {
                        "type": "string",
                        "description": "Detailed description of what needs approval",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of option labels (e.g., ['Allow', 'Deny'])",
                    },
                    "timeout_s": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 300)",
                        "default": 300,
                    },
                },
                "required": ["session_id", "title", "description", "options"],
            },
        },
        {
            "name": "check_input",
            "description": "Check for pending human input or approval responses",
            "input_schema": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Tether session ID",
                    },
                    "since_seq": {
                        "type": "integer",
                        "description": "Only return events after this sequence number",
                        "default": 0,
                    },
                },
                "required": ["session_id"],
            },
        },
        # --- Remote agent execution tools ---
        {
            "name": "run_agent",
            "description": (
                "Create a session, optionally clone a repo or use an existing directory, "
                "start an agent with a prompt, and return the session ID. "
                "Use get_session_status to poll progress and get_session_output for results."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Task prompt for the agent",
                    },
                    "clone_url": {
                        "type": "string",
                        "description": "Git URL to clone (e.g. git@github.com:user/repo.git)",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch to check out after cloning",
                    },
                    "directory": {
                        "type": "string",
                        "description": "Existing local directory to use instead of cloning",
                    },
                    "adapter": {
                        "type": "string",
                        "description": "Runner adapter (e.g. claude_auto, claude_subprocess, pi_rpc)",
                    },
                    "approval_mode": {
                        "type": "integer",
                        "description": "0=interactive, 1=edits only, 2=full auto (default: 2)",
                        "default": 2,
                    },
                    "wait": {
                        "type": "boolean",
                        "description": (
                            "If true, block until the agent finishes its first turn "
                            "(reaches AWAITING_INPUT or a terminal state). Default: false."
                        ),
                        "default": False,
                    },
                    "wait_timeout_s": {
                        "type": "integer",
                        "description": "Seconds to wait when wait=true before giving up (default: 300)",
                        "default": 300,
                    },
                },
                "required": ["prompt"],
            },
        },
        {
            "name": "get_session_status",
            "description": "Get the current state and metadata for a Tether agent session",
            "input_schema": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Tether session ID returned by run_agent",
                    },
                },
                "required": ["session_id"],
            },
        },
        {
            "name": "get_session_output",
            "description": "Get output events produced by an agent session",
            "input_schema": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Tether session ID",
                    },
                    "since_seq": {
                        "type": "integer",
                        "description": "Only return events after this sequence number (default: 0)",
                        "default": 0,
                    },
                },
                "required": ["session_id"],
            },
        },
        {
            "name": "send_followup",
            "description": "Send follow-up input to a session that is waiting for input (AWAITING_INPUT state)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Tether session ID",
                    },
                    "text": {
                        "type": "string",
                        "description": "Follow-up message to send",
                    },
                },
                "required": ["session_id", "text"],
            },
        },
        {
            "name": "get_diff",
            "description": "Get the git diff from an agent session's workspace",
            "input_schema": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Tether session ID",
                    },
                },
                "required": ["session_id"],
            },
        },
        {
            "name": "stop_session",
            "description": "Interrupt a running agent session (sends an interrupt signal)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Tether session ID",
                    },
                },
                "required": ["session_id"],
            },
        },
    ]


def _resolve_base_url() -> str:
    """Return the Tether API base URL.

    Checks TETHER_API_URL first so the MCP server can point at a remote
    Tether instance.  Falls back to localhost:{port}.
    """
    import os

    explicit = os.environ.get("TETHER_API_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    return f"http://localhost:{settings.port()}"


async def _wait_for_terminal(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict | None,
    session_id: str,
    timeout_s: int,
) -> dict:
    """Poll session status until it leaves RUNNING state or timeout expires.

    Returns the final session dict.
    """
    terminal_states = {"awaiting_input", "done", "error", "created"}
    deadline = asyncio.get_event_loop().time() + timeout_s
    poll_interval = 2.0  # seconds between polls
    while True:
        resp = await client.get(
            f"{base_url}/api/sessions/{session_id}",
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        state = (data.get("state") or "").lower()
        if state in terminal_states or asyncio.get_event_loop().time() >= deadline:
            return data
        await asyncio.sleep(poll_interval)


async def execute_tool(tool_name: str, arguments: dict) -> dict:
    """Execute an MCP tool by calling the internal API.

    Args:
        tool_name: Name of the tool to execute.
        arguments: Tool arguments.

    Returns:
        Tool execution result.

    Raises:
        ValueError: If tool name is unknown.
        httpx.HTTPError: If API call fails.
    """
    base_url = _resolve_base_url()
    token = settings.token()
    headers = {"Authorization": f"Bearer {token}"} if token else None

    async with httpx.AsyncClient() as client:
        if tool_name == "create_session":
            response = await client.post(
                f"{base_url}/api/sessions",
                headers=headers,
                json={
                    "agent_name": arguments["agent_name"],
                    "agent_type": arguments["agent_type"],
                    "agent_workspace": arguments.get("workspace"),
                    "session_name": arguments["session_name"],
                    "platform": arguments.get("platform", "telegram"),
                },
            )
            response.raise_for_status()
            return response.json()

        elif tool_name == "send_output":
            session_id = arguments["session_id"]
            response = await client.post(
                f"{base_url}/api/sessions/{session_id}/events",
                headers=headers,
                json={
                    "type": "output",
                    "data": {
                        "text": arguments["text"],
                    },
                },
            )
            response.raise_for_status()
            return response.json()

        elif tool_name == "request_approval":
            session_id = arguments["session_id"]
            title = arguments["title"]
            description = arguments["description"]
            options = arguments["options"]

            # Format as AskUserQuestion so the subscriber creates a
            # choice request with the actual option labels instead of
            # falling back to generic Allow/Deny.
            response = await client.post(
                f"{base_url}/api/sessions/{session_id}/events",
                headers=headers,
                json={
                    "type": "permission_request",
                    "data": {
                        "tool_name": "AskUserQuestion",
                        "tool_input": {
                            "questions": [
                                {
                                    "header": title,
                                    "question": description,
                                    "options": [
                                        {"label": opt} for opt in options
                                    ],
                                }
                            ],
                        },
                    },
                },
            )
            response.raise_for_status()
            return response.json()

        elif tool_name == "check_input":
            session_id = arguments["session_id"]
            since_seq = arguments.get("since_seq", 0)
            response = await client.get(
                f"{base_url}/api/sessions/{session_id}/events/poll",
                headers=headers,
                params={
                    "since_seq": since_seq,
                    "types": "user_input,permission_resolved",
                },
            )
            response.raise_for_status()
            return response.json()

        # --- Remote agent execution tools ---

        elif tool_name == "run_agent":
            prompt = arguments["prompt"]
            clone_url = arguments.get("clone_url")
            branch = arguments.get("branch")
            directory = arguments.get("directory")
            adapter = arguments.get("adapter")
            approval_mode = arguments.get("approval_mode", 2)
            wait = arguments.get("wait", False)
            wait_timeout_s = arguments.get("wait_timeout_s", 300)

            # Step 1: create the session
            create_payload: dict = {}
            if clone_url:
                create_payload["clone_url"] = clone_url
            if branch:
                create_payload["clone_branch"] = branch
            if directory:
                create_payload["directory"] = directory
            if adapter:
                create_payload["adapter"] = adapter
            if approval_mode is not None:
                create_payload["approval_mode"] = approval_mode

            create_resp = await client.post(
                f"{base_url}/api/sessions",
                headers=headers,
                json=create_payload,
            )
            create_resp.raise_for_status()
            session = create_resp.json()
            session_id = session["id"]

            # Step 2: start the session with the prompt
            start_payload = {
                "prompt": prompt,
                "approval_choice": approval_mode if approval_mode in (0, 1, 2) else 2,
            }
            start_resp = await client.post(
                f"{base_url}/api/sessions/{session_id}/start",
                headers=headers,
                json=start_payload,
            )
            start_resp.raise_for_status()
            result = start_resp.json()

            if wait:
                result = await _wait_for_terminal(
                    client, base_url, headers, session_id, wait_timeout_s
                )

            return {"session_id": session_id, "session": result}

        elif tool_name == "get_session_status":
            session_id = arguments["session_id"]
            response = await client.get(
                f"{base_url}/api/sessions/{session_id}",
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
            return {
                "session_id": session_id,
                "state": data.get("state"),
                "name": data.get("name"),
                "summary": data.get("summary"),
                "started_at": data.get("started_at"),
                "ended_at": data.get("ended_at"),
                "last_activity_at": data.get("last_activity_at"),
                "exit_code": data.get("exit_code"),
                "directory": data.get("directory"),
                "working_branch": data.get("working_branch"),
                "clone_url": data.get("clone_url"),
                "has_pending_permission": data.get("has_pending_permission"),
                "adapter": data.get("adapter"),
            }

        elif tool_name == "get_session_output":
            session_id = arguments["session_id"]
            since_seq = arguments.get("since_seq", 0)
            response = await client.get(
                f"{base_url}/api/sessions/{session_id}/events/poll",
                headers=headers,
                params={
                    "since_seq": since_seq,
                    "types": "output,state,error",
                },
            )
            response.raise_for_status()
            return response.json()

        elif tool_name == "send_followup":
            session_id = arguments["session_id"]
            text = arguments["text"]
            response = await client.post(
                f"{base_url}/api/sessions/{session_id}/input",
                headers=headers,
                json={"text": text},
            )
            response.raise_for_status()
            return response.json()

        elif tool_name == "get_diff":
            session_id = arguments["session_id"]
            response = await client.get(
                f"{base_url}/api/sessions/{session_id}/diff",
                headers=headers,
            )
            response.raise_for_status()
            return response.json()

        elif tool_name == "stop_session":
            session_id = arguments["session_id"]
            response = await client.post(
                f"{base_url}/api/sessions/{session_id}/interrupt",
                headers=headers,
            )
            response.raise_for_status()
            return response.json()

        else:
            raise ValueError(f"Unknown tool: {tool_name}")
