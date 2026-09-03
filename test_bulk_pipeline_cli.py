import json
import tempfile
import unittest
from pathlib import Path

from bulk_pipeline_cli import (
    Deal,
    PhaseOptions,
    PipelineCLI,
    build_file_tree,
    compact_indices,
    format_bytes,
    parse_index_expression,
    parse_phases,
    resume_options,
    select_deals,
    visible_tree_rows,
)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.deals = [
            Deal(index=i, name=f"Deal {i}", file_count=i, subfolder_count=0, item_id=str(i))
            for i in range(1, 8)
        ]

    def test_index_ranges_are_normalized(self):
        self.assertEqual(parse_index_expression("5-3,1,3", 7), [1, 3, 4, 5])

    def test_invalid_indices_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            parse_index_expression("1,8", 7)

    def test_compact_indices(self):
        self.assertEqual(compact_indices([self.deals[i] for i in (0, 1, 2, 4, 6)]), "1-3,5,7")

    def test_pending_excludes_existing_phase3_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            done_dir = root / self.deals[0].folder_name
            done_dir.mkdir()
            (done_dir / "DEAL_SYNTHESIS.artifact.json").write_text("{}")
            selected = select_deals("pending", self.deals, root)
            self.assertEqual([deal.index for deal in selected], [2, 3, 4, 5, 6, 7])

    def test_phase_parser(self):
        self.assertEqual(parse_phases("3,1,3"), (1, 3))
        with self.assertRaises(ValueError):
            parse_phases("4")


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in ("bulk_1_extract.py", "bulk_2_normalize.py", "bulk_3_synthesize.py"):
            (self.root / name).write_text("", encoding="utf-8")
        self.discovery = self.root / "deal_discovery.json"
        self.discovery.write_text(
            json.dumps(
                {
                    "deals": [
                        {"name": "Large", "id": "2", "file_count": 9},
                        {"name": "Small", "id": "1", "file_count": 1},
                        {"name": "Same count first", "id": "3", "file_count": 9},
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.python = self.root / "python"
        self.python.write_text("", encoding="utf-8")
        self.cli = PipelineCLI(
            app_dir=self.root,
            discovery_path=self.discovery,
            python_executable=self.python,
            run_state_path=self.root / "run_state.json",
            output_fn=lambda _message: None,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_deals_uses_phase_sort_order(self):
        deals = self.cli.load_deals()
        self.assertEqual(
            [(deal.index, deal.name) for deal in deals],
            [(1, "Small"), (2, "Large"), (3, "Same count first")],
        )

    def test_commands_keep_existing_phase_entrypoints(self):
        deals = self.cli.load_deals()
        options = PhaseOptions("redo", "redo-degraded", "redo", True)
        self.assertEqual(
            self.cli.build_phase_command(1, deals, options)[-3:],
            ["--deals", "1-3", "--redo"],
        )
        self.assertEqual(self.cli.build_phase_command(2, deals, options)[-1], "--redo-degraded")
        self.assertEqual(self.cli.build_phase_command(3, deals, options)[-2:], ["--redo", "--force-degraded"])

    def test_dry_run_does_not_spawn_children(self):
        deals = self.cli.load_deals()
        result = self.cli.run_pipeline(deals, (1, 2, 3), PhaseOptions(), dry_run=True)
        self.assertEqual(result, 0)

    def test_run_state_round_trip_and_redo_becomes_resume(self):
        deals = self.cli.load_deals()
        original = PhaseOptions("redo", "redo", "redo", True)
        self.cli.write_run_state(
            deals,
            (1, 2, 3),
            original,
            status="interrupted",
            completed_phases=(1,),
            active_phase=2,
            last_exit_code=130,
        )
        payload = self.cli.load_run_state()
        selected, phases, restored, completed = self.cli.restore_run(deals, payload)
        self.assertEqual([deal.index for deal in selected], [1, 2, 3])
        self.assertEqual(phases, (1, 2, 3))
        self.assertEqual(completed, [1])
        self.assertEqual(resume_options(restored), PhaseOptions("resume", "resume", "resume", True))

    def test_completed_state_is_not_offered_for_resume(self):
        deals = self.cli.load_deals()
        self.cli.write_run_state(
            deals,
            (1,),
            PhaseOptions(),
            status="completed",
            completed_phases=(1,),
            active_phase=None,
        )
        self.assertIsNone(self.cli.resumable_run_state())


class TreeTests(unittest.TestCase):
    def test_flat_graph_paths_become_expandable_tree(self):
        tree = build_file_tree(
            "Deal",
            [
                {"id": "1", "path": "Financials/FY25/model.xlsx", "size": 2048},
                {"id": "2", "path": "Deck.pdf", "size": 1024},
            ],
        )
        collapsed = visible_tree_rows(tree, {""})
        self.assertEqual([row[0].name for row in collapsed], ["Deal", "Financials", "Deck.pdf"])
        expanded = visible_tree_rows(tree, {"", "Financials", "Financials/FY25"})
        self.assertEqual(
            [row[0].name for row in expanded],
            ["Deal", "Financials", "FY25", "model.xlsx", "Deck.pdf"],
        )
        self.assertEqual(format_bytes(expanded[3][0].size), "2.0 KB")


if __name__ == "__main__":
    unittest.main()
