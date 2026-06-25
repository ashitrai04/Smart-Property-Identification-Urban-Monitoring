import os
from huggingface_hub import HfApi
from dotenv import load_dotenv

load_dotenv()

hf_token = os.environ.get('HF_TOKEN')
repo_id = os.environ.get('HF_SPACE_NAME')

if not hf_token or not repo_id:
    print("Error: Missing HF credentials in .env")
    exit(1)

api = HfApi()

print(f"Deploying backend to Hugging Face Space: {repo_id}")

try:
    # Upload server.py
    api.upload_file(
        path_or_fileobj="backend/server.py",
        path_in_repo="server.py",
        repo_id=repo_id,
        repo_type="space",
        token=hf_token
    )
    print("server.py uploaded")

    # Upload requirements.txt
    api.upload_file(
        path_or_fileobj="backend/requirements.txt",
        path_in_repo="requirements.txt",
        repo_id=repo_id,
        repo_type="space",
        token=hf_token
    )
    print("requirements.txt uploaded")

    # Upload Dockerfile
    api.upload_file(
        path_or_fileobj="backend/Dockerfile",
        path_in_repo="Dockerfile",
        repo_id=repo_id,
        repo_type="space",
        token=hf_token
    )
    print("Dockerfile uploaded")

    print(f"\nDeployment triggered! Check your space at: https://huggingface.co/spaces/{repo_id}")

except Exception as e:
    print(f"Failed to deploy: {e}")
