"""Deploy the pothole detector into the SegFormer Space (uses HF_TOKEN from .env).

The detector is a FastAPI router (ml/pothole-space/pothole.py) mounted by that
Space's server.py under /pothole/*. It lives there rather than in its own Space
because new free-tier Docker Spaces now need HF PRO; the existing one doesn't.

Usage: python deploy_pothole_space.py     (re-run after changing pothole.py or the weights)
"""
import os
import sys

from dotenv import load_dotenv
from huggingface_hub import HfApi

ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(ROOT, ".env"))

token = os.environ.get("HF_TOKEN")
repo_id = os.environ.get("HF_SEGFORMER_SPACE", "asashit/smart-property-segformer")
if not token:
    sys.exit("Missing HF_TOKEN in .env")

api = HfApi(token=token)
api.upload_folder(
    folder_path=os.path.join(ROOT, "ml", "pothole-space"),
    repo_id=repo_id,
    repo_type="space",
    allow_patterns=["pothole.py", "final_best.pt"],
    commit_message="Update pothole detector",
)
print(f"Deployed to https://huggingface.co/spaces/{repo_id}")
print(f"API: https://{repo_id.replace('/', '-').lower()}.hf.space/pothole/detect")
