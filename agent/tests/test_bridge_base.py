"""Tests for BridgeInterface base class shared logic."""

import time
from unittest.mock import AsyncMock, patch

import pytest

from tether.bridges.base import (
    ApprovalRequest,
    BridgeInterface,
    _ALLOW_ALL_DURATION_S,
    _EXTERNAL_PAGE_SIZE,
)


class ConcreteBridge(BridgeInterface):
    """Minimal concrete bridge for testing shared base logic."""

    def __init__(self):
        super().__init__()
        self.output_calls: list[dict] = []
        self.approval_calls: list[dict] = []
        self.status_calls: list[dict] = []

    async def on_output(
        self, session_id: str, text: str, metadata: dict | None = None
    ) -> None:
        self.output_calls.append({"session_id": session_id, "text": text})

    async def on_approval_request(
        self, session_id: str, request: ApprovalRequest
    ) -> None:
        self.approval_calls.append({"session_id": session_id, "request": request})

    async def on_status_change(
        self, session_id: str, status: str, metadata: dict | None = None
    ) -> None:
        self.status_calls.append({"session_id": session_id, "status": status})

    async def create_thread(self, session_id: str, session_name: str) -> dict:
        return {"thread_id": f"thread_{session_id}", "platform": "test"}


class TestAutoApprove:
    """Test auto-approve timer logic in BridgeInterface."""

    def test_no_auto_approve_by_default(self) -> None:
        bridge = ConcreteBridge()
        assert bridge.check_auto_approve("sess_1", "Read") is None

    def test_allow_all_approves_any_tool(self) -> None:
        bridge = ConcreteBridge()
        bridge.set_allow_all("sess_1")
        assert bridge.check_auto_approve("sess_1", "Read") == "Allow All"
        assert bridge.check_auto_approve("sess_1", "Edit") == "Allow All"
        assert bridge.check_auto_approve("sess_1", "Write") == "Allow All"

    def test_allow_all_does_not_affect_other_sessions(self) -> None:
        bridge = ConcreteBridge()
        bridge.set_allow_all("sess_1")
        assert bridge.check_auto_approve("sess_2", "Read") is None

    def test_allow_tool_approves_specific_tool(self) -> None:
        bridge = ConcreteBridge()
        bridge.set_allow_tool("sess_1", "Read")
        assert bridge.check_auto_approve("sess_1", "Read") == "Allow Read"
        assert bridge.check_auto_approve("sess_1", "Edit") is None

    def test_allow_tool_does_not_affect_other_sessions(self) -> None:
        bridge = ConcreteBridge()
        bridge.set_allow_tool("sess_1", "Read")
        assert bridge.check_auto_approve("sess_2", "Read") is None

    def test_allow_all_expires(self) -> None:
        bridge = ConcreteBridge()
        # Set expiry in the past
        bridge._allow_all_until["sess_1"] = time.time() - 1
        assert bridge.check_auto_approve("sess_1", "Read") is None

    def test_allow_tool_expires(self) -> None:
        bridge = ConcreteBridge()
        bridge._allow_tool_until["sess_1"] = {"Read": time.time() - 1}
        assert bridge.check_auto_approve("sess_1", "Read") is None

    def test_allow_all_duration(self) -> None:
        bridge = ConcreteBridge()
        before = time.time()
        bridge.set_allow_all("sess_1")
        after = time.time()
        expiry = bridge._allow_all_until["sess_1"]
        assert before + _ALLOW_ALL_DURATION_S <= expiry <= after + _ALLOW_ALL_DURATION_S

    def test_allow_tool_duration(self) -> None:
        bridge = ConcreteBridge()
        before = time.time()
        bridge.set_allow_tool("sess_1", "Read")
        after = time.time()
        expiry = bridge._allow_tool_until["sess_1"]["Read"]
        assert before + _ALLOW_ALL_DURATION_S <= expiry <= after + _ALLOW_ALL_DURATION_S

    def test_multiple_tools_can_be_allowed(self) -> None:
        bridge = ConcreteBridge()
        bridge.set_allow_tool("sess_1", "Read")
        bridge.set_allow_tool("sess_1", "Edit")
        assert bridge.check_auto_approve("sess_1", "Read") == "Allow Read"
        assert bridge.check_auto_approve("sess_1", "Edit") == "Allow Edit"
        assert bridge.check_auto_approve("sess_1", "Write") is None

    def test_allow_all_takes_precedence_over_tool(self) -> None:
        bridge = ConcreteBridge()
        bridge.set_allow_tool("sess_1", "Read")
        bridge.set_allow_all("sess_1")
        # Allow All should match first
        assert bridge.check_auto_approve("sess_1", "Read") == "Allow All"
        assert bridge.check_auto_approve("sess_1", "Write") == "Allow All"


class TestSessionRemoved:
    """Test on_session_removed cleanup."""

    @pytest.mark.anyio
    async def test_cleans_allow_all_timer(self) -> None:
        bridge = ConcreteBridge()
        bridge.set_allow_all("sess_1")
        assert "sess_1" in bridge._allow_all_until
        await bridge.on_session_removed("sess_1")
        assert "sess_1" not in bridge._allow_all_until

    @pytest.mark.anyio
    async def test_cleans_allow_tool_timers(self) -> None:
        bridge = ConcreteBridge()
        bridge.set_allow_tool("sess_1", "Read")
        bridge.set_allow_tool("sess_1", "Edit")
        assert "sess_1" in bridge._allow_tool_until
        await bridge.on_session_removed("sess_1")
        assert "sess_1" not in bridge._allow_tool_until

    @pytest.mark.anyio
    async def test_safe_for_unknown_session(self) -> None:
        bridge = ConcreteBridge()
        # Should not raise
        await bridge.on_session_removed("nonexistent")

    @pytest.mark.anyio
    async def test_does_not_affect_other_sessions(self) -> None:
        bridge = ConcreteBridge()
        bridge.set_allow_all("sess_1")
        bridge.set_allow_all("sess_2")
        await bridge.on_session_removed("sess_1")
        assert "sess_2" in bridge._allow_all_until


class TestAutoApproveAction:
    """Test _auto_approve makes the correct API call."""

    @pytest.mark.anyio
    async def test_auto_approve_calls_api(self) -> None:
        bridge = ConcreteBridge()
        request = ApprovalRequest(
            request_id="req_1",
            title="Read",
            description="Read file.txt",
            options=["Allow", "Deny"],
        )

        mock_response = AsyncMock()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await bridge._auto_approve("sess_1", request, reason="Allow All")

        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert "/permission" in call_kwargs.args[0]
        json_body = call_kwargs.kwargs["json"]
        assert json_body["request_id"] == "req_1"
        assert json_body["allow"] is True
        assert "Allow All" in json_body["message"]


class TestUsageFormatting:
    """Test _format_usage_text output."""

    def test_format_with_cost(self) -> None:
        bridge = ConcreteBridge()
        usage = {"input_tokens": 1000, "output_tokens": 500, "total_cost_usd": 0.0123}
        text = bridge._format_usage_text(usage)
        assert "1,000" in text
        assert "500" in text
        assert "1,500" in text
        assert "$0.0123" in text

    def test_format_without_cost(self) -> None:
        bridge = ConcreteBridge()
        usage = {"input_tokens": 100, "output_tokens": 50, "total_cost_usd": 0.0}
        text = bridge._format_usage_text(usage)
        assert "not tracked" in text

    def test_format_with_missing_fields(self) -> None:
        bridge = ConcreteBridge()
        usage = {}
        text = bridge._format_usage_text(usage)
        assert "0" in text
        assert "not tracked" in text

    def test_format_large_numbers(self) -> None:
        bridge = ConcreteBridge()
        usage = {
            "input_tokens": 1_234_567,
            "output_tokens": 890_123,
            "total_cost_usd": 12.5,
        }
        text = bridge._format_usage_text(usage)
        assert "1,234,567" in text
        assert "890,123" in text
        assert "2,124,690" in text


class TestExternalSessionPagination:
    """Test _set_external_view and _format_external_page."""

    def _make_sessions(self, count: int) -> list[dict]:
        return [
            {
                "id": f"ext_{i}",
                "runner_type": "claude_local",
                "directory": f"/home/user/project{i}",
                "is_running": i % 2 == 0,
                "first_prompt": f"Prompt for session {i}",
            }
            for i in range(count)
        ]

    def test_empty_sessions(self) -> None:
        bridge = ConcreteBridge()
        text, page, total = bridge._format_external_page(1)
        assert "No external sessions found" in text
        assert page == 1
        assert total == 1

    def test_empty_with_query(self) -> None:
        bridge = ConcreteBridge()
        bridge._cached_external = self._make_sessions(5)
        bridge._set_external_view("nonexistent")
        text, page, total = bridge._format_external_page(1)
        assert "No external sessions match" in text
        assert "nonexistent" in text

    def test_single_page(self) -> None:
        bridge = ConcreteBridge()
        bridge._cached_external = self._make_sessions(3)
        bridge._set_external_view(None)
        text, page, total = bridge._format_external_page(1)
        assert page == 1
        assert total == 1
        assert "project0" in text
        assert "project2" in text

    def test_multiple_pages(self) -> None:
        bridge = ConcreteBridge()
        bridge._cached_external = self._make_sessions(25)
        bridge._set_external_view(None)

        text1, page1, total1 = bridge._format_external_page(1)
        assert page1 == 1
        assert total1 == 3
        assert "page 1/3" in text1

        text2, page2, total2 = bridge._format_external_page(2)
        assert page2 == 2
        assert "page 2/3" in text2

    def test_page_clamped_to_bounds(self) -> None:
        bridge = ConcreteBridge()
        bridge._cached_external = self._make_sessions(5)
        bridge._set_external_view(None)
        _, page, _ = bridge._format_external_page(999)
        assert page == 1  # Only 1 page, so clamped

    def test_custom_commands(self) -> None:
        bridge = ConcreteBridge()
        bridge._cached_external = []
        text, _, _ = bridge._format_external_page(
            1, attach_cmd="/attach", list_cmd="/list"
        )
        assert "/list" in text

    def test_custom_commands_with_sessions(self) -> None:
        bridge = ConcreteBridge()
        bridge._cached_external = self._make_sessions(3)
        bridge._set_external_view(None)
        text, _, _ = bridge._format_external_page(
            1, attach_cmd="/attach", list_cmd="/list"
        )
        assert "/attach" in text

    def test_directory_search_filter(self) -> None:
        bridge = ConcreteBridge()
        bridge._cached_external = self._make_sessions(5)
        bridge._set_external_view("project2")
        text, page, total = bridge._format_external_page(1)
        assert "project2" in text
        assert "[search: project2]" in text
        # Only project2 should match
        assert len(bridge._external_view) == 1

    def test_relative_time_shown(self) -> None:
        bridge = ConcreteBridge()
        bridge._cached_external = self._make_sessions(2)
        bridge._set_external_view(None)
        text, _, _ = bridge._format_external_page(1)
        # Directory names should be shown without runner type or status emoji
        assert "project0" in text
        assert "project1" in text
        # No status emojis
        assert "\U0001f7e2" not in text
        assert "\u26aa" not in text


class TestOnTypingDefault:
    """Test default on_typing is a no-op."""

    @pytest.mark.anyio
    async def test_on_typing_does_nothing(self) -> None:
        bridge = ConcreteBridge()
        # Should not raise
        await bridge.on_typing("sess_1")


class TestInitState:
    """Test __init__ sets up correct initial state."""

    def test_initial_state(self) -> None:
        bridge = ConcreteBridge()
        assert bridge._cached_external == []
        assert bridge._external_query is None
        assert bridge._external_view == []
        assert bridge._allow_all_until == {}
        assert bridge._allow_tool_until == {}
        assert bridge._pending_permissions == {}


class TestPendingPermissions:
    """Test pending permission tracking."""

    def _make_request(self, rid: str = "req_1") -> ApprovalRequest:
        return ApprovalRequest(
            request_id=rid,
            title="Read",
            description="Read file",
            options=["Allow", "Deny"],
        )

    def test_no_pending_by_default(self) -> None:
        bridge = ConcreteBridge()
        assert bridge.get_pending_permission("sess_1") is None

    def test_set_and_get(self) -> None:
        bridge = ConcreteBridge()
        req = self._make_request()
        bridge.set_pending_permission("sess_1", req)
        assert bridge.get_pending_permission("sess_1") is req

    def test_clear(self) -> None:
        bridge = ConcreteBridge()
        bridge.set_pending_permission("sess_1", self._make_request())
        bridge.clear_pending_permission("sess_1")
        assert bridge.get_pending_permission("sess_1") is None

    def test_clear_unknown_safe(self) -> None:
        bridge = ConcreteBridge()
        bridge.clear_pending_permission("nonexistent")  # should not raise

    @pytest.mark.anyio
    async def test_session_removed_clears_pending(self) -> None:
        bridge = ConcreteBridge()
        bridge.set_pending_permission("sess_1", self._make_request())
        await bridge.on_session_removed("sess_1")
        assert bridge.get_pending_permission("sess_1") is None

    def test_multiple_sessions(self) -> None:
        bridge = ConcreteBridge()
        req1 = self._make_request("req_1")
        req2 = self._make_request("req_2")
        bridge.set_pending_permission("sess_1", req1)
        bridge.set_pending_permission("sess_2", req2)
        assert bridge.get_pending_permission("sess_1") is req1
        assert bridge.get_pending_permission("sess_2") is req2
        bridge.clear_pending_permission("sess_1")
        assert bridge.get_pending_permission("sess_1") is None
        assert bridge.get_pending_permission("sess_2") is req2


class TestParseApprovalText:
    """Test parse_approval_text shared helper."""

    def test_allow(self) -> None:
        bridge = ConcreteBridge()
        assert bridge.parse_approval_text("allow") == {
            "allow": True,
            "reason": None,
            "timer": None,
        }

    def test_approve(self) -> None:
        bridge = ConcreteBridge()
        assert bridge.parse_approval_text("approve") == {
            "allow": True,
            "reason": None,
            "timer": None,
        }

    def test_yes(self) -> None:
        bridge = ConcreteBridge()
        assert bridge.parse_approval_text("yes") == {
            "allow": True,
            "reason": None,
            "timer": None,
        }

    def test_deny_bare(self) -> None:
        bridge = ConcreteBridge()
        assert bridge.parse_approval_text("deny") == {
            "allow": False,
            "reason": None,
            "timer": None,
        }

    def test_reject(self) -> None:
        bridge = ConcreteBridge()
        assert bridge.parse_approval_text("reject") == {
            "allow": False,
            "reason": None,
            "timer": None,
        }

    def test_no(self) -> None:
        bridge = ConcreteBridge()
        assert bridge.parse_approval_text("no") == {
            "allow": False,
            "reason": None,
            "timer": None,
        }

    def test_deny_with_colon_reason(self) -> None:
        bridge = ConcreteBridge()
        result = bridge.parse_approval_text("deny: use cookies instead")
        assert result == {
            "allow": False,
            "reason": "use cookies instead",
            "timer": None,
        }

    def test_deny_with_space_reason(self) -> None:
        bridge = ConcreteBridge()
        result = bridge.parse_approval_text("deny bad approach, try again")
        assert result == {
            "allow": False,
            "reason": "bad approach, try again",
            "timer": None,
        }

    def test_reject_with_reason(self) -> None:
        bridge = ConcreteBridge()
        result = bridge.parse_approval_text("reject: not safe")
        assert result == {"allow": False, "reason": "not safe", "timer": None}

    def test_allow_all(self) -> None:
        bridge = ConcreteBridge()
        assert bridge.parse_approval_text("allow all") == {
            "allow": True,
            "reason": None,
            "timer": "all",
        }

    def test_allow_tool(self) -> None:
        bridge = ConcreteBridge()
        assert bridge.parse_approval_text("allow Read") == {
            "allow": True,
            "reason": None,
            "timer": "Read",
        }

    def test_case_insensitive(self) -> None:
        bridge = ConcreteBridge()
        assert bridge.parse_approval_text("ALLOW")["allow"] is True
        assert bridge.parse_approval_text("DENY")["allow"] is False
        assert bridge.parse_approval_text("Allow All")["timer"] == "all"

    def test_non_approval_text_returns_none(self) -> None:
        bridge = ConcreteBridge()
        assert bridge.parse_approval_text("hello world") is None
        assert bridge.parse_approval_text("fix the bug") is None
        assert bridge.parse_approval_text("") is None

    def test_whitespace_handling(self) -> None:
        bridge = ConcreteBridge()
        assert bridge.parse_approval_text("  allow  ")["allow"] is True
        result = bridge.parse_approval_text("  deny:  reason here  ")
        assert result["reason"] == "reason here"

    def test_deny_colon_empty_reason(self) -> None:
        bridge = ConcreteBridge()
        result = bridge.parse_approval_text("deny:")
        assert result == {"allow": False, "reason": None, "timer": None}
