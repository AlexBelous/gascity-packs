#!/usr/bin/env python3
"""Advance only a confirmed patrol claim; never burn surplus molecules."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


class ReconcileNeeded(Exception):
    pass


def gc(*args, protocol=False):
    result = subprocess.run(["gc", *args], capture_output=True, text=True, timeout=30)
    if result.returncode and not protocol:
        raise ReconcileNeeded(f"gc {' '.join(args)} failed: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise ReconcileNeeded("gc returned invalid JSON") from exc
    if protocol:
        if not isinstance(data, dict):
            raise ReconcileNeeded("claim result is not an object")
        if data.get("ok") is not True:
            raise ReconcileNeeded("claim failed")
        if data.get("action") == "work" and result.returncode == 0 and data.get("bead_id"):
            return data["bead_id"]
        if data.get("action") == "drain" and data.get("reason") == "no_work" and result.returncode in (0, 1):
            return None
        raise ReconcileNeeded("claim did not report work or authoritative no_work")
    return data


def inventory(identities):
    rows = gc("bd", "list", "--type=molecule", "--include-infra", "--all", "--skip-labels", "--limit=0", "--json")
    if isinstance(rows, dict):
        # bd --skip-labels uses a versioned envelope, not the legacy array.
        meta = rows.get("meta")
        issues = rows.get("issues")
        if (type(rows.get("schema_version")) is not int or rows["schema_version"] != 1
                or not isinstance(meta, dict) or not isinstance(issues, list)
                or type(meta.get("count")) is not int or meta["count"] != len(issues)
                or meta.get("skip_labels") is not True
                or meta.get("has_more") or meta.get("truncated") or meta.get("next_cursor")):
            raise ReconcileNeeded("molecule inventory envelope is unsupported or incomplete")
        rows = issues
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ReconcileNeeded("molecule inventory is not an array of records")
    for row in rows:
        if (not isinstance(row.get("id"), str) or not row["id"]
                or not isinstance(row.get("status"), str)
                or row.get("issue_type") != "molecule"
                or not isinstance(row.get("title"), str)
                or (row.get("assignee") is not None and not isinstance(row["assignee"], str))):
            raise ReconcileNeeded("molecule inventory contains an invalid record")
    return [row for row in rows if row.get("assignee") in identities
            and row.get("status") != "closed"]


def select(rows, current, formula="mol-witness-patrol"):
    """Return at most one queued successor, excluding current even if it is open."""
    if not current:
        raise ReconcileNeeded("current claim is unknown")
    matches = [row for row in rows if row.get("id") == current]
    if len(matches) != 1:
        raise ReconcileNeeded("current claim is absent or ambiguous in active owned inventory")
    for row in rows:
        if row.get("status") not in ("open", "in_progress"):
            raise ReconcileNeeded("owned patrol has blocked, deferred or unknown lifecycle status")
        if (row.get("issue_type") != "molecule" or row.get("title") != formula
                or row.get("ephemeral") is not True):
            raise ReconcileNeeded("owned inventory includes an unverified patrol molecule")
    others = [row for row in rows if row.get("id") != current]
    if len(others) > 1 or any(row.get("status") != "open" for row in others):
        raise ReconcileNeeded("multiple or already active successors require reconciliation")
    return others[0]["id"] if others else None


def pour(actor, binding_prefix, checkpoint, formula="mol-witness-patrol", refinery_vars=None):
    checkpoint({"pending": True, "phase": "pour"})
    variables = ["--var", f"binding_prefix={binding_prefix}"]
    for key, value in (refinery_vars or {}).items():
        variables.extend(["--var", f"{key}={value}"])
    result = gc("bd", "mol", "wisp", formula, "--root-only", *variables, "--json")
    bead = result.get("new_epic_id") if isinstance(result, dict) else None
    if not bead:
        raise ReconcileNeeded("pour did not return a new root id; inspect before retry")
    result = subprocess.run(["gc", "bd", "update", bead, f"--assignee={actor}"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ReconcileNeeded(f"poured {bead} but assignment failed; reconcile before retry")
    return bead


def run(mode, actor, identities, binding_prefix, checkpoint,
        formula="mol-witness-patrol", refinery_vars=None, completed_current=None):
    if mode == "next" and not completed_current:
        raise ReconcileNeeded("next requires --completed-current from this cycle startup receipt; retain that id for retries")
    if mode == "startup":
        # Claim protocol distinguishes healthy no_work from backend/claim errors.
        current = gc("hook", "--claim", "--json", protocol=True)
        rows = inventory(identities)
        if current is None:
            if rows:
                raise ReconcileNeeded("no_work conflicts with assigned active molecules")
            new = pour(actor, binding_prefix, checkpoint, formula, refinery_vars)
            current = gc("hook", "--claim", "--json", protocol=True)
            if current != new:
                raise ReconcileNeeded(f"new patrol {new} was not claimed; do not pour again")
            rows = inventory(identities)
        select(rows, current, formula)
        return {"action": "resume", "current": current}

    result = subprocess.run(["gc", "hook", "current", "--id-only"],
                            capture_output=True, text=True, timeout=30)
    current = result.stdout.strip()
    if result.returncode or not current:
        raise ReconcileNeeded("cannot confirm current session claim")
    if current != completed_current:
        raise ReconcileNeeded("completed-current does not match the session claim; preserve all patrols")
    next_bead = select(inventory(identities), current, formula)
    if not next_bead:
        next_bead = pour(actor, binding_prefix, checkpoint, formula, refinery_vars)
    # Re-read after pour/assignment and before retiring the completed current.
    if select(inventory(identities), current, formula) != next_bead or next_bead == current:
        raise ReconcileNeeded("successor changed; preserve all molecules")
    confirmed = subprocess.run(["gc", "hook", "current", "--id-only"],
                               capture_output=True, text=True, timeout=30)
    if confirmed.returncode or confirmed.stdout.strip() != current:
        raise ReconcileNeeded("session claim changed; preserve all molecules")
    checkpoint({"pending": True, "phase": "burn", "current": current, "next": next_bead})
    result = subprocess.run(["gc", "bd", "mol", "burn", current, "--force"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ReconcileNeeded(f"could not retire {current}; queued successor is {next_bead}")
    claimed = gc("hook", "--claim", "--json", protocol=True)
    if claimed != next_bead:
        raise ReconcileNeeded(f"successor {next_bead} was not claimed after retirement")
    return {"action": "advanced", "current": current, "next": next_bead}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("startup", "next"))
    parser.add_argument("--binding-prefix", default="")
    parser.add_argument("--formula", choices=("mol-witness-patrol", "mol-refinery-patrol"),
                        default="mol-witness-patrol")
    parser.add_argument("--completed-current")
    parser.add_argument("--target-branch")
    parser.add_argument("--rig-name")
    args = parser.parse_args()
    if args.mode == "next" and not args.completed_current:
        raise ReconcileNeeded("next requires --completed-current from this cycle startup receipt; retain that id for retries")
    refinery_vars = None
    if args.formula == "mol-refinery-patrol":
        if not args.target_branch or not args.rig_name:
            raise ReconcileNeeded("refinery requires --target-branch and --rig-name")
        refinery_vars = {"target_branch": args.target_branch, "rig_name": args.rig_name}
    elif args.target_branch is not None or args.rig_name is not None:
        raise ReconcileNeeded("refinery variables cannot be passed to a witness patrol")
    actor = os.environ.get("GC_AGENT")
    city = os.environ.get("GC_CITY_PATH")
    session = os.environ.get("GC_SESSION_ID")
    if not actor or not city or not session:
        raise ReconcileNeeded("city, agent and session identity are required")
    identities = {value for value in (actor, session, os.environ.get("GC_ALIAS")) if value}
    lock_dir = Path(city) / ".gc" / "witness-patrol-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / (hashlib.sha256((os.environ.get("GC_TEMPLATE") or actor).encode()).hexdigest() + ".lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReconcileNeeded("another patrol transition is active") from exc
        state_path = lock_path.with_suffix(".state.json")
        state = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
            except ValueError as exc:
                raise ReconcileNeeded(f"invalid transition journal: {state_path}") from exc
            if not isinstance(state, dict) or state.get("pending") is not False:
                raise ReconcileNeeded(f"unfinished transition; inspect {state_path} before retry")

        previous = state.get("result", {})
        if not isinstance(previous, dict):
            raise ReconcileNeeded("invalid transition receipt; preserve the journal")
        if (args.mode == "next" and state.get("formula") == args.formula
                and previous.get("action") == "advanced"
                and previous.get("current") == args.completed_current):
            print(json.dumps(dict(previous, replayed=True)))
            return

        def checkpoint(state):
            # Persist before mutation: crash/timeout must not trigger a second pour.
            with state_path.open("w") as handle:
                json.dump(state, handle)
                handle.flush()
                os.fsync(handle.fileno())

        result = run(args.mode, actor, identities, args.binding_prefix, checkpoint,
                     args.formula, refinery_vars, args.completed_current)
        checkpoint({"pending": False, "formula": args.formula, "result": result})
        print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (ReconcileNeeded, OSError, subprocess.TimeoutExpired) as exc:
        print(f"RECONCILE_NEEDED: {exc}", file=sys.stderr)
        sys.exit(1)
