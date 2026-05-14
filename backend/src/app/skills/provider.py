"""Agent Skills provider factory (CTR-0043, PRP-0024).

Creates a MAF SkillsProvider for file-based Agent Skills discovery.
SkillsProvider implements progressive disclosure:
  1. Advertise -- skill names + descriptions injected into system prompt (~100 tokens/skill)
  2. Load -- full SKILL.md body returned via load_skill tool
  3. Read resources -- supplementary files returned via read_skill_resource tool

Skills are discovered from SKILLS_DIR (default: ".skills"), searching up to 2 levels deep.
"""

import logging
from pathlib import Path

from agent_framework import SkillsProvider

from app.core.config import settings

logger = logging.getLogger(__name__)


def create_skills_provider() -> SkillsProvider | None:
    """Create SkillsProvider if SKILLS_DIR exists and is a directory.

    Returns:
        SkillsProvider instance if skills directory exists, None otherwise.
    """
    skills_path = Path(settings.skills_dir)

    if not skills_path.is_dir():
        logger.info("Skills directory not found: %s (skipping SkillsProvider)", skills_path)
        return None

    provider = SkillsProvider(skill_paths=skills_path)
    logger.info("SkillsProvider created (skills_dir=%s)", skills_path)
    return provider
