"""Read-accuracy campaign for the grounded screen-read.

Measures the instrument `gen<Reading>(prompt, images=[observe screen()])`
against kernel-grade ground truth: every trial screen is *rendered* from
a known value (ImageMagick, deterministic seed), so scoring is exact
string comparison — no human labeling, no judge in the loop.

Two arms:
  present  render an 8-char join code (varied size, polarity, phrasing);
           a trial passes iff the committed read equals the truth exactly
           -> beta (true-read rate), Wilson 95 lower bound
  absent   render a plausible dialog with NO code (loading states,
           errors, settings — including 8-letter words as bait); a trial
           passes iff the read is exactly NONE; any invented code is a
           false read -> alpha, Wilson 95 upper bound (rule-of-three
           spirit at zero errors)

Every trial runs through the real language path (KIMIYA_SCREEN=none with
the trial PNG as fixture -> observe screen -> gen images= -> claude_cli
Opus). Output: datasheets/screen_read.json (installable via
`kimiya datasheet`, provenance embedded) + a per-trial JSONL audit log.

Usage: python3 tools/read_campaign.py [--present 60] [--absent 20]
       [--workers 4] [--out datasheets]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from kimiya.runtime import _wilson  # noqa: E402

# No 0/O or 1/I: the target product's join codes avoid glyph-ambiguous
# characters, and the instrument should be measured on the task it will
# actually perform.
CHARSET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

PROBE = '''agent V:
    backend = "claude_cli"
    model   = "claude-opus-4-8"

schema Reading:
    text: text

shot := observe screen()
check shot.exists
r := gen<Reading>("Read the 8-character join code shown on this screen, exactly as displayed. If no join code is visible, respond with exactly NONE.", images=[shot]) by V
check len(r.text) > 0
commit(r.text)
'''

PRESENT_TEMPLATES = [
    "Join code:\\n{code}",
    "Share this code with viewers:\\n\\n{code}",
    "Session live — code {code}",
    "{code}\\n\\nWaiting for participants…",
]
STYLES = [  # (pointsize, background, fill)
    (36, "white", "black"),
    (28, "white", "black"),
    (22, "#1e1e28", "#e8e6e0"),
    (18, "#f4f1ea", "#333333"),
]
ABSENT_SCREENS = [
    "Connecting…\\nplease wait",
    "Waiting for the host to start the session",
    "Error: connection lost\\nRetry?",
    "Settings\\nAudio: on\\nVideo: off",       # 8-letter word as bait
    "Download complete\\nDocument.pdf saved",  # 8-letter word as bait
]


def render(text: str, style, path: Path) -> None:
    ps, bg, fg = style
    subprocess.run(
        ["convert", "-size", "480x320", "-background", bg, "-fill", fg,
         "-pointsize", str(ps), "-gravity", "center",
         f"label:{text}", str(path)],
        check=True, capture_output=True, timeout=30)


def run_trial(trial: dict) -> dict:
    d = Path(trial["dir"])
    (d / "readprobe.kim").write_text(PROBE)
    env = dict(__import__("os").environ,
               PYTHONPATH=str(REPO), KIMIYA_SCREEN="none",
               KIMIYA_SCREEN_FIXTURE=trial["png"], KIMIYA_TIMEOUT="150")
    env.pop("KIMIYA_MOCK", None)  # this is a live measurement
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, "-m", "kimiya", "run", "readprobe.kim"],
        cwd=d, env=env, capture_output=True, text=True, timeout=400)
    latency = round(time.time() - t0, 1)
    cert = d / ".kimiya" / "certificate.json"
    read = None
    if cert.exists():
        c = json.loads(cert.read_text())
        if c.get("status") == "COMMITTED":
            read = str(c.get("value", "")).strip()
    ok = (read == trial["truth"]) if trial["truth"] != "NONE" \
        else (read is not None and read.strip().upper() == "NONE")
    return dict(trial, read=read, ok=ok, latency_s=latency,
                rc=proc.returncode)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--present", type=int, default=60)
    ap.add_argument("--absent", type=int, default=20)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=str(REPO / "datasheets"))
    args = ap.parse_args()

    if not shutil.which("convert") or not shutil.which("claude"):
        sys.exit("needs ImageMagick `convert` and the `claude` CLI")

    rng = random.Random(args.seed)
    root = Path(tempfile.mkdtemp(prefix="readcamp-"))
    trials = []
    for i in range(args.present):
        code = "".join(rng.choice(CHARSET) for _ in range(8))
        tmpl = PRESENT_TEMPLATES[i % len(PRESENT_TEMPLATES)]
        style = STYLES[i % len(STYLES)]
        d = root / f"p{i:03d}"
        d.mkdir(parents=True)
        png = d / "screen.png"
        render(tmpl.format(code=code), style, png)
        trials.append({"id": f"p{i:03d}", "arm": "present", "truth": code,
                       "style": list(style), "dir": str(d),
                       "png": str(png)})
    for i in range(args.absent):
        text = ABSENT_SCREENS[i % len(ABSENT_SCREENS)]
        style = STYLES[i % len(STYLES)]
        d = root / f"a{i:03d}"
        d.mkdir(parents=True)
        png = d / "screen.png"
        render(text, style, png)
        trials.append({"id": f"a{i:03d}", "arm": "absent", "truth": "NONE",
                       "style": list(style), "dir": str(d),
                       "png": str(png)})

    print(f"{len(trials)} trials ({args.present} present, "
          f"{args.absent} absent), {args.workers} workers, seed "
          f"{args.seed}", flush=True)
    results = []
    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        for res in pool.map(run_trial, trials):
            results.append(res)
            mark = "✓" if res["ok"] else "✗"
            print(f"  {mark} {res['id']} {res['arm']:7s} "
                  f"truth={res['truth']:9s} read={res['read']!r} "
                  f"({res['latency_s']}s)", flush=True)

    present = [r for r in results if r["arm"] == "present"]
    absent = [r for r in results if r["arm"] == "absent"]
    tp = sum(r["ok"] for r in present)
    fr = sum(not r["ok"] for r in absent)   # false reads on absent screens
    beta_lo = _wilson(tp, len(present))[0]
    alpha_hi = _wilson(fr, len(absent))[1]
    lat = sorted(r["latency_s"] for r in results)
    p50 = lat[len(lat) // 2]
    p90 = lat[int(len(lat) * 0.9)]

    out = Path(args.out)
    out.mkdir(exist_ok=True)
    source = (f"screen-read campaign, {len(present)} present + "
              f"{len(absent)} absent rendered trials, claude_cli "
              f"claude-opus-4-8, seed {args.seed}, {time.strftime('%Y-%m-%d')}")
    sheet = {"read:k_read": {
        "alpha_hi": round(alpha_hi, 4), "beta_lo": round(beta_lo, 4),
        "n_true": len(present), "n_false": len(absent),
        "source": source,
        "detail": {
            "true_reads": tp, "misses": len(present) - tp,
            "false_reads_on_absent": fr,
            "beta_hat": round(tp / len(present), 4),
            "latency_s_p50": p50, "latency_s_p90": p90,
            "ground_truth": "rendered screens; exact string match",
        },
    }}
    (out / "screen_read.json").write_text(json.dumps(sheet, indent=2))
    with (out / "screen_read_campaign.jsonl").open("w") as f:
        for r in results:
            f.write(json.dumps({k: r[k] for k in
                                ("id", "arm", "truth", "read", "ok",
                                 "latency_s", "style")}) + "\n")
    print(f"\nbeta: {tp}/{len(present)} correct -> beta_lo {beta_lo:.4f}")
    print(f"alpha: {fr}/{len(absent)} false reads -> alpha_hi "
          f"{alpha_hi:.4f}")
    print(f"latency p50 {p50}s p90 {p90}s")
    print(f"wrote {out}/screen_read.json + screen_read_campaign.jsonl")
    print(f"trial artifacts: {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
