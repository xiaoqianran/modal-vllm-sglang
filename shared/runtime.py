"""Process and HTTP readiness helpers shared by vLLM and SGLang."""
import subprocess
import threading
import time
import urllib.error
import urllib.request


def wait_for_health(process, url, *, timeout=900, interval=3, request_timeout=3):
    """Wait for HTTP 200, failing immediately when the engine dies."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Engine exited during startup: {process.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=request_timeout) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    raise TimeoutError(f"Service not ready within {timeout}s: {url}")


def stop_process(process, *, timeout=20):
    """Terminate a child and reap it; force-kill after a bounded wait."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def monitor_processes(processes, volume, *, label, poll_interval=2, commit_interval=None):
    """Monitor linked services; stop siblings on exit and commit persisted files."""
    processes = tuple(processes)
    def run():
        last_commit = time.monotonic()
        try:
            while all(process.poll() is None for process in processes):
                if commit_interval and time.monotonic() - last_commit >= commit_interval:
                    try:
                        volume.commit()
                    except Exception as exc:
                        print(f"{label} periodic commit failed: {exc}", flush=True)
                    last_commit = time.monotonic()
                time.sleep(poll_interval)
            print(f"{label} process exits: {[p.poll() for p in processes]}", flush=True)
        finally:
            for process in processes:
                try:
                    stop_process(process)
                except Exception as exc:
                    print(f"{label} process cleanup failed: {exc}", flush=True)
            try:
                volume.commit()
            except Exception as exc:
                print(f"{label} final commit failed: {exc}", flush=True)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread
