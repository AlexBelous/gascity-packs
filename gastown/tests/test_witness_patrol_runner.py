"""Opt-in installed-gc command discovery against isolated, mocked city state."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_witness_patrol import ACTOR, row


@unittest.skipUnless(os.environ.get("GC_TEST_BIN"), "set GC_TEST_BIN to test command dispatch")
class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="witness-runner-"))
        pack = Path(__file__).resolve().parents[1]
        (self.root / "city.toml").write_text(
            '[workspace]\nname="witness-runner-fixture"\n[imports.gastown]\nsource='
            + json.dumps(str(pack)) + '\n')
        binary = self.root / "bin"
        binary.mkdir()
        fake = binary / "gc"
        fake.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
sys.path.insert(0, os.environ["WITNESS_TEST_MODULES"])
from test_witness_patrol import City
p = pathlib.Path(os.environ["WITNESS_TEST_STATE"])
s = json.loads(p.read_text())
city = City(s["rows"], s.get("current"))
r = city.call(sys.argv)
s.update(rows=city.rows, current=city.current)
s.setdefault("commands", []).append(sys.argv[1:])
s.setdefault("mutations", []).extend(city.mutations)
p.write_text(json.dumps(s))
sys.stdout.write(r.stdout)
sys.stderr.write(r.stderr)
sys.exit(r.returncode)
''')
        fake.chmod(0o755)
        self.state = self.root / "state.json"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("GC_", "BD_", "BEADS_", "DOLT_"))}
        self.env.update(PATH=str(binary) + os.pathsep + os.environ["PATH"],
                        GC_AGENT=ACTOR, GC_ALIAS="witness-alias-a", GC_TEMPLATE=ACTOR,
                        GC_SESSION_ID="isolated-session", PYTHONDONTWRITEBYTECODE="1",
                        WITNESS_TEST_MODULES=str(Path(__file__).resolve().parent),
                        WITNESS_TEST_STATE=str(self.state))

    def call(self, mode):
        return subprocess.run([os.environ["GC_TEST_BIN"], "gastown", "witness-patrol",
                               mode, "--binding-prefix", "gastown."],
                              cwd=self.root, env=self.env, capture_output=True,
                              text=True, timeout=20)

    def test_registered_command_preserves_arguments_and_claims_successor(self):
        self.state.write_text(json.dumps({"rows": [row("current"), row("queued")],
                                         "current": "current"}))
        result = self.call("next")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["next"], "queued")
        state = json.loads(self.state.read_text())
        self.assertEqual(state["current"], "queued")
        self.assertEqual(state["mutations"], [["burn", "current"]])

    def test_registered_fresh_start_preserves_binding_and_no_work_protocol(self):
        self.state.write_text(json.dumps({"rows": [], "current": None}))
        result = self.call("startup")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["current"], "next")
        pour = next(args for args in state["commands"] if args[:3] == ["bd", "mol", "wisp"])
        self.assertIn("binding_prefix=gastown.", pour)
        self.assertEqual(state["mutations"], [["pour", "next"], ["assign", "next"]])

    def test_different_alias_cannot_bypass_same_agent_transition_lock(self):
        self.state.write_text(json.dumps({"rows": [], "current": None}))
        directory = self.root / ".gc/witness-patrol-locks"
        directory.mkdir(parents=True)
        lock = directory / (hashlib.sha256(ACTOR.encode()).hexdigest() + ".lock")
        with lock.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.env["GC_ALIAS"] = "witness-alias-b"
            result = self.call("startup")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another patrol transition is active", result.stderr)
        self.assertNotIn("commands", json.loads(self.state.read_text()))


if __name__ == "__main__":
    unittest.main()
