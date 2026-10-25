"""
skills/skill_base.py — Data classes for JARVIS Skill definitions.

A Skill is a recorded sequence of JARVIS actions that can be:
- Taught by the user ("اتعلم مهارة جديدة")
- Executed by name ("نفّذ مهارة X")
- Exported as a .skill ZIP for sharing
- Imported from another user's .skill file
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SkillStep:
    """A single step in a skill's execution sequence."""
    action: str          # Tool name: 'open_app' | 'browser_control' | 'computer_control' | etc.
    params: dict         # Parameters to pass to the tool
    description: str     # Human-readable Arabic description of this step
    wait_after_ms: int = 500  # Wait after step in milliseconds

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> SkillStep:
        return cls(
            action=d["action"],
            params=d.get("params", {}),
            description=d.get("description", ""),
            wait_after_ms=d.get("wait_after_ms", 500),
        )


@dataclass
class SkillDefinition:
    """Complete definition of a learnable JARVIS skill."""
    name: str                    # Unique identifier: 'open_youtube_search'
    display_name: str            # Arabic display: 'فتح يوتيوب والبحث'
    description: str             # What it does in Arabic
    author: str                  # User who created it (e.g. 'AMOX')
    version: str = "1.0"
    created_at: str = ""
    parameters: list[str] = field(default_factory=list)   # Template vars: ['query', 'url']
    steps: list[SkillStep] = field(default_factory=list)
    required_files: list[str] = field(default_factory=list)   # Files needed to run
    required_packages: list[str] = field(default_factory=list)  # pip packages
    tags: list[str] = field(default_factory=list)  # e.g. ['browser', 'discord', 'system']

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> SkillDefinition:
        steps = [SkillStep.from_dict(s) for s in d.get("steps", [])]
        return cls(
            name=d["name"],
            display_name=d.get("display_name", d["name"]),
            description=d.get("description", ""),
            author=d.get("author", "Unknown"),
            version=d.get("version", "1.0"),
            created_at=d.get("created_at", ""),
            parameters=d.get("parameters", []),
            steps=steps,
            required_files=d.get("required_files", []),
            required_packages=d.get("required_packages", []),
            tags=d.get("tags", []),
        )

    def generate_manifest(self) -> str:
        """Generate a Markdown manifest explaining what's needed to install this skill."""
        lines = [
            f"# Required Changes to Add \"{self.display_name}\" Skill",
            "",
            f"**Description**: {self.description}",
            f"**Author**: {self.author}",
            f"**Version**: {self.version}",
            "",
        ]
        if self.required_packages:
            lines += ["## Required Packages", ""]
            lines += [f"```\npip install {' '.join(self.required_packages)}\n```", ""]
        else:
            lines += ["## Required Packages", "", "None — no additional packages needed.", ""]

        if self.required_files:
            lines += ["## Required Files", ""]
            for f in self.required_files:
                lines.append(f"- `{f}`")
            lines.append("")
        else:
            lines += ["## Required Files", "", "None — uses existing JARVIS files.", ""]

        lines += [
            "## Installation",
            "",
            "1. Copy this `.skill` file to any JARVIS MK installation",
            "2. Launch JARVIS → say **\"استورد مهارة\"** → select the `.skill` file",
            "3. Or open the Skill Manager from the HUD toolbar",
            "4. Done ✅",
            "",
            "## Parameters",
            "",
        ]
        if self.parameters:
            for p in self.parameters:
                lines.append(f"- `{{{p}}}` — replace with actual value when calling")
        else:
            lines.append("No parameters — skill runs as-is.")

        lines += [
            "",
            "## Steps",
            "",
        ]
        for i, step in enumerate(self.steps, 1):
            lines.append(f"{i}. **{step.description}** — action: `{step.action}`")

        return "\n".join(lines)
