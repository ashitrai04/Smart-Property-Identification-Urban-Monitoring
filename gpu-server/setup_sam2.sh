#!/usr/bin/env bash
# A second, separate environment for SAM 2 (needs transformers >= 4.56).
# The API keeps .venv with transformers 4.44.2 — the version your SegFormer weights load
# cleanly with — so nothing that is running is touched. uv re-uses the wheels it already
# downloaded for .venv, so this takes a few minutes, not a full reinstall.
#   bash setup_sam2.sh
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"
CUDA_INDEX="${CUDA_INDEX:-https://download.pytorch.org/whl/cu126}"
PY=.venv-sam2/bin/python

[ -x "$PY" ] || uv venv .venv-sam2 --python 3.12 --seed
uv pip install --python "$PY" torch torchvision --index-url "$CUDA_INDEX"
grep -vE '^(transformers|tokenizers|safetensors|huggingface_hub)==' requirements.txt > /tmp/req-sam2.txt
uv pip install --python "$PY" -r /tmp/req-sam2.txt "transformers>=4.57,<4.58" "huggingface_hub>=0.34,<1.0" safetensors

echo "== checking SAM 2 + SegFormer in the new environment"
HF_HOME="$PWD/weights/hf" MPLBACKEND=Agg "$PY" - <<'PY'
import sys
sys.path.insert(0, "app")
import transformers, torch
from transformers import Sam2Model, Sam2Processor
mid = "facebook/sam2.1-hiera-large"
Sam2Processor.from_pretrained(mid); Sam2Model.from_pretrained(mid)
print("SAM 2 ready:", mid, "| transformers", transformers.__version__, "| cuda", torch.cuda.is_available())
PY
echo "== done: refine_now.py will use .venv-sam2"
