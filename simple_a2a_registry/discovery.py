"""
Profile and Skill discovery — scans the filesystem to generate
A2A Agent Cards from agent profiles.

Handles two levels of skill nesting:
  Flat:  <profiles_home>/skills/<name>/SKILL.md
  Deep:  <profiles_home>/skills/<category>/<name>/SKILL.md

Profile-local skills mirror the same shape under each profile's skills/ dir.
Global skills are linked to a profile by matching their category directory
name against the profile's local skill-category directories.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from simple_a2a_registry.models import make_agent_card, make_agent_skill

logger = logging.getLogger("a2a_registry.discovery")

# Directories/files to skip during scans
SKIP_DIRS = {".archive", ".curator_backups", ".hub", ".git", "__pycache__"}


def discover_profiles(profiles_home: str) -> List[Dict[str, Any]]:
    """Scan a profiles directory to build
    A2A Agent Cards for every discovered profile.

    Args:
        profiles_home: Base directory containing ``profiles/`` and ``skills/``
            subdirectories (e.g. ``~/.hermes`` for Hermes Agent compatibility).

    Returns:
        List of Agent Card dicts, one per profile.
    """
    home = Path(profiles_home).expanduser().resolve()
    profiles_dir = home / "profiles"
    skills_dir = home / "skills"
    agents: List[Dict[str, Any]] = []

    if not profiles_dir.is_dir():
        logger.warning("Profiles directory not found: %s", profiles_dir)
        return agents

    # Pre-build global skill index: {category: [skills]}
    global_skills_index = _build_global_skills_index(skills_dir)
    logger.debug(
        "Global skills index: %d categories", len(global_skills_index)
    )

    for prof_dir in sorted(profiles_dir.iterdir()):
        if not prof_dir.is_dir() or prof_dir.name.startswith("."):
            continue

        agent_id = f"a2a:{prof_dir.name}"
        name = prof_dir.name
        description = _read_profile_description(prof_dir)
        tags = _read_profile_tags(prof_dir)
        profile_categories = _get_profile_skill_categories(prof_dir)
        skills = _discover_profile_skills(
            agent_id, prof_dir, global_skills_index, profile_categories
        )

        agents.append(
            make_agent_card(
                agent_id=agent_id,
                name=name,
                description=description or f"A2A profile: {name}",
                skills=skills,
                provider={"organization": "A2A Agent", "url": ""},
                tags=tags or None,
            )
        )

    logger.info("Discovered %d agent(s) from %s", len(agents), profiles_dir)
    return agents


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_profile_description(prof_dir: Path) -> str:
    """Read description from ``profile.yaml``, falling back to ``SOUL.md``.

    Handles simple key-value, folded (``>-``), and quoted descriptions.
    """
    profile_yaml = prof_dir / "profile.yaml"
    if profile_yaml.exists():
        try:
            text = profile_yaml.read_text()
            desc = _extract_yaml_value(text, "description")
            if desc:
                return desc[:512]
        except Exception:
            logger.debug("Failed to read %s", profile_yaml, exc_info=True)

    # Fallback to SOUL.md first substantive paragraph
    soul_md = prof_dir / "SOUL.md"
    if soul_md.exists():
        try:
            lines = soul_md.read_text().splitlines()
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("#") or not stripped:
                    continue
                if len(stripped) > 10:
                    return stripped[:200]
        except Exception:
            pass

    return ""


def _extract_yaml_value(yaml_text: str, key: str) -> str:
    """Extract a YAML key's value from raw text using regex.

    Handles:
      key: value
      key: "quoted value"
      key: >
        folded value
        continues
      key: |
        literal block
    """
    # Multi-line folded/literal: key: >\n  ...
    m = re.search(
        rf"^{key}:\s*[>|]\s*\n((?:  .*\n?)+)",
        yaml_text,
        re.MULTILINE,
    )
    if m:
        parts = []
        for line in m.group(1).splitlines():
            stripped = line.strip()
            if stripped:
                parts.append(stripped)
        return " ".join(parts)

    # Single-line: key: value or key: "quoted value" with YAML indented continuation
    m = re.search(
        rf"^{key}:\s*\"(.+?)\"\s*$",
        yaml_text,
        re.MULTILINE,
    )
    if m:
        return m.group(1)

    # Plain key: value — may have indented continuation lines
    lines = yaml_text.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"^{key}:\s+\S", line):
            parts = [line.split(":", 1)[1].strip().strip("'\"")]
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.startswith(" ") and nxt.strip():
                    parts.append(nxt.strip())
                    j += 1
                else:
                    break
            return " ".join(parts)
        elif re.match(rf"^{key}:\s*$", line):
            pass

    return ""


def _read_profile_tags(prof_dir: Path) -> List[str]:
    """Read tags from ``config.yaml`` toolsets list.

    Matches:
      toolsets:
      - hermes-cli
      - terminal
    """
    config_yaml = prof_dir / "config.yaml"
    if not config_yaml.exists():
        return []
    try:
        text = config_yaml.read_text()
        m = re.search(
            r"^toolsets:\s*\n((?:\s*-\s+\S+\s*\n?)+)", text, re.MULTILINE
        )
        if m:
            return re.findall(r"- (\S+)", m.group(1))
    except Exception:
        pass
    return []


def _get_profile_skill_categories(prof_dir: Path) -> set[str]:
    """Return the set of category dir names under a profile's skills/.

    E.g. for ``coder/skills/software-development/spike/SKILL.md``,
    the category is ``software-development``.
    Flat skills (``skill/SKILL.md`` directly under skills/) use their
    own name as the category.
    """
    skills_dir = prof_dir / "skills"
    if not skills_dir.is_dir():
        return set()

    categories: set[str] = set()
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir() or child.name in SKIP_DIRS:
            continue
        categories.add(child.name)

    return categories


# ---------------------------------------------------------------------------
# Global skills index
# ---------------------------------------------------------------------------


def _build_global_skills_index(
    skills_dir: Path,
) -> Dict[str, List[Dict[str, Any]]]:
    """Build {category: [parsed_skills]} from a ``skills/`` directory.

    Category is the first path segment after ``skills_dir``:
    - ``skills/github/github-auth/SKILL.md`` → category ``github``
    - ``skills/dogfood/SKILL.md`` → category ``dogfood`` (flat)
    """
    if not skills_dir.is_dir():
        return {}

    index: Dict[str, List[Dict[str, Any]]] = {}

    for sk_path in sorted(skills_dir.rglob("SKILL.md")):
        if any(skip in sk_path.parts for skip in SKIP_DIRS):
            continue

        rel = sk_path.relative_to(skills_dir)
        category = rel.parts[0]

        fm = _parse_frontmatter(sk_path)
        if fm is None:
            continue

        skill_name = fm.get("name", sk_path.parent.name)

        tags = _get_skill_tags(fm)

        if category not in tags:
            tags.append(category)

        parsed = {
            "name": skill_name,
            "description": fm.get("description", ""),
            "tags": tags,
            "path": str(sk_path),
            "original_name": sk_path.parent.name,
        }

        index.setdefault(category, []).append(parsed)

    return index


def _get_skill_tags(fm: Dict[str, Any]) -> List[str]:
    """Extract tags from a parsed frontmatter dict.

    Checks in order:
    1. ``metadata.hermes.tags`` (Hermes Agent SKILL.md format)
    2. ``metadata.tags``
    3. Top-level ``tags`` (list or inline)
    4. ``category`` field
    """
    tags: List[str] = []

    # 1. metadata > hermes > tags
    meta = fm.get("metadata", {})
    if isinstance(meta, dict):
        hermes = meta.get("hermes", {})
        if isinstance(hermes, dict):
            hermes_tags = hermes.get("tags", [])
            if isinstance(hermes_tags, list):
                tags.extend(hermes_tags)
        meta_tags = meta.get("tags", [])
        if isinstance(meta_tags, list):
            tags.extend(meta_tags)

    # 3. Top-level tags (list or inline)
    top_tags = fm.get("tags", [])
    if isinstance(top_tags, list):
        tags.extend(top_tags)

    # 4. category field
    cat = fm.get("category")
    if cat and isinstance(cat, str) and cat not in tags:
        tags.append(cat)

    return tags


# ---------------------------------------------------------------------------
# Profile skill discovery
# ---------------------------------------------------------------------------


def _discover_profile_skills(
    agent_id: str,
    prof_dir: Path,
    global_skills_index: Dict[str, List[Dict[str, Any]]],
    profile_categories: set[str],
) -> List[Dict]:
    """Collect skills for a profile.

    1. Profile-local skills (recursively from profile/skills/)
    2. Global skills whose category matches profile_categories,
       skipping those already included locally (dedup by name).
    """
    seen_names: set[str] = set()
    skills: List[Dict] = []

    # 1. Profile-local skills
    local_skills_dir = prof_dir / "skills"
    if local_skills_dir.is_dir():
        for skill_file in sorted(local_skills_dir.rglob("SKILL.md")):
            if any(skip in skill_file.parts for skip in SKIP_DIRS):
                continue
            fm = _parse_frontmatter(skill_file)
            if fm is None:
                continue
            sname = fm.get("name", skill_file.parent.name)
            if sname in seen_names:
                continue
            seen_names.add(sname)
            tags = _get_skill_tags(fm)
            skills.append(
                make_agent_skill(
                    skill_id=f"{agent_id}:{sname}",
                    name=sname,
                    description=fm.get("description", ""),
                    tags=tags or None,
                )
            )

    # 2. Global skills matching profile categories
    for category in profile_categories:
        for gs in global_skills_index.get(category, []):
            sname = gs["name"]
            if sname in seen_names:
                continue
            seen_names.add(sname)
            skills.append(
                make_agent_skill(
                    skill_id=f"a2a:skill:{sname}",
                    name=sname,
                    description=gs["description"],
                    tags=gs["tags"] or None,
                )
            )

    return skills


# ---------------------------------------------------------------------------
# Frontmatter parser (no PyYAML dependency)
# ---------------------------------------------------------------------------


def _parse_frontmatter(path: Path) -> Optional[Dict[str, Any]]:
    """Parse a SKILL.md-style YAML frontmatter block between ``---`` markers.

    Handles nested dicts, inline lists, indented lists, multi-line descriptions,
    and simple key-value pairs. Pure regex — no PyYAML dependency.
    """
    try:
        content = path.read_text()
    except Exception:
        return None

    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None

    raw = parts[1]
    return _parse_yaml_block(raw)


def _parse_yaml_block(raw: str) -> Dict[str, Any]:
    """Parse a YAML block (without markers) into a nested dict.

    Indentation-aware: tracks the indent level of each line and adjusts
    the path_stack accordingly.

    Supports:
    - key: value
    - key: "quoted value"
    - key: [inline, list]
    - key:
        subkey: value
      (indented nested keys)
    - - list items (at any level)
    - key: >- / >  (folded scalar — multi-line)
    - key: |       (literal block)
    - key:
        - item1
        - item2
      (indented list under a key)
    """
    result: Dict[str, Any] = {}
    path_stack: List[str] = []
    indent_stack: List[int] = []
    lines = raw.splitlines()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        indent = len(line) - len(line.lstrip())

        while indent_stack and indent <= indent_stack[-1]:
            indent_stack.pop()
            path_stack.pop()

        # - list item
        if stripped.startswith("- ") and ":" not in stripped:
            val = stripped[2:].strip().strip("'\"")
            if path_stack:
                parent = _deep_get(result, path_stack)
                if parent is None:
                    _deep_set(result, path_stack[:-1], path_stack[-1], [val])
                elif isinstance(parent, list):
                    parent.append(val)
                elif isinstance(parent, dict):
                    _deep_set(result, path_stack[:-1], path_stack[-1], [val])
            i += 1
            continue

        # - key: value list item
        if stripped.startswith("- ") and ":" in stripped:
            sub = stripped[2:].strip()
            key_part, _, value_part = sub.partition(":")
            v_trimmed = value_part.strip()
            effective_key = key_part.strip()
            if v_trimmed:
                _deep_set(result, path_stack, effective_key, v_trimmed.strip("'\""))
            i += 1
            continue

        # key: value
        if ":" in stripped:
            key_part, _, value_part = stripped.partition(":")
            v_trimmed = value_part.strip()
            effective_key = key_part.strip()

            # Nested block (no value)
            if v_trimmed == "" or v_trimmed.startswith("#"):
                path_stack.append(effective_key)
                indent_stack.append(indent)
                i += 1
                continue

            # Folded/literal scalar
            if v_trimmed in (">", ">-", "|", "|-"):
                lines_content: List[str] = []
                i += 1
                while i < len(lines):
                    nxt = lines[i]
                    nxt_stripped = nxt.strip()
                    if nxt_stripped == "":
                        i += 1
                        continue
                    if not nxt.startswith(" ") and nxt_stripped:
                        break
                    if nxt_stripped:
                        lines_content.append(nxt_stripped)
                    i += 1
                if v_trimmed.startswith(">"):
                    value = " ".join(lines_content)
                else:
                    value = "\n".join(lines_content)
                _deep_set(result, path_stack, effective_key, value)
                continue

            # Inline list
            if v_trimmed.startswith("[") and v_trimmed.endswith("]"):
                items = [
                    x.strip().strip("'\"")
                    for x in v_trimmed[1:-1].split(",")
                    if x.strip()
                ]
                _deep_set(result, path_stack, effective_key, items)
                i += 1
                continue

            # Plain value
            _deep_set(result, path_stack, effective_key, v_trimmed.strip("'\""))
            i += 1
            continue

        # Plain list item under a parent key (indented)
        if stripped.startswith("-"):
            val = stripped[1:].strip().strip("'\"")
            if path_stack:
                parent = _deep_get(result, path_stack)
                if parent is None:
                    _deep_set(result, path_stack[:-1], path_stack[-1], [val])
                elif isinstance(parent, list):
                    parent.append(val)
            i += 1
            continue

        i += 1

    return result


def _deep_get(d: Dict, path: List[str]) -> Any:
    """Get a nested dict value by key path."""
    current = d
    for key in path:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current


def _deep_set(d: Dict, path: List[str], key: str, value: Any) -> None:
    """Set a nested dict value by key path, creating intermediate dicts."""
    current = d
    for k in path:
        if k not in current:
            current[k] = {}
        current = current[k]
        if not isinstance(current, dict):
            current = {}
    current[key] = value