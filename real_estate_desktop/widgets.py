from __future__ import annotations
import asyncio,logging,queue,re,threading
import tkinter as tk
from tkinter import font as tkfont

COLORS = {
    "nav": "#eef3ef",
    "nav_hover": "#dfe9e2",
    "accent": "#276b55",
    "accent_hover": "#1e5543",
    "page": "#f7f8f6",
    "card": "#ffffff",
    "chat": "#ffffff",
    "text": "#23352c",
    "muted": "#738178",
    "line": "#e1e7e1",
    "user": "#276b55",
    "soft": "#f0f5f0",
    "danger": "#b45b50",
}

def _rounded_points(width, height, radius, inset=1):
    x, y, right, bottom = inset, inset, max(inset, width-inset), max(inset, height-inset)
    r = max(0, min(radius, (right-x)/2, (bottom-y)/2))
    return (x+r,y, right-r,y, right,y, right,y+r, right,bottom-r,
            right,bottom, right-r,bottom, x+r,bottom, x,bottom,
            x,bottom-r, x,y+r, x,y)

class RoundedFrame(tk.Frame):
    """A resizing rounded surface with an ordinary Tk content container."""
    def __init__(self, parent, *, fill=COLORS["card"], radius=18, padding=10, border=COLORS["line"], **kwargs):
        super().__init__(parent, bg=parent.cget("background"), bd=0, **kwargs)
        self.surface = tk.Canvas(self, bg=self.cget("background"), highlightthickness=0, bd=0)
        self.surface.place(x=0, y=0, relwidth=1, relheight=1)
        self._shape = self.surface.create_polygon(0,0,0,0, smooth=True, splinesteps=32,
            fill=fill, outline=border, width=1)
        self.radius = radius
        self.body = tk.Frame(self, bg=fill, bd=0)
        self.body.pack(fill="both", expand=True, padx=padding, pady=padding)
        self.bind("<Configure>", self._resize)

    def _resize(self, event):
        self.surface.coords(self._shape, *_rounded_points(event.width, event.height, self.radius))

    def set_fill(self, color):
        self.surface.itemconfigure(self._shape, fill=color)
        self.body.configure(bg=color)
        for child in self.body.winfo_children():
            if isinstance(child, tk.Label):
                child.configure(bg=color)

class RoundedButton(tk.Canvas):
    """Rounded native-Tk control supporting keyboard focus and disabled state."""
    def __init__(self, parent, *, text, command, bg, fg, activebackground, activeforeground,
                 font, padx=16, pady=10):
        super().__init__(parent, bg=parent.cget("background"), bd=0, highlightthickness=0,
                         takefocus=1, cursor="hand2")
        self._text, self._command = text, command
        self._fill, self._hover_fill, self._foreground = bg, activebackground, fg
        self._font = tkfont.Font(self, font=font)
        self._padx, self._pady, self._anchor = padx, pady, "center"
        self._state, self._hovered, self._focused, self._pressed = "normal", False, False, False
        self._shape = self.create_polygon(0,0,0,0, smooth=True, splinesteps=32, width=1)
        self._label = self.create_text(0,0, text=text, font=self._font, fill=fg)
        self.bind("<Configure>", lambda event: self._draw())
        self.bind("<Enter>", lambda event: self._hover(True))
        self.bind("<Leave>", lambda event: self._hover(False))
        self.bind("<FocusIn>", lambda event: self._focus(True))
        self.bind("<FocusOut>", lambda event: self._focus(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Return>", self._keyboard)
        self.bind("<space>", self._keyboard)
        self._measure()

    def _measure(self):
        super().configure(width=self._font.measure(self._text)+2*self._padx,
                          height=self._font.metrics("linespace")+2*self._pady)
        self._draw()

    def _draw(self):
        width, height = self.winfo_width(), self.winfo_height()
        enabled = self._state != "disabled"
        color = self._hover_fill if self._hovered and enabled else self._fill
        self.coords(self._shape, *_rounded_points(width, height, 12, inset=2))
        focus_color = "#94baa5" if self._fill == COLORS["accent"] else COLORS["accent"]
        self.itemconfigure(self._shape, fill=color,
            outline=focus_color if self._focused and enabled else color,
            width=2 if self._focused and enabled else 1)
        self.coords(self._label, self._padx if self._anchor == "w" else width-self._padx if self._anchor == "e" else width/2, height/2)
        self.itemconfigure(self._label, text=self._text, font=self._font, anchor=self._anchor,
                           fill=self._foreground if enabled else "#97aaa0")

    def configure(self, cnf=None, **kwargs):
        options = dict(cnf or {})
        options.update(kwargs)
        if not options:
            return super().configure()
        for key, value in options.items():
            if key in {"bg", "background"}:
                self._fill = value
            elif key in {"fg", "foreground"}:
                self._foreground = value
            elif key == "font":
                self._font = tkfont.Font(self, font=value)
            elif key in {"text", "command", "padx", "pady", "anchor", "state"}:
                setattr(self, "_"+key, value)
            elif key == "activebackground":
                self._hover_fill = value
            elif key != "activeforeground":
                super().configure(**{key:value})
        super().configure(cursor="arrow" if self._state == "disabled" else "hand2",
                          takefocus=0 if self._state == "disabled" else 1)
        self._measure()

    config = configure

    def cget(self, key):
        if key in {"state", "text", "command", "padx", "pady", "anchor"}:
            return getattr(self, "_"+key)
        return super().cget(key)

    def set_hover_colors(self, normal, hover):
        self._fill, self._hover_fill = normal, hover
        self._draw()

    def _hover(self, inside):
        self._hovered = inside
        if not inside:
            self._pressed = False
        self._draw()

    def _focus(self, focused):
        self._focused = focused
        self._draw()

    def _press(self, event):
        if self._state != "disabled":
            self._pressed = True
            self.focus_set()

    def _release(self, event):
        pressed, self._pressed = self._pressed, False
        if pressed and 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height():
            self.invoke()

    def _keyboard(self, event):
        self.invoke()
        return "break"

    def invoke(self):
        if self._state != "disabled" and self._command:
            return self._command()

def markdown_segments(text):
    """Small safe text renderer: formatting only, never HTML or script execution."""
    segments, code = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            code = not code
            continue
        if code:
            segments.append((line + "\n", "code"))
            continue
        heading = re.match(r"^#{1,4}\s+(.+)$", line)
        if heading:
            segments.append((heading.group(1) + "\n", "heading"))
            continue
        if re.fullmatch(r"\s*[-*_]{3,}\s*", line):
            segments.append(("\n", "body"))
            continue
        line = re.sub(r"^\s*[-*]\s+", "  •  ", line)
        for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", line):
            if part.startswith("**") and part.endswith("**"):
                segments.append((part[2:-2], "strong"))
            elif part.startswith("`") and part.endswith("`"):
                segments.append((part[1:-1], "inline_code"))
            elif part:
                segments.append((part, "body"))
        segments.append(("\n", "body"))
    return segments

class AsyncLoopBridge:
    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self.loop = asyncio.new_event_loop()
        self.events = queue.Queue()
        self.closed = False
        self.futures = set()
        self._poll_id = root.after(30, self._drain)
        self.thread = threading.Thread(target=self._run, name="property-assistant-async", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
        pending=asyncio.all_tasks(self.loop)
        for task in pending:task.cancel()
        if pending:self.loop.run_until_complete(asyncio.gather(*pending,return_exceptions=True))
        self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        self.loop.close()

    def dispatch(self, callback):
        if not self.closed:
            self.events.put(callback)

    def _drain(self):
        if self.closed:
            return
        for _ in range(100):
            try:
                callback = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception:
                # One failed view callback must not stop delivery of later results.
                logging.getLogger(__name__).exception("UI callback failed")
        self._poll_id = self.root.after(30, self._drain)

    def submit(self, coroutine, on_success, on_error) -> None:
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        self.futures.add(future)

        def finished(done) -> None:
            self.futures.discard(done)
            try:
                result = done.result()
            except BaseException as exc:  # noqa: BLE001
                self.dispatch(lambda error=exc: on_error(error))
            else:
                self.dispatch(lambda value=result: on_success(value))

        future.add_done_callback(finished)

    def shutdown(self, close_coroutine=None) -> None:
        self.closed = True
        self.root.after_cancel(self._poll_id)
        for future in tuple(self.futures):future.cancel()
        if close_coroutine is not None:
            try:
                asyncio.run_coroutine_threadsafe(close_coroutine, self.loop).result(timeout=8)
            except Exception:  # noqa: BLE001,S110 - the process is closing regardless.
                pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
