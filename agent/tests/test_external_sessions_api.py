"""Test external sessions API endpoints."""
import httpx
import pytest


@pytest.mark.anyio
async def test_list_external_sessions_includes_last_prompt(
    api_client: httpx.AsyncClient,
) -> None:
    """Test that /api/external-sessions includes last_prompt field."""
    response = await api_client.get("/api/external-sessions?limit=5")
    
    assert response.status_code == 200
    sessions = response.json()
    assert isinstance(sessions, list)
    
    # Check that all sessions have the last_prompt field (can be null)
    for session in sessions:
        assert "last_prompt" in session, f"Session {session.get('id')} missing last_prompt"
        assert "first_prompt" in session
        assert "directory" in session
