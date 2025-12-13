import sys
import os
import time
import subprocess
import re
import shutil
import sqlite3
import atexit
import urllib.parse
import json
import threading
import tkinter as tk
from tkinter import simpledialog, messagebox
from collections import Counter

# --- NATIVE IMPORTS ---
try:
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.diagnostics import DiagnosticsService
    from pymobiledevice3.usbmux import list_devices
    NATIVE_SUPPORT = True
except ImportError:
    NATIVE_SUPPORT = False

# --- UI SUPPORT ---
try:
    from PIL import Image, ImageTk
except ImportError:
    Image = None
    ImageTk = None

class BypassAutomation:
    def __init__(self, log_callback=None):
        self.log_callback = log_callback
        
        # --- PATH SETUP ---
        script_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(script_dir)
        
        self.tools_dir = os.path.join(project_root, "libimobiledevice")
        if not os.path.exists(self.tools_dir):
            self.tools_dir = "C:/R1nderPest/libimobiledevice"

        # Resolve Commands
        self.pymobiledevice3_cmd = self._get_cmd_path("pymobiledevice3")
        self.ideviceinfo_cmd = self._get_cmd_path("ideviceinfo")
        self.idevicediagnostics_cmd = self._get_cmd_path("idevicediagnostics")
        self.ifuse_cmd = self._get_cmd_path("ifuse")
        self.curl_cmd = self._get_cmd_path("curl")

        self.api_url = "http://192.168.1.3:8000/get2.php"
        self.mount_point = os.path.join(os.path.expanduser("~"), f".ifuse_mount_{os.getpid()}")
        self.afc_mode = None
        self.device_info = {}
        self.guid = None
        self.manual_guid = None
        self.attempt_count = 0
        self.max_attempts = 15
        
        atexit.register(self._cleanup)

    def _get_cmd_path(self, cmd_name):
        local_path = os.path.join(self.tools_dir, f"{cmd_name}.exe")
        if os.path.exists(local_path):
            return local_path
        
        if os.name == 'nt':
            python_scripts = os.path.join(sys.prefix, 'Scripts')
            script_path = os.path.join(python_scripts, f"{cmd_name}.exe")
            if os.path.exists(script_path):
                return script_path
        
        system_path = shutil.which(cmd_name)
        if system_path:
            return system_path
            
        return cmd_name

    def log(self, msg, level='info'):
        if self.log_callback:
            self.log_callback(msg, level)
        else:
            print(f"[{level.upper()}] {msg}")

    def safe_remove(self, path):
        try:
            if os.path.isfile(path) or os.path.islink(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
        except Exception as e:
            if os.path.exists(path):
                self.log(f"Cleanup warning for {path}: {e}", "detail")

    def _run_cmd(self, cmd, timeout=None):
        try:
            cmd = [str(c) for c in cmd]
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, startupinfo=startupinfo)
            return res.returncode, res.stdout.strip(), res.stderr.strip()
        except subprocess.TimeoutExpired:
            return 124, "", "Timeout"
        except Exception as e:
            return 1, "", str(e)

    def detect_device(self):
        self.log("Detecting Device...", "step")

        if NATIVE_SUPPORT:
            try:
                devices = list_devices()
                if devices:
                    udid = devices[0].serial
                    try:
                        lockdown = create_using_usbmux(serial=udid)
                        vals = lockdown.get_value()
                        self.device_info = {
                            'UniqueDeviceID': udid,
                            'ProductType': vals.get('ProductType', 'Unknown'),
                            'ProductVersion': vals.get('ProductVersion', 'Unknown'),
                            'SerialNumber': vals.get('SerialNumber', 'Unknown')
                        }
                    except:
                        self.device_info = {'UniqueDeviceID': udid}
                    
                    desc = f"{self.device_info.get('ProductType','?')} (iOS {self.device_info.get('ProductVersion','?')})"
                    self.log(f"Connected (Native): {desc}", "success")
                    return
            except Exception as e:
                self.log(f"Native detection skipped: {e}", "detail")

        code, out, err = self._run_cmd([self.ideviceinfo_cmd])
        if code != 0:
            self.log(f"Device not found. Error: {err or 'Unknown'}", "error")
            raise Exception("Device not connected")
        
        info = {}
        for line in out.splitlines():
            if ": " in line:
                key, val = line.split(": ", 1)
                info[key.strip()] = val.strip()
        self.device_info = info
        self.log(f"Connected (CLI): {info.get('ProductType')} | UDID: {info.get('UniqueDeviceID')}", "success")

    def reboot_device(self):
        self.log("Rebooting device...", "step")
        reboot_initiated = False

        if NATIVE_SUPPORT:
            try:
                lockdown = create_using_usbmux()
                with DiagnosticsService(lockdown) as diagnostics:
                    diagnostics.restart()
                self.log("Native reboot command sent.", "detail")
                reboot_initiated = True
            except Exception as e:
                self.log(f"Native reboot failed ({e}), trying CLI...", "detail")

        if not reboot_initiated:
            code, _, err = self._run_cmd([self.pymobiledevice3_cmd, "restart"])
            if code == 0:
                reboot_initiated = True
            else:
                code, _, err = self._run_cmd([self.idevicediagnostics_cmd, "restart"])
                if code == 0:
                    reboot_initiated = True
                else:
                    self.log(f"Soft reboot failed: {err}", "warn")
                    return False

        self.log("Waiting for device to shut down...", "detail")
        disconnected = False
        for _ in range(30):
            if NATIVE_SUPPORT:
                if not list_devices():
                    disconnected = True
                    break
            else:
                if self._run_cmd([self.ideviceinfo_cmd])[0] != 0:
                    disconnected = True
                    break
            time.sleep(1)
            
        if not disconnected:
            self.log("Warning: Device didn't disappear, it might not have rebooted.", "warn")
        
        self.log("Waiting for reconnect...", "info")
        for i in range(60): 
            time.sleep(5)
            found = False
            if NATIVE_SUPPORT and list_devices():
                found = True
            elif not NATIVE_SUPPORT:
                if self._run_cmd([self.ideviceinfo_cmd])[0] == 0:
                    found = True
            if found:
                self.log(f"Device reconnected after {i * 5}s", "success")
                time.sleep(10)
                return True
            if i % 6 == 0:
                self.log(f"Waiting... ({i * 5}s)", "detail")
        
        self.log("Device did not reconnect in time", "error")
        return False

    # --- GUID EXTRACTION ---
    def parse_tracev3_structure(self, data):
        signatures = []
        db_patterns = [
            b'BLDatabaseManager',
            b'BLDatabase',
            b'BLDatabaseManager.sqlite', 
            b'bookassetd [Database]: Store is at file:///private/var/containers/Shared/SystemGroup',
        ]
        for pattern in db_patterns:
            pos = 0
            while True:
                pos = data.find(pattern, pos)
                if pos == -1: break
                signatures.append(('string', pattern, pos))
                pos += len(pattern)
        return signatures

    def extract_guid_candidates(self, data, context_pos, window_size=512):
        candidates = []
        guid_pattern = re.compile(rb'([0-9A-F]{8}[-][0-9A-F]{4}[-][0-9A-F]{4}[-][0-9A-F]{4}[-][0-9A-F]{12})', re.IGNORECASE)
        start = max(0, context_pos - window_size)
        end = min(len(data), context_pos + window_size)
        context_data = data[start:end]
        for match in guid_pattern.finditer(context_data):
            guid = match.group(1).decode('ascii').upper()
            if self.validate_guid_structure(guid):
                candidates.append({'guid': guid, 'position': match.start() + start - context_pos})
        return candidates

    def validate_guid_structure(self, guid):
        try:
            parts = guid.split('-')
            if len(parts) != 5: return False
            if len(parts[0]) != 8 or len(parts[1]) != 4 or len(parts[2]) != 4 or len(parts[3]) != 4 or len(parts[4]) != 12: return False
            clean_guid = guid.replace('-', '')
            if not all(c in '0123456789ABCDEFabcdef' for c in clean_guid): return False
            if parts[2][0] != '4': return False
            if parts[3][0].upper() not in '89AB': return False
            return True
        except: return False

    def analyze_guid_confidence(self, guid_candidates):
        if not guid_candidates: return None
        guid_counts = Counter(candidate['guid'] for candidate in guid_candidates)
        scored_guids = []
        for guid, count in guid_counts.items():
            score = count * 10
            positions = [c['position'] for c in guid_candidates if c['guid'] == guid]
            close_positions = [p for p in positions if abs(p) < 100]
            if close_positions: score += len(close_positions) * 5
            before_positions = [p for p in positions if p < 0]
            if before_positions: score += len(before_positions) * 3
            scored_guids.append((guid, score, count))
        scored_guids.sort(key=lambda x: x[1], reverse=True)
        return scored_guids

    def get_guid_enhanced(self):
        self.attempt_count += 1
        self.log(f"GUID search attempt {self.attempt_count}/{self.max_attempts}", "attempt")
        udid = self.device_info.get('UniqueDeviceID')
        log_path = f"{udid}.logarchive"
        try:
            self.log("Collecting device logs...", "detail")
            code, _, err = self._run_cmd([self.pymobiledevice3_cmd, "syslog", "collect", log_path], timeout=120)
            if code != 0:
                self.log(f"Log collection failed: {err}", "error")
                return None
            trace_file = os.path.join(log_path, "logdata.LiveData.tracev3")
            if not os.path.exists(trace_file):
                self.log("tracev3 file not found", "error")
                return None
            with open(trace_file, 'rb') as f:
                data = f.read()
            self.log(f"Analyzing {len(data)/1024/1024:.2f} MB...", "detail")
            signatures = self.parse_tracev3_structure(data)
            all_candidates = []
            for sig_type, pattern, pos in signatures:
                if pattern == b'BLDatabaseManager':
                    all_candidates.extend(self.extract_guid_candidates(data, pos))
            if not all_candidates:
                return None
            scored = self.analyze_guid_confidence(all_candidates)
            if not scored: return None
            best_guid, best_score, best_count = scored[0]
            
            # --- MODIFIED LOGIC: Lower Threshold to 15 ---
            # You confirmed Score 18 was valid. 
            # We now accept anything >= 15 as "Good enough" to avoid loops.
            
            if best_score >= 25:
                self.log(f"✅ HIGH CONFIDENCE: {best_guid} (Score: {best_score})", "success")
                return best_guid
            elif best_score >= 15:
                self.log(f"⚠️ MEDIUM CONFIDENCE: {best_guid} (Score: {best_score})", "warn")
                self.log("   Accepting this GUID as the best candidate to avoid reboot loop.", "warn")
                return best_guid
            else:
                self.log(f"❌ VERY LOW CONFIDENCE: {best_guid} (Score: {best_score})", "error")
                self.log("   Score too low (<15). Retrying...", "detail")
                return None 
            
        except Exception as e:
            self.log(f"GUID Extraction Error: {e}", "error")
            return None
        finally:
            self.safe_remove(log_path)

    def get_guid_auto(self):
        if self.manual_guid:
            self.log(f"Using Manual GUID: {self.manual_guid}", "success")
            return self.manual_guid
        
        self.attempt_count = 0
        while self.attempt_count < self.max_attempts:
            guid = self.get_guid_enhanced()
            if guid: return guid
            
            self.log("GUID not found. Rebooting...", "warn")
            if not self.reboot_device():
                self.log("Reboot failed, retrying anyway...", "warn")
            try:
                self.detect_device()
            except:
                self.log("Device not ready yet, retrying loop...", "detail")
            time.sleep(5)
            
        return None

    def get_all_urls(self, prd, guid, sn):
        params = urllib.parse.urlencode({'prd': prd, 'guid': guid, 'sn': sn})
        url = f"{self.api_url}?{params}"
        self.log(f"Requesting config from: {url}", "detail")
        code, out, err = self._run_cmd([self.curl_cmd, "-s", "-k", url])
        if code != 0:
             self.log(f"Server Connection Failed: {err}", "error")
             return None, None, None
        try:
            data = json.loads(out)
            if data.get('success'):
                return (data['links']['step1_fixedfile'], data['links']['step2_bldatabase'], data['links']['step3_final'])
            else:
                self.log(f"Server replied with error: {data.get('error')}", "error")
                return None, None, None
        except json.JSONDecodeError:
            self.log(f"Server returned INVALID JSON.", "error")
            return None, None, None
        except Exception as e:
            self.log(f"Unknown error parsing response: {e}", "error")
            return None, None, None

    def verify_afc(self):
        if shutil.which("ifuse") or os.path.exists(self.ifuse_cmd):
            self.afc_mode = "ifuse"
        else:
            self.afc_mode = "pymobiledevice3"
        self.log(f"AFC Mode: {self.afc_mode}", "info")

    def mount_afc(self):
        if self.afc_mode != "ifuse": return True
        os.makedirs(self.mount_point, exist_ok=True)
        self._run_cmd(["umount", self.mount_point])
        if self._run_cmd([self.ifuse_cmd, self.mount_point])[0] == 0: return True
        time.sleep(2)
        return self._run_cmd([self.ifuse_cmd, self.mount_point])[0] == 0

    def afc_op(self, op, *args):
        if self.afc_mode == "ifuse":
            if not self.mount_afc(): raise Exception("Mount failed")
            if op == "push": 
                dst = self.mount_point + args[1]
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.exists(dst): os.remove(dst)
                shutil.copy(args[0], dst)
            elif op == "pull":
                src = self.mount_point + args[0]
                if os.path.exists(src): shutil.copy(src, args[1])
            elif op == "exists": return os.path.exists(self.mount_point + args[0])
            elif op == "rm":
                fp = self.mount_point + args[0]
                if os.path.exists(fp): os.remove(fp)
            elif op == "ls":
                path = self.mount_point + args[0]
                return os.listdir(path) if os.path.exists(path) else []

        else:
            cmd = [self.pymobiledevice3_cmd, "afc"]
            
            if op == "push": 
                self._run_cmd(cmd + ["rm", args[1]])
                self._run_cmd(cmd + ["push", args[0], args[1]])
            elif op == "pull":
                self.safe_remove(args[1])
                self._run_cmd(cmd + ["pull", args[0], args[1]])
            elif op == "rm": 
                self._run_cmd(cmd + ["rm", args[0]])
            elif op == "exists": 
                return self._run_cmd(cmd + ["ls", args[0]])[0] == 0
            elif op == "mv":
                self._run_cmd(cmd + ["mv", args[0], args[1]])
            elif op == "ls":
                _, out, _ = self._run_cmd(cmd + ["ls", args[0]])
                return out.splitlines()

            return True

    def _cleanup(self):
        if self.afc_mode == "ifuse": self._run_cmd(["umount", self.mount_point])

    def run_activation_flow(self):
        self.verify_afc()
        self.detect_device()
        self.log("Starting GUID Search...", "step")

        self.guid = self.get_guid_auto()
        if not self.guid:
            raise Exception("Failed to find GUID")
        self.log(f"Using GUID: {self.guid}", "success")

        prd = self.device_info.get('ProductType')
        sn = self.device_info.get('SerialNumber')
        s1, s2, s3 = self.get_all_urls(prd, self.guid, sn)
        if not s3:
            raise Exception("Server error - check logs above")

        self.log("Downloading Payload...", "step")
        local_db = "downloads.28.sqlitedb"
        self.safe_remove(local_db)

        if self._run_cmd([self.curl_cmd, "-L", "-k", "-o", local_db, s3])[0] != 0:
            raise Exception("Download failed")

        self.log("Deploying Payload...", "step")
        self.afc_op("rm", "/Downloads/downloads.28.sqlitedb-wal")
        self.afc_op("push", local_db, "/Downloads/downloads.28.sqlitedb")
        self.log("Payload pushed.", "success")

        self.log("Rebooting...", "step")
        self.reboot_device()

        self.log("Waiting for Metadata (Pull-and-Verify Mode)...", "step")
        
        check_dir = os.path.join(os.getcwd(), f"temp_pull_{os.getpid()}")
        if not os.path.exists(check_dir):
            os.makedirs(check_dir)

        # Candidates to check
        candidates = [
            ("/iTunes_Control/iTunes/iTunesMetadata.plist", "metadata_std.plist"),
            ("/iTunes_Control/iTunes/iTunesMetadata.plist.ext", "metadata_ext.plist")
        ]

        valid_file_path = None

        for i in range(60):
            found = False
            for remote_path, local_name in candidates:
                local_check_path = os.path.join(check_dir, local_name)
                self.safe_remove(local_check_path)
                self.afc_op("pull", remote_path, local_check_path)

                if os.path.exists(local_check_path):
                    if os.path.isfile(local_check_path) and os.path.getsize(local_check_path) > 100:
                        self.log(f"FOUND: Valid file at {remote_path}", "success")
                        valid_file_path = local_check_path
                        found = True
                        break
                    elif os.path.isdir(local_check_path):
                        for root, dirs, files in os.walk(local_check_path):
                            for file in files:
                                fpath = os.path.join(root, file)
                                if os.path.getsize(fpath) > 100:
                                    self.log(f"FOUND: Hidden file inside directory dump: {file}", "success")
                                    extracted_path = os.path.join(check_dir, "extracted_meta.plist")
                                    shutil.copy(fpath, extracted_path)
                                    valid_file_path = extracted_path
                                    found = True
                                    break
                            if found: break
                if found: break
            if found and valid_file_path: break
            
            if i % 4 == 0:
                self.log(f"Checking for files... ({i*5}s)", "detail")
            time.sleep(5)
        
        if not valid_file_path:
             self.safe_remove(check_dir)
             raise Exception("Metadata generation timed out. Valid file never appeared.")

        self.log("STAGE 1: Copy to /Books/...", "step")
        self.afc_op("push", valid_file_path, "/Books/iTunesMetadata.plist")


        
        self.log("Rebooting device (Stage 1 complete)...", "step")
        if not self.reboot_device():
            self.log("Reboot failed, continuing anyway...", "warn")

        # Helper verify check (Non-blocking)
        self.log("Verifying Stage 1 payload delivery...", "detail")
        books_content = self.afc_op("ls", "/Books")
        if "asset.epub" not in books_content:
             self.log("⚠️ WARNING: 'asset.epub' not visible in /Books/ (False negative possible).", "warn")
             self.log("   Assuming Stage 1 worked based on user report. Proceeding blindly...", "warn")
        else:
             self.log("✅ FOUND: 'asset.epub' verified in /Books/.", "success")

        # Retry loop for Stage 2 verification (bookassetd might be slow)
        has_extracted_content = False
        self.log("Checking /Books/ for extracted assets (Wait up to 60s)...", "step")
        
        for wait_step in range(12): # 12 * 5s = 60s
            files_in_books = self.afc_op("ls", "/Books")
            
            for f in files_in_books:
                if "Caches" in f or "MobileGestalt" in f:
                     has_extracted_content = True
            
            if has_extracted_content:
                self.log(f"FOUND: Extracted content detected after {wait_step * 5}s", "success")
                break
            
            self.log(f"Waiting for extraction... ({wait_step * 5}s)", "detail")
            time.sleep(5)

        if not has_extracted_content:
            self.log(f"Files currently in /Books/: {files_in_books}", "detail")
            
            if "asset.epub" in files_in_books:
                self.log("⚠️ WARNING: Extracted content not found in /Books/, but 'asset.epub' is present (SystemGroup target?).", "warn")
            else:
                self.log("⚠️ WARNING: Neither 'asset.epub' nor extracted content visible in /Books/.", "warn")
                self.log("   Since you confirmed the files are there, AFC ls might be blind.", "warn")
            
            self.log("   Proceeding with hope that bookassetd ran successfully in the background.", "warn")
        
        self.log("✅ Stage 2 wait complete.", "success")
        # ---------------------------------------

        self.log("STAGE 2: Copy back to /iTunes_Control/...", "step")
        self.log("Restoring valid metadata to iTunes_Control...", "detail")
        self.afc_op("push", valid_file_path, "/iTunes_Control/iTunes/iTunesMetadata.plist")
        
        self.log("Waiting 30s for bookassetd/system sync...", "step")
        for i in range(6):
            time.sleep(5)
            self.log(f"Wait... {i*5}/30s", "detail")
        
        self.log("Final Reboot...", "step")
        self.reboot_device()
        
        self.safe_remove(check_dir)
        self.log("ACTIVATION COMPLETE!", "success")


class ModernApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Japa Remover A12+")
        self.geometry("900x600")
        self.configure(bg="#0a0a0a")
        self.backend = BypassAutomation(log_callback=self.update_status)
        self._create_ui()
        
    def _create_ui(self):
        h = tk.Frame(self, bg="#1a1a1a", height=60)
        h.pack(fill="x")
        tk.Label(h, text="Japa Remover A12+", bg="#1a1a1a", fg="#00ffff", font=("Consolas", 18)).pack(side="left", padx=20, pady=15)
        main = tk.Frame(self, bg="#0a0a0a")
        main.pack(fill="both", expand=True, padx=20, pady=20)
        self.log_text = tk.Text(main, bg="#111", fg="#ccc", font=("Consolas", 10))
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_config('error', foreground='#ff5555')
        self.log_text.tag_config('success', foreground='#55ff55')
        self.log_text.tag_config('step', foreground='#00ffff')
        self.log_text.tag_config('detail', foreground='#666')
        self.log_text.tag_config('warn', foreground='#ffff55')
        self.log_text.tag_config('attempt', foreground='#00aaaa')
        self.btn = tk.Button(self, text="START ACTIVATION", bg="#005555", fg="white", font=("Consolas", 14), command=self.start)
        self.btn.pack(fill="x", pady=20, padx=20)

    def update_status(self, msg, level):
        self.log_text.insert("end", f"{msg}\n", level)
        self.log_text.see("end")
        self.update_idletasks()

    def start(self):
        # RESET GUID to ensure fresh run every time
        self.backend.manual_guid = None 

        if messagebox.askyesno("Manual GUID", "Do you want to enter a known GUID manually?"):
            manual_val = simpledialog.askstring("Input GUID", "Paste GUID here:")
            if manual_val and len(manual_val.strip()) > 10:
                self.backend.manual_guid = manual_val.strip()
            else:
                self.update_status("Invalid or empty GUID entered. Using auto-detection.", "warn")

        self.btn.config(state="disabled")
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        try:
            self.backend.run_activation_flow()
        except Exception as e:
            self.update_status(f"ERROR: {e}", "error")
        finally:
            self.btn.config(state="normal")

if __name__ == "__main__":
    app = ModernApp()
    app.mainloop()