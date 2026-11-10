"""Window for selecting a user-supplied NUP (plus an optional update or DLC NUP) and compiling it locally."""
from pathlib import Path
import argparse
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compiler.backend import RESOLUTIONS, STATE


class Window:
    def __init__(self, root, bundle):
        self.root, self.bundle = root, bundle
        self.process = None
        self.events = queue.Queue()
        self.nup, self.output = tk.StringVar(), tk.StringVar()
        self.update_nup, self.dlc_nup = tk.StringVar(), tk.StringVar()   # optional; empty = not used
        self.resolution = tk.StringVar(value="1920x1080")
        self.unlock = tk.BooleanVar(value=True)
        self.jobs = tk.IntVar(value=0)
        self.status = tk.StringVar(value="Select your game NUP and a new or existing HWAoI output folder.")
        root.title("Hyrule Warriors AOI Recomp Optimized - Compiler")
        root.geometry("860x790")
        root.minsize(740, 700)
        style = ttk.Style(root)
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")
        style.configure("Title.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Sub.TLabel", foreground="#525963")
        panel = ttk.Frame(root, padding=24)
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(1, weight=1)
        panel.rowconfigure(13, weight=1)
        ttk.Label(panel, text="Hyrule Warriors AOI Recomp Optimized", style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(panel, text="Build the experimental Windows version from your own game files.", style="Sub.TLabel").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(5, 22))
        self.fields = []
        panel.columnconfigure(3, weight=0)
        for row, label, var, picker in ((2, "Game file (.nup)", self.nup, self.pick_nup),
                                        (3, "Update file (.nup, optional)", self.update_nup, self.pick_update),
                                        (4, "DLC file (.nup, optional)", self.dlc_nup, self.pick_dlc),
                                        (6, "Output folder", self.output, self.pick_output)):
            ttk.Label(panel, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=7)
            entry = ttk.Entry(panel, textvariable=var)
            entry.grid(row=row, column=1, sticky="ew")
            button = ttk.Button(panel, text="Browse...", command=picker)
            button.grid(row=row, column=2, padx=(10, 0))
            self.fields.extend((entry, button))
            if var in (self.update_nup, self.dlc_nup):
                clear = ttk.Button(panel, text="Clear", width=6, command=lambda v=var: v.set(""))
                clear.grid(row=row, column=3, padx=(6, 0))
                self.fields.append(clear)
        ttk.Label(panel, style="Sub.TLabel", wraplength=780, text=
                  "Leave Update and DLC blank if your game file already includes them. Use Update only when the game "
                  "file is the base game and the update is a separate NUP.").grid(
                      row=5, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Label(panel, text="Native resolution").grid(row=7, column=0, sticky="w", pady=9)
        resolution = ttk.Combobox(panel, textvariable=self.resolution, values=RESOLUTIONS, state="readonly", width=18)
        resolution.grid(row=7, column=1, sticky="w")
        self.fields.append(resolution)
        options = ttk.Frame(panel)
        options.grid(row=8, column=0, columnspan=4, sticky="w", pady=(4, 5))
        unlock = ttk.Checkbutton(options, text="Allow 90 / 120 FPS settings (they speed up the game)", variable=self.unlock)
        unlock.pack(side="left")
        ttk.Label(options, text="    Build jobs (0 = auto)").pack(side="left")
        jobs = ttk.Spinbox(options, from_=0, to=8, width=3, textvariable=self.jobs)
        jobs.pack(side="left", padx=7)
        self.fields.extend((unlock, jobs))
        ttk.Label(panel, foreground="#875300", wraplength=780, text=
                  "90 / 120 FPS speed up the whole game (1.5x / 2x): gameplay advances a fixed step per frame. "
                  "Play at 60 FPS for normal speed, and use frame generation (e.g. Lossless Scaling 2x) for smoother "
                  "motion. Higher resolutions are unstable.").grid(row=9, column=0, columnspan=4, sticky="w", pady=(5, 9))
        ttk.Label(panel, style="Sub.TLabel", wraplength=780, text=
                  "Windows 10/11 x64, AVX2 CPU and Vulkan graphics required. Keep the game NUP after compiling "
                  "(update and DLC files are copied into the output). "
                  "Allow at least 20 GiB of free output space; compiling may take a while.").grid(
                      row=10, column=0, columnspan=4, sticky="w", pady=(0, 14))
        actions = ttk.Frame(panel)
        actions.grid(row=11, column=0, columnspan=4, sticky="ew")
        self.build_button = ttk.Button(actions, text="Compile / update", command=self.compile)
        self.build_button.pack(side="left")
        self.launch_button = ttk.Button(actions, text="Open compiled game", command=self.launch)
        self.launch_button.pack(side="left", padx=9)
        self.cancel_button = ttk.Button(actions, text="Cancel build", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="right")
        ttk.Label(panel, textvariable=self.status, wraplength=780).grid(row=12, column=0, columnspan=4, sticky="w", pady=(15, 7))
        self.log = ScrolledText(panel, height=13, font=("Consolas", 9), state="disabled", background="#f5f6f8", relief="flat")
        self.log.grid(row=13, column=0, columnspan=4, sticky="nsew")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    def pick_nup(self):
        value = filedialog.askopenfilename(title="Select your HWAoI NUP", filetypes=[("NUP game package", "*.nup")])
        if value:
            self.nup.set(value)

    def pick_update(self):
        value = filedialog.askopenfilename(title="Select the game's update NUP (optional)",
                                           filetypes=[("NUP game package", "*.nup")])
        if value:
            self.update_nup.set(value)

    def pick_dlc(self):
        value = filedialog.askopenfilename(title="Select the game's DLC NUP (optional)",
                                           filetypes=[("NUP game package", "*.nup")])
        if value:
            self.dlc_nup.set(value)

    def pick_output(self):
        parent = filedialog.askdirectory(title="Select an existing HWAoI output or a parent folder for a new build")
        if parent:
            selected = Path(parent) if (Path(parent) / STATE).is_file() else Path(parent) / "HWAoI-Game"
            self.output.set(str(selected))
            if (selected / STATE).is_file():
                try:
                    state = json.loads((selected / STATE).read_text(encoding="utf-8"))
                    if not self.nup.get():
                        self.nup.set(state.get("nup", ""))
                        self.update_nup.set(state.get("update_nup") or "")
                        self.dlc_nup.set(state.get("dlc_nup") or "")
                    self.resolution.set(state.get("resolution", "1920x1080"))
                    self.unlock.set(state.get("rate_unlock", True))
                except (OSError, ValueError):
                    pass

    def busy(self, on):
        for field in self.fields:
            field.configure(state="disabled" if on else "readonly" if isinstance(field, ttk.Combobox) else "normal")
        for button in (self.build_button, self.launch_button):
            button.configure(state="disabled" if on else "normal")
        self.cancel_button.configure(state="normal" if on else "disabled")

    def append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        if int(self.log.index("end-1c").split(".")[0]) > 1400:
            self.log.delete("1.0", "400.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self, action, extra=()):
        command = [str(self.bundle / "python/python.exe"), "-E", "-s", str(self.bundle / "source/compiler/backend.py"),
                   action, "--bundle", str(self.bundle), "--output", self.output.get(), *extra]
        try:
            self.process = subprocess.Popen(command, cwd=self.bundle, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError as error:
            messagebox.showerror("Cannot start compiler", str(error))
            return
        self.busy(True)
        self.status.set("Compiling locally..." if action == "build" else "Checking and opening the compiled game...")
        proc = self.process
        def reader():
            for line in proc.stdout:
                self.events.put(("line", line))
            proc.stdout.close()
            self.events.put(("done", (proc.wait(), action)))
        threading.Thread(target=reader, daemon=True).start()

    def compile(self):
        try:
            if not Path(self.nup.get()).is_file() or not self.output.get().strip():
                raise ValueError("Choose your game NUP and output folder first.")
            for name, var in (("Update", self.update_nup), ("DLC", self.dlc_nup)):
                value = var.get().strip()
                if value and not Path(value).is_file():
                    raise ValueError(f"The {name} file does not exist. Choose it again, or click Clear to leave it empty.")
                if value and Path(value).resolve() == Path(self.nup.get()).resolve():
                    raise ValueError(f"The {name} file is the game file itself. Click Clear to leave it empty.")
            jobs = self.jobs.get()
            if not 0 <= jobs <= 8:
                raise ValueError("Build jobs must be 0 (automatic) or between 1 and 8.")
        except (ValueError, tk.TclError) as error:
            messagebox.showerror("Check your selection", str(error))
            return
        extra = ["--nup", self.nup.get(), "--jobs", str(jobs), "--resolution", self.resolution.get()]
        if self.update_nup.get().strip():
            extra += ["--update-nup", self.update_nup.get().strip()]
        if self.dlc_nup.get().strip():
            extra += ["--dlc-nup", self.dlc_nup.get().strip()]
        if self.unlock.get():
            extra.append("--unlock")
        self.start("build", extra)

    def launch(self):
        if not (Path(self.output.get()) / STATE).is_file():
            messagebox.showerror("Choose a completed build", "Set Output folder to the folder created by a completed compilation.")
            return
        self.start("launch")

    def poll(self):
        for _ in range(150):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "line":
                self.append(value)
                if value.startswith("[") and "/6]" in value:
                    self.status.set(value.strip())
            else:
                code, action = value
                self.process = None
                self.busy(False)
                self.status.set(("Build complete. You can open the compiled game." if action == "build" else "Game launched.")
                                if code == 0 else "Stopped. See the log above; existing output is kept for diagnosis.")
        self.root.after(100, self.poll)

    def cancel(self):
        if self.process and self.process.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.process.pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            self.status.set("Build cancelled. Compile in the same output folder to resume.")

    def close(self):
        self.cancel()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if os.name == "nt":
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    root = tk.Tk()
    window = Window(root, args.bundle.resolve())
    if args.selftest:
        # Complete mapping as well as idle geometry propagation before checking
        # nested controls; their initial 1x1 placeholders are not final bounds.
        root.update()
        root.update_idletasks()
        root.update()
        assert window.unlock.get() and window.resolution.get() == "1920x1080"
        assert window.jobs.get() == 0
        assert window.update_nup.get() == "" and window.dlc_nup.get() == ""
        for widget in (window.build_button, window.launch_button, window.cancel_button, window.log):
            x, y = widget.winfo_rootx() - root.winfo_rootx(), widget.winfo_rooty() - root.winfo_rooty()
            assert x >= 0 and y >= 0 and x + widget.winfo_width() <= root.winfo_width()
            assert y + widget.winfo_height() <= root.winfo_height()
        window.busy(True)
        assert str(window.build_button["state"]) == "disabled"
        window.busy(False)
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
