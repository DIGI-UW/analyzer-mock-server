#!/usr/bin/env python3
"""Unit tests for the LIS-initiated order handling added to MLLPProtocolHandler.

When the mock receives an ORM^O01 over MLLP, after ACK'ing it the mock pushes
a matching ORU^R01 back to the configured destination (the bridge's MLLP
listener). The result echoes the inbound placer + filler order numbers so the
OpenELIS inbound import correlates to the originating accession (OBR-3).
"""

import os
import unittest
from unittest.mock import MagicMock, patch

from protocols.mllp_listener import MLLPProtocolHandler


MINDRAY_TEMPLATE = {
    'analyzer': {'name': 'Mindray BC-5380', 'model': 'BC-5380'},
    'protocol': {'type': 'HL7', 'version': '2.3.1'},
    'identification': {
        'hl7_sending_app': 'MINDRAY',
        'hl7_sending_facility': 'BC-5380',
    },
    'fields': [
        {'code': 'WBC', 'type': 'NUMERIC', 'seedValue': 7.5, 'unit': '10*3/uL'},
        {'code': 'HGB', 'type': 'NUMERIC', 'seedValue': 14.2, 'unit': 'g/dL'},
    ],
    'testPatient': {'id': 'PAT001', 'name': 'TEST^PATIENT', 'dob': '19850412', 'sex': 'F'},
    'testSample': {'id': '53', 'type': 'CBC^Complete Blood Count'},
}


ORDER_ORM_O01 = (
    "MSH|^~\\&|OE2|LAB|MINDRAY|BC-5380|20260519121500||ORM^O01|CTRL-001|P|2.3.1\r"
    "PID|1||PAT-99^^^HOSPITAL||DOE^JOHN||19800101|M\r"
    "ORC|NW|PLACER-ACC-12345|FILLER-ACC-12345||SC|||||||||||||||\r"
    "OBR|1|PLACER-ACC-12345|FILLER-ACC-12345|^^^CBC^Complete Blood Count|||20260519121500\r"
)


RESULT_ORU_R01 = (
    "MSH|^~\\&|MINDRAY|BC-5380|OE2|LAB|...||ORU^R01|...\r"
    "PID|1||...\r"
    "ORC|...\r"
    "OBR|...\r"
    "OBX|1|NM|^^^WBC^WHITE BLOOD CELL||7.5|10*3/uL...\r"
)


def _make_handler(template=MINDRAY_TEMPLATE):
    conn = MagicMock()
    # getsockname() is called for the listening port log line
    conn.getsockname = MagicMock(return_value=('0.0.0.0', 5380))
    handler = MLLPProtocolHandler(
        conn=conn,
        addr=('127.0.0.1', 12345),
        template=template,
        template_name='mindray_bc5380',
        response_delay_ms=0,
    )
    return handler, conn


class TestExtractOrderCorrelation(unittest.TestCase):

    def test_extracts_placer_and_filler_from_obr(self):
        h, _ = _make_handler()
        placer, filler = h._extract_order_correlation(ORDER_ORM_O01)
        self.assertEqual(placer, 'PLACER-ACC-12345')
        self.assertEqual(filler, 'FILLER-ACC-12345')

    def test_falls_back_to_orc_when_obr_missing(self):
        h, _ = _make_handler()
        msg = (
            "MSH|^~\\&|OE2|LAB|MINDRAY|BC-5380|20260519121500||ORM^O01|CTRL-002|P|2.3.1\r"
            "ORC|NW|ORC-PLACER|ORC-FILLER\r"
        )
        placer, filler = h._extract_order_correlation(msg)
        self.assertEqual(placer, 'ORC-PLACER')
        self.assertEqual(filler, 'ORC-FILLER')

    def test_returns_none_when_neither_present(self):
        h, _ = _make_handler()
        msg = "MSH|^~\\&|OE2|LAB|MINDRAY|BC-5380|...||ORM^O01|CTRL-003|P|2.3.1\rPID|1||PAT-1\r"
        placer, filler = h._extract_order_correlation(msg)
        self.assertIsNone(placer)
        self.assertIsNone(filler)


class TestPushOrderResult(unittest.TestCase):

    def test_pushes_oru_r01_echoing_placer_and_filler(self):
        h, _ = _make_handler()
        with patch('push.push_hl7_mllp', return_value=(True, None)) as mock_push:
            with patch.dict(os.environ, {
                'ORDER_RESULT_PUSH_HOST': 'bridge-test',
                'ORDER_RESULT_PUSH_PORT': '2575',
            }):
                h._push_order_result(ORDER_ORM_O01)

        self.assertEqual(mock_push.call_count, 1)
        args, kwargs = mock_push.call_args
        # Positional: (host, port, hl7_message)
        host, port, message = args[0], args[1], args[2]
        self.assertEqual(host, 'bridge-test')
        self.assertEqual(port, 2575)
        self.assertIn('ORU^R01', message)
        # The originating filler accession is echoed in OBR-3 and ORC-3
        self.assertIn('FILLER-ACC-12345', message)

    def test_skip_push_when_host_empty(self):
        h, _ = _make_handler()
        with patch('push.push_hl7_mllp', return_value=(True, None)) as mock_push:
            with patch.dict(os.environ, {'ORDER_RESULT_PUSH_HOST': ''}):
                h._push_order_result(ORDER_ORM_O01)
        mock_push.assert_not_called()

    def test_skip_push_when_port_invalid(self):
        h, _ = _make_handler()
        with patch('push.push_hl7_mllp', return_value=(True, None)) as mock_push:
            with patch.dict(os.environ, {
                'ORDER_RESULT_PUSH_HOST': 'bridge-test',
                'ORDER_RESULT_PUSH_PORT': 'not-a-number',
            }):
                h._push_order_result(ORDER_ORM_O01)
        mock_push.assert_not_called()

    def test_skip_push_when_no_correlation(self):
        h, _ = _make_handler()
        msg_without_orders = "MSH|^~\\&|OE2|LAB|...|||ORM^O01|X|P|2.3.1\r"
        with patch('push.push_hl7_mllp', return_value=(True, None)) as mock_push:
            with patch.dict(os.environ, {
                'ORDER_RESULT_PUSH_HOST': 'bridge-test',
                'ORDER_RESULT_PUSH_PORT': '2575',
            }):
                h._push_order_result(msg_without_orders)
        mock_push.assert_not_called()

    def test_push_failure_does_not_raise(self):
        h, _ = _make_handler()
        with patch('push.push_hl7_mllp', side_effect=Exception('connection refused')):
            with patch.dict(os.environ, {
                'ORDER_RESULT_PUSH_HOST': 'bridge-test',
                'ORDER_RESULT_PUSH_PORT': '2575',
            }):
                # Should not raise — receipt path stays successful
                h._push_order_result(ORDER_ORM_O01)


class TestHandleDispatchesOnOrm(unittest.TestCase):
    """The handle() loop should detect ORM^O01 (MSH-9) and call _push_order_result.
    Non-ORM messages (ORU^R01, QRY, etc.) should not trigger the push.
    """

    def test_orm_o01_triggers_push(self):
        h, _ = _make_handler()
        h._push_order_result = MagicMock()
        # Stub the receive loop: deliver one ORM frame then disconnect
        h._receive_mllp_frame = MagicMock(side_effect=[ORDER_ORM_O01, None])
        h._send_mllp_frame = MagicMock()

        h.handle()

        h._push_order_result.assert_called_once_with(ORDER_ORM_O01)

    def test_oru_r01_does_not_trigger_push(self):
        h, _ = _make_handler()
        h._push_order_result = MagicMock()
        h._receive_mllp_frame = MagicMock(side_effect=[RESULT_ORU_R01, None])
        h._send_mllp_frame = MagicMock()

        h.handle()

        h._push_order_result.assert_not_called()

    def test_no_template_skips_push_even_for_orm(self):
        h, _ = _make_handler(template=None)
        h._push_order_result = MagicMock()
        h._receive_mllp_frame = MagicMock(side_effect=[ORDER_ORM_O01, None])
        h._send_mllp_frame = MagicMock()

        h.handle()

        h._push_order_result.assert_not_called()


if __name__ == '__main__':
    unittest.main()
