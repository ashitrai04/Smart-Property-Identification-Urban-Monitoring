#!/usr/bin/env bash
# One-time setup on the GPU server. No root needed.
#   bash setup.sh
set -euo pipefail
cd "$(dirname "$0")"

CUDA_INDEX="${CUDA_INDEX:-https://download.pytorch.org/whl/cu126}"   # driver 580 runs CUDA 12.x wheels

# 1) uv — a standalone Python/venv manager in ~/.local/bin (this box has no python3-venv)
if ! command -v uv >/dev/null 2>&1; then
  echo "== installing uv to ~/.local/bin"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# 2) isolated environment (does not touch the system Python other users rely on)
echo "== creating .venv (Python 3.12)"
uv venv .venv --python 3.12 --seed
PY=.venv/bin/python

echo "== PyTorch (CUDA) from $CUDA_INDEX"
uv pip install --python "$PY" torch torchvision --index-url "$CUDA_INDEX"

echo "== app requirements"
uv pip install --python "$PY" -r requirements.txt
uv pip install --python "$PY" --no-deps "ultralytics>=8.4,<8.5" ultralytics-thop

# 3) folders + config
mkdir -p data/raw data/cogs data/results weights/hf bin app/demo

# Large files are fetched here from the public SegFormer Space instead of being
# uploaded through the browser (big Jupyter uploads tend to stop partway).
SPACE_RAW="https://huggingface.co/spaces/asashit/smart-property-segformer/resolve/main"
fetch() {   # fetch <remote path> <local path> <min bytes>
  if [ -f "$2" ] && [ "$(stat -c %s "$2")" -ge "$3" ]; then return 0; fi
  echo "== downloading $1"
  curl -fL --retry 5 --retry-delay 3 -C - -o "$2.part" "$SPACE_RAW/$1" && mv "$2.part" "$2"
}
fetch best_model.pth weights/best_model.pth 300000000      # SegFormer-B5, 323 MB
fetch final_best.pt  weights/final_best.pt  20000000       # pothole YOLO (if not in the zip)
for f in cd_past.png cd_present.png seg_input.png seg1_input.png seg2_input.png veg_past.png veg_present.png \
         change.json change_building.json change_veg.json segmentation.json segmentation1.json segmentation2.json; do
  fetch "demo/$f" "app/demo/$f" 1000                          # guided-tour demo assets
done
[ -f config.env ] || { cp config.env.example config.env; echo "== created config.env — edit SP_API_KEY and R2_* before starting"; }

# 4) SAM weights cached locally so fusion works even without internet later
echo "== caching SAM weights"
HF_HOME="$PWD/weights/hf" "$PY" - <<'PY'
import os
from transformers import SamModel, SamProcessor
mid = os.environ.get("SAM_MODEL_ID", "facebook/sam-vit-base")
SamProcessor.from_pretrained(mid); SamModel.from_pretrained(mid)
print("SAM cached:", mid)
PY

echo "== checking GPU + models"
"$PY" - <<'PY'
import torch
print("torch", torch.__version__, "| CUDA", torch.cuda.is_available(), "| GPUs", torch.cuda.device_count())
for i in range(torch.cuda.device_count()):
    f, t = torch.cuda.mem_get_info(i); print(f"  cuda:{i} {torch.cuda.get_device_name(i)} free {f/2**30:.1f}/{t/2**30:.1f} GB")
import ultralytics, transformers, rasterio, rio_tiler, rio_cogeo
print("ultralytics", ultralytics.__version__, "| transformers", transformers.__version__, "| rasterio", rasterio.__version__, "| GDAL", rasterio.__gdal_version__)
PY
echo "== setup done. Next: edit config.env, then  bash start.sh"
