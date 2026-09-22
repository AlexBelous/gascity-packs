import json
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_witness_patrol import ACTOR, City, patrol, row


class RecoveryCity(City):
    def __init__(self, current='old'):
        super().__init__([row('next')], current)
        self.old_exists = False
        self.claim_wrong = False
        self.claim_timeout = False
        self.claim_no_work = 0
        self.claims = 0
        self.absence_checks = 0
        self.absence_response = None
        self.inventory_override = None
        self.old_reappears = False
        self.post_claim_ambiguity = False

    def call(self, command, **kwargs):
        args = command[1:]
        if args == ['bd', '--rig', 'office-work', '--readonly', 'show', 'old', '--json']:
            self.absence_checks += 1
            if self.absence_response is not None:
                return self.absence_response
            if self.old_exists or (self.old_reappears and self.absence_checks > 1):
                return subprocess.CompletedProcess(command, 0, json.dumps([row('old')]), '')
            return subprocess.CompletedProcess(command, 1, json.dumps({
                'schema_version': 1, 'error': 'no issues found matching the provided IDs'}),
                'Issue old not found\nTry history for purged issues')
        if args[:4] == ['bd', '--rig', 'office-work', 'list'] and self.inventory_override is not None:
            return subprocess.CompletedProcess(command, 0, json.dumps(self.inventory_override), '')
        if args == ['hook', '--claim', '--json']:
            self.claims += 1
            if self.claim_timeout:
                raise subprocess.TimeoutExpired(command, kwargs['timeout'])
            if self.claim_no_work:
                self.claim_no_work -= 1
                return subprocess.CompletedProcess(command, 1, json.dumps({
                    'ok': True, 'action': 'drain', 'reason': 'no_work'}), 'no work')
            self.current = 'foreign' if self.claim_wrong else 'next'
            if self.post_claim_ambiguity:
                self.rows.append(row('unexpected'))
        if args == ['hook', 'current', '--id-only'] and self.current is None:
            return subprocess.CompletedProcess(command, 1, '',
                f"gc hook current: session {os.environ['GC_SESSION_ID']} has no current claim "
                '(nothing claimed through gc hook --claim)')
        return super().call(command, **kwargs)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='patrol-recovery-'))
        self.path = self.root / 'pending.state.json'
        self.state = {'pending': True, 'phase': 'burn', 'current': 'old', 'next': 'next'}
        self.original = json.dumps(self.state)
        self.path.write_text(self.original)

    def recover(self, city, completed='old', expected='next', token=None,
                session='session-fixture', formula='mol-witness-patrol', reconcile_only=False):
        with patch.object(patrol.subprocess, 'run', city.call), \
                patch.dict(patrol.os.environ, GC_SESSION_ID=session):
            return patrol.recover(self.state, self.path, {ACTOR}, formula,
                                  completed, expected, "office-work", token, reconcile_only)

    def attempt(self):
        attempts = [path for path in self.root.glob('*.attempt.json')
                    if not path.name.endswith('.retry.attempt.json')]
        self.assertEqual(len(attempts), 1)
        return attempts[0]

    def tree_bytes(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def test_write_evidence_fsyncs_file_and_parent_directory(self):
        path = self.root / 'durable.json'
        real_open = os.open
        real_fsync = os.fsync
        real_close = os.close
        with patch.object(patrol.os, 'open', wraps=real_open) as open_directory, \
                patch.object(patrol.os, 'fsync', wraps=real_fsync) as fsync, \
                patch.object(patrol.os, 'close', wraps=real_close) as close_directory:
            patrol.write_evidence(path, {'schema_version': 1})
        self.assertEqual(json.loads(path.read_text()), {'schema_version': 1})
        open_directory.assert_called_once()
        self.assertEqual(Path(open_directory.call_args.args[0]), self.root)
        self.assertEqual(fsync.call_count, 2)
        close_directory.assert_called_once_with(fsync.call_args_list[-1].args[0])

    def test_main_reconcile_only_is_byte_identical_for_every_current_state(self):
        for current in [None, 'old', 'next']:
            with self.subTest(current=current):
                self.setUp()
                lock_dir = self.root / '.gc/witness-patrol-locks'
                lock_dir.mkdir(parents=True)
                key = hashlib.sha256(ACTOR.encode()).hexdigest()
                lock = lock_dir / (key + '.lock')
                lock.write_bytes(b'')
                self.path = lock.with_suffix('.state.json')
                self.path.write_text(self.original)
                before = self.tree_bytes()
                city = RecoveryCity(current)
                env = {'GC_CITY_PATH': str(self.root), 'GC_AGENT': ACTOR,
                       'GC_TEMPLATE': ACTOR, 'GC_ALIAS': ACTOR,
                       'GC_SESSION_ID': 'session-fixture', 'GC_RIG': 'office-work'}
                argv = ['patrol', 'recover', '--completed-current', 'old',
                        '--expected-next', 'next', '--reconcile-only']
                with patch.dict(patrol.os.environ, env), \
                        patch.object(patrol.sys, 'argv', argv), \
                        patch.object(patrol.subprocess, 'run', city.call), \
                        patch.object(patrol.sys, 'stdout', io.StringIO()) as output:
                    if current == 'next':
                        patrol.main()
                        result = json.loads(output.getvalue())
                        self.assertEqual(result['action'], 'reconciled')
                        self.assertTrue(result['mutation_free'])
                    else:
                        with self.assertRaisesRegex(
                                patrol.ReconcileNeeded,
                                'reconcile-only found no current successor'):
                            patrol.main()
                self.assertEqual(self.tree_bytes(), before)
                self.assertEqual(city.claims, 0)
                self.assertEqual(city.mutations, [])

    def test_recovery_claims_recorded_next_without_pour_or_burn(self):
        for current in ['old', None, 'next']:
            with self.subTest(current=current):
                # Separate evidence archive for each independent scenario.
                self.setUp()
                city = RecoveryCity(current)
                result = self.recover(city)
                self.assertTrue(result['recovered'])
                self.assertEqual(city.claims, 0 if current == 'next' else 1)
                self.assertEqual(city.mutations, [])
                self.assertEqual(self.path.read_text(), self.original)
                self.assertEqual(Path(result['original_journal']).read_text(), self.original)
                self.assertEqual(len(list(self.root.glob('*.verified.json'))), 1)

    def test_wrong_ids_or_formula_never_claim(self):
        city = RecoveryCity()
        for completed, expected in [('different', 'next'), ('old', 'different'), ('old', 'old')]:
            with self.assertRaises(patrol.ReconcileNeeded):
                self.recover(city, completed, expected)
        self.state['formula'] = 'mol-refinery-patrol'
        with self.assertRaises(patrol.ReconcileNeeded):
            self.recover(city)
        self.assertEqual(city.claims, 0)

    def test_existing_old_foreign_next_ambiguity_and_read_failure_preserved(self):
        cities = [RecoveryCity() for _ in range(6)]
        cities[0].old_exists = True
        cities[1].rows[0]['assignee'] = 'foreign'
        cities[2].rows.append(row('other'))
        cities[3].fail_read = True
        cities[4].rows[0]['title'] = 'mol-refinery-patrol'
        cities[5].current = 'unrelated'
        for city in cities:
            with self.assertRaises(patrol.ReconcileNeeded):
                self.recover(city)
            self.assertEqual(city.claims, 0)
            self.assertEqual(city.mutations, [])

    def test_untyped_not_found_and_timeout_never_authorize_recovery(self):
        for response in [subprocess.CompletedProcess([], 1, '', ''),
                         subprocess.CompletedProcess([], 1, '{"schema_version":1,"error":"backend unavailable"}', '')]:
            with patch.object(patrol.subprocess, 'run', return_value=response):
                with self.assertRaises(patrol.ReconcileNeeded):
                    patrol.require_retired('old', 'office-work')
        with patch.object(patrol.subprocess, 'run', side_effect=subprocess.TimeoutExpired([], 30)):
            with self.assertRaises(subprocess.TimeoutExpired):
                patrol.require_retired('old', 'office-work')

    def test_wrong_claim_keeps_pending_journal_and_evidence(self):
        city = RecoveryCity()
        city.claim_wrong = True
        with self.assertRaises(patrol.ReconcileNeeded):
            self.recover(city)
        self.assertEqual(self.path.read_text(), self.original)
        self.assertEqual(city.mutations, [])
        with self.assertRaises(patrol.ReconcileNeeded):
            self.recover(city)
        self.assertEqual(city.claims, 1)

    def test_timeout_retry_does_not_repeat_unknown_claim(self):
        city = RecoveryCity()
        city.claim_timeout = True
        with self.assertRaisesRegex(patrol.ReconcileNeeded, 'recovery claim timeout'):
            self.recover(city)
        attempt = self.attempt()
        outcome = patrol.evidence_peer(attempt, 'outcome')
        receipt = json.loads(outcome.read_text())
        self.assertEqual(receipt['status'], 'timeout')
        self.assertEqual(receipt['session_id'], 'session-fixture')
        self.assertIsNone(receipt['exit'])
        self.assertEqual(receipt['command'], ['gc', 'hook', '--claim', '--json'])
        city.claim_timeout = False
        with self.assertRaisesRegex(patrol.ReconcileNeeded, 'requires live reconciliation'):
            self.recover(city)
        self.assertEqual(city.claims, 1)
        with self.assertRaisesRegex(patrol.ReconcileNeeded, 'reconcile-only found no current successor'):
            self.recover(city, reconcile_only=True)
        self.assertEqual(city.claims, 1)
        self.assertEqual(city.mutations, [])
        self.assertEqual(self.path.read_text(), self.original)
        # Explicit token + no current + two identical open-successor reads permits
        # exactly one retry.  A restarted session is recorded as a handoff.
        city.current = None
        result = self.recover(city, token=patrol.retry_token(attempt), session='restarted-session')
        self.assertTrue(result['recovered'])
        self.assertEqual(city.claims, 2)
        retry = patrol.evidence_peer(attempt, 'retry.attempt')
        retry_doc = json.loads(retry.read_text())
        self.assertEqual(retry_doc['prior_session_id'], 'session-fixture')
        self.assertEqual(retry_doc['session_id'], 'restarted-session')

    def test_authoritative_no_work_receipt_allows_one_explicit_retry(self):
        city = RecoveryCity(current=None)
        city.claim_no_work = 1
        with self.assertRaisesRegex(patrol.ReconcileNeeded, 'did not return the recorded successor'):
            self.recover(city)
        attempt = self.attempt()
        receipt = json.loads(patrol.evidence_peer(attempt, 'outcome').read_text())
        self.assertEqual(receipt['status'], 'completed')
        self.assertEqual(receipt['exit'], 1)
        self.assertIn('"reason": "no_work"', receipt['stdout'])
        self.assertEqual(receipt['stderr'], 'no work')
        result = self.recover(city, token=patrol.retry_token(attempt))
        self.assertTrue(result['recovered'])
        self.assertEqual(city.claims, 2)
        with self.assertRaisesRegex(patrol.ReconcileNeeded, 'retry was already attempted'):
            city.current = None
            self.recover(city, token=patrol.retry_token(attempt))
        self.assertEqual(city.claims, 2)

    def test_claim_exception_is_durable_and_wrong_token_never_retries(self):
        city = RecoveryCity(current=None)
        original_call = city.call

        def exception_once(command, **kwargs):
            if command[1:] == ['hook', '--claim', '--json']:
                city.claims += 1
                raise OSError('exec unavailable')
            return original_call(command, **kwargs)

        city.call = exception_once
        with self.assertRaisesRegex(patrol.ReconcileNeeded, 'recovery claim exception'):
            self.recover(city)
        attempt = self.attempt()
        receipt = json.loads(patrol.evidence_peer(attempt, 'outcome').read_text())
        self.assertEqual(receipt['status'], 'exception')
        self.assertIn('exec unavailable', receipt['stderr'])
        with self.assertRaisesRegex(patrol.ReconcileNeeded, 'requires live reconciliation'):
            self.recover(city, token='wrong-token')
        self.assertEqual(city.claims, 1)

    def test_retry_token_cannot_override_non_open_or_unstable_successor(self):
        for mutate in ('in_progress', 'unstable'):
            with self.subTest(mutate=mutate):
                self.setUp()
                city = RecoveryCity(current=None)
                city.claim_timeout = True
                with self.assertRaises(patrol.ReconcileNeeded):
                    self.recover(city)
                attempt = self.attempt()
                city.claim_timeout = False
                if mutate == 'in_progress':
                    city.rows[0]['status'] = 'in_progress'
                else:
                    original_call = city.call
                    inventory_reads = {'count': 0}

                    def changing_call(command, **kwargs):
                        if command[1:5] == ['bd', '--rig', 'office-work', 'list']:
                            inventory_reads['count'] += 1
                            if inventory_reads['count'] >= 2:
                                city.rows[0]['title'] = 'changed'
                        return original_call(command, **kwargs)

                    city.call = changing_call
                with self.assertRaises(patrol.ReconcileNeeded):
                    self.recover(city, token=patrol.retry_token(attempt))
                self.assertEqual(city.claims, 1)

    def test_legacy_attempt_can_only_be_explicitly_handed_off_after_live_checks(self):
        city = RecoveryCity(current=None)
        city.claim_timeout = True
        with self.assertRaises(patrol.ReconcileNeeded):
            self.recover(city)
        attempt = self.attempt()
        modern = json.loads(attempt.read_text())
        legacy = {key: modern[key] for key in ('original', 'formula', 'store_rig',
                                               'old_not_found', 'inventory',
                                               'claim_before', 'claim_attempted')}
        attempt.write_text(json.dumps(legacy))
        city.claim_timeout = False
        with self.assertRaisesRegex(patrol.ReconcileNeeded, 'requires live reconciliation'):
            self.recover(city)
        result = self.recover(city, token=patrol.retry_token(attempt), session='new-session')
        self.assertTrue(result['recovered'])
        retry = json.loads(patrol.evidence_peer(attempt, 'retry.attempt').read_text())
        self.assertTrue(retry['legacy_handoff'])
        self.assertIsNone(retry['prior_session_id'])

    def test_recovery_receipts_cover_refinery_formula_without_rewriting_journal(self):
        city = RecoveryCity(current=None)
        city.rows[0]['title'] = 'mol-refinery-patrol'
        result = self.recover(city, formula='mol-refinery-patrol')
        self.assertTrue(result['recovered'])
        self.assertEqual(self.path.read_text(), self.original)
        attempt = self.attempt()
        self.assertEqual(json.loads(attempt.read_text())['formula'], 'mol-refinery-patrol')
        self.assertTrue(patrol.evidence_peer(attempt, 'outcome').exists())

    def test_main_archives_then_finishes_and_successful_retry_is_read_only(self):
        lock_dir = self.root / '.gc/witness-patrol-locks'
        lock_dir.mkdir(parents=True)
        key = hashlib.sha256(ACTOR.encode()).hexdigest()
        self.path = lock_dir / (key + '.state.json')
        self.path.write_text(self.original)
        city = RecoveryCity()
        env = {'GC_CITY_PATH': str(self.root), 'GC_AGENT': ACTOR,
               'GC_TEMPLATE': ACTOR, 'GC_ALIAS': ACTOR,
               'GC_SESSION_ID': 'session-fixture', 'GC_RIG': 'office-work'}
        argv = ['patrol', 'recover', '--completed-current', 'old', '--expected-next', 'next']
        with patch.dict(patrol.os.environ, env), patch.object(patrol.sys, 'argv', argv), \
                patch.object(patrol.subprocess, 'run', city.call), \
                patch.object(patrol.sys, 'stdout', io.StringIO()) as output:
            patrol.main()
            result = json.loads(output.getvalue())
        state = json.loads(self.path.read_text())
        self.assertFalse(state['pending'])
        self.assertEqual(state['formula'], 'mol-witness-patrol')
        self.assertEqual(state['result'], result)
        self.assertEqual(Path(result['original_journal']).read_text(), self.original)
        self.assertEqual(city.mutations, [])
        with patch.dict(patrol.os.environ, env), patch.object(patrol.sys, 'argv', argv), \
                patch.object(patrol.subprocess, 'run') as command, \
                patch.object(patrol.sys, 'stdout', io.StringIO()) as output:
            patrol.main()
            self.assertTrue(json.loads(output.getvalue())['replayed'])
            command.assert_not_called()

    def test_pending_journal_with_old_success_cannot_bypass_recovery_checks(self):
        lock_dir = self.root / '.gc/witness-patrol-locks'
        lock_dir.mkdir(parents=True)
        key = hashlib.sha256(ACTOR.encode()).hexdigest()
        state = dict(self.state, formula='mol-witness-patrol',
                     result={'action': 'advanced', 'current': 'old', 'next': 'next'})
        (lock_dir / (key + '.state.json')).write_text(json.dumps(state))
        city = RecoveryCity()
        city.old_exists = True
        with patch.dict(patrol.os.environ, {'GC_CITY_PATH': str(self.root), 'GC_AGENT': ACTOR,
                                            'GC_TEMPLATE': ACTOR, 'GC_SESSION_ID': 'session-fixture', 'GC_RIG': 'office-work'}), \
                patch.object(patrol.sys, 'argv', ['patrol', 'recover', '--completed-current',
                                                 'old', '--expected-next', 'next']), \
                patch.object(patrol.subprocess, 'run', city.call):
            with self.assertRaisesRegex(patrol.ReconcileNeeded, 'old patrol still exists'):
                patrol.main()
        self.assertEqual(city.claims, 0)

    def test_claim_timeout_budget_only_and_startup_timeout_never_pours(self):
        claim = subprocess.CompletedProcess([], 0, '{"ok":true,"action":"work","bead_id":"next"}', '')
        with patch.object(patrol.subprocess, 'run', return_value=claim) as run:
            patrol.gc('hook', '--claim', '--json', protocol=True)
            self.assertEqual(run.call_args.kwargs['timeout'], 165)
            patrol.gc('bd', 'list', '--json')
            self.assertEqual(run.call_args.kwargs['timeout'], 30)
        with patch.object(patrol.subprocess, 'run', side_effect=subprocess.TimeoutExpired([], 165)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                patrol.run('startup', ACTOR, {ACTOR}, 'gastown.', lambda state: None, store_rig='office-work')
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0], ['gc', 'hook', '--claim', '--json'])

    def test_structured_not_found_accepts_new_empty_or_arbitrary_stderr(self):
        for stderr in ['Issue old not found', '', 'presentation changed']:
            response = subprocess.CompletedProcess([], 1, json.dumps({
                'schema_version': 1, 'error': 'no issues found matching the provided IDs'}), stderr)
            with self.subTest(stderr=stderr), patch.object(patrol.subprocess, 'run', return_value=response) as command:
                evidence = patrol.require_retired('old', 'office-work')
                self.assertEqual(evidence['stderr'], stderr)
                self.assertEqual(command.call_args.args[0],
                                 ['gc', 'bd', '--rig', 'office-work', '--readonly', 'show', 'old', '--json'])

    def test_invalid_absence_and_incomplete_inventory_preserve_journal_without_claim(self):
        valid = {'schema_version': 1, 'error': 'no issues found matching the provided IDs'}
        responses = [(1, '{'), (1, '[]'), (1, json.dumps(dict(valid, schema_version=True))),
                     (1, json.dumps(dict(valid, schema_version=2))),
                     (1, json.dumps(dict(valid, error='backend unavailable'))),
                     (0, json.dumps(valid)), (2, json.dumps(valid)),
                     (0, json.dumps([row('old')]))]
        for code, stdout in responses:
            city = RecoveryCity()
            city.absence_response = subprocess.CompletedProcess([], code, stdout, 'Issue old not found')
            with self.subTest(code=code, stdout=stdout), self.assertRaises(patrol.ReconcileNeeded):
                self.recover(city)
            self.assertEqual(city.claims, 0)
            self.assertEqual(city.mutations, [])
            self.assertEqual(self.path.read_text(), self.original)
            self.assertEqual(list(self.root.glob('*.original')), [])
        for meta in [{'count': 2, 'skip_labels': True},
                     {'count': 1, 'skip_labels': True, 'truncated': True},
                     {'count': 1, 'skip_labels': True, 'next_cursor': 'more'}]:
            city = RecoveryCity()
            city.inventory_override = {'schema_version': 1, 'issues': [row('next')], 'meta': meta}
            with self.subTest(meta=meta), self.assertRaises(patrol.ReconcileNeeded):
                self.recover(city)
            self.assertEqual(city.claims, 0)
            self.assertEqual(city.mutations, [])
            self.assertEqual(self.path.read_text(), self.original)

    def test_wrong_store_cannot_authorize_even_with_valid_absence_fixture(self):
        city = RecoveryCity()
        with patch.object(patrol.subprocess, 'run', city.call):
            for rig in [None, 'wrong-store']:
                with self.subTest(rig=rig), self.assertRaises(patrol.ReconcileNeeded):
                    patrol.recover(self.state, self.path, {ACTOR}, 'mol-witness-patrol',
                                   'old', 'next', rig)
        self.assertEqual(city.claims, 0)
        self.assertEqual(city.commands, [])
        self.assertEqual(self.path.read_text(), self.original)

    def test_recovery_rechecks_absence_and_inventory_after_claim(self):
        for attribute in ['old_reappears', 'post_claim_ambiguity']:
            self.setUp()
            city = RecoveryCity()
            setattr(city, attribute, True)
            with self.subTest(attribute=attribute), self.assertRaises(patrol.ReconcileNeeded):
                self.recover(city)
            self.assertEqual(city.claims, 1)
            self.assertEqual(city.absence_checks, 2)
            self.assertEqual(city.mutations, [])
            self.assertEqual(self.path.read_text(), self.original)
            self.assertEqual(list(self.root.glob('*.verified.json')), [])

    def test_store_bound_pending_journal_rejects_changed_scope(self):
        self.state['store_rig'] = 'another-rig'
        city = RecoveryCity()
        with self.assertRaisesRegex(patrol.ReconcileNeeded, 'journal store rig'):
            self.recover(city)
        self.assertEqual(city.claims, 0)
        self.assertEqual(city.commands, [])
