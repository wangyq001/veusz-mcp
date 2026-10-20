import json
import socket
import threading
import time
import unittest

from veusz_bridge.protocol import Broker, BridgeError, LocalServer


def request(rid="one", **extra):
    result = {"id": rid, "token": "test-token", "method": "set", "params": {}}
    result.update(extra)
    return result


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.queue = []
        self.broker = Broker("test-token", self.queue.append)

    def test_reconnect_reuses_completed_mutation_without_reexecution(self):
        job = self.broker.submit(request())
        calls = []
        self.broker.execute(job, lambda req: calls.append(req) or {"revision": 1})
        again = self.broker.submit(request(timeout_ms=20000))
        self.broker.execute(again, lambda req: self.fail("Mutation replayed"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.queue), 1)
        self.assertEqual(self.broker.await_result(again)["result"]["revision"], 1)

    def test_same_id_with_different_arguments_is_rejected(self):
        self.broker.submit(request())
        with self.assertRaises(BridgeError) as caught:
            self.broker.submit(request(params={"different": True}))
        self.assertEqual(caught.exception.code, "ID_REUSED")

    def test_unauthorized_request_never_reaches_gui_queue(self):
        with self.assertRaises(BridgeError):
            self.broker.submit(request(token="bad"))
        self.assertEqual(self.queue, [])

    def test_expired_queued_mutation_never_executes_later(self):
        job = self.broker.submit(request(timeout_ms=1))
        result = self.broker.await_result(job)
        self.assertEqual(result["error"]["code"], "EXPIRED")
        self.broker.execute(job, lambda req: self.fail("Expired job executed"))

    def test_started_mutation_is_queryable_after_timeout(self):
        job = self.broker.submit(request(timeout_ms=10))
        started = threading.Event()
        release = threading.Event()

        def handler(req):
            started.set()
            release.wait(2)
            return {"revision": 2}

        worker = threading.Thread(target=self.broker.execute, args=(job, handler))
        worker.start()
        self.assertTrue(started.wait(1))
        self.assertEqual(self.broker.await_result(job)["error"]["code"], "IN_PROGRESS")
        release.set()
        worker.join(2)
        self.assertTrue(self.broker.await_result(self.broker.submit(request()))["ok"])

    def test_old_write_result_expiry_does_not_allow_replay(self):
        broker = Broker("test-token", self.queue.append, capacity=1)
        original = broker.submit(request())
        broker.execute(original, lambda req: "done")
        second = broker.submit(request("two"))
        broker.execute(second, lambda req: "second")
        with self.assertRaises(BridgeError) as caught:
            broker.submit(request())
        self.assertEqual(caught.exception.code, "RESULT_EXPIRED")

    def test_handler_exception_is_cached_without_reexecution(self):
        job = self.broker.submit(request())

        def fail(req):
            raise BridgeError("REVISION_CONFLICT", "Document changed")

        self.broker.execute(job, fail)
        self.assertEqual(self.broker.await_result(job)["error"]["code"], "REVISION_CONFLICT")
        self.broker.execute(job, lambda req: self.fail("Failed request replayed"))

    def test_malformed_json_and_auth_failure_do_not_kill_server(self):
        server = LocalServer(self.broker)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for raw, expected in [(b"{bad}\n", "BAD_REQUEST"),
                                  (json.dumps(request(token="bad")).encode() + b"\n", "UNAUTHORIZED"),
                                  (b'{"value":NaN}\n', "BAD_REQUEST")]:
                with socket.create_connection(server.server_address, timeout=2) as sock:
                    sock.sendall(raw)
                    with sock.makefile("rb") as stream:
                        response = json.loads(stream.readline())
                self.assertEqual(response["error"]["code"], expected)
            self.assertEqual(self.queue, [])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
