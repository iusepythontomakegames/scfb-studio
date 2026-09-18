# SCFB Studio

A runtime + Windows 11 UI for the **SCFB basic** DSL. Single-file Python, stdlib only (tkinter).

## Run (Windows 11)

    py scfb_studio.py

Headless self-test (36 checks):

    py scfb_studio.py --selftest

## v1.2.1 — input rework

- `input yes` — a **bare word placeholder is literal text**; quotes optional
  (`input yes` ≡ `input "yes"`). Expressions still work: `input var 1`.
- Match is **trimmed + case-insensitive** — typing `YES` or ` yes ` both match.
- **The block and `fi` are optional.** `input "hello"` with no block just waits
  for the line and reports the match in the LOG.
- **Bare `input`** (no placeholder) reads a line **into the selected variable**;
  placeholder input never touches your variables (no more silent RGB clobbering).
- While waiting: hint on the SCREEN, the input bar turns hot orange + auto-focuses,
  and the status bar says "Waiting for input".

## v1.2 — packages: spm + lib + ext

- **spm — the SCFB package manager** (from PowerShell / Command Prompt):

      py scfb_studio.py spm available        list registry packages
      py scfb_studio.py spm install all      install every library + extension
      py scfb_studio.py spm install math     install one (or several)
      py scfb_studio.py spm update all       refresh
      py scfb_studio.py spm remove NAME      uninstall
      py scfb_studio.py spm list             show installed

  Packages come from the `scfb-registry` repo and install to `%LOCALAPPDATA%\scfb`.

- **`lib <math>`** — load an installed SCFB basic library: its top-level defs/vars run
  on load (up to its first checkpoint), its functions are called with `call name()`.
- **`ext <mathx>`** — load an installed Python extension: registers functions callable
  inside expressions, e.g. `print(rnd(1, 100))`, `var 8 = spinby("cube", 2, 1, 0)`.
- Parenthesized expressions work: `print((5 + 2) * 2)`.
- Fixed: programs could fall through their end into lib/import-appended code
  (e.g. anim's infinite spin loop) — a guard statement now ends programs cleanly.

Libraries: math, color, shapes, calc, std, greet, demo, anim (.spin3d/.spin2d animation loops).
Extensions: mathx, timex, winx (beep/msgbox/speak/clip), filex, sysx, canvasx, textx, netx.

## Features

- One live SCREEN mixing `print` text and drawings (optional `x,y;` positions, `rot` updates in place); LOG pane separate
- 2D boxes = pixel-exact squares/rectangles; 3D objects = proper shaded rotating cubes
- Dark Windows 11 title bar (Mica attempt), Segoe UI Variable / Cascadia Code, per-monitor DPI
- Editor with line numbers, Run (F5 / Ctrl+Enter), Stop, Open, Save, Example, Help
- Full language: def, create, cvar/var, import <file.scb>, lib <>, ext <>, both print forms, ref,
  if + fi, input + fi (optional), function + call, jump, .checkpoints, draw/rot, operators, // comments
- No step limit — jump loops animate until Stop; live step counter in the status bar

(branches close with `fi` — no indentation rules.)
