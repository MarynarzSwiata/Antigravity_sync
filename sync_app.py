import os
import zipfile
import getpass
import sys
import shutil
import socket
import glob
import threading
import time
import json
import queue
import re
from datetime import datetime, timedelta
import customtkinter as ctk
from tkinter import messagebox, filedialog
from PIL import Image, ImageDraw
import pystray # type: ignore

# ==================== CONFIGURATION MANAGER ====================
CONFIG_FILE = "sync_config.json"
APP_VERSION = "1.0"
CHANGELOG = f"""
[{datetime.now().strftime('%Y-%m-%d')}] v1.0
- Initial Release
- GUI Implementation (CustomTkinter)
- Bidirectional Sync (Backup & Restore)
- Google Drive Integration
- Smart Scheduling & Tray Support
- Advanced Filtering & Retention Policy
"""

CONFIG_FILE = "sync_config.json"
DEFAULT_CONFIG = {
    "drive_path": "",
    "target_folders": [".gemini", ".antigravity"],
    "retention_count": 2,          # Max 7
    "compression_level": 5,        # 0-9
    "ignore_patterns": ["__pycache__", ".git", "*.tmp", "*.log", ".tmp.driveupload", "desktop.ini"],
    "scheduled_times": []          # List of "HH:MM" strings
}

class SyncConfig:
    def __init__(self):
        self.config = self.load_config()
        self.current_user = getpass.getuser()
        self.hostname = socket.gethostname()
        self.base_user_path = rf"C:\Users\{self.current_user}"
        self.file_prefix = "Antigravity_Backup"
        
        if not self.config["drive_path"]:
            self.detect_drive_path()

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    return json.load(f)
            except:
                return DEFAULT_CONFIG.copy()
        return DEFAULT_CONFIG.copy()

    def save_config(self):
        # Validate retention
        if self.config["retention_count"] > 7: self.config["retention_count"] = 7
        if self.config["retention_count"] < 1: self.config["retention_count"] = 1
        
        with open(CONFIG_FILE, 'w') as f:
            json.dump(self.config, f, indent=4)

    def detect_drive_path(self):
        possible_paths = [r"G:\Mój dysk", r"G:\My Drive"]
        for path in possible_paths:
            if os.path.exists(path):
                self.config["drive_path"] = os.path.join(path, "AntigravitySync")
                return
        self.config["drive_path"] = r"G:\Mój dysk\AntigravitySync"

    # Properties for easy access
    def get(self, key):
        return self.config.get(key, DEFAULT_CONFIG.get(key))

    def set(self, key, value):
        self.config[key] = value
        self.save_config()

# ==================== LOGIC CLASS ====================
class SyncLogic:
    def __init__(self, config_manager, msg_queue):
        self.cfg = config_manager
        self.queue = msg_queue
        self.stop_requested = False

    def log(self, text, type="info"):
        if self.queue:
            self.queue.put(("log", text))
        else:
            print(text) # Fallback for background scheduler if GUI closed

    def progress(self, val, msg, elapsed, eta):
        if self.queue:
            self.queue.put(("progress", (val, msg, elapsed, eta)))

    def finish(self, success, msg=""):
        if self.queue:
            self.queue.put(("finish", (success, msg)))

    def abort(self):
        self.stop_requested = True

    def format_time(self, seconds):
        return str(timedelta(seconds=int(seconds)))

    def ensure_drive(self):
        drive_path = self.cfg.get("drive_path")
        if not drive_path:
            self.log("ERROR: Drive path is not configured.", "error")
            return False

        if not os.path.exists(drive_path):
            try:
                os.makedirs(drive_path)
            except Exception as e:
                self.log(f"Could not create directory: {e}", "error")
                return False
        return True

    def should_ignore(self, path):
        patterns = self.cfg.get("ignore_patterns")
        # Check all parts of the path
        parts = path.replace('\\', '/').split('/')
        for part in parts:
            for p in patterns:
                if glob.fnmatch.fnmatch(part, p):
                    return True
        return False

    def cleanup_old_backups(self):
        count = self.cfg.get("retention_count")
        drive_path = self.cfg.get("drive_path")
        
        self.log(f"Cleaning up logic (Keep last {count})...")
        pattern = os.path.join(drive_path, f"{self.cfg.file_prefix}_*.zip")
        files = glob.glob(pattern)
        files.sort(reverse=True) 

        if len(files) > count:
            files_to_delete = files[count:]
            for f in files_to_delete:
                try:
                    os.remove(f)
                    self.log(f"Deleted old backup: {os.path.basename(f)}")
                except Exception as e:
                    self.log(f"Failed to delete {os.path.basename(f)}: {e}", "error")

    def scan_for_backups(self):
        if not self.ensure_drive(): return
        drive_path = self.cfg.get("drive_path")
        pattern = os.path.join(drive_path, f"{self.cfg.file_prefix}_*.zip")
        files = glob.glob(pattern)
        files.sort(reverse=True)
        
        if files:
            self.log(f"=== Found {len(files)} remote backups ===")
            for f in files:
                size_mb = os.path.getsize(f) / (1024 * 1024)
                self.log(f"📄 {os.path.basename(f)} ({size_mb:.2f} MB)")
            self.log("==================================")
        else:
            self.log("=== No remote backups found ===")

    def run_backup(self, silent=False):
        start_time = time.time()
        try:
            if not silent: self.log(f"--- Starting BACKUP ({self.cfg.hostname}) ---")
            if not self.ensure_drive():
                if not silent: self.finish(False, "Drive not available")
                return

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            final_zip_name = f"{self.cfg.file_prefix}_{self.cfg.hostname}_{timestamp}.zip"
            drive_path = self.cfg.get("drive_path")
            full_output_path = os.path.join(drive_path, final_zip_name)

            files_to_zip = []
            for folder in self.cfg.get("target_folders"):
                folder = folder.strip()
                if not folder: continue
                source_path = os.path.join(self.cfg.base_user_path, folder)
                if os.path.exists(source_path):
                    if not silent: self.log(f"Scanning: {source_path}")
                    for root, dirs, files in os.walk(source_path):
                        # Filter directories
                        dirs[:] = [d for d in dirs if not self.should_ignore(d)]
                        
                        for file in files:
                            if self.should_ignore(file):
                                continue
                                
                            full_file_path = os.path.join(root, file)
                            arcname = os.path.join(folder, os.path.relpath(full_file_path, source_path))
                            files_to_zip.append((full_file_path, arcname))
                else:
                    if not silent: self.log(f"Warning: Folder {folder} not found.", "warning")

            if not files_to_zip:
                if not silent: 
                    self.log("No files found to back up.", "error")
                    self.finish(False, "No files found")
                return

            if not silent: self.log(f"Creating archive: {final_zip_name}")
            total_files = len(files_to_zip)
            compression = self.cfg.get("compression_level")
            # Map 0-9 roughly to zipfile compression args (if using lzma/bzip2) 
            # or just use ZIP_DEFLATED level if python supported it easily, 
            # standard zipfile uses ZIP_DEFLATED (level 6 approx). 
            # Here we just use ZIP_DEFLATED as simple switch.
            
            with zipfile.ZipFile(full_output_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for idx, (src_file, zip_path) in enumerate(files_to_zip):
                    if self.stop_requested:
                        if not silent: self.log("Backup Cancelled.")
                        zip_file.close() 
                        try: os.remove(full_output_path)
                        except: pass
                        if not silent: self.finish(False, "Cancelled")
                        return

                    try:
                        zip_file.write(src_file, zip_path)
                    except Exception as fe:
                        if not silent: self.log(f"Error skipping file {src_file}: {fe}", "error")
                    
                    elapsed = time.time() - start_time
                    progress_val = (idx + 1) / total_files
                    
                    eta_str = "--:--"
                    if progress_val > 0:
                        total_estimated_time = elapsed / progress_val
                        remaining_time = total_estimated_time - elapsed
                        eta_str = self.format_time(remaining_time)
                    
                    if not silent: self.progress(progress_val, "", self.format_time(elapsed), eta_str)

            if not silent: self.log(f"[SUCCESS] Backup saved to: {full_output_path}")
            self.cleanup_old_backups()
            if not silent: self.finish(True, "Backup Complete")
            return True

        except Exception as e:
            if not silent: 
                self.log(f"[CRITICAL ERROR] Failed to create ZIP: {e}", "error")
                self.finish(False, str(e))
            return False

    def run_restore(self):
        start_time = time.time()
        try:
            self.log(f"--- Starting RESTORE ---")
            if not self.ensure_drive():
                self.finish(False, "Drive not available")
                return
            
            drive_path = self.cfg.get("drive_path")
            pattern = os.path.join(drive_path, f"{self.cfg.file_prefix}_*.zip")
            files = glob.glob(pattern)
            
            if not files:
                self.log("No backup files found in Drive.", "error")
                self.finish(False, "No backups found")
                return
            
            files.sort(reverse=True)
            latest_backup = files[0]
            filename = os.path.basename(latest_backup)
            self.log(f"Found latest backup: {filename}")
            
            # --- Smart Check Preview ---
            self.log("Analyzing archive...")
            try:
                with zipfile.ZipFile(latest_backup, 'r') as zf:
                    file_list = zf.namelist()
                    total_files = len(file_list)
                    conflicts_found = 0
                    
                    # First pass: Check conflicts
                    # First pass: Check conflicts and update progress slightly
                    for i, file in enumerate(file_list):
                        # Show some activity during analysis (0-10%)
                        # Show some activity during analysis (0-10%)
                        an_prog = (i / total_files) * 0.1
                        self.progress(an_prog, "Analyzing...", self.format_time(time.time()-start_time), "--:--")

                        dest_path = os.path.join(self.cfg.base_user_path, file)
                        if os.path.exists(dest_path):
                            # Compare times. Zip stores time as tuple (Y,M,D,H,M,S)
                            # Logic: If local file mtime > zip time, warn.
                            pass # For now overly complex to do precise per-file prompt.
                            
                    # Extract
                    for idx, file in enumerate(file_list):
                        if self.stop_requested:
                            self.finish(False, "Cancelled")
                            return

                        if file.startswith("..") or os.path.isabs(file):
                            continue

                        if self.should_ignore(file):
                            # self.log(f"Skipping ignored: {file}")
                            continue
                        
                        try:
                            zf.extract(file, self.cfg.base_user_path)
                        except PermissionError:
                             self.log(f"⚠️ SKIPPED (Locked): {file}", "error")
                             continue
                        except Exception as e:
                             self.log(f"⚠️ Error extracting {file}: {e}", "error")
                             continue
                        
                        elapsed = time.time() - start_time
                        # Map actual extraction to 10-100% range
                        # progress_val = (idx + 1) / total_files 
                        progress_val = 0.1 + ((idx + 1) / total_files * 0.9)
                        
                        eta_str = "--:--"
                        eta_str = "--:--"
                        if progress_val > 0.1: # Avoid division by zero or tiny numbers
                             # Calculate based on the extraction part (0.1 to 1.0 range)
                             scaled_prog = (progress_val - 0.1) / 0.9
                             if scaled_prog > 0:
                                total_estimated = (time.time() - start_time) / progress_val # Total time based on overall progress
                                rem = total_estimated - (time.time() - start_time)
                                eta_str = self.format_time(max(0, rem))
                        
                        self.progress(progress_val, f"Copying files... ({idx+1}/{total_files})", self.format_time(elapsed), eta_str)
                        time.sleep(0.005) # Force UI breather

            except Exception as e:
                self.log(f"Zip Error: {e}")
                self.finish(False, str(e))
                return

            self.log(f"[SUCCESS] Restore completed from {filename}")
            self.finish(True, "Restore Complete")

        except Exception as e:
            self.log(f"[CRITICAL ERROR] Restore failed: {e}", "error")
            self.finish(False, str(e))


# ==================== SETTINGS DIALOG ====================
class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, parent, config_manager):
        super().__init__(parent)
        self.cfg = config_manager
        self.title("Settings")
        self.geometry("450x550")
        self.grid_columnconfigure(0, weight=1)

        # Drive Path
        ctk.CTkLabel(self, text="Google Drive Path:", anchor="w").grid(row=0, column=0, padx=20, pady=(10,0), sticky="ew")
        self.entry_path = ctk.CTkEntry(self)
        self.entry_path.grid(row=1, column=0, padx=20, pady=5, sticky="ew")
        self.entry_path.insert(0, self.cfg.get("drive_path"))
        ctk.CTkButton(self, text="Browse...", command=self.browse_folder, width=80).grid(row=2, column=0, padx=20, pady=5, sticky="e")

        # Folders
        ctk.CTkLabel(self, text="Target Folders (csv):", anchor="w").grid(row=3, column=0, padx=20, pady=(10,0), sticky="ew")
        self.textbox_folders = ctk.CTkTextbox(self, height=60)
        self.textbox_folders.grid(row=4, column=0, padx=20, pady=5, sticky="ew")
        self.textbox_folders.insert("1.0", ", ".join(self.cfg.get("target_folders")))

        # Ignored
        ctk.CTkLabel(self, text="Ignored Patterns (csv):", anchor="w").grid(row=5, column=0, padx=20, pady=(10,0), sticky="ew")
        self.textbox_ignore = ctk.CTkTextbox(self, height=60)
        self.textbox_ignore.grid(row=6, column=0, padx=20, pady=5, sticky="ew")
        self.textbox_ignore.insert("1.0", ", ".join(self.cfg.get("ignore_patterns")))

        # Scheduled Times
        ctk.CTkLabel(self, text="Scheduled Times (HH:MM, csv):", anchor="w").grid(row=7, column=0, padx=20, pady=(10,0), sticky="ew")
        self.entry_schedule = ctk.CTkEntry(self)
        self.entry_schedule.grid(row=8, column=0, padx=20, pady=5, sticky="ew")
        self.entry_schedule.insert(0, ", ".join(self.cfg.get("scheduled_times")))

        # Retention & Compression
        self.frame_opts = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_opts.grid(row=9, column=0, padx=20, pady=10, sticky="ew")
        
        ctk.CTkLabel(self.frame_opts, text="Keep Max Backups (1-7):").pack(side="left")
        self.spin_retention = ctk.CTkEntry(self.frame_opts, width=40)
        self.spin_retention.pack(side="left", padx=10)
        self.spin_retention.insert(0, str(self.cfg.get("retention_count")))

        # Save
        self.btn_save = ctk.CTkButton(self, text="Save Settings", command=self.save_settings, fg_color="#2CC985", hover_color="#229965")
        self.btn_save.grid(row=10, column=0, padx=20, pady=20, sticky="ew")

    def browse_folder(self):
        path = filedialog.askdirectory(initialdir=self.entry_path.get())
        if path:
            self.entry_path.delete(0, "end")
            self.entry_path.insert(0, path)

    def save_settings(self):
        # Path
        self.cfg.set("drive_path", self.entry_path.get().strip())
        
        # Lists
        self.cfg.set("target_folders", [x.strip() for x in self.textbox_folders.get("1.0", "end").split(",") if x.strip()])
        self.cfg.set("ignore_patterns", [x.strip() for x in self.textbox_ignore.get("1.0", "end").split(",") if x.strip()])
        
        # Schedule
        raw_times = self.entry_schedule.get().split(",")
        valid_times = []
        for t in raw_times:
            t = t.strip()
            if re.match(r"^\d{2}:\d{2}$", t):
                valid_times.append(t)
        self.cfg.set("scheduled_times", valid_times)

        # Retention
        try:
            r = int(self.spin_retention.get())
            self.cfg.set("retention_count", r)
        except: pass

        messagebox.showinfo("Settings", "Configuration saved!")
        self.destroy()

class InfoDialog(ctk.CTkToplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("About")
        self.geometry("400x500")
        
        # Logo/Title
        ctk.CTkLabel(self, text="Antigravity Sync", font=("Roboto", 24, "bold")).pack(pady=(20, 5))
        ctk.CTkLabel(self, text=f"Version {APP_VERSION}", font=("Roboto", 14), text_color="gray").pack(pady=(0, 20))      
      
        # Changelog
        ctk.CTkLabel(self, text="Changelog:", anchor="w").pack(padx=20, pady=(20, 5), fill="x")
        self.txt_change = ctk.CTkTextbox(self, height=200)
        self.txt_change.pack(padx=20, fill="both", expand=True)
        self.txt_change.insert("1.0", CHANGELOG.strip())
        self.txt_change.configure(state="disabled")

        ctk.CTkButton(self, text="Close", command=self.destroy).pack(pady=20)


# ==================== MAIN APPLICATION ====================
class SyncApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.cfg = SyncConfig()
        self.queue = queue.Queue()
        self.is_minimized = False
        self.tray_icon = None

        # Window Setup
        self.title(f"Antigravity Sync {APP_VERSION}")
        self.geometry("600x550")
        ctk.set_appearance_mode("Dark")
        self.protocol('WM_DELETE_WINDOW', self.on_closing)

        self.setup_ui()
        self.setup_scheduler()
        
        # Periodic Queue Check
        self.after(100, self.check_queue)
        
        self.log_to_ui(f"Ready. Target: {self.cfg.get('drive_path')}")

    def setup_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # 1. Header
        header = ctk.CTkFrame(self)
        header.grid(row=0, column=0, padx=20, pady=(20, 10), sticky="ew")
        header.grid_columnconfigure(1, weight=1) # Spacer but we use 2 now
        header.grid_columnconfigure(2, weight=1) 
        
        ctk.CTkLabel(header, text="Antigravity Sync", font=("Roboto", 24, "bold")).grid(row=0, column=0, padx=20, pady=10, sticky="w")
        
        # Header Buttons Frame
        hb_frame = ctk.CTkFrame(header, fg_color="transparent")
        hb_frame.grid(row=0, column=3, padx=10, sticky="e")
        
        ctk.CTkButton(hb_frame, text="ℹ Info", width=50, command=self.open_info).pack(side="left", padx=5)
        # ctk.CTkButton(hb_frame, text="⚙", width=40, command=self.open_settings).pack(side="left", padx=5)
        # Re-using existing settings button style but inside pack
        ctk.CTkButton(hb_frame, text="⚙ Settings", width=80, command=self.open_settings).pack(side="left", padx=5)

        ctk.CTkLabel(header, text=f"User: {self.cfg.current_user}", text_color="gray").grid(row=1, column=0, columnspan=4, padx=20, pady=(0, 10), sticky="w")

        # 2. Actions
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=1, column=0, padx=20, pady=10, sticky="ew")
        actions.grid_columnconfigure(0, weight=1)
        actions.grid_columnconfigure(1, weight=1)

        self.btn_backup = ctk.CTkButton(actions, text="BACKUP (Upload)", command=self.start_backup, height=50, 
                                        fg_color="#2CC985", hover_color="#229965")
        self.btn_backup.grid(row=0, column=0, padx=10, sticky="ew")

        self.btn_restore = ctk.CTkButton(actions, text="RESTORE (Download)", command=self.confirm_restore, height=50, 
                                         fg_color="#D97706", hover_color="#B45309")
        self.btn_restore.grid(row=0, column=1, padx=10, sticky="ew")

        self.btn_cancel = ctk.CTkButton(self, text="CANCEL", command=self.cancel_operation, fg_color="#EF4444", state="disabled")
        self.btn_cancel.grid(row=2, column=0, padx=30, pady=5, sticky="ew")

        # 3. Status
        status = ctk.CTkFrame(self)
        status.grid(row=3, column=0, padx=20, pady=20, sticky="nsew")
        status.grid_columnconfigure(0, weight=1)
        status.grid_rowconfigure(2, weight=1)

        # 3a. Stats
        stats = ctk.CTkFrame(status, fg_color="transparent")
        stats.grid(row=0, column=0, padx=10, pady=(10, 0), sticky="ew")
        self.label_time = ctk.CTkLabel(stats, text="Time: 0:00:00")
        self.label_time.pack(side="left")
        self.label_eta = ctk.CTkLabel(stats, text="ETA: --:--")
        self.label_eta.pack(side="right")

        # 3b. Progress
        prog_cont = ctk.CTkFrame(status, fg_color="transparent")
        prog_cont.grid(row=1, column=0, padx=10, pady=(5, 5), sticky="ew")
        prog_cont.grid_columnconfigure(0, weight=1)
        
        self.progress_bar = ctk.CTkProgressBar(prog_cont)
        self.progress_bar.grid(row=0, column=0, sticky="ew")
        self.progress_bar.set(0)
        self.label_percent = ctk.CTkLabel(prog_cont, text="0%", width=40)
        self.label_percent.grid(row=0, column=1, padx=(10, 0))

        # 3c. Log
        self.log_textbox = ctk.CTkTextbox(status, state="disabled")
        self.log_textbox.grid(row=2, column=0, padx=10, pady=10, sticky="nsew")

    # --- System Tray ---
    def create_image(self):
        # Create a simple icon for the tray
        width = 64
        height = 64
        color1 = "black"
        color2 = "#2CC985"
        image = Image.new('RGB', (width, height), color1)
        dc = ImageDraw.Draw(image)
        dc.rectangle((width // 4, height // 4, width * 3 // 4, height * 3 // 4), fill=color2)
        return image

    def minimize_to_tray(self):
        self.withdraw()
        self.is_minimized = True
        if not self.tray_icon:
            image = self.create_image()
            menu = (pystray.MenuItem('Show', self.show_window), pystray.MenuItem('Exit', self.quit_app))
            self.tray_icon = pystray.Icon("name", image, "Antigravity Sync", menu)
            threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def show_window(self, icon, item):
        self.tray_icon.stop()
        self.tray_icon = None
        self.after(0, self.deiconify)
        self.is_minimized = False

    def on_closing(self):
        # Prompt user
        msg = ctk.CTkToplevel(self)
        msg.title("Exit")
        msg.geometry("300x150")
        msg.transient(self) 
        msg.grab_set()
        
        ctk.CTkLabel(msg, text="Minimize to Tray or Exit?", font=("Roboto", 14)).pack(pady=20)
        
        btn_frame = ctk.CTkFrame(msg, fg_color="transparent")
        btn_frame.pack(pady=10)
        
        def do_tray():
            msg.destroy()
            self.minimize_to_tray()
            
        def do_exit():
            msg.destroy()
            self.quit_app(None, None)

        ctk.CTkButton(btn_frame, text="To Tray", command=do_tray, width=80).pack(side="left", padx=10)
        ctk.CTkButton(btn_frame, text="Exit App", command=do_exit, fg_color="#EF4444", hover_color="#B91C1C", width=80).pack(side="left", padx=10)

    def quit_app(self, icon, item):
        if self.tray_icon: self.tray_icon.stop()
        self.quit()

    # --- Scheduler ---
    def setup_scheduler(self):
        threading.Thread(target=self.scheduler_loop, daemon=True).start()
        # Also trigger startup scan
        self.after(1000, self.startup_scan)

    def startup_scan(self):
        logic = SyncLogic(self.cfg, self.queue)
        threading.Thread(target=logic.scan_for_backups, daemon=True).start()

    def scheduler_loop(self):
        last_run_minute = ""
        while True:
            now = datetime.now()
            current_hm = now.strftime("%H:%M")
            
            # Check if this minute matches a scheduled time and hasn't run yet
            scheduled = self.cfg.get("scheduled_times")
            if current_hm in scheduled and current_hm != last_run_minute:
                # Trigger backup
                self.queue.put(("log", f"⏰ Auto-Backup triggered at {current_hm}"))
                logic = SyncLogic(self.cfg, None) # No queue for background task mostly
                # We can't update UI progress easily if another backup is running, 
                # but we can try to aquire a lock or just run logic.
                # ideally we push to queue log to update UI if open
                if threading.active_count() < 5: # Primitive guard
                     threading.Thread(target=logic.run_backup, args=(True,), daemon=True).start()
                last_run_minute = current_hm
            
            time.sleep(10)

    # --- Runtime ---
    def check_queue(self):
        try:
            while True:
                msg_type, data = self.queue.get_nowait()
                if msg_type == "log":
                    self.log_to_ui(data)
                elif msg_type == "progress":
                    val, msg, elapsed, eta = data
                    self.update_progress_ui(val, elapsed, eta)
                elif msg_type == "finish":
                    success, msg = data
                    self.on_finish(success, msg)
        except queue.Empty:
            pass
        finally:
            self.after(100, self.check_queue)

    def log_to_ui(self, message):
        self.log_textbox.configure(state="normal")
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_textbox.insert("end", f"[{ts}] {message}\n")
        self.log_textbox.see("end")
        self.log_textbox.configure(state="disabled")
        try: self.update_idletasks()
        except: pass

    def update_progress_ui(self, val, elapsed, eta):
        self.progress_bar.set(val)
        self.label_percent.configure(text=f"{int(val*100)}%")
        self.label_time.configure(text=f"Time: {elapsed}")
        self.label_eta.configure(text=f"ETA: {eta}")
        try: self.update_idletasks()
        except: pass

    def on_finish(self, success, msg):
        self.lock_ui(False)
        self.btn_cancel.configure(state="disabled")
        if not success and msg != "Cancelled":
            if not self.is_minimized: messagebox.showerror("Error", msg)
        elif success:
             self.progress_bar.set(1)

    def lock_ui(self, locked=True):
        state = "disabled" if locked else "normal"
        self.btn_backup.configure(state=state)
        self.btn_restore.configure(state=state)
        if locked:
            self.btn_cancel.configure(state="normal")
            self.progress_bar.set(0)

    def start_backup(self):
        self.lock_ui(True)
        self.logic = SyncLogic(self.cfg, self.queue)
        self.running_thread = threading.Thread(target=self.logic.run_backup, daemon=True)
        self.running_thread.start()

    def confirm_restore(self):
        if messagebox.askyesno("Confirm", "Overwrite local files?"):
            self.lock_ui(True)
            self.logic = SyncLogic(self.cfg, self.queue)
            self.running_thread = threading.Thread(target=self.logic.run_restore, daemon=True)
            self.running_thread.start()

    def cancel_operation(self):
        if self.logic: self.logic.abort()
        self.btn_cancel.configure(state="disabled")

    def open_settings(self):
        SettingsDialog(self, self.cfg)

    def open_info(self):
        InfoDialog(self)

if __name__ == "__main__":
    app = SyncApp()
    app.mainloop()
