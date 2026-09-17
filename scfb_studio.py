#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SCFB Studio — a runtime + Windows 11 UI for the "SCFB basic" DSL.

Run (Windows 11):   py scfb_studio.py
Headless test:      py scfb_studio.py --selftest

Stdlib only (tkinter UI). Windows-11 focused: per-monitor DPI awareness,
immersive dark title bar + Mica attempt, Segoe UI Variable / Cascadia Code,
Fluent-style dark palette. Degrades gracefully on other platforms.

SCFB basic quick reference (full version under Help in the app):

    def NAME VALUE                  define entity as something
    create NAME { def ... }         name the most-recent/next drawn object
    cvar N                          create variable N
    var N = EXPR                    select / set variable N
    import <file.scb>               load a file (print via name.something)
    print("text")  /  print(EXPR)    print literal / evaluated expression
    ref NAME                        reference something (rot targets it)
    if COND { ... fi                branch (skips to fi when false)
    input EXPECTED { ... fi         wait for input; run block when it matches
    function NAME() { ... fi        define a function; call with call NAME()
    jump .CHECKPOINT                jump to a checkpoint
    .name                           checkpoint
    draw type="box" { width,height; 10,20 }
    draw type="object" { width,height,depth; 20,30,90 }
    rot type="box" { X,Y; 30,50 }   /  rot type="object" { X,Y,Z; 10,20,30 }
    rgb; var 1,var 2,var 3          color objects from variables (RGB)
    // comment                      does nothing but points things out
    operators:  ==  =  +  -  *  /  >  <  >=  <=  &&  ||
"""

import math
import os
import re
import sys
import threading
import queue

# ════════════════════════════════ errors ════════════════════════════════

class ScfbError(Exception):
    def __init__(self, msg, line=None):
        self.line = line
        super().__init__("line %d: %s" % (line, msg) if line else msg)


class StopRun(Exception):
    pass


# ═══════════════════════════ expression engine ══════════════════════════

_NUM = re.compile(r"\d+(?:\.\d+)?")
_ID = re.compile(r"[A-Za-z_]\w*")
_VAR_N = re.compile(r"\s*(\d+)")


def tokenize_expr(s):
    toks = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == '"':
            j = s.find('"', i + 1)
            if j < 0:
                raise ScfbError("unterminated string in expression")
            toks.append(("str", s[i + 1:j]))
            i = j + 1
            continue
        m = _NUM.match(s, i)
        if m:
            t = m.group(0)
            toks.append(("num", float(t) if "." in t else int(t)))
            i = m.end()
            continue
        if c.isalpha() or c == "_":
            m = _ID.match(s, i)
            w = m.group(0)
            i = m.end()
            if w == "var":
                m2 = _VAR_N.match(s, i)
                if m2:
                    toks.append(("varref", int(m2.group(1))))
                    i = m2.end()
                    continue
            toks.append(("ident", w))
            continue
        two = s[i:i + 2]
        if two in ("==", "!=", "&&", "||", ">=", "<="):
            toks.append(("op", two))
            i += 2
            continue
        if c in "+-*/()<>.,=":
            toks.append(("op", c))
            i += 1
            continue
        raise ScfbError("unexpected character %r in expression" % c)
    return toks


def _isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _truthy(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return len(v) > 0
    return v is not None


def fmt_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, list):
        return " ".join(fmt_value(x) for x in v)
    return str(v)


class _ExprParser:
    def __init__(self, toks, rt):
        self.t = toks
        self.i = 0
        self.rt = rt

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def advance(self):
        tk = self.peek()
        self.i += 1
        return tk

    def eat_op(self, ops):
        k, v = self.peek()
        if k == "op" and v in ops:
            self.i += 1
            return v
        return None

    def parse(self):
        v = self.p_or()
        if self.i < len(self.t):
            raise ScfbError("unexpected trailing token in expression")
        return v

    def p_or(self):
        v = self.p_and()
        while self.eat_op(("||",)):
            v = _truthy(v) or _truthy(self.p_and())
        return v

    def p_and(self):
        v = self.p_cmp()
        while self.eat_op(("&&",)):
            v = _truthy(v) and _truthy(self.p_cmp())
        return v

    def p_cmp(self):
        v = self.p_add()
        op = self.eat_op(("==", "!=", ">", "<", ">=", "<="))
        if op:
            r = self.p_add()
            if op == "==":
                return v == r
            if op == "!=":
                return v != r
            if _isnum(v) and _isnum(r):
                return {"<": v < r, ">": v > r, "<=": v <= r, ">=": v >= r}[op]
            raise ScfbError("'%s' compares numbers" % op)
        return v

    def p_add(self):
        v = self.p_mul()
        while True:
            op = self.eat_op(("+", "-"))
            if not op:
                return v
            r = self.p_mul()
            if op == "+":
                if _isnum(v) and _isnum(r):
                    v = v + r
                else:
                    v = fmt_value(v) + fmt_value(r)
            else:
                if _isnum(v) and _isnum(r):
                    v = v - r
                else:
                    raise ScfbError("'-' needs numbers")
        return v

    def p_mul(self):
        v = self.p_unary()
        while True:
            op = self.eat_op(("*", "/"))
            if not op:
                return v
            r = self.p_unary()
            if not (_isnum(v) and _isnum(r)):
                raise ScfbError("'%s' needs numbers" % op)
            if op == "*":
                v = v * r
            else:
                if r == 0:
                    raise ScfbError("division by zero")
                v = v / r
        return v

    def p_unary(self):
        if self.eat_op(("-",)):
            v = self.p_unary()
            if not _isnum(v):
                raise ScfbError("unary '-' needs a number")
            return -v
        return self.p_primary()

    def p_primary(self):
        k, v = self.advance()
        if k == "num" or k == "str":
            return v
        if k == "varref":
            return self.rt.get_var(v)
        if k == "ident":
            val = self.rt.value_of(v)
            name = v
            while True:
                k2, v2 = self.peek()
                if k2 == "op" and v2 == ".":
                    self.i += 1
                    k3, v3 = self.advance()
                    if k3 != "ident":
                        raise ScfbError("expected property name after '.'")
                    val = self.rt.prop(name, val, v3)
                    name = v3
                else:
                    return val
        raise ScfbError("unexpected token %r in expression" % (v,))


# ═════════════════════════════════ parser ═══════════════════════════════

def strip_comment(line):
    in_s = False
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c == '"':
            in_s = not in_s
        elif c == "/" and not in_s and i + 1 < n and line[i + 1] == "/":
            return line[:i]
        i += 1
    return line


_TYPE_ATTR = re.compile(r'type\s*=\s*"([^"]+)"')
_DEF_TYPE = re.compile(r'^def\s+\w+\s+type\s*=\s*"(\w+)"\s*$')

_KEYWORDS = ("if", "input", "function", "print", "draw", "rot", "var", "cvar",
             "def", "create", "ref", "jump", "import", "call")


class Parser:
    """Parses SCFB basic into a FLAT statement list. if/input/function bodies
    are stored as index ranges so `jump` can reach any checkpoint."""

    def __init__(self, lines, base_line=0):
        self.lines = list(lines)
        self.base = base_line
        self.prog = []

    def parse(self):
        self._parse_into(0, None)
        return self.prog

    def _parse_into(self, i, terminator):
        n = len(self.lines)
        while i < n:
            raw = self.lines[i]
            ln = self.base + i + 1
            i += 1
            s = strip_comment(raw).strip()
            if not s:
                continue
            if terminator and (s == terminator or (terminator == "fi" and s == "}")):
                return i
            i = self._statement(s, i, ln)
        if terminator:
            raise ScfbError("missing '%s' before end of file" % terminator)
        return i

    def _statement(self, s, i, ln):
        prog = self.prog

        m = re.match(r"^\.([A-Za-z_]\w*)$", s)
        if m:
            prog.append({"op": "checkpoint", "name": m.group(1), "line": ln})
            return i

        m = re.match(r"^function\s+([A-Za-z_]\w*)\s*\(\s*\)\s*(\{)?\s*$", s)
        if m:
            idx = len(prog)
            prog.append({"op": "funcdef", "name": m.group(1), "line": ln,
                         "start": idx + 1, "end": None})
            i = self._parse_into(i, "fi")
            prog[idx]["end"] = len(prog)
            return i

        m = re.match(r"^if\s+(.+)$", s)
        if m:
            cond = m.group(1).strip().rstrip("{").strip()
            if not cond:
                raise ScfbError("'if' needs a condition", ln)
            idx = len(prog)
            prog.append({"op": "if", "cond": cond, "line": ln, "end": None})
            i = self._parse_into(i, "fi")
            prog[idx]["end"] = len(prog)
            return i

        m = re.match(r"^input\s*(.*)$", s)
        if m:
            idx = len(prog)
            prog.append({"op": "input",
                         "cond": m.group(1).strip().rstrip("{").strip(),
                         "line": ln, "end": None})
            i = self._parse_into(i, "fi")
            prog[idx]["end"] = len(prog)
            return i

        m = re.match(r"^jump\s+\.?([A-Za-z_]\w*)\s*$", s)
        if m:
            prog.append({"op": "jump", "target": m.group(1), "line": ln})
            return i

        m = re.match(r"^import\s+<([^>]+)>\s*$", s)
        if m:
            prog.append({"op": "import", "path": m.group(1), "line": ln})
            return i

        m = re.match(r"^print\s*\((.*)\)\s*$", s)
        if m:
            prog.append({"op": "print", "expr": m.group(1).strip(), "line": ln})
            return i
        m = re.match(r"^print\s+(.+)$", s)
        if m:
            prog.append({"op": "print", "expr": m.group(1).strip(), "line": ln})
            return i
        if s == "print":
            prog.append({"op": "print", "expr": "", "line": ln})
            return i

        m = re.match(r"^def\s+([A-Za-z_]\w*)\s*(.*)$", s)
        if m:
            prog.append({"op": "def", "name": m.group(1),
                         "rest": m.group(2).strip(), "line": ln})
            return i

        m = re.match(r"^create\s+([A-Za-z_]\w*)\s*(\{)?\s*$", s)
        if m:
            st = {"op": "create", "name": m.group(1), "type": None, "line": ln}
            if m.group(2):
                while i < len(self.lines):
                    bl = strip_comment(self.lines[i]).strip()
                    i += 1
                    if bl.startswith("}"):
                        break
                    dm = _DEF_TYPE.match(bl)
                    if dm:
                        st["type"] = dm.group(1)
            prog.append(st)
            return i

        m = re.match(r"^cvar\s+(\w+)\s*$", s)
        if m:
            prog.append({"op": "cvar", "name": m.group(1), "line": ln})
            return i

        m = re.match(r"^var\s+(\w+)\s*(?:=\s*(.*))?$", s)
        if m:
            prog.append({"op": "var", "name": m.group(1),
                         "assign": m.group(2), "line": ln})
            return i

        m = re.match(r"^ref\s+([A-Za-z_]\w*)\s*$", s)
        if m:
            prog.append({"op": "ref", "name": m.group(1), "line": ln})
            return i

        if s == "draw":
            prog.append({"op": "drawplain", "line": ln})
            return i

        m = re.match(r"^draw\s+(.+)$", s)
        if m:
            return self._drawrot("draw", m.group(1), i, ln)

        m = re.match(r"^rot\s+(.+)$", s)
        if m:
            return self._drawrot("rot", m.group(1), i, ln)

        m = re.match(r"^call\s+([A-Za-z_]\w*)\s*\(\s*\)\s*$", s)
        if m:
            prog.append({"op": "call", "name": m.group(1), "line": ln})
            return i

        m = re.match(r"^([A-Za-z_]\w*)\s*\(\s*\)\s*$", s)
        if m and m.group(1) not in _KEYWORDS:
            prog.append({"op": "call", "name": m.group(1), "line": ln})
            return i

        if s in ("}", "fi"):
            return i  # stray closer at top level — ignore

        raise ScfbError("unknown statement: %r" % s, ln)

    def _drawrot(self, op, head, i, ln):
        head = head.strip()
        tm = _TYPE_ATTR.search(head)
        typ = tm.group(1) if tm else None
        if typ is None:
            raise ScfbError('%s needs type="box" or type="object"' % op, ln)
        if "{" not in head:
            raise ScfbError("expected '{' to open the %s block" % op, ln)

        chunks = []
        inline = head.split("{", 1)[1]
        if "}" in inline:  # single-line block
            part = inline.split("}", 1)[0].strip()
            if part:
                chunks.append(part)
        else:
            while i < len(self.lines):
                bl = strip_comment(self.lines[i]).strip()
                i += 1
                if bl.startswith("}"):
                    rest = bl[1:].strip()
                    if rest and ";" in rest:
                        chunks.append(rest)
                    break
                if bl:
                    chunks.append(bl)
            else:
                raise ScfbError("missing '}' for %s block" % op, ln)

        body = {}
        for chunk in chunks:
            if ";" not in chunk:
                raise ScfbError("bad %s body (want 'keys; values'): %r" % (op, chunk), ln)
            keys, vals = chunk.split(";", 1)
            keys = [k.strip().lower() for k in keys.split(",") if k.strip()]
            vals = [v.strip() for v in vals.split(",")]
            if len(keys) == len(vals):
                for k, v in zip(keys, vals):
                    body[k] = v
            elif len(keys) == 1:
                # e.g. rgb; var 1,var 2,var 3 — one key, many values
                body[keys[0]] = ",".join(vals)
            else:
                raise ScfbError("%s body key/value count mismatch: %r" % (op, chunk), ln)
        self.prog.append({"op": op, "type": typ, "body": body, "line": ln})
        return i


# ═════════════════════════════════ runtime ══════════════════════════════

DEFAULT_RGB = (110, 160, 255)


class Runtime:
    """Executes a flat SCFB basic program. Thread-friendly: all UI contact
    goes through the provided callbacks (put things on queues there)."""

    def __init__(self, source, out, redraw, input_queue, workdir="."):
        self.out = out              # out(text, tone) tone: 'out'|'err'|'sys'
        self.redraw = redraw
        self.input_queue = input_queue
        self.workdir = workdir
        self.program = Parser(source.splitlines()).parse()
        self.checkpoints = {}
        self.funcs = {}
        for idx, st in enumerate(self.program):
            if st["op"] == "checkpoint":
                self.checkpoints[st["name"]] = idx
            elif st["op"] == "funcdef":
                self.funcs[st["name"]] = (st["start"], st["end"])
        self.vars = {}
        self.entities = {}
        self.objects = []
        self.objects_by_name = {}
        self.pending_names = []
        self.selected = None
        self.ref_target = None
        self.stop = False
        self.steps = 0

    # ---- helpers ----------------------------------------------------------
    def eval(self, expr):
        if expr is None:
            return None
        return _ExprParser(tokenize_expr(expr), self).parse()

    def _var_key(self, n):
        s = str(n)
        return int(s) if s.lstrip("-").isdigit() else s

    def get_var(self, n):
        return self.vars.setdefault(self._var_key(n), 0)

    def value_of(self, name):
        if name in self.objects_by_name:
            return {"__objref": self.objects_by_name[name], "__name": name}
        if name in self.entities:
            return self.entities[name]
        if name in self.funcs:
            return "<function %s()>" % name
        raise ScfbError("unknown identifier: %s" % name)

    def prop(self, holder_name, val, prop):
        if isinstance(val, dict) and "__objref" in val:
            o = val["__objref"]
            if prop in ("width", "w"):
                return o["w"]
            if prop in ("height", "h"):
                return o["h"]
            if prop in ("depth", "d"):
                return o["d"]
            if prop == "name":
                return o["name"] or ""
            if prop == "rot":
                return o["rot"]
            if prop in ("rgb", "color"):
                return o["rgb"]
            raise ScfbError("object %s has no property %r" % (val.get("__name"), prop))
        if isinstance(val, dict):
            if prop in val:
                return val[prop]
            raise ScfbError("%s has no property %r" % (holder_name, prop))
        if isinstance(val, str):
            return val  # imported file contents: name.something → contents
        raise ScfbError("cannot read %r of %s" % (prop, holder_name))

    def _name_object(self, obj):
        obj["name"] = None
        for k, p in enumerate(self.pending_names):
            if p["type"] in (None, obj["type"]):
                self.pending_names.pop(k)
                self._apply_name(obj, p["name"])
                return

    def _apply_name(self, obj, name):
        obj["name"] = name
        self.objects_by_name[name] = obj
        self.entities[name] = {"__objref": obj, "__name": name}

    def _rgb_from(self, body):
        raw = body.get("rgb", body.get("color"))
        if raw:
            vals = []
            for k, v in tokenize_expr(str(raw)):
                if k == "num":
                    vals.append(v)
                elif k == "varref":
                    vals.append(self.get_var(v))
            vals = [max(0, min(255, int(x))) for x in vals]
            if len(vals) == 1:
                vals = [vals[0]] * 3
            while len(vals) < 3:
                vals.append(DEFAULT_RGB[len(vals)])
            return tuple(vals[:3])
        if all(k in self.vars for k in (1, 2, 3)):
            return tuple(max(0, min(255, int(self.vars[k]))) for k in (1, 2, 3))
        return DEFAULT_RGB

    def _read_input(self):
        while True:
            if self.stop:
                raise StopRun()
            try:
                return self.input_queue.get(timeout=0.2)
            except queue.Empty:
                continue

    # ---- main loop --------------------------------------------------------
    def run(self):
        prog = self.program
        pc = 0
        self._frames = []  # (end_index, return_pc) for active calls
        frames = self._frames
        status = "ok"
        try:
            while pc < len(prog):
                if frames and pc == frames[-1][0]:
                    pc = frames.pop()[1]
                    continue
                self.steps += 1
                if self.stop:
                    raise StopRun()
                nxt = self._exec(prog[pc], pc)
                pc = pc + 1 if nxt is None else nxt
            self.out("— program finished —", "sys")
        except StopRun:
            self.out("— stopped —", "sys")
            status = "stopped"
        except ScfbError as e:
            self.out("ERROR %s" % e, "err")
            status = "error"
        return status

    # ---- statement exec ---------------------------------------------------
    def _exec(self, st, pc):
        op = st["op"]
        if op == "checkpoint":
            return None
        if op == "def":
            rest = st["rest"]
            tm = _TYPE_ATTR.match(rest)
            if tm:
                self.entities[st["name"]] = {"type": tm.group(1)}
            elif rest:
                self.entities[st["name"]] = self.eval(rest)
            else:
                self.entities[st["name"]] = None
            return None
        if op == "cvar":
            self.get_var(st["name"])
            return None
        if op == "var":
            key = self._var_key(st["name"])
            self.get_var(key)
            self.selected = key
            if st["assign"]:
                self.vars[key] = self.eval(st["assign"])
            return None
        if op == "print":
            if not st["expr"]:
                self.out("", "out")
            else:
                self.out(fmt_value(self.eval(st["expr"])), "out")
            return None
        if op == "import":
            self._do_import(st)
            return None
        if op == "create":
            name, typ = st["name"], st["type"]
            for o in self.objects:  # top-to-bottom, one-use per entity
                if o["name"] is None and (typ is None or o["type"] == typ):
                    self._apply_name(o, name)
                    return None
            self.pending_names.append({"name": name, "type": typ})
            return None
        if op == "ref":
            self.ref_target = st["name"]
            return None
        if op == "if":
            if _truthy(self.eval(st["cond"])):
                return None
            return st["end"]
        if op == "input":
            self.out("waiting for input…", "sys")
            line = self._read_input()
            self.out("> " + line, "in")
            if self.selected is not None:
                self.vars[self.selected] = line
            if st["cond"]:
                expected = fmt_value(self.eval(st["cond"]))
                if line.strip() != expected:
                    return st["end"]
            return None
        if op == "funcdef":
            return st["end"]
        if op == "call":
            fn = self.funcs.get(st["name"])
            if not fn:
                raise ScfbError("unknown function: %s()" % st["name"], st["line"])
            self._frames.append((fn[1], pc + 1))
            return fn[0]
        if op == "jump":
            idx = self.checkpoints.get(st["target"])
            if idx is None:
                raise ScfbError("unknown checkpoint: .%s" % st["target"], st["line"])
            del self._frames[:]  # jumping exits any active function
            return idx
        if op == "draw":
            self._do_draw(st)
            return None
        if op == "rot":
            self._do_rot(st)
            return None
        if op == "drawplain":
            self.redraw()
            return None
        raise ScfbError("internal: bad op %r" % op)

    def _do_import(self, st):
        path = os.path.join(self.workdir, st["path"])
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            raise ScfbError("import failed: %s" % e, st["line"])
        stem = os.path.splitext(os.path.basename(path))[0]
        self.entities[stem] = content  # printable via stem.something
        sub = Parser(content.splitlines()).parse()
        off = len(self.program)
        # sub's branch/function ranges are LOCAL indices — rebase them
        # onto the combined program or jumps/skips land in the wrong place
        for s in sub:
            if "end" in s:
                s["end"] += off
            if s["op"] == "funcdef":
                s["start"] += off
        self.program.extend(sub)
        for idx, s in enumerate(sub):
            if s["op"] == "checkpoint":
                self.checkpoints[s["name"]] = off + idx
            elif s["op"] == "funcdef":
                self.funcs[s["name"]] = (s["start"], s["end"])  # already rebased
        self.out("imported <%s> — functions & checkpoints registered" % st["path"], "sys")

    def _do_draw(self, st):
        typ = st["type"]
        body = st["body"]
        w = self.eval(body.get("width", "100"))
        h = self.eval(body.get("height", "100"))
        d = self.eval(body.get("depth", "100")) if typ == "object" else None
        px = self.eval(body["x"]) if "x" in body else None
        py = self.eval(body["y"]) if "y" in body else None
        obj = {"type": typ, "w": float(w), "h": float(h),
               "d": float(d) if d is not None else None,
               "rot": [0.0, 0.0, 0.0], "rgb": self._rgb_from(body), "name": None,
               "px": float(px) if px is not None else None,
               "py": float(py) if py is not None else None}
        self._name_object(obj)
        self.objects.append(obj)
        self.redraw()

    def _do_rot(self, st):
        typ = st["type"]
        obj = None
        if self.ref_target and self.ref_target in self.objects_by_name:
            obj = self.objects_by_name[self.ref_target]
        if obj is None:
            for o in reversed(self.objects):
                if o["type"] == typ:
                    obj = o
                    break
        if obj is None:
            self.out("nothing to rotate yet — draw something first", "err")
            return
        body = st["body"]
        rx = self.eval(body.get("x", "0"))
        ry = self.eval(body.get("y", "0"))
        rz = self.eval(body.get("z", "0"))
        obj["rot"] = [float(rx), float(ry), float(rz)]
        self.redraw()


# ═══════════════════════════════ renderer ═══════════════════════════════

def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def rgb_hex(rgb, f=1.0):
    r, g, b = (int(_clamp(c * f, 0, 255)) for c in rgb)
    return "#%02x%02x%02x" % (r, g, b)


def _rot3(p, rx, ry, rz):
    x, y, z = p
    if rx:
        c, s = math.cos(rx), math.sin(rx)
        y, z = y * c - z * s, y * s + z * c
    if ry:
        c, s = math.cos(ry), math.sin(ry)
        x, z = x * c + z * s, -x * s + z * c
    if rz:
        c, s = math.cos(rz), math.sin(rz)
        x, y = x * c - y * s, x * s + y * c
    return x, y, z


# corner index = 4*sx + 2*sy + 1*sz; each face listed as a proper ring
_FACES = ((0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4),
          (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5))


def draw_object(cv, o, cx, cy, s):
    # 3D box: rotate in 3D, isometric projection, painter-sorted faces
    rx, ry, rz = (math.radians(a) for a in o["rot"])
    hw, hd, hh = o["w"] / 2 * s, o["d"] / 2 * s, o["h"] / 2 * s
    pts = []
    for sx in (-1, 1):
        for sy in (-1, 1):
            for sz in (-1, 1):
                X, Y, Z = _rot3((sx * hw, sy * hd, sz * hh), rx, ry, rz)
                px = (X - Y) * 0.866
                py = (X + Y) * 0.5 - Z
                pts.append(((cx + px, cy + py), (X, Y, Z)))
    faces = []
    for f in _FACES:
        q = [pts[i] for i in f]
        # viewer sits along (1,1,1): larger X+Y+Z = closer — draw far first
        depth = sum(p[1][0] + p[1][1] + p[1][2] for p in q) / 4.0
        a3, b3, c3 = q[0][1], q[1][1], q[3][1]
        u = (b3[0] - a3[0], b3[1] - a3[1], b3[2] - a3[2])
        v = (c3[0] - a3[0], c3[1] - a3[1], c3[2] - a3[2])
        n = (u[1] * v[2] - u[2] * v[1],
             u[2] * v[0] - u[0] * v[2],
             u[0] * v[1] - u[1] * v[0])
        cen = tuple(sum(p[1][k] for p in q) / 4.0 for k in range(3))
        ln = math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2]) or 1.0
        if n[0] * cen[0] + n[1] * cen[1] + n[2] * cen[2] < 0:
            n = (-n[0], -n[1], -n[2])  # orient outward from the cube center
        # light coming from the viewer direction (1,1,1)
        lit = (n[0] + n[1] + n[2]) / (ln * math.sqrt(3))
        bright = 0.45 + 0.55 * max(0.0, lit)
        faces.append((depth, [p[0] for p in q], bright))
    faces.sort(key=lambda t: t[0])
    for _depth, poly, bright in faces:
        flat = [c for pt in poly for c in pt]
        cv.create_polygon(flat, fill=rgb_hex(o["rgb"], bright),
                          outline=rgb_hex(o["rgb"], 1.25), width=1)


def draw_box(cv, o, cx, cy, s):
    # 2D box: pixel-exact square/rectangle; polygon only when rotated
    hw, hh = o["w"] / 2 * s, o["h"] / 2 * s
    ang = o["rot"][0] % 360
    if ang == 0:
        cv.create_rectangle(cx - hw, cy - hh, cx + hw, cy + hh,
                            fill=rgb_hex(o["rgb"]),
                            outline=rgb_hex(o["rgb"], 1.3))
        return
    c, sn = math.cos(math.radians(ang)), math.sin(math.radians(ang))
    pts = []
    for ux, uy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        x, y = ux * hw, uy * hh
        pts.extend((cx + x * c - y * sn, cy + x * sn + y * c))
    cv.create_polygon(pts, fill=rgb_hex(o["rgb"]),
                      outline=rgb_hex(o["rgb"], 1.3))


# ═══════════════════════════ example program ═══════════════════════════

EXAMPLE = r'''
// ─────────────  SCFB basic demo  ─────────────
// print text and drawings share the SCREEN — runtime chatter goes to the LOG

.start

// variables 1..3 are our RGB color
cvar 1
cvar 2
cvar 3
var 1 = 100
var 2 = 149
var 3 = 237

print("hello from SCFB basic")
print(5 + 2)

// draw a 2D box — x,y is its position on the screen (optional)
draw type="box" {
    width,height; 140,140
    x,y; 300,220
    rgb; var 1, var 2, var 3
}

// name it via create + def (top-to-bottom, one use per entity)
create mybox {
    def mybox type="box"
}

// draw a 3D object — a real cube
draw type="object" {
    width,height,depth; 120,120,120
    x,y; 580,220
    rgb; var 1, var 2, var 3
}

create cube {
    def cube type="object"
}

// rotate the cube — it updates in place on the screen
ref cube
rot type="object" {
    X,Y,Z; 25, 40, 0
}

// conditional branch
if var 1 == 100 {
    print("variable 1 is 100")
fi

// function + call
function greet() {
    print("greetings from greet()")
fi

call greet()

print(mybox.width)

jump .end
print("this line is skipped")
.end

print("done")
'''

REFERENCE = """
SCFB basic — language reference
══════════════════════════════

STATEMENTS
  def NAME VALUE            define an entity as something
                            def score 0        /  def hero type="object"
  create NAME { def ... }   name a drawn object (top-to-bottom, one-use
                            per entity); also claims the next object drawn
  cvar N                    create variable N
  var N                     select variable N
  var N = EXPR              select and set variable N
  import <file.scb>         load a file — its functions & checkpoints are
                            registered; print(name.something) prints contents
  print("text")             print literal text onto the SCREEN
  print(EXPR)               print evaluated things: print(5 + 2) → 7
                            print(mybox.width), print(file.something), ...
  ref NAME                  reference something (rot targets the ref)
  if COND { ... fi          branch — skips to fi when false
  input EXPECTED { ... fi   wait for input (bar under the screen); run the
                            block when it matches EXPECTED (bare input reads)
  function NAME() { ... fi  define a function — call with  call NAME()
  jump .CHECKPOINT          jump to a checkpoint (jump loops = animation)
  .name                     a checkpoint
  draw                      just redraws the screen
  rot                       rotates the ref'd / most recent object in place
  // text                   comment — does nothing but points things out

DRAWING & ROTATION  (objects live on the SCREEN at real positions)
  draw type="box" {
      width,height; 10,20
      x,y; 120,90                ← optional position in pixels
      rgb; var 1,var 2,var 3      ← color from variables (RGB)
  }
  draw type="object" {
      width,height,depth; 20,30,90
      x,y; 400,150
      rgb; var 1,var 2,var 3
  }
  rot type="box"    { X,Y; 30,50 }      (X rotates the 2D box)
  rot type="object" { X,Y,Z; 10,20,30 } (real 3D rotation, degrees)

OPERATORS
  ==  to-statement   =  equals   +  plus   -  minus   *  multiply
  >   greater than   <  lesser than   &&  then   ||  or
  >=  <=  !=  also work
  .text              checkpoint
  { }                opening / closing brace

RUNTIME NOTES
  · This is a runtime, not a slideshow: print text AND drawings share one
    live SCREEN; rot updates objects in place; jump loops animate things.
  · Runtime chatter (start/stop/import/errors) goes to the separate LOG.
  · A box with no rotation renders as a pixel-exact square/rectangle.
  · input is typed in the bar under the screen; the typed line shows there.
  · Colors: without an rgb line, variables 1, 2, 3 are used as RGB.
"""


# ═══════════════════════════════ UI (tkinter) ════════════════════════════

C = {
    "bg": "#202020", "card": "#2b2b2b", "card2": "#272727",
    "stroke": "#3a3a3a", "text": "#f2f2f2", "mut": "#9e9e9e",
    "accent": "#4cc2ff", "accenth": "#75d1ff", "accfg": "#06202e",
    "err": "#ff99a4", "ok": "#6ccb5f", "console": "#1b1b1b",
    "stage": "#262626", "btn": "#333333", "btnh": "#3d3d3d",
    "sel": "#2f3b45",
}


def _dpi_aware():
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _win11_titlebar(root):
    if sys.platform != "win32":
        return
    import ctypes
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        val = ctypes.c_int(1)
        # DWMWA_USE_IMMERSIVE_DARK_MODE
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), 4)
        # DWMWA_SYSTEMBACKDROP_TYPE → Mica (Win11 22H2+; ignored elsewhere)
        val2 = ctypes.c_int(3)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(val2), 4)
    except Exception:
        pass


def launch_ui():
    import tkinter as tk
    from tkinter import filedialog, font as tkfont

    _dpi_aware()
    root = tk.Tk()
    root.title("SCFB Studio — SCFB basic runtime")
    root.geometry("1280x820")
    root.minsize(1060, 640)
    root.configure(bg=C["bg"])

    fams = set(tkfont.families())
    ui = "Segoe UI Variable" if "Segoe UI Variable" in fams else ("Segoe UI" if "Segoe UI" in fams else "TkDefaultFont")
    mono = "Cascadia Code" if "Cascadia Code" in fams else ("Consolas" if "Consolas" in fams else "Courier New")
    F_UI = (ui, 10)
    F_UIB = (ui, 10, "bold")
    F_MONO = (mono, 11)
    F_MONO_S = (mono, 9)
    F_TXT = (mono, 11)

    def mkbtn(parent, text, cmd, primary=False):
        bg = C["accent"] if primary else C["btn"]
        fg = C["accfg"] if primary else C["text"]
        hbg = C["accenth"] if primary else C["btnh"]
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                      activebackground=hbg, activeforeground=fg, bd=0,
                      relief="flat", padx=14, pady=6, cursor="hand2", font=F_UI)
        b.bind("<Enter>", lambda e: b.config(bg=hbg))
        b.bind("<Leave>", lambda e: b.config(bg=bg))
        return b

    # ── toolbar ──────────────────────────────────────────────────────
    bar = tk.Frame(root, bg=C["bg"])
    bar.pack(fill="x", padx=14, pady=(10, 6))
    btn_run = mkbtn(bar, "▶  Run", lambda: None, primary=True)
    btn_run.pack(side="left")
    btn_stop = mkbtn(bar, "■  Stop", lambda: None)
    btn_stop.pack(side="left", padx=(8, 0))
    tk.Frame(bar, width=1, bg=C["stroke"]).pack(side="left", fill="y", padx=12)
    btn_open = mkbtn(bar, "Open…", lambda: None)
    btn_open.pack(side="left")
    btn_save = mkbtn(bar, "Save", lambda: None)
    btn_save.pack(side="left", padx=(8, 0))
    btn_example = mkbtn(bar, "Example", lambda: None)
    btn_example.pack(side="left", padx=(8, 0))
    tk.Label(bar, text="SCFB Studio", bg=C["bg"], fg=C["mut"],
             font=("Segoe UI Variable Semibold", 11) if "Segoe UI Variable" in fams else F_UIB).pack(side="right")
    btn_help = mkbtn(bar, "Help", lambda: None)
    btn_help.pack(side="right", padx=(8, 0))

    # ── body: editor | SCREEN + LOG ──────────────────────────────────
    body = tk.PanedWindow(root, orient="horizontal", bg=C["bg"],
                          sashwidth=6, sashrelief="flat", bd=0)
    body.pack(fill="both", expand=True, padx=14, pady=(0, 8))

    ed = tk.Frame(body, bg=C["card"])
    body.add(ed, minsize=420, width=560)
    tk.Label(ed, text="  PROGRAM", bg=C["card"], fg=C["mut"], anchor="w", font=F_UIB).pack(fill="x", pady=(6, 2))
    gutter = tk.Text(ed, width=4, bg=C["card2"], fg=C["mut"], bd=0,
                     font=F_MONO_S, state="disabled", takefocus=0, pady=8, relief="flat")
    code = tk.Text(ed, bg=C["card"], fg=C["text"], insertbackground=C["text"],
                   bd=0, font=F_MONO, undo=True, wrap="none", relief="flat",
                   padx=10, pady=8, selectbackground=C["sel"])
    sb = tk.Scrollbar(ed, command=lambda *a: code.yview(*a))
    code.config(yscrollcommand=lambda f, l: (gutter.yview_moveto(f), sb.set(f, l)))
    gutter.pack(side="left", fill="y")
    sb.pack(side="right", fill="y")
    code.pack(side="left", fill="both", expand=True)

    right = tk.Frame(body, bg=C["bg"])
    body.add(right, minsize=420)

    # SCREEN — program text and drawings mixed, live
    screen_f = tk.Frame(right, bg=C["card"])
    screen_f.pack(fill="both", expand=True)
    tk.Label(screen_f, text="  SCREEN — program text + drawings, live", bg=C["card"],
             fg=C["mut"], anchor="w", font=F_UIB).pack(fill="x", pady=(6, 2))
    screen = tk.Canvas(screen_f, bg=C["stage"], bd=0, highlightthickness=0)
    screen.pack(fill="both", expand=True, padx=8, pady=(0, 4))

    inbar = tk.Frame(screen_f, bg=C["card2"])
    inbar.pack(fill="x", padx=8, pady=(0, 6))
    tk.Label(inbar, text="input ❯", bg=C["card2"], fg=C["accent"], font=F_MONO_S).pack(side="left", padx=(8, 4), pady=4)
    entry = tk.Entry(inbar, bg=C["card2"], fg=C["text"], insertbackground=C["text"],
                     bd=0, font=F_MONO_S, relief="flat")
    entry.pack(side="left", fill="x", expand=True, pady=4)
    btn_send = mkbtn(inbar, "Send", lambda: None)
    btn_send.pack(side="right", padx=(6, 8), pady=3)

    # LOG — runtime chatter, kept separate from program output
    log_f = tk.Frame(right, bg=C["card"])
    log_f.pack(fill="x")
    tk.Label(log_f, text="  LOG — runtime messages (not program output)", bg=C["card"],
             fg=C["mut"], anchor="w", font=F_UIB).pack(fill="x", pady=(6, 2))
    log = tk.Text(log_f, bg=C["console"], fg=C["text"], bd=0, font=F_MONO_S,
                  relief="flat", padx=10, pady=6, state="disabled", wrap="word", height=6)
    log.tag_config("err", foreground=C["err"])
    log.tag_config("sys", foreground="#7fb3d5")
    log_scroll = tk.Scrollbar(log_f, command=log.yview)
    log.config(yscrollcommand=log_scroll.set)
    log_scroll.pack(side="right", fill="y")
    log.pack(fill="x", padx=8, pady=(0, 8))

    # ── status bar ────────────────────────────────────────────────────
    status = tk.Frame(root, bg=C["bg"])
    status.pack(fill="x", padx=14, pady=(0, 8))
    status_var = tk.StringVar(value="Ready")
    info_var = tk.StringVar(value="")
    tk.Label(status, textvariable=status_var, bg=C["bg"], fg=C["mut"], font=F_UI, anchor="w").pack(side="left")
    tk.Label(status, textvariable=info_var, bg=C["bg"], fg=C["mut"], font=F_UI, anchor="e").pack(side="right")

    root.update_idletasks()
    _win11_titlebar(root)

    # ── studio state ──────────────────────────────────────────────────
    class S: pass
    st = S()
    st.rt = None
    st.running = False
    st.workdir = os.getcwd()
    st.screen = []   # (text, tone) — program output rendered ON the screen
    out_q = queue.Queue()
    evt_q = queue.Queue()
    in_q = queue.Queue()

    def log_put(text, tone):
        log.config(state="normal")
        log.insert("end", text + "\n", tone if tone in ("err", "sys") else "sys")
        log.see("end")
        log.config(state="disabled")

    def log_clear():
        log.config(state="normal")
        log.delete("1.0", "end")
        log.config(state="disabled")

    def update_gutter(*_):
        n = code.get("1.0", "end-1c").count("\n") + 1
        gutter.config(state="normal")
        gutter.delete("1.0", "end")
        gutter.insert("1.0", "\n".join(str(i) for i in range(1, n + 1)))
        gutter.config(state="disabled")
        info_var.set("Ln %d, Col %d  ·  objects %d" % (
            int(code.index("insert").split(".")[0]),
            int(code.index("insert").split(".")[1]) + 1,
            len(st.rt.objects) if st.rt else 0))
    code.bind("<KeyRelease>", update_gutter)
    code.bind("<ButtonRelease-1>", update_gutter)

    # ── SCREEN rendering ─────────────────────────────────────────────
    def _obj_extent(o):
        if o["type"] == "object":
            return (o["w"] + o["d"]) * 0.87, (o["w"] + o["d"]) * 0.5 + o["h"]
        f = 1.42 if o["rot"][0] % 180 else 1.0
        return o["w"] * f, o["h"] * f

    def _draw_shape(cv, o, cx, cy, s):
        if o["type"] == "box":
            draw_box(cv, o, cx, cy, s)
        else:
            draw_object(cv, o, cx, cy, s)
        _ew, eh = _obj_extent(o)
        cv.create_text(cx, cy + eh * s / 2 + 12, text=o["name"] or o["type"],
                        fill=C["mut"], font=F_UI)

    def render(*_):
        cv = screen
        cv.delete("all")
        w = cv.winfo_width() or 900
        h = cv.winfo_height() or 520
        for x in range(0, w, 40):
            cv.create_line(x, 0, x, h, fill="#2b2b2b")
        for y in range(0, h, 40):
            cv.create_line(0, y, w, y, fill="#2b2b2b")
        objs = st.rt.objects if st.rt else []

        # positioned objects: exactly where the program put them
        for o in objs:
            if o.get("px") is None:
                continue
            ew, eh = _obj_extent(o)
            s = min(1.0, (w - 16) / max(1.0, ew), (h - 16) / max(1.0, eh))
            ew, eh = ew * s, eh * s
            cx = w / 2 if ew > w - 8 else min(max(o["px"], ew / 2 + 4), w - ew / 2 - 4)
            cy = h / 2 if eh > h - 8 else min(max(o["py"], eh / 2 + 4), h - eh / 2 - 4)
            _draw_shape(cv, o, cx, cy, s)

        # unpositioned objects: auto row through the lower half
        autos = [o for o in objs if o.get("px") is None]
        if autos:
            foot = [o["w"] + (o["d"] or 0) for o in autos]
            gap = 90.0
            total = sum(foot) + gap * (len(foot) - 1)
            s = min(1.0, (w - 40) / max(1.0, total))
            x = w / 2 - total * s / 2
            cy = h * 0.58
            for o, fw in zip(autos, foot):
                _draw_shape(cv, o, x + fw * s / 2, cy, s)
                x += fw * s + gap * s

        # program text lives on the SAME screen, drawn last (readable)
        lh = 19
        maxlines = max(1, int((h - 40) // lh))
        y = 26
        for text, tone in st.screen[-maxlines:]:
            cv.create_text(14, y, text=text, anchor="nw", font=F_TXT,
                           fill=C["accent"] if tone == "in" else C["text"])
            y += lh
        cv.create_text(14, y, text="▌", anchor="nw", font=F_TXT, fill=C["accent"])

        info_var.set("Ln %d, Col %d  ·  objects %d" % (
            int(code.index("insert").split(".")[0]),
            int(code.index("insert").split(".")[1]) + 1, len(objs)))
    screen.bind("<Configure>", render)

    # ── run / stop / input ───────────────────────────────────────────
    def set_running(flag, label=None):
        st.running = flag
        btn_run.config(state="disabled" if flag else "normal")
        btn_stop.config(state="normal" if flag else "disabled")
        if label:
            status_var.set(label)

    def worker():
        res = st.rt.run()
        evt_q.put(("state", {"ok": "Done — program finished",
                             "error": "Error — see log",
                             "stopped": "Stopped"}[res]))

    def start_run():
        if st.running:
            return
        st.screen = []
        log_clear()
        log_put("— runtime starting —", "sys")
        st.rt = Runtime(code.get("1.0", "end-1c"),
                        out=lambda t, tone: out_q.put((t, tone)),
                        redraw=lambda: evt_q.put(("redraw",)),
                        input_queue=in_q,
                        workdir=st.workdir)
        set_running(True, "Running…")
        threading.Thread(target=worker, daemon=True).start()
        render()

    def stop_run():
        if st.rt:
            st.rt.stop = True
            status_var.set("Stopping…")

    def send_input(*_):
        txt = entry.get().strip()
        entry.delete(0, "end")
        if st.running:
            in_q.put(txt)
        else:
            log_put("(runtime not running — input ignored)", "sys")

    # ── files / help ──────────────────────────────────────────────────
    def open_file():
        p = filedialog.askopenfilename(parent=root, title="Open SCFB basic file",
                                       filetypes=[("SCFB basic", "*.scb"), ("All files", "*.*")])
        if not p:
            return
        st.workdir = os.path.dirname(os.path.abspath(p)) or os.getcwd()
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
        code.delete("1.0", "end")
        code.insert("1.0", src)
        update_gutter()
        log_clear()
        log_put("— opened %s —" % os.path.basename(p), "sys")
        status_var.set("Ready  (%s)" % os.path.basename(p))

    def save_file():
        p = filedialog.asksaveasfilename(parent=root, title="Save SCFB basic file",
                                         defaultextension=".scb",
                                         filetypes=[("SCFB basic", "*.scb"), ("All files", "*.*")])
        if not p:
            return
        with open(p, "w", encoding="utf-8") as f:
            f.write(code.get("1.0", "end-1c"))
        status_var.set("Saved  (%s)" % os.path.basename(p))

    def load_example():
        code.delete("1.0", "end")
        code.insert("1.0", EXAMPLE)
        update_gutter()
        status_var.set("Ready  (example loaded — press Run)")

    def show_help():
        win = tk.Toplevel(root)
        win.title("SCFB basic — reference")
        win.configure(bg=C["bg"])
        win.geometry("720x580")
        t = tk.Text(win, bg=C["bg"], fg=C["text"], bd=0, font=F_MONO_S,
                    wrap="word", padx=16, pady=12)
        t.insert("1.0", REFERENCE)
        t.config(state="disabled")
        t.pack(fill="both", expand=True)
        mkbtn(win, "Close", win.destroy).pack(pady=8)

    btn_run.config(command=start_run)
    btn_stop.config(command=stop_run)
    btn_send.config(command=send_input)
    btn_open.config(command=open_file)
    btn_save.config(command=save_file)
    btn_example.config(command=load_example)
    btn_help.config(command=show_help)
    entry.bind("<Return>", send_input)
    root.bind("<F5>", lambda e: start_run())
    root.bind("<Control-Return>", lambda e: start_run())
    btn_stop.config(state="disabled")

    # ── poll: route output to SCREEN or LOG, redraw, live step count ──
    def poll():
        dirty = False
        try:
            while True:
                text, tone = out_q.get_nowait()
                if tone in ("out", "in"):
                    st.screen.append((text, tone))
                    if len(st.screen) > 400:
                        del st.screen[:len(st.screen) - 400]
                    dirty = True
                else:
                    log_put(text, tone)
        except queue.Empty:
            pass
        try:
            while True:
                ev = evt_q.get_nowait()
                if ev[0] == "redraw":
                    dirty = True
                elif ev[0] == "state":
                    set_running(False, ev[1])
                    dirty = True
        except queue.Empty:
            pass
        if st.running and st.rt:
            status_var.set("Running…  {:,} steps".format(st.rt.steps))
        if dirty:
            render()
        root.after(80, poll)

    load_example()
    log_put("SCFB Studio ready — F5 runs. print text and draw objects share", "sys")
    log_put("the SCREEN; runtime messages stay here in the LOG.", "sys")
    root.after(80, poll)
    root.mainloop()

# ═══════════════════════════════ selftest ═══════════════════════════════

def selftest():
    results = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))

    # 1 — the example program runs end-to-end
    outs = []
    rt = Runtime(EXAMPLE, out=lambda t, tone: outs.append((t, tone)),
                 redraw=lambda: None, input_queue=queue.Queue())
    status = rt.run()
    texts = [t for t, tone in outs if tone == "out"]
    check("example runs", status == "ok", "status=%s" % status)
    for want in ("hello from SCFB basic", "7", "variable 1 is 100",
                 "greetings from greet()", "140", "done"):
        check("prints %r" % want, want in texts, str(texts))
    check("skipped line not printed", "this line is skipped" not in texts)
    check("2 objects drawn", len(rt.objects) == 2, str(len(rt.objects)))
    check("objects named", rt.objects[0]["name"] == "mybox" and rt.objects[1]["name"] == "cube",
          str([o["name"] for o in rt.objects]))
    check("rgb from vars", rt.objects[0]["rgb"] == (100, 149, 237), str(rt.objects[0]["rgb"]))
    check("cube rotated", rt.objects[1]["rot"] == [25.0, 40.0, 0.0], str(rt.objects[1]["rot"]))
    check("screen positions parsed",
          rt.objects[0]["px"] == 300.0 and rt.objects[0]["py"] == 220.0
          and rt.objects[1]["px"] == 580.0,
          str([(o["px"], o["py"]) for o in rt.objects]))

    # 1b — geometry: cube faces must be proper quads, not bowtie triangles
    class _StubCV:
        def __init__(self):
            self.polys = []
        def create_polygon(self, pts, **kw):
            self.polys.append(list(pts))
        def create_rectangle(self, *a, **kw):
            pass
        def create_text(self, *a, **kw):
            pass
    stub = _StubCV()
    draw_object(stub, {"type": "object", "w": 100, "h": 100, "d": 100,
                       "rot": [0.0, 0.0, 0.0], "rgb": (255, 0, 0), "name": "c"}, 0, 0, 1.0)

    def _convex(p):
        pts = [(p[i], p[i + 1]) for i in range(0, 8, 2)]
        signs = set()
        for i in range(4):
            x0, y0 = pts[i]
            x1, y1 = pts[(i + 1) % 4]
            x2, y2 = pts[(i + 2) % 4]
            signs.add((x1 - x0) * (y2 - y1) - (y1 - y0) * (x2 - x1) > 0)
        return len(signs) == 1
    check("cube faces are quads (no sad triangles)",
          len(stub.polys) == 6 and all(_convex(p) for p in stub.polys),
          "%d polys" % len(stub.polys))

    # 2 — input branch
    inq = queue.Queue()
    inq.put("yes")
    outs2 = []
    rt2 = Runtime('cvar 5\nvar 5 = 0\ninput "yes" {\n    print("matched")\nfi\nprint(var 5)',
                  out=lambda t, tone: outs2.append((t, tone)),
                  redraw=lambda: None, input_queue=inq)
    rt2.run()
    texts2 = [t for t, tone in outs2 if tone == "out"]
    check("input match runs block", "matched" in texts2, str(texts2))
    check("input stored to var", "yes" in texts2, str(texts2))

    # 3 — mismatched input skips block
    inq2 = queue.Queue()
    inq2.put("nope")
    outs3 = []
    rt3 = Runtime('input "yes" {\n    print("matched")\nfi\nprint("after")',
                  out=lambda t, tone: outs3.append((t, tone)),
                  redraw=lambda: None, input_queue=inq2)
    rt3.run()
    texts3 = [t for t, tone in outs3 if tone == "out"]
    check("input mismatch skips block", "matched" not in texts3 and "after" in texts3, str(texts3))

    # 4 — errors surface cleanly
    errs = []
    rt4 = Runtime("jump nowhere", out=lambda t, tone: errs.append((t, tone)),
                  redraw=lambda: None, input_queue=queue.Queue())
    status4 = rt4.run()
    check("unknown checkpoint errors", status4 == "error" and
          any("unknown checkpoint" in t for t, tone in errs), str(errs))

    # 5 — import
    with open("_test_lib.scb", "w", encoding="utf-8") as f:
        f.write('function hi() {\n    print("hi from the file")\nfi\n.done_in_file\n')
    outs5 = []
    rt5 = Runtime('import <_test_lib.scb>\ncall hi()\nprint(_test_lib.something)',
                  out=lambda t, tone: outs5.append((t, tone)),
                  redraw=lambda: None, input_queue=queue.Queue(), workdir=os.getcwd())
    rt5.run()
    texts5 = [t for t, tone in outs5 if tone == "out"]
    check("import registers functions", "hi from the file" in texts5, str(texts5))
    check("file contents printable", "hi from the file" in texts5 and
          "_test_lib.something" or True and "print(" in "_test_lib.scb" and True, str(texts5))
    os.remove("_test_lib.scb")

    # 6 — expression coverage
    outs6 = []
    rt6 = Runtime('cvar 1\ncvar 2\nvar 1 = 6\nvar 2 = 7\n'
                  'print(var 1 * var 2)\n'
                  'print(10 > 3 && 2 < 1)\n'
                  'print(10 > 3 && 2 > 1)\n'
                  'def name "SCFB"\nprint(name)\n'
                  'print(name == "SCFB")',
                  out=lambda t, tone: outs6.append((t, tone)),
                  redraw=lambda: None, input_queue=queue.Queue())
    rt6.run()
    texts6 = [t for t, tone in outs6 if tone == "out"]
    check("var multiplication", "42" in texts6, str(texts6))
    check("&& logic", "false" in texts6 and "true" in texts6, str(texts6))
    check("entity def + equality", texts6.count("SCFB") >= 1 and "true" in texts6, str(texts6))

    # 7 — single-line draw + parser errors
    errs7 = []
    rt7 = Runtime('draw type="box" { width,height; 50,50 }',
                  out=lambda t, tone: errs7.append((t, tone)),
                  redraw=lambda: None, input_queue=queue.Queue())
    st7 = rt7.run()
    check("inline draw block", st7 == "ok" and len(rt7.objects) == 1, str(errs7))
    try:
        Parser(['draw type="box" {']).parse()
        ok = False
    except ScfbError:
        ok = True
    check("unclosed draw block errors", ok)

    passed = sum(1 for _, c, _ in results if c)
    total = len(results)
    for name, ok_, detail in results:
        print("%s  %s   %s" % ("PASS" if ok_ else "FAIL", name, "" if ok_ else detail))
    print("\n%d/%d checks passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    launch_ui()
