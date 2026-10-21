"""Push the survey videos to Cloudflare R2 so they are not in the git repo.

The drone clip is a 607 MB lossless remux of the original and GitHub rejects
anything over 100 MB, so the videos live in the same R2 bucket the rest of this
project's large data already uses. The detection JSON and route GeoJSON stay in
the repo - they are small, and keeping them there means the map, chart and
defect list still work on a deploy even if the media is unreachable.

  python upload_media_r2.py            # upload anything missing or changed
  python upload_media_r2.py --force    # re-upload regardless
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PREFIX = "nsv/"
FILES = ["L2_web.mp4", "drone_web.mp4"]


def env():
    out = dict(os.environ)
    for p in (ROOT / ".env", HERE / "config.env"):
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    out.setdefault(k.strip(), v.split("#", 1)[0].strip().strip('"').strip("'"))
    return out


def client(e):
    import boto3
    acc = (e.get("R2_ACCOUNT_ID", "").replace("https://", "")
           .replace(".r2.cloudflarestorage.com", "").replace("/", "").strip())
    if not (acc and e.get("R2_ACCESS_KEY_ID") and e.get("R2_SECRET_ACCESS_KEY")):
        raise SystemExit("R2 is not configured in .env")
    return boto3.client("s3", endpoint_url=f"https://{acc}.r2.cloudflarestorage.com",
                        aws_access_key_id=e["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=e["R2_SECRET_ACCESS_KEY"],
                        region_name="auto"), e["R2_BUCKET_NAME"]


def main(force=False):
    e = env()
    s3, bucket = client(e)
    for name in FILES:
        src = ROOT / "public" / "nsv" / name
        if not src.exists():
            print(f"{name}: not in public/nsv — skipped")
            continue
        key = PREFIX + name
        size = src.stat().st_size
        if not force:
            try:
                if s3.head_object(Bucket=bucket, Key=key)["ContentLength"] == size:
                    print(f"{name}: already on R2 at the same size — skipped")
                    continue
            except Exception:
                pass
        done = [0]

        def cb(n, _d=done, _t=size, _n=name):
            _d[0] += n
            pct = 100 * _d[0] / max(_t, 1)
            if int(pct) % 10 == 0:
                print(f"  {_n} {pct:5.1f}%", flush=True)

        print(f"{name}: uploading {size/1e6:.0f} MB -> s3://{bucket}/{key}", flush=True)
        s3.upload_file(str(src), bucket, key, Callback=cb,
                       ExtraArgs={"ContentType": "video/mp4",
                                  "CacheControl": "public, max-age=31536000, immutable"})
        print(f"{name}: done", flush=True)
    print("\nMedia keys:")
    for n in FILES:
        print(f"   {PREFIX}{n}")
    print("\nThese are private until the bucket has a public URL. Enable R2 public\n"
          "access (or attach a custom domain) in the Cloudflare dashboard, then set\n"
          "VITE_NSV_MEDIA_BASE to that base + '/nsv' in Vercel.")


if __name__ == "__main__":
    main("--force" in sys.argv)
