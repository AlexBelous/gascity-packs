import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "assets/scripts/witness-patrol.py"
SPEC = importlib.util.spec_from_file_location("witness_patrol", SCRIPT)
patrol = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(patrol)
ACTOR = "office-work/gastown.witness"


def row(bead, status="open"):
    return dict(id=bead, status=status, assignee=ACTOR,
                title="mol-witness-patrol", issue_type="molecule", ephemeral=True)


class City:
    def __init__(self, rows, current=None, fail_read=False, fail_assignment=False):
        self.rows = rows
        self.current = current
        self.fail_read = fail_read
        self.fail_assignment = fail_assignment
        self.mutations = []
        self.checkpoints = []
        self.force_no_work = False
        self.commands = []

    def call(self, command, **kwargs):
        self.commands.append(command)
        args = command[1:]
        if args[:1] == ["bd"]:
            assert args[1:3] == ["--rig", "office-work"], command
            args = ["bd", *args[3:]]
        code, data = 0, None
        if args == ["hook", "current", "--id-only"]:
            return subprocess.CompletedProcess(command, 0 if self.current else 1,
                                               self.current or "", "no current")
        if args == ["hook", "--claim", "--json"]:
            if not self.current and self.rows and not self.force_no_work:
                self.current = self.rows[0]["id"]
            data = ({"ok": True, "action": "work", "bead_id": self.current}
                    if self.current else {"ok": True, "action": "drain", "reason": "no_work"})
            code = 0 if self.current else 1
        elif args[:2] == ["bd", "list"]:
            assert "--include-infra" in args and "--all" in args and "--skip-labels" in args
            if self.fail_read:
                return subprocess.CompletedProcess(command, 1, "", "database unavailable")
            data = {"issues": self.rows, "meta": {"count": len(self.rows), "skip_labels": True},
                    "schema_version": 1}
        elif args[:3] == ["bd", "mol", "wisp"]:
            self.mutations.append(("pour", "next"))
            next_row = row("next")
            next_row["title"] = args[3]
            self.rows.append(next_row)
            data = {"new_epic_id": "next"}
        elif args[:2] == ["bd", "update"]:
            self.mutations.append(("assign", args[2]))
            code = 1 if self.fail_assignment else 0
            if code == 0:
                for item in self.rows:
                    if item["id"] == args[2]:
                        item["assignee"] = args[3].split("=", 1)[1]
        elif args[:3] == ["bd", "mol", "burn"]:
            self.mutations.append(("burn", args[3]))
            self.rows = [item for item in self.rows if item["id"] != args[3]]
            self.current = None
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, code, json.dumps(data), "fixture error" if code else "")

    def run(self, mode="next"):
        with patch.object(patrol.subprocess, "run", self.call):
            return patrol.run(mode, ACTOR, {ACTOR}, "gastown.", self.checkpoints.append,
                              completed_current=self.current or "unknown", store_rig="office-work")


class PatrolTests(unittest.TestCase):
    def test_open_current_is_excluded_from_open_next(self):
        city = City([row("current"), row("queued")], "current")
        self.assertEqual(city.run()["next"], "queued")
        self.assertEqual(city.mutations, [("burn", "current")])

    def test_unknown_current_preserves_everything(self):
        city = City([row("queued")])
        with self.assertRaises(patrol.ReconcileNeeded):
            city.run()
        self.assertEqual(city.mutations, [])

    def test_multiple_open_successors_are_not_burned_or_replaced(self):
        city = City([row("current"), row("one"), row("two")], "current")
        with self.assertRaises(patrol.ReconcileNeeded):
            city.run()
        self.assertEqual(city.mutations, [])

    def test_single_queued_successor_for_in_progress_current(self):
        city = City([row("current", "in_progress"), row("queued")], "current")
        self.assertEqual(city.run()["next"], "queued")
        self.assertEqual(city.mutations, [("burn", "current")])

    def test_confirmed_current_without_next_pours_once(self):
        city = City([row("current")], "current")
        self.assertEqual(city.run()["next"], "next")
        self.assertEqual(city.mutations, [("pour", "next"), ("assign", "next"), ("burn", "current")])

    def test_normal_fresh_start_claims_bootstrap_without_burning(self):
        city = City([])
        self.assertEqual(city.run("startup"), {"action": "resume", "current": "next"})
        self.assertEqual(city.mutations, [("pour", "next"), ("assign", "next")])

    def test_inventory_error_is_not_empty_fresh_start(self):
        city = City([], fail_read=True)
        with self.assertRaises(patrol.ReconcileNeeded):
            city.run("startup")
        self.assertEqual(city.mutations, [])

    def test_partial_assignment_does_not_burn_and_leaves_pending_intent(self):
        city = City([row("current")], "current", fail_assignment=True)
        with self.assertRaises(patrol.ReconcileNeeded):
            city.run()
        self.assertNotIn(("burn", "current"), city.mutations)
        self.assertTrue(city.checkpoints[-1]["pending"])

    def test_other_formula_is_not_a_successor(self):
        other = row("other")
        other["title"] = "mol-refinery-patrol"
        city = City([row("current"), other], "current")
        with self.assertRaises(patrol.ReconcileNeeded):
            city.run()
        self.assertEqual(city.mutations, [])

    def test_fresh_no_work_with_owned_nonready_molecule_never_pours(self):
        for status in ("blocked", "hooked", "deferred", "unknown"):
            with self.subTest(status=status):
                city = City([row("existing", status)])
                city.force_no_work = True
                with self.assertRaises(patrol.ReconcileNeeded):
                    city.run("startup")
                self.assertEqual(city.mutations, [])

    def test_current_unknown_status_preserves_everything(self):
        city = City([row("current", "unknown")], "current")
        with self.assertRaises(patrol.ReconcileNeeded):
            city.run()
        self.assertEqual(city.mutations, [])

    def test_claim_error_is_not_authoritative_no_work(self):
        response = subprocess.CompletedProcess([], 1, json.dumps({
            "ok": True, "action": "drain", "reason": "claims_errored"}), "")
        with patch.object(patrol.subprocess, "run", return_value=response):
            with self.assertRaises(patrol.ReconcileNeeded):
                patrol.gc("hook", "--claim", "--json", protocol=True)

    def test_inventory_accepts_observed_envelope_and_filters_other_owner(self):
        other = row("refinery")
        other.update(assignee="office-work/gastown.refinery", title="mol-refinery-patrol")
        envelope = {"schema_version": 1, "issues": [row("current"), other],
                    "meta": {"count": 2, "skip_labels": True}}
        with patch.object(patrol, "gc", return_value=envelope):
            self.assertEqual([item["id"] for item in patrol.inventory({ACTOR}, "office-work")], ["current"])

    def test_inventory_rejects_malformed_or_truncated_envelopes(self):
        cases = [
            {},
            {"schema_version": 1, "issues": [], "meta": {"count": 2, "skip_labels": True}},
            {"schema_version": 1, "issues": [], "meta": {"count": 0, "skip_labels": False}},
            {"schema_version": 2, "issues": [], "meta": {"count": 0, "skip_labels": True}},
            {"schema_version": 1, "issues": [], "meta": {"count": 0, "skip_labels": True, "has_more": True}},
        ]
        for envelope in cases:
            with self.subTest(envelope=envelope), patch.object(patrol, "gc", return_value=envelope):
                with self.assertRaises(patrol.ReconcileNeeded):
                    patrol.inventory({ACTOR}, "office-work")

    def test_inventory_keeps_legacy_array_compatibility(self):
        with patch.object(patrol, "gc", return_value=[row("current")]):
            self.assertEqual(patrol.inventory({ACTOR}, "office-work"), [row("current")])

    def test_pending_journal_blocks_retry_before_any_gc_command(self):
        # Retain the tiny isolated fixture for inspection; never touch a city.
        city = Path(tempfile.mkdtemp(prefix="witness-pending-"))
        lock_dir = city / ".gc/witness-patrol-locks"
        lock_dir.mkdir(parents=True)
        key = hashlib.sha256(ACTOR.encode()).hexdigest()
        (lock_dir / (key + ".state.json")).write_text('{"pending":true,"phase":"pour"}')
        with patch.dict(patrol.os.environ, {"GC_CITY_PATH": str(city), "GC_AGENT": ACTOR,
                                          "GC_SESSION_ID": "session-fixture", "GC_RIG": "office-work",
                                          "GC_TEMPLATE": ACTOR}), \
                patch.object(patrol.sys, "argv", [str(SCRIPT), "startup"]), \
                patch.object(patrol.subprocess, "run") as command:
            with self.assertRaisesRegex(patrol.ReconcileNeeded, "unfinished transition"):
                patrol.main()
            command.assert_not_called()


    def test_all_bd_operations_use_exact_expected_rig(self):
        city = City([row("current")], "current")
        city.run()
        commands = [command for command in city.commands if command[1] == "bd"]
        self.assertEqual(commands, [
            ["gc", "bd", "--rig", "office-work", "list", "--type=molecule", "--include-infra",
             "--all", "--skip-labels", "--limit=0", "--json"],
            ["gc", "bd", "--rig", "office-work", "mol", "wisp", "mol-witness-patrol",
             "--root-only", "--var", "binding_prefix=gastown.", "--json"],
            ["gc", "bd", "--rig", "office-work", "update", "next", "--assignee=" + ACTOR],
            ["gc", "bd", "--rig", "office-work", "list", "--type=molecule", "--include-infra",
             "--all", "--skip-labels", "--limit=0", "--json"],
            ["gc", "bd", "--rig", "office-work", "mol", "burn", "current", "--force"],
        ])

    def test_scope_mismatch_or_missing_scope_rejects_before_any_command(self):
        for rig in [None, "other-rig", "", "../office-work", "--help"]:
            with self.subTest(rig=rig), patch.object(patrol.subprocess, "run") as command:
                with self.assertRaises(patrol.ReconcileNeeded):
                    patrol.run("startup", ACTOR, {ACTOR}, "gastown.", lambda state: None,
                               store_rig=rig)
                command.assert_not_called()

    def test_city_identity_cannot_be_coerced_into_rig_store(self):
        with patch.object(patrol.subprocess, "run") as command:
            with self.assertRaisesRegex(patrol.ReconcileNeeded, "city patrol identity"):
                patrol.run("startup", "gastown.witness", {"gastown.witness"}, "gastown.",
                           lambda state: None, store_rig="office-work")
            command.assert_not_called()

    def test_hq_store_is_retained_for_city_identity(self):
        envelope = {"schema_version": 1, "issues": [], "meta": {"count": 0, "skip_labels": True}}
        with patch.object(patrol, "gc", return_value=envelope) as command:
            self.assertEqual(patrol.inventory({"gastown.witness"}, None), [])
            self.assertEqual(command.call_args.args,
                             ("bd", "list", "--type=molecule", "--include-infra", "--all",
                              "--skip-labels", "--limit=0", "--json"))


if __name__ == "__main__":
    unittest.main()
