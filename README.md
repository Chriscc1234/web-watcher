# Web Watcher

Offline desktop app that watches webpages for you — marketplace listings, classifieds,
prices, weather, anything — reads each page with a local AI (no cloud, no accounts), and
alerts you when something new matches what you're looking for.

---

## ⬇️ Download & install (Windows)

### 👉 **[Click here to download the latest installer](https://github.com/Chriscc1234/web-watcher/releases/latest)**

That link opens the **Releases** page. Then:

1. Under **Assets**, click the file named **`WebWatcher-Setup-…-alpha.exe`** to download it.
2. Open the downloaded file and follow the prompts. No admin rights needed — it installs
   just for you.
3. The **first launch sets everything up automatically** (downloads the local AI engine and
   its models). This takes a while and needs internet **the first time only** — after that,
   Web Watcher runs fully offline. Leave it running; it'll open on its own when it's ready.
4. From then on, launch it any time from the **Web Watcher** desktop shortcut or Start menu.

> **Heads up:** it's a big first-time setup (the AI models are ~2 GB) and Windows may show a
> "Windows protected your PC" notice for an unsigned app — click **More info → Run anyway**.
> Updates after that are small and the app can update itself.

Found a problem? Click **🐛 Report a bug** inside the app — it saves a report to your Desktop
you can send back.

---

## For developers

Run from source (requires Python 3.13 + a local [Ollama](https://ollama.com)):

```
pip install -r requirements.txt
python -m web_watcher.main
```

Run the tests:

```
pytest tests/
```

Build the shipped installer (bundles a standalone Python runtime + Inno Setup package):

```
python build_runtime.py        # stage the self-contained runtime
python build_installer.py      # compile the Windows installer
python build_release.py        # code-only zip + notes for the auto-updater
```

## Configuration

Everything is editable in-app via the dashboard, or directly in `config.yaml` (stored under
`%LOCALAPPDATA%\WebWatcher`). See the technical spec for full schema documentation.
