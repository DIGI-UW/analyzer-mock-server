#!/usr/bin/env python3
"""Unit tests for the order-in/result-out behavior added to ASTMProtocolHandler.

Covers the LIS-initiated flow where the bridge sends an order to the mock and
the mock pushes a result back on the same connection, echoing the originating
accession (ASTM O-record field 3) so OpenELIS can correlate via the existing
inbound result-import path.
"""

import unittest
from unittest.mock import MagicMock

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


_UNSET = object()


def _make_handler(template=_UNSET):
    conn = MagicMock()
    conn.send = MagicMock()
    # _send_frame uses conn.recv(1) directly (not _receive_byte) to wait for the
    # per-frame ACK; make every recv return ACK so the send loop completes.
    conn.recv = MagicMock(return_value=server.ACK)
    if template is _UNSET:
        template = GENEXPERT_TEMPLATE
    handler = server.ASTMProtocolHandler(
        conn=conn,
        addr=('127.0.0.1', 12345),
        fields_config={},
        response_delay_ms=0,
        astm_template=template,
    )
    return handler, conn


def _decode_sent(conn):
    """Concatenate every byte the handler sent via conn.send and return text."""
    chunks = []
    for call in conn.send.call_args_list:
        args, _ = call
        if args:
            chunks.append(args[0])
    return b''.join(chunks).decode('utf-8', errors='replace')


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

    def test_pushes_result_with_echoed_accession(self):
        handler, conn = _make_handler()
        # Stub the ACK reply to our ENQ + each frame
        handler._receive_byte = MagicMock(return_value=server.ACK)

        handler.send_order_response([
            {'sample_id': 'ACC-12345', 'test_code': 'MTB-RIF'},
        ])

        sent = _decode_sent(conn)
        # ENQ was the first byte sent
        self.assertIn('\x05', sent)
        # Header, patient, order, result, terminator records all present
        self.assertIn('H|', sent)
        self.assertIn('P|1|', sent)
        # The originating accession is echoed verbatim in the O-record
        self.assertIn('O|1|ACC-12345|', sent)
        # Result record carries the requested test code + template seed value
        self.assertIn('R|1|^^^MTB-RIF|NOT DETECTED', sent)
        self.assertIn('L|1|N', sent)
        # EOT closes the session
        self.assertEqual(sent[-1], '\x04')

    def test_numeric_field_emits_value_with_unit(self):
        handler, conn = _make_handler()
        handler._receive_byte = MagicMock(return_value=server.ACK)

        handler.send_order_response([
            {'sample_id': 'ACC-99', 'test_code': 'HIV-VL'},
        ])

        sent = _decode_sent(conn)
        self.assertIn('R|1|^^^HIV-VL|1250|copies/mL', sent)

    def test_multiple_tests_one_sample_share_pid_block(self):
        handler, conn = _make_handler()
        handler._receive_byte = MagicMock(return_value=server.ACK)

        handler.send_order_response([
            {'sample_id': 'ACC-7', 'test_code': 'MTB-RIF'},
            {'sample_id': 'ACC-7', 'test_code': 'HIV-VL'},
        ])

        sent = _decode_sent(conn)
        # Two R records, single P/O block
        self.assertEqual(sent.count('P|1|'), 1)
        self.assertEqual(sent.count('O|1|'), 1)
        self.assertIn('R|1|^^^MTB-RIF', sent)
        self.assertIn('R|2|^^^HIV-VL', sent)

    def test_unknown_test_code_is_skipped_not_fatal(self):
        handler, conn = _make_handler()
        handler._receive_byte = MagicMock(return_value=server.ACK)

        handler.send_order_response([
            {'sample_id': 'ACC-1', 'test_code': 'UNKNOWN'},
            {'sample_id': 'ACC-1', 'test_code': 'MTB-RIF'},
        ])

        sent = _decode_sent(conn)
        self.assertNotIn('^^^UNKNOWN', sent)
        self.assertIn('^^^MTB-RIF', sent)

    def test_no_template_aborts_quietly(self):
        handler, conn = _make_handler(template=None)
        handler.send_order_response([
            {'sample_id': 'ACC-1', 'test_code': 'MTB-RIF'},
        ])
        # Nothing should be sent over the wire when there's no template
        self.assertEqual(conn.send.call_count, 0)

    def test_no_ack_after_enq_aborts_send(self):
        handler, conn = _make_handler()
        handler._receive_byte = MagicMock(return_value=server.NAK)

        handler.send_order_response([
            {'sample_id': 'ACC-1', 'test_code': 'MTB-RIF'},
        ])

        sent = _decode_sent(conn)
        # Only the ENQ should have been sent; no result frames
        self.assertNotIn('R|', sent)
        self.assertNotIn('H|\\^&', sent)


class TestProcessReceivedDataDispatchesOrderResponse(unittest.TestCase):
    """End-to-end at the dispatch layer: simulate received frames, verify the
    handler picks the order-response branch (not field-query / results-query).
    """

    def test_order_message_routes_to_send_order_response(self):
        handler, _ = _make_handler()
        handler._receive_byte = MagicMock(return_value=server.ACK)
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
