import os
from huggingface_hub import HfApi
from dotenv import load_dotenv

load_dotenv()

hf_token = os.environ.get('HF_TOKEN')
repo_id = os.environ.get('HF_SPACE_NAME')

api = HfApi()

secrets = {
    "R2_ACCOUNT_ID": os.environ.get('R2_ACCOUNT_ID'),
    "R2_ACCESS_KEY_ID": os.environ.get('R2_ACCESS_KEY_ID'),
    "R2_SECRET_ACCESS_KEY": os.environ.get('R2_SECRET_ACCESS_KEY'),
    "R2_BUCKET_NAME": os.environ.get('R2_BUCKET_NAME'),
}

for key, value in secrets.items():
    if value:
        print(f"Adding secret {key}...")
        api.add_space_secret(repo_id=repo_id, key=key, value=value, token=hf_token)
        
print("Done adding secrets. Restarting space...")
api.restart_space(repo_id=repo_id, token=hf_token)
print("Space restarted successfully!")
