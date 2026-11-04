"""Turn a laptop that only has the node package into a full Urbanly server.

Run once. It fetches the project, builds a virtual environment beside it,
installs PyTorch and the rest, sizes the model settings to this machine's
graphics card, and pulls the weights from the other laptop.

Deliberately stdlib-only: this runs before any environment exists, on whatever
Python the machine happens to have.

    python setup_server.py                 install into C:\\Urbanly
    python setup_server.py --dir E:\\Urb    somewhere else
    python setup_server.py --skip-torch    everything except the 2.5 GB download
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO_ZIP = ("https://codeload.github.com/ashitrai04/"
            "Smart-Property-Identification-Urban-Monitoring/zip/refs/heads/main")
REPO_GIT = ("https://github.com/ashitrai04/"
            "Smart-Property-Identification-Urban-Monitoring.git")
FOLDER = "Smart-Property-Identification-Urban-Monitoring"

# Matching the machine that trained the model, so a checkpoint loads the same
# way on both. cu126 wheels run on any reasonably current NVIDIA driver.
TORCH_INDEX = "https://download.pytorch.org/whl/cu126"

STEP = 0


def step(msg):
    global STEP
    STEP += 1
    print(f"\n[{STEP}] {msg}", flush=True)


def run(args, cwd=None, quiet=False):
    print(f"      $ {' '.join(str(a) for a in args[:6])}"
          + (" ..." if len(args) > 6 else ""), flush=True)
    r = subprocess.run(args, cwd=cwd,
                       stdout=subprocess.DEVNULL if quiet else None)
    return r.returncode == 0


# ── graphics card ───────────────────────────────────────────────────
def gpu_memory_gb():
    """Dedicated video memory, in GB, via Windows itself.

    Asked before PyTorch exists, because the answer decides how PyTorch is
    configured. AdapterRAM misreports above 4 GB on some drivers, so the
    registry value is preferred and this is only the fallback.
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\"
             "{4d36e968-e325-11ce-bfc1-08002be10318}\\*' -ErrorAction SilentlyContinue |"
             " Where-Object { $_.'HardwareInformation.qwMemorySize' } |"
             " Sort-Object 'HardwareInformation.qwMemorySize' -Descending |"
             " Select-Object -First 1).'HardwareInformation.qwMemorySize'"],
            capture_output=True, text=True, timeout=40)
        v = out.stdout.strip()
        if v.isdigit() and int(v) > 0:
            return round(int(v) / 1024 ** 3, 1)
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_VideoController | Where-Object { $_.AdapterRAM } |"
             " Sort-Object AdapterRAM -Descending | Select-Object -First 1).AdapterRAM"],
            capture_output=True, text=True, timeout=40)
        v = out.stdout.strip()
        if v.isdigit() and int(v) > 0:
            return round(int(v) / 1024 ** 3, 1)
    except Exception:
        pass
    return None


def gpu_name():
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_VideoController | Where-Object { $_.AdapterRAM } |"
             " Sort-Object AdapterRAM -Descending | Select-Object -First 1).Name"],
            capture_output=True, text=True, timeout=40)
        return out.stdout.strip() or None
    except Exception:
        return None


def tuning_for(vram):
    """Settings that fit the card.

    The defaults were chosen on a 6 GB card. On 4 GB a batch of eight
    1536-pixel windows does not fit, and the failure is an out-of-memory
    part-way through a long job rather than at startup - so the sizes are cut
    here instead of being discovered the hard way.
    """
    if vram is None:
        return {}, "graphics memory unknown - leaving defaults"
    if vram <= 4.5:
        return ({"SEG_BATCH": "2", "SP_JOB_WINDOW": "1024",
                 "SP_POTHOLE_WINDOW": "2048", "SEG_FP16": "1"},
                f"{vram} GB card - smaller windows and batches")
    if vram <= 6.5:
        return ({"SEG_BATCH": "4", "SP_JOB_WINDOW": "1536", "SEG_FP16": "1"},
                f"{vram} GB card - stock settings")
    return ({"SEG_BATCH": "8", "SEG_FP16": "1"}, f"{vram} GB card - full size")


# ── fetching the project ────────────────────────────────────────────
def fetch_repo(dest: Path):
    target = dest / FOLDER
    if (target / "gpu-server" / "app" / "main.py").is_file():
        print("      already present")
        if shutil.which("git") and (target / ".git").is_dir():
            run(["git", "-C", str(target), "pull", "--ff-only"], quiet=True)
        return target

    dest.mkdir(parents=True, exist_ok=True)
    if shutil.which("git"):
        if run(["git", "clone", "--depth", "1", REPO_GIT, str(target)]):
            return target
        print("      git clone failed, falling back to a zip download")

    tmp = dest / "_repo.zip"
    print(f"      downloading {REPO_ZIP}")
    with urllib.request.urlopen(REPO_ZIP, timeout=300) as r, open(tmp, "wb") as fh:
        shutil.copyfileobj(r, fh)
    with zipfile.ZipFile(tmp) as z:
        z.extractall(dest)
    tmp.unlink(missing_ok=True)
    extracted = dest / f"{FOLDER}-main"
    if extracted.is_dir():
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        extracted.rename(target)
    return target


# ── the environment ─────────────────────────────────────────────────
def make_venv(repo: Path):
    venv = repo / "gpu-server" / ".venv"
    py = venv / "Scripts" / "python.exe"
    if py.is_file():
        print("      already built")
        return py
    if not run([sys.executable, "-m", "venv", str(venv)]):
        sys.exit("could not create the virtual environment")
    run([str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"], quiet=True)
    return py


def install(py: Path, repo: Path, skip_torch: bool):
    if skip_torch:
        print("      skipping PyTorch as asked")
    else:
        print("      installing PyTorch - this is the slow part, about 2.5 GB")
        if not run([str(py), "-m", "pip", "install", "torch", "torchvision",
                    "--index-url", TORCH_INDEX]):
            sys.exit("PyTorch install failed - check the internet connection")
    req = repo / "gpu-server" / "requirements.txt"
    if req.is_file():
        print("      installing the rest")
        if not run([str(py), "-m", "pip", "install", "-r", str(req)]):
            sys.exit("dependency install failed")
    # ultralytics, --no-deps and on its own: with its dependencies it pulls
    # the GUI build of opencv and moves numpy, both of which break rasterio.
    # Its runtime deps are already pinned in requirements.txt above. The
    # Linux setup.sh has always done this; leaving it out of the Windows
    # installer is why a fresh machine started the model server and lost it
    # on "No module named ultralytics" - a package only potholes use.
    print("      installing ultralytics (pothole model)")
    run([str(py), "-m", "pip", "install", "--quiet", "--no-deps",
         "ultralytics>=8.4,<8.5", "ultralytics-thop"], quiet=True)

    # The data server needs these too; they overlap with the above but are
    # named here so a change to one requirements file cannot silently break it.
    run([str(py), "-m", "pip", "install", "--quiet",
         "fastapi", "uvicorn[standard]", "rasterio", "rio-tiler", "pyogrio",
         "geopandas", "shapely", "numpy", "requests"], quiet=True)


def write_env(repo: Path, tuning: dict, why: str):
    cfg = repo / "gpu-server" / "config.env"
    lines = ["# Written by setup_server.py for this machine's graphics card.",
             f"# {why}", ""]
    lines += [f"{k}={v}" for k, v in tuning.items()]
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"      {cfg.name}: {why}")
    return cfg


def verify(py: Path, repo: Path):
    code = (
        "import torch, sys;"
        "ok = torch.cuda.is_available();"
        "print('      torch', torch.__version__, '| CUDA', torch.version.cuda,"
        "      '| usable', ok);"
        "print('      device:', torch.cuda.get_device_name(0) if ok else 'CPU only');"
        "print('      memory:', round(torch.cuda.mem_get_info(0)[1]/1e9,2), 'GB') if ok else None"
    )
    run([str(py), "-c", code])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=r"C:\Urbanly", help="where to install")
    ap.add_argument("--skip-torch", action="store_true")
    a = ap.parse_args()

    here = Path(__file__).resolve().parent
    dest = Path(a.dir)

    print("=" * 66)
    print("  Urbanly - turning this laptop into a full server")
    print("=" * 66)

    step("looking at this machine")
    name, vram = gpu_name(), gpu_memory_gb()
    print(f"      graphics: {name or 'unknown'}")
    print(f"      memory  : {str(vram) + ' GB' if vram else 'unknown'}")
    tuning, why = tuning_for(vram)
    if vram is None:
        print("      (settings left at their defaults; adjust later if a job"
              " runs out of memory)")

    step(f"fetching the project into {dest}")
    repo = fetch_repo(dest)
    print(f"      {repo}")

    step("building the virtual environment")
    py = make_venv(repo)

    step("installing packages")
    install(py, repo, a.skip_torch)

    step("sizing the settings to this card")
    write_env(repo, tuning, why)

    step("checking PyTorch can see the card")
    verify(py, repo)

    step("pointing the node package at the project")
    cfg = here / "config.env"
    txt = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    lines, seen = [], set()
    for line in txt.splitlines():
        k = line.split("=", 1)[0].strip() if "=" in line else None
        if k in ("UN_GPU_SERVER_DIR", "UN_DATA_SERVER_DIR", "UN_WEIGHTS_DIR"):
            seen.add(k)
            continue
        lines.append(line)
    lines += [
        "",
        "# Set by setup_server.py - where the project was installed.",
        f"UN_GPU_SERVER_DIR={repo / 'gpu-server'}",
        f"UN_DATA_SERVER_DIR={repo / 'local-server'}",
        f"UN_WEIGHTS_DIR={repo / 'gpu-server' / 'weights'}",
    ]
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"      {cfg}")

    # The project carries its own copy of this package, and its START
    # URBANLY.bat is just as double-clickable. Give that copy the same
    # settings, so it does not matter which of the two gets started.
    twin = repo / "urbanly-node" / "config.env"
    try:
        if twin.parent.is_dir() and twin.resolve() != cfg.resolve():
            shutil.copy2(cfg, twin)
            print(f"      {twin}")
    except OSError:
        pass

    step("pulling the model weights from the other laptop")
    (repo / "gpu-server" / "weights").mkdir(parents=True, exist_ok=True)
    ok = run([sys.executable, str(here / "node.py"), "sync"])
    if not ok:
        print("      could not sync now - start Urbanly on the other laptop,")
        print("      then run:  START URBANLY.bat sync")

    print("\n" + "=" * 66)
    print("  Done. Start it with:   START URBANLY.bat")
    print("=" * 66)


if __name__ == "__main__":
    main()
