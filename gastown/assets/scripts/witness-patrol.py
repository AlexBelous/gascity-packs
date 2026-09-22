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
    result = subprocess.run(["gc", *args], capture_output=True, text=True, timeout=165 if protocol else 30)
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


def validate_store(store_rig, identities):
    """Use explicit runtime scope; never infer a store from an issue ID."""
    if store_rig is not None and (not isinstance(store_rig, str) or not store_rig or "/" in store_rig
                                   or any(char.isspace() for char in store_rig)
                                   or store_rig.startswith("-")):
        raise ReconcileNeeded("invalid patrol store rig")
    for identity in identities:
        if "/" in identity and identity.split("/", 1)[0] != store_rig:
            raise ReconcileNeeded("patrol store rig does not match scoped identity")
    return store_rig


def bd_args(store_rig):
    return ("bd", "--rig", store_rig) if store_rig else ("bd",)


def inventory(identities, store_rig):
    validate_store(store_rig, identities)
    rows = gc(*bd_args(store_rig), "list", "--type=molecule", "--include-infra", "--all", "--skip-labels", "--limit=0", "--json")
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


def pour(actor, binding_prefix, checkpoint, formula="mol-witness-patrol", refinery_vars=None, store_rig=None):
    validate_store(store_rig, {actor})
    checkpoint({"pending": True, "phase": "pour"})
    variables = ["--var", f"binding_prefix={binding_prefix}"]
    for key, value in (refinery_vars or {}).items():
        variables.extend(["--var", f"{key}={value}"])
    result = gc(*bd_args(store_rig), "mol", "wisp", formula, "--root-only", *variables, "--json")
    bead = result.get("new_epic_id") if isinstance(result, dict) else None
    if not bead:
        raise ReconcileNeeded("pour did not return a new root id; inspect before retry")
    result = subprocess.run(["gc", *bd_args(store_rig), "update", bead, f"--assignee={actor}"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ReconcileNeeded(f"poured {bead} but assignment failed; reconcile before retry")
    return bead


def run(mode, actor, identities, binding_prefix, checkpoint,
        formula="mol-witness-patrol", refinery_vars=None, completed_current=None, store_rig=None):
    validate_store(store_rig, identities | {actor})
    if store_rig and "/" not in actor:
        raise ReconcileNeeded("city patrol identity cannot be coerced into a rig store")
    if mode == "next" and not completed_current:
        raise ReconcileNeeded("next requires --completed-current from this cycle startup receipt; retain that id for retries")
    if mode == "startup":
        # Claim protocol distinguishes healthy no_work from backend/claim errors.
        current = gc("hook", "--claim", "--json", protocol=True)
        rows = inventory(identities, store_rig)
        if current is None:
            if rows:
                raise ReconcileNeeded("no_work conflicts with assigned active molecules")
            new = pour(actor, binding_prefix, checkpoint, formula, refinery_vars, store_rig)
            current = gc("hook", "--claim", "--json", protocol=True)
            if current != new:
                raise ReconcileNeeded(f"new patrol {new} was not claimed; do not pour again")
            rows = inventory(identities, store_rig)
        select(rows, current, formula)
        return {"action": "resume", "current": current}

    result = subprocess.run(["gc", "hook", "current", "--id-only"],
                            capture_output=True, text=True, timeout=30)
    current = result.stdout.strip()
    if result.returncode or not current:
        raise ReconcileNeeded("cannot confirm current session claim")
    if current != completed_current:
        raise ReconcileNeeded("completed-current does not match the session claim; preserve all patrols")
    next_bead = select(inventory(identities, store_rig), current, formula)
    if not next_bead:
        next_bead = pour(actor, binding_prefix, checkpoint, formula, refinery_vars, store_rig)
    # Re-read after pour/assignment and before retiring the completed current.
    if select(inventory(identities, store_rig), current, formula) != next_bead or next_bead == current:
        raise ReconcileNeeded("successor changed; preserve all molecules")
    confirmed = subprocess.run(["gc", "hook", "current", "--id-only"],
                               capture_output=True, text=True, timeout=30)
    if confirmed.returncode or confirmed.stdout.strip() != current:
        raise ReconcileNeeded("session claim changed; preserve all molecules")
    checkpoint({"pending": True, "phase": "burn", "current": current, "next": next_bead})
    result = subprocess.run(["gc", *bd_args(store_rig), "mol", "burn", current, "--force"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ReconcileNeeded(f"could not retire {current}; queued successor is {next_bead}")
    checkpoint({"pending": True, "phase": "claim", "current": current, "next": next_bead})
    claimed = gc("hook", "--claim", "--json", protocol=True)
    if claimed != next_bead:
        raise ReconcileNeeded(f"successor {next_bead} was not claimed after retirement")
    return {"action": "advanced", "current": current, "next": next_bead}


def recovery_current():
    result = subprocess.run(["gc", "hook", "current", "--id-only"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    expected = (f"gc hook current: session {os.environ['GC_SESSION_ID']} has no current claim "
                "(nothing claimed through gc hook --claim)")
    if result.returncode == 1 and not result.stdout.strip() and result.stderr.strip() == expected:
        return None
    raise ReconcileNeeded("cannot establish recovery session claim")


def require_retired(bead, store_rig):
    validate_store(store_rig, set())
    result = subprocess.run(["gc", *bd_args(store_rig), "--readonly", "show", bead, "--json"],
                            capture_output=True, text=True, timeout=30)
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise ReconcileNeeded("retired patrol lookup is not authoritative NotFound") from exc
    if (result.returncode != 1 or not isinstance(data, dict)
            or type(data.get("schema_version")) is not int or data["schema_version"] != 1
            or data.get("error") != "no issues found matching the provided IDs"):
        raise ReconcileNeeded("old patrol still exists or its absence cannot be established")
    return {"exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def write_evidence(path, data):
    # Exclusive, durable evidence: never overwrite the original pending journal.
    with path.open("x") as handle:
        json.dump(data, handle)
        handle.flush()
        os.fsync(handle.fileno())


def evidence_peer(attempt, kind):
    suffix = ".attempt.json"
    if not attempt.name.endswith(suffix):
        raise ReconcileNeeded("invalid recovery attempt evidence path")
    return attempt.with_name(attempt.name[:-len(suffix)] + f".{kind}.json")


def evidence_json(path, label):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ReconcileNeeded(f"invalid {label} evidence") from exc
    if not isinstance(data, dict):
        raise ReconcileNeeded(f"invalid {label} evidence")
    return data


def timeout_text(value):
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else str(value)


def claim_with_receipt(attempt, expected_next):
    """Run at most one claim for an immutable attempt and persist its exact outcome."""
    outcome = evidence_peer(attempt, "outcome")
    if outcome.exists():
        receipt = evidence_json(outcome, "claim outcome")
    else:
        command = ["gc", "hook", "--claim", "--json"]
        session = os.environ.get("GC_SESSION_ID")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=165)
            receipt = {"schema_version": 1, "session_id": session, "command": command,
                       "status": "completed", "exit": result.returncode,
                       "stdout": result.stdout, "stderr": result.stderr}
        except subprocess.TimeoutExpired as exc:
            receipt = {"schema_version": 1, "session_id": session, "command": command,
                       "status": "timeout", "exit": None,
                       "stdout": timeout_text(exc.stdout), "stderr": timeout_text(exc.stderr)}
        except OSError as exc:
            receipt = {"schema_version": 1, "session_id": session, "command": command,
                       "status": "exception", "exit": None, "stdout": "", "stderr": repr(exc)}
        write_evidence(outcome, receipt)
    if (type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1
            or receipt.get("session_id") != os.environ.get("GC_SESSION_ID")
            or receipt.get("command") != ["gc", "hook", "--claim", "--json"]
            or receipt.get("status") not in ("completed", "timeout", "exception")
            or not isinstance(receipt.get("stdout"), str)
            or not isinstance(receipt.get("stderr"), str)):
        raise ReconcileNeeded("claim outcome receipt is invalid or belongs to another session")
    if receipt["status"] != "completed":
        raise ReconcileNeeded(f"recovery claim {receipt['status']}; use guarded retry token after live reconciliation")
    try:
        data = json.loads(receipt["stdout"])
    except ValueError as exc:
        raise ReconcileNeeded("recorded recovery claim returned invalid JSON") from exc
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise ReconcileNeeded("recorded recovery claim failed")
    if (receipt.get("exit") == 0 and data.get("action") == "work"
            and data.get("bead_id")):
        if data["bead_id"] != expected_next:
            raise ReconcileNeeded("recorded recovery claim returned another bead")
        return data["bead_id"]
    if (receipt.get("exit") in (0, 1) and data.get("action") == "drain"
            and data.get("reason") == "no_work"):
        return None
    raise ReconcileNeeded("recorded recovery claim did not report work or authoritative no_work")


def retry_token(attempt):
    return hashlib.sha256(attempt.read_bytes()).hexdigest()


def recover(state, state_path, identities, formula, completed, expected_next, store_rig=None,
            claim_retry_token=None, reconcile_only=False):
    validate_store(store_rig, identities)
    if "store_rig" in state and state["store_rig"] != store_rig:
        raise ReconcileNeeded("recovery journal store rig does not match runtime")
    if (state.get("pending") is not True or state.get("phase") not in ("burn", "claim")
            or not completed or not expected_next or completed == expected_next
            or state.get("current") != completed or state.get("next") != expected_next
            or state.get("formula", formula) != formula):
        raise ReconcileNeeded("recovery IDs/formula do not match a pending retirement")
    absence = require_retired(completed, store_rig)
    rows = inventory(identities, store_rig)
    if len(rows) != 1 or select(rows, expected_next, formula) is not None:
        raise ReconcileNeeded("recovery needs exactly one verified owned successor")
    current = recovery_current()
    if current not in (None, completed, expected_next):
        raise ReconcileNeeded("recovery session claim points to unrelated work")
    original = state_path.read_bytes()
    key = hashlib.sha256(original).hexdigest()
    archive = state_path.with_name(state_path.name + "." + key + ".original")
    if archive.exists():
        if archive.read_bytes() != original:
            raise ReconcileNeeded("original recovery journal archive mismatch")
    else:
        with archive.open("xb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
    attempt = archive.with_suffix(".attempt.json")
    if current != expected_next:
        if reconcile_only:
            detail = (f"; retry-token={retry_token(attempt)}" if attempt.exists()
                      else "; no prior claim attempt exists")
            raise ReconcileNeeded(f"reconcile-only found no current successor{detail}")
        if not attempt.exists():
            if claim_retry_token:
                raise ReconcileNeeded("claim retry token has no matching prior attempt")
            write_evidence(attempt, {"schema_version": 1, "original": str(archive),
                                    "formula": formula, "store_rig": store_rig,
                                    "session_id": os.environ.get("GC_SESSION_ID"),
                                    "expected_next": expected_next,
                                    "old_not_found": absence, "inventory": rows,
                                    "claim_before": current, "claim_attempted": True})
            claim_attempt = attempt
        else:
            prior = evidence_json(attempt, "claim attempt")
            token = retry_token(attempt)
            retry = evidence_peer(attempt, "retry.attempt")
            if retry.exists():
                raise ReconcileNeeded("guarded recovery retry was already attempted; reconcile current claim only")
            if claim_retry_token != token:
                raise ReconcileNeeded(f"prior recovery claim requires live reconciliation; retry-token={token}")
            prior_inventory = prior.get("inventory")
            prior_expected = prior.get("expected_next")
            if prior_expected is None and isinstance(prior_inventory, list) and len(prior_inventory) == 1:
                # Compatibility for attempts written by the original guarded-recovery
                # release.  The explicit token turns this into a recorded handoff; live
                # current + two stable inventory reads remain authoritative.
                prior_expected = prior_inventory[0].get("id")
            if (prior.get("claim_attempted") is not True or prior_expected != expected_next
                    or prior.get("formula") != formula or prior.get("store_rig") != store_rig):
                raise ReconcileNeeded("prior recovery attempt does not identify this successor")
            if current is not None or rows[0].get("status") != "open":
                raise ReconcileNeeded("guarded retry requires no current claim and one open successor")
            stable_rows = inventory(identities, store_rig)
            if stable_rows != rows or len(stable_rows) != 1 or stable_rows[0].get("id") != expected_next:
                raise ReconcileNeeded("successor inventory is not stable enough for guarded retry")
            write_evidence(retry, {"schema_version": 1, "prior_attempt": str(attempt),
                                  "prior_session_id": prior.get("session_id"),
                                  "session_id": os.environ.get("GC_SESSION_ID"),
                                  "expected_next": expected_next, "inventory": stable_rows,
                                  "retry_token": claim_retry_token,
                                  "legacy_handoff": not bool(prior.get("session_id"))})
            claim_attempt = retry
        try:
            claimed = claim_with_receipt(claim_attempt, expected_next)
        except ReconcileNeeded as exc:
            raise ReconcileNeeded(f"{exc}; retry-token={retry_token(attempt)}") from exc
        if claimed != expected_next:
            raise ReconcileNeeded(f"recovery claim did not return the recorded successor; "
                                  f"retry-token={retry_token(attempt)}")
    if recovery_current() != expected_next:
        raise ReconcileNeeded("recovery successor claim readback failed")
    # Recheck the store after claim. No recovery path pours or burns anything.
    require_retired(completed, store_rig)
    rows = inventory(identities, store_rig)
    if len(rows) != 1 or select(rows, expected_next, formula) is not None:
        raise ReconcileNeeded("recovery inventory changed after claim")
    result = {"action": "advanced", "current": completed, "next": expected_next,
              "recovered": True, "original_journal": str(archive)}
    evidence = archive.with_suffix(".verified.json")
    if not evidence.exists():
        write_evidence(evidence, {"result": result, "inventory": rows,
                                  "claim_after": expected_next, "store_rig": store_rig})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("startup", "next", "recover"))
    parser.add_argument("--binding-prefix", default="")
    parser.add_argument("--formula", choices=("mol-witness-patrol", "mol-refinery-patrol"),
                        default="mol-witness-patrol")
    parser.add_argument("--completed-current")
    parser.add_argument("--expected-next")
    parser.add_argument("--target-branch")
    parser.add_argument("--rig-name")
    parser.add_argument("--store-rig", help="expected BD rig; defaults to GC_RIG (empty for HQ)")
    parser.add_argument("--claim-retry-token",
                        help="one-time guarded claim retry/handoff token emitted by recover")
    parser.add_argument("--reconcile-only", action="store_true",
                        help="inspect native current + inventory without issuing a claim")
    args = parser.parse_args()
    if args.mode == "next" and not args.completed_current:
        raise ReconcileNeeded("next requires --completed-current from this cycle startup receipt; retain that id for retries")
    if args.reconcile_only and (args.mode != "recover" or args.claim_retry_token):
        raise ReconcileNeeded("--reconcile-only is only valid for recover without a retry token")
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
    runtime_rig = os.environ.get("GC_RIG") or None
    store_rig = args.store_rig if args.store_rig is not None else runtime_rig
    if args.store_rig is not None and runtime_rig is not None and store_rig != runtime_rig:
        raise ReconcileNeeded("explicit patrol store rig does not match GC_RIG")
    validate_store(store_rig, identities | {os.environ.get("GC_TEMPLATE") or actor})
    if store_rig and "/" not in actor:
        raise ReconcileNeeded("city patrol identity cannot be coerced into a rig store")
    if refinery_vars and args.rig_name != store_rig:
        raise ReconcileNeeded("refinery rig-name does not match patrol store rig")
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
            if (not isinstance(state, dict)
                    or (state.get("pending") is not False and args.mode != "recover")):
                raise ReconcileNeeded(f"unfinished transition; inspect {state_path} before retry")

        if "store_rig" in state and state["store_rig"] != store_rig:
            raise ReconcileNeeded("transition journal store rig does not match runtime")
        previous = state.get("result", {})
        if not isinstance(previous, dict):
            raise ReconcileNeeded("invalid transition receipt; preserve the journal")
        if (state.get("pending") is False
                and args.mode in ("next", "recover") and state.get("formula") == args.formula
                and previous.get("action") == "advanced"
                and previous.get("current") == args.completed_current
                and (args.mode != "recover" or previous.get("next") == args.expected_next)):
            print(json.dumps(dict(previous, replayed=True)))
            return

        def checkpoint(state):
            # Persist before mutation: crash/timeout must not trigger a second pour.
            state = dict(state, formula=args.formula, store_rig=store_rig)
            with state_path.open("w") as handle:
                json.dump(state, handle)
                handle.flush()
                os.fsync(handle.fileno())

        if args.mode == "recover":
            result = recover(state, state_path, identities, args.formula,
                             args.completed_current, args.expected_next, store_rig,
                             args.claim_retry_token, args.reconcile_only)
        else:
            result = run(args.mode, actor, identities, args.binding_prefix, checkpoint,
                         args.formula, refinery_vars, args.completed_current, store_rig)
        checkpoint({"pending": False, "formula": args.formula, "result": result})
        print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (ReconcileNeeded, OSError, subprocess.TimeoutExpired) as exc:
        print(f"RECONCILE_NEEDED: {exc}", file=sys.stderr)
        sys.exit(1)
