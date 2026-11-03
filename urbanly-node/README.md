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

Copy this folder to **both** laptops, then double-click:

```
START URBANLY.bat
```

That is the whole setup. There is nothing to type and nothing to edit.

On the first run it works out this machine's settings by itself and writes
them to `config.env`:

- **Name** — taken from the computer name.
- **Data folder** — it checks the usual places (`D:\GEO_SERVE`,
  `Downloads\GEO_SERVE`, and so on) and picks the one that actually holds
  survey files.
- **The other laptop** — it asks Tailscale which of your machines are online
  and checks each for a running node. If the other laptop is up, it is filled
  in automatically. If it is not, this one runs standalone and picks it up
  next time.

`config.env` is a plain text file you can edit afterwards if any of that was
guessed wrong.

### If the other laptop is not found

It only needs to be running `START URBANLY.bat` too. Start it there, then run
this one again — or press `status`, which looks again. Nothing breaks in the
meantime; a node with no peer simply serves its own data.

### About the key

`UN_API_KEY` is left empty, which is right for a private Tailscale network:
the node port is not exposed to the internet. If you ever publish that port,
set the **same** long random string in `config.env` on both laptops.

## Making a laptop a full server

The node package on its own only shares files with a peer. To let a laptop
*run the models* and *serve the webapp and QGIS*, run once:

```
SETUP SERVER.bat
```

It fetches the project (68 MB), builds a virtual environment, installs PyTorch
and the rest, sizes the model settings to that machine's graphics card, and
pulls the model weights from the other laptop. Expect a long wait: PyTorch
alone is about 2.5 GB.

It reads the card's memory before PyTorch exists and sets the job sizes
accordingly, because the alternative is discovering the card is too small
part-way through a long run:

| Graphics memory | Batch | Window |
|---|---|---|
| 4 GB | 2 | 1024 px |
| 6 GB | 4 | 1536 px |
| 8 GB and above | 8 | 1536 px |

Install somewhere else with `SETUP SERVER.bat E:\Urbanly`.

After it finishes, `START URBANLY.bat` brings up the model server and the data
server on that machine too.

### Either laptop can be the one serving

Once both are set up they are interchangeable. Whichever is running publishes
its own public address, and the webapp and the QGIS plugin look that address up
rather than remembering it - so you can start one, stop it, start the other,
and both clients follow without being reconfigured.

If both run at once, the one that started most recently is the one clients
reach. Stop the one you are not using if that matters.

---

## What the commands do

| Command | What happens |
|---|---|
| `START URBANLY.bat` | Sync, start model server, data server and node service, then report |
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
