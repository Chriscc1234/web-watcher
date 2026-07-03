"""
Web Watcher Installer
=====================

Run via install.bat (Windows) or:

    python install.py [options]

Options
-------
--skip-models       Skip model pulls (useful for dev re-runs when models are
                    already present).
--skip-playwright   Skip Playwright Chromium install.
--skip-startup      Skip Windows startup registration prompt.
--tier NAME         Override tier (48GB+, 24GB, 16GB, 8-12GB, 6GB, CPU). Skips GPU probe.
--no-validate       Skip post-pull inference validation.
--yes               Accept all prompts non-interactively (credentials stay blank).
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.resolve()
CONFIG_PATH = ROOT / "config.yaml"
DATA_DIR    = ROOT / "data"
OLLAMA_URL  = "http://localhost:11434"

# ---------------------------------------------------------------------------
# ANSI colours (disabled on Windows without ANSI support)
# ---------------------------------------------------------------------------

_ANSI = sys.stdout.isatty() and platform.system() != "Windows" or (
    platform.system() == "Windows"
    and "TERM" in os.environ
)

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _ANSI else text

def ok(msg: str)   -> None: print(_c("32", f"  ✓ {msg}"))
def warn(msg: str) -> None: print(_c("33", f"  ! {msg}"))
def err(msg: str)  -> None: print(_c("31", f"  ✗ {msg}"))
def info(msg: str) -> None: print(f"    {msg}")
def step(n: int, total: int, title: str) -> None:
    print()
    print(_c("1;36", f"[{n}/{total}] {title}"))


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kwargs)


def _run_silently(cmd: list[str]) -> bool:
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _pull_model(model_name: str) -> bool:
    """
    Pull a model as a background process, streaming progress to stdout.
    Returns True on success.
    """
    log_fd, log_path = tempfile.mkstemp(suffix=".log", prefix=f"ww_pull_{model_name.replace(':', '_')}_")
    os.close(log_fd)
    log_path = Path(log_path)

    try:
        with log_path.open("w") as log_f:
            proc = subprocess.Popen(
                ["ollama", "pull", model_name],
                stdout=log_f,
                stderr=log_f,
            )

        last_pos = 0
        last_line = ""
        while proc.poll() is None:
            time.sleep(1)
            try:
                content = log_path.read_text(errors="ignore")
                if len(content) > last_pos:
                    new_text = content[last_pos:]
                    last_pos = len(content)
                    lines = [l.strip() for l in new_text.splitlines() if l.strip()]
                    if lines:
                        last_line = lines[-1]
                        display = last_line[:70]
                        print(f"\r    {display:<72}", end="", flush=True)
            except Exception:
                pass

        print()  # newline after progress

        if proc.returncode != 0:
            tail = log_path.read_text(errors="ignore")[-500:]
            err(f"ollama pull {model_name!r} exited {proc.returncode}")
            if tail.strip():
                print(f"    --- log tail ---\n{tail}\n    ----------------")
            return False
        return True

    finally:
        try:
            log_path.unlink(missing_ok=True)
        except Exception:
            pass


def _model_present(model_name: str) -> bool:
    """Return True if the model is already available in the local Ollama store."""
    try:
        import urllib.request, json
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5) as r:
            data = json.loads(r.read())
        names = [m["name"] for m in data.get("models", [])]
        base = model_name.split(":")[0]
        for name in names:
            if name == model_name or name.startswith(base + ":"):
                return True
    except Exception:
        pass
    return False


def _ollama_running() -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(f"{OLLAMA_URL}/", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------

def check_python() -> None:
    step(1, TOTAL_STEPS, "Checking Python environment")
    v = sys.version_info
    if v < (3, 10):
        err(f"Python 3.10+ required, found {v.major}.{v.minor}.{v.micro}")
        sys.exit(1)
    ok(f"Python {v.major}.{v.minor}.{v.micro}")


def install_dependencies() -> None:
    step(2, TOTAL_STEPS, "Installing Python dependencies")
    req = ROOT / "requirements.txt"
    if not req.exists():
        warn("requirements.txt not found — skipping pip install")
        return
    try:
        _run([sys.executable, "-m", "pip", "install", "-r", str(req), "--quiet"])
        ok("Dependencies installed")
    except subprocess.CalledProcessError:
        err("pip install failed — check your network connection or Python environment")
        sys.exit(1)


def check_ollama() -> None:
    step(3, TOTAL_STEPS, "Checking Ollama")
    if shutil.which("ollama"):
        ok("ollama found in PATH")
        return

    warn("ollama not found — attempting to install via winget")
    if platform.system() != "Windows":
        err("Automatic Ollama install is only supported on Windows here.")
        info("Install Ollama from https://ollama.com and re-run install.")
        sys.exit(1)

    if not shutil.which("winget"):
        err("winget not available — install Ollama manually from https://ollama.com")
        sys.exit(1)

    try:
        info("Running: winget install Ollama.Ollama ...")
        _run(
            ["winget", "install", "Ollama.Ollama",
             "--silent", "--accept-package-agreements",
             "--accept-source-agreements"],
            capture_output=False,
        )
        # Refresh PATH
        ollama_path = shutil.which("ollama")
        if not ollama_path:
            # Common install location on Windows
            candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
            if candidate.exists():
                os.environ["PATH"] = str(candidate.parent) + os.pathsep + os.environ["PATH"]

        if shutil.which("ollama"):
            ok("Ollama installed successfully")
        else:
            warn("Ollama installed but not yet in PATH. Open a new terminal after install completes.")
    except subprocess.CalledProcessError:
        err("winget install failed — install Ollama manually from https://ollama.com")
        sys.exit(1)


def detect_gpu(args) -> "TierInfo":
    from web_watcher.gpu_detect import TierInfo, _TIERS, _select_tier, _detect_vram_mb

    step(4, TOTAL_STEPS, "Detecting GPU / selecting model tier")

    if args.tier:
        match = {t.tier_name: t for t in _TIERS}
        if args.tier not in match:
            err(f"Unknown tier {args.tier!r}. Valid: {list(match)}")
            sys.exit(1)
        tier = match[args.tier]
        info(f"Tier override: {tier.tier_name}")
    else:
        vram_mb = _detect_vram_mb()
        tier    = _select_tier(vram_mb)
        if vram_mb:
            info(f"VRAM detected : {vram_mb:,} MB")
        else:
            info("No discrete GPU detected — using CPU-only tier")

    ok(f"Tier selected : {tier.tier_name}")
    info(f"  text model  : {tier.text_model}")
    info(f"  vision model: {tier.vision_model or 'disabled'}")
    return tier


def ensure_ollama_running() -> None:
    if _ollama_running():
        return
    info("Starting Ollama for model management ...")
    subprocess.Popen(
        ["ollama", "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        if _ollama_running():
            info("Ollama is ready")
            return
        time.sleep(1)
    warn("Ollama did not start within 20s — model pulls may fail")


def pull_models(tier: "TierInfo", skip: bool) -> "TierInfo":
    from web_watcher.gpu_detect import _TIERS

    step(5, TOTAL_STEPS, "Pulling required models")

    if skip:
        warn("--skip-models: skipping model pull")
        return tier

    ensure_ollama_running()

    # Dedup while preserving order: text, vision, then council (usually == text).
    models_to_pull: list[str] = []
    for m in (tier.text_model, tier.vision_model, tier.council_model):
        if m and m not in models_to_pull:
            models_to_pull.append(m)

    for model in models_to_pull:
        print(f"  {model} ...", end="", flush=True)
        if _model_present(model):
            ok("already installed")
            continue
        print()
        info(f"Downloading {model} — this may take several minutes ...")
        if not _pull_model(model):
            err(f"Failed to pull {model}")
            # Try falling back a tier
            current_idx = _TIERS.index(tier)
            if current_idx + 1 < len(_TIERS):
                fallback = _TIERS[current_idx + 1]
                warn(f"Falling back to tier {fallback.tier_name!r}")
                return pull_models(fallback, skip=False)
            else:
                err("No lower tier available — cannot continue")
                sys.exit(1)
        ok(f"{model} ready")

    return tier


def validate_gpu(tier: "TierInfo", no_validate: bool) -> "TierInfo":
    from web_watcher.gpu_detect import validate_inference, _TIERS

    if no_validate:
        warn("--no-validate: skipping GPU inference validation")
        return tier

    if tier.tier_name == "CPU":
        info("CPU tier — skipping GPU validation")
        return tier

    step(6, TOTAL_STEPS, "Validating GPU inference")
    info(f"Running test inference with {tier.text_model} (max 45s) ...")
    ensure_ollama_running()

    ok_flag = validate_inference(tier.text_model)
    if ok_flag:
        ok("GPU inference confirmed")
        return tier

    warn("Inference timed out or failed — GPU may not be active")
    current_idx = _TIERS.index(tier)
    if current_idx + 1 < len(_TIERS):
        fallback = _TIERS[current_idx + 1]
        warn(f"Falling back from {tier.tier_name!r} to {fallback.tier_name!r}")
        return fallback
    else:
        warn("Already at CPU tier — continuing with best effort")
        return tier


def install_playwright(skip: bool) -> None:
    step(7 if not SKIP_VALIDATE else 6, TOTAL_STEPS, "Installing Playwright browser")
    if skip:
        warn("--skip-playwright: skipping Playwright install")
        return
    try:
        _run([sys.executable, "-m", "playwright", "install", "chromium"])
        ok("Chromium installed")
    except subprocess.CalledProcessError:
        warn("playwright install failed — browser automation may not work")
        info("Run manually: python -m playwright install chromium")


def write_config(tier: "TierInfo", args) -> None:
    step(8 if not SKIP_VALIDATE else 7, TOTAL_STEPS, "Creating configuration")

    # Collect credentials unless --yes
    telegram_token = ""
    telegram_chat  = ""
    email_from     = ""
    email_password = ""
    email_smtp     = ""
    email_port     = 587

    if not args.yes:
        print()
        info("Telegram notifications (leave blank to configure later):")
        telegram_token = input("    Bot token: ").strip()
        if telegram_token:
            telegram_chat = input("    Chat ID  : ").strip()

        print()
        info("Email notifications (leave blank to configure later):")
        email_from = input("    From address      : ").strip()
        if email_from:
            email_password = input("    App password/token: ").strip()
            email_smtp     = input("    SMTP host (e.g. smtp.gmail.com): ").strip() or "smtp.gmail.com"
            port_str       = input("    SMTP port [587]   : ").strip()
            email_port     = int(port_str) if port_str.isdigit() else 587

    # Write YAML directly (avoids requiring pyyaml at this early stage,
    # though it should be installed by step 2)
    try:
        import yaml  # type: ignore

        config = {
            "notifications": {
                "telegram": {
                    "bot_token": telegram_token,
                    "chat_id":   telegram_chat,
                },
                "email": {
                    "from_address": email_from,
                    "password":     email_password,
                    "smtp_host":    email_smtp or "smtp.gmail.com",
                    "smtp_port":    email_port,
                } if email_from else {},
            },
            "models": {
                "text_model":    tier.text_model,
                "vision_model":  tier.vision_model or "",
                "council_model": tier.council_model or tier.text_model,
            },
            "watches": [
                {
                    "name":             "Example — NWS Seattle weather",
                    "enabled":          False,
                    "urls":             [
                        "https://forecast.weather.gov/MapClick.php"
                        "?CityName=Seattle&state=WA&site=SEW"
                        "&textField1=47.6062&textField2=-122.3321"
                    ],
                    "instruction": (
                        "Is there any severe weather warning, winter storm, "
                        "frost advisory, or flood watch in effect?"
                    ),
                    "interval_minutes": 60,
                    "perception":       "auto",
                    "notify":           {"telegram": bool(telegram_token), "email": bool(email_from)},
                    "click_path":       [],
                    "model_override":   None,
                }
            ],
        }

        if CONFIG_PATH.exists():
            backup = CONFIG_PATH.with_suffix(".yaml.bak")
            CONFIG_PATH.rename(backup)
            info(f"Existing config backed up to {backup.name}")

        with CONFIG_PATH.open("w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

        DATA_DIR.mkdir(exist_ok=True)
        ok(f"Config written to {CONFIG_PATH.relative_to(ROOT)}")

    except Exception as exc:
        err(f"Failed to write config: {exc}")
        info("You can edit config.yaml manually after installation.")


def register_startup(args) -> None:
    total_step = TOTAL_STEPS
    step(total_step, total_step, "Windows startup registration (optional)")

    if args.yes:
        info("--yes: skipping startup registration")
        return

    if platform.system() != "Windows":
        info("Not on Windows — skipping Task Scheduler registration")
        return

    answer = input("    Register Web Watcher to start on Windows login? [y/N]: ").strip().lower()
    if answer not in ("y", "yes"):
        info("Skipped — you can run start.bat manually whenever you want the app")
        return

    start_bat = ROOT / "start.bat"
    task_name = "WebWatcher"
    cmd = [
        "schtasks", "/create",
        "/tn",  task_name,
        "/tr",  f'"{start_bat}"',
        "/sc",  "ONLOGON",
        "/rl",  "HIGHEST",
        "/f",          # overwrite if exists
    ]
    try:
        _run(cmd, capture_output=True)
        ok(f"Task Scheduler entry created: {task_name}")
        info("Web Watcher will start automatically at next login.")
    except subprocess.CalledProcessError as exc:
        warn("schtasks failed — you can add start.bat to Startup manually")
        info(f"  Error: {exc.stderr.decode(errors='ignore').strip()[:200]}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Web Watcher Installer")
    p.add_argument("--skip-models",    action="store_true")
    p.add_argument("--skip-playwright", action="store_true")
    p.add_argument("--skip-startup",   action="store_true")
    p.add_argument("--tier",           default=None,
                   help="Override tier: '48GB+', '24GB', '16GB', '8-12GB', '6GB', 'CPU'")
    p.add_argument("--no-validate",    action="store_true")
    p.add_argument("--yes",            action="store_true",
                   help="Non-interactive: accept defaults, skip credential prompts")
    return p.parse_args()


def _compute_steps(args) -> int:
    """Total step count depends on flags."""
    n = 8  # base: python, deps, ollama, gpu, models, validate, playwright, config
    if args.no_validate or args.tier == "CPU":
        n -= 1  # skip validate step
    if not args.skip_startup and platform.system() == "Windows":
        n += 1  # startup registration
    return n


TOTAL_STEPS   = 9   # updated in main() before first step() call
SKIP_VALIDATE = False


def main() -> None:
    global TOTAL_STEPS, SKIP_VALIDATE

    args = parse_args()
    TOTAL_STEPS   = _compute_steps(args)
    SKIP_VALIDATE = args.no_validate

    print()
    print(_c("1;37", "=" * 50))
    print(_c("1;37", "  Web Watcher Installer"))
    print(_c("1;37", "=" * 50))

    check_python()
    install_dependencies()
    check_ollama()
    tier = detect_gpu(args)
    tier = pull_models(tier, skip=args.skip_models)

    if not args.no_validate and tier.tier_name != "CPU":
        tier = validate_gpu(tier, no_validate=False)

    install_playwright(skip=args.skip_playwright)
    write_config(tier, args)

    if not args.skip_startup:
        register_startup(args)

    print()
    print(_c("1;32", "=" * 50))
    print(_c("1;32", "  Installation complete!"))
    print(_c("1;32", "=" * 50))
    print()
    info(f"Tier        : {tier.tier_name}")
    info(f"Text model  : {tier.text_model}")
    info(f"Vision model: {tier.vision_model or 'disabled'}")
    print()
    print("  Double-click  start.bat  to launch Web Watcher.")
    print()


if __name__ == "__main__":
    main()
