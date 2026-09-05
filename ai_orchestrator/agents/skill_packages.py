from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

ALLOWED_SKILL_CAPABILITIES = frozenset({
    "deals.read", "documents.search", "web.search", "artifacts.read",
})
MAX_PACKAGE_FILES = 64
MAX_PACKAGE_BYTES = 1_000_000
ALLOWED_FILE_SUFFIXES = (".md", ".json", ".txt")

class SkillPackageContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SkillCompatibility(SkillPackageContract):
    runtime: Literal["pydantic_ai"] = "pydantic_ai"
    min_runtime_version: str = Field(min_length=1, max_length=40)
    max_runtime_version: str | None = Field(default=None, min_length=1, max_length=40)


class SkillReference(SkillPackageContract):
    path: str = Field(min_length=1, max_length=240)
    media_type: str = Field(default="text/markdown", min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if value.startswith(("/", ".")) or ".." in value.split("/"):
            raise ValueError("reference paths must be relative package paths")
        return value


class AgentSkillManifest(SkillPackageContract):
    schema_version: Literal["agent_skill_v1"]
    slug: str = Field(pattern=r"^[a-z][a-z0-9_]{1,99}$")
    description: str = Field(min_length=1, max_length=2_000)
    capabilities: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    risk: Literal["read_only", "controlled_write", "high_risk"]
    compatibility: SkillCompatibility
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    references: tuple[SkillReference, ...] = Field(default_factory=tuple, max_length=100)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted(set(values)))
        if any(not re.fullmatch(r"[a-z][a-z0-9._-]{1,99}", value) for value in normalized):
            raise ValueError("capability IDs must use lowercase allowlist identifiers")
        return normalized


@dataclass(frozen=True)
class SkillPackageValidation:
    valid: bool
    digest: str
    report: dict[str, Any]
    errors: list[str]


def canonical_package_bytes(manifest: AgentSkillManifest, files: dict[str, str]) -> bytes:
    payload = {
        "manifest": manifest.model_dump(mode="json"),
        "files": {path: files[path] for path in sorted(files)},
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def validate_skill_package(manifest: Any, files: Any) -> SkillPackageValidation:
    errors: list[str] = []
    parsed: AgentSkillManifest | None = None
    try:
        parsed = AgentSkillManifest.model_validate(manifest)
    except ValidationError as exc:
        errors.extend(
            f"manifest.{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in exc.errors(include_url=False)
        )
    if not isinstance(files, dict) or any(
        not isinstance(path, str) or not isinstance(content, str)
        for path, content in (files.items() if isinstance(files, dict) else ())
    ):
        errors.append("files must be an object mapping relative paths to text")
        files = {}
    if len(files) > MAX_PACKAGE_FILES:
        errors.append(f"packages may contain at most {MAX_PACKAGE_FILES} files")
    if sum(len(value.encode()) for value in files.values()) > MAX_PACKAGE_BYTES:
        errors.append(f"package text may contain at most {MAX_PACKAGE_BYTES} bytes")
    for path in files:
        if path.startswith(("/", ".")) or ".." in path.split("/"):
            errors.append(f"unsafe package path: {path}")
        elif not path.endswith(ALLOWED_FILE_SUFFIXES):
            errors.append(f"unsupported package file type: {path}")
    if parsed:
        unknown_capabilities = sorted(set(parsed.capabilities) - ALLOWED_SKILL_CAPABILITIES)
        if unknown_capabilities:
            errors.append(f"unknown capabilities: {', '.join(unknown_capabilities)}")
        declared = {reference.path for reference in parsed.references}
        supplied = set(files)
        missing = sorted(declared - supplied)
        undeclared = sorted(supplied - declared - {"SKILL.md"})
        if missing:
            errors.append(f"missing reference files: {', '.join(missing)}")
        if undeclared:
            errors.append(f"undeclared package files: {', '.join(undeclared)}")
    digest = hashlib.sha256(canonical_package_bytes(parsed, files)).hexdigest() if parsed and not errors else ""
    report = {"schema_version": "agent_skill_v1", "valid": not errors, "errors": errors}
    return SkillPackageValidation(not errors, digest, report, errors)
