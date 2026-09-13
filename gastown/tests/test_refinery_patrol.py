import json
import hashlib
import os
import subprocess
import unittest
from unittest.mock import patch

from test_witness_patrol import City, patrol, row
import test_witness_patrol_runner as runner_fixtures


REFINERY = "office-work/gastown.refinery"
FORMULA = "mol-refinery-patrol"
VARIABLES = {"target_branch": "main", "rig_name": "office-work"}


def refinery_row(bead):
    result = row(bead)
    result.update(title=FORMULA, assignee=REFINERY)
    return result


class RefineryTests(unittest.TestCase):
    def test_wrong_formula_or_foreign_current_is_preserved(self):
        for current in (row("current"), dict(refinery_row("current"), assignee="other/refinery")):
            city = City([current], "current")
            with patch.object(patrol.subprocess, "run", city.call):
                with self.assertRaises(patrol.ReconcileNeeded):
                    patrol.run("next", REFINERY, {REFINERY}, "gastown.",
                               city.checkpoints.append, FORMULA, VARIABLES, "current")
            self.assertEqual(city.mutations, [])

    def test_missing_completed_current_never_runs_commands(self):
        with patch.object(patrol.subprocess, "run") as command:
            with self.assertRaises(patrol.ReconcileNeeded):
                patrol.run("next", REFINERY, {REFINERY}, "gastown.", lambda state: None,
                           FORMULA, VARIABLES)
            command.assert_not_called()


@unittest.skipUnless(os.environ.get("GC_TEST_BIN"), "set GC_TEST_BIN to test command dispatch")
class RefineryRunnerTests(unittest.TestCase):
    def setUp(self):
        runner_fixtures.RunnerTests.setUp(self)
        self.env.update(GC_AGENT=REFINERY, GC_ALIAS="refinery-alias", GC_TEMPLATE=REFINERY)

    def call(self, mode, required_vars=True):
        args = [os.environ["GC_TEST_BIN"], "gastown", "refinery-patrol", mode,
                "--binding-prefix", "gastown."]
        if required_vars:
            args.extend(["--target-branch", "main", "--rig-name", "office-work"])
        if mode == "next":
            args.extend(["--completed-current", "current"])
        return subprocess.run(args, cwd=self.root, env=self.env,
                              capture_output=True, text=True, timeout=20)

    def test_unclaimed_existing_startup_resumes_without_pour(self):
        self.state.write_text(json.dumps({"rows": [refinery_row("current")], "current": None}))
        result = self.call("startup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["current"], "current")
        self.assertEqual(json.loads(self.state.read_text())["mutations"], [])

    def test_fresh_start_preserves_formula_and_required_vars(self):
        self.state.write_text(json.dumps({"rows": [], "current": None}))
        result = self.call("startup")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.state.read_text())
        pour = next(args for args in state["commands"] if args[:3] == ["bd", "mol", "wisp"])
        self.assertEqual(pour[3], FORMULA)
        for variable in ("target_branch=main", "rig_name=office-work", "binding_prefix=gastown."):
            self.assertIn(variable, pour)

    def test_successful_next_retry_does_not_retire_new_current(self):
        self.state.write_text(json.dumps({"rows": [refinery_row("current"), refinery_row("queued")],
                                         "current": "current"}))
        result = self.call("next")
        self.assertEqual(result.returncode, 0, result.stderr)
        before = self.state.read_text()
        repeated = self.call("next")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertTrue(json.loads(repeated.stdout)["replayed"])
        self.assertEqual(self.state.read_text(), before)

    def test_partial_assignment_latches_and_retry_never_pours_again(self):
        self.state.write_text(json.dumps({"rows": [refinery_row("current")], "current": "current",
                                         "fail_assignment": True}))
        self.assertNotEqual(self.call("next").returncode, 0)
        before = self.state.read_text()
        repeated = self.call("next")
        self.assertNotEqual(repeated.returncode, 0)
        self.assertIn("unfinished transition", repeated.stderr)
        self.assertEqual(self.state.read_text(), before)

    def test_missing_refinery_vars_does_not_run_a_claim(self):
        self.state.write_text(json.dumps({"rows": [], "current": None}))
        result = self.call("startup", required_vars=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires --target-branch and --rig-name", result.stderr)
        self.assertNotIn("commands", json.loads(self.state.read_text()))

    def test_switching_formula_cannot_bypass_pending_journal(self):
        self.state.write_text(json.dumps({"rows": [], "current": None}))
        directory = self.root / ".gc/witness-patrol-locks"
        directory.mkdir(parents=True)
        journal = directory / (hashlib.sha256(REFINERY.encode()).hexdigest() + ".state.json")
        journal.write_text(json.dumps({"pending": True, "phase": "pour", "formula": "mol-witness-patrol"}))
        result = self.call("startup")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unfinished transition", result.stderr)
        self.assertNotIn("commands", json.loads(self.state.read_text()))


if __name__ == "__main__":
    unittest.main()
