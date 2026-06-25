import os
import boto3
import glob
from pathlib import Path
from dotenv import load_dotenv

# Load credentials from .env
load_dotenv()

account_id = os.environ.get('R2_ACCOUNT_ID')
access_key = os.environ.get('R2_ACCESS_KEY_ID')
secret_key = os.environ.get('R2_SECRET_ACCESS_KEY')
bucket_name = os.environ.get('R2_BUCKET_NAME')

if not all([account_id, access_key, secret_key, bucket_name]):
    print("Error: Missing R2 credentials in .env")
    exit(1)

# Clean account_id if user pasted full URL
account_id = account_id.replace("https://", "").replace(".r2.cloudflarestorage.com", "").replace("/", "").strip()

# Initialize S3 client for Cloudflare R2
s3 = boto3.client(
    service_name='s3',
    endpoint_url=f'https://{account_id}.r2.cloudflarestorage.com',
    aws_access_key_id=access_key,
    aws_secret_access_key=secret_key,
    region_name='auto',
)

data_dir = os.path.join("datasets", "cleaned_features")
files = glob.glob(f"{data_dir}/*")

print(f"Uploading {len(files)} files to R2 bucket '{bucket_name}'...")

for file_path in files:
    file_name = os.path.basename(file_path)
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    print(f"Uploading {file_name} ({file_size_mb:.1f} MB)... ", end="", flush=True)
    try:
        s3.upload_file(file_path, bucket_name, file_name)
        print("Done!")
    except Exception as e:
        print(f"Failed: {e}")

print("Upload complete!")
