import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from zento_command_admission import (
    AuthorizationEvidence,
    CommandRequest,
    DecisionCode,
    GatewaySnapshot,
    admit_command,
)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)
        self.request = CommandRequest(
            command_id="cmd-001",
            idempotency_key="idem-001",
            gateway_serial="pilot-01",
            rs485_address=0,
            command="open",
            generation=7,
            expires_at=self.now + timedelta(seconds=15),
        )
        self.authorization = AuthorizationEvidence(
            trusted=True,
            subject_id="user-123",
            gateway_serial="pilot-01",
            rs485_address=0,
            permitted_command="open",
            valid_until=self.now + timedelta(minutes=1),
        )
        self.snapshot = GatewaySnapshot(
            gateway_serial="pilot-01",
            rs485_address=0,
            connected=True,
            online=True,
            barrier_state="closed",
            last_seen_at=self.now - timedelta(seconds=2),
            generation=7,
        )

    def evaluate(self, request=None, authorization=None,
                 snapshot=None, **kwargs):
        return admit_command(
            request or self.request,
            authorization or self.authorization,
            snapshot or self.snapshot,
            now=self.now,
            **kwargs,
        )

    def test_valid_request_admitted_but_never_executable(self):
        result = self.evaluate()
        self.assertEqual(result.code, DecisionCode.ADMITTED)
        self.assertTrue(result.admitted)
        self.assertFalse(result.execution_enabled)

    def test_unsupported_command(self):
        result = self.evaluate(
            request=replace(self.request, command="firmware")
        )
        self.assertEqual(result.code, DecisionCode.INVALID_REQUEST)

    def test_invalid_rs485_address(self):
        result = self.evaluate(
            request=replace(self.request, rs485_address=256)
        )
        self.assertEqual(result.code, DecisionCode.INVALID_REQUEST)

    def test_missing_idempotency_key(self):
        result = self.evaluate(
            request=replace(self.request, idempotency_key="")
        )
        self.assertEqual(result.code, DecisionCode.INVALID_REQUEST)

    def test_untrusted_authorization(self):
        result = self.evaluate(
            authorization=replace(self.authorization, trusted=False)
        )
        self.assertEqual(result.code, DecisionCode.UNAUTHORIZED)

    def test_wrong_authorized_command(self):
        result = self.evaluate(
            authorization=replace(
                self.authorization, permitted_command="close"
            )
        )
        self.assertEqual(result.code, DecisionCode.UNAUTHORIZED)

    def test_expired_authorization(self):
        result = self.evaluate(
            authorization=replace(
                self.authorization,
                valid_until=self.now - timedelta(seconds=1),
            )
        )
        self.assertEqual(result.code, DecisionCode.UNAUTHORIZED)

    def test_expired_request(self):
        result = self.evaluate(
            request=replace(
                self.request,
                expires_at=self.now - timedelta(seconds=1),
            )
        )
        self.assertEqual(result.code, DecisionCode.INVALID_EXPIRY)

    def test_excessive_ttl(self):
        result = self.evaluate(
            request=replace(
                self.request,
                expires_at=self.now + timedelta(minutes=5),
            )
        )
        self.assertEqual(result.code, DecisionCode.INVALID_EXPIRY)

    def test_target_mismatch(self):
        result = self.evaluate(
            snapshot=replace(
                self.snapshot, gateway_serial="another-gateway"
            )
        )
        self.assertEqual(result.code, DecisionCode.TARGET_MISMATCH)

    def test_session_mismatch(self):
        result = self.evaluate(
            snapshot=replace(self.snapshot, generation=8)
        )
        self.assertEqual(result.code, DecisionCode.SESSION_MISMATCH)

    def test_disconnected_gateway(self):
        result = self.evaluate(
            snapshot=replace(self.snapshot, connected=False)
        )
        self.assertEqual(result.code, DecisionCode.OFFLINE)

    def test_offline_gateway(self):
        result = self.evaluate(
            snapshot=replace(self.snapshot, online=False)
        )
        self.assertEqual(result.code, DecisionCode.OFFLINE)

    def test_stale_status(self):
        result = self.evaluate(
            snapshot=replace(
                self.snapshot,
                last_seen_at=self.now - timedelta(seconds=25),
            )
        )
        self.assertEqual(result.code, DecisionCode.STALE_STATUS)

    def test_unknown_state(self):
        result = self.evaluate(
            snapshot=replace(self.snapshot, barrier_state="unknown")
        )
        self.assertEqual(result.code, DecisionCode.UNKNOWN_STATE)

    def test_moving_barrier_rejected(self):
        for state in ("opening", "closing", "stopped"):
            with self.subTest(state=state):
                result = self.evaluate(
                    snapshot=replace(
                        self.snapshot, barrier_state=state
                    )
                )
                self.assertEqual(
                    result.code, DecisionCode.UNSAFE_STATE
                )

    def test_conflicting_idempotency(self):
        result = self.evaluate(
            existing_fingerprint=("different-request",)
        )
        self.assertEqual(
            result.code, DecisionCode.IDEMPOTENCY_CONFLICT
        )

    def test_matching_idempotency_not_execution_permission(self):
        fingerprint = (
            "user-123",
            "pilot-01",
            0,
            "open",
            7,
            self.request.expires_at.isoformat(),
        )
        result = self.evaluate(existing_fingerprint=fingerprint)
        self.assertTrue(result.admitted)
        self.assertFalse(result.execution_enabled)


if __name__ == "__main__":
    unittest.main()
