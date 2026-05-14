"""File-based JSON storage for Prompt Templates (CTR-0047)."""

from datetime import UTC, datetime
import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


class TemplateStorage:
    def __init__(self, templates_dir: Path) -> None:
        self._dir = templates_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def list_all(self) -> list[dict[str, Any]]:
        templates: list[dict[str, Any]] = []
        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                templates.append(data)
            except (json.JSONDecodeError, OSError):
                logger.warning("Skipping malformed template file: %s", path)
        templates.sort(key=lambda t: t.get("updated_at", ""), reverse=True)
        return templates

    def get(self, template_id: str) -> dict[str, Any] | None:
        path = self._dir / f"{template_id}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to read template file: %s", path)
            return None

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        template = {
            "id": str(uuid4()),
            "name": data["name"],
            "description": data.get("description", ""),
            "category": data.get("category", ""),
            "body": data["body"],
            "created_at": now,
            "updated_at": now,
        }
        path = self._dir / f"{template['id']}.json"
        path.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Created template %s: %s", template["id"], template["name"])
        return template

    def update(self, template_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        path = self._dir / f"{template_id}.json"
        if not path.is_file():
            return None
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        existing["name"] = data["name"]
        existing["body"] = data["body"]
        existing["description"] = data.get("description", "")
        existing["category"] = data.get("category", "")
        existing["updated_at"] = datetime.now(UTC).isoformat()

        path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Updated template %s: %s", template_id, existing["name"])
        return existing

    def delete(self, template_id: str) -> bool:
        path = self._dir / f"{template_id}.json"
        if not path.is_file():
            return False
        try:
            path.unlink()
            logger.info("Deleted template %s", template_id)
            return True
        except OSError:
            logger.warning("Failed to delete template file: %s", path)
            return False
