#!/usr/bin/env python3
"""Unit tests for the order-in/result-out behavior added to ASTMProtocolHandler.

When the bridge sends an LIS-initiated ASTM order, the mock parses the
sample_id + test_code, then pushes a matching result via a fresh TCP
connection to the bridge's ASTM listener (mirror of the HL7 path that opens
a fresh MLLP connection to bridge:2575). The originating sample_id is echoed
verbatim in the response O-record (ASTM field 3) so OpenELIS's existing
accession-keyed inbound result import correlates back to the originating
sample.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

import server


GENEXPERT_TEMPLATE = {
    'analyzer': {'name': 'Cepheid GeneXpert', 'model': 'GeneXpert'},
    'protocol': {'type': 'ASTM', 'version': 'E-1394-97'},
    'astm_config': {'proactive_enq': False},
    'fields': [
        {
            'code': 'MTB-RIF',
            'type': 'QUALITATIVE',
            'seedQualitative': 'NOT DETECTED',
        },
        {
            'code': 'HIV-VL',
            'type': 'NUMERIC',
            'seedValue': 1250,
            'unit': 'copies/mL',
        },
    ],
    'testPatient': {'id': 'PAT003', 'name': 'TEST^PATIENT'},
}


def _make_handler(template=None):
    conn = MagicMock()
    conn.send = MagicMock()
    handler = server.ASTMProtocolHandler(
        conn=conn,
        addr=('127.0.0.1', 12345),
        fields_config={},
        response_delay_ms=0,
        astm_template=template if template is not None else GENEXPERT_TEMPLATE,
    )
    return handler, conn


class TestProcessOrderRecord(unittest.TestCase):

    def test_parses_sample_id_and_test_code(self):
        handler, _ = _make_handler()
        handler._process_order('O|1|ACC-12345||^^^MTB-RIF|R|||||||||||N|||')
        self.assertEqual(handler.received_orders, [
            {'sample_id': 'ACC-12345', 'test_code': 'MTB-RIF'},
        ])

    def test_skips_record_with_missing_test_code(self):
        handler, _ = _make_handler()
        handler._process_order('O|1|ACC-12345||^^^|R')
        self.assertEqual(handler.received_orders, [])

    def test_skips_record_with_missing_sample_id(self):
        handler, _ = _make_handler()
        handler._process_order('O|1|||^^^MTB-RIF|R')
        self.assertEqual(handler.received_orders, [])

    def test_accumulates_multiple_orders(self):
        handler, _ = _make_handler()
        handler._process_order('O|1|ACC-1||^^^MTB-RIF|R')
        handler._process_order('O|2|ACC-2||^^^HIV-VL|R')
        self.assertEqual(len(handler.received_orders), 2)


class TestSendOrderResponse(unittest.TestCase):

    def test_pushes_via_fresh_connection_to_bridge_listener(self):
        """Mock pushes ASTM result via push_astm_tcp to the configured destination
        rather than reusing self.conn (the bridge closes its outbound socket
        after EOT, so same-connection push would be dropped)."""
        handler, _ = _make_handler()
        with patch('push.push_astm_tcp', return_value=True) as mock_push:
            with patch.dict(os.environ, {
                'ORDER_RESULT_PUSH_HOST': 'bridge-test',
                'ORDER_RESULT_PUSH_ASTM_PORT': '12001',
            }):
                handler.send_order_response([
                    {'sample_id': 'ACC-12345', 'test_code': 'MTB-RIF'},
                ])
        self.assertEqual(mock_push.call_count, 1)
        args, kwargs = mock_push.call_args
        host, port, message = args[0], args[1], args[2]
        self.assertEqual(host, 'bridge-test')
        self.assertEqual(port, 12001)
        # The originating accession is echoed verbatim in the O-record
        self.assertIn('ACC-12345', message)
        # Header, patient, order, result, terminator records all present
        self.assertIn('H|\\^&', message)
        self.assertIn('P|1|', message)
        self.assertIn('O|1|ACC-12345', message)
        self.assertIn('R|1|^^^MTB-RIF|NOT DETECTED', message)
        self.assertIn('L|1|N', message)

    def test_numeric_field_emits_value_with_unit(self):
        handler, _ = _make_handler()
        with patch('push.push_astm_tcp', return_value=True) as mock_push:
            with patch.dict(os.environ, {
                'ORDER_RESULT_PUSH_HOST': 'bridge-test',
                'ORDER_RESULT_PUSH_ASTM_PORT': '12001',
            }):
                handler.send_order_response([
                    {'sample_id': 'ACC-99', 'test_code': 'HIV-VL'},
                ])
        message = mock_push.call_args[0][2]
        self.assertIn('R|1|^^^HIV-VL|1250|copies/mL', message)

    def test_multiple_tests_one_sample_share_pid_block(self):
        handler, _ = _make_handler()
        with patch('push.push_astm_tcp', return_value=True) as mock_push:
            with patch.dict(os.environ, {
                'ORDER_RESULT_PUSH_HOST': 'bridge-test',
                'ORDER_RESULT_PUSH_ASTM_PORT': '12001',
            }):
                handler.send_order_response([
                    {'sample_id': 'ACC-7', 'test_code': 'MTB-RIF'},
                    {'sample_id': 'ACC-7', 'test_code': 'HIV-VL'},
                ])
        message = mock_push.call_args[0][2]
        self.assertEqual(message.count('P|1|'), 1, "single P/O block for one sample")
        self.assertEqual(message.count('O|1|'), 1)
        self.assertIn('R|1|^^^MTB-RIF', message)
        self.assertIn('R|2|^^^HIV-VL', message)

    def test_unknown_test_code_is_skipped_not_fatal(self):
        handler, _ = _make_handler()
        with patch('push.push_astm_tcp', return_value=True) as mock_push:
            with patch.dict(os.environ, {
                'ORDER_RESULT_PUSH_HOST': 'bridge-test',
                'ORDER_RESULT_PUSH_ASTM_PORT': '12001',
            }):
                handler.send_order_response([
                    {'sample_id': 'ACC-1', 'test_code': 'UNKNOWN'},
                    {'sample_id': 'ACC-1', 'test_code': 'MTB-RIF'},
                ])
        message = mock_push.call_args[0][2]
        self.assertNotIn('^^^UNKNOWN', message)
        self.assertIn('^^^MTB-RIF', message)

    def test_no_template_aborts_without_pushing(self):
        handler, _ = _make_handler(template=False)  # explicit no template
        # Force template to None (False is falsy but not exactly None — explicit set)
        handler.astm_template = None
        with patch('push.push_astm_tcp') as mock_push:
            handler.send_order_response([
                {'sample_id': 'ACC-1', 'test_code': 'MTB-RIF'},
            ])
        mock_push.assert_not_called()

    def test_skip_push_when_host_empty(self):
        handler, _ = _make_handler()
        with patch('push.push_astm_tcp') as mock_push:
            with patch.dict(os.environ, {'ORDER_RESULT_PUSH_HOST': ''}):
                handler.send_order_response([
                    {'sample_id': 'ACC-1', 'test_code': 'MTB-RIF'},
                ])
        mock_push.assert_not_called()

    def test_skip_push_when_port_invalid(self):
        handler, _ = _make_handler()
        with patch('push.push_astm_tcp') as mock_push:
            with patch.dict(os.environ, {
                'ORDER_RESULT_PUSH_HOST': 'bridge-test',
                'ORDER_RESULT_PUSH_ASTM_PORT': 'not-a-number',
            }):
                handler.send_order_response([
                    {'sample_id': 'ACC-1', 'test_code': 'MTB-RIF'},
                ])
        mock_push.assert_not_called()

    def test_push_failure_does_not_raise(self):
        handler, _ = _make_handler()
        with patch('push.push_astm_tcp', side_effect=Exception('connection refused')):
            with patch.dict(os.environ, {
                'ORDER_RESULT_PUSH_HOST': 'bridge-test',
                'ORDER_RESULT_PUSH_ASTM_PORT': '12001',
            }):
                # Should not raise — receipt path stays successful
                handler.send_order_response([
                    {'sample_id': 'ACC-1', 'test_code': 'MTB-RIF'},
                ])


class TestProcessReceivedDataDispatchesOrderResponse(unittest.TestCase):
    """End-to-end at the dispatch layer: simulate received frames, verify the
    handler picks the order-response branch (not field-query / results-query).
    """

    def test_order_message_routes_to_send_order_response(self):
        handler, _ = _make_handler()
        handler.send_order_response = MagicMock()
        handler.send_field_query_response = MagicMock()
        handler.send_results_query_response = MagicMock()

        # Simulate received frames as if _handle_frame had appended them
        handler.received_data = [
            b'H|\\^&|||LIS|||||||||LIS2-A2',
            b'P|1|||PAT-1',
            b'O|1|ACC-555||^^^MTB-RIF|R',
            b'L|1|N',
        ]
        handler._process_received_data()

        handler.send_field_query_response.assert_not_called()
        handler.send_results_query_response.assert_not_called()
        handler.send_order_response.assert_called_once()
        args, _ = handler.send_order_response.call_args
        orders = args[0]
        self.assertEqual(orders, [
            {'sample_id': 'ACC-555', 'test_code': 'MTB-RIF'},
        ])


if __name__ == '__main__':
    unittest.main()
