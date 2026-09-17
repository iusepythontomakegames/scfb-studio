# SCFB Studio

A runtime + Windows 11 UI for the **SCFB basic** DSL. Single-file Python, stdlib only (tkinter).

## Run (Windows 11)

    py scfb_studio.py

Headless self-test:

    py scfb_studio.py --selftest

## v1.1 — one live SCREEN, real runtime behavior

- **SCREEN**: `print` text and drawn objects share ONE live canvas — text renders like a terminal, boxes/objects draw at real pixel positions (`x,y;` in draw blocks, optional). It's a runtime, not a museum.
- **LOG**: runtime chatter (start/stop/import/errors) is kept separate in its own pane.
- **No more sad triangles**: the 3D cube face corners were not in ring order (every face drew as a self-crossing bowtie = 2 triangles). Faces are now proper rings — a real shaded cube.
- 2D boxes render as pixel-exact squares/rectangles (rotated polygon only when you actually `rot` them).
- `rot` updates objects **in place** at their position.
- No step limit — `jump` loops animate forever until you press Stop; the status bar shows a live step counter.
- Fixed: `import`ed files had un-rebased branch indices (could loop); now their functions/checkpoints/branches work correctly.

## Features

- Dark Windows 11 title bar (Mica attempt), Segoe UI Variable / Cascadia Code, per-monitor DPI awareness
- Code editor with line numbers, Run (F5 / Ctrl+Enter), Stop, Open, Save, Example, Help
- Full language: `def`, `create` (top-to-bottom naming, one-use per entity), `cvar`/`var`,
  `import <file.scb>`, both `print` forms, `ref`, `if` + `fi`, `input` + `fi`,
  `function NAME()` + `call`, `jump`, `.checkpoints`, `draw`/`rot` (with optional `x,y` positions),
  operators `== = + - * / > < >= <= && ||`, `//` comments
- Colors from variables: `rgb; var 1, var 2, var 3` (or variables 1-3 as RGB by default)
- Input bar under the screen; typed lines show on the screen
- Help window inside the app with the full language reference; a demo program is preloaded

## Example

    .start
    cvar 1
    cvar 2
    cvar 3
    var 1 = 100
    var 2 = 149
    var 3 = 237

    print("hello from SCFB basic")
    print(5 + 2)

    draw type="box" {
        width,height; 140,140
        x,y; 300,220
        rgb; var 1, var 2, var 3
    }

    create mybox {
        def mybox type="box"
    }

    draw type="object" {
        width,height,depth; 120,120,120
        x,y; 580,220
        rgb; var 1, var 2, var 3
    }

    create cube {
        def cube type="object"
    }

    ref cube
    rot type="object" {
        X,Y,Z; 25, 40, 0
    }

    if var 1 == 100 {
        print("variable 1 is 100")
    fi

    function greet() {
        print("greetings from greet()")
    fi

    call greet()
    print(mybox.width)
    jump .end
    print("this line is skipped")
    .end
    print("done")

(branches close with `fi` — no indentation rules.)
