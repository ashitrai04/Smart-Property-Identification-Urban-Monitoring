"""Seed the Ongole run from the released Deventer checkpoint, in one step.

WHY THIS EXISTS

acpv_ongole512_train.yaml is written as a FINE-TUNE: BASE_LR 2e-5 instead of
the published 6e-5, TOTAL_ITERS 40k instead of 160k, a short warmup. Its own
comments say so - "Fine-tuning from a converged checkpoint, not training from
scratch… this starts from a model that already does the task and only has to
move domain."

Nothing in that config ever pointed at such a checkpoint. The only weights it
loads are MODEL.BACKBONE.PRETRAINED, the VMamba ImageNet trunk, and job
116724 said as much in its first ten lines:

    training INFO: Start training from scratch.

So the run would have trained randomly-initialised heads on a quarter of the
iterations at a third of the learning rate. Not a crash - something worse: 28
hours of GPU time producing a weak model that reads as "ACPV-Net does not
work on Ongole" when the truth is that it was never given its head start.

The repo ships the converged model, in the output directory of the run that
produced it:

    outputs/deventer512_vmamba-s_m_vh-ldm_kl4_b8/model_iter_0160000.pth

WHAT THIS DOES

Writes a seed checkpoint into the Ongole output directory holding the model
weights and nothing else, with `iteration` set to 0, and points that
directory's `last_checkpoint` at it. The chain's first slice then resumes
from the Deventer weights at iteration 0 and trains the full 40k.

WHY NOT JUST --resume FROM THE DEVENTER FILE

Because checkpointer.load() restores the iteration counter along with the
weights, and 160000 is past TOTAL_ITERS 40000: training would "finish"
immediately, having done nothing, and the log would look almost normal. The
optimizer state would come across too - Adam moments and a poly LR schedule
belonging to a different dataset at a different learning rate.

So the seed is deliberately stripped:

    model       kept      - the point of the exercise
    model_ema   kept      - USE_EMA is on; a converged EMA is a fair start
    optimizer   DROPPED   - a fresh one, for this LR and this data
    iteration   set to 0  - so the full 40k runs and the LR schedule starts
                            at the top. Set explicitly rather than omitted,
                            because whether train_iter.py reads it with
                            .get() or [] is not something to leave to chance.

This touches no code and no config: it only writes two files into the Ongole
output directory, which is empty apart from a config.yml and a log.

    python acpv_init_from_deventer.py                 # check, then write
    python acpv_init_from_deventer.py --dry-run       # check only
"""
import argparse
import os
import sys

DEFAULT_REPO = f"/scratch/{os.environ.get('USER', 'unknown')}/ACPV-Net"


def find_yaml_value(path, key):
    """Pull one top-level-ish scalar out of a YAML file without needing yaml.

    Used for NUM_CLASSES, which decides whether the classification heads can
    be carried over or will be re-initialised. A plain text scan is enough and
    works even if the saved config.yml is not valid YAML to this parser.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if s.startswith(key):
                    rest = s.split(":", 1)
                    if len(rest) == 2:
                        return rest[1].strip()
    except OSError:
        return None
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--src", default="outputs/deventer512_vmamba-s_m_vh-ldm_kl4_b8/model_iter_0160000.pth",
                    help="the converged checkpoint to seed from, relative to --repo")
    ap.add_argument("--out", default="outputs/ongole512_vmamba-s_m_vh-ldm_kl4_b8",
                    help="this run's OUTPUT_DIR, relative to --repo")
    ap.add_argument("--name", default="init_from_deventer.pth")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing seed, and proceed past a "
                         "NUM_CLASSES mismatch")
    a = ap.parse_args()

    import torch

    repo = os.path.abspath(a.repo)
    src = os.path.join(repo, a.src)
    out = os.path.join(repo, a.out)
    dst = os.path.join(out, a.name)
    pointer = os.path.join(out, "last_checkpoint")

    def die(msg):
        print(f"\nREFUSING: {msg}")
        print("Nothing was written.")
        sys.exit(1)

    print(f"repo : {repo}")
    print(f"src  : {src}")
    print(f"out  : {out}\n")

    if not os.path.isdir(repo):
        die(f"{repo} is not a directory")
    if not os.path.isfile(src):
        die(f"no checkpoint at {src} - list outputs/ and pass --src")
    if not os.path.isdir(out):
        die(f"{out} does not exist. Run the training once so it is created, "
            f"or check OUTPUT_DIR in the config.")

    # ---- is the run already under way? then leave it completely alone ----
    # A real checkpoint in here means slices have already trained. Seeding on
    # top of that would throw away their progress and silently restart.
    existing = sorted(f for f in os.listdir(out)
                      if f.endswith(".pth") and f != a.name)
    if existing:
        die(f"{out} already holds trained checkpoints ({', '.join(existing[:4])}"
            f"{'…' if len(existing) > 4 else ''}). Seeding now would discard "
            f"that progress. If you really want to start over, move them aside "
            f"first.")
    if os.path.isfile(dst) and not a.force:
        die(f"{dst} already exists. Pass --force to replace it.")

    # ---- do the heads line up? ----
    src_cfg = os.path.join(os.path.dirname(src), "config.yml")
    n_src = find_yaml_value(src_cfg, "NUM_CLASSES") if os.path.isfile(src_cfg) else None
    n_out = find_yaml_value(os.path.join(out, "config.yml"), "NUM_CLASSES")
    print(f"NUM_CLASSES  source: {n_src}   this run: {n_out}")
    if n_src and n_out and n_src != n_out:
        msg = (f"the seed was trained with NUM_CLASSES {n_src} and this run "
               f"uses {n_out}, so the class-dependent heads cannot carry over "
               f"and will be re-initialised")
        if not a.force:
            die(msg + ". That is often still the right thing to do - pass "
                      "--force to accept it.")
        print(f"  NOTE: {msg} - proceeding because --force was given")
    print()

    # ---- read it ----
    print("loading the seed checkpoint (1.3 GB, this takes a moment)…")
    ck = torch.load(src, map_location="cpu")
    if not isinstance(ck, dict):
        die(f"{src} is a {type(ck).__name__}, not a checkpoint dict")
    print(f"  top-level keys: {', '.join(sorted(ck.keys()))}")

    sd = ck.get("model")
    if sd is None:
        # Some checkpoints are a bare state_dict. Accept that, but only if it
        # actually looks like one.
        if all(hasattr(v, "shape") for v in ck.values()):
            sd = ck
            print("  no 'model' key - treating the whole file as a state_dict")
        else:
            die("no 'model' key and the file is not a bare state_dict")
    n_tensors = sum(1 for v in sd.values() if hasattr(v, "shape"))
    print(f"  model: {len(sd)} entries, {n_tensors} tensors")
    if n_tensors < 100:
        die(f"only {n_tensors} tensors - that is not a full ACPV-Net")
    if "iteration" in ck:
        print(f"  iteration in the source: {ck['iteration']}  "
              f"(dropped; the seed says 0)")

    seed = {"model": sd, "iteration": 0}
    if "model_ema" in ck:
        seed["model_ema"] = ck["model_ema"]
        print(f"  model_ema: carried over ({len(ck['model_ema'])} entries)")
    else:
        print("  model_ema: absent in the source; EMA will start from the "
              "loaded weights")
    print(f"  optimizer: {'dropped' if 'optimizer' in ck else 'absent'}")

    if a.dry_run:
        print("\n--dry-run: everything above checks out, nothing written.")
        print(f"Would write {dst}")
        print(f"Would point {pointer} at it")
        return

    # ---- write ----
    tmp = dst + ".partial"
    torch.save(seed, tmp)
    os.replace(tmp, dst)          # replace, so a half-written file is never
                                  # left where the trainer would read it
    size_mb = os.path.getsize(dst) / 1e6
    print(f"\nwrote {dst}  ({size_mb:.0f} MB)")

    # The pointer is written the way the checkpointer writes it - a path
    # relative to the repo, since OUTPUT_DIR in the config is relative and
    # the trainer runs with the repo as its working directory.
    rel = os.path.join(a.out, a.name)
    if not rel.startswith("."):
        rel = "./" + rel
    with open(pointer, "w", encoding="utf-8") as fh:
        fh.write(rel)
    print(f"wrote {pointer} -> {rel}")

    print("\n================= READY =================")
    print("The next slice will print:")
    print(f"    resuming from: {a.name}")
    print("and train from iteration 0 with the Deventer weights.")
    print("If it says 'starting from scratch' instead, stop it - the pointer "
          "was not picked up.")


if __name__ == "__main__":
    main()
