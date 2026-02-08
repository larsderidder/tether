"""MCP tool definitions and execution.

This module wraps the internal API endpoints as MCP tools, allowing
agents like Claude Code to interact with Tether.
"""

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
    ]


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
    base_url = f"http://localhost:{settings.port()}"
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

        else:
            raise ValueError(f"Unknown tool: {tool_name}")
