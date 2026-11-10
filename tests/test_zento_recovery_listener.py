import unittest
from unittest.mock import Mock, patch

import zento_recovery_listener as mod


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = mod.SessionRegistry()

    def test_new_session_replaces_old(self):
        first, second = Mock(), Mock()
        g1 = self.registry.replace('gateway', first)
        g2 = self.registry.replace('gateway', second)
        self.assertFalse(self.registry.active('gateway', g1))
        self.assertTrue(self.registry.active('gateway', g2))
        first.shutdown.assert_called_once()
        first.close.assert_called_once()

    def test_old_worker_cannot_remove_replacement(self):
        g1 = self.registry.replace('gateway', Mock())
        g2 = self.registry.replace('gateway', Mock())
        self.registry.release('gateway', g1)
        self.assertTrue(self.registry.active('gateway', g2))

    def test_fresh_status_and_release(self):
        g = self.registry.replace('gateway', Mock())
        self.assertTrue(self.registry.record_status('gateway', g, 'open'))
        self.assertTrue(self.registry.snapshot('gateway')['online'])
        self.assertEqual(self.registry.snapshot('gateway')['barrier_state'], 'open')
        self.registry.release('gateway', g)
        self.assertFalse(self.registry.snapshot('gateway')['online'])
        self.assertEqual(self.registry.snapshot('gateway')['barrier_state'], 'unknown')

    def test_stale_status(self):
        g = self.registry.replace('gateway', Mock())
        self.registry.record_status('gateway', g, 'open')
        with patch.object(mod.time, 'monotonic', return_value=10**12):
            self.assertFalse(self.registry.snapshot('gateway')['online'])
            self.assertEqual(self.registry.snapshot('gateway')['barrier_state'], 'unknown')

    def test_new_connection_invalidates_old_status(self):
        g = self.registry.replace('gateway', Mock())
        self.registry.record_status('gateway', g, 'open')
        self.registry.replace('gateway', Mock())
        self.assertFalse(self.registry.snapshot('gateway')['online'])

    def test_old_worker_cannot_publish_status(self):
        g1 = self.registry.replace('gateway', Mock())
        self.registry.replace('gateway', Mock())
        self.assertFalse(self.registry.record_status('gateway', g1, 'open'))


class CertificateTests(unittest.TestCase):
    def test_reject_wrong_cn(self):
        cert = {'subject': ((('commonName', 'wrong-gateway'),),)}
        self.assertIsNone(mod.authorized_identity(cert, b'certificate'))

    def test_accept_expected_cn(self):
        cert = {'subject': ((('commonName', mod.EXPECTED_CN),),)}
        with patch.object(mod, 'EXPECTED_SHA256', ''):
            self.assertEqual(mod.authorized_identity(cert, b'certificate'), mod.EXPECTED_CN)

    def test_reject_wrong_fingerprint(self):
        cert = {'subject': ((('commonName', mod.EXPECTED_CN),),)}
        with patch.object(mod, 'EXPECTED_SHA256', '0' * 64):
            self.assertIsNone(mod.authorized_identity(cert, b'certificate'))


if __name__ == '__main__':
    unittest.main()
