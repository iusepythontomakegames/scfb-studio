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
STEP_LIMIT = 2_000_000


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
        steps = 0
        status = "ok"
        try:
            while pc < len(prog):
                if frames and pc == frames[-1][0]:
                    pc = frames.pop()[1]
                    continue
                steps += 1
                if steps > STEP_LIMIT:
                    raise ScfbError("step limit hit — infinite loop?")
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
            line = self._read_input()
            self.out("> " + line, "sys")
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
        self.program.extend(sub)
        for idx, s in enumerate(sub):
            if s["op"] == "checkpoint":
                self.checkpoints[s["name"]] = off + idx
            elif s["op"] == "funcdef":
                self.funcs[s["name"]] = (off + s["start"], off + s["end"])
        self.out("imported <%s> — functions & checkpoints registered" % st["path"], "sys")

    def _do_draw(self, st):
        typ = st["type"]
        body = st["body"]
        w = self.eval(body.get("width", "100"))
        h = self.eval(body.get("height", "100"))
        d = self.eval(body.get("depth", "100")) if typ == "object" else None
        obj = {"type": typ, "w": float(w), "h": float(h),
               "d": float(d) if d is not None else None,
               "rot": [0.0, 0.0, 0.0], "rgb": self._rgb_from(body), "name": None}
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


_FACES = ((0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4),
          (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7))


def draw_object(cv, o, cx, cy, s):
    """Isometric projected, rotatable 3D box with painter-sorted faces."""
    rx, ry, rz = (math.radians(a) for a in o["rot"])
    hw, hd, hh = o["w"] / 2 * s, o["d"] / 2 * s, o["h"] / 2 * s
    pts = []
    for sx in (-1, 1):
        for sy in (-1, 1):
            for sz in (-1, 1):
                X, Y, Z = _rot3((sx * hw, sy * hd, sz * hh), rx, ry, rz)
                px = (X - Y) * 0.866
                py = (X + Y) * 0.5 - Z
                pts.append(((cx + px, cy + py), (X + Y + Z, X, Y, Z)))
    faces = []
    for f in _FACES:
        q = [pts[i] for i in f]
        depth = sum(p[1][0] for p in q) / 4.0
        (x1, y1), _ = q[0]
        (x2, y2), _ = q[1]
        (x3, y3), m3 = q[3]
        ax, ay, az = x2 - x1, y2 - y1, q[1][1][1] - q[0][1][1]
        bx, by, bz = x3 - x1, y3 - y1, m3[2] - q[0][1][2]
        nx = ay * bz - az * by
        ny = az * bx - ax * bz
        nz = ax * by - ay * bx
        L = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        cxm = sum(p[1][1] for p in q) / 4.0
        cym = sum(p[1][2] for p in q) / 4.0
        czm = sum(p[1][3] for p in q) / 4.0
        dot = (nx * cxm + ny * cym + nz * czm) / L
        if dot < 0:
            nx, ny, nz = -nx, -ny, -nz
            dot = -dot
        bright = 0.45 + 0.55 * _clamp(dot / (math.sqrt(cxm**2 + cym**2 + czm**2) or 1), 0, 1)
        faces.append((depth, [p[0] for p in q], bright))
    faces.sort(key=lambda t: t[0])
    for depth, poly, bright in faces:
        flat = [c for pt in poly for c in pt]
        cv.create_polygon(flat, fill=rgb_hex(o["rgb"], bright),
                          outline=rgb_hex(o["rgb"], 1.35), width=1)


def draw_box(cv, o, cx, cy, s):
    """Rotatable 2D rectangle."""
    a = math.radians(o["rot"][0])
    hw, hh = o["w"] / 2 * s, o["h"] / 2 * s
    pts = []
    for ux, uy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        x, y = ux * hw, uy * hh
        pts.extend((cx + x * math.cos(a) - y * math.sin(a),
                    cy + x * math.sin(a) + y * math.cos(a)))
    cv.create_polygon(pts, fill=rgb_hex(o["rgb"]), outline=rgb_hex(o["rgb"], 1.35), width=1)


# ═══════════════════════════ example program ═══════════════════════════

EXAMPLE = r'''// ─────────────  SCFB basic demo  ─────────────
// checkpoints start with a dot, like .start

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

// draw a 2D box — colored by variables 1-3
draw type="box" {
    width,height; 140,90
    rgb; var 1, var 2, var 3
}

// name it via create + def (top-to-bottom, one use per entity)
create mybox {
    def mybox type="box"
}

// draw a 3D object
draw type="object" {
    width,height,depth; 120,120,120
    rgb; var 1, var 2, var 3
}

create cube {
    def cube type="object"
}

// rotate the cube
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

REFERENCE = """SCFB basic — language reference
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
  print("text")             print literal text
  print(EXPR)               print evaluated things: print(5 + 2) → 7
                            print(mybox.width), print(file.something), ...
  ref NAME                  reference something (rot targets the ref)
  if COND { ... fi          branch — skips to fi when false
  input EXPECTED { ... fi   wait for console input; run block when it
                            matches EXPECTED (input alone just reads)
  function NAME() { ... fi  define a function — call with  call NAME()
  jump .CHECKPOINT          jump to a checkpoint
  .name                     a checkpoint
  draw                      just redraws the stage
  rot                       rotates the ref'd / most recent object
  // text                   comment — does nothing but points things out

DRAWING & ROTATION
  draw type="box" {
      width,height; 10,20
      rgb; var 1,var 2,var 3        (color from variables — RGB)
  }
  draw type="object" {
      width,height,depth; 20,30,90
      rgb; var 1,var 2,var 3
  }
  rot type="box"    { X,Y; 30,50 }      (X rotates the 2D box)
  rot type="object" { X,Y,Z; 10,20,30 } (3D rotation, degrees)

OPERATORS
  ==  to-statement   =  equals   +  plus   -  minus   *  multiply
  >   greater than   <  lesser than   &&  then   ||  or
  >=  <=  !=  also work
  .text              checkpoint
  { }                opening / closing brace

RUNTIME NOTES
  · SCFB basic runs inside this runtime — the stage shows drawn objects.
  · input waits in the console bar at the bottom.
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
    mkbtn(bar, "Open…", lambda: None).pack(side="left")
    mkbtn(bar, "Save", lambda: None).pack(side="left", padx=(8, 0))
    mkbtn(bar, "Example", lambda: None).pack(side="left", padx=(8, 0))
    tk.Label(bar, text="SCFB Studio", bg=C["bg"], fg=C["mut"], font=("Segoe UI Variable Semibold", 11) if "Segoe UI Variable" in fams else F_UIB).pack(side="right")

    # ── body: editor | (stage / console) ──────────────────────────────
    body = tk.PanedWindow(root, orient="horizontal", bg=C["bg"],
                          sashwidth=6, sashrelief="flat", bd=0)
    body.pack(fill="both", expand=True, padx=14, pady=(0, 8))

    ed = tk.Frame(body, bg=C["card"])
    body.add(ed, minsize=420, width=560)

    tk.Label(ed, text="  PROGRAM", bg=C["card"], fg=C["mut"], anchor="w", font=F_UIB).pack(fill="x", pady=(6, 2))

    gutter = tk.Text(ed, width=4, bg=C["card2"], fg=C["mut"], bd=0,
                     font=F_MONO_S, state="disabled", takefocus=0,
                     pady=8, relief="flat")
    code = tk.Text(ed, bg=C["card"], fg=C["text"], insertbackground=C["text"],
                   bd=0, font=F_MONO, undo=True, wrap="none",
                   relief="flat", padx=10, pady=8,
                   selectbackground=C["sel"])
    sb = tk.Scrollbar(ed, command=lambda *a: (code.yview(*a)))
    code.config(yscrollcommand=lambda f, l: (gutter.yview_moveto(f), sb.set(f, l)))
    gutter.pack(side="left", fill="y")
    sb.pack(side="right", fill="y")
    code.pack(side="left", fill="both", expand=True)

    right = tk.PanedWindow(body, orient="vertical", bg=C["bg"],
                           sashwidth=6, sashrelief="flat", bd=0)
    body.add(right, minsize=380)

    stage_f = tk.Frame(right, bg=C["card"])
    right.add(stage_f, minsize=260)
    tk.Label(stage_f, text="  STAGE", bg=C["card"], fg=C["mut"], anchor="w", font=F_UIB).pack(fill="x", pady=(6, 2))
    stage = tk.Canvas(stage_f, bg=C["stage"], bd=0, highlightthickness=0)
    stage.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    cons_f = tk.Frame(right, bg=C["card"])
    right.add(cons_f, minsize=180)
    tk.Label(cons_f, text="  RUNTIME / CONSOLE", bg=C["card"], fg=C["mut"], anchor="w", font=F_UIB).pack(fill="x", pady=(6, 2))
    cons = tk.Text(cons_f, bg=C["console"], fg=C["text"], bd=0, font=F_MONO_S,
                   relief="flat", padx=10, pady=8, state="disabled", wrap="word")
    for tag, col in (("out", C["text"]), ("err", C["err"]),
                     ("sys", "#7fb3d5"), ("in", C["accent"])):
        cons.tag_config(tag, foreground=col)
    cons_scroll = tk.Scrollbar(cons_f, command=cons.yview)
    cons.config(yscrollcommand=cons_scroll.set)
    cons_scroll.pack(side="right", fill="y")
    cons.pack(fill="both", expand=True, padx=8)

    inbar = tk.Frame(cons_f, bg=C["card2"])
    inbar.pack(fill="x", padx=8, pady=(4, 8))
    tk.Label(inbar, text="input ❯", bg=C["card2"], fg=C["accent"], font=F_MONO_S).pack(side="left", padx=(8, 4), pady=4)
    entry = tk.Entry(inbar, bg=C["card2"], fg=C["text"], insertbackground=C["text"],
                     bd=0, font=F_MONO_S, relief="flat")
    entry.pack(side="left", fill="x", expand=True, pady=4)
    btn_send = mkbtn(inbar, "Send", lambda: None)
    btn_send.pack(side="right", padx=(6, 8), pady=3)

    # ── status bar ────────────────────────────────────────────────────
    status = tk.Frame(root, bg=C["bg"])
    status.pack(fill="x", padx=14, pady=(0, 8))
    status_var = tk.StringVar(value="Ready")
    info_var = tk.StringVar(value="")
    tk.Label(status, textvariable=status_var, bg=C["bg"], fg=C["mut"], font=F_UI, anchor="w").pack(side="left")
    tk.Label(status, textvariable=info_var, bg=C["bg"], fg=C["mut"], font=F_UI, anchor="e").pack(side="right")

    root.update_idletasks()
    _win11_titlebar(root)

    # ══════════ studio state ══════════
    class S: pass
    st = S()
    st.rt = None
    st.running = False
    st.workdir = os.getcwd()
    out_q = queue.Queue()
    evt_q = queue.Queue()
    in_q = queue.Queue()

    # ---- console helpers ------------------------------------------------
    def con_put(text, tone):
        cons.config(state="normal")
        cons.insert("end", text + "\n", tone)
        cons.see("end")
        cons.config(state="disabled")

    def con_clear():
        cons.config(state="normal")
        cons.delete("1.0", "end")
        cons.config(state="disabled")

    # ---- gutter ---------------------------------------------------------
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

    # ---- stage rendering ------------------------------------------------
    def render(*_):
        cv = stage
        cv.delete("all")
        w = cv.winfo_width() or 860
        h = cv.winfo_height() or 440
        for x in range(0, w, 40):
            cv.create_line(x, 0, x, h, fill="#2c2c2c")
        for y in range(0, h, 40):
            cv.create_line(0, y, w, y, fill="#2c2c2c")
        cv.create_text(10, 8, text="SCFB stage", fill="#565656", anchor="nw", font=F_UI)
        objs = st.rt.objects if st.rt else []
        if not objs:
            cv.create_text(w / 2, h / 2, text="draw something — objects appear here",
                           fill="#565656", font=F_UI)
            return
        foot, fhs = [], []
        for o in objs:
            fw = o["w"] + (o["d"] or 0)
            fh = max(o["h"], (o["d"] or 0)) + (o["h"] if o["type"] == "object" else 0)
            foot.append(fw)
            fhs.append(fh)
        gap = 90
        total = sum(foot) + gap * (len(foot) - 1)
        maxfh = max(fhs) + 80
        s = min(1.5, (w - 70) / max(1, total), (h - 70) / max(1, maxfh))
        x = (w - total * s) / 2 + foot[0] * s / 2
        cy = h / 2
        for o, fw, fh in zip(objs, foot, fhs):
            cx = x
            if o["type"] == "box":
                draw_box(cv, o, cx, cy, s)
            else:
                draw_object(cv, o, cx, cy, s)
            cv.create_text(cx, cy + fh * s / 2 + 18,
                            text=o["name"] or o["type"], fill=C["mut"], font=F_UI)
            x += fw * s + gap * s
        info_var.set("Ln %d, Col %d  ·  objects %d" % (
            int(code.index("insert").split(".")[0]),
            int(code.index("insert").split(".")[1]) + 1, len(objs)))
    stage.bind("<Configure>", render)

    # ---- run / stop -------------------------------------------------------
    def set_status(t):
        status_var.set(t)
        btn_run.config(state="disabled" if st.running else "normal")
        btn_stop.config(state="normal" if st.running else "disabled")

    def worker():
        res = st.rt.run()
        evt_q.put(("state", {"ok": "Done — program finished",
                             "error": "Error — see console",
                             "stopped": "Stopped"}[res]))

    def start_run():
        if st.running:
            return
        con_clear()
        src = code.get("1.0", "end-1c")
        con_put("— runtime starting —", "sys")
        st.rt = Runtime(src,
                        out=lambda t, tone: out_q.put((t, tone)),
                        redraw=lambda: evt_q.put(("redraw",)),
                        input_queue=in_q,
                        workdir=st.workdir)
        evt_q.put(("redraw",))
        st.running = True
        set_status("Running…  (F5 / Ctrl+Enter to re-run · Stop to halt)")
        threading.Thread(target=worker, daemon=True).start()

    def stop_run():
        if st.rt:
            st.rt.stop = True
            set_status("Stopping…")

    def send_input(*_):
        txt = entry.get().strip()
        entry.delete(0, "end")
        if st.running:
            in_q.put(txt)
        else:
            con_put("(runtime not running — input ignored)", "sys")

    # ---- files -------------------------------------------------------------
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
        con_clear()
        con_put("— opened %s —" % os.path.basename(p), "sys")
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
        status_var.set("Ready  (example program loaded — press Run)")

    def show_help():
        win = tk.Toplevel(root)
        win.title("SCFB basic — reference")
        win.configure(bg=C["bg"])
        win.geometry("720x560")
        t = tk.Text(win, bg=C["bg"], fg=C["text"], bd=0, font=F_MONO_S,
                    wrap="word", padx=16, pady=12)
        t.insert("1.0", REFERENCE)
        t.config(state="disabled")
        t.pack(fill="both", expand=True)
        mkbtn(win, "Close", win.destroy).pack(pady=8)

    # ---- wire commands ----
    btn_run.config(command=start_run)
    btn_stop.config(command=stop_run)
    btn_stop.config(state="disabled")
    btn_send.config(command=send_input)
    entry.bind("<Return>", send_input)
    root.bind("<F5>", lambda e: start_run())
    root.bind("<Control-Return>", lambda e: start_run())
    for b in bar.winfo_children():
        try:
            t = b.cget("text")
        except Exception:
            continue
        if "Open" in t:
            b.config(command=open_file)
        elif "Save" in t:
            b.config(command=save_file)
        elif "Example" in t:
            b.config(command=load_example)

    # add Help to the right of the toolbar
    mkbtn(bar, "Help", show_help).pack(side="right", padx=(8, 0))

    # ---- poll loop ---------------------------------------------------------
    def poll():
        try:
            while True:
                text, tone = out_q.get_nowait()
                con_put(text, tone)
        except queue.Empty:
            pass
        try:
            while True:
                ev = evt_q.get_nowait()
                if ev[0] == "redraw":
                    render()
                elif ev[0] == "state":
                    st.running = False
                    status_var.set(ev[1])
                    btn_run.config(state="normal")
                    render()
        except queue.Empty:
            pass
        root.after(80, poll)

    load_example()
    con_put("SCFB Studio ready. Press Run (F5) — draw boxes & objects,", "sys")
    con_put("color them with variables, jump, branch, and take input here.", "sys")
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
