#!/usr/bin/env python3
"""Interactive runner for the three-stage OneDrive document pipeline.

This module deliberately orchestrates the existing phase scripts instead of
importing or copying their implementation. Each child process runs from this
file's directory, which preserves the existing data/extractions layout and
artifact formats.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


APP_DIR = Path(__file__).resolve().parent
DISCOVERY_PATH = APP_DIR / "deal_discovery.json"
EXTRACTIONS_DIR = APP_DIR / "data" / "extractions"
AUDIT_DIR = EXTRACTIONS_DIR / "audit"
RUN_LOG_DIR = AUDIT_DIR / "pipeline_cli"
RUN_STATE_PATH = RUN_LOG_DIR / "run_state.json"

PHASE_SCRIPTS = {
    1: APP_DIR / "bulk_1_extract.py",
    2: APP_DIR / "bulk_2_normalize.py",
    3: APP_DIR / "bulk_3_synthesize.py",
}


try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
except ImportError:  # Rich is optional. The phase scripts already treat it this way.
    Console = None
    Panel = None
    Table = None


@dataclass(frozen=True)
class Deal:
    index: int
    name: str
    file_count: int
    subfolder_count: int
    item_id: str
    drive_id: str = ""

    @property
    def folder_name(self) -> str:
        return self.name.replace(" ", "_").replace("/", "-")


@dataclass(frozen=True)
class PhaseOptions:
    phase1_mode: str = "resume"
    phase2_mode: str = "resume"
    phase3_mode: str = "resume"
    force_degraded: bool = False
    include_terminal_failures: bool = True


class UserCancelled(Exception):
    """Raised when the operator exits an interactive prompt."""


@dataclass
class TreeNode:
    name: str
    is_folder: bool = True
    size: int = 0
    item_id: str = ""
    children: dict[str, "TreeNode"] | None = None

    def __post_init__(self) -> None:
        if self.children is None:
            self.children = {}


def build_file_tree(deal_name: str, files: Sequence[dict[str, Any]]) -> TreeNode:
    root = TreeNode(deal_name)
    for item in sorted(files, key=lambda row: str(row.get("path") or row.get("name") or "").casefold()):
        path = str(item.get("path") or item.get("name") or "").strip("/")
        if not path:
            continue
        parts = [part for part in path.split("/") if part]
        node = root
        for part in parts[:-1]:
            assert node.children is not None
            node = node.children.setdefault(part, TreeNode(part))
        assert node.children is not None
        node.children[parts[-1]] = TreeNode(
            parts[-1],
            is_folder=False,
            size=int(item.get("size") or 0),
            item_id=str(item.get("id") or ""),
        )
    return root


def visible_tree_rows(root: TreeNode, expanded: set[str]) -> list[tuple[TreeNode, int, str]]:
    rows: list[tuple[TreeNode, int, str]] = []

    def visit(node: TreeNode, depth: int, key: str) -> None:
        rows.append((node, depth, key))
        if not node.is_folder or key not in expanded:
            return
        assert node.children is not None
        children = sorted(node.children.values(), key=lambda item: (not item.is_folder, item.name.casefold()))
        for child in children:
            child_key = f"{key}/{child.name}" if key else child.name
            visit(child, depth + 1, child_key)

    visit(root, 0, "")
    return rows


class PipelineCLI:
    def __init__(
        self,
        *,
        app_dir: Path = APP_DIR,
        discovery_path: Path = DISCOVERY_PATH,
        python_executable: Path | str | None = None,
        run_state_path: Path | None = None,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ) -> None:
        self.app_dir = Path(app_dir).resolve()
        self.discovery_path = Path(discovery_path).resolve()
        self.extractions_dir = self.app_dir / "data" / "extractions"
        self.audit_dir = self.extractions_dir / "audit"
        self.run_log_dir = self.audit_dir / "pipeline_cli"
        self.run_state_path = Path(run_state_path or (self.run_log_dir / "run_state.json"))
        self.python_executable = str(python_executable or find_python(self.app_dir))
        self.input = input_fn
        self.output = output_fn
        self.console = Console() if Console and output_fn is print else None

    def load_deals(self) -> list[Deal]:
        try:
            payload = json.loads(self.discovery_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Discovery file not found: {self.discovery_path}. Run bulk_discovery.py first."
            ) from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Discovery file is not valid JSON: {exc}") from exc

        raw_deals = payload.get("deals")
        if not isinstance(raw_deals, list):
            raise RuntimeError("deal_discovery.json does not contain a deals list")

        ordered = sorted(
            raw_deals,
            key=lambda item: int(item.get("file_count") or 0),
        )
        return [
            Deal(
                index=index,
                name=str(item.get("name") or f"Unnamed deal {index}"),
                file_count=int(item.get("file_count") or 0),
                subfolder_count=int(item.get("subfolder_count") or 0),
                item_id=str(item.get("id") or ""),
                drive_id=str(item.get("drive_id") or payload.get("drive_id") or ""),
            )
            for index, item in enumerate(ordered, 1)
        ]

    def show_deals(self, deals: Sequence[Deal], *, page: int = 1, page_size: int = 25) -> None:
        page_size = max(1, page_size)
        page_count = max(1, (len(deals) + page_size - 1) // page_size)
        page = min(max(1, page), page_count)
        visible = deals[(page - 1) * page_size : page * page_size]
        if self.console and Table:
            table = Table(title=f"OneDrive deal folders, page {page}/{page_count}")
            table.add_column("#", justify="right")
            table.add_column("Folder")
            table.add_column("Files", justify="right")
            table.add_column("Subfolders", justify="right")
            table.add_column("Local state")
            for deal in visible:
                table.add_row(
                    str(deal.index),
                    deal.name,
                    str(deal.file_count),
                    str(deal.subfolder_count),
                    self.local_state(deal),
                )
            self.console.print(table)
            return
        self.output(f"OneDrive deal folders, page {page}/{page_count}")
        for deal in visible:
            self.output(
                f"{deal.index:5}  {deal.name[:55]:55}  files={deal.file_count:5}  "
                f"folders={deal.subfolder_count:4}  {self.local_state(deal)}"
            )

    def local_state(self, deal: Deal) -> str:
        deal_dir = self.extractions_dir / deal.folder_name
        if not deal_dir.is_dir():
            return "not started"
        phase1 = sum(
            1
            for path in deal_dir.glob("*.json")
            if not path.name.endswith(".artifact.json")
            and "[Pages " not in path.name
            and ".part" not in path.name
            and ".tmp" not in path.name
        )
        phase2 = sum(
            1
            for path in deal_dir.glob("*.artifact.json")
            if path.name != "DEAL_SYNTHESIS.artifact.json"
        )
        phase3 = (deal_dir / "DEAL_SYNTHESIS.artifact.json").exists()
        return f"P1 {phase1}/{deal.file_count}, P2 {phase2}/{phase1}, P3 {'yes' if phase3 else 'no'}"

    def interactive_selection(self, deals: Sequence[Deal]) -> list[Deal]:
        page = 1
        page_size = 25
        filtered = list(deals)
        while True:
            self.show_deals(filtered, page=page, page_size=page_size)
            self.output(
                "Select: 1,3,8-12 | all | pending | search <text> | next | prev | quit"
            )
            raw = self.input("Folders to process: ").strip()
            lowered = raw.casefold()
            if lowered in {"q", "quit", "exit"}:
                raise UserCancelled()
            if lowered in {"n", "next"}:
                page += 1
                continue
            if lowered in {"p", "prev", "previous"}:
                page = max(1, page - 1)
                continue
            if lowered.startswith("search "):
                term = raw[7:].strip().casefold()
                filtered = [deal for deal in deals if term in deal.name.casefold()]
                page = 1
                if not filtered:
                    self.output(f"No folders match {raw[7:].strip()!r}.")
                    filtered = list(deals)
                continue
            try:
                selected = select_deals(raw, deals, self.extractions_dir)
            except ValueError as exc:
                self.output(f"Invalid selection: {exc}")
                continue
            if not selected:
                self.output("The selection is empty.")
                continue
            return selected

    def keyboard_selection(self, deals: Sequence[Deal], *, maximum: int = 5) -> list[Deal]:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("Keyboard browser needs an interactive terminal")
        try:
            import curses
        except ImportError as exc:
            raise RuntimeError("This Python installation does not include curses") from exc

        selected_indices: set[int] = set()
        tree_cache: dict[int, TreeNode] = {}

        def run(stdscr: Any) -> list[Deal]:
            curses.curs_set(0)
            stdscr.keypad(True)
            cursor = 0
            top = 0
            message = ""
            while True:
                height, width = stdscr.getmaxyx()
                body_height = max(1, height - 6)
                cursor = max(0, min(cursor, len(deals) - 1))
                if cursor < top:
                    top = cursor
                if cursor >= top + body_height:
                    top = cursor - body_height + 1
                stdscr.erase()
                self._safe_addstr(stdscr, 0, 0, "Select up to five complete deal folders", width, curses.A_BOLD)
                self._safe_addstr(
                    stdscr,
                    1,
                    0,
                    "Up/Down move  Space select  B browse tree  Enter continue  Q cancel",
                    width,
                )
                self._safe_addstr(stdscr, 2, 0, f"Selected {len(selected_indices)}/{maximum}", width)
                for screen_row, deal in enumerate(deals[top : top + body_height], 3):
                    absolute_row = top + screen_row - 3
                    marker = "[x]" if deal.index in selected_indices else "[ ]"
                    label = f"{marker} {deal.index:4}  {deal.name}  ({deal.file_count} files)"
                    style = curses.A_REVERSE if absolute_row == cursor else 0
                    self._safe_addstr(stdscr, screen_row, 0, label, width, style)
                footer_row = min(height - 2, 3 + body_height)
                self._safe_addstr(stdscr, footer_row, 0, message, width, curses.A_BOLD)
                stdscr.refresh()
                key = stdscr.getch()
                if key in (curses.KEY_UP, ord("k")):
                    cursor = max(0, cursor - 1)
                    message = ""
                elif key in (curses.KEY_DOWN, ord("j")):
                    cursor = min(len(deals) - 1, cursor + 1)
                    message = ""
                elif key == curses.KEY_PPAGE:
                    cursor = max(0, cursor - body_height)
                elif key == curses.KEY_NPAGE:
                    cursor = min(len(deals) - 1, cursor + body_height)
                elif key == ord(" "):
                    deal = deals[cursor]
                    if deal.index in selected_indices:
                        selected_indices.remove(deal.index)
                        message = f"Removed {deal.name}"
                    elif len(selected_indices) >= maximum:
                        message = f"Selection is limited to {maximum} folders"
                    else:
                        selected_indices.add(deal.index)
                        message = f"Selected {deal.name}"
                elif key in (ord("b"), ord("B"), curses.KEY_RIGHT):
                    deal = deals[cursor]
                    try:
                        tree = tree_cache.get(deal.index)
                        if tree is None:
                            self._draw_loading(stdscr, deal.name)
                            tree = self.load_onedrive_tree(deal)
                            tree_cache[deal.index] = tree
                        self._browse_tree(stdscr, tree)
                        message = f"Returned from {deal.name} file tree"
                    except Exception as exc:
                        message = f"Could not load tree: {str(exc)[:100]}"
                elif key in (10, 13, curses.KEY_ENTER):
                    if selected_indices:
                        return [deal for deal in deals if deal.index in selected_indices]
                    message = "Select at least one folder with Space"
                elif key in (ord("q"), ord("Q"), 27):
                    raise UserCancelled()

        return curses.wrapper(run)

    @staticmethod
    def _safe_addstr(window: Any, row: int, col: int, value: str, width: int, style: int = 0) -> None:
        if row < 0:
            return
        try:
            window.addstr(row, col, value[: max(0, width - col - 1)], style)
        except Exception:
            pass

    def _draw_loading(self, stdscr: Any, deal_name: str) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        self._safe_addstr(stdscr, height // 2, 2, f"Loading OneDrive tree for {deal_name}...", width)
        stdscr.refresh()

    def load_onedrive_tree(self, deal: Deal) -> TreeNode:
        payload = json.loads(self.discovery_path.read_text(encoding="utf-8"))
        user_email = str(payload.get("user_email") or "")
        drive_id = deal.drive_id or str(payload.get("drive_id") or "")
        if not user_email or not drive_id or not deal.item_id:
            raise RuntimeError("Discovery data is missing user_email, drive_id, or folder id")
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
        if str(self.app_dir) not in sys.path:
            sys.path.insert(0, str(self.app_dir))
        import django

        django.setup()
        from microsoft.services.graph_service import GraphAPIService

        files = GraphAPIService().get_folder_tree(
            drive_id,
            deal.item_id,
            user_email,
            max_depth=None,
        )
        return build_file_tree(deal.name, files)

    def _browse_tree(self, stdscr: Any, root: TreeNode) -> None:
        import curses

        expanded = {""}
        cursor = 0
        top = 0
        while True:
            rows = visible_tree_rows(root, expanded)
            cursor = max(0, min(cursor, len(rows) - 1))
            height, width = stdscr.getmaxyx()
            body_height = max(1, height - 4)
            if cursor < top:
                top = cursor
            if cursor >= top + body_height:
                top = cursor - body_height + 1
            stdscr.erase()
            self._safe_addstr(stdscr, 0, 0, f"OneDrive tree: {root.name}", width, curses.A_BOLD)
            self._safe_addstr(stdscr, 1, 0, "Arrows move  Enter/Right expand  Left collapse  Backspace/Q return", width)
            for screen_row, (node, depth, key) in enumerate(rows[top : top + body_height], 2):
                absolute_row = top + screen_row - 2
                if node.is_folder:
                    glyph = "[-]" if key in expanded else "[+]"
                    suffix = ""
                else:
                    glyph = "   "
                    suffix = f"  {format_bytes(node.size)}"
                label = f"{'  ' * depth}{glyph} {node.name}{suffix}"
                style = curses.A_REVERSE if absolute_row == cursor else 0
                self._safe_addstr(stdscr, screen_row, 0, label, width, style)
            stdscr.refresh()
            pressed = stdscr.getch()
            node, _depth, key = rows[cursor]
            if pressed in (curses.KEY_UP, ord("k")):
                cursor = max(0, cursor - 1)
            elif pressed in (curses.KEY_DOWN, ord("j")):
                cursor = min(len(rows) - 1, cursor + 1)
            elif pressed in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT):
                if node.is_folder:
                    if key in expanded:
                        expanded.remove(key)
                    else:
                        expanded.add(key)
            elif pressed == curses.KEY_LEFT:
                if node.is_folder and key in expanded:
                    expanded.remove(key)
                elif "/" in key:
                    parent = key.rsplit("/", 1)[0]
                    for index, (_item, _item_depth, item_key) in enumerate(rows):
                        if item_key == parent:
                            cursor = index
                            break
            elif pressed in (curses.KEY_BACKSPACE, 8, 127, ord("q"), ord("Q"), 27):
                return

    def choose_phases(self) -> tuple[int, ...]:
        self.output("Run: 1) all phases  2) extraction only  3) artifacts only  4) analysis only  5) choose")
        answer = self.input("Choice [1]: ").strip() or "1"
        mapping = {"1": (1, 2, 3), "2": (1,), "3": (2,), "4": (3,)}
        if answer in mapping:
            return mapping[answer]
        if answer == "5":
            return parse_phases(self.input("Phases, for example 1,2 or 2,3: "))
        raise ValueError("Choose 1, 2, 3, 4, or 5")

    def choose_options(self, phases: Sequence[int]) -> PhaseOptions:
        p1 = "resume"
        p2 = "resume"
        p3 = "resume"
        force_degraded = False
        if 1 in phases:
            p1 = prompt_choice(
                self.input,
                "Phase 1 mode: 1) resume and retry failures  2) redo every file [1]: ",
                {"1": "resume", "2": "redo", "": "resume"},
            )
        if 2 in phases:
            p2 = prompt_choice(
                self.input,
                "Phase 2 mode: 1) resume  2) redo degraded  3) redo everything [1]: ",
                {"1": "resume", "2": "redo-degraded", "3": "redo", "": "resume"},
            )
        if 3 in phases:
            p3 = prompt_choice(
                self.input,
                "Phase 3 mode: 1) keep valid analyses  2) redo analyses [1]: ",
                {"1": "resume", "2": "redo", "": "resume"},
            )
            force_degraded = yes_no(
                self.input,
                "Allow Phase 3 to analyze deals with missing Phase 1/2 files? [y/N]: ",
                default=False,
            )
        return PhaseOptions(p1, p2, p3, force_degraded, True)

    def build_phase_command(
        self,
        phase: int,
        selected: Sequence[Deal],
        options: PhaseOptions,
    ) -> list[str]:
        if phase not in PHASE_SCRIPTS:
            raise ValueError(f"Unknown phase: {phase}")
        script = self.app_dir / PHASE_SCRIPTS[phase].name
        command = [self.python_executable, str(script), "--deals", compact_indices(selected)]
        if phase == 1 and options.phase1_mode == "redo":
            command.append("--redo")
        elif phase == 2 and options.phase2_mode == "redo":
            command.append("--redo")
        elif phase == 2 and options.phase2_mode == "redo-degraded":
            command.append("--redo-degraded")
        elif phase == 3:
            if options.phase3_mode == "redo":
                command.append("--redo")
            if options.force_degraded:
                command.append("--force-degraded")
        return command

    def preflight(self, phases: Sequence[int]) -> list[str]:
        errors = []
        if not self.discovery_path.is_file():
            errors.append(f"Missing {self.discovery_path}")
        if not Path(self.python_executable).exists():
            errors.append(f"Python interpreter not found: {self.python_executable}")
        for phase in phases:
            script = self.app_dir / PHASE_SCRIPTS[phase].name
            if not script.is_file():
                errors.append(f"Missing phase {phase} script: {script}")
        return errors

    def run_phase(self, phase: int, command: Sequence[str], *, dry_run: bool = False) -> int:
        rendered = shlex.join(str(part) for part in command)
        self.output(f"\nPhase {phase} command:\n  {rendered}")
        if dry_run:
            return 0
        self.run_log_dir.mkdir(parents=True, exist_ok=True)
        latest_log = self.run_log_dir / f"phase{phase}_latest.log"
        self.output(f"Live output follows. A copy is saved to {latest_log}")
        with latest_log.open("w", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                list(command),
                cwd=self.app_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=os.environ.copy(),
            )
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    self.output(line.rstrip("\n"))
                    log_file.write(line)
                    log_file.flush()
            except KeyboardInterrupt:
                self.output("\nStopping the active phase...")
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                return 130
            return process.wait()

    def run_pipeline(
        self,
        selected: Sequence[Deal],
        phases: Sequence[int],
        options: PhaseOptions,
        *,
        dry_run: bool = False,
        continue_on_failure: bool = False,
        completed_phases: Sequence[int] = (),
        resumed: bool = False,
    ) -> int:
        errors = self.preflight(phases)
        if errors:
            for error in errors:
                self.output(f"Preflight error: {error}")
            return 2
        completed = list(dict.fromkeys(int(phase) for phase in completed_phases))
        if not dry_run:
            self.write_run_state(
                selected,
                phases,
                options,
                status="running",
                completed_phases=completed,
                active_phase=None,
                resumed=resumed,
            )
        failed_phases: list[tuple[int, int]] = []
        for phase in phases:
            if phase in completed:
                self.output(f"Phase {phase} is already complete. Skipping it.")
                continue
            effective_options = resume_options(options) if resumed else options
            if not dry_run:
                self.write_run_state(
                    selected,
                    phases,
                    options,
                    status="running",
                    completed_phases=completed,
                    active_phase=phase,
                    resumed=resumed,
                )
            command = self.build_phase_command(phase, selected, effective_options)
            exit_code = self.run_phase(phase, command, dry_run=dry_run)
            if exit_code == 0:
                completed.append(phase)
                if not dry_run:
                    self.write_run_state(
                        selected,
                        phases,
                        options,
                        status="running",
                        completed_phases=completed,
                        active_phase=None,
                        resumed=resumed,
                    )
            if exit_code and not continue_on_failure:
                if not dry_run:
                    self.write_run_state(
                        selected,
                        phases,
                        options,
                        status="interrupted" if exit_code == 130 else "failed",
                        completed_phases=completed,
                        active_phase=phase,
                        last_exit_code=exit_code,
                        resumed=resumed,
                    )
                self.output(f"Phase {phase} stopped with exit code {exit_code}. Later phases were not started.")
                return exit_code
            if exit_code:
                failed_phases.append((phase, exit_code))
        if failed_phases:
            failed_phase, failed_code = failed_phases[0]
            if not dry_run:
                self.write_run_state(
                    selected,
                    phases,
                    options,
                    status="failed",
                    completed_phases=completed,
                    active_phase=failed_phase,
                    last_exit_code=failed_code,
                    resumed=resumed,
                )
            self.output(
                "Run finished with failed phases: "
                + ", ".join(f"{phase} (exit {code})" for phase, code in failed_phases)
            )
            return failed_code
        if not dry_run:
            self.write_run_state(
                selected,
                phases,
                options,
                status="completed",
                completed_phases=completed,
                active_phase=None,
                resumed=resumed,
            )
        return 0

    def write_run_state(
        self,
        selected: Sequence[Deal],
        phases: Sequence[int],
        options: PhaseOptions,
        *,
        status: str,
        completed_phases: Sequence[int],
        active_phase: int | None,
        last_exit_code: int | None = None,
        resumed: bool = False,
    ) -> None:
        previous = self.load_run_state(required=False) or {}
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "version": 1,
            "status": status,
            "created_at": previous.get("created_at") or now,
            "updated_at": now,
            "resumed_at": now if resumed else previous.get("resumed_at"),
            "selected_indices": [deal.index for deal in selected],
            "selected_deals": [{"index": deal.index, "name": deal.name} for deal in selected],
            "phases": [int(phase) for phase in phases],
            "completed_phases": [int(phase) for phase in completed_phases],
            "active_phase": active_phase,
            "last_exit_code": last_exit_code,
            "options": {
                "phase1_mode": options.phase1_mode,
                "phase2_mode": options.phase2_mode,
                "phase3_mode": options.phase3_mode,
                "force_degraded": options.force_degraded,
                "include_terminal_failures": options.include_terminal_failures,
            },
        }
        self.run_state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.run_state_path.with_suffix(self.run_state_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp_path, self.run_state_path)

    def load_run_state(self, *, required: bool = True) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.run_state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if required:
                raise RuntimeError(f"No saved run found at {self.run_state_path}")
            return None
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Saved run is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise RuntimeError("Saved run uses an unsupported format")
        return payload

    def resumable_run_state(self) -> dict[str, Any] | None:
        payload = self.load_run_state(required=False)
        if payload and payload.get("status") in {"running", "failed", "interrupted"}:
            return payload
        return None

    def restore_run(self, deals: Sequence[Deal], payload: dict[str, Any]) -> tuple[list[Deal], tuple[int, ...], PhaseOptions, list[int]]:
        selected_rows = payload.get("selected_deals") or []
        by_index = {deal.index: deal for deal in deals}
        selected = []
        for row in selected_rows:
            index = int(row.get("index") or 0)
            deal = by_index.get(index)
            expected_name = str(row.get("name") or "")
            if not deal or deal.name != expected_name:
                raise RuntimeError(
                    "deal_discovery.json changed after the saved run. "
                    f"Index {index} was {expected_name!r}. Start a new run after reviewing the selection."
                )
            selected.append(deal)
        if not selected:
            raise RuntimeError("Saved run has no selected folders")
        phases = parse_phases(",".join(str(phase) for phase in payload.get("phases") or []))
        raw_options = payload.get("options") or {}
        options = PhaseOptions(
            phase1_mode=str(raw_options.get("phase1_mode") or "resume"),
            phase2_mode=str(raw_options.get("phase2_mode") or "resume"),
            phase3_mode=str(raw_options.get("phase3_mode") or "resume"),
            force_degraded=bool(raw_options.get("force_degraded")),
            include_terminal_failures=bool(raw_options.get("include_terminal_failures", True)),
        )
        completed = [int(phase) for phase in payload.get("completed_phases") or []]
        return selected, phases, options, completed

    def resume_saved_run(self, deals: Sequence[Deal], *, confirm: bool = True) -> int:
        payload = self.load_run_state()
        if payload.get("status") == "completed":
            raise RuntimeError("The saved run is already complete")
        selected, phases, options, completed = self.restore_run(deals, payload)
        self.output(
            f"\nResuming saved run from {payload.get('updated_at') or 'unknown time'}. "
            f"Completed phases: {','.join(map(str, completed)) or 'none'}."
        )
        self.show_plan(selected, phases, resume_options(options))
        if confirm and not yes_no(self.input, "Resume this run? [Y/n]: ", default=True):
            raise UserCancelled()
        return self.run_pipeline(
            selected,
            phases,
            options,
            completed_phases=completed,
            resumed=True,
        )

    def run_interactive(self) -> int:
        deals = self.load_deals()
        self.banner(deals)
        saved = self.resumable_run_state()
        if saved and yes_no(
            self.input,
            f"Resume the unfinished run last updated {saved.get('updated_at', 'at an unknown time')}? [Y/n]: ",
            default=True,
        ):
            return self.resume_saved_run(deals, confirm=False)
        if sys.stdin.isatty() and sys.stdout.isatty():
            selected = self.keyboard_selection(deals)
        else:
            selected = self.interactive_selection(deals)
        phases = self.choose_phases()
        options = self.choose_options(phases)
        self.show_plan(selected, phases, options)
        if not yes_no(self.input, "Start this run? [y/N]: ", default=False):
            raise UserCancelled()
        return self.run_pipeline(selected, phases, options)

    def banner(self, deals: Sequence[Deal]) -> None:
        message = (
            "India Alternatives document pipeline\n"
            f"{len(deals)} discovered folders, {sum(deal.file_count for deal in deals):,} files\n"
            f"Data directory: {self.extractions_dir}"
        )
        if self.console and Panel:
            self.console.print(Panel(message, title="Bulk pipeline"))
        else:
            self.output(message)

    def show_plan(self, selected: Sequence[Deal], phases: Sequence[int], options: PhaseOptions) -> None:
        self.output("\nRun plan")
        self.output(f"  Folders: {len(selected)}")
        self.output(f"  Files reported by discovery: {sum(deal.file_count for deal in selected):,}")
        self.output(f"  Selection indices: {compact_indices(selected)}")
        self.output(f"  Phases: {','.join(map(str, phases))}")
        if 1 in phases:
            self.output(f"  Phase 1: {options.phase1_mode}")
        if 2 in phases:
            self.output(f"  Phase 2: {options.phase2_mode}")
        if 3 in phases:
            self.output(
                f"  Phase 3: {options.phase3_mode}, "
                f"force degraded={'yes' if options.force_degraded else 'no'}"
            )


def find_python(app_dir: Path) -> Path:
    candidates = [
        app_dir / "venv" / "bin" / "python",
        app_dir / ".venv" / "bin" / "python",
        app_dir / "venv" / "Scripts" / "python.exe",
        app_dir / ".venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            # Keep the venv path. Calling its resolved system-python target would
            # bypass the virtual environment's installed packages.
            return candidate.absolute()
    return Path(sys.executable).resolve()


def format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


def resume_options(options: PhaseOptions) -> PhaseOptions:
    return PhaseOptions(
        phase1_mode="resume",
        phase2_mode="resume",
        phase3_mode="resume",
        force_degraded=options.force_degraded,
        include_terminal_failures=options.include_terminal_failures,
    )


def parse_index_expression(expression: str, maximum: int) -> list[int]:
    values: set[int] = set()
    if not expression.strip():
        raise ValueError("enter at least one index")
    for raw_part in expression.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" in part:
            pieces = part.split("-", 1)
            if not all(piece.strip().isdigit() for piece in pieces):
                raise ValueError(f"invalid range {part!r}")
            start, end = (int(piece) for piece in pieces)
            if start > end:
                start, end = end, start
            values.update(range(start, end + 1))
        elif part.isdigit():
            values.add(int(part))
        else:
            raise ValueError(f"invalid index {part!r}")
    invalid = sorted(value for value in values if value < 1 or value > maximum)
    if invalid:
        raise ValueError(f"indices outside 1-{maximum}: {','.join(map(str, invalid[:10]))}")
    return sorted(values)


def select_deals(expression: str, deals: Sequence[Deal], extractions_dir: Path) -> list[Deal]:
    lowered = expression.strip().casefold()
    if lowered == "all":
        return list(deals)
    if lowered == "pending":
        return [
            deal
            for deal in deals
            if not (extractions_dir / deal.folder_name / "DEAL_SYNTHESIS.artifact.json").exists()
        ]
    indices = set(parse_index_expression(expression, len(deals)))
    return [deal for deal in deals if deal.index in indices]


def compact_indices(deals: Sequence[Deal]) -> str:
    indices = sorted({deal.index for deal in deals})
    if not indices:
        return ""
    parts: list[str] = []
    start = previous = indices[0]
    for value in indices[1:]:
        if value == previous + 1:
            previous = value
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    parts.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(parts)


def parse_phases(value: str) -> tuple[int, ...]:
    try:
        phases = tuple(sorted({int(item.strip()) for item in value.split(",") if item.strip()}))
    except ValueError as exc:
        raise ValueError("phases must be comma-separated numbers") from exc
    if not phases or any(phase not in PHASE_SCRIPTS for phase in phases):
        raise ValueError("phases must contain 1, 2, or 3")
    return phases


def prompt_choice(input_fn: Callable[[str], str], prompt: str, choices: dict[str, str]) -> str:
    while True:
        answer = input_fn(prompt).strip()
        if answer in choices:
            return choices[answer]
        print(f"Choose one of: {', '.join(key or 'Enter' for key in choices)}")


def yes_no(input_fn: Callable[[str], str], prompt: str, *, default: bool) -> bool:
    answer = input_fn(prompt).strip().casefold()
    if not answer:
        return default
    if answer in {"y", "yes"}:
        return True
    if answer in {"n", "no"}:
        return False
    return yes_no(input_fn, prompt, default=default)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Select OneDrive deal folders and run extraction, artifact generation, and analysis."
    )
    parser.add_argument("--select", help="Folder indices, ranges, 'all', or 'pending'. Omit for interactive mode.")
    parser.add_argument("--phases", default="1,2,3", help="Comma-separated phases. Default: 1,2,3")
    parser.add_argument("--phase1-mode", choices=("resume", "redo"), default="resume")
    parser.add_argument("--phase2-mode", choices=("resume", "redo-degraded", "redo"), default="resume")
    parser.add_argument("--phase3-mode", choices=("resume", "redo"), default="resume")
    parser.add_argument("--force-degraded", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Run without the final confirmation prompt.")
    parser.add_argument("--dry-run", action="store_true", help="Print child commands without running them.")
    parser.add_argument("--resume-run", action="store_true", help="Resume the last unfinished pipeline run.")
    parser.add_argument(
        "--continue-on-failure",
        action="store_true",
        help="Start later phases even when an earlier phase exits nonzero.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cli = PipelineCLI()
    try:
        if args.resume_run:
            deals = cli.load_deals()
            return cli.resume_saved_run(deals, confirm=not args.yes)
        if not args.select:
            return cli.run_interactive()
        deals = cli.load_deals()
        selected = select_deals(args.select, deals, cli.extractions_dir)
        if not selected:
            print("The selection is empty.")
            return 2
        phases = parse_phases(args.phases)
        options = PhaseOptions(
            phase1_mode=args.phase1_mode,
            phase2_mode=args.phase2_mode,
            phase3_mode=args.phase3_mode,
            force_degraded=args.force_degraded,
        )
        cli.banner(deals)
        cli.show_plan(selected, phases, options)
        if not args.yes and not args.dry_run and not yes_no(input, "Start this run? [y/N]: ", default=False):
            raise UserCancelled()
        return cli.run_pipeline(
            selected,
            phases,
            options,
            dry_run=args.dry_run,
            continue_on_failure=args.continue_on_failure,
        )
    except UserCancelled:
        print("Cancelled.")
        return 130
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
