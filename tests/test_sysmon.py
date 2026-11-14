import os, shutil, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pm5m import sysmon as S


class TestParsers(unittest.TestCase):
    def test_cpu_line(self):
        tot, idle = S.parse_cpu_line("cpu  100 0 50 800 50 0 0 0 0 0")
        self.assertEqual((tot, idle), (1000, 850))
        self.assertIsNone(S.parse_cpu_line("intr 123"))

    def test_meminfo(self):
        d = S.parse_meminfo("MemTotal:       8000000 kB\nMemFree: 1 kB\nMemAvailable:   6000000 kB\n")
        self.assertEqual(d, {"MemTotal": 8000000 * 1024, "MemAvailable": 6000000 * 1024})

    def test_netdev_excludes_lo(self):
        txt = ("Inter-|   Receive\n face |bytes\n"
               "    lo: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0\n"
               "  eth0: 1000 1 0 0 0 0 0 0 500 1 0 0 0 0 0 0\n")
        self.assertEqual(S.parse_netdev(txt), (1000, 500))

    def test_self_stat_with_spaces_in_comm(self):
        line = "123 (python 3 x) S 1 123 123 0 -1 4194560 100 0 0 0 40 60 0 0 20 0 1 0 1 0 0"
        self.assertEqual(S.parse_self_stat_ticks(line), 100)

    def test_vmrss(self):
        self.assertEqual(S.parse_vmrss("Name: x\nVmRSS:\t  2048 kB\n"), 2048 * 1024)


class TestSampler(unittest.TestCase):
    def test_rates_and_totals(self):
        readings = [
            {"t": 0.0, "cpu_total": 1000, "cpu_idle": 800, "proc_ticks": 0, "mem_total": 100, "mem_avail": 60,
             "rx": 1000, "tx": 500, "rss": 10_000_000, "feed_bytes": 0, "http_out": 0},
            {"t": 2.0, "cpu_total": 1200, "cpu_idle": 900, "proc_ticks": 200, "mem_total": 100, "mem_avail": 50,
             "rx": 3000, "tx": 1500, "rss": 12_000_000, "feed_bytes": 4000, "http_out": 600},
        ]
        it = iter(readings)
        m = S.SysMon("/tmp", lambda: 0, lambda: 0, reader=lambda: next(it))
        self.assertIsNone(m.sample())
        p = m.sample()
        self.assertAlmostEqual(p["cpu_sys"], 50.0)          # 100 busy of 200 total
        self.assertAlmostEqual(p["cores"], 200 / S.CLK / 2.0, places=3)
        self.assertAlmostEqual(p["mem_sys"], 50.0)
        self.assertEqual(p["rx_bps"], 1000)
        self.assertEqual(p["tx_bps"], 500)
        self.assertEqual(p["feed_bps"], 2000)
        self.assertEqual(p["http_bps"], 300)
        s = m.summary()
        self.assertEqual(s["mem"]["total_bytes"], 100)
        self.assertEqual(s["mem"]["used_bytes"], 50)
        self.assertEqual(s["nic"]["rx_bytes"], 3000)
        self.assertEqual(s["nic"]["tx_bytes"], 1500)
        self.assertEqual(s["totals_since_start"]["nic_rx_bytes"], 2000)
        self.assertEqual(s["totals_since_start"]["feed_bytes"], 4000)
        self.assertEqual(s["stats"]["rx_bps"]["max"], 1000)
        self.assertEqual(len(s["series"]["t"]), 1)

    def test_counter_wrap_clamped(self):
        readings = [{"t": 0.0, "rx": 5000, "tx": 5000, "feed_bytes": 0, "http_out": 0},
                    {"t": 1.0, "rx": 100, "tx": 100, "feed_bytes": 0, "http_out": 0}]
        it = iter(readings)
        m = S.SysMon("/tmp", lambda: 0, lambda: 0, reader=lambda: next(it))
        m.sample(); p = m.sample()
        self.assertEqual(p["rx_bps"], 0)

    def test_window_bounded(self):
        m = S.SysMon("/tmp", lambda: 0, lambda: 0, window=5,
                     reader=lambda: {"t": S.time.time(), "feed_bytes": 0, "http_out": 0})
        for _ in range(20):
            m.sample()
        self.assertEqual(len(m.hist), 5)

    def test_real_proc_reads_on_linux(self):
        if not os.path.exists("/proc/stat"):
            self.skipTest("not Linux")
        d = tempfile.mkdtemp()
        try:
            m = S.SysMon(d, lambda: 0, lambda: 0)
            m.sample(); import time; time.sleep(0.2); p = m.sample()
            self.assertIn("cpu_sys", p)
            self.assertIn("rss_mb", p)
            self.assertGreater(m.summary()["disk"]["free_bytes"], 0)
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
