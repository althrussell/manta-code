from __future__ import annotations

from pathlib import Path

from .config import project_manta_dir
from .schemas import SessionEvent, new_session_id


class MantaSession:
    def __init__(self, root: Path | None = None, session_id: str | None = None):
        self.root = root or Path.cwd()
        self.session_id = session_id or new_session_id()
        self.manta_dir = project_manta_dir(self.root)
        self.sessions_dir = self.manta_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.sessions_dir / f"{self.session_id}.jsonl"

    def event(self, event_type: str, payload: dict) -> SessionEvent:
        event = SessionEvent(session_id=self.session_id, type=event_type, payload=payload)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(event.model_dump_json() + "\n")
        return event

    @staticmethod
    def last_session_path(root: Path | None = None) -> Path | None:
        sessions = sorted((project_manta_dir(root) / "sessions").glob("*.jsonl"))
        return sessions[-1] if sessions else None
