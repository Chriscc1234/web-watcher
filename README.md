# Web Watcher

Offline desktop tool that monitors webpages on a schedule, interprets page
content with a local LLM (Ollama), and sends Telegram/email alerts on a match.

## Quick start

```
pip install -r requirements.txt
python -m web_watcher.main
```

## Running tests

```
pytest tests/
```

## Configuration

Edit `config.yaml`. The dashboard (Step 7) provides a UI for the same file.

See the technical spec for full schema documentation.
