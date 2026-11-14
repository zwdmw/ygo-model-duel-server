import subprocess
import unittest
from unittest import mock

from tools.watch_outbound_room import AGENT_SCRIPT, ROOM_SCRIPT, decision, remote


class OutboundRoomWatchTest(unittest.TestCase):
    def test_connected_model_is_not_restarted(self):
        self.assertEqual(decision({'connected': True}, {'active': True}, 300, 300, 0), 'connected')

    def test_reconnecting_worker_is_left_running(self):
        self.assertEqual(decision({'connected': False}, {'active': True}, 300, 300, 0), 'worker_reconnecting')

    def test_recovery_requires_confirmed_disconnect_and_cooldown(self):
        self.assertEqual(decision({}, {'state': 'failed'}, 59, 300, 0), 'waiting_before_recovery')
        self.assertEqual(decision({}, {'state': 'failed'}, 120, 119, 0), 'waiting_before_recovery')
        self.assertEqual(decision({}, {'state': 'failed'}, 60, 120, 0), 'recover')

    def test_manual_stop_is_not_undone(self):
        self.assertEqual(decision({}, {'state': 'stopped'}, 300, 300, 0), 'owner_stopped')
        self.assertEqual(decision({}, {'error': 'stopped_by_owner'}, 300, 300, 0), 'owner_stopped')

    def test_closed_or_replaced_room_stops_watch(self):
        self.assertEqual(decision({'terminal': True}, {}, 300, 300, 0), 'room_finished')

    def test_competing_model_task_prevents_duplicate_join(self):
        self.assertEqual(decision({}, {'competing': True}, 300, 300, 0), 'another_task_present')

    def test_recovery_attempts_are_bounded(self):
        self.assertEqual(decision({}, {'state': 'failed'}, 300, 300, 12), 'recovery_limit')

    def test_remote_programs_are_valid_python(self):
        for template in (ROOM_SCRIPT, AGENT_SCRIPT):
            compile(template.replace('REQUEST', repr({'room_id': 'r-example'}), 1), '<remote>', 'exec')

    @mock.patch('tools.watch_outbound_room.subprocess.run')
    def test_credentials_are_sent_only_through_stdin(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout='{}', stderr='')
        remote('agent', AGENT_SCRIPT, {'password': 'private-test-code'})
        self.assertNotIn('private-test-code', repr(run.call_args.args))
        self.assertIn('private-test-code', run.call_args.kwargs['input'])
        self.assertTrue(run.call_args.kwargs['capture_output'])

    @mock.patch('tools.watch_outbound_room.subprocess.run')
    def test_remote_errors_do_not_leak_response_or_credentials(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, stdout='private-test-code', stderr='private-test-code')
        with self.assertRaises(RuntimeError) as caught:
            remote('agent', AGENT_SCRIPT, {'password': 'private-test-code'})
        self.assertNotIn('private-test-code', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
