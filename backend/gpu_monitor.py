"""Cached NVIDIA telemetry; no model or CUDA context is initialized."""
import csv
import io
import subprocess
import threading
import time

_lock = threading.Lock()
_cached = None
_expires = 0.

def gpu_snapshot():
    global _cached, _expires
    with _lock:
        if _cached is not None and time.monotonic() < _expires:
            return _cached
        try:
            result = subprocess.run(["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=2, check=True)
            devices = []
            def number(value):
                try:return float(value.strip())
                except ValueError:return None
            for row in csv.reader(io.StringIO(result.stdout)):
                if len(row) != 6:raise ValueError("Unexpected GPU telemetry format")
                devices.append(dict(index=int(row[0]),name=row[1].strip(),utilization_percent=number(row[2]),memory_used_mb=number(row[3]),memory_total_mb=number(row[4]),temperature_c=number(row[5])))
            _cached = {"available": bool(devices), "devices": devices}
        except (OSError, subprocess.SubprocessError, ValueError):
            _cached = {"available": False, "devices": [], "error": "GPU 指标暂不可用"}
        _expires = time.monotonic() + 5
        return _cached
