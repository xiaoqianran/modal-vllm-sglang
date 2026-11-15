"""Best-effort container resource sampling; no runtime dependencies required."""
import subprocess
import threading
from pathlib import Path


def resource_sample():
    result = {}
    # cgroup v2 includes the model server and its children, unlike Python RSS.
    for filename, key, scale in (("memory.current", "ram_bytes", 1),
                                 ("memory.peak", "ram_peak_bytes", 1)):
        try:
            result[key] = int(Path("/sys/fs/cgroup", filename).read_text()) * scale
        except (OSError, ValueError):
            pass
    try:
        stats = dict(line.split() for line in Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines())
        result["cpu_seconds"] = int(stats["usage_usec"]) / 1_000_000
    except (OSError, ValueError, KeyError):
        pass
    try:
        process = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2, check=True)
        result["gpus"] = [{"used_mib": int(memory.strip()), "utilization_percent": int(util.strip())}
                          for memory, util in (line.split(",") for line in process.stdout.splitlines())]
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return result


class ResourceMonitor:
    """Container lifetime peaks, sampled VRAM peaks; unavailable fields are omitted."""
    def __init__(self, interval=5, recorder=None):
        self.recorder = recorder
        self.interval = interval
        self.done = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.latest = {}
        self.gpu_peaks = []
        self.thread = threading.Thread(target=self.run, daemon=True)

    def sample(self):
        value = {**resource_sample(), "sample_interval_seconds": self.interval}
        if self.recorder is not None:
            try:
                self.recorder.resource(value)
            except Exception:
                pass
        with self.lock:
            self.latest = value
            for index, gpu in enumerate(value.get("gpus", [])):
                if index == len(self.gpu_peaks):
                    self.gpu_peaks.append(0)
                self.gpu_peaks[index] = max(self.gpu_peaks[index], gpu["used_mib"])

    def run(self):
        while not self.done.is_set():
            self.sample()
            self.wake.wait(self.interval)
            self.wake.clear()

    def request(self):
        """Ask the background sampler for a boundary sample without waiting for nvidia-smi."""
        self.wake.set()

    def start(self):
        self.thread.start()

    def snapshot(self, *, refresh=True):
        if refresh:
            self.sample()
        with self.lock:
            return {**self.latest, "sampled_gpu_peak_mib": list(self.gpu_peaks),
                    "sample_interval_seconds": self.interval}

    def stop(self):
        self.done.set()
        self.wake.set()
        self.thread.join(timeout=3)
