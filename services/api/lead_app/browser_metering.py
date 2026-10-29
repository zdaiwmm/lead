"""Only the native process belonging to this exact profile is measured."""

import time

import psutil


class Counter:
    def __init__(self, profile):
        self.profile = str(profile)
        self.previous = {}
        self.cpu_seconds = 0.0
        self.rss_peak = 0
        self.fd_peak = 0
        self.samples = 0
        self.started = time.monotonic()
        self.root = None

    def sample(self, proxy):
        if self.root is None:
            for process in psutil.Process().children(recursive=True):
                try:
                    if (
                        self.profile in process.cmdline()
                        and process.name().lower().startswith("camoufox")
                    ):
                        self.root = process
                        break
                except psutil.Error:
                    pass
        rss = fds = 0
        current = {}
        if self.root:
            try:
                children = [self.root] + self.root.children(recursive=True)
            except psutil.Error:
                children = []
            for process in children:
                try:
                    key = (process.pid, process.create_time())
                    timing = process.cpu_times()
                    current[key] = timing.user + timing.system
                    if key in self.previous:
                        self.cpu_seconds += max(0, current[key] - self.previous[key])
                    rss += process.memory_info().rss
                    fds += process.num_fds()
                except psutil.Error:
                    pass
        self.previous = current
        self.samples += 1
        self.rss_peak = max(self.rss_peak, rss)
        self.fd_peak = max(self.fd_peak, fds)
        return {
            "schema": "browser-usage-v1",
            "sample_count": self.samples,
            "cpu_sampled_seconds": round(self.cpu_seconds, 4),
            "rss_sum_peak_bytes": self.rss_peak,
            "fd_sum_peak": self.fd_peak,
            "proxy_bytes": proxy.bytes_forwarded if proxy else 0,
            "proxy_connections": proxy.connections if proxy else 0,
            "cpu_coverage": "sampled_lower_bound",
            "sampled_elapsed_seconds": round(time.monotonic() - self.started, 3),
        }
