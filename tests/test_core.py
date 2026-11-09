import os
import tempfile
import unittest

from trainmon.config import EXAMPLE_CONFIG, load_config
from trainmon.eta import EtaEstimator
from trainmon.parser import LogParser
from trainmon.tailer import Tailer


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = LogParser()

    def test_common_log_lines(self):
        parsed = self.parser.parse_line(
            "2026-01-01 10:00:00 INFO step 120/1200 loss=0.1234 lr=1.0e-4")
        self.assertEqual(parsed["step"], 120)
        self.assertEqual(parsed["total_steps"], 1200)
        self.assertEqual(parsed["loss"], 0.1234)
        self.assertEqual(parsed["lr"], 0.0001)
        self.assertIn("log_ts", parsed)
        self.assertEqual(
            self.parser.parse_line("global_step=500 total_steps=2000 loss: 0.42"),
            {"step": 500, "loss": 0.42},
        )

    def test_tqdm_and_dict_style(self):
        parsed = self.parser.parse_line(
            " 45%|####      | 900/2000 [01:20<01:38, 11.1it/s]")
        self.assertEqual(parsed["step"], 900)
        self.assertEqual(parsed["total_steps"], 2000)
        self.assertEqual(parsed["reported_rate"], 11.1)
        parsed = self.parser.parse_line(
            "{'loss': 1.234, 'learning_rate': 1e-5, 'epoch': 0.5}")
        self.assertEqual(parsed["loss"], 1.234)
        self.assertEqual(parsed["lr"], 1e-5)
        self.assertEqual(parsed["epoch"], 0.5)

    def test_done_and_fail(self):
        self.assertTrue(self.parser.parse_line("Training complete")["done"])
        self.assertTrue(self.parser.parse_line("CUDA out of memory")["fail"])


class EtaTests(unittest.TestCase):
    def test_linear_rate_and_interval(self):
        estimator = EtaEstimator()
        t0 = 1000.0
        for i in range(20):
            estimator.add(t0 + i * 0.5, i * 10)
        rate, eta, confidence, fast, slow = estimator.estimate(1000, 190)
        self.assertAlmostEqual(rate, 20.0, places=3)
        self.assertAlmostEqual(eta, 40.5, places=3)
        self.assertEqual(confidence, "high")
        self.assertLessEqual(fast, eta)
        self.assertIsNotNone(slow)
        self.assertGreaterEqual(slow, eta)

    def test_coarse_timestamps(self):
        estimator = EtaEstimator()
        for i in range(20):
            estimator.add(1000.0 + i, 10 + i * 43)
        rate, eta, _, fast, slow = estimator.estimate(2000, 10 + 19 * 43)
        self.assertAlmostEqual(rate, 43.0, places=2)
        self.assertIsNotNone(eta)
        self.assertLessEqual(fast, eta)
        self.assertGreaterEqual(slow, eta)

    def test_no_total(self):
        estimator = EtaEstimator()
        for i in range(10):
            estimator.add(1000.0 + i, i * 5)
        _, eta, _, _, _ = estimator.estimate(None, 45)
        self.assertIsNone(eta)

    def test_reset_on_large_backwards_step(self):
        estimator = EtaEstimator()
        for i in range(10):
            estimator.add(1000.0 + i, i * 10)
        estimator.add(1011.0, 1)
        self.assertEqual(len(estimator.samples), 1)


class TailerTests(unittest.TestCase):
    def test_incremental_and_truncate(self):
        handle = tempfile.NamedTemporaryFile(mode="w", delete=False)
        path = handle.name
        try:
            handle.write("one\ntwo\n")
            handle.close()
            tailer = Tailer(path)
            self.assertEqual(tailer.poll(), ["one", "two"])
            with open(path, "a") as append:
                append.write("three\n")
            self.assertEqual(tailer.poll(), ["three"])
            with open(path, "w") as rewrite:
                rewrite.write("new\n")
            self.assertEqual(tailer.poll(), ["new"])
        finally:
            os.unlink(path)

    def test_partial_line(self):
        handle = tempfile.NamedTemporaryFile(mode="w", delete=False)
        path = handle.name
        try:
            handle.write("step 1/10")
            handle.close()
            tailer = Tailer(path)
            self.assertEqual(tailer.poll(), [])
            with open(path, "a") as append:
                append.write("\n")
            self.assertEqual(tailer.poll(), ["step 1/10"])
        finally:
            os.unlink(path)


class ConfigTests(unittest.TestCase):
    def test_example_config_is_valid(self):
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False)
        path = handle.name
        try:
            handle.write(EXAMPLE_CONFIG)
            handle.close()
            config = load_config(path)
            self.assertIn("gpu-01", config.servers)
            self.assertEqual(config.resolve().host, "gpu-01")
            self.assertEqual(config.resolve().remote_dir_cmd, '"$HOME"/trainmon')
        finally:
            os.unlink(path)

    def test_ssh_fields(self):
        text = """
[general]
default = example

[server:example]
host = example-host
user = alice
port = 2222
identity_file = ~/.ssh/id_ed25519
extra_ssh_args = -o ProxyJump=bastion
remote_dir = ~/tm
local_port = 9000
remote_port = 9001
idle_exit = 60
"""
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False)
        path = handle.name
        try:
            handle.write(text)
            handle.close()
            server = load_config(path).resolve()
            self.assertEqual(server.name, "example")
            self.assertEqual(server.port, 2222)
            self.assertEqual(server.local_port, 9000)
            self.assertEqual(server.remote_port, 9001)
            self.assertEqual(server.idle_exit, 60.0)
            args = server.ssh_args()
            self.assertIn("-p", args)
            self.assertIn("2222", args)
            self.assertIn("-i", args)
            self.assertIn("-l", args)
            self.assertIn("alice", args)
            self.assertIn("ProxyJump=bastion", args)
            self.assertEqual(args[-1], "example-host")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
