#!/usr/bin/env python3
"""
ZeroxAI VPN — Desktop VPN Client
Modern white/black design. Cross-platform (Windows, Mac, Linux).
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
import io

APP_NAME = "ZeroxAI VPN"
BG_COLOR = "#FFFFFF"
FG_COLOR = "#000000"
ACCENT = "#2563EB"
ACCENT_HOVER = "#1D4ED8"
SUCCESS = "#16A34A"
ERROR = "#DC2626"
GRAY = "#F3F4F6"
TEXT_SECONDARY = "#6B7280"
BORDER = "#E5E7EB"

CONFIG_STORE = "conf_cache.json"


def load_configs():
    try:
        with open(CONFIG_STORE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_configs(configs):
    with open(CONFIG_STORE, "w") as f:
        json.dump(configs, f, indent=2)


def get_public_ip():
    services = ["https://api.ipify.org", "https://icanhazip.com", "https://checkip.amazonaws.com"]
    for svc in services:
        try:
            req = urllib.request.Request(svc, headers={"User-Agent": "curl/8.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                ip = r.read().decode().strip()
                if ip:
                    return ip
        except Exception:
            continue
    return "—"


class RoundedButton(tk.Canvas):
    def __init__(self, parent, text, command=None, color=ACCENT, width=280, height=48, **kwargs):
        super().__init__(parent, width=width, height=height, bg=BG_COLOR, highlightthickness=0, **kwargs)
        self.command = command
        self.color = color
        self.text = text
        self.width = width
        self.height = height
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self._draw()

    def _draw(self, hover=False):
        self.delete("all")
        color = ACCENT_HOVER if hover else self.color
        r = 12
        self.create_rounded_rect(1, 1, self.width - 1, self.height - 1, r, fill=color, outline=color)
        self.create_text(self.width // 2, self.height // 2, text=self.text, fill="#FFFFFF",
                         font=("Segoe UI", 12, "bold"))

    def create_rounded_rect(self, x1, y1, x2, y2, r, **kwargs):
        points = []
        for coord in [(x1 + r, y1), (x2 - r, y1), (x2, y1 + r), (x2, y2 - r),
                      (x2 - r, y2), (x1 + r, y2), (x1, y2 - r), (x1, y1 + r)]:
            points.extend(coord)
        return self.create_polygon(points, smooth=True, **kwargs)

    def _on_click(self, event):
        if self.command:
            self.command()

    def _on_enter(self, event):
        self._draw(hover=True)

    def _on_leave(self, event):
        self._draw(hover=False)


class ZeroxaiVPN:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("480x700")
        self.root.configure(bg=BG_COLOR)
        self.root.minsize(400, 600)

        try:
            self.root.iconbitmap(default="")
        except Exception:
            pass

        self.configs = load_configs()
        self.current_config = None
        self.connected = False
        self.monitor_running = False
        self.user_ip = get_public_ip()

        self._setup_styles()
        self._build_ui()
        self._animate_in()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background=BG_COLOR)
        style.configure("TLabel", background=BG_COLOR, foreground=FG_COLOR, font=("Segoe UI", 10))
        style.configure("TButton", background=ACCENT, foreground="#FFFFFF", font=("Segoe UI", 10, "bold"),
                        borderwidth=0, focuscolor="none", relief="flat")
        style.map("TButton", background=[("active", ACCENT_HOVER)])
        style.configure("Treeview", background=BG_COLOR, foreground=FG_COLOR, fieldbackground=BG_COLOR,
                        borderwidth=0, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"), background=GRAY, foreground=FG_COLOR)

    def _build_ui(self):
        # Main container with padding
        self.main = tk.Frame(self.root, bg=BG_COLOR)
        self.main.pack(fill="both", expand=True, padx=24, pady=20)

        # Header
        header_frame = tk.Frame(self.main, bg=BG_COLOR)
        header_frame.pack(fill="x", pady=(0, 20))

        tk.Label(header_frame, text="ZeroxAI", font=("Segoe UI", 22, "bold"),
                 bg=BG_COLOR, fg=FG_COLOR).pack(anchor="w")
        tk.Label(header_frame, text="VPN Client", font=("Segoe UI", 12),
                 bg=BG_COLOR, fg=TEXT_SECONDARY).pack(anchor="w")

        # Status card
        self.status_frame = tk.Frame(self.main, bg=GRAY, highlightbackground=BORDER, highlightthickness=1)
        self.status_frame.pack(fill="x", pady=(0, 20))

        self.status_dot = tk.Canvas(self.status_frame, width=16, height=16, bg=GRAY, highlightthickness=0)
        self.status_dot.pack(pady=(16, 0))
        self._draw_dot(ERROR)

        self.status_label = tk.Label(self.status_frame, text="Отключено", font=("Segoe UI", 14, "bold"),
                                     bg=GRAY, fg=ERROR)
        self.status_label.pack(pady=(8, 4))

        self.ip_label = tk.Label(self.status_frame, text=f"IP: {self.user_ip}", font=("Segoe UI", 10),
                                 bg=GRAY, fg=TEXT_SECONDARY)
        self.ip_label.pack(pady=(0, 4))

        self.server_label = tk.Label(self.status_frame, text="", font=("Segoe UI", 9),
                                     bg=GRAY, fg=TEXT_SECONDARY)
        self.server_label.pack(pady=(0, 16))

        # Connect / Disconnect button
        self.connect_btn = RoundedButton(self.main, text="Подключиться", command=self._toggle_connect,
                                         color=ACCENT, width=432, height=52)
        self.connect_btn.pack(pady=(0, 20))

        # Configs list
        list_header = tk.Frame(self.main, bg=BG_COLOR)
        list_header.pack(fill="x")

        tk.Label(list_header, text="Конфигурации", font=("Segoe UI", 12, "bold"),
                 bg=BG_COLOR, fg=FG_COLOR).pack(side="left")

        add_btn = tk.Label(list_header, text="+ Добавить", font=("Segoe UI", 10),
                           bg=BG_COLOR, fg=ACCENT, cursor="hand2")
        add_btn.pack(side="right")
        add_btn.bind("<Button-1>", lambda e: self._import_config())

        # Listbox with custom styling
        self.list_frame = tk.Frame(self.main, bg=BG_COLOR, highlightbackground=BORDER, highlightthickness=1)
        self.list_frame.pack(fill="both", expand=True, pady=(8, 0))

        self.config_listbox = tk.Listbox(self.list_frame, bg=BG_COLOR, fg=FG_COLOR,
                                         selectbackground=GRAY, selectforeground=FG_COLOR,
                                         borderwidth=0, highlightthickness=0,
                                         font=("Segoe UI", 10), activestyle="none")
        self.config_listbox.pack(side="left", fill="both", expand=True, padx=4, pady=4)

        scrollbar = tk.Scrollbar(self.list_frame, orient="vertical", command=self.config_listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.config_listbox.config(yscrollcommand=scrollbar.set)

        self.config_listbox.bind("<<ListboxSelect>>", self._on_config_select)
        self.config_listbox.bind("<Double-Button-1>", lambda e: self._toggle_connect())

        # Delete button
        del_btn = tk.Label(self.main, text="🗑 Удалить выбранное", font=("Segoe UI", 9),
                           bg=BG_COLOR, fg=ERROR, cursor="hand2")
        del_btn.pack(pady=(4, 0), anchor="e")
        del_btn.bind("<Button-1>", lambda e: self._delete_config())

        self._refresh_list()

    def _draw_dot(self, color):
        self.status_dot.delete("all")
        r = 6
        self.status_dot.create_oval(8 - r, 8 - r, 8 + r, 8 + r, fill=color, outline=color)

    def _animate_in(self):
        self.root.attributes("-alpha", 0.0)
        for i in range(1, 11):
            self.root.after(i * 20, lambda v=i / 10: self.root.attributes("-alpha", v))

    def _animate_status(self, to_connected):
        colors = [ERROR, SUCCESS] if to_connected else [SUCCESS, ERROR]
        target_color = colors[1]
        self.status_dot.delete("all")
        self._animate_dot(target_color, 5)

    def _animate_dot(self, target_color, steps):
        if steps <= 0:
            self._draw_dot(target_color)
            return
        self._draw_dot(GRAY)
        self.root.after(50, lambda: self._draw_dot(target_color))

    def _refresh_list(self):
        self.config_listbox.delete(0, tk.END)
        for cfg in self.configs:
            name = cfg.get("name", cfg.get("country", "Без имени"))
            flag = cfg.get("flag", "🌍")
            self.config_listbox.insert(tk.END, f"{flag} {name}")

    def _import_config(self):
        path = filedialog.askopenfilename(
            title="Выберите .conf файл",
            filetypes=[("WireGuard Config", "*.conf"), ("All Files", "*.*")]
        )
        if not path:
            return
        try:
            with open(path, "r") as f:
                content = f.read()
            name = os.path.splitext(os.path.basename(path))[0]
            # Extract country from name or content
            country = name.replace("zeroxai_vpn_", "").replace("_", " ").title()
            cfg = {
                "name": name,
                "country": country,
                "flag": "🌍",
                "content": content,
                "path": path,
            }
            self.configs.append(cfg)
            save_configs(self.configs)
            self._refresh_list()
            self._show_toast("✅ Конфиг импортирован")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось импортировать:\n{e}")

    def _on_config_select(self, event):
        sel = self.config_listbox.curselection()
        if sel:
            idx = sel[0]
            if idx < len(self.configs):
                self.current_config = self.configs[idx]

    def _toggle_connect(self):
        if not self.current_config:
            if self.configs:
                self.current_config = self.configs[0]
                self.config_listbox.selection_set(0)
            else:
                messagebox.showinfo("Инфо", "Сначала импортируйте .conf файл\n\n"
                                   "1. Подключитесь к серверу в Telegram боте\n"
                                   "2. Нажмите «Скачать .conf»\n"
                                   "3. Откройте скачанный файл через это приложение")
                return

        if self.connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        self.status_label.config(text="Подключение...", fg=ACCENT)
        self.status_dot.delete("all")
        self._animate_dot(ACCENT, 5)
        self.connect_btn.config(state="disabled")
        self.root.update()

        def do_connect():
            cfg = self.current_config
            path = cfg.get("path")
            content = cfg.get("content", "")

            # Save to temp file if no path
            if not path or not os.path.exists(path):
                path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_wg.conf")
                with open(path, "w") as f:
                    f.write(content)
                cfg["path"] = path

            # Try to launch WireGuard
            launched = False
            wg_commands = []
            if sys.platform == "win32":
                wg_commands = [
                    f'start "" wireguard /config="{path}"',
                    f'start "" "C:\\Program Files\\WireGuard\\wireguard.exe" /config="{path}"',
                    f'rundll32.exe url.dll,FileProtocolHandler "{path}"',
                ]
            elif sys.platform == "darwin":
                wg_commands = [f'open -a "WireGuard" "{path}"', f'open "{path}"']
            else:
                wg_commands = [f'xdg-open "{path}"', f'wg-quick up "{path}"']

            for cmd in wg_commands:
                try:
                    subprocess.Popen(cmd, shell=True)
                    launched = True
                    break
                except Exception:
                    continue

            self.root.after(0, lambda: self._on_connect_result(launched))

        threading.Thread(target=do_connect, daemon=True).start()

    def _on_connect_result(self, launched):
        if launched:
            self.connected = True
            self.status_label.config(text="Подключено ✓", fg=SUCCESS)
            self._draw_dot(SUCCESS)
            self.connect_btn.text = "Отключиться"
            self.connect_btn._draw()

            flag = self.current_config.get("flag", "🌍")
            country = self.current_config.get("country", "")
            self.server_label.config(text=f"{flag} {country}")

            # Start IP monitoring
            if not self.monitor_running:
                self.monitor_running = True
                threading.Thread(target=self._monitor_ip, daemon=True).start()

            self._show_toast("✅ VPN подключён")
            # Start pulse animation
            self._pulse_dot(SUCCESS)
        else:
            self.status_label.config(text="Ошибка подключения", fg=ERROR)
            self._draw_dot(ERROR)
            self._show_toast("❌ Не удалось открыть WireGuard\nУстановите WireGuard: wireguard.com")
        self.connect_btn.config(state="normal")

    def _disconnect(self):
        self.connected = False
        self.status_label.config(text="Отключено", fg=ERROR)
        self._draw_dot(ERROR)
        self.server_label.config(text="")
        self.connect_btn.text = "Подключиться"
        self.connect_btn._draw()
        self._show_toast("🛑 VPN отключён")
        self.user_ip = get_public_ip()
        self.root.after(500, lambda: self.ip_label.config(text=f"IP: {self.user_ip}"))

    def _monitor_ip(self):
        while self.connected and self.monitor_running:
            ip = get_public_ip()
            if ip and ip != self.user_ip:
                self.user_ip = ip
                self.root.after(0, lambda: self.ip_label.config(text=f"IP: {self.user_ip}"))
            time.sleep(5)

    def _pulse_dot(self, color):
        if not self.connected:
            return

        def pulse(step):
            if not self.connected:
                return
            opacity = 0.4 + 0.6 * abs((step % 20) - 10) / 10
            self._draw_dot(color)
            self.root.after(100, lambda: pulse((step + 1) % 20))

        pulse(0)

    def _delete_config(self):
        sel = self.config_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx < len(self.configs):
            if messagebox.askyesno("Удалить", "Удалить этот конфиг?"):
                del self.configs[idx]
                save_configs(self.configs)
                self._refresh_list()
                self.current_config = None
                if self.connected:
                    self._disconnect()
                self._show_toast("🗑 Конфиг удалён")

    def _show_toast(self, text):
        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        toast.configure(bg=FG_COLOR)

        label = tk.Label(toast, text=text, fg=BG_COLOR, bg=FG_COLOR,
                         font=("Segoe UI", 10), padx=20, pady=10)
        label.pack()

        toast.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - toast.winfo_width()) // 2
        y = self.root.winfo_y() + self.root.winfo_height() - 80
        toast.geometry(f"+{x}+{y}")

        self.root.after(2000, toast.destroy)

    def _on_close(self):
        self.monitor_running = False
        save_configs(self.configs)
        self.root.destroy()


if __name__ == "__main__":
    ZeroxaiVPN()
