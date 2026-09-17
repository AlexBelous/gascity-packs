import json
import hashlib
import io
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
            self.current = 'foreign' if self.claim_wrong else 'next'
            if self.post_claim_ambiguity:
                self.rows.append(row('unexpected'))
        if args == ['hook', 'current', '--id-only'] and self.current is None:
            return subprocess.CompletedProcess(command, 1, '',
                'gc hook current: session session-fixture has no current claim '
                '(nothing claimed through gc hook --claim)')
        return super().call(command, **kwargs)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='patrol-recovery-'))
        self.path = self.root / 'pending.state.json'
        self.state = {'pending': True, 'phase': 'burn', 'current': 'old', 'next': 'next'}
        self.original = json.dumps(self.state)
        self.path.write_text(self.original)

    def recover(self, city, completed='old', expected='next'):
        with patch.object(patrol.subprocess, 'run', city.call), \
                patch.dict(patrol.os.environ, GC_SESSION_ID='session-fixture'):
            return patrol.recover(self.state, self.path, {ACTOR}, 'mol-witness-patrol',
                                  completed, expected, "office-work")

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
        with self.assertRaises(subprocess.TimeoutExpired):
            self.recover(city)
        city.claim_timeout = False
        with self.assertRaisesRegex(patrol.ReconcileNeeded, 'prior recovery claim outcome'):
            self.recover(city)
        self.assertEqual(city.claims, 1)
        self.assertEqual(city.mutations, [])
        self.assertEqual(self.path.read_text(), self.original)
        # A delayed successful claim can be reconciled by readback alone.
        city.current = 'next'
        self.assertTrue(self.recover(city)['recovered'])
        self.assertEqual(city.claims, 1)

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
