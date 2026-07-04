# Clean-machine test in VirtualBox (buddy-handoff certainty)

Goal: prove `WebWatcher-Setup-<ver>.exe` works **start to finish on a fresh Windows** that has
nothing installed — no Python, no Ollama, and (importantly) testing whether it copes with a
machine that lacks `winget`. This is the real "will it work on my buddy's PC" test.

You do Parts A–D (the setup). When the VM is booted and Guest Additions are installed, ping
Claude — it will drive Part E (the actual install test) via computer-use.

---

## Part A — Install VirtualBox (~5 min)
1. Download **Oracle VirtualBox** for Windows hosts: https://www.virtualbox.org/wiki/Downloads
2. Also download the matching **Extension Pack** (same page) and install it (adds USB/clipboard niceties).
3. Install VirtualBox with defaults.

## Part B — Get a clean Windows image
**Recommended (most representative of a buddy's PC): a fresh Windows install from ISO.**
1. Download a Windows 11 (or 10) ISO:
   - Win 11: https://www.microsoft.com/software-download/windows11  → "Download disk image (ISO)"
   - or Win 10: https://www.microsoft.com/software-download/windows10
   - (Runs unactivated with a small watermark — fine for testing, fully functional.)

**Faster alternative (less clean): Microsoft's prebuilt dev VM.**
- Search "Microsoft developer virtual machines" → download the **VirtualBox** `.ova` (~20 GB),
  then *File → Import Appliance* in VirtualBox. Caveat: it ships WITH Visual Studio / dev tools
  (and possibly Python), so it's not a truly bare machine — good enough for a quick pass, but the
  ISO route is what proves the real from-nothing experience. If you use this, skip to Part D.

## Part C — Create the VM (ISO route)
In VirtualBox → **New**:
- **Type/Version:** Microsoft Windows, Windows 11 (or 10) 64-bit
- **RAM:** 12288 MB (12 GB) minimum — 16 GB if your host has 32 GB+. *(The AI models run on CPU
  inside the VM with no GPU, so they need real RAM to load; 14B wants ~10 GB.)*
- **CPUs:** 4 or more
- **Disk:** create a **new VDI, dynamically allocated, 80 GB** *(Windows ~25 GB + the app's Python
  bundle ~1.5 GB + Ollama + models ~15 GB + headroom).*
- After creating, open **Settings → System → Processor**: enable **PAE/NX**; **Settings → Display →**
  bump Video Memory to 128 MB.
- **Settings → Network:** leave as **NAT** (gives the VM internet — needed to download Ollama + models).
- Attach the ISO: **Settings → Storage → Empty (optical) → choose your Windows ISO**.
- Start the VM and install Windows:
  - Pick "I don't have a product key" → Windows 11/10 **Home** or **Pro**.
  - When possible choose a **local account** / skip the Microsoft account (closest to a normal setup).
  - Skip all the optional Cortana/OneDrive prompts.

## Part D — Guest Additions (so Claude can drive it + move the file in)
1. With the VM running, VirtualBox menu → **Devices → Insert Guest Additions CD image…**
2. In the VM, open the CD drive, run **VBoxWindowsAdditions.exe**, accept defaults, reboot the VM.
3. Enable convenience: VirtualBox menu → **Devices → Shared Clipboard → Bidirectional**, and
   **Drag and Drop → Bidirectional**.
4. Leave the VM window visible (don't minimize) so Claude's computer-use can see and click it.

## Part E — The install test (Claude drives this)
Two ways to get the installer into the VM — we'll do whichever you prefer:
- **Most authentic (recommended):** publish the GitHub release first, then **download the installer
  inside the VM from the release page** in Edge — exactly what your buddy will do.
- **Quick:** drag-and-drop `installer/Output/WebWatcher-Setup-<ver>.exe` onto the VM window.

Then Claude will:
1. Run the installer (expect: no admin prompt, per-user install).
2. Watch first-run provisioning — **this is the key test**: does Ollama install even without winget
   (the new direct-download fallback), then do the models pull?
3. Confirm the app window opens, a watch can be created, and it runs a sweep.
4. Test the Start-menu/desktop shortcuts, then uninstall (with the "delete my data?" prompt).

Expected slow spots (normal): the ~15 GB model download, and model inference on CPU (no GPU in the
VM) will be slow — we're proving it WORKS, not that it's fast.

---

### Quick checklist before pinging Claude
- [ ] VirtualBox + Extension Pack installed
- [ ] Windows installed in the VM and booted to desktop
- [ ] Guest Additions installed; clipboard + drag-drop set to Bidirectional
- [ ] VM has internet (open Edge, load a page)
- [ ] VM window left visible on your screen
