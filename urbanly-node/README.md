Written for: you, setting this up on both laptops.

# Urbanly node

One command that brings a laptop up as an Urbanly machine: it syncs with the
other laptop, starts the model server and the data server, and tells you what
is online.

**Both laptops run this same folder.** Neither is the master. Whichever is
running serves; on sync the newer file wins. That is why the command is
identical on both and why nothing here is called "client".

---

## Setting it up

On **each** laptop:

1. Copy this folder to the machine.
2. Copy `config.env.example` to `config.env`.
3. Edit three lines:

```ini
UN_NODE_NAME=laptop-a                     # a name you will recognise
UN_DATA_DIR=D:\GEO_SERVE                  # where the survey data is HERE
UN_PEERS=http://100.74.15.17:8790         # the OTHER laptop's node address
UN_API_KEY=pick-one-long-random-string    # must be IDENTICAL on both
```

Then:

```bat
start.bat
```

That is all. First run installs three small packages if they are missing.

### The peer address

Use the other laptop's **Tailscale** address. A `192.168.x.x` address only
works while both machines are on the same router, and they frequently are not —
one has been on a phone hotspot. The Tailscale address follows the laptop onto
any network and is not reachable from the internet.

`start.bat status` prints the address to give the other machine.

### The API key

Must match on both laptops. Without it the two will see each other but refuse
to exchange files, which is reported plainly rather than failing silently. Any
long random string will do.

---

## What the commands do

| Command | What happens |
|---|---|
| `start.bat` | Sync, start model server, data server and node service, then report |
| `start.bat status` | What this machine holds, and which peers answer |
| `start.bat sync` | Pull anything newer from a peer; start nothing |
| `start.bat sync --dry-run` | List what would be pulled, transfer nothing |
| `start.bat --no-sync` | Start the servers without syncing first |
| `start.bat --no-tunnel` | Start without opening a public address |

`status` looks like this:

```
  THIS NODE   laptop-a
  address     172.20.10.5:8790
  GPU         NVIDIA GeForce RTX 3050 6GB Laptop GPU · 5.4 of 6.44 GB free
  data        D:\GEO_SERVE
  holds       17 layers (80.7 MB) · 4 weights (1.5 GB) · 4 rasters (44.4 GB)

  PEERS
  ONLINE   laptop-b at http://100.74.15.17:8790  (18 ms)
           NVIDIA GeForce RTX 4060 · 7.1 GB free
           17 layers · 4 weights · 4 rasters
```

An unreachable peer says so and gives the reason, rather than hanging.

---

## What syncs, and what does not

| | Synced | Why |
|---|---|---|
| Model weights | **yes** | 1.5 GB, and they change whenever a model is retrained |
| Vector layers | **yes** | ~80 MB, and they change constantly |
| Orthomosaics and satellite rasters | **no** | 44 GB, and both laptops already hold them |

Rasters are **listed** in the status so you can see at a glance whether the two
machines have the same imagery, but they are never transferred. Copying 42.7 GB
between laptops over a tunnel takes longer than re-flying the survey. If one
machine is genuinely missing imagery, copy it once with a USB disk.

Anything over 3 GB is excluded from transfer by the same rule
(`UN_MAX_SYNC_BYTES`).

### How a file is chosen

Two files are compared by **fingerprint** first — size plus both ends of the
file — and by modification time second. Fingerprint first matters because
copying between machines rarely preserves timestamps, so two identical files
often look different by mtime alone. Only a file that differs **and** is newer
on the peer is pulled.

### How a file is written

Downloaded to a `.part` name, verified for length, and only then moved into
place. The file it replaces is kept as `.prev`. A half-written checkpoint that
happens to have the right name is far worse than no checkpoint, especially
while the model server is reading it.

---

## What gets started

| Service | Port | What it is |
|---|---|---|
| Model server | 8800 | Building delineation, segmentation, pothole and height routes |
| Data server | 8777 | Layers, imagery tiles, public address, proxy to the model server |
| Node service | 8790 | This package's own peer API: status and file transfer |

Each runs in its own console window, so one crashing does not take the others
down. A service already running on its port is left alone rather than started
twice.

The data server opens the Cloudflare tunnel and publishes its address, exactly
as before — the web application and the QGIS plugin keep finding it with no
configuration.

---

## The node API

Two endpoints, deliberately.

```
GET /node/status            what I am, what I hold, how fresh it is
GET /node/file?kind=&name=  one layer or one weight
```

`/node/status` answers without a key but gives only identity and GPU; the file
list and the file endpoint both require the key. `name` is matched against the
manifest rather than joined onto a path, so a request cannot walk out of the
data directory however it is spelled.

---

## If something is wrong

**"config.env is missing"** — copy `config.env.example` and fill in the three
values.

**Peer shows `[no key — manifest hidden]`** — the two `UN_API_KEY` values do
not match. They are compared exactly, including spaces.

**Peer is offline but the laptop is on** — check it is running `start.bat`, and
that `UN_PEERS` points at port 8790, not 8777 or 8800.

**`holds 0 rasters`** — `UN_DATA_DIR` is pointing somewhere without imagery.
Check the path in `config.env` against where the `.tif` files actually are.

**Sync pulled something you did not want** — the previous version is beside it
as `.prev`; rename it back.
