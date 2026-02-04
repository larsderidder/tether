"""State management for Telegram session-to-topic mappings."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class TopicMapping:
    """Mapping between a session and a Telegram forum topic."""

    topic_id: int
    name: str
    created_at: str


class StateManager:
    """Manages persistent state for session↔topic mappings.

    Stores mappings as JSON to preserve connections between sessions
    and their forum topics across restarts.
    """

    def __init__(self, path: str):
        self._path = Path(path)
        self._mappings: dict[str, TopicMapping] = {}
        self._topic_to_session: dict[int, str] = {}

    def load(self) -> None:
        """Load state from disk."""
        if not self._path.exists():
            logger.info("No Telegram state file found, starting fresh", path=str(self._path))
            return

        try:
            with self._path.open() as f:
                data = json.load(f)

            mappings_data = data.get("mappings", {})
            for session_id, mapping_data in mappings_data.items():
                mapping = TopicMapping(
                    topic_id=mapping_data["topic_id"],
                    name=mapping_data["name"],
                    created_at=mapping_data["created_at"],
                )
                self._mappings[session_id] = mapping
                self._topic_to_session[mapping.topic_id] = session_id

            logger.info("Loaded Telegram state", mapping_count=len(self._mappings))
        except Exception:
            logger.exception("Failed to load Telegram state")

    def save(self) -> None:
        """Save state to disk."""
        try:
            data = {
                "mappings": {
                    session_id: asdict(mapping)
                    for session_id, mapping in self._mappings.items()
                }
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            logger.exception("Failed to save Telegram state")

    def get_topic_for_session(self, session_id: str) -> int | None:
        """Get the topic ID for a session."""
        mapping = self._mappings.get(session_id)
        return mapping.topic_id if mapping else None

    def set_topic_for_session(self, session_id: str, topic_id: int, name: str) -> None:
        """Record a topic mapping for a session."""
        mapping = TopicMapping(
            topic_id=topic_id,
            name=name,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._mappings[session_id] = mapping
        self._topic_to_session[topic_id] = session_id
        self.save()

    def get_session_for_topic(self, topic_id: int) -> str | None:
        """Get the session ID for a topic."""
        return self._topic_to_session.get(topic_id)
