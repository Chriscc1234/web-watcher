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
--skip-shortcuts    Skip creating the Desktop + Start Menu shortcuts.
                    (--skip-startup is kept as an alias.)
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

# The installer runs this under the bundled runtime with output piped to Inno Setup, where
# the default Windows encoding (cp1252) can't encode the ✓/→ status glyphs and would crash on
# the first print. Force UTF-8 on the streams (no-op if already UTF-8 or detached).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.resolve()
# User data (config.yaml + DB + logins + logs) lives in a per-user data root, kept fully
# separate from the installed code so updates never touch it. See web_watcher/paths.py.
from web_watcher import paths
CONFIG_PATH = paths.config_path()
DATA_DIR    = paths.data_dir()
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


# A model download that makes no progress for this long is treated as stalled (flaky
# network) and restarted — `ollama pull` resumes from the cached partial layers, so retrying
# is cheap and safe. Without this, a single network blip hangs the whole install forever.
_PULL_STALL_SECONDS = 150
_PULL_MAX_ATTEMPTS = 6


def _pull_once(model_name: str) -> str:
    """One `ollama pull` attempt with stall detection. Returns 'ok', 'stalled', or 'failed'."""
    log_fd, log_path = tempfile.mkstemp(suffix=".log", prefix=f"ww_pull_{model_name.replace(':', '_')}_")
    os.close(log_fd)
    log_path = Path(log_path)
    try:
        with log_path.open("w") as log_f:
            proc = subprocess.Popen(["ollama", "pull", model_name], stdout=log_f, stderr=log_f)

        last_pos = 0
        last_growth = time.time()
        while proc.poll() is None:
            time.sleep(1)
            try:
                content = log_path.read_text(errors="ignore")
                if len(content) > last_pos:
                    last_growth = time.time()
                    new_text = content[last_pos:]
                    last_pos = len(content)
                    lines = [l.strip() for l in new_text.splitlines() if l.strip()]
                    if lines:
                        print(f"\r    {lines[-1][:70]:<72}", end="", flush=True)
                elif time.time() - last_growth > _PULL_STALL_SECONDS:
                    print()
                    warn(f"download stalled ({_PULL_STALL_SECONDS}s no progress) — restarting it")
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return "stalled"
            except Exception:
                pass
        print()  # newline after progress
        return "ok" if proc.returncode == 0 else "failed"
    finally:
        try:
            log_path.unlink(missing_ok=True)
        except Exception:
            pass


def _pull_model(model_name: str) -> bool:
    """Pull a model, streaming progress, with automatic resume on stall/failure. Ollama caches
    partial layers, so each retry continues where the last left off. Returns True on success."""
    if _model_present(model_name):
        ok(f"{model_name} already present")
        return True
    for attempt in range(1, _PULL_MAX_ATTEMPTS + 1):
        if attempt > 1:
            info(f"Retrying {model_name} download (attempt {attempt}/{_PULL_MAX_ATTEMPTS}) — "
                 "resumes where it left off ...")
            time.sleep(2)
        result = _pull_once(model_name)
        if result == "ok" or _model_present(model_name):
            return True
        # 'stalled' or 'failed' — loop and resume.
    err(f"ollama pull {model_name!r} failed after {_PULL_MAX_ATTEMPTS} attempts")
    return False


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


_OLLAMA_SETUP_URL = "https://ollama.com/download/OllamaSetup.exe"


def _ollama_on_path_or_known() -> "Optional[Path]":
    """ollama.exe if it's on PATH or at its default Windows install location."""
    found = shutil.which("ollama")
    if found:
        return Path(found)
    candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return candidate if candidate.exists() else None


def _add_ollama_to_path() -> None:
    exe = _ollama_on_path_or_known()
    if exe and not shutil.which("ollama"):
        os.environ["PATH"] = str(exe.parent) + os.pathsep + os.environ.get("PATH", "")


def _install_ollama_via_winget() -> bool:
    """Try the winget path. Returns True only if ollama is present afterwards."""
    if not shutil.which("winget"):
        return False
    try:
        info("Installing Ollama via winget ...")
        _run(["winget", "install", "Ollama.Ollama", "--silent",
              "--accept-package-agreements", "--accept-source-agreements"],
             capture_output=False)
    except subprocess.CalledProcessError:
        warn("winget install failed — will try a direct download instead.")
        return False
    _add_ollama_to_path()
    return _ollama_on_path_or_known() is not None


def _install_ollama_via_download() -> bool:
    """Fallback that needs NO winget: download the official OllamaSetup.exe and run it
    silently (its installer is Inno-based, so /VERYSILENT works). This is what makes a fresh
    machine — and Windows Sandbox, which ships without winget — able to provision Ollama."""
    import tempfile
    try:
        import httpx
        dest = Path(tempfile.gettempdir()) / "OllamaSetup.exe"
        info(f"Downloading Ollama from {_OLLAMA_SETUP_URL} (~700 MB, one time) ...")
        with httpx.Client(timeout=None, follow_redirects=True) as c:
            with c.stream("GET", _OLLAMA_SETUP_URL) as r:
                r.raise_for_status()
                with dest.open("wb") as f:
                    for chunk in r.iter_bytes(1 << 16):
                        f.write(chunk)
        info("Running the Ollama installer silently ...")
        _run([str(dest), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
             capture_output=False)
    except Exception as exc:
        warn(f"Direct Ollama download/install failed: {exc}")
        return False
    _add_ollama_to_path()
    return _ollama_on_path_or_known() is not None


def check_ollama() -> None:
    step(3, TOTAL_STEPS, "Checking Ollama")
    if _ollama_on_path_or_known():
        _add_ollama_to_path()
        ok("ollama found")
        return

    if platform.system() != "Windows":
        err("Automatic Ollama install is only supported on Windows here.")
        info("Install Ollama from https://ollama.com and re-run install.")
        sys.exit(1)

    warn("ollama not found — installing it (winget, then direct download as a fallback)")
    if _install_ollama_via_winget() or _install_ollama_via_download():
        ok("Ollama installed successfully")
        return

    err("Could not install Ollama automatically.")
    info("Install it manually from https://ollama.com, then re-run setup.")
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

    # Preserve an existing config (e.g. a reinstall/upgrade, or data already migrated into the
    # per-user root). Overwriting would bury the user's real watches in a .bak. First-run has no
    # config yet, so this only ever fires on a re-provision — exactly when we must NOT clobber.
    if getattr(args, "keep_config", False) and CONFIG_PATH.exists():
        ok(f"Keeping existing config ({CONFIG_PATH})")
        return

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

        # MUST specify utf-8: the default watch name has an em-dash, and Windows' default
        # cp1252 encoding would write it as byte 0x97, which config.load() (utf-8) then can't
        # decode — the app would 500 on every /api/watches call on a fresh install.
        with CONFIG_PATH.open("w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ok(f"Config written to {CONFIG_PATH}")

    except Exception as exc:
        err(f"Failed to write config: {exc}")
        info("You can edit config.yaml manually after installation.")


def create_shortcuts(args) -> None:
    """Create the ONLY two ways to start Web Watcher: a Desktop shortcut and a Start Menu
    entry. Both launch it windowless (pythonw launcher.py) so there's no console flash. Also
    removes any legacy auto-start-at-login Task Scheduler entry from older installs — the app
    should start when the user asks, not on boot."""
    total_step = TOTAL_STEPS
    step(total_step, total_step, "Desktop + Start Menu shortcuts")

    if platform.system() != "Windows":
        info("Not on Windows — skipping shortcut creation")
        return

    # Clean up the old auto-start-at-login task if a previous install created one.
    subprocess.run(["schtasks", "/delete", "/tn", "WebWatcher", "/f"],
                   capture_output=True)

    pyw = Path(sys.executable).with_name("pythonw.exe")
    target = str(pyw if pyw.exists() else sys.executable)
    launcher = str(ROOT / "launcher.py")
    # Prefer the app's own icon; fall back to the python exe's icon if it's somehow missing.
    app_icon = ROOT / "web_watcher" / "dashboard" / "static" / "icon.ico"
    icon = str(app_icon if app_icon.exists() else (pyw if pyw.exists() else sys.executable))

    def q(s: str) -> str:            # PowerShell single-quote escaping
        return s.replace("'", "''")

    # Resolve Desktop + Start Menu via the shell (handles OneDrive-redirected Desktop) and
    # create both shortcuts in one PowerShell call.
    ps = (
        "$W = New-Object -COM WScript.Shell;"
        "$targets = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'));"
        "foreach ($d in $targets) {"
        "  $lnk = Join-Path $d 'Web Watcher.lnk';"
        "  $s = $W.CreateShortcut($lnk);"
        f"  $s.TargetPath = '{q(target)}';"
        f"  $s.Arguments = '\"{q(launcher)}\"';"
        f"  $s.WorkingDirectory = '{q(str(ROOT))}';"
        f"  $s.IconLocation = '{q(icon)},0';"
        "  $s.Description = 'Web Watcher';"
        "  $s.Save();"
        "}"
    )
    try:
        _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
             capture_output=True)
        ok("Desktop and Start Menu shortcuts created")
        info("Start Web Watcher from the Desktop icon or the Start Menu.")
    except subprocess.CalledProcessError as exc:
        warn("Could not create shortcuts automatically")
        info(f"  {exc.stderr.decode(errors='ignore').strip()[:200] if exc.stderr else ''}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Web Watcher Installer")
    p.add_argument("--skip-models",    action="store_true")
    p.add_argument("--skip-deps",      action="store_true",
                   help="Skip pip install (deps already present, e.g. bundled runtime)")
    p.add_argument("--keep-config",    action="store_true",
                   help="Do not overwrite an existing config.yaml (preserve user's watches)")
    p.add_argument("--skip-playwright", action="store_true")
    p.add_argument("--skip-shortcuts", "--skip-startup", dest="skip_shortcuts",
                   action="store_true")
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
    if not args.skip_shortcuts and platform.system() == "Windows":
        n += 1  # shortcut creation
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
    if not args.skip_deps:
        install_dependencies()
    check_ollama()
    tier = detect_gpu(args)
    tier = pull_models(tier, skip=args.skip_models)

    if not args.no_validate and tier.tier_name != "CPU":
        tier = validate_gpu(tier, no_validate=False)

    install_playwright(skip=args.skip_playwright)
    write_config(tier, args)

    if not args.skip_shortcuts:
        create_shortcuts(args)

    # Drop a completion marker so the launcher knows setup finished. If an install is
    # interrupted (e.g. a stalled download), this file is absent and the launcher re-runs
    # setup on next app start — so a broken install self-heals by just reopening the app.
    try:
        marker = paths.data_dir() / ".setup_complete"
        marker.write_text(f"{tier.tier_name} {tier.text_model}\n", encoding="utf-8")
    except Exception as exc:
        warn(f"could not write setup marker: {exc}")

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
