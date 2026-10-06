"""Shared GPU access.

The A100s on this box are shared with other users, so free memory changes under
us. Interactive requests (the webapp) and batch jobs take this lock per unit of
work — a job holds it for one window at a time — so a user's request waits
seconds behind a 50 GB job, not hours.
"""
import threading

import torch

GPU_LOCK = threading.RLock()


def device_info():
    import os
    if not torch.cuda.is_available() or os.environ.get("SEG_DEVICE", "").startswith("cpu"):
        return {"device": "cpu"}
    i = torch.cuda.current_device()
    free, total = torch.cuda.mem_get_info(i)
    return {"device": f"cuda:{i}", "name": torch.cuda.get_device_name(i),
            "free_gb": round(free / 2**30, 2), "total_gb": round(total / 2**30, 2)}


def is_oom(e):
    return isinstance(e, torch.cuda.OutOfMemoryError) or "out of memory" in str(e).lower()


def release():
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
