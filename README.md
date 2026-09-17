# SCFB Studio

A runtime + Windows 11 UI for the **SCFB basic** DSL. Single-file Python, stdlib only (tkinter).

## Run (Windows 11)

    py scfb_studio.py

Headless self-test (23 checks):

    py scfb_studio.py --selftest

## What you get

- Dark Windows 11 title bar (Mica attempt), Segoe UI Variable / Cascadia Code, per-monitor DPI awareness
- Code editor with line numbers, Run (F5 / Ctrl+Enter), Stop, Open, Save, Example, Help
- **Stage**: 2D boxes (`draw type="box"`, rotatable via `X`) and 3D objects (`draw type="object"`, isometric, painter-sorted shaded faces, full X,Y,Z rotation)
- Colors from variables: `rgb; var 1, var 2, var 3` (or variables 1-3 as RGB by default)
- Runtime console with an input bar for `input`, line-numbered errors
- Full language: `def`, `create` (top-to-bottom naming, one-use per entity), `cvar`/`var`,
  `import <file.scb>`, both `print` forms, `ref`, `if` + `fi`, `input` + `fi`,
  `function NAME()` + `call`, `jump`, `.checkpoints`, `draw`/`rot`, operators
  `== = + - * / > < >= <= && ||`, `//` comments
- Help window inside the app with the full language reference; a demo program is preloaded

## Example

    .start
    cvar 1
    cvar 2
    cvar 3
    var 1 = 100
    var 2 = 149
    var 3 = 237

    draw type="box" {
        width,height; 140,90
        rgb; var 1, var 2, var 3
    }

    create mybox {
        def mybox type="box"
    }

    draw type="object" {
        width,height,depth; 120,120,120
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
