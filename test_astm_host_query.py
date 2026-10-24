#!/usr/bin/env python3
"""
A GeneXpert in Host Query mode keeps one connection to the LIS, sends a query
session per specimen and waits for the LIS's reply on that connection (Cepheid
301-2002 Rev. E, 6.2.2 and 6.3.2). Without a reply in 60 seconds it cancels the
query (6.3.1.2).
"""

import http.client
import json
import os
import socket
import sys
import threading
from http.server import HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import push as push_module
from api import MockAPIHandler

NO_ORDERS = ["H|@^\\|r-1||LIS|||||Lab-1^GeneXpert^6.2||P|1394-97|20261020101500", "L|1|I"]


def _start_lis(reply):
    """A LIS that accepts one connection, records each session and answers queries with `reply`."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    seen = {"sessions": [], "connections": 0}

    def run():
        conn, _ = listener.accept()
        seen["connections"] += 1
        with conn:
            conn.settimeout(10)
            while True:
                session = push_module.receive_astm_session(conn, 10)
                if session is None:
                    break
                seen["sessions"].append(session)
                cancels = any(r.startswith("Q|") and r.split("|")[12:13] == ["A"] for r in session)
                if reply and not cancels:
                    push_module.send_astm_session(conn, reply, "lis")
        listener.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return port, seen, thread


def test_a_query_answered_with_no_orders_is_recorded():
    port, seen, thread = _start_lis(NO_ORDERS)

    results, error = push_module.query_astm_host("127.0.0.1", port, ["S-1"], "Lab-1", reply_timeout_s=5)
    thread.join(timeout=5)

    assert error is None
    assert results == [{"specimen_id": "S-1", "reply": NO_ORDERS, "orders": [], "no_orders": True}]
    query = seen["sessions"][0]
    assert query[0].startswith("H|@^\\|") and query[0].split("|")[4] == "Lab-1^GeneXpert^6.2"
    assert query[1] == "Q|1|^S-1||||||||||O@N"
    assert query[2] == "L|1|N"


def test_queries_share_one_connection():
    port, seen, thread = _start_lis(NO_ORDERS)

    results, error = push_module.query_astm_host("127.0.0.1", port, ["S-1", "S-2"], "Lab-1", reply_timeout_s=5)
    thread.join(timeout=5)

    assert error is None
    assert [r["specimen_id"] for r in results] == ["S-1", "S-2"]
    assert all(r["no_orders"] for r in results)
    assert seen["connections"] == 1
    assert [s[1] for s in seen["sessions"]] == ["Q|1|^S-1||||||||||O@N", "Q|1|^S-2||||||||||O@N"]


def test_an_unanswered_query_is_cancelled_as_the_instrument_does():
    port, seen, thread = _start_lis(None)

    results, error = push_module.query_astm_host("127.0.0.1", port, ["S-1"], "Lab-1", reply_timeout_s=0.5)
    thread.join(timeout=5)

    assert error is None
    assert results == [{"specimen_id": "S-1", "reply": None, "orders": [], "no_orders": False}]
    cancel = seen["sessions"][1]
    assert cancel[1] == "Q|1|||||||||||A"
    assert cancel[2] == "C|1|I|timeout^last request has been cancelled|I"


def _post(path, body):
    httpd = HTTPServer(("127.0.0.1", 0), MockAPIHandler)
    server = threading.Thread(target=httpd.serve_forever, daemon=True)
    server.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=20)
        connection.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_api_returns_what_the_lis_answered():
    port, seen, thread = _start_lis(NO_ORDERS)

    status, body = _post("/simulate/host-query/genexpert",
                         {"destination": f"tcp://127.0.0.1:{port}", "specimen_ids": ["S-1"],
                          "sender_id": "Lab-1", "reply_timeout_seconds": 5})
    thread.join(timeout=5)

    assert status == 200
    assert body["status"] == "completed"
    assert body["results"] == [{"specimen_id": "S-1", "reply": NO_ORDERS, "orders": [], "no_orders": True}]


def test_the_api_refuses_a_specimen_that_would_break_the_query():
    status, body = _post("/simulate/host-query/genexpert",
                         {"destination": "tcp://127.0.0.1:1", "specimen_ids": ["S-1^X"]})

    assert status == 400
    assert "may not contain" in body["error"]
