/**
 * Serve the survey videos out of the private R2 bucket.
 *
 * The clips are far too large to commit (the drone one is a 636 MB lossless
 * remux and GitHub caps files at 100 MB), so they live in R2. Making the
 * bucket public would work but needs a dashboard change and exposes every
 * object in it; instead this signs a short-lived URL server-side and redirects
 * the browser to it. The credentials never reach the client.
 *
 * The redirect matters for more than tidiness: the browser then talks to R2
 * directly, so HTTP range requests - which is how video seeking works - go
 * straight to storage instead of being proxied through a serverless function
 * with a response size limit.
 *
 * SigV4 is hand-rolled on node:crypto rather than pulling in the AWS SDK,
 * which would add several megabytes to a function that builds one URL.
 */
import crypto from "node:crypto";

const ALLOWED = new Set(["L2_web.mp4", "drone_web.mp4"]);
const PREFIX = "nsv/";
const EXPIRES = 60 * 60 * 6; // seconds; long enough to watch, short enough to not be a public link

const sha256hex = (s) => crypto.createHash("sha256").update(s).digest("hex");
const hmac = (key, s) => crypto.createHmac("sha256", key).update(s).digest();
const enc = (s) =>
    encodeURIComponent(s).replace(/[!'()*]/g, (c) => "%" + c.charCodeAt(0).toString(16).toUpperCase());

function presign({ account, accessKey, secretKey, bucket, key, expires }) {
    const host = `${account}.r2.cloudflarestorage.com`;
    const region = "auto";
    const service = "s3";
    const now = new Date();
    const amzDate = now.toISOString().replace(/[:-]|\.\d{3}/g, "");
    const dateStamp = amzDate.slice(0, 8);
    const scope = `${dateStamp}/${region}/${service}/aws4_request`;

    // path-style addressing: R2 supports it and it avoids bucket-name-in-host
    const canonicalUri = "/" + bucket + "/" + key.split("/").map(enc).join("/");

    const q = new Map([
        ["X-Amz-Algorithm", "AWS4-HMAC-SHA256"],
        ["X-Amz-Credential", `${accessKey}/${scope}`],
        ["X-Amz-Date", amzDate],
        ["X-Amz-Expires", String(expires)],
        ["X-Amz-SignedHeaders", "host"],
    ]);
    const canonicalQuery = [...q.entries()]
        .map(([k, v]) => [enc(k), enc(v)])
        .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
        .map(([k, v]) => `${k}=${v}`)
        .join("&");

    const canonicalRequest = [
        "GET",
        canonicalUri,
        canonicalQuery,
        `host:${host}\n`,
        "host",
        "UNSIGNED-PAYLOAD",
    ].join("\n");

    const stringToSign = [
        "AWS4-HMAC-SHA256",
        amzDate,
        scope,
        sha256hex(canonicalRequest),
    ].join("\n");

    const kDate = hmac("AWS4" + secretKey, dateStamp);
    const kRegion = hmac(kDate, region);
    const kService = hmac(kRegion, service);
    const kSigning = hmac(kService, "aws4_request");
    const signature = crypto.createHmac("sha256", kSigning).update(stringToSign).digest("hex");

    return `https://${host}${canonicalUri}?${canonicalQuery}&X-Amz-Signature=${signature}`;
}

export default function handler(req, res) {
    const name = String(req.query?.name || "");
    // allowlist, not sanitisation: this function can sign a URL for any object
    // in the bucket, and the bucket holds other people's survey data
    if (!ALLOWED.has(name)) {
        res.status(404).json({ error: "unknown media", allowed: [...ALLOWED] });
        return;
    }

    const account = (process.env.R2_ACCOUNT_ID || "")
        .replace("https://", "")
        .replace(".r2.cloudflarestorage.com", "")
        .replace(/\//g, "")
        .trim();
    const accessKey = process.env.R2_ACCESS_KEY_ID;
    const secretKey = process.env.R2_SECRET_ACCESS_KEY;
    const bucket = process.env.R2_BUCKET_NAME;

    if (!(account && accessKey && secretKey && bucket)) {
        res.status(503).json({
            error: "R2 is not configured for this deployment",
            needs: ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"],
        });
        return;
    }

    const url = presign({
        account, accessKey, secretKey, bucket,
        key: PREFIX + name, expires: EXPIRES,
    });

    // cache the redirect for less than it is valid for, so a cached 302 can
    // never outlive the signature it points at
    res.setHeader("Cache-Control", `public, max-age=${EXPIRES - 600}`);
    res.redirect(302, url);
}
