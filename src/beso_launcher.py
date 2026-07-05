#!/usr/bin/env python3
"""
=============================================================================
  BESO OPTIMIZATION LAUNCHER
  Run with: python beso_launcher.py
  Opens a browser interface for configuring and launching the BESO optimizer.
=============================================================================
"""

import http.server
import socketserver
import webbrowser
import json
import os
import subprocess
import threading
import urllib.parse
import re
import glob
from datetime import datetime

PORT = 8087
CONFIG_FILE = "beso_config.json"
PID_FILE    = "beso_pid.json"

# =============================================================================
#   RUN STATE  (shared between threads - always access under _run_lock)
# =============================================================================
_run_lock = threading.Lock()
_run_state = {
    "status":     "idle",   # idle | running | completed | crashed
    "engine":     None,
    "start_time": None,
    "end_time":   None,
    "iteration":  0,
    "stiffness":  0.0,
    "log":        [],
}

# STL generation state - separate from optimizer run state
_stl_lock  = threading.Lock()
_stl_state = {
    "status":     "idle",   # idle | running | completed | crashed
    "start_time": None,
    "end_time":   None,
    "log":        [],
}
_stl_proc = None   # subprocess.Popen handle for the active STL generator

# Tracks whether the auto-populate popup was already shown this session.
# Reset to False whenever a new optimizer run starts.
_stl_popup_shown = False

# Stale temp files detected at launcher startup
_stale_temp_files = []

# ---- PID file helpers -------------------------------------------------------

def write_pid_file(pid, engine):
    try:
        with open(PID_FILE, 'w') as f:
            json.dump({"pid": pid, "engine": engine,
                       "start": datetime.now().isoformat()}, f)
    except Exception as e:
        print("[Launcher] Could not write PID file: {}".format(e))

def delete_pid_file():
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        pass

def kill_run_process():
    """Kill the active optimizer process and its children, then clean up the PID file."""
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, 'r') as f:
                data = json.load(f)
            pid = int(data.get('pid', 0))
            if pid:
                try:
                    subprocess.run(
                        ['taskkill', '/F', '/T', '/PID', str(pid)],
                        capture_output=True, timeout=10
                    )
                except Exception:
                    pass
                try:
                    import signal
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        delete_pid_file()

def kill_stl_process():
    """Kill the active STL generator subprocess."""
    global _stl_proc
    if _stl_proc is not None:
        pid = _stl_proc.pid
        try:
            subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(pid)],
                capture_output=True, timeout=5
            )
        except Exception:
            pass
        try:
            _stl_proc.terminate()
        except Exception:
            pass
        _stl_proc = None

def scan_abaqus_leftovers(inp_path):
    """
    Find all job-named files/folders in the INP directory that are leftover
    Abaqus artifacts, excluding the .inp file itself.
    Returns a list of dicts: {name, path, is_dir}.
    """
    if not inp_path or not os.path.isfile(inp_path):
        return [], ""
    directory  = os.path.dirname(os.path.abspath(inp_path))
    basename   = os.path.basename(inp_path)
    job_name   = os.path.splitext(basename)[0]   # e.g. "beam_beso_base"
    leftovers  = []
    try:
        for entry in os.listdir(directory):
            if entry == basename:
                continue   # keep the .inp file
            if entry.lower().startswith(job_name.lower() + '.'):
                full = os.path.join(directory, entry)
                leftovers.append({
                    "name":   entry,
                    "path":   full,
                    "is_dir": os.path.isdir(full)
                })
    except Exception:
        pass
    return sorted(leftovers, key=lambda x: x["name"]), directory

def send_to_recycle_bin(path, is_dir=False):
    """
    Send a file or folder to the Windows Recycle Bin via PowerShell.
    Falls back to hard deletion on non-Windows or on error.
    """
    abs_path = os.path.abspath(path).replace("'", "''")
    try:
        if is_dir:
            ps_cmd = (
                "Add-Type -AssemblyName Microsoft.VisualBasic; "
                "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory("
                "'{p}', 'OnlyErrorDialogs', 'SendToRecycleBin')".format(p=abs_path)
            )
        else:
            ps_cmd = (
                "Add-Type -AssemblyName Microsoft.VisualBasic; "
                "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
                "'{p}', 'OnlyErrorDialogs', 'SendToRecycleBin')".format(p=abs_path)
            )
        result = subprocess.run(
            ['powershell', '-NoProfile', '-Command', ps_cmd],
            capture_output=True, timeout=15
        )
        return result.returncode == 0
    except Exception:
        pass
    # Hard-delete fallback (non-Windows / PowerShell unavailable)
    try:
        if is_dir:
            import shutil
            shutil.rmtree(path)
        else:
            os.remove(path)
        return True
    except Exception:
        return False

def is_pid_alive(pid):
    """Check whether a process PID is still running (Windows + Unix)."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "PID eq {}".format(pid)],
            capture_output=True, text=True, timeout=5
        )
        return str(pid) in result.stdout
    except Exception:
        pass
    # Unix fallback
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False

def check_previous_session():
    """Return info about a run that was active when the launcher was last stopped."""
    if not os.path.exists(PID_FILE):
        return None
    try:
        with open(PID_FILE, 'r') as f:
            data = json.load(f)
        pid = int(data.get("pid", 0))
        alive = is_pid_alive(pid) if pid else False
        if not alive:
            delete_pid_file()   # tidy up - run already ended
        return {
            "alive":  alive,
            "pid":    pid,
            "engine": data.get("engine", "unknown"),
            "start":  data.get("start",  "unknown"),
        }
    except Exception:
        delete_pid_file()
        return None

# ---- Startup stale temp file scan -------------------------------------------

def _init_stale_temp_scan():
    """Called once at launcher startup. Populates _stale_temp_files global."""
    global _stale_temp_files
    work_dir = os.path.dirname(os.path.abspath(__file__))
    _stale_temp_files = scan_stale_temp_files(work_dir)
    if _stale_temp_files:
        print("[Launcher] WARNING: {} stale STL temp file(s) detected from a previous "
              "crashed run. The STL page will show a notification.".format(
              len(_stale_temp_files)))

def parse_log_line(line):
    """
    Scan a single output line for iteration number and specific stiffness.
    Returns a dict with zero or more of the keys: iteration, stiffness.
    """
    result = {}
    # Iteration markers from both optimisers:
    #   "GENERATING DESIGN ITERATION 5"  /  "ITERATION 5"
    m = re.search(r'(?:DESIGN\s+)?ITERATION\s+(\d+)', line, re.IGNORECASE)
    if m:
        result["iteration"] = int(m.group(1))
    # Specific stiffness - tolerant pattern catches various label spellings
    m = re.search(
        r'specific[\s_]stiffness[^:=]*[:=]?\s*([\d]+\.?[\d]*(?:[eE][+\-]?\d+)?)',
        line, re.IGNORECASE
    )
    if m:
        try:
            result["stiffness"] = float(m.group(1))
        except ValueError:
            pass
    return result

# =============================================================================
#   INP FILE INSPECTOR
# =============================================================================

def _flat_non_design(lines, set_name="NON_DESIGN_SET"):
    """Structure-blind NON_DESIGN_SET reader for models with no *Part blocks.
    Expands generate ranges. Mirrors the optimisers' get_non_design_elements."""
    target = set_name.upper()
    out = set()
    reading = False
    gen = False
    for line in lines:
        u = line.strip().upper()
        if u.startswith("*ELSET"):
            reading = False
            gen = "GENERATE" in u
            for tok in u.split(","):
                tk = tok.strip()
                if tk.startswith("ELSET=") and tk.split("=", 1)[1].strip() == target:
                    reading = True
            continue
        if u.startswith("*") and reading:
            reading = False
        if reading:
            nums = [p.strip() for p in line.strip().split(",") if p.strip()]
            if gen and len(nums) >= 2:
                try:
                    s = int(nums[0]); e = int(nums[1])
                    st = int(nums[2]) if len(nums) >= 3 else 1
                    for x in range(s, e + 1, st):
                        out.add(x)
                except ValueError:
                    pass
            else:
                for p in nums:
                    if p.isdigit():
                        out.add(int(p))
    return out


def _resolve_design_domain(lines, set_name="NON_DESIGN_SET"):
    """
    Mirror of the optimisers' design-part + design-material resolution, for the
    validation panel. Identifies the design part (the part with the most FREE
    elements), reading NON_DESIGN_SET at BOTH part level and assembly level
    (via the *Instance instance->part map, generate-expanded, de-duplicated),
    then resolves that part's *Solid Section material and its elastic modulus.
    Returns: design_part, parts_count, protected_count (design part frozen count),
    material_name, e_modulus, poisson, material_fallback.
    """
    target = set_name.upper()
    parts = {}                 # name(original) -> {count, frozen set, section_mat}
    instance_to_part = {}      # INSTANCE(upper) -> part(original case)
    materials = {}             # NAME(upper) -> (E, nu)
    legacy_section_mat = None  # last *Solid Section material= seen anywhere
    current_part = None
    current_mat = None
    in_element = False
    reading_set = False
    set_is_generate = False
    reading_asm_set = False
    asm_set_generate = False
    asm_target_part = None

    def _add_frozen(part_name, nums, is_generate):
        if part_name is None or part_name not in parts:
            return
        fset = parts[part_name]["frozen"]
        if is_generate and len(nums) >= 2:
            try:
                s = int(nums[0]); e = int(nums[1])
                st = int(nums[2]) if len(nums) >= 3 else 1
                for x in range(s, e + 1, st):
                    fset.add(x)
            except ValueError:
                pass
        else:
            for p in nums:
                if p.isdigit():
                    fset.add(int(p))

    for i, line in enumerate(lines):
        stripped = line.strip()
        upper = stripped.upper()

        if upper.startswith("*PART"):
            current_part = None
            for tok in stripped.split(","):
                tk = tok.strip()
                if tk.upper().startswith("NAME="):
                    current_part = tk.split("=", 1)[1].strip()
            if current_part is not None:
                parts.setdefault(current_part, {"count": 0, "frozen": set(), "section_mat": None})
            in_element = False
            reading_set = False
            continue

        if upper.startswith("*END PART"):
            current_part = None
            in_element = False
            reading_set = False
            continue

        # materials are defined globally (outside parts) -- read regardless
        if upper.startswith("*MATERIAL"):
            current_mat = None
            for tok in stripped.split(","):
                tk = tok.strip()
                if tk.upper().startswith("NAME="):
                    current_mat = tk.split("=", 1)[1].strip()
            continue
        if upper.startswith("*ELASTIC"):
            if current_mat is not None and (i + 1) < len(lines):
                md = lines[i + 1].strip().split(",")
                if len(md) >= 2:
                    try:
                        materials[current_mat.upper()] = (float(md[0].strip()), float(md[1].strip()))
                    except ValueError:
                        pass
            continue

        # ----- inside a *Part block (part-local element IDs) -----
        if current_part is not None:
            if upper.startswith("*ELEMENT"):
                in_element = True
                reading_set = False
                continue
            if upper.startswith("*SOLID SECTION"):
                for tok in stripped.split(","):
                    tk = tok.strip()
                    if tk.upper().startswith("MATERIAL="):
                        sm = tk.split("=", 1)[1].strip()
                        parts[current_part]["section_mat"] = sm
                        legacy_section_mat = sm
                in_element = False
                reading_set = False
                continue
            if upper.startswith("*ELSET"):
                in_element = False
                set_is_generate = "GENERATE" in upper
                reading_set = False
                for tok in upper.split(","):
                    tk = tok.strip()
                    if tk.startswith("ELSET=") and tk.split("=", 1)[1].strip() == target:
                        reading_set = True
                continue
            if upper.startswith("*"):
                in_element = False
                reading_set = False
                continue
            if not stripped:
                continue
            if in_element:
                parts[current_part]["count"] += 1
            if reading_set:
                nums = [p.strip() for p in stripped.split(",") if p.strip()]
                _add_frozen(current_part, nums, set_is_generate)
            continue

        # ----- outside any *Part: assembly / model level -----
        if upper.startswith("*INSTANCE"):
            inst_name = None
            inst_part = None
            for tok in stripped.split(","):
                tk = tok.strip()
                if tk.upper().startswith("NAME="):
                    inst_name = tk.split("=", 1)[1].strip()
                elif tk.upper().startswith("PART="):
                    inst_part = tk.split("=", 1)[1].strip()
            if inst_name is not None and inst_part is not None:
                instance_to_part[inst_name.upper()] = inst_part
            reading_asm_set = False
            continue
        if upper.startswith("*SOLID SECTION"):
            for tok in stripped.split(","):
                tk = tok.strip()
                if tk.upper().startswith("MATERIAL="):
                    legacy_section_mat = tk.split("=", 1)[1].strip()
            continue
        if upper.startswith("*ELSET"):
            asm_set_generate = "GENERATE" in upper
            reading_asm_set = False
            asm_target_part = None
            is_target = False
            inst_ref = None
            for tok in upper.split(","):
                tk = tok.strip()
                if tk.startswith("ELSET=") and tk.split("=", 1)[1].strip() == target:
                    is_target = True
                if tk.startswith("INSTANCE="):
                    inst_ref = tk.split("=", 1)[1].strip()
            if is_target and inst_ref is not None:
                mapped = instance_to_part.get(inst_ref)
                if mapped is not None and mapped in parts:
                    reading_asm_set = True
                    asm_target_part = mapped
            continue
        if upper.startswith("*"):
            reading_asm_set = False
            continue
        if not stripped:
            continue
        if reading_asm_set:
            nums = [p.strip() for p in stripped.split(",") if p.strip()]
            _add_frozen(asm_target_part, nums, asm_set_generate)

    out = {
        "design_part": None, "parts_count": len(parts), "protected_count": 0,
        "material_name": None, "e_modulus": None, "poisson": None,
        "material_fallback": False,
    }

    if parts:
        design_part = None
        best_free = -1
        for name, d in parts.items():
            free = d["count"] - len(d["frozen"])
            if free > best_free:
                best_free = free
                design_part = name
        out["design_part"] = design_part
        out["protected_count"] = len(parts[design_part]["frozen"])
        design_mat = parts[design_part]["section_mat"]
        chosen = design_mat if design_mat is not None else legacy_section_mat
        if design_mat is None and chosen is not None:
            out["material_fallback"] = True
        if chosen is not None:
            out["material_name"] = chosen
            props = materials.get(chosen.upper())
            if props is not None:
                out["e_modulus"] = props[0]
                out["poisson"] = props[1]
    else:
        # no *Part blocks: flat structure-blind read + legacy (last) section material
        out["protected_count"] = len(_flat_non_design(lines, set_name))
        if legacy_section_mat is not None:
            out["material_name"] = legacy_section_mat
            props = materials.get(legacy_section_mat.upper())
            if props is not None:
                out["e_modulus"] = props[0]
                out["poisson"] = props[1]

    return out


def inspect_inp_file(inp_path):
    """
    Reads the INP file and extracts all detectable properties.
    Returns a dict of findings for the frontend validation panel.
    """
    result = {
        "found": False,
        "readable": False,
        "element_type": None,
        "element_count": 0,
        "non_design_set_found": False,
        "non_design_element_count": 0,
        "material_name": None,
        "e_modulus": None,
        "poisson": None,
        "has_thermal_step": False,
        "has_solid_section": False,
        "instance_name": None,
        "design_part": None,
        "parts_detected": 0,
        "material_fallback": False,
        "errors": []
    }

    if not inp_path:
        result["errors"].append("No file path provided.")
        return result

    if not os.path.exists(inp_path):
        result["errors"].append("File not found at: {}".format(inp_path))
        return result

    result["found"] = True

    try:
        with open(inp_path, 'r') as f:
            lines = f.readlines()
        result["readable"] = True
    except Exception as e:
        result["errors"].append("Could not read file: {}".format(str(e)))
        return result

    # --- Simple per-line reads: element type/count, instance, thermal, section ---
    is_reading_elements = False
    element_labels = set()

    for line in lines:
        upper = line.strip().upper()

        if upper.startswith("*ELEMENT") and "TYPE=" in upper:
            for p in upper.split(","):
                p = p.strip()
                if p.startswith("TYPE="):
                    result["element_type"] = p.split("=")[1].strip()
            is_reading_elements = True
            continue

        if is_reading_elements:
            if upper.startswith("*"):
                is_reading_elements = False
            else:
                first = line.strip().split(",")[0].strip()
                if first.isdigit():
                    element_labels.add(int(first))

        if upper.startswith("*INSTANCE"):
            for p in line.split(","):
                cp = p.strip()
                if cp.upper().startswith("NAME="):
                    result["instance_name"] = cp.split("=")[1].strip()

        if upper.startswith("*SOLID SECTION"):
            result["has_solid_section"] = True

        if upper.startswith("*STEP") and "THERMAL" in upper:
            result["has_thermal_step"] = True
        if "COUPLED TEMPERATURE" in upper:
            result["has_thermal_step"] = True

    result["element_count"] = len(element_labels)

    # --- Design part + design material (mirrors the optimisers exactly) ---
    # Resolves the design part (most free elements), its NON_DESIGN_SET count
    # (part-level AND assembly-level, generate-aware), and the design part's
    # section material. This is part-scoped so a multi-part model with a
    # separate (non-design) component cannot mask the real design material.
    dom = _resolve_design_domain(lines, "NON_DESIGN_SET")
    result["design_part"] = dom["design_part"]
    result["parts_detected"] = dom["parts_count"]
    result["material_fallback"] = dom["material_fallback"]
    result["material_name"] = dom["material_name"]
    result["e_modulus"] = dom["e_modulus"]
    result["poisson"] = dom["poisson"]
    result["non_design_element_count"] = dom["protected_count"]
    result["non_design_set_found"] = dom["protected_count"] > 0

    if not result["element_type"]:
        result["errors"].append("Could not detect element type.")
    if not result["has_solid_section"]:
        result["errors"].append("No *Solid Section found in INP.")
    if result["e_modulus"] is None:
        result["errors"].append("Could not detect material elastic properties.")

    return result


def check_abaqus_on_path():
    try:
        result = subprocess.run(
            "abaqus information=release",
            shell=True,
            capture_output=True,
            text=True,
            timeout=15
        )
        return True
    except subprocess.TimeoutExpired:
        return True  # Timed out but command was found
    except Exception:
        return False


def load_existing_config():
    """
    Load beso_config.json if it exists AND was written by the launcher
    (verified by presence of the 'timestamp' key written only by this launcher).
    Returns None if file doesn't exist, is unreadable, or is missing the timestamp.
    """
    if not os.path.exists(CONFIG_FILE):
        return None
    try:
        with open(CONFIG_FILE, 'r') as f:
            cfg = json.load(f)
        # Only surface configs written by this launcher - they always have a timestamp
        if not cfg.get("timestamp"):
            return None
        # Must also have at least the optimization section
        if "optimization" not in cfg:
            return None
        return cfg
    except:
        return None


# =============================================================================
#   FILE BROWSER
# =============================================================================

# Session memory - remembers last browsed directory per server lifetime
_last_browse_dir = os.path.abspath(os.getcwd())


def browse_directory(path=None):
    """
    Returns the contents of a directory for the file browser modal.
    Filters to show only subdirectories and .inp files.
    Remembers the last visited directory across calls.
    """
    global _last_browse_dir

    # Resolve path
    if not path:
        path = _last_browse_dir
    path = os.path.abspath(path)

    if not os.path.isdir(path):
        # If given a file path, use its parent directory
        path = os.path.dirname(path)

    if not os.path.isdir(path):
        path = os.path.abspath(os.getcwd())

    _last_browse_dir = path

    entries = []

    # On Windows, if we're at a drive root with no parent, list drives
    parent = os.path.dirname(path)
    at_root = (parent == path)

    try:
        raw = os.listdir(path)
    except PermissionError:
        return {
            "current_path": path,
            "parent_path": parent if not at_root else None,
            "at_root": at_root,
            "entries": [],
            "error": "Permission denied"
        }
    except Exception as e:
        return {
            "current_path": path,
            "parent_path": parent if not at_root else None,
            "at_root": at_root,
            "entries": [],
            "error": str(e)
        }

    for name in sorted(raw, key=lambda x: (not os.path.isdir(os.path.join(path, x)), x.lower())):
        full = os.path.join(path, name)
        try:
            is_dir = os.path.isdir(full)
            if is_dir:
                entries.append({
                    "name": name,
                    "full_path": full,
                    "is_dir": True,
                    "size": None
                })
            elif name.lower().endswith(".inp"):
                size = os.path.getsize(full)
                entries.append({
                    "name": name,
                    "full_path": full,
                    "is_dir": False,
                    "size": size
                })
        except:
            continue

    # Build breadcrumb parts
    parts = []
    p = path
    while True:
        head, tail = os.path.split(p)
        if tail:
            parts.insert(0, {"name": tail, "path": p})
            p = head
        else:
            parts.insert(0, {"name": p, "path": p})
            break

    return {
        "current_path": path,
        "parent_path": parent if not at_root else None,
        "at_root": at_root,
        "breadcrumbs": parts,
        "entries": entries,
        "error": None
    }


def get_drives():
    """Returns available drives on Windows, or ['/'] on Unix."""
    if os.name == 'nt':
        import string
        drives = []
        for letter in string.ascii_uppercase:
            drive = letter + ":\\"
            if os.path.exists(drive):
                drives.append({"name": drive, "path": drive})
        return drives
    else:
        return [{"name": "/", "path": "/"}]


def browse_directory_folders_only(path=None):
    """
    Like browse_directory but shows ALL files (not just .inp),
    used for the History and STL folder selectors.
    Returns folders and csv files.
    """
    global _last_browse_dir

    if not path:
        path = _last_browse_dir
    path = os.path.abspath(path)

    if not os.path.isdir(path):
        path = os.path.dirname(path)
    if not os.path.isdir(path):
        path = os.path.abspath(os.getcwd())

    _last_browse_dir = path
    parent = os.path.dirname(path)
    at_root = (parent == path)
    entries = []

    try:
        raw = os.listdir(path)
    except Exception as e:
        return {"current_path": path, "parent_path": parent if not at_root else None,
                "at_root": at_root, "entries": [], "error": str(e), "breadcrumbs": []}

    for name in sorted(raw, key=lambda x: (not os.path.isdir(os.path.join(path, x)), x.lower())):
        full = os.path.join(path, name)
        try:
            is_dir = os.path.isdir(full)
            if is_dir:
                entries.append({"name": name, "full_path": full, "is_dir": True, "size": None})
            elif name.lower().endswith(".csv"):
                entries.append({"name": name, "full_path": full, "is_dir": False,
                                "size": os.path.getsize(full)})
        except:
            continue

    parts = []
    p = path
    while True:
        head, tail = os.path.split(p)
        if tail:
            parts.insert(0, {"name": tail, "path": p})
            p = head
        else:
            parts.insert(0, {"name": p, "path": p})
            break

    return {"current_path": path, "parent_path": parent if not at_root else None,
            "at_root": at_root, "breadcrumbs": parts, "entries": entries, "error": None}


def scan_history_folder(folder_path):
    """Scans a folder for BESO history CSVs (solid-void and lattice)."""
    results = []
    if not os.path.isdir(folder_path):
        return {"error": "Not a valid folder", "runs": []}
    try:
        # --- Solid-void: BESO_History_*.csv ---
        sv_files = sorted([f for f in os.listdir(folder_path)
                           if f.startswith("BESO_History_") and f.endswith(".csv")])
        for fname in sv_files:
            full = os.path.join(folder_path, fname)
            direction = fname.replace("BESO_History_", "").replace(".csv", "")  # e.g. "+Y"
            rows = []
            try:
                with open(full, "r") as f:
                    reader = __import__("csv").DictReader(f)
                    for row in reader:
                        try:
                            rows.append({
                                "iteration": int(row.get("Iteration", 0)),
                                "specific_stiffness": float(row.get("Specific_Stiffness", 0)),
                                "compliance": float(row.get("Compliance", 0)),
                                "volume_fraction": float(row.get("Volume_Fraction", 1))
                            })
                        except:
                            continue
            except:
                continue
            if rows:
                best = max(rows, key=lambda r: r["specific_stiffness"])
                results.append({
                    "name": fname.replace(".csv", ""),
                    "file": fname,
                    "engine": "solid_void",
                    "default_label": direction + " Run",
                    "source_path": folder_path,
                    "rows": rows,
                    "best_iteration": best["iteration"],
                    "best_stiffness": best["specific_stiffness"]
                })

        # --- Lattice: Lattice_Optimization_Data.csv ---
        lat_file = os.path.join(folder_path, "Lattice_Optimization_Data.csv")
        if os.path.exists(lat_file):
            rows = []
            try:
                with open(lat_file, "r") as f:
                    reader = __import__("csv").DictReader(f)
                    for row in reader:
                        try:
                            rows.append({
                                "iteration": int(row.get("Iteration", 0)),
                                "specific_stiffness": float(row.get("Specific_Stiffness", 0)),
                                "compliance": 0.0,
                                "volume_fraction": float(row.get("Mass_Fraction", 1))
                            })
                        except:
                            continue
            except:
                pass
            if rows:
                best = max(rows, key=lambda r: r["specific_stiffness"])
                results.append({
                    "name": "Lattice_Optimization_Data",
                    "file": "Lattice_Optimization_Data.csv",
                    "engine": "lattice",
                    "default_label": "Lattice Run",
                    "source_path": folder_path,
                    "rows": rows,
                    "best_iteration": best["iteration"],
                    "best_stiffness": best["specific_stiffness"]
                })

        return {"error": None, "runs": results}
    except Exception as e:
        return {"error": str(e), "runs": []}


def read_density_manifest(folder_path):
    """Return the lattice_type from the '# BESO_LATTICE ...' line of
    Optimized_Density_Map.csv in folder_path, or None if absent."""
    path = os.path.join(folder_path, "Optimized_Density_Map.csv")
    try:
        with open(path, 'r') as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                if s.startswith('#') and 'BESO_LATTICE' in s.upper():
                    body = s.split('BESO_LATTICE', 1)[1].strip()
                    out = {}
                    for part in body.split(';'):
                        if '=' in part:
                            k, v = part.split('=', 1)
                            out[k.strip()] = v.strip()
                    return out.get('lattice_type')
                if not s.startswith('#'):
                    return None
    except Exception:
        return None
    return None


def scan_stl_folder(folder_path):
    """Checks a folder for STL-generation inputs. Returns engine type and file status."""
    if not os.path.isdir(folder_path):
        return {"valid": False, "engine": None,
                "nodes": False, "elements": False, "solid": False,
                "density_map": False, "detected_lattice_type": None}
    # Solid-void files
    nodes       = os.path.exists(os.path.join(folder_path, "best_nodes.csv"))
    elements    = os.path.exists(os.path.join(folder_path, "best_elements.csv"))
    solid       = os.path.exists(os.path.join(folder_path, "best_solid_elements.csv"))
    sv_valid    = nodes and elements and solid
    # Lattice file
    density_map = os.path.exists(os.path.join(folder_path, "Optimized_Density_Map.csv"))

    # Auto-detect engine from what is present
    if density_map:
        engine = "lattice"
        valid  = True
    elif sv_valid:
        engine = "solid_void"
        valid  = True
    else:
        engine = None
        valid  = False

    return {"valid": valid, "engine": engine,
            "nodes": nodes, "elements": elements, "solid": solid,
            "density_map": density_map,
            "detected_lattice_type": (read_density_manifest(folder_path) if density_map else None)}


def scan_stale_temp_files(work_dir):
    """Scan for leftover .npy temp files from previous crashed lattice STL generations."""
    patterns = [
        os.path.join(work_dir, "*_temp_chunk_*.npy"),
        os.path.join(work_dir, "*_temp_p1_*.npy"),
    ]
    files = []
    for p in patterns:
        files.extend(glob.glob(p))
    return files


def scan_specific_temp_files(output_path, lattice_type):
    """Check for stale temp files matching the current output path + lattice type."""
    if not output_path:
        return []
    base, _ = os.path.splitext(output_path)
    prefix  = "{}_{}".format(base, lattice_type)
    patterns = [
        prefix + "_temp_chunk_*.npy",
        prefix + "_temp_p1_*.npy",
    ]
    files = []
    for p in patterns:
        files.extend(glob.glob(p))
    return files


def _stl_reader_thread(proc):
    """
    Background thread: reads stdout from an STL generator subprocess line by line,
    appends to _stl_state["log"], and sets status to completed or crashed on exit.
    """
    global _stl_state
    try:
        for raw in iter(proc.stdout.readline, b''):
            line = raw.decode('utf-8', errors='replace').rstrip()
            print("[STL] " + line)
            with _stl_lock:
                _stl_state["log"].append(line)
        proc.wait()
        with _stl_lock:
            _stl_state["end_time"] = datetime.now().isoformat()
            _stl_state["status"]   = "completed" if proc.returncode == 0 else "crashed"
    except Exception as e:
        with _stl_lock:
            _stl_state["end_time"] = datetime.now().isoformat()
            _stl_state["status"]   = "crashed"
            _stl_state["log"].append("[Launcher] STL reader error: {}".format(str(e)))


# =============================================================================
#   HTML INTERFACE
# =============================================================================

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BESO Optimizer - Launch Control</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:          #06060c;
    --bg2:         #0c0c14;
    --bg3:         #10101a;
    --bg4:         #15151f;
    --border:      #18182a;
    --border2:     #22223a;
    --violet:      #6366f1;
    --violet2:     #818cf8;
    --violet-dim:  rgba(99,102,241,0.10);
    --violet-glow: rgba(99,102,241,0.22);
    --green:       #4ade80;
    --green-dim:   #14532d;
    --red:         #f87171;
    --red-dim:     #7f1d1d;
    --yellow:      #facc15;
    --yellow-dim:  #713f12;
    --text:        #eaeaf5;
    --text2:       #7878a0;
    --text3:       #484868;
    --font-head:   'DM Sans', sans-serif;
    --font-mono:   'Geist Mono', monospace;
    --font-body:   'DM Sans', sans-serif;

    /* Animation timing */
    --t-fast:   0.12s;
    --t-mid:    0.20s;
    --ease:     cubic-bezier(0.4, 0, 0.2, 1);
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-body);
    font-size: 13px;
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* No background texture - clean surface */
  body::before { display: none; }

  .app { position: relative; z-index: 1; }

  /* ═══════════════════════════════════════════════════
     KEYFRAMES
  ═══════════════════════════════════════════════════ */

  @keyframes fadeIn {
    from { opacity: 0; }
    to   { opacity: 1; }
  }

  @keyframes slideDown {
    from { opacity: 0; transform: translateY(-8px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  @keyframes slideInUp {
    from { opacity: 0; transform: translateY(8px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  @keyframes slideInRight {
    from { opacity: 0; transform: translateX(10px); }
    to   { opacity: 1; transform: translateX(0); }
  }

  @keyframes rowFadeIn {
    from { opacity: 0; transform: translateX(8px); }
    to   { opacity: 1; transform: translateX(0); }
  }

  @keyframes warningSlideIn {
    from { opacity: 0; transform: translateY(-6px); max-height: 0; }
    to   { opacity: 1; transform: translateY(0);    max-height: 300px; }
  }

  @keyframes btnSweep {
    0%   { background-position: -200% center; }
    100% { background-position: 300% center; }
  }

  @keyframes allClearPulse {
    0%   { box-shadow: 0 0 0 0 rgba(74,222,128,0.35); }
    60%  { box-shadow: 0 0 0 10px rgba(74,222,128,0); }
    100% { box-shadow: 0 0 0 0 rgba(74,222,128,0); }
  }

  @keyframes completionFlash {
    0%   { box-shadow: 0 0 0 0 var(--violet-glow), inset 0 0 20px rgba(99,102,241,0.3); }
    50%  { box-shadow: 0 0 0 10px rgba(99,102,241,0), inset 0 0 0 transparent; }
    100% { box-shadow: none; }
  }

  @keyframes panelGlow {
    0%   { box-shadow: 0 0 0 0 var(--violet-glow); }
    100% { box-shadow: 0 0 24px 0 rgba(99,102,241,0); }
  }

  @keyframes pulseRing {
    0%   { transform: scale(1);   opacity: 0.7; }
    100% { transform: scale(2.4); opacity: 0; }
  }

  /* ---- TOP BAR ---- */
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 28px;
    height: 52px;
    background: rgba(6,6,12,0.92);
    border-bottom: 1px solid var(--border);
    backdrop-filter: blur(12px);
    position: sticky;
    top: 0;
    z-index: 100;
  }

  .topbar-left {
    display: flex;
    align-items: center;
    gap: 14px;
  }

  .logo-mark {
    width: 28px;
    height: 28px;
    background: transparent;
    border: 1.5px solid var(--violet);
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: var(--font-head);
    font-weight: 600;
    font-size: 12px;
    color: var(--violet);
    flex-shrink: 0;
    letter-spacing: 0;
  }

  .topbar-title {
    font-family: var(--font-head);
    font-size: 16px;
    font-weight: 500;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--text);
  }

  .topbar-title span { color: var(--violet); }

  .topbar-right {
    display: flex;
    align-items: center;
    gap: 20px;
  }

  .topbar-badge {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--text3);
    letter-spacing: 0.1em;
  }

  /* ---- GLOBAL BANNER ---- */
  .global-banner {
    display: none;
    margin: 16px 28px 0;
    padding: 14px 20px;
    border-radius: 6px;
    border: 1px solid;
    font-size: 13px;
    align-items: center;
    gap: 14px;
    animation: slideDown 0.3s ease;
  }

  .global-banner.config-found {
    display: flex;
    background: rgba(99,102,241,0.06);
    border-color: var(--border2);
    color: var(--text);
  }

  .global-banner.running {
    display: flex;
    background: rgba(34,197,94,0.08);
    border-color: var(--green-dim);
    color: var(--text);
  }

  .global-banner.completed {
    display: flex;
    background: rgba(34,197,94,0.1);
    border-color: var(--green);
    color: var(--text);
  }

  .global-banner.crashed {
    display: flex;
    background: rgba(239,68,68,0.08);
    border-color: rgba(239,68,68,0.35);
    color: var(--text);
  }

  .global-banner.prev-session {
    display: flex;
    background: rgba(99,102,241,0.08);
    border-color: var(--violet-dim);
    color: var(--text);
  }

  .banner-body { flex: 1; min-width: 0; }
  .banner-status { font-size: 13px; font-weight: 700; }
  .banner-metrics {
    font-size: 11px;
    color: var(--text3);
    margin-top: 3px;
    font-family: var(--font-mono);
  }
  .banner-right {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-shrink: 0;
  }
  .banner-elapsed {
    font-size: 11px;
    color: var(--text3);
    font-family: var(--font-mono);
  }

  /* ---- LOG MODAL ---- */
  .log-modal-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.8);
    z-index: 600;
    align-items: center;
    justify-content: center;
    animation: fadeIn 0.15s ease;
  }
  .log-modal-overlay.open { display: flex; }

  .log-modal {
    width: min(960px, 95vw);
    height: min(680px, 88vh);
    background: var(--bg2);
    border: 1px solid var(--border2);
    border-radius: 10px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .log-modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 20px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }

  .log-modal-title {
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text);
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .log-status-badge {
    font-size: 10px;
    padding: 2px 8px;
    border-radius: 10px;
    font-weight: 600;
    letter-spacing: 0.05em;
  }
  .log-status-badge.running  { background: rgba(34,197,94,0.15);   color: var(--green);  border: 1px solid var(--green-dim); }
  .log-status-badge.completed{ background: rgba(34,197,94,0.15);   color: var(--green);  border: 1px solid var(--green-dim); }
  .log-status-badge.crashed  { background: rgba(239,68,68,0.15);   color: #ef4444;       border: 1px solid rgba(239,68,68,0.3); }

  .log-modal-close {
    background: none;
    border: none;
    color: var(--text3);
    font-size: 18px;
    cursor: pointer;
    padding: 2px 6px;
    border-radius: 4px;
    line-height: 1;
  }
  .log-modal-close:hover { background: var(--bg3); color: var(--text); }

  .log-body {
    flex: 1;
    overflow-y: auto;
    background: var(--bg1);
    padding: 12px 16px;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text2);
    line-height: 1.65;
  }

  .log-line { margin: 0; white-space: pre-wrap; word-break: break-all; }
  .log-line.ll-error   { color: #ef4444; }
  .log-line.ll-warn    { color: var(--violet); }
  .log-line.ll-success { color: var(--green); }
  .log-line.ll-section { color: var(--text); font-weight: 700; }

  /* ---- FILE BROWSER MODAL ---- */
  .modal-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.75);
    z-index: 500;
    align-items: center;
    justify-content: center;
    animation: fadeIn 0.15s ease;
  }

  .modal-overlay.open { display: flex; }

  @keyframes fadeIn {
    from { opacity: 0; }
    to   { opacity: 1; }
  }

  .modal {
    width: 680px;
    max-width: 95vw;
    max-height: 80vh;
    background: var(--bg2);
    border: 1px solid var(--border2);
    border-radius: 10px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    animation: modalSlide 0.2s ease;
    box-shadow: 0 24px 60px rgba(0,0,0,0.6);
  }

  @keyframes modalSlide {
    from { transform: translateY(-16px); opacity: 0; }
    to   { transform: translateY(0);     opacity: 1; }
  }

  .modal-header {
    padding: 16px 20px 12px;
    border-bottom: 1px solid var(--border);
    background: var(--bg3);
    flex-shrink: 0;
  }

  .modal-title {
    font-family: var(--font-head);
    font-size: 15px;
    font-weight: 700;
    letter-spacing: 0.06em;
    color: var(--text);
    margin-bottom: 10px;
  }

  /* Breadcrumb */
  .breadcrumb {
    display: flex;
    align-items: center;
    gap: 0;
    flex-wrap: wrap;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text3);
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 6px 10px;
    overflow-x: auto;
  }

  .breadcrumb-part {
    color: var(--text2);
    cursor: pointer;
    white-space: nowrap;
    transition: color 0.15s;
  }

  .breadcrumb-part:hover { color: var(--violet); }
  .breadcrumb-part.last  { color: var(--violet); cursor: default; }

  .breadcrumb-sep {
    margin: 0 4px;
    color: var(--text3);
    user-select: none;
  }

  /* Toolbar */
  .modal-toolbar {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 20px;
    border-bottom: 1px solid var(--border);
    background: var(--bg3);
    flex-shrink: 0;
  }

  .btn-up {
    padding: 6px 12px;
    background: var(--bg4);
    border: 1px solid var(--border);
    border-radius: 4px;
    color: var(--text2);
    font-family: var(--font-head);
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
    transition: all 0.15s;
    flex-shrink: 0;
  }

  .btn-up:hover:not(:disabled) {
    border-color: var(--violet);
    color: var(--violet);
  }

  .btn-up:disabled {
    opacity: 0.35;
    cursor: not-allowed;
  }

  .toolbar-path {
    flex: 1;
    background: var(--bg4);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 6px 10px;
    color: var(--text);
    font-family: var(--font-mono);
    font-size: 11px;
    outline: none;
    transition: border-color 0.15s;
  }

  .toolbar-path:focus { border-color: var(--violet); }

  .btn-go {
    padding: 6px 12px;
    background: var(--violet);
    border: none;
    border-radius: 4px;
    color: white;
    font-family: var(--font-head);
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.15s;
    flex-shrink: 0;
  }

  .btn-go:hover { background: var(--violet2); }

  /* Drive selector (Windows) */
  .drive-bar {
    display: none;
    padding: 8px 20px;
    gap: 6px;
    flex-wrap: wrap;
    border-bottom: 1px solid var(--border);
    background: var(--bg3);
    flex-shrink: 0;
  }

  .drive-bar.visible { display: flex; }

  .btn-drive {
    padding: 4px 10px;
    background: var(--bg4);
    border: 1px solid var(--border);
    border-radius: 3px;
    color: var(--text2);
    font-family: var(--font-mono);
    font-size: 11px;
    cursor: pointer;
    transition: all 0.15s;
  }

  .btn-drive:hover {
    border-color: var(--violet);
    color: var(--violet);
  }

  /* File list */
  .modal-filelist {
    flex: 1;
    overflow-y: auto;
    padding: 8px 12px;
  }

  .modal-filelist::-webkit-scrollbar { width: 6px; }
  .modal-filelist::-webkit-scrollbar-track { background: var(--bg); }
  .modal-filelist::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 3px; }

  .file-entry {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 10px;
    border-radius: 5px;
    cursor: pointer;
    transition: background 0.1s;
    border: 1px solid transparent;
  }

  .file-entry:hover { background: var(--bg3); }

  .file-entry.selected {
    background: rgba(99,102,241,0.12);
    border-color: rgba(99,102,241,0.35);
  }

  .file-entry.is-dir { cursor: pointer; }

  .file-icon {
    font-size: 16px;
    width: 20px;
    text-align: center;
    flex-shrink: 0;
  }

  .file-name {
    flex: 1;
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .file-entry.is-dir .file-name { color: var(--text2); }
  .file-entry.selected .file-name { color: var(--text); }

  .file-size {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--text3);
    flex-shrink: 0;
  }

  .filelist-empty {
    padding: 32px;
    text-align: center;
    color: var(--text3);
    font-size: 12px;
    font-family: var(--font-mono);
  }

  .filelist-error {
    padding: 16px;
    text-align: center;
    color: var(--red);
    font-size: 12px;
    font-family: var(--font-mono);
  }

  /* Modal footer */
  .modal-footer {
    padding: 12px 20px;
    border-top: 1px solid var(--border);
    background: var(--bg3);
    display: flex;
    align-items: center;
    gap: 10px;
    flex-shrink: 0;
  }

  .modal-selected-path {
    flex: 1;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text3);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .modal-selected-path.has-file { color: var(--text2); }

  .btn-modal-cancel {
    padding: 8px 16px;
    background: transparent;
    border: 1px solid var(--border2);
    border-radius: 4px;
    color: var(--text2);
    font-family: var(--font-head);
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s;
  }

  .btn-modal-cancel:hover { border-color: var(--text3); color: var(--text); }

  .btn-modal-select {
    padding: 8px 20px;
    background: var(--violet);
    border: none;
    border-radius: 4px;
    color: white;
    font-family: var(--font-head);
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.05em;
    cursor: pointer;
    transition: background 0.15s;
  }

  .btn-modal-select:hover:not(:disabled) { background: var(--violet2); }

  .btn-modal-select:disabled {
    background: var(--bg4);
    color: var(--text3);
    cursor: not-allowed;
  }

  @keyframes slideDown {
    from { opacity: 0; transform: translateY(-8px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  .banner-icon { font-size: 18px; flex-shrink: 0; }
  .banner-text { flex: 1; line-height: 1.5; }
  .banner-text strong { color: var(--violet); }

  .banner-actions {
    display: flex;
    gap: 8px;
    flex-shrink: 0;
  }

  .btn-banner {
    padding: 6px 14px;
    border-radius: 4px;
    font-family: var(--font-head);
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.05em;
    cursor: pointer;
    border: 1px solid;
    transition: all 0.15s;
  }

  .btn-banner.yes {
    background: var(--violet);
    border-color: var(--violet);
    color: white;
  }

  .btn-banner.yes:hover { background: var(--violet2); }

  .btn-banner.no {
    background: transparent;
    border-color: var(--border2);
    color: var(--text2);
  }

  .btn-banner.no:hover { border-color: var(--text3); color: var(--text); }

  /* ---- MAIN GRID ---- */
  .main-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    padding: 16px 28px 28px;
    align-items: start;
  }

  /* ---- PANEL ---- */
  .panel {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    transition: border-color var(--t-fast) var(--ease),
                box-shadow var(--t-fast) var(--ease);
    box-shadow: 0 2px 20px rgba(0,0,0,0.35);
  }

  .panel:hover {
    border-color: rgba(99,102,241,0.22);
    box-shadow: 0 4px 36px rgba(0,0,0,0.5),
                0 0 28px rgba(99,102,241,0.10),
                0 0 0 1px rgba(99,102,241,0.06);
  }

  .panel:focus-within {
    border-color: rgba(99,102,241,0.38);
    box-shadow: 0 4px 36px rgba(0,0,0,0.5),
                inset 0 0 50px rgba(99,102,241,0.04),
                0 0 28px rgba(99,102,241,0.14),
                0 0 0 1px rgba(99,102,241,0.10);
  }

  .panel-header {
    padding: 14px 18px 12px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 10px;
    background: linear-gradient(135deg, var(--bg2) 0%, rgba(99,102,241,0.03) 100%);
  }

  .panel-number {
    width: 20px;
    height: 20px;
    border-radius: 4px;
    background: transparent;
    border: 1px solid rgba(99,102,241,0.25);
    color: var(--violet2);
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 500;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }

  .panel-title {
    font-family: var(--font-head);
    font-size: 15px;
    font-weight: 700;
    letter-spacing: 0.06em;
    color: var(--text);
  }

  .panel-body { padding: 18px; }

  /* ---- PAGE CONTAINERS -- instant switch ---- */
  .page        { display: none; }
  .page.active { display: block; }

  /* ---- WARNING BLOCK ANIMATIONS ---- */
  .stl-warning-block.visible {
    animation: warningSlideIn var(--t-fast) var(--ease) forwards;
    overflow: hidden;
  }

  /* ---- PRE-FLIGHT ROW STAGGER ---- */
  .pf-row-animate {
    animation: rowFadeIn var(--t-fast) var(--ease) both;
  }

  /* ---- SUBGRID ICON INDICATOR ---- */
  .subgrid-icon-wrap { position: relative; }
  .subgrid-icon-wrap input { padding-right: 30px; }

  .subgrid-status-icon {
    position: absolute;
    right: 9px;
    top: 50%;
    transform: translateY(-50%);
    font-size: 13px;
    line-height: 1;
    cursor: help;
    pointer-events: auto;
    z-index: 2;
    /* NOTE: do NOT override to position:relative - absolute elements
       are valid positioning contexts for ::after pseudo-elements */
  }

  /* Tooltip - positioned relative to the icon (which is absolute, hence a positioned ctx) */
  .subgrid-status-icon[data-tip]:hover::after {
    content: attr(data-tip);
    position: absolute;
    bottom: calc(100% + 6px);
    right: -4px;
    background: var(--bg3);
    border: 1px solid var(--border2);
    border-radius: 5px;
    padding: 5px 9px;
    font-size: 10px;
    white-space: nowrap;
    color: var(--text2);
    font-family: var(--font-mono);
    z-index: 100;
    pointer-events: none;
    letter-spacing: 0.04em;
    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
  }

  /* ---- FORM ELEMENTS ---- */
  .field { margin-bottom: 16px; }
  .field:last-child { margin-bottom: 0; }

  label {
    display: block;
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text3);
    margin-bottom: 6px;
  }

  input[type="text"],
  input[type="number"],
  select {
    width: 100%;
    background: var(--bg4);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 8px 10px;
    color: var(--text);
    font-family: var(--font-mono);
    font-size: 12px;
    outline: none;
    transition: border-color 0.15s;
    appearance: none;
    -webkit-appearance: none;
  }

  select {
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%23555577'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 10px center;
    padding-right: 28px;
    cursor: pointer;
  }

  select option {
    background: #16161f;
    color: var(--text);
  }

  input[type="text"]:focus,
  input[type="number"]:focus {
    border-color: var(--violet);
  }

  input[type="text"]::placeholder,
  input[type="number"]::placeholder {
    color: var(--text3);
  }

  /* Browse row */
  .browse-row {
    display: flex;
    gap: 8px;
    align-items: stretch;
  }

  .browse-row input { flex: 1; }

  .btn-browse {
    padding: 0 12px;
    background: var(--bg4);
    border: 1px solid var(--border);
    border-radius: 4px;
    color: var(--text2);
    font-family: var(--font-head);
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
    transition: all 0.15s;
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .btn-browse:hover {
    border-color: var(--violet);
    color: var(--violet);
  }

  /* Toggle pair */
  .toggle-group {
    display: flex;
    gap: 0;
    border-radius: 4px;
    overflow: hidden;
    border: 1px solid var(--border);
  }

  .toggle-group input[type="radio"] { display: none; }

  .toggle-group label {
    flex: 1;
    text-align: center;
    padding: 8px 6px;
    background: var(--bg4);
    color: var(--text3);
    cursor: pointer;
    font-family: var(--font-head);
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: none;
    transition: all 0.15s;
    border: none;
    margin: 0;
  }

  .toggle-group label:not(:last-child) {
    border-right: 1px solid var(--border);
  }

  .toggle-group input[type="radio"]:checked + label {
    background: var(--violet);
    color: white;
  }
  .toggle-group input[type="radio"]:disabled + label {
    opacity: 0.4;
    cursor: not-allowed;
  }

  /* Lattice type radio list (always-visible options with circular dot) */
  .lattice-radio-list { display: flex; flex-direction: column; gap: 4px; }
  .lattice-radio {
    display: flex; align-items: center; gap: 10px;
    padding: 6px 11px;
    border: 1px solid var(--border2);
    border-radius: 6px; cursor: pointer;
    background: var(--bg4); transition: all 0.15s;
  }
  .lattice-radio:hover { border-color: var(--text3); }
  .lattice-radio input[type="radio"] { display: none; }
  .lattice-radio-dot {
    width: 14px; height: 14px; border-radius: 50%;
    border: 2px solid var(--border2); flex-shrink: 0;
    position: relative; transition: all 0.15s;
  }
  .lattice-radio input[type="radio"]:checked + .lattice-radio-dot { border-color: var(--violet); }
  .lattice-radio input[type="radio"]:checked + .lattice-radio-dot::after {
    content: ""; position: absolute; top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    width: 7px; height: 7px; border-radius: 50%; background: var(--violet);
  }
  .lattice-radio:has(input[type="radio"]:checked) {
    border-color: var(--violet); background: var(--violet-dim);
  }
  .lattice-radio:has(input[type="radio"]:checked) .lattice-radio-text { color: var(--text); }
  .lattice-radio-text {
    display: flex; flex-direction: column; gap: 1px;
    font-family: var(--font-head); font-size: 12px; color: var(--text2); line-height: 1.3;
  }
  .lattice-radio-name { display: inline-flex; align-items: center; }
  .lattice-radio-coef {
    font-family: var(--font-mono); font-size: 8.5px; color: var(--text3); letter-spacing: 0.02em; white-space: nowrap;
  }

  /* Direction grid */
  .dir-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 6px;
  }

  .dir-grid input[type="radio"] { display: none; }

  .dir-grid label {
    text-align: center;
    padding: 8px 4px;
    background: var(--bg4);
    border: 1px solid var(--border);
    border-radius: 4px;
    color: var(--text3);
    cursor: pointer;
    font-family: var(--font-mono);
    font-size: 12px;
    font-weight: 500;
    text-transform: none;
    letter-spacing: 0.05em;
    transition: all 0.15s;
    margin: 0;
  }

  .dir-grid label:hover {
    border-color: var(--violet);
    color: var(--violet);
  }

  .dir-grid input[type="radio"]:checked + label {
    background: rgba(99,102,241,0.12);
    border-color: var(--violet);
    color: var(--violet);
  }

  /* Slider */
  .slider-row {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .slider-row input[type="range"] {
    flex: 1;
    -webkit-appearance: none;
    height: 4px;
    border-radius: 2px;
    background: var(--bg4);
    border: 1px solid var(--border);
    outline: none;
    cursor: pointer;
  }

  .slider-row input[type="range"]::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: var(--violet);
    cursor: pointer;
  }

  .slider-val {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--violet);
    width: 36px;
    text-align: right;
    flex-shrink: 0;
  }

  /* Detected properties */
  .detected-panel {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 12px;
    margin-top: 10px;
  }

  .detected-panel.hidden { display: none; }

  .detected-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 4px 0;
    border-bottom: 1px solid var(--border);
    font-size: 11px;
  }

  .detected-row:last-child { border-bottom: none; }

  .detected-key {
    color: var(--text3);
    font-family: var(--font-mono);
  }

  .detected-val {
    color: var(--text);
    font-family: var(--font-mono);
    font-weight: 500;
  }

  .detected-val.good { color: var(--green); }
  .detected-val.warn { color: var(--yellow); }
  .detected-val.bad  { color: var(--red); }
  /* Design part names can be long; show full when they fit, ellipsis (with
     full name on hover) only when too long. The key never shrinks. */
  #detected-designpart-row .detected-key { flex-shrink: 0; white-space: nowrap; }
  #d-designPart { min-width: 0; overflow: hidden;
                  text-overflow: ellipsis; white-space: nowrap; }

  /* Custom weights conditional field */
  .custom-weights-field { display: none; margin-top: 10px; }
  .custom-weights-field.visible { display: block; }

  /* ---- VALIDATION CHECKLIST ---- */
  .checklist { display: flex; flex-direction: column; gap: 8px; }

  .check-item {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 5px;
    transition: border-color 0.2s;
  }

  .check-item.pass  { border-color: var(--border); background: var(--bg2); }
  .check-item.fail  { border-color: var(--red-dim); }
  .check-item.warn  { border-color: var(--yellow-dim); }
  .check-item.idle  { border-color: var(--border); }

  .check-icon {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
    margin-top: 0;
    align-self: center;
  }

  .check-item.pass .check-icon  { background: var(--green); }
  .check-item.fail .check-icon  { background: var(--red); }
  .check-item.warn .check-icon  { background: var(--yellow); }
  .check-item.idle .check-icon  { background: var(--border2); }

  .check-content { flex: 1; }

  .check-label {
    font-size: 12px;
    font-weight: 500;
    color: var(--text);
    margin-bottom: 2px;
  }

  .check-detail {
    font-size: 11px;
    font-family: var(--font-mono);
    color: var(--text3);
    line-height: 1.4;
  }

  .check-item.pass .check-detail { color: var(--green); opacity: 0.7; }
  .check-item.fail .check-detail { color: var(--red);   opacity: 0.8; }
  .check-item.warn .check-detail { color: var(--yellow); opacity: 0.8; }

  /* ---- LAUNCH BUTTON ---- */
  /* ---- ENGINE SELECTOR ---- */
  .engine-selector {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 18px;
    margin-bottom: 18px;
    display: flex;
    align-items: center;
    gap: 16px;
  }
  .engine-selector-label {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--text3);
    white-space: nowrap;
  }
  .engine-cards {
    display: flex;
    gap: 10px;
    flex: 1;
  }
  .engine-cards input[type="radio"] { display: none; }
  .engine-card {
    flex: 1;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 14px;
    border: 1px solid var(--border);
    border-radius: 8px;
    cursor: pointer;
    background: var(--bg3);
    transition: border-color 0.18s, background 0.18s;
  }
  .engine-cards input[type="radio"]:checked + .engine-card {
    border-color: var(--violet);
    background: rgba(74,111,165,0.1);
  }
  .engine-card-icon { font-size: 20px; line-height: 1; }
  .engine-card-text { display: flex; flex-direction: column; gap: 2px; }
  .engine-card-name { font-size: 12px; font-weight: 700; color: var(--text); letter-spacing: 0.03em; }
  .engine-card-desc { font-size: 10px; color: var(--text3); }

  /* Show/hide engine-specific fields */
  .hidden-field { display: none !important; }

  .launch-section { margin-top: 16px; }

  /* Launch + Cancel side-by-side row */
  .launch-btn-row {
    display: flex;
    gap: 8px;
    align-items: stretch;
  }
  .launch-btn-row .btn-launch { flex: 1; width: auto; }

  .btn-cancel-run {
    display: none;
    flex-shrink: 0;
    padding: 0 14px;
    background: rgba(239,68,68,0.07);
    border: 1px solid rgba(239,68,68,0.5);
    color: #ef4444;
    border-radius: 6px;
    font-family: var(--font-head);
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.08em;
    cursor: pointer;
    white-space: nowrap;
    transition: background 0.15s;
  }
  .btn-cancel-run:hover { background: rgba(239,68,68,0.16); }
  .btn-cancel-run.visible { display: flex; align-items: center; gap: 5px; }

  /* Cancelled state on launch button */
  .btn-launch.cancelled {
    background: transparent !important;
    border: 1px solid #ef4444 !important;
    color: #ef4444 !important;
    box-shadow: none !important;
    cursor: default;
  }
  .cancel-drain-bar {
    position: absolute;
    bottom: 0;
    left: 0;
    height: 2px;
    width: 100%;
    background: #ef4444;
    animation: cancelDrain 1s linear forwards;
    pointer-events: none;
  }
  @keyframes cancelDrain {
    from { width: 100%; opacity: 1; }
    to   { width: 0%;   opacity: 0.4; }
  }

  /* Cancel confirmation modal */
  .cancel-confirm-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.65);
    z-index: 700;
    align-items: center;
    justify-content: center;
  }
  .cancel-confirm-overlay.open { display: flex; }
  .cancel-confirm-box {
    background: var(--bg3);
    border: 1px solid rgba(239,68,68,0.3);
    border-radius: 10px;
    padding: 24px 28px;
    max-width: 420px;
    width: 92%;
    box-shadow: 0 16px 48px rgba(0,0,0,0.7);
  }
  .cancel-confirm-title {
    font-family: var(--font-head);
    font-size: 13px;
    font-weight: 600;
    color: #ef4444;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    margin-bottom: 12px;
  }
  .cancel-confirm-body {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--text2);
    line-height: 1.7;
    margin-bottom: 20px;
  }
  .cancel-confirm-body .keep-note {
    color: var(--text3);
    font-size: 11px;
    margin-top: 8px;
    display: block;
  }
  .cancel-confirm-actions {
    display: flex;
    gap: 10px;
    justify-content: flex-end;
  }
  .cancel-confirm-actions button {
    padding: 8px 18px;
    border-radius: 5px;
    font-family: var(--font-head);
    font-size: 12px;
    font-weight: 500;
    letter-spacing: 0.06em;
    cursor: pointer;
    transition: all 0.15s;
  }
  .btn-keep-running {
    background: transparent;
    border: 1px solid var(--border2);
    color: var(--text2);
  }
  .btn-keep-running:hover { border-color: var(--text2); color: var(--text); }
  .btn-confirm-cancel {
    background: rgba(239,68,68,0.1);
    border: 1px solid #ef4444;
    color: #ef4444;
  }
  .btn-confirm-cancel:hover { background: rgba(239,68,68,0.2); }

  /* Stale files warning modal */
  .stale-files-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.65);
    z-index: 700;
    align-items: center;
    justify-content: center;
  }
  .stale-files-overlay.open { display: flex; }
  .stale-files-box {
    background: var(--bg3);
    border: 1px solid rgba(251,191,36,0.35);
    border-radius: 10px;
    padding: 24px 28px;
    max-width: 480px;
    width: 92%;
    box-shadow: 0 16px 48px rgba(0,0,0,0.7);
  }
  .stale-files-title {
    font-family: var(--font-head);
    font-size: 13px;
    font-weight: 600;
    color: #fbbf24;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    margin-bottom: 6px;
  }
  .stale-files-dir {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--text3);
    margin-bottom: 12px;
    word-break: break-all;
    line-height: 1.5;
  }
  .stale-files-desc {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text2);
    line-height: 1.7;
    margin-bottom: 14px;
  }
  .stale-files-list {
    background: var(--bg4);
    border: 1px solid var(--border2);
    border-radius: 4px;
    padding: 10px 12px;
    max-height: 160px;
    overflow-y: auto;
    margin-bottom: 20px;
  }
  .stale-file-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text2);
    padding: 3px 0;
    border-bottom: 1px solid var(--border2);
  }
  .stale-file-row:last-child { border-bottom: none; }
  .stale-file-icon { color: var(--text3); font-size: 10px; flex-shrink: 0; }
  .stale-files-actions {
    display: flex;
    gap: 10px;
    justify-content: flex-end;
  }
  .stale-files-actions button {
    padding: 8px 18px;
    border-radius: 5px;
    font-family: var(--font-head);
    font-size: 12px;
    font-weight: 500;
    letter-spacing: 0.06em;
    cursor: pointer;
    transition: all 0.15s;
  }
  .btn-stale-abort {
    background: transparent;
    border: 1px solid var(--border2);
    color: var(--text2);
  }
  .btn-stale-abort:hover { border-color: var(--text2); color: var(--text); }
  .btn-stale-confirm {
    background: rgba(251,191,36,0.1);
    border: 1px solid #fbbf24;
    color: #fbbf24;
  }
  .btn-stale-confirm:hover { background: rgba(251,191,36,0.2); }

  .btn-launch {
    width: 100%;
    padding: 13px;
    background: var(--violet);
    border: none;
    border-radius: 6px;
    color: white;
    font-family: var(--font-head);
    font-size: 13px;
    font-weight: 500;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    cursor: pointer;
    transition: background var(--t-fast) var(--ease),
                box-shadow var(--t-fast) var(--ease);
    position: relative;
    overflow: hidden;
  }

  /* Sweep shimmer layer - fires once on launch */
  .btn-launch::after {
    content: '';
    position: absolute;
    inset: 0;
    background: linear-gradient(
      105deg,
      transparent 20%,
      rgba(255,255,255,0.18) 50%,
      transparent 80%
    );
    background-size: 300% 100%;
    background-position: -200% center;
    opacity: 0;
    pointer-events: none;
  }

  .btn-launch.sweeping::after {
    opacity: 1;
    animation: btnSweep 0.55s var(--ease) forwards;
  }

  .btn-launch:hover:not(:disabled) {
    background: var(--violet2);
    box-shadow: 0 0 20px rgba(99,102,241,0.35);
  }

  .btn-launch:active:not(:disabled) { transform: translateY(1px); }

  .btn-launch:disabled {
    background: var(--bg4);
    color: var(--text3);
    cursor: not-allowed;
    box-shadow: none;
  }

  .btn-launch.running {
    background: var(--green-dim);
    color: var(--green);
    cursor: not-allowed;
    box-shadow: 0 0 12px rgba(74,222,128,0.15);
  }

  .btn-launch.completed {
    background: rgba(34,197,94,0.18);
    color: var(--green);
    border: 1px solid var(--green-dim);
    cursor: not-allowed;
    animation: completionFlash 0.8s var(--ease) forwards;
  }

  .btn-launch.crashed {
    background: rgba(239,68,68,0.12);
    color: #ef4444;
    border: 1px solid rgba(239,68,68,0.3);
    cursor: not-allowed;
  }

  /* Pre-flight all-clear glow on the preflight panel */
  .preflight-allclear {
    animation: allClearPulse 0.8s var(--ease) forwards;
  }

  /* ---- RUN INFO PANEL (Panel 4, below launch button) ---- */
  .run-info {
    margin-top: 8px;
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 9px 13px;
    font-family: var(--font-mono);
    transition: border-color 0.2s;
  }
  .run-info.ri-running   { border-color: var(--green-dim); }
  .run-info.ri-completed { border-color: var(--green); }
  .run-info.ri-crashed   { border-color: rgba(239,68,68,0.4); }

  .run-info-metrics {
    font-size: 11px;
    color: var(--text2);
    margin-bottom: 6px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .run-info-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .run-info-elapsed {
    font-size: 10px;
    color: var(--text3);
  }
  .btn-extend {
    font-size: 10px;
    padding: 3px 10px;
    background: transparent;
    border: 1px solid var(--border2);
    border-radius: 4px;
    color: var(--text2);
    cursor: pointer;
    font-family: var(--font-head);
    letter-spacing: 0.05em;
    transition: border-color 0.15s, color 0.15s;
  }
  .btn-extend:hover { border-color: var(--violet); color: var(--violet); }

  .btn-relaunch {
    width: 100%;
    margin-top: 8px;
    padding: 10px;
    background: transparent;
    border: 1px solid var(--border2);
    border-radius: 6px;
    color: var(--text2);
    font-family: var(--font-head);
    font-size: 14px;
    font-weight: 600;
    letter-spacing: 0.06em;
    cursor: pointer;
    transition: all 0.15s;
    display: none;
  }

  .btn-relaunch:hover {
    border-color: var(--violet);
    color: var(--violet);
  }

  /* ---- DIVIDER ---- */
  .divider {
    height: 1px;
    background: var(--border);
    margin: 14px 0;
  }

  /* ---- MULTI-COLUMN FIELD ROWS ---- */
  .field-row-3 {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 8px;
    margin-bottom: 10px;
  }
  .field-row-2 {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    margin-bottom: 10px;
  }
  .field-row-3 .field,
  .field-row-2 .field { margin-bottom: 0; }
  .field-row-3 .field-hint,
  .field-row-2 .field-hint { font-size: 9px; }

  /* ---- COLLAPSIBLE SECTIONS ---- */
  .collapsible-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    cursor: pointer;
    padding: 7px 0;
    user-select: none;
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--violet);
    border-bottom: 1px solid var(--violet-dim);
    margin-bottom: 0;
  }
  .collapsible-header:hover { color: var(--text); }
  .collapsible-arrow {
    font-size: 9px;
    color: var(--text3);
    transition: transform 0.2s ease;
    transform: rotate(0deg);
  }
  .collapsible-header.open .collapsible-arrow { transform: rotate(90deg); }
  .collapsible-body {
    display: none;
    padding-top: 10px;
  }
  .collapsible-body.open { display: block; }

  /* ---- SECTION SUBTITLE ---- */
  .section-sub {
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--violet);
    margin-bottom: 10px;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .section-sub::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--violet-dim);
  }

  /* Spinner */
  @keyframes spin { to { transform: rotate(360deg); } }
  .spinner {
    display: inline-block;
    width: 14px;
    height: 14px;
    border: 2px solid rgba(249,115,22,0.3);
    border-top-color: var(--violet);
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
    vertical-align: middle;
    margin-right: 6px;
  }

  /* Pulse dot for running state */
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }

  .pulse-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--green);
    animation: pulse 1.5s ease infinite;
    margin-right: 6px;
    vertical-align: middle;
  }

  /* Hidden file input */
  #fileInput { display: none; }

  /* Tooltip helper */
  .field-hint {
    font-size: 10px;
    color: var(--text3);
    margin-top: 4px;
    font-family: var(--font-mono);
  }

  /* ---- NAV TABS ---- */
  .nav-tabs {
    display: flex;
    align-items: center;
    gap: 4px;
    position: absolute;
    left: 50%;
    transform: translateX(-50%);
  }

  .nav-tab {
    padding: 8px 28px;
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text3);
    font-family: var(--font-head);
    font-size: 13px;
    font-weight: 500;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    cursor: pointer;
    transition: all 0.15s;
    margin-bottom: -1px;
  }

  .nav-tab:hover { color: var(--text2); }

  .nav-tab.active {
    color: var(--violet);
    border-bottom-color: var(--violet);
  }

  /* ---- HISTORY PAGE ---- */
  .history-container {
    padding: 20px 28px 28px;
  }

  .history-top {
    display: flex;
    align-items: flex-end;
    gap: 12px;
    margin-bottom: 20px;
    flex-wrap: wrap;
  }

  .history-top .field { margin-bottom: 0; flex: 1; min-width: 300px; }

  .chart-wrapper {
    position: relative;
    height: 420px;
  }

  .btn-scan {
    padding: 9px 20px;
    background: var(--violet);
    border: none;
    border-radius: 4px;
    color: white;
    font-family: var(--font-head);
    font-size: 12px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    cursor: pointer;
    transition: background 0.15s;
    white-space: nowrap;
    flex-shrink: 0;
  }

  .btn-scan:hover { background: var(--violet2); }

  .history-empty {
    text-align: center;
    padding: 60px;
    color: var(--text3);
    font-family: var(--font-mono);
    font-size: 12px;
  }

  .run-summary {
    display: flex;
    gap: 12px;
    margin-bottom: 16px;
    flex-wrap: wrap;
  }

  .run-badge {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 12px;
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 4px;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text2);
  }

  .run-badge-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .chart-panel {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 20px;
  }

  .chart-panel canvas { width: 100% !important; height: 100% !important; }

  .btn-add-run {
    padding: 9px 16px;
    background: transparent;
    border: 1px solid var(--border2);
    border-radius: 4px;
    color: var(--text2);
    font-family: var(--font-head);
    font-size: 12px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    cursor: pointer;
    transition: all 0.15s;
    white-space: nowrap;
    flex-shrink: 0;
    align-self: center;
    margin-top: 3px;
  }

  .btn-add-run:hover { border-color: var(--violet); color: var(--violet); }

  .run-badge .remove-run {
    margin-left: 4px;
    color: var(--text3);
    cursor: pointer;
    font-size: 13px;
    line-height: 1;
  }

  .run-badge .remove-run:hover { color: var(--red); }

  .run-badge-icon {
    margin-left: 1px;
    color: var(--text3);
    cursor: pointer;
    font-size: 12px;
    line-height: 1;
    transition: color 0.12s;
    user-select: none;
  }
  .run-badge-icon.pencil:hover { color: var(--violet); }
  .run-badge-icon.info-btn:hover { color: var(--text); }

  /* Label / Rename modal */
  .run-label-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.55);
    z-index: 600;
    align-items: center;
    justify-content: center;
  }
  .run-label-overlay.open { display: flex; }
  .run-label-box {
    background: var(--bg3);
    border: 1px solid var(--border2);
    border-radius: 10px;
    padding: 24px 28px;
    max-width: 460px;
    width: 92%;
    box-shadow: 0 16px 48px rgba(0,0,0,0.6);
  }
  .run-label-title {
    font-family: var(--font-head);
    font-size: 13px;
    font-weight: 600;
    color: var(--text);
    margin-bottom: 18px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .run-label-row { margin-bottom: 14px; }
  .run-label-row label {
    display: block;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text3);
    margin-bottom: 5px;
  }
  .run-label-row input {
    width: 100%;
    box-sizing: border-box;
    background: var(--bg4);
    border: 1px solid var(--border2);
    border-radius: 4px;
    color: var(--text);
    font-family: var(--font-mono);
    font-size: 12px;
    padding: 8px 10px;
    outline: none;
    transition: border-color 0.12s;
  }
  .run-label-row input:focus { border-color: var(--violet); }
  .run-label-row input.error { border-color: var(--red); }
  .run-label-error {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--red);
    margin-top: 4px;
    display: none;
  }
  .run-label-error.visible { display: block; }
  .run-label-actions {
    display: flex;
    gap: 10px;
    justify-content: flex-end;
    margin-top: 20px;
  }
  .run-label-actions button {
    padding: 8px 18px;
    border-radius: 5px;
    font-family: var(--font-head);
    font-size: 12px;
    font-weight: 500;
    letter-spacing: 0.06em;
    cursor: pointer;
    transition: all 0.15s;
  }
  .btn-label-confirm {
    background: var(--violet);
    border: 1px solid var(--violet);
    color: #fff;
  }
  .btn-label-confirm:hover { background: var(--violet2); border-color: var(--violet2); }
  .btn-label-cancel {
    background: transparent;
    border: 1px solid var(--border2);
    color: var(--text2);
  }
  .btn-label-cancel:hover { border-color: var(--text2); color: var(--text); }

  /* Run info popup */
  .run-info-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.45);
    z-index: 600;
    align-items: center;
    justify-content: center;
  }
  .run-info-overlay.open { display: flex; }
  .run-info-box {
    background: var(--bg3);
    border: 1px solid var(--border2);
    border-radius: 8px;
    padding: 20px 24px;
    max-width: 500px;
    width: 92%;
    box-shadow: 0 12px 40px rgba(0,0,0,0.6);
  }
  .run-info-title {
    font-family: var(--font-head);
    font-size: 11px;
    font-weight: 600;
    color: var(--text2);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin-bottom: 8px;
  }
  .run-info-label {
    font-family: var(--font-mono);
    font-size: 13px;
    color: var(--text);
    margin-bottom: 12px;
    font-weight: 500;
  }
  .run-info-path {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--violet);
    background: var(--violet-dim);
    border-radius: 4px;
    padding: 7px 10px;
    word-break: break-all;
    line-height: 1.6;
    margin-bottom: 16px;
  }
  .run-info-close {
    display: flex;
    justify-content: flex-end;
  }
  .run-info-close button {
    padding: 7px 16px;
    border-radius: 5px;
    font-family: var(--font-head);
    font-size: 12px;
    font-weight: 500;
    letter-spacing: 0.06em;
    cursor: pointer;
    background: transparent;
    border: 1px solid var(--border2);
    color: var(--text2);
    transition: all 0.15s;
  }
  .run-info-close button:hover { border-color: var(--text2); color: var(--text); }

  /* Chart panel toolbar + metric chips */
  .chart-toolbar {
    display: flex;
    justify-content: flex-end;
    margin-bottom: 12px;
  }

  .metric-chips {
    display: flex;
    gap: 6px;
  }

  .metric-chip {
    padding: 4px 11px;
    border-radius: 20px;
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.04em;
    cursor: pointer;
    border: 1px solid var(--border2);
    color: var(--text3);
    background: transparent;
    transition: border-color 0.15s, color 0.15s, background 0.15s;
    user-select: none;
  }

  .metric-chip.active {
    border-color: var(--violet);
    color: var(--violet);
    background: var(--violet-dim);
  }

  .metric-chip:not(.active):hover {
    border-color: var(--border2);
    color: var(--text2);
  }

  @keyframes chipShake {
    0%, 100% { transform: translateX(0); }
    20%       { transform: translateX(-4px); }
    40%       { transform: translateX(4px); }
    60%       { transform: translateX(-3px); }
    80%       { transform: translateX(3px); }
  }

  .metric-chip.shaking { animation: chipShake 0.3s ease; }

  .metric-chip.muted {
    opacity: 0.3;
    cursor: pointer;
  }

  /* ---- STL PAGE ---- */
  .stl-container {
    padding: 12px 28px 28px;
  }

  /* Stale temp file banner */
  .stl-temp-banner {
    display: none;
    padding: 11px 16px;
    background: rgba(248,113,113,0.06);
    border: 1px solid rgba(248,113,113,0.3);
    border-radius: 6px;
    color: var(--red);
    font-size: 12px;
    font-family: var(--font-mono);
    margin-bottom: 16px;
    line-height: 1.5;
    position: relative;
  }

  .stl-temp-banner.visible { display: block; }

  .stl-temp-banner .banner-dismiss {
    position: absolute;
    top: 8px; right: 10px;
    background: none; border: none;
    color: var(--text3); cursor: pointer;
    font-size: 14px; line-height: 1;
    padding: 2px 4px;
  }

  .stl-temp-banner .banner-dismiss:hover { color: var(--red); }

  /* Auto-populate popup overlay */
  .stl-autopop-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.55);
    z-index: 500;
    align-items: center;
    justify-content: center;
  }

  .stl-autopop-overlay.visible { display: flex; }

  .stl-autopop-box {
    background: var(--bg3);
    border: 1px solid var(--border2);
    border-radius: 10px;
    padding: 24px 28px;
    max-width: 420px;
    width: 90%;
    box-shadow: 0 16px 48px rgba(0,0,0,0.6);
  }

  .stl-autopop-title {
    font-family: var(--font-head);
    font-size: 14px;
    font-weight: 600;
    color: var(--text);
    margin-bottom: 10px;
    letter-spacing: 0.04em;
  }

  .stl-autopop-body {
    font-size: 12px;
    color: var(--text2);
    line-height: 1.6;
    margin-bottom: 18px;
    font-family: var(--font-mono);
  }

  .stl-autopop-path {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--violet);
    background: var(--violet-dim);
    border-radius: 4px;
    padding: 5px 8px;
    margin-bottom: 16px;
    word-break: break-all;
  }

  .stl-autopop-actions {
    display: flex;
    gap: 10px;
    justify-content: flex-end;
  }

  .stl-autopop-actions button {
    padding: 8px 18px;
    border-radius: 5px;
    font-family: var(--font-head);
    font-size: 12px;
    font-weight: 500;
    letter-spacing: 0.06em;
    cursor: pointer;
    transition: all 0.15s;
  }

  .btn-autopop-yes {
    background: var(--violet);
    border: 1px solid var(--violet);
    color: #fff;
  }

  .btn-autopop-yes:hover { background: var(--violet2); }

  .btn-autopop-no {
    background: transparent;
    border: 1px solid var(--border2);
    color: var(--text2);
  }

  .btn-autopop-no:hover { border-color: var(--text2); color: var(--text); }

  .stl-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    align-items: start;
  }

  .csv-status-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
    margin-bottom: 0;
  }

  .csv-status-grid.single { grid-template-columns: 1fr; }

  .csv-status-item {
    padding: 12px;
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 6px;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .csv-status-item.found   { border-color: var(--green-dim); }
  .csv-status-item.missing { border-color: var(--red-dim);   }

  .csv-dot {
    width: 6px; height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .csv-status-item.found   .csv-dot { background: var(--green); }
  .csv-status-item.missing .csv-dot { background: var(--red);   }

  .csv-label {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text2);
  }

  /* Pre-flight info panel */
  .preflight-panel {
    margin-top: 14px;
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
    display: none;
  }

  .preflight-panel.visible { display: block; }

  .preflight-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    font-size: 11px;
    font-family: var(--font-mono);
  }

  .preflight-row:last-child { border-bottom: none; }

  /* Two-line variant for RAM and Decimation rows */
  .preflight-row.pf-row-tall { align-items: flex-start; padding: 9px 12px; }

  .pf-value-col {
    display: flex;
    flex-direction: column;
    gap: 4px;
    text-align: right;
    flex: 1;
    margin: 0 8px;
  }

  .pf-sub {
    font-size: 10px;
    color: var(--text3);
    line-height: 1.45;
  }

  .preflight-row .pf-label { color: var(--text3); }
  .preflight-row .pf-value { color: var(--text); }
  .preflight-row .pf-badge {
    padding: 2px 7px;
    border-radius: 3px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.05em;
  }

  .pf-pass    { background: var(--green-dim); color: var(--green); }
  .pf-warning { background: rgba(250,189,0,0.1); color: #fabd00; }
  .pf-critical{ background: var(--red-dim);  color: var(--red);   }
  .pf-overkill{ background: var(--violet-dim); color: var(--violet); }

  /* Warning / recommendation blocks */
  .stl-warning-block {
    margin-top: 12px;
    padding: 12px 14px;
    border-radius: 6px;
    border: 1px solid rgba(250,189,0,0.25);
    background: rgba(250,189,0,0.04);
    font-size: 11px;
    font-family: var(--font-mono);
    line-height: 1.6;
    display: none;
  }

  .stl-warning-block.critical {
    border-color: rgba(248,113,113,0.3);
    background: rgba(248,113,113,0.04);
  }

  .stl-warning-block.visible { display: block; }

  .warning-title {
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: #fabd00;
    margin-bottom: 6px;
  }

  .stl-warning-block.critical .warning-title { color: var(--red); }

  .warning-body { color: var(--text2); margin-bottom: 10px; }

  .warning-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 6px;
  }

  .warning-collapse-btn {
    background: none;
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 3px;
    color: var(--text3);
    cursor: pointer;
    font-size: 10px;
    padding: 2px 7px;
    line-height: 1.4;
    transition: all 0.15s;
    flex-shrink: 0;
  }

  .warning-collapse-btn:hover { color: var(--text); border-color: var(--text2); }

  .stl-warning-block.collapsed .warning-content { display: none; }
  .stl-warning-block.collapsed {
    padding: 9px 14px;
  }
  .stl-warning-block.collapsed .warning-header {
    margin-bottom: 0;
    align-items: center;
    min-height: 0;
  }
  .stl-warning-block.collapsed .warning-title { margin-bottom: 0; }

  /* Resolved (Yes applied) state */
  .stl-warning-block.resolved {
    border-color: rgba(134,239,172,0.25);
    background: rgba(134,239,172,0.04);
  }
  .stl-warning-block.resolved .warning-title { color: var(--green); }

  /* Compact toggle group inside warning blocks */
  .stl-warning-block .toggle-group { margin-top: 8px; }
  .stl-warning-block .toggle-group label {
    padding: 4px 12px;
    font-size: 11px;
  }

  .warning-toggle label {
    font-size: 11px;
    color: var(--text2);
    cursor: pointer;
  }

  /* Subgrid recommendation indicator on the field */
  .field-rec-hint {
    display: none;
    font-size: 10px;
    color: #fabd00;
    font-family: var(--font-mono);
    margin-top: 4px;
  }

  .field-rec-hint.visible { display: block; }

  /* Output path preview */
  .output-preview {
    font-size: 10px;
    color: var(--violet);
    font-family: var(--font-mono);
    margin-top: 4px;
    min-height: 14px;
  }

  /* STL status message */
  .stl-status {
    margin-top: 12px;
    padding: 12px;
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 4px;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text3);
    display: none;
  }

  .stl-status.visible  { display: block; }
  .stl-status.success  { color: var(--green);  border-color: var(--green-dim);  }
  .stl-status.running  { color: var(--violet); border-color: var(--violet-dim); }
  .stl-status.crashed  { color: var(--red);    border-color: var(--red-dim);    }

  /* STL log modal */
  .stl-log-modal-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.7);
    z-index: 600;
    align-items: flex-end;
  }

  .stl-log-modal-overlay.visible { display: flex; }

  .stl-log-modal {
    width: 100%;
    height: 55vh;
    background: #0a0a0f;
    border-top: 1px solid var(--border2);
    display: flex;
    flex-direction: column;
  }

  .stl-log-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 18px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }

  .stl-log-title {
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 600;
    color: var(--text2);
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .stl-log-close {
    background: none; border: none;
    color: var(--text3); cursor: pointer;
    font-size: 16px; line-height: 1;
    padding: 2px 6px;
  }

  .stl-log-close:hover { color: var(--text); }

  #stlLogContent {
    flex: 1;
    overflow-y: auto;
    padding: 12px 18px;
    font-family: var(--font-mono);
    font-size: 11px;
    line-height: 1.65;
  }

  /* STL log line colour coding */
  .stl-log-step     { color: var(--violet);  font-weight: 600; }
  .stl-log-pass     { color: var(--green); }
  .stl-log-warning  { color: #fabd00; }
  .stl-log-critical { color: var(--red); }
  .stl-log-chunk    { color: #7dd3fc; }
  .stl-log-info     { color: var(--text2); }
  .stl-log-done     { color: var(--green); font-weight: 600; }
  .stl-log-dim      { color: var(--text3); }

  /* ---- FIELD TOOLTIPS ---- */
  .tip {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 13px;
    height: 13px;
    border-radius: 50%;
    border: 1px solid var(--text3);
    font-size: 9px;
    font-weight: 700;
    color: var(--text3);
    cursor: default;
    margin-left: 5px;
    vertical-align: middle;
    line-height: 1;
    transition: border-color 0.15s, color 0.15s;
    font-style: normal;
    flex-shrink: 0;
  }
  .tip:hover { border-color: var(--text); color: var(--text); }
  .tip.warn { width: 11px; height: 11px; font-size: 8px; margin-left: 5px; }

  #global-tooltip {
    position: fixed;
    z-index: 9999;
    max-width: 280px;
    background: var(--bg3);
    border: 1px solid var(--border2);
    color: var(--text2);
    font-size: 11px;
    font-family: var(--font-mono);
    line-height: 1.55;
    padding: 8px 11px;
    border-radius: 6px;
    pointer-events: none;
    opacity: 0;
    transition: opacity 0.12s;
    box-shadow: 0 6px 20px rgba(0,0,0,0.5);
    white-space: normal;
    word-break: break-word;
  }
  #global-tooltip.tt-visible { opacity: 1; }

</style>
</head>
<body>
<div id="global-tooltip"></div>
<div class="app">

  <!-- TOP BAR -->
  <div class="topbar">
    <div class="topbar-left">
      <div class="topbar-title">BESO <span>OPTIMIZER</span></div>
    </div>
    <div class="nav-tabs">
      <button class="nav-tab active" onclick="switchPage('launch')">Launch</button>
      <button class="nav-tab" onclick="switchPage('history')">History</button>
      <button class="nav-tab" onclick="switchPage('stl')">STL</button>
    </div>
    <div class="topbar-right">
      <span class="topbar-badge">v1.0</span>
    </div>
  </div>

  <!-- GLOBAL BANNER (config found or running) -->
  <div class="global-banner config-found" id="configBanner" style="display:none">
    <span class="banner-icon">📁</span>
    <div class="banner-text">
      A previous configuration was found (<strong id="configTimestamp"></strong>).
      Would you like to load it?
    </div>
    <div class="banner-actions">
      <button class="btn-banner yes" onclick="loadConfig()">Load Previous</button>
      <button class="btn-banner no" onclick="dismissConfigBanner()">Start Fresh</button>
    </div>
  </div>

  <!-- PREVIOUS SESSION BANNER -->
  <div class="global-banner prev-session" id="prevSessionBanner" style="display:none">
    <span class="banner-icon">&#9888;</span>
    <div class="banner-body">
      <div class="banner-status" id="prevSessionStatus"></div>
      <div class="banner-metrics" id="prevSessionMetrics"></div>
    </div>
    <div class="banner-right">
      <button class="btn-banner no" onclick="dismissPrevSessionBanner()">Dismiss</button>
    </div>
  </div>

  <!-- LOG MODAL -->
  <div class="log-modal-overlay" id="logModal">
    <div class="log-modal">
      <div class="log-modal-header">
        <div class="log-modal-title">
          Live Optimization Log
          <span class="log-status-badge running" id="logStatusBadge">RUNNING</span>
        </div>
        <button class="log-modal-close" onclick="closeLogModal()">&#10005;</button>
      </div>
      <div class="log-body" id="logBody">
        <div id="logLines"></div>
      </div>
    </div>
  </div>

  <!-- FILE BROWSER MODAL -->
  <div class="modal-overlay" id="fileBrowserModal">
    <div class="modal">

      <div class="modal-header">
        <div class="modal-title">&#128194; Select INP File</div>
        <div class="breadcrumb" id="breadcrumb">
          <span class="breadcrumb-part last">Loading...</span>
        </div>
      </div>

      <div class="modal-toolbar">
        <button class="btn-up" id="btnUp" onclick="browserGoUp()">&#8593; Up</button>
        <input class="toolbar-path" id="toolbarPath" type="text"
               placeholder="Type or paste a path..."
               onkeydown="if(event.key==='Enter') browserNavigateTo(this.value)" />
        <button class="btn-go" onclick="browserNavigateTo(document.getElementById('toolbarPath').value)">Go</button>
      </div>

      <div class="drive-bar" id="driveBar"></div>

      <div class="modal-filelist" id="fileList">
        <div class="filelist-empty">Loading...</div>
      </div>

      <div class="modal-footer">
        <div class="modal-selected-path" id="modalSelectedPath">No file selected</div>
        <button class="btn-modal-cancel" onclick="closeBrowser()">Cancel</button>
        <button class="btn-modal-select" id="btnModalSelect"
                disabled onclick="confirmBrowserSelection()">Select</button>
      </div>

    </div>
  </div>

  <!-- ============================================================ -->
  <!-- PAGE 1: LAUNCH                                               -->
  <!-- ============================================================ -->
  <div class="page active" id="page-launch">

  <!-- ENGINE SELECTOR -->
  <div class="engine-selector">
    <div class="engine-selector-label">Engine</div>
    <div class="engine-cards">
      <input type="radio" name="engineType" id="eng-sv" value="solid_void" checked
             onchange="switchEngine('solid_void')">
      <label for="eng-sv" class="engine-card">
        <span class="engine-card-icon">◼</span>
        <div class="engine-card-text">
          <div class="engine-card-name">Solid-Void BESO</div>
          <div class="engine-card-desc">Classic topology + thermal overhang</div>
        </div>
      </label>
      <input type="radio" name="engineType" id="eng-lat" value="lattice"
             onchange="switchEngine('lattice')">
      <label for="eng-lat" class="engine-card">
        <span class="engine-card-icon">⬡</span>
        <div class="engine-card-text">
          <div class="engine-card-name">Lattice BESO</div>
          <div class="engine-card-desc">Functionally graded structures (FGL)</div>
        </div>
      </label>
    </div>
  </div>

  <!-- MAIN 4-PANEL GRID -->
  <div class="main-grid">

    <!-- ================================================ -->
    <!-- PANEL 1 - MODEL FILE                             -->
    <!-- ================================================ -->
    <div class="panel">
      <div class="panel-header">
        <div class="panel-number">1</div>
        <div class="panel-title">MODEL FILE</div>
      </div>
      <div class="panel-body">

        <div class="field">
          <label>Base INP File</label>
          <div class="browse-row">
            <input type="text" id="inpPath" placeholder="Click Browse or type full path..."
                   oninput="onInpPathTyped()" />
            <button class="btn-browse" onclick="openBrowser()">
              &#128194; Browse
            </button>
          </div>
          <div class="field-hint">Full path to your Abaqus INP file</div>
        </div>

        <div class="field">
          <label>Base Job Name</label>
          <input type="text" id="baseJob" placeholder="beam_beso_base"
                 oninput="syncJobName()" />
          <div class="field-hint">Filename without .inp extension</div>
        </div>

        <!-- Detected properties panel -->
        <div class="detected-panel hidden" id="detectedPanel">
          <div class="section-sub">Detected Properties</div>
          <div class="detected-row">
            <span class="detected-key">Element Type</span>
            <span class="detected-val" id="d-elemType">-</span>
          </div>
          <div class="detected-row">
            <span class="detected-key">Total Elements</span>
            <span class="detected-val" id="d-elemCount">-</span>
          </div>
          <div class="detected-row">
            <span class="detected-key">Parts Detected</span>
            <span class="detected-val" id="d-partsDetected">-</span>
          </div>
          <div class="detected-row" id="detected-designpart-row">
            <span class="detected-key">Design Part</span>
            <span class="detected-val" id="d-designPart">-</span>
          </div>
          <div class="detected-row">
            <span class="detected-key">NON_DESIGN_SET</span>
            <span class="detected-val" id="d-nonDesign">-</span>
          </div>
          <div class="detected-row">
            <span class="detected-key">Young's Modulus</span>
            <span class="detected-val" id="d-eModulus">-</span>
          </div>
          <div class="detected-row">
            <span class="detected-key">Material Source</span>
            <span class="detected-val" id="d-matSource">-</span>
          </div>
          <div class="detected-row">
            <span class="detected-key">Poisson Ratio</span>
            <span class="detected-val" id="d-poisson">-</span>
          </div>
          <div class="detected-row sv-only" id="detected-thermal-row">
            <span class="detected-key">Thermal Step</span>
            <span class="detected-val" id="d-thermal">-</span>
          </div>
          <div class="detected-row">
            <span class="detected-key">Instance Name</span>
            <span class="detected-val" id="d-instance">-</span>
          </div>
        </div>

      </div>
    </div>

    <!-- ================================================ -->
    <!-- PANEL 2 - OPTIMIZATION PARAMETERS               -->
    <!-- ================================================ -->
    <div class="panel">
      <div class="panel-header">
        <div class="panel-number">2</div>
        <div class="panel-title">OPTIMIZATION</div>
      </div>
      <div class="panel-body">

        <!-- LATTICE ONLY: Lattice Type (primary selector) -->
        <div class="field lat-only hidden-field" id="lat-type-field">
          <label>Lattice Type <span class="tip" data-tip="TPMS topology. Sets the Gibson-Ashby stiffness (C1,n1) and yield (C2,n2) relationships the optimiser uses to size each element. Selecting a type fills the manual-override fields under Advanced." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
          <div class="lattice-radio-list">
            <label class="lattice-radio">
              <input type="radio" name="latticeType" value="gyroid_ligament" onchange="onLatticeTypeChange()">
              <span class="lattice-radio-dot"></span>
              <span class="lattice-radio-text">Gyroid ligament<span class="lattice-radio-coef">C1 0.9515 &middot; n1 2.174 &middot; C2 0.6182 &middot; n2 1.746</span></span>
            </label>
            <label class="lattice-radio">
              <input type="radio" name="latticeType" value="gyroid_sheet" onchange="onLatticeTypeChange()">
              <span class="lattice-radio-dot"></span>
              <span class="lattice-radio-text"><span class="lattice-radio-name">Gyroid sheet<span class="tip warn" data-tip="Gyroid sheet was published by Pais 2023 as a 3rd-order polynomial, not a power law. The C1/n1/C2/n2 shown are a single power-law fit over the operating density range (about 2.5 percent RMS error). See the thesis notes for the fit and its justification." onmouseenter="showTip(this)" onmouseleave="hideTip()">!</span></span><span class="lattice-radio-coef">C1 0.5909 &middot; n1 1.3454 &middot; C2 0.7298 &middot; n2 1.2066</span></span>
            </label>
            <label class="lattice-radio">
              <input type="radio" name="latticeType" value="primitive" onchange="onLatticeTypeChange()">
              <span class="lattice-radio-dot"></span>
              <span class="lattice-radio-text">Schwarz Primitive sheet<span class="lattice-radio-coef">C1 0.61 &middot; n1 1.57 &middot; C2 0.794 &middot; n2 1.36</span></span>
            </label>
            <label class="lattice-radio">
              <input type="radio" name="latticeType" value="diamond" onchange="onLatticeTypeChange()">
              <span class="lattice-radio-dot"></span>
              <span class="lattice-radio-text">Diamond ligament<span class="lattice-radio-coef">C1 0.6438 &middot; n1 2.026 &middot; C2 0.6802 &middot; n2 1.614</span></span>
            </label>
            <label class="lattice-radio">
              <input type="radio" name="latticeType" value="iwp" checked onchange="onLatticeTypeChange()">
              <span class="lattice-radio-dot"></span>
              <span class="lattice-radio-text">I-WP sheet<span class="lattice-radio-coef">C1 0.699 &middot; n1 1.217 &middot; C2 0.738 &middot; n2 1.151</span></span>
            </label>
            <label class="lattice-radio">
              <input type="radio" name="latticeType" value="neovius" onchange="onLatticeTypeChange()">
              <span class="lattice-radio-dot"></span>
              <span class="lattice-radio-text"><span class="lattice-radio-name">Neovius sheet<span class="tip warn" data-tip="Neovius yield has no material-independent homogenization source. C2 is from an experimental 316L study (Ravichander 2022) and n2 = 1.5 is imposed, not fitted. Stiffness C1/n1 is homogenization-based (Abueidda 2016). See the thesis notes." onmouseenter="showTip(this)" onmouseleave="hideTip()">!</span></span><span class="lattice-radio-coef">C1 0.705 &middot; n1 1.236 &middot; C2 0.7210 &middot; n2 1.5</span></span>
            </label>
          </div>
        </div>
        <!-- SHARED: Spatial Filter -->
        <div class="field">
          <label>Spatial Filter <span class="tip" data-tip="Smooths sensitivity values across neighbouring elements before ranking. Linear: sharp, element-accurate gradients. Gaussian Bell: wider bell-curve influence - generally produces cleaner lattice grades and smoother topologies." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
          <div class="toggle-group">
            <input type="radio" name="filterType" id="filterLinear" value="linear" checked>
            <label for="filterLinear">Linear</label>
            <input type="radio" name="filterType" id="filterGaussian" value="gaussian">
            <label for="filterGaussian">Gaussian Bell</label>
          </div>
        </div>

        <!-- SHARED: Load Case Weighting -->
        <div class="field">
          <label>Load Case Weighting <span class="tip" data-tip="How multiple load steps are combined into one sensitivity map. Equal: each step contributes the same weight. Custom: enter per-step weights as comma-separated values, normalised automatically to sum to 1." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
          <div class="toggle-group">
            <input type="radio" name="weightType" id="weightEqual" value="equal" checked
                   onchange="toggleCustomWeights()">
            <label for="weightEqual">Equal</label>
            <input type="radio" name="weightType" id="weightCustom" value="custom"
                   onchange="toggleCustomWeights()">
            <label for="weightCustom">Custom</label>
          </div>
          <div class="custom-weights-field" id="customWeightsField">
            <input type="text" id="customWeights" placeholder="e.g. 0.7, 0.3" />
            
          </div>
        </div>

        <!-- SOLID-VOID ONLY -->
        <div class="sv-only" id="sv-opt-fields">

          <div class="field">
            <label>Evolution Ratio <span class="tip" data-tip="Fraction of elements that change state each iteration. Fixed 2%: constant evolution rate throughout. Dynamic 2% to 0.5%: rate tapers as the design converges to reduce oscillation near the optimum." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
            <div class="toggle-group">
              <input type="radio" name="erType" id="erFixed" value="fixed" checked>
              <label for="erFixed">Fixed 2%</label>
              <input type="radio" name="erType" id="erDynamic" value="dynamic">
              <label for="erDynamic">Dynamic 2%→0.5%</label>
            </div>
          </div>

          <div class="field">
            <label>Overhang Mitigation <span class="tip" data-tip="When enabled, a coupled thermal analysis is injected into every Abaqus iteration. Overhanging elements receive a conductance penalty that discourages unsupported geometry in the chosen print direction. Leave Off for pure mechanical topology optimisation." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
            <div class="toggle-group">
              <input type="radio" name="thermalToggle" id="thermalOff" value="off" checked
                     onchange="toggleThermalParams()">
              <label for="thermalOff">Off</label>
              <input type="radio" name="thermalToggle" id="thermalOn" value="on"
                     onchange="toggleThermalParams()">
              <label for="thermalOn">Via Thermal Analysis</label>
            </div>
          </div>

          <div id="thermalParamsGroup" class="hidden-field">

          <div class="field">
            <label>Thermal Weight <span class="tip" data-tip="Objective blending between mechanical stiffness and thermal conductance. 0.0 = pure structural optimisation. 1.0 = pure thermal optimisation. Values between 0 and 1 optimise both objectives simultaneously." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
            <div class="slider-row">
              <input type="range" id="thermalWeight" min="0" max="1" step="0.05" value="0.2"
                     oninput="document.getElementById('thermalWeightVal').textContent=parseFloat(this.value).toFixed(2)">
              <span class="slider-val" id="thermalWeightVal">0.20</span>
            </div>
          </div>

          <div class="field">
            <label>Print Direction <span class="tip" data-tip="The vertical build axis used for overhang detection. Elements that cantilever against gravity in this direction receive a stiffness penalty, discouraging geometry that cannot be 3D printed without support." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
            <div class="dir-grid">
              <input type="radio" name="printDir" id="dir-px" value="+X">
              <label for="dir-px">+X</label>
              <input type="radio" name="printDir" id="dir-py" value="+Y" checked>
              <label for="dir-py">+Y</label>
              <input type="radio" name="printDir" id="dir-pz" value="+Z">
              <label for="dir-pz">+Z</label>
              <input type="radio" name="printDir" id="dir-nx" value="-X">
              <label for="dir-nx">−X</label>
              <input type="radio" name="printDir" id="dir-ny" value="-Y">
              <label for="dir-ny">−Y</label>
              <input type="radio" name="printDir" id="dir-nz" value="-Z">
              <label for="dir-nz">−Z</label>
            </div>
          </div>

          <div class="field">
            <label>Transverse Conductivity K22 <span class="tip" data-tip="Thermal conductivity assigned perpendicular to the print direction, in W/m.K. Controls heat flow through lattice and void elements in the transverse plane. Used only when the thermal step is active." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
            <input type="number" id="k22" value="5.0" min="0.001" step="0.5" />
            
          </div>

          </div><!-- end thermalParamsGroup -->

        </div><!-- end sv-only -->

        <!-- LATTICE ONLY -->
        <div class="lat-only hidden-field" id="lat-opt-fields">

          <div class="field">
            <label>Sizing Mode <span class="tip" data-tip="Max Specific Stiffness: the classic load-normalised objective; the load magnitude and yield cancel out. Sized to Load: the part is sized to carry the actual load in the INP at the actual material yield, so the INP must apply the real design load. Discrete 3-Bin requires this mode." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
            <div class="toggle-group">
              <input type="radio" name="latticeSizing" id="latSizeScaled" value="scaled" checked onchange="enforceLatticeModeConstraints()">
              <label for="latSizeScaled">Specific Stiffness</label>
              <input type="radio" name="latticeSizing" id="latSizeAbs" value="absolute" onchange="enforceLatticeModeConstraints()">
              <label for="latSizeAbs">Sized to Load</label>
            </div>
          </div>

          <div class="field">
            <label>Smart Skin <span class="tip" data-tip="Flood-fill morphological operation that detects external void surfaces and hardens the adjacent lattice elements into a solid protective shell. Disabled produces a naked lattice core - structurally stiffer but all internal geometry is exposed at the surface." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
            <div class="toggle-group">
              <input type="radio" name="skinOpt" id="skinOff" value="off" checked onchange="toggleSkinThicknessField()">
              <label for="skinOff">Disabled</label>
              <input type="radio" name="skinOpt" id="skinOn" value="on" onchange="toggleSkinThicknessField()">
              <label for="skinOn">Enabled</label>
            </div>
            
          </div>

          <div class="field">
            <label>Property Model <span class="tip" data-tip="Safe: Gibson-Ashby below the density cap, solid bulk at and above it (recommended). Experimental: an endpoint-corrected blend that trends continuously to bulk with no cap - an optimistic estimate of lattice potential, not a validated law." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
            <div class="toggle-group">
              <input type="radio" name="latticeMethod" id="latMethodSafe" value="safe" checked>
              <label for="latMethodSafe">Safe</label>
              <input type="radio" name="latticeMethod" id="latMethodExp" value="experimental">
              <label for="latMethodExp">Experimental</label>
            </div>
          </div>

        </div><!-- end lat-only -->

      </div>
    </div>

    <!-- ================================================ -->
    <!-- PANEL 3 - ADVANCED PARAMETERS                   -->
    <!-- ================================================ -->
    <div class="panel">
      <div class="panel-header">
        <div class="panel-number">3</div>
        <div class="panel-title">ADVANCED</div>
      </div>
      <div class="panel-body">

        <!-- SOLID-VOID ONLY -->
        <div class="sv-only" id="sv-adv-fields">

          <div class="section-sub">Volume & Geometry</div>

          <div class="field">
            <label>Target Volume Fraction <span class="tip" data-tip="The fraction of the design domain that remains solid at the end of optimisation. 0.48 means 48% of elements stay solid and 52% become void. Lower values produce lighter, more aggressive topologies." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
            <input type="number" id="targetVf" value="0.48" min="0.1" max="0.95" step="0.01" />
            
          </div>

          <div class="field">
            <label>Filter Radius - Mechanical (mm) <span class="tip" data-tip="Sensitivity smoothing radius for mechanical stiffness, in mm. Should be roughly 1.5 to 2.5x the average element edge length. Larger values produce smoother but less detailed topologies." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
            <input type="number" id="filterRadius" value="2.0" min="0.5" step="0.5" />
            
          </div>

          <div class="field">
            <label>Micro Radius - Overhang (mm) <span class="tip" data-tip="Smearing radius for the printability overhang penalty, in mm. Controls how far the penalty spreads from detected unsupported surfaces into the surrounding elements." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
            <input type="number" id="microRadius" value="2.0" min="0.5" step="0.5" />
            
          </div>

          <div class="divider"></div>
          <div class="section-sub">Load & Solver</div>

          <div class="field">
            <label>Total Applied Force (N) <span class="tip" data-tip="Total resultant load applied to the model in Newtons. Used to normalise compliance into specific stiffness (stiffness per unit volume fraction) for the optimisation history chart." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
            <input type="number" id="totalForce" value="1000.0" min="1" step="100" />
            
          </div>

        </div><!-- end sv-only -->

        <!-- LATTICE ONLY -->
        <div class="lat-only hidden-field" id="lat-adv-fields">

          <div class="section-sub">Lattice Material</div>

          <div class="field">
            <label>Yield Stress (MPa) <span class="tip" data-tip="Yield stress of the solid base material in MPa. Auto-detected from Young's modulus via heuristic (Al ~300, Steel ~400, Ti ~900). Override here if your material differs or the auto-detection is wrong." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
            <input type="number" id="latYieldStress" value="900" min="50" max="5000" step="10" />
          </div>

          <div class="field">
            <label>Lattice Density Cap <span class="tip" data-tip="Safe mode only. Relative density at and above which an element is treated as solid bulk material instead of lattice; Gibson-Ashby is applied only below this cap. Ignored in Experimental mode." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
            <input type="number" id="latticeCap" value="0.50" min="0.2" max="0.95" step="0.05" />
          </div>

          <div class="field hidden-field" id="safetyFactorField">
            <label>Safety Factor <span class="tip" data-tip="Sized to Load only. Multiplies the sizing target so each element is built to carry SF times its actual stress. TRUE_UTILIZATION stays measured against the true material yield, so at convergence the plot reads about 1/SF - that is your real margin to yielding (SF 1.5 puts the field near 0.67)." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
            <input type="number" id="safetyFactor" value="1.0" min="1.0" max="5.0" step="0.05" />
          </div>

          <div class="collapsible-header" onclick="toggleCollapsible(this)">
            Manual Override of Gibson-Ashby Coefficients
            <span class="collapsible-arrow">&#9658;</span>
          </div>
          <div class="collapsible-body">
            <div class="field-hint">Auto-set from the chosen Lattice Type. Editing these decouples the run from the standard lattice values and is recorded in the output manifest.</div>
            <div class="field-row-2">
              <div class="field">
                <label>Stiffness C1 <span class="tip" data-tip="Stiffness coefficient C1 in E*/Es = C1 * rho^n1. Comes from the chosen lattice type." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
                <input type="number" id="stiffnessCoeff" value="0.699" min="0.05" max="2.0" step="0.0001" />
              </div>
              <div class="field">
                <label>Stiffness n1 <span class="tip" data-tip="Stiffness exponent n1 in E*/Es = C1 * rho^n1." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
                <input type="number" id="gibsonAshbyExp" value="1.217" min="1.0" max="4.0" step="0.0001" />
              </div>
            </div>
            <div class="field-row-2">
              <div class="field">
                <label>Yield C2 <span class="tip" data-tip="Yield coefficient C2 in sy*/sys = C2 * rho^n2. Comes from the chosen lattice type." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
                <input type="number" id="yieldCoeff" value="0.738" min="0.05" max="2.0" step="0.0001" />
              </div>
              <div class="field">
                <label>Yield n2 <span class="tip" data-tip="Yield exponent n2 in sy*/sys = C2 * rho^n2." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
                <input type="number" id="yieldExponent" value="1.151" min="0.5" max="4.0" step="0.0001" />
              </div>
            </div>
          </div>

          <div class="divider"></div>
          <div class="section-sub">Evolution and Filter</div>

          <div class="field-row-2">
            <div class="field">
              <label>Evolution Quota (%) <span class="tip" data-tip="Percentage of design elements that change density each iteration. Analogous to the Evolution Ratio in the solid-void engine. Higher values converge faster but risk oscillation. 4% is a stable starting point." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
              <input type="number" id="evolutionQuota" value="4" min="0.5" max="20" step="0.5" />
              
            </div>
            <div class="field">
              <label>Void Threshold (%) <span class="tip" data-tip="Relative density at or below which an element is eliminated as void. Raising this (e.g. 20-30%) produces more aggressive void removal and a more discrete final result. Lowering it allows very sparse lattice regions to survive." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
              <input type="number" id="voidThreshold" value="10" min="1" max="50" step="1" />
              
            </div>
          </div>

          <div class="field">
            <label>Move Limit <span class="tip" data-tip="Maximum density change per iteration as a fraction of the full range. 1.0 = unrestricted, elements jump directly to their target density. 0.2 = elements move at most 20% of full range toward their target each step, slowing convergence but reducing oscillation." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
            <div class="slider-row">
              <input type="range" id="moveLimit" min="0.05" max="1.0" step="0.05" value="1.0"
                     oninput="document.getElementById('moveLimitVal').textContent=parseFloat(this.value).toFixed(2)">
              <span class="slider-val" id="moveLimitVal">1.00</span>
            </div>
          </div>

          <div class="field hidden-field" id="skinThicknessField">
            <label>Skin Thickness <span class="tip" data-tip="Smart Skin only. Number of element layers hardened into the solid shell on every exposed surface (the outer boundary and any air-void walls). 1 is a single-element skin; raise it for a thicker, stiffer shell at the cost of more solid material. Internal trapped-void walls are always left as lattice." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
            <input type="number" id="skinThickness" value="1" min="1" max="10" step="1" />
          </div>

          <div class="field">
            <label>Filter Radius Multiplier <span class="tip" data-tip="The filter radius is auto-detected as: average element size x this multiplier. 3x is the standard starting point. Increase for smoother density gradients between lattice grades; decrease for sharper transitions." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
            <input type="number" id="filterMultiplier" value="3" min="1" max="8" step="1" />
            
          </div>

          <div class="divider"></div>

          <div class="collapsible-header" onclick="toggleCollapsible(this)">
            Lattice Philosophy
            <span class="collapsible-arrow">&#9658;</span>
          </div>
          <div class="collapsible-body">
            <div class="field">
              <label>Density Philosophy <span class="tip" data-tip="Discrete 3-Bin: elements are assigned to one of three states - Solid (100%), a fixed 20% density Core lattice, or Void. Continuous FGL: elements take any density from 15% to 100%, forming a functionally graded structure with smooth density transitions. Discrete runs only in Sized to Load mode." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
              <div class="toggle-group">
                <input type="radio" name="latPhil" id="latDiscrete" value="discrete" onchange="enforceLatticeModeConstraints()">
                <label for="latDiscrete">Discrete 3-Bin</label>
                <input type="radio" name="latPhil" id="latContinuous" value="continuous" checked onchange="enforceLatticeModeConstraints()">
                <label for="latContinuous">Continuous FGL</label>
              </div>
            </div>
          </div>

          <div class="divider"></div>

          <!-- COLLAPSIBLE: Early Stopping and Convergence -->
          <div class="collapsible-header" onclick="toggleCollapsible(this)">
            Early Stopping and Convergence
            <span class="collapsible-arrow">&#9658;</span>
          </div>
          <div class="collapsible-body">
            <div class="field">
              <label>Warm-up Iterations <span class="tip" data-tip="Minimum number of iterations before any early-stopping trigger is checked. Prevents the run from terminating during the initial chaotic cutting phase before the design has settled. Must be at least 6 (required for the 5-iteration lookback window)." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
              <input type="number" id="earlyStopWarmup" value="15" min="6" max="100" step="1" />
              
            </div>
            <div class="field">
              <label>Patience (iterations) <span class="tip" data-tip="Maximum iterations allowed without achieving a new best specific stiffness score. Once this count is exceeded the run terminates. This is Trigger 3 of the early-stopping system (stagnation)." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
              <input type="number" id="earlyStopPatience" value="10" min="1" max="100" step="1" />
              
            </div>
            <div class="field">
              <label>Flatline Threshold (%) <span class="tip" data-tip="If the relative change in specific stiffness over the last 5 iterations falls below this value, the run is considered converged and terminates. Expressed as a percentage - 0.1 means less than 0.1% change. This is Trigger 1 of the early-stopping system." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
              <input type="number" id="earlyStopFlatline" value="0.1" min="0.01" max="5.0" step="0.01" />
              
            </div>
            <div class="field">
              <label>Max Iterations <span class="tip" data-tip="Hard ceiling on total iteration count. The early-stopping system (flatline, disintegration, stagnation triggers) normally terminates the run well before this limit is reached. Use this as a safety cap for very long runs." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
              <input type="number" id="maxIterations" value="200" min="10" max="1000" step="10" />
              
            </div>
          </div>

        </div><!-- end lat-only -->

        <!-- SHARED: Solver (collapsible) -->
        <div class="divider"></div>

        <div class="collapsible-header" onclick="toggleCollapsible(this)">
          Solver
          <span class="collapsible-arrow">&#9658;</span>
        </div>
        <div class="collapsible-body">
          <div class="field">
            <label>CPU Cores <span class="tip" data-tip="Number of CPU cores allocated to the Abaqus FEA solver. Set to your physical core count minus 1 or 2 to keep the operating system responsive during long optimisation runs." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
            <input type="number" id="cpus" value="6" min="1" max="64" step="1" />
          </div>
          <div class="field">
            <label>Memory (%) <span class="tip" data-tip="Percentage of total system RAM that Abaqus is allowed to use for the solver. 90% is safe for a dedicated workstation. Reduce if other applications need to run concurrently during the optimisation." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
            <input type="number" id="memPercent" value="90" min="10" max="99" step="5" />
            
          </div>
        </div>

      </div>
    </div>

    <!-- ================================================ -->
    <!-- PANEL 4 - VALIDATION + LAUNCH                   -->
    <!-- ================================================ -->
    <div class="panel">
      <div class="panel-header">
        <div class="panel-number">4</div>
        <div class="panel-title">PRE-FLIGHT</div>
      </div>
      <div class="panel-body">

        <div class="checklist" id="checklist">

          <div class="check-item idle" id="chk-inp">
            <div class="check-icon"></div>
            <div class="check-content">
              <div class="check-label">INP File</div>
              <div class="check-detail">No file selected</div>
            </div>
          </div>

          <div class="check-item idle" id="chk-nondesign">
            <div class="check-icon"></div>
            <div class="check-content">
              <div class="check-label">NON_DESIGN_SET</div>
              <div class="check-detail">Awaiting file inspection</div>
            </div>
          </div>

          <div class="check-item idle" id="chk-material">
            <div class="check-icon"></div>
            <div class="check-content">
              <div class="check-label">Material Properties</div>
              <div class="check-detail">Awaiting file inspection</div>
            </div>
          </div>

          <div class="check-item idle sv-only" id="chk-elemtype">
            <div class="check-icon"></div>
            <div class="check-content">
              <div class="check-label">Element Type</div>
              <div class="check-detail">Awaiting file inspection</div>
            </div>
          </div>

          <div class="check-item idle" id="chk-abaqus">
            <div class="check-icon"></div>
            <div class="check-content">
              <div class="check-label">Abaqus on PATH</div>
              <div class="check-detail" id="chk-abaqus-detail">Checking...</div>
            </div>
          </div>

          <div class="check-item idle" id="chk-weights">
            <div class="check-icon"></div>
            <div class="check-content">
              <div class="check-label">Load Case Weights</div>
              <div class="check-detail">Equal weighting selected</div>
            </div>
          </div>

          <div class="check-item idle sv-only" id="chk-vf">
            <div class="check-icon"></div>
            <div class="check-content">
              <div class="check-label">Volume Fraction</div>
              <div class="check-detail">0.48 (valid range 0.10–0.95)</div>
            </div>
          </div>

        </div>

        <!-- Launch -->
        <div class="launch-section">
          <div class="launch-btn-row">
            <button class="btn-launch" id="launchBtn" disabled onclick="launchOptimizer()">
              LAUNCH OPTIMIZATION
            </button>
            <button class="btn-cancel-run" id="launchCancelBtn" onclick="requestCancelRun()">
              &#10005; CANCEL
            </button>
          </div>
          <div class="run-info" id="runInfo" style="display:none">
            <div class="run-info-metrics" id="runInfoMetrics">Waiting for first iteration...</div>
            <div class="run-info-row">
              <span class="run-info-elapsed" id="runInfoElapsed"></span>
              <button class="btn-extend" onclick="openLogModal()">&#9654; Log</button>
            </div>
          </div>
          <button class="btn-relaunch" id="relaunchBtn" onclick="resetForRelaunch()">
            &#8635; Configure New Run
          </button>
        </div>

      </div>
    </div>

  </div><!-- end main-grid -->
  </div><!-- end page-launch -->

  <!-- ============================================================ -->
  <!-- PAGE 2: HISTORY                                              -->
  <!-- ============================================================ -->
  <div class="page" id="page-history">
    <div class="history-container">

      <div class="history-top">
        <div class="field">
          <label>Add Run Folder</label>
          <div class="browse-row">
            <input type="text" id="historyFolderPath"
                   placeholder="Select folder containing BESO history CSV files..."
                   readonly />
            <button class="btn-browse" onclick="openFolderBrowser('history')">&#128194; Browse</button>
          </div>
          <div class="field-hint">Supports BESO_History_*.csv (solid-void) and Lattice_Optimization_Data.csv (lattice)</div>
        </div>
        <button class="btn-add-run" onclick="addHistoryRun()">+ Add Run</button>
      </div>

      <div id="historyContent">
        <div class="history-empty">Browse to a results folder and click Add Run to begin.</div>
      </div>

    </div>
  </div>

  <!-- ============================================================ -->
  <!-- PAGE 3: STL GENERATOR                                        -->
  <!-- ============================================================ -->
  <div class="page" id="page-stl">

    <!-- Auto-populate popup (fixed overlay, DOM position irrelevant) -->
    <div class="stl-autopop-overlay" id="stlAutopopOverlay">
      <div class="stl-autopop-box">
        <div class="stl-autopop-title">SUCCESSFUL RUN DETECTED</div>
        <div class="stl-autopop-body" id="stlAutopopBody"></div>
        <div class="stl-autopop-path" id="stlAutopopPath"></div>
        <div class="stl-autopop-actions">
          <button class="btn-autopop-no"  onclick="dismissAutopop()">No thanks</button>
          <button class="btn-autopop-yes" onclick="acceptAutopop()">Yes, populate</button>
        </div>
      </div>
    </div>

    <!-- ENGINE SELECTOR - exact same classes as the Launch page -->
    <div class="engine-selector">
      <div class="engine-selector-label">Engine</div>
      <div class="engine-cards">
        <input type="radio" name="stlEngine" id="stlEngSV" value="solid_void" checked onchange="switchStlEngine('solid_void')">
        <label for="stlEngSV" class="engine-card">
          <span class="engine-card-icon">&#9724;</span>
          <div class="engine-card-text">
            <div class="engine-card-name">Solid-Void BESO</div>
            <div class="engine-card-desc">Classic topology output</div>
          </div>
        </label>
        <input type="radio" name="stlEngine" id="stlEngLat" value="lattice" onchange="switchStlEngine('lattice')">
        <label for="stlEngLat" class="engine-card">
          <span class="engine-card-icon">&#11041;</span>
          <div class="engine-card-text">
            <div class="engine-card-name">Lattice BESO</div>
            <div class="engine-card-desc">Functionally graded structures (FGL)</div>
          </div>
        </label>
      </div>
    </div>

    <div class="stl-container">

      <!-- Stale temp file banner -->
      <div class="stl-temp-banner" id="stlTempBanner">
        <button class="banner-dismiss" onclick="dismissTempBanner()">&#215;</button>
        <span id="stlTempBannerText"></span>
      </div>

      <!-- Main two-column grid -->
      <div class="stl-grid">

        <!-- -- LEFT COLUMN: Data Source -- -->
        <div>
          <div class="panel">
            <div class="panel-header">
              <div class="panel-number">1</div>
              <div class="panel-title">DATA SOURCE</div>
            </div>
            <div class="panel-body">
              <div class="field">
                <label>Data Folder</label>
                <div class="browse-row">
                  <input type="text" id="stlFolderPath"
                        placeholder="Select your Data_Files folder..."
                        oninput="onStlPathTyped()" />
                  <button class="btn-browse" onclick="openFolderBrowser('stl')">&#128194; Browse</button>
                </div>
                <div class="field-hint">The Data_Files folder from your optimizer run</div>
              </div>

              <!-- Solid-void file indicators -->
              <div id="stlSVFiles">
                <div class="section-sub" style="margin-top:14px">Required Files</div>
                <div class="csv-status-grid" id="csvStatusGrid">
                  <div class="csv-status-item" id="csv-nodes">
                    <div class="csv-dot"></div>
                    <div class="csv-label">best_nodes.csv</div>
                  </div>
                  <div class="csv-status-item" id="csv-elements">
                    <div class="csv-dot"></div>
                    <div class="csv-label">best_elements.csv</div>
                  </div>
                  <div class="csv-status-item" id="csv-solid">
                    <div class="csv-dot"></div>
                    <div class="csv-label">best_solid_elements.csv</div>
                  </div>
                </div>
              </div>

              <!-- Lattice file indicator + pre-flight panel -->
              <div id="stlLatFiles" style="display:none">
                <div class="section-sub" style="margin-top:14px">Required File</div>
                <div class="csv-status-grid single">
                  <div class="csv-status-item" id="csv-density-map">
                    <div class="csv-dot"></div>
                    <div class="csv-label">Optimized_Density_Map.csv</div>
                  </div>
                </div>

                <!-- Pre-flight info panel -->
                <div class="preflight-panel" id="preflightPanel">
                  <div class="preflight-row">
                    <span class="pf-label">Elements</span>
                    <span class="pf-value" id="pfElemVal">-</span>
                  </div>
                  <div class="preflight-row">
                    <span class="pf-label">Grid</span>
                    <span class="pf-value" id="pfGridVal">-</span>
                  </div>
                  <div class="preflight-row">
                    <span class="pf-label">Min Feature Size</span>
                    <span class="pf-value" id="pfFeatureVal">-</span>
                    <span class="pf-badge" id="pfFeatureBadge"></span>
                  </div>
                  <div class="preflight-row">
                    <span class="pf-label">Subgrid</span>
                    <span class="pf-value" id="pfSubgridVal">-</span>
                    <span class="pf-badge" id="pfSubgridBadge"></span>
                  </div>
                  <div class="preflight-row pf-row-tall">
                    <span class="pf-label" style="padding-top:1px">RAM</span>
                    <div class="pf-value-col">
                      <span class="pf-value" id="pfRamVal">-</span>
                      <span class="pf-sub"   id="pfRamSub"></span>
                    </div>
                    <span class="pf-badge" id="pfRamBadge" style="margin-top:1px"></span>
                  </div>
                  <div class="preflight-row pf-row-tall">
                    <span class="pf-label" style="padding-top:1px">Decimation</span>
                    <div class="pf-value-col">
                      <span class="pf-value" id="pfDecimVal">-</span>
                      <span class="pf-sub"   id="pfDecimSub"></span>
                    </div>
                  </div>
                </div>
              </div>

            </div>
          </div>
        </div>

        <!-- -- RIGHT COLUMN: Options + Generate -- -->
        <div>
          <div class="panel">
            <div class="panel-header">
              <div class="panel-number">2</div>
              <div class="panel-title">GENERATION OPTIONS</div>
            </div>
            <div class="panel-body" style="padding-top:8px">
              <div id="stlSVOptions">
                <div class="field">
                  <label>Smoothing</label>
                  <div class="toggle-group">
                    <input type="radio" name="stlSmooth" id="stlSmoothOff" value="off" checked onchange="toggleSmoothOptions()">
                    <label for="stlSmoothOff">None</label>
                    <input type="radio" name="stlSmooth" id="stlSmoothOn" value="on" onchange="toggleSmoothOptions()">
                    <label for="stlSmoothOn">HC Laplacian</label>
                  </div>
                </div>
                <div id="smoothIterField" style="display:none">
                  <div class="field">
                    <label>Smooth Iterations</label>
                    <input type="number" id="stlSmoothIter" value="10" min="1" max="100" step="1" />
                  </div>
                  <div class="field">
                    <label>Lambda (strength)</label>
                    <input type="number" id="stlSmoothLambda" value="0.5" min="0.1" max="1.0" step="0.05" />
                  </div>
                </div>
              </div>

              <!-- -- LATTICE OPTIONS -- -->
              <div id="stlLatOptions" style="display:none">

                <!-- Warning 1: Density floor -->
                <div class="stl-warning-block" id="warnDensityFloor">
                  <div class="warning-header">
                    <div class="warning-title">SLM FEATURE SIZE WARNING</div>
                    <button class="warning-collapse-btn" onclick="toggleWarning('warnDensityFloor', this)" title="Minimise">&#10003;</button>
                  </div>
                  <div class="warning-content">
                    <div class="warning-body" id="warnDensityFloorBody"></div>
                    <div class="warning-toggle">
                      <div class="toggle-group">
                        <input type="radio" name="stlDensityFloor" id="stlFloorOff" value="off" checked onchange="onFloorToggle()">
                        <label for="stlFloorOff">No</label>
                        <input type="radio" name="stlDensityFloor" id="stlFloorOn" value="on" onchange="onFloorToggle()">
                        <label for="stlFloorOn">Yes - apply density floor</label>
                      </div>
                    </div>
                  </div>
                </div>

                <!-- Warning 2: Subgrid (dynamic) -->
                <div class="stl-warning-block" id="warnSubgrid">
                  <div class="warning-header">
                    <div class="warning-title">MESH RESOLUTION WARNING</div>
                    <button class="warning-collapse-btn" onclick="toggleWarning('warnSubgrid', this)" title="Minimise">&#10003;</button>
                  </div>
                  <div class="warning-content">
                    <div class="warning-body" id="warnSubgridBody"></div>
                    <div class="warning-toggle">
                      <div class="toggle-group">
                        <input type="radio" name="stlSubgridFix" id="stlSubgridFixOff" value="off" checked onchange="onSubgridFixToggle()">
                        <label for="stlSubgridFixOff">No</label>
                        <input type="radio" name="stlSubgridFix" id="stlSubgridFixOn" value="on" onchange="onSubgridFixToggle()">
                        <label for="stlSubgridFixOn">Yes - update subgrid</label>
                      </div>
                    </div>
                  </div>
                </div>

                <!-- Lattice type + period + subgrid stacked left, larger preview right -->
                <div style="margin-top:14px; display:flex; gap:14px; align-items:flex-start">

                  <!-- Left: controls stacked -->
                  <div style="flex:1; display:flex; flex-direction:column; gap:12px; min-width:0">

                    <div class="field" style="margin-bottom:0">
                      <label>Lattice Type <span class="tip" data-tip="TPMS lattice topology. Sheet types are stretching-dominated, stronger for a given density. Ligament types are bending-dominated, more flexible." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
                      <select id="stlLatticeType" onchange="onStlLatticeChange()">
                        <option value="iwp">IWP Sheet</option>
                        <option value="gyroid_sheet">Gyroid Sheet</option>
                        <option value="primitive">Schwarz Primitive Sheet</option>
                        <option value="neovius">Neovius Sheet</option>
                        <option value="gyroid_ligament">Gyroid Ligament</option>
                        <option value="diamond">Diamond Ligament</option>
                      </select>
                    </div>

                    <div class="field" style="margin-bottom:0">
                      <label>Period (mm) <span class="tip" data-tip="Lattice cell size in mm. Smaller = more cells. Preview updates live." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
                      <input type="number" id="stlPeriod" value="2.0" min="0.5" max="20.0" step="0.5"
                             oninput="onPeriodInput()" />
                    </div>

                    <div class="field" style="margin-bottom:0">
                      <label>Subgrid <span class="tip" data-tip="Sub-divisions per voxel for mesh resolution. Higher = finer geometry capture but more RAM and time." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
                      <div class="subgrid-icon-wrap">
                        <input type="number" id="stlSubgrid" value="6" min="2" max="60" step="1"
                               oninput="onSubgridInput(); updateSubgridIcon()" />
                        <span id="subgridStatusIcon" class="subgrid-status-icon" data-tip=""></span>
                      </div>
                    </div>

                  </div>

                  <!-- Right: lattice preview (larger) -->
                  <div id="latticePreviewWrap"
                       style="flex-shrink:0; width:180px; border-radius:8px; overflow:hidden;
                              background:#06060c; border:1px solid var(--border2);
                              position:relative; cursor:grab; align-self:stretch;
                              display:flex; flex-direction:column">
                    <canvas id="latticePreviewCanvas" width="180" height="165" style="display:block; flex:1"></canvas>
                    <div style="padding:3px 6px; border-top:1px solid var(--border); text-align:center;
                                font-size:9px; font-family:var(--font-mono); color:var(--text3);
                                letter-spacing:0.06em; pointer-events:none; flex-shrink:0"
                         id="latticePreviewLabel">IWP</div>
                  </div>

                </div>

                <!-- Advanced collapsible -->
                <div class="divider" style="margin:14px 0 10px"></div>
                <div class="collapsible-header" onclick="toggleCollapsible(this)">
                  Advanced
                  <span class="collapsible-arrow">&#9658;</span>
                </div>
                <div class="collapsible-body">
                  <div class="field">
                    <label>Gaussian Sigma <span class="tip" data-tip="Gaussian blur applied to the density field before meshing. 0.0 = faithful to CSV (default). 0.3 is recommended for sheet-type lattices to smooth abrupt density transitions." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
                    <input type="number" id="stlSmoothSigma" value="0.0" min="0.0" max="3.0" step="0.1" />
                  </div>
                  <div class="field-row-2">
                    <div class="field">
                      <label>Decimation Ratio <span class="tip" data-tip="Fraction of triangles to remove during mesh simplification. 0.90 = keep 10% of raw triangles (default). Set to 0.0 to disable decimation." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
                      <input type="number" id="stlDecimation" value="0.90" min="0.0" max="0.99" step="0.01" />
                    </div>
                    <div class="field">
                      <label>Decimation Mode <span class="tip" data-tip="Auto: chooses global or two-pass based on available RAM (recommended). Global: single pass on full mesh, cleanest. Two-pass: memory-safe chunked decimation." onmouseenter="showTip(this)" onmouseleave="hideTip()">?</span></label>
                      <select id="stlDecimationMode">
                        <option value="auto" selected>Auto</option>
                        <option value="global">Global</option>
                        <option value="two_pass">Two-Pass</option>
                      </select>
                    </div>
                  </div>
                </div>

              </div><!-- end lattice options -->

              <!-- Output path (shared) -->
              <div class="divider" style="margin:14px 0 10px"></div>
              <div class="field">
                <label>Output STL Path</label>
                <input type="text" id="stlOutputPath"
                      placeholder="Auto-filled when folder is selected"
                      oninput="updateOutputPreview()" />
                <div class="output-preview" id="stlOutputPreview"></div>
                <div class="field-hint">Leave blank to use folder default</div>
              </div>

              <!-- Generate section - same structure as Launch page -->
              <div class="launch-section" style="margin-top:14px">
                <div class="launch-btn-row">
                  <button class="btn-launch" id="stlGenerateBtn" disabled onclick="generateSTL()">
                    GENERATE STL
                  </button>
                  <button class="btn-cancel-run" id="stlCancelBtn" onclick="requestCancelSTL()">
                    &#10005; CANCEL
                  </button>
                </div>
                <div class="run-info" id="stlRunInfo" style="display:none">
                  <div class="run-info-metrics" id="stlRunInfoMetrics">Launching STL generator...</div>
                  <div class="run-info-row">
                    <span class="run-info-elapsed" id="stlRunInfoElapsed"></span>
                    <button class="btn-extend" id="stlLogBtn" style="display:none" onclick="openStlLog()">&#9654; Log</button>
                  </div>
                </div>
              </div>

            </div>
          </div>
        </div>

      </div><!-- end stl-grid -->
    </div><!-- end stl-container -->
  </div><!-- end page-stl -->

  <!-- STL LOG MODAL -->
  <div class="stl-log-modal-overlay" id="stlLogModal">
    <div class="stl-log-modal">
      <div class="stl-log-header">
        <div class="stl-log-title">STL Generator Output</div>
        <button class="stl-log-close" onclick="closeStlLog()">&#215;</button>
      </div>
      <div id="stlLogContent"></div>
    </div>
  </div>

  <!-- CANCEL CONFIRMATION MODAL -->
  <div class="cancel-confirm-overlay" id="cancelConfirmModal">
    <div class="cancel-confirm-box">
      <div class="cancel-confirm-title" id="cancelConfirmTitle">CANCEL OPTIMIZATION?</div>
      <div class="cancel-confirm-body" id="cancelConfirmBody"></div>
      <div class="cancel-confirm-actions">
        <button class="btn-keep-running" onclick="dismissCancelModal()">Keep Running</button>
        <button class="btn-confirm-cancel" id="cancelConfirmBtn" onclick="confirmCancelAction()">Cancel</button>
      </div>
    </div>
  </div>

  <!-- STALE FILES WARNING MODAL -->
  <div class="stale-files-overlay" id="staleFilesModal">
    <div class="stale-files-box">
      <div class="stale-files-title">&#9888; Leftover Files Detected</div>
      <div class="stale-files-dir" id="staleFilesDir"></div>
      <div class="stale-files-desc">
        The following files were left behind by a previous run and must be removed before launching — Abaqus will refuse to start if a lock file is present. They will be sent to the <strong>Recycle Bin</strong>.
      </div>
      <div class="stale-files-list" id="staleFilesList"></div>
      <div class="stale-files-actions">
        <button class="btn-stale-abort" onclick="dismissStaleModal()">Abort Launch</button>
        <button class="btn-stale-confirm" onclick="confirmStaleAndLaunch()">Send to Bin &amp; Launch</button>
      </div>
    </div>
  </div>

  <!-- LATTICE OVERRIDE WARNING MODAL -->
  <div class="stale-files-overlay" id="latOverrideModal">
    <div class="stale-files-box">
      <div class="stale-files-title">&#9888; Lattice Override Warning</div>
      <div class="stale-files-desc" id="latOverrideModalText"></div>
      <div class="stale-files-actions">
        <button class="btn-stale-abort" onclick="latOverrideRevert()">Keep optimised lattice</button>
        <button class="btn-stale-confirm" onclick="latOverrideProceed()">Render anyway</button>
      </div>
    </div>
  </div>

  <!-- HISTORY: Label / Rename run modal -->
  <div class="run-label-overlay" id="runLabelModal">
    <div class="run-label-box">
      <div class="run-label-title" id="runLabelTitle">LABEL THIS RUN</div>
      <div id="runLabelRows"></div>
      <div class="run-label-actions">
        <button class="btn-label-cancel" onclick="cancelLabelModal()">Cancel</button>
        <button class="btn-label-confirm" id="runLabelConfirmBtn" onclick="confirmLabelModal()">Add</button>
      </div>
    </div>
  </div>

  <!-- HISTORY: Run source info popup -->
  <div class="run-info-overlay" id="runInfoModal">
    <div class="run-info-box">
      <div class="run-info-title">Run Source</div>
      <div class="run-info-label" id="runInfoLabel"></div>
      <div class="run-info-path" id="runInfoPath"></div>
      <div class="run-info-close"><button onclick="closeRunInfo()">Close</button></div>
    </div>
  </div>

  <!-- FOLDER BROWSER MODAL (shared by History and STL pages) -->
  <div class="modal-overlay" id="folderBrowserModal">
    <div class="modal">
      <div class="modal-header">
        <div class="modal-title">&#128194; Select Folder</div>
        <div class="breadcrumb" id="folderBreadcrumb">
          <span class="breadcrumb-part last">Loading...</span>
        </div>
      </div>
      <div class="modal-toolbar">
        <button class="btn-up" id="folderBtnUp" onclick="folderBrowserGoUp()">&#8593; Up</button>
        <input class="toolbar-path" id="folderToolbarPath" type="text"
               placeholder="Type or paste a path..."
               onkeydown="if(event.key==='Enter') folderBrowserNavigateTo(this.value)" />
        <button class="btn-go" onclick="folderBrowserNavigateTo(document.getElementById('folderToolbarPath').value)">Go</button>
      </div>
      <div class="drive-bar" id="folderDriveBar"></div>
      <div class="modal-filelist" id="folderFileList">
        <div class="filelist-empty">Loading...</div>
      </div>
      <div class="modal-footer">
        <div class="modal-selected-path" id="folderModalSelectedPath">No folder selected</div>
        <button class="btn-modal-cancel" onclick="closeFolderBrowser()">Cancel</button>
        <button class="btn-modal-select" id="btnFolderSelect"
                onclick="confirmFolderSelection()">Select This Folder</button>
      </div>
    </div>
  </div>

</div><!-- end app -->

<script>
// Chart.js from CDN
</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script>
// =============================================================================
//   STATE
// =============================================================================
let inspectionData = null;
let abaqusFound = false;
let isRunning = false;
let _cancelTarget      = null;  // 'run' | 'stl'
let _cancelInProgress  = false; // blocks handleRunStatus during cancel animation
let currentEngine = 'solid_void';

// =============================================================================
//   FIELD TOOLTIPS
// =============================================================================
(function() {
  var tt = document.getElementById('global-tooltip');
  var _hideTimer = null;

  window.showTip = function(el) {
    clearTimeout(_hideTimer);
    tt.textContent = el.dataset.tip || '';
    tt.classList.add('tt-visible');
    var r = el.getBoundingClientRect();
    // Default: centred above the icon
    var left = r.left + r.width / 2;
    var top  = r.top - 10;
    tt.style.left      = left + 'px';
    tt.style.top       = top  + 'px';
    tt.style.transform = 'translate(-50%, -100%)';
    // Clamp to viewport edges after layout
    requestAnimationFrame(function() {
      var tr = tt.getBoundingClientRect();
      if (tr.left < 8)                        tt.style.left = (left - tr.left + 8) + 'px';
      if (tr.right > window.innerWidth - 8)   tt.style.left = (left - (tr.right - window.innerWidth + 8)) + 'px';
      if (tr.top < 8) {
        // Flip below if too close to top
        tt.style.top       = (r.bottom + 10) + 'px';
        tt.style.transform = 'translate(-50%, 0)';
      }
    });
  };

  window.hideTip = function() {
    _hideTimer = setTimeout(function() { tt.classList.remove('tt-visible'); }, 80);
  };
})();

// =============================================================================
//   UTILITIES
// =============================================================================
function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function hexToRgba(hex, alpha) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
}

// =============================================================================
//   ENGINE SWITCHER
// =============================================================================
function toggleSafetyFactorField() {
  const abs = document.getElementById("latSizeAbs");
  const f = document.getElementById("safetyFactorField");
  if (f && abs) f.classList.toggle("hidden-field", !abs.checked);
}
function toggleSkinThicknessField() {
  const on = document.getElementById("skinOn");
  const f = document.getElementById("skinThicknessField");
  if (f && on) f.classList.toggle("hidden-field", !on.checked);
}
function enforceLatticeModeConstraints() {
  const disc = document.getElementById("latDiscrete");
  const sScaled = document.getElementById("latSizeScaled");
  const sAbs = document.getElementById("latSizeAbs");
  if (!disc || !sScaled || !sAbs) return;
  if (disc.checked && sScaled.checked) { sAbs.checked = true; }
  const scaledOn = sScaled.checked;
  const discreteOn = disc.checked;
  disc.disabled = scaledOn;
  sScaled.disabled = discreteOn;
  const discLbl = document.querySelector('label[for="latDiscrete"]');
  const scaledLbl = document.querySelector('label[for="latSizeScaled"]');
  if (discLbl) discLbl.title = scaledOn ? "Discrete 3-Bin requires Sized to Load mode (scaling has no effect on discrete geometry)." : "";
  if (scaledLbl) scaledLbl.title = discreteOn ? "Specific Stiffness is unavailable while Discrete 3-Bin is selected - discrete runs only in Sized to Load." : "";
  toggleSafetyFactorField();
}
function toggleThermalParams() {
  const on = document.getElementById('thermalOn').checked;
  document.getElementById('thermalParamsGroup').classList.toggle('hidden-field', !on);
}

function switchEngine(engine) {
  currentEngine = engine;

  // Toggle solid-void-only panels
  document.querySelectorAll('.sv-only').forEach(el => {
    el.classList.toggle('hidden-field', engine === 'lattice');
  });
  // Toggle lattice-only panels
  document.querySelectorAll('.lat-only').forEach(el => {
    el.classList.toggle('hidden-field', engine === 'solid_void');
  });

  // Update launch button text
  const btn = document.getElementById('launchBtn');
  if (btn && !isRunning) {
    btn.innerHTML = engine === 'lattice'
      ? 'LAUNCH LATTICE OPTIMIZATION'
      : 'LAUNCH OPTIMIZATION';
  }

  updateLaunchButton();
  enforceLatticeModeConstraints();
  toggleSkinThicknessField();
}

function toggleCollapsible(header) {
  const body = header.nextElementSibling;
  const isOpen = body.classList.contains('open');
  body.classList.toggle('open', !isOpen);
  header.classList.toggle('open', !isOpen);
}
let existingConfig = null;
let browserSelectedPath = null;
let browserCurrentPath = null;
let folderBrowserTarget = null; // 'history' or 'stl'
let folderBrowserCurrentPath = null;
let historyChart = null;

// =============================================================================
//   PAGE NAVIGATION
// =============================================================================
var _stlPageVisited = false;

function switchPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  document.querySelectorAll('.nav-tab').forEach(t => {
    t.classList.toggle('active', t.textContent.trim().toLowerCase() === name);
  });
  if (name === 'stl' && !_stlPageVisited) {
    _stlPageVisited = true;
    setTimeout(onStlPageInit, 80);
  }
}

// =============================================================================
//   INIT
// =============================================================================
window.onload = function() {
  checkAbaqus();
  checkExistingConfig();
  loadDrives();
  loadFolderDrives();
  validateAll();
};

function checkAbaqus() {
  fetch('/api/check_abaqus')
    .then(r => r.json())
    .then(data => {
      abaqusFound = data.found;
      setCheck('chk-abaqus',
        abaqusFound ? 'pass' : 'fail',
        abaqusFound ? '✓' : '✗',
        abaqusFound ? 'Abaqus executable found on PATH'
                    : 'abaqus command not found - check installation'
      );
      updateLaunchButton();
    })
    .catch(() => {
      setCheck('chk-abaqus', 'warn', '!', 'Could not verify Abaqus on PATH');
    });
}

function checkExistingConfig() {
  fetch('/api/existing_config')
    .then(r => r.json())
    .then(data => {
      // Only show banner if launcher-written config exists (has timestamp)
      if (data.exists && data.config && data.config.timestamp) {
        existingConfig = data.config;
        document.getElementById('configTimestamp').textContent = data.config.timestamp;
        document.getElementById('configBanner').style.display = 'flex';
      }
    })
    .catch(() => { /* silently ignore */ });
}

function loadDrives() {
  fetch('/api/drives')
    .then(r => r.json())
    .then(data => {
      const bar = document.getElementById('driveBar');
      if (data.drives && data.drives.length > 1) {
        bar.classList.add('visible');
        data.drives.forEach(d => {
          const btn = document.createElement('button');
          btn.className = 'btn-drive';
          btn.textContent = d.name;
          btn.onclick = () => browserNavigateTo(d.path);
          bar.appendChild(btn);
        });
      }
    })
    .catch(() => {});
}

// =============================================================================
//   CONFIG BANNER
// =============================================================================
function loadConfig() {
  if (!existingConfig) return;
  const c = existingConfig;

  // Restore engine selector
  if (c.engine) {
    const engRadio = document.querySelector(`input[name="engineType"][value="${c.engine}"]`);
    if (engRadio) { engRadio.checked = true; switchEngine(c.engine); }
  }

  if (c.model) {
    if (c.model.inp_path) {
      document.getElementById('inpPath').value = c.model.inp_path;
      inspectInpFile(c.model.inp_path);
    }
    if (c.model.base_job) document.getElementById('baseJob').value = c.model.base_job;
  }

  if (c.optimization) {
    const o = c.optimization;
    if (o.is_gaussian_filter !== undefined)
      document.querySelector(`input[name="filterType"][value="${o.is_gaussian_filter ? 'gaussian' : 'linear'}"]`).checked = true;
    if (o.custom_weights) {
      document.querySelector('input[name="weightType"][value="custom"]').checked = true;
      document.getElementById('customWeights').value = o.custom_weights.join(', ');
      toggleCustomWeights();
    }

    if (c.engine === 'lattice') {
      // Lattice-specific optimization fields
      if (o.is_continuous !== undefined) {
        const v = o.is_continuous ? 'continuous' : 'discrete';
        const r = document.querySelector(`input[name="latPhil"][value="${v}"]`);
        if (r) r.checked = true;
      }
      if (o.enable_skin !== undefined) {
        const v = o.enable_skin ? 'on' : 'off';
        const r = document.querySelector(`input[name="skinOpt"][value="${v}"]`);
        if (r) r.checked = true;
      }
      if (o.skin_thickness        !== undefined) document.getElementById('skinThickness').value = o.skin_thickness;
      if (o.yield_stress          !== undefined) document.getElementById('latYieldStress').value  = o.yield_stress;
      if (o.gibson_ashby_exponent !== undefined) document.getElementById('gibsonAshbyExp').value  = o.gibson_ashby_exponent;
      if (o.yield_exponent        !== undefined) document.getElementById('yieldExponent').value   = o.yield_exponent;
      if (o.lattice_type          !== undefined) { const _r = document.querySelector('input[name="latticeType"][value="' + o.lattice_type + '"]'); if (_r) _r.checked = true; }
      if (o.stiffness_coeff       !== undefined) document.getElementById('stiffnessCoeff').value  = o.stiffness_coeff;
      if (o.yield_coeff           !== undefined) document.getElementById('yieldCoeff').value      = o.yield_coeff;
      if (o.evolution_quota_pct   !== undefined) document.getElementById('evolutionQuota').value  = o.evolution_quota_pct;
      if (o.void_threshold_pct    !== undefined) document.getElementById('voidThreshold').value   = o.void_threshold_pct;
      if (o.move_limit            !== undefined) {
        document.getElementById('moveLimit').value    = o.move_limit;
        document.getElementById('moveLimitVal').textContent = parseFloat(o.move_limit).toFixed(2);
      }
      if (o.lattice_property_model !== undefined) { const _m = document.querySelector('input[name="latticeMethod"][value="' + o.lattice_property_model + '"]'); if (_m) _m.checked = true; }
      if (o.lattice_cap           !== undefined) document.getElementById('latticeCap').value = o.lattice_cap;
      if (o.load_mode             !== undefined) { const _s = document.querySelector('input[name="latticeSizing"][value="' + o.load_mode + '"]'); if (_s) _s.checked = true; }
      if (o.safety_factor         !== undefined) document.getElementById('safetyFactor').value = o.safety_factor;
      enforceLatticeModeConstraints();
      toggleSkinThicknessField();
    } else {
      // Solid-void-specific optimization fields
      if (o.is_dynamic_er !== undefined)
        document.querySelector(`input[name="erType"][value="${o.is_dynamic_er ? 'dynamic' : 'fixed'}"]`).checked = true;
      if (o.thermal_weight !== undefined) {
        document.getElementById('thermalWeight').value = o.thermal_weight;
        document.getElementById('thermalWeightVal').textContent =
          parseFloat(o.thermal_weight).toFixed(2);
      }
      if (o.use_thermal !== undefined) {
        document.getElementById(o.use_thermal ? 'thermalOn' : 'thermalOff').checked = true;
        toggleThermalParams();
      }
      if (o.print_direction) {
        const d = document.querySelector(`input[name="printDir"][value="${o.print_direction}"]`);
        if (d) d.checked = true;
      }
      if (o.target_k22 !== undefined) document.getElementById('k22').value = o.target_k22;
    }
  }

  if (c.advanced) {
    const a = c.advanced;
    if (c.engine === 'lattice') {
      if (a.filter_multiplier   !== undefined) document.getElementById('filterMultiplier').value   = a.filter_multiplier;
      if (a.max_iterations      !== undefined) document.getElementById('maxIterations').value      = a.max_iterations;
      if (a.early_stop_warmup   !== undefined) document.getElementById('earlyStopWarmup').value    = a.early_stop_warmup;
      if (a.early_stop_patience !== undefined) document.getElementById('earlyStopPatience').value  = a.early_stop_patience;
      if (a.early_stop_flatline !== undefined) document.getElementById('earlyStopFlatline').value  = (a.early_stop_flatline * 100).toFixed(2);
    } else {
      if (a.target_volume_fraction !== undefined) document.getElementById('targetVf').value = a.target_volume_fraction;
      if (a.filter_radius !== undefined)          document.getElementById('filterRadius').value = a.filter_radius;
      if (a.micro_radius !== undefined)           document.getElementById('microRadius').value = a.micro_radius;
      if (a.total_applied_force !== undefined)    document.getElementById('totalForce').value = a.total_applied_force;
    }
    // Shared solver fields (both engines)
    if (a.cpus           !== undefined) document.getElementById('cpus').value       = a.cpus;
    if (a.memory_percent !== undefined) document.getElementById('memPercent').value = a.memory_percent;
  }

  dismissConfigBanner();
  validateAll();
}

function dismissConfigBanner() {
  document.getElementById('configBanner').style.display = 'none';
  existingConfig = null;
}

// =============================================================================
//   FILE BROWSER MODAL
// =============================================================================
function openBrowser() {
  document.getElementById('fileBrowserModal').classList.add('open');
  // Navigate to last known directory or cwd
  browserNavigateTo(browserCurrentPath || '');
}

function closeBrowser() {
  document.getElementById('fileBrowserModal').classList.remove('open');
}

function browserNavigateTo(path) {
  document.getElementById('fileList').innerHTML =
    '<div class="filelist-empty">Loading...</div>';

  fetch('/api/browse', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: path || ''})
  })
  .then(r => r.json())
  .then(data => {
    browserCurrentPath = data.current_path;
    document.getElementById('toolbarPath').value = data.current_path;

    // Up button
    document.getElementById('btnUp').disabled =
      data.at_root || !data.parent_path;

    // Breadcrumb
    renderBreadcrumb(data.breadcrumbs || []);

    // File list
    renderFileList(data.entries || [], data.error);
  })
  .catch(err => {
    document.getElementById('fileList').innerHTML =
      '<div class="filelist-error">Error loading directory: ' + err + '</div>';
  });
}

function renderBreadcrumb(parts) {
  const bc = document.getElementById('breadcrumb');
  bc.innerHTML = '';
  parts.forEach((p, i) => {
    const span = document.createElement('span');
    span.className = 'breadcrumb-part' + (i === parts.length - 1 ? ' last' : '');
    span.textContent = p.name;
    if (i < parts.length - 1) {
      span.onclick = () => browserNavigateTo(p.path);
    }
    bc.appendChild(span);
    if (i < parts.length - 1) {
      const sep = document.createElement('span');
      sep.className = 'breadcrumb-sep';
      sep.textContent = ' › ';
      bc.appendChild(sep);
    }
  });
}

function renderFileList(entries, error) {
  const list = document.getElementById('fileList');
  list.innerHTML = '';

  if (error) {
    list.innerHTML = '<div class="filelist-error">⚠ ' + error + '</div>';
    return;
  }

  if (entries.length === 0) {
    list.innerHTML = '<div class="filelist-empty">No folders or .inp files found here</div>';
    return;
  }

  entries.forEach(entry => {
    const div = document.createElement('div');
    div.className = 'file-entry' + (entry.is_dir ? ' is-dir' : '');

    const icon = document.createElement('span');
    icon.className = 'file-icon';
    icon.textContent = entry.is_dir ? '📁' : '📄';

    const name = document.createElement('span');
    name.className = 'file-name';
    name.textContent = entry.name;

    div.appendChild(icon);
    div.appendChild(name);

    if (!entry.is_dir && entry.size !== null) {
      const size = document.createElement('span');
      size.className = 'file-size';
      size.textContent = formatSize(entry.size);
      div.appendChild(size);
    }

    if (entry.is_dir) {
      div.ondblclick = () => browserNavigateTo(entry.full_path);
      div.onclick    = () => browserNavigateTo(entry.full_path);
    } else {
      div.onclick = () => browserSelectFile(entry.full_path, div);
      div.ondblclick = () => {
        browserSelectFile(entry.full_path, div);
        confirmBrowserSelection();
      };
    }

    list.appendChild(div);
  });
}

function browserSelectFile(path, el) {
  // Deselect all
  document.querySelectorAll('.file-entry.selected').forEach(e => e.classList.remove('selected'));
  el.classList.add('selected');

  browserSelectedPath = path;

  const footer = document.getElementById('modalSelectedPath');
  footer.textContent = path;
  footer.className = 'modal-selected-path has-file';

  document.getElementById('btnModalSelect').disabled = false;
}

function browserGoUp() {
  if (!browserCurrentPath) return;
  fetch('/api/browse', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: browserCurrentPath, go_up: true})
  })
  .then(r => r.json())
  .then(data => {
    browserCurrentPath = data.current_path;
    document.getElementById('toolbarPath').value = data.current_path;
    document.getElementById('btnUp').disabled = data.at_root || !data.parent_path;
    renderBreadcrumb(data.breadcrumbs || []);
    renderFileList(data.entries || [], data.error);
    // Reset selection
    browserSelectedPath = null;
    document.getElementById('modalSelectedPath').textContent = 'No file selected';
    document.getElementById('modalSelectedPath').className = 'modal-selected-path';
    document.getElementById('btnModalSelect').disabled = true;
  });
}

function confirmBrowserSelection() {
  if (!browserSelectedPath) return;
  document.getElementById('inpPath').value = browserSelectedPath;

  // Auto-fill base job name from filename
  const filename = browserSelectedPath.split(/[\\/]/).pop();
  const jobName  = filename.replace(/\.inp$/i, '');
  document.getElementById('baseJob').value = jobName;

  closeBrowser();
  inspectInpFile(browserSelectedPath);
}

function formatSize(bytes) {
  if (bytes < 1024)       return bytes + ' B';
  if (bytes < 1048576)    return (bytes/1024).toFixed(1) + ' KB';
  return (bytes/1048576).toFixed(1) + ' MB';
}

// Close modal when clicking outside
document.addEventListener('click', function(e) {
  const overlay = document.getElementById('fileBrowserModal');
  if (e.target === overlay) closeBrowser();
});

// =============================================================================
//   FILE SELECTION (typed path)
// =============================================================================
let _inpTypingTimer = null;

function onInpPathTyped() {
  clearTimeout(_inpTypingTimer);
  _inpTypingTimer = setTimeout(() => {
    const path = document.getElementById('inpPath').value.trim();
    if (path.length > 3) inspectInpFile(path);
  }, 600);
}

function inspectInpFile(path) {
  document.getElementById('detectedPanel').classList.add('hidden');
  setCheck('chk-inp', 'idle', '○', 'Inspecting file...');
  setCheck('chk-nondesign', 'idle', '○', 'Awaiting inspection...');
  setCheck('chk-material',  'idle', '○', 'Awaiting inspection...');
  setCheck('chk-elemtype',  'idle', '○', 'Awaiting inspection...');

  fetch('/api/inspect_inp', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: path})
  })
  .then(r => r.json())
  .then(data => {
    inspectionData = data;
    updateDetectedPanel(data);
    updateValidationFromInspection(data);
    updateLaunchButton();
  })
  .catch(() => {
    setCheck('chk-inp', 'fail', '✗', 'Server error during inspection');
  });
}

function syncJobName() {}

function updateDetectedPanel(data) {
  if (!data.found) return;
  document.getElementById('detectedPanel').classList.remove('hidden');

  const set = (id, val, cls) => {
    const el = document.getElementById(id);
    el.textContent = val;
    el.className   = 'detected-val ' + cls;
  };

  set('d-elemType',  data.element_type || 'Not detected', data.element_type ? 'good' : 'bad');
  set('d-elemCount', data.element_count > 0 ? data.element_count.toLocaleString() : '-',
      data.element_count > 0 ? 'good' : 'warn');
  set('d-partsDetected',
      data.parts_detected > 0 ? String(data.parts_detected) : 'Single domain (no parts)',
      'good');
  set('d-designPart',
      data.design_part ? data.design_part : (data.parts_detected > 0 ? 'Not resolved' : 'Single domain'),
      data.design_part || data.parts_detected === 0 ? 'good' : 'warn');
  const _dpEl = document.getElementById('d-designPart');
  if (_dpEl) _dpEl.title = data.design_part ? data.design_part : '';
  set('d-nonDesign',
      data.non_design_set_found
        ? data.non_design_element_count.toLocaleString() + ' elements'
            + (data.design_part ? ' (design part)' : '')
        : 'Not found',
      data.non_design_set_found ? 'good' : 'warn');
  set('d-eModulus',  data.e_modulus ? data.e_modulus.toLocaleString() + ' MPa' : 'Not detected',
      data.e_modulus ? 'good' : 'warn');
  set('d-matSource',
      data.material_fallback
        ? 'Fallback - last section (verify)'
        : (data.material_name
            ? (data.design_part ? 'Design part section' : 'Section material')
            : 'Not detected'),
      data.material_fallback ? 'warn' : (data.material_name ? 'good' : 'warn'));
  set('d-poisson',   data.poisson !== null ? String(data.poisson) : 'Not detected',
      data.poisson !== null ? 'good' : 'warn');
  set('d-thermal',
      data.has_thermal_step ? 'Detected' : 'Not found (will be injected)',
      data.has_thermal_step ? 'good' : 'warn');
  set('d-instance',  data.instance_name || 'Not detected (using fallback)',
      data.instance_name ? 'good' : 'warn');

  // Populate the lattice yield stress field with the same heuristic used by beso_lattice_main.py
  if (data.e_modulus) {
    const E = parseFloat(data.e_modulus);
    let detectedYield;
    if      (E < 80000)  detectedYield = 300;   // Aluminum
    else if (E > 180000) detectedYield = 400;   // Steel
    else                 detectedYield = 900;   // Titanium (default)
    const yieldField = document.getElementById('latYieldStress');
    if (yieldField) yieldField.value = detectedYield;
  }
}

function updateValidationFromInspection(data) {
  if (!data.found) {
    setCheck('chk-inp', 'fail', '✗', 'File not found: ' + document.getElementById('inpPath').value);
    setCheck('chk-nondesign', 'idle', '○', 'Awaiting valid file');
    setCheck('chk-material',  'idle', '○', 'Awaiting valid file');
    setCheck('chk-elemtype',  'idle', '○', 'Awaiting valid file');
    return;
  }
  if (!data.readable) {
    setCheck('chk-inp', 'fail', '✗', 'File exists but could not be read');
    return;
  }
  if (!data.has_solid_section) {
    setCheck('chk-inp', 'fail', '✗', 'No *Solid Section found - check INP structure');
    return;
  }
  setCheck('chk-inp', 'pass', '✓',
    'File found and readable (' + data.element_count.toLocaleString() + ' elements)');

  // Non-design
  if (data.non_design_set_found) {
    setCheck('chk-nondesign', 'pass', '✓',
      data.non_design_element_count.toLocaleString() + ' protected elements found');
  } else {
    setCheck('chk-nondesign', 'warn', '!',
      'NON_DESIGN_SET not found - entire mesh is design space');
  }

  // Material
  if (data.e_modulus !== null) {
    if (data.material_fallback) {
      setCheck('chk-material', 'warn', '!',
        'E = ' + data.e_modulus + ' MPa (no section in design part - using last section; verify material)');
    } else {
      setCheck('chk-material', 'pass', '✓',
        'E = ' + data.e_modulus + ' MPa, ν = ' + data.poisson
        + (data.design_part ? ' [' + data.design_part + ']' : ''));
    }
  } else {
    setCheck('chk-material', 'warn', '!',
      'Not detected - defaults used (E=110000 MPa, ν=0.33)');
  }

  // Element type
  if (data.element_type) {
    const supported = ['C3D8','C3D4','C3D6','C3D10','C3D15','C3D20'];
    const ok = supported.some(s => data.element_type.startsWith(s));
    setCheck('chk-elemtype', ok ? 'pass' : 'warn', ok ? '✓' : '!',
      data.element_type + (ok ? ' - supported by STL generator' : ' - may not be fully supported'));
  } else {
    setCheck('chk-elemtype', 'warn', '!', 'Element type not detected in INP');
  }
}

// =============================================================================
//   LIVE VALIDATION
// =============================================================================
function validateAll() {
  validateWeights();
  validateVF();
  updateLaunchButton();
}

function toggleCustomWeights() {
  const isCustom = document.querySelector('input[name="weightType"]:checked').value === 'custom';
  document.getElementById('customWeightsField').classList.toggle('visible', isCustom);
  validateWeights();
}

function validateWeights() {
  const isCustom = document.querySelector('input[name="weightType"]:checked').value === 'custom';
  if (!isCustom) {
    setCheck('chk-weights', 'pass', '✓', 'Equal weighting - auto-calculated per load case');
    updateLaunchButton();
    return;
  }
  const raw   = document.getElementById('customWeights').value.trim();
  if (!raw) { setCheck('chk-weights', 'fail', '✗', 'Custom weights field is empty'); updateLaunchButton(); return; }
  const parts = raw.split(',').map(s => s.trim()).filter(s => s);
  const vals  = parts.map(Number);
  if (vals.some(isNaN))   { setCheck('chk-weights', 'fail', '✗', 'Non-numeric value in weights'); updateLaunchButton(); return; }
  if (vals.some(v => v<0)){ setCheck('chk-weights', 'fail', '✗', 'Weights must be positive');     updateLaunchButton(); return; }
  const total = vals.reduce((a,b)=>a+b,0);
  const norm  = vals.map(v=>(v/total).toFixed(3)).join(', ');
  setCheck('chk-weights', 'pass', '✓', vals.length + ' weights - normalised: ' + norm);
  updateLaunchButton();
}

function validateVF() {
  const vf = parseFloat(document.getElementById('targetVf').value);
  if (isNaN(vf) || vf < 0.10 || vf > 0.95) {
    setCheck('chk-vf', 'fail', '✗', 'Must be between 0.10 and 0.95');
  } else {
    setCheck('chk-vf', 'pass', '✓',
      (vf*100).toFixed(0) + '% solid - ' + ((1-vf)*100).toFixed(0) + '% will be removed');
  }
  updateLaunchButton();
}

// =============================================================================
//   HELPERS
// =============================================================================
function setCheck(id, state, icon, detail) {
  const el = document.getElementById(id);
  if (!el) return;
  // Preserve engine-visibility classes before className is overwritten
  const isSvOnly  = el.classList.contains('sv-only');
  const isLatOnly = el.classList.contains('lat-only');
  el.className = 'check-item ' + state;
  if (isSvOnly)  {
    el.classList.add('sv-only');
    if (currentEngine === 'lattice')    el.classList.add('hidden-field');
  }
  if (isLatOnly) {
    el.classList.add('lat-only');
    if (currentEngine === 'solid_void') el.classList.add('hidden-field');
  }
  el.querySelector('.check-detail').textContent = detail;
}

function updateLaunchButton() {
  const btn = document.getElementById('launchBtn');
  if (isRunning) return;
  const criticalIds = currentEngine === 'lattice'
    ? ['chk-inp','chk-abaqus','chk-weights']
    : ['chk-inp','chk-abaqus','chk-weights','chk-vf'];
  const anyFail = criticalIds.some(id => {
    const el = document.getElementById(id);
    return el && el.classList.contains('fail');
  });
  const hasFile  = document.getElementById('inpPath').value.trim().length > 0;
  const inpReady = inspectionData && inspectionData.found && inspectionData.readable;
  const wasDisabled = btn.disabled;
  btn.disabled = anyFail || !hasFile || !inpReady;

  // Cinematic all-clear: pulse the checklist panel when it first becomes ready
  if (wasDisabled && !btn.disabled) {
    const checklist = document.getElementById('checklist');
    if (checklist) {
      checklist.classList.remove('preflight-allclear');
      void checklist.offsetWidth; // reflow to restart animation
      checklist.classList.add('preflight-allclear');
      setTimeout(() => checklist.classList.remove('preflight-allclear'), 900);
    }
  }
}

// =============================================================================
//   LAUNCH
// =============================================================================
function launchOptimizer() {
  if (isRunning) return;
  const payload  = gatherSettings();
  const inpPath  = document.getElementById('inpPath').value.trim();

  // Pre-launch: scan for Abaqus leftover files that would block the run
  fetch('/api/scan_abaqus_leftovers', {
    method:  'POST',
    headers: {'Content-Type': 'application/json'},
    body:    JSON.stringify({inp_path: inpPath})
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.files && data.files.length > 0) {
      showStaleFilesModal(data.files, data.directory, payload);
    } else {
      doLaunch(payload);
    }
  })
  .catch(function() {
    // If the scan itself fails, proceed — don't block the user
    doLaunch(payload);
  });
}

// --- Stale files modal ---
var _staleFilesPayload = null;
var _staleFilesItems   = [];

function showStaleFilesModal(files, directory, payload) {
  _staleFilesPayload = payload;
  _staleFilesItems   = files;

  // Directory note
  document.getElementById('staleFilesDir').textContent =
    'Located in: ' + (directory || 'the same folder as your INP file');

  // File list
  const listEl = document.getElementById('staleFilesList');
  listEl.innerHTML = '';
  files.forEach(function(f) {
    const row = document.createElement('div');
    row.className = 'stale-file-row';
    row.innerHTML =
      '<span class="stale-file-icon">' + (f.is_dir ? '&#128193;' : '&#128196;') + '</span>'
      + '<span>' + escHtml(f.name) + '</span>';
    listEl.appendChild(row);
  });

  document.getElementById('staleFilesModal').classList.add('open');
}

function dismissStaleModal() {
  document.getElementById('staleFilesModal').classList.remove('open');
  _staleFilesPayload = null;
  _staleFilesItems   = [];
}

function confirmStaleAndLaunch() {
  const payload = _staleFilesPayload;   // capture before dismissal clears these
  const items   = _staleFilesItems;
  const confirmBtn = document.querySelector('.btn-stale-confirm');
  if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.textContent = 'Deleting...'; }

  fetch('/api/recycle_files', {
    method:  'POST',
    headers: {'Content-Type': 'application/json'},
    body:    JSON.stringify({files: items})
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    dismissStaleModal();
    if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.textContent = 'Send to Bin \u0026 Launch'; }
    if (!data.success) {
      const failed = (data.results || []).filter(function(r) { return !r.success; })
                                         .map(function(r) { return r.path.split(/[\\/]/).pop(); });
      if (failed.length) {
        alert('Warning: could not remove the following files — you may need to delete them manually before Abaqus can run:\n\n' + failed.join('\n'));
      }
    }
    doLaunch(payload);
  })
  .catch(function() {
    dismissStaleModal();
    if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.textContent = 'Send to Bin \u0026 Launch'; }
    doLaunch(payload);
  });
}

function doLaunch(payload) {
  fetch('/api/launch', {
    method:  'POST',
    headers: {'Content-Type': 'application/json'},
    body:    JSON.stringify(payload)
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.success) { isRunning = true; setRunningState(); }
    else { alert('Launch failed: ' + (data.error || 'Unknown error')); }
  })
  .catch(function(err) { alert('Server error: ' + err); });
}

function gatherSettings() {
  const isCustom = document.querySelector('input[name="weightType"]:checked').value === 'custom';
  let customWeights = null;
  if (isCustom) {
    const raw  = document.getElementById('customWeights').value.trim();
    const vals = raw.split(',').map(s => parseFloat(s.trim())).filter(v => !isNaN(v));
    const tot  = vals.reduce((a,b)=>a+b,0);
    customWeights = vals.map(v => v/tot);
  }

  const base = {
    engine: currentEngine,
    model: {
      base_job: document.getElementById('baseJob').value.trim() ||
                document.getElementById('inpPath').value.split(/[\\/]/).pop().replace(/\.inp$/i,''),
      inp_path: document.getElementById('inpPath').value.trim(),
      detected_element_type:  inspectionData ? inspectionData.element_type  : null,
      detected_element_count: inspectionData ? inspectionData.element_count : 0
    }
  };

  if (currentEngine === 'lattice') {
    const latPhil = document.querySelector('input[name="latPhil"]:checked');
    const skinOpt = document.querySelector('input[name="skinOpt"]:checked');
    base.optimization = {
      is_gaussian_filter:    document.querySelector('input[name="filterType"]:checked').value === 'gaussian',
      is_continuous:         latPhil ? latPhil.value === 'continuous' : true,
      enable_skin:           skinOpt ? skinOpt.value === 'on' : true,
      skin_thickness:        parseInt(document.getElementById('skinThickness').value, 10),
      custom_weights:        customWeights,
      yield_stress:          parseFloat(document.getElementById('latYieldStress').value),
      lattice_type:          (document.querySelector('input[name="latticeType"]:checked') || {}).value || 'iwp',
      stiffness_coeff:       parseFloat(document.getElementById('stiffnessCoeff').value),
      gibson_ashby_exponent: parseFloat(document.getElementById('gibsonAshbyExp').value),
      yield_coeff:           parseFloat(document.getElementById('yieldCoeff').value),
      yield_exponent:        parseFloat(document.getElementById('yieldExponent').value),
      evolution_quota_pct:   parseFloat(document.getElementById('evolutionQuota').value),
      void_threshold_pct:    parseFloat(document.getElementById('voidThreshold').value),
      move_limit:            parseFloat(document.getElementById('moveLimit').value),
      lattice_property_model: (document.querySelector('input[name="latticeMethod"]:checked') || {}).value || 'safe',
      lattice_cap:           parseFloat(document.getElementById('latticeCap').value),
      load_mode:             (document.querySelector('input[name="latticeSizing"]:checked') || {}).value || 'scaled',
      safety_factor:         parseFloat(document.getElementById('safetyFactor').value)
    };
    base.advanced = {
      filter_multiplier:    parseInt(document.getElementById('filterMultiplier').value),
      max_iterations:       parseInt(document.getElementById('maxIterations').value),
      early_stop_warmup:    parseInt(document.getElementById('earlyStopWarmup').value),
      early_stop_patience:  parseInt(document.getElementById('earlyStopPatience').value),
      early_stop_flatline:  parseFloat(document.getElementById('earlyStopFlatline').value) / 100,
      cpus:                 parseInt(document.getElementById('cpus').value),
      memory_percent:       parseInt(document.getElementById('memPercent').value)
    };
  } else {
    const printDir   = document.querySelector('input[name="printDir"]:checked');
    const useThermal = document.getElementById('thermalOn').checked;
    base.optimization = {
      is_dynamic_er:      document.querySelector('input[name="erType"]:checked').value === 'dynamic',
      is_gaussian_filter: document.querySelector('input[name="filterType"]:checked').value === 'gaussian',
      custom_weights:     customWeights,
      use_thermal:        useThermal,
      thermal_weight:     useThermal ? parseFloat(document.getElementById('thermalWeight').value) : 0.0,
      print_direction:    useThermal ? (printDir ? printDir.value : '+Y') : 'Traditional',
      target_k22:         useThermal ? parseFloat(document.getElementById('k22').value) : 5.0
    };
    base.advanced = {
      target_volume_fraction: parseFloat(document.getElementById('targetVf').value),
      filter_radius:          parseFloat(document.getElementById('filterRadius').value),
      micro_radius:           parseFloat(document.getElementById('microRadius').value),
      total_applied_force:    parseFloat(document.getElementById('totalForce').value),
      cpus:                   parseInt(document.getElementById('cpus').value),
      memory_percent:         parseInt(document.getElementById('memPercent').value)
    };
  }
  return base;
}

// =============================================================================
//   CANCEL - confirmation modal + process kill
// =============================================================================
function requestCancelRun() {
  _cancelTarget = 'run';
  document.getElementById('cancelConfirmTitle').textContent = 'CANCEL OPTIMIZATION?';
  document.getElementById('cancelConfirmBody').innerHTML =
    'This will immediately terminate the running optimization process.'
    + '<span class="keep-note">Partial results and output files in the results folder will be kept.</span>';
  document.getElementById('cancelConfirmBtn').textContent = 'Cancel Optimization';
  document.getElementById('cancelConfirmModal').classList.add('open');
}

function requestCancelSTL() {
  _cancelTarget = 'stl';
  document.getElementById('cancelConfirmTitle').textContent = 'CANCEL STL GENERATION?';
  document.getElementById('cancelConfirmBody').innerHTML =
    'This will immediately terminate the STL generation process.'
    + '<span class="keep-note">Any partial output files will be kept.</span>';
  document.getElementById('cancelConfirmBtn').textContent = 'Cancel STL Generation';
  document.getElementById('cancelConfirmModal').classList.add('open');
}

function dismissCancelModal() {
  document.getElementById('cancelConfirmModal').classList.remove('open');
  _cancelTarget = null;
}

function confirmCancelAction() {
  const target = _cancelTarget;
  dismissCancelModal();
  if (target === 'run') { executeCancelRun(); }
  else if (target === 'stl') { executeCancelSTL(); }
}

function executeCancelRun() {
  fetch('/api/cancel_run', { method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: '{}' })
    .then(function(r) { return r.json(); })
    .then(function() { applyRunCancelledState(); })
    .catch(function() { applyRunCancelledState(); });
}

function applyRunCancelledState() {
  _cancelInProgress = true;
  stopPolling();
  stopLogPoll();
  isRunning  = false;
  _runStatus = 'idle';

  // Hide cancel button
  var cancelBtn = document.getElementById('launchCancelBtn');
  if (cancelBtn) cancelBtn.classList.remove('visible');

  // Re-enable everything
  document.querySelectorAll('input, button').forEach(function(el) { el.disabled = false; });
  updateLaunchButton();

  // Show cancelled state — fresh span ensures animation always restarts
  var btn = document.getElementById('launchBtn');
  btn.className = 'btn-launch cancelled';
  btn.innerHTML = '&#10005; CANCELLED<span class="cancel-drain-bar"></span>';

  // After 1s the drain animation finishes — reset to normal
  setTimeout(function() {
    _cancelInProgress = false;
    btn.classList.remove('cancelled');
    btn.innerHTML = currentEngine === 'lattice' ? 'LAUNCH LATTICE OPTIMIZATION' : 'LAUNCH OPTIMIZATION';
    document.getElementById('runInfo').style.display    = 'none';
    document.getElementById('relaunchBtn').style.display = 'none';
  }, 1000);
}

function executeCancelSTL() {
  fetch('/api/cancel_stl', { method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: '{}' })
    .then(function(r) { return r.json(); })
    .then(function() { applySTLCancelledState(); })
    .catch(function() { applySTLCancelledState(); });
}

function applySTLCancelledState() {
  if (window._stlPollInterval) { clearInterval(window._stlPollInterval); window._stlPollInterval = null; }

  // Hide cancel button
  var cancelBtn = document.getElementById('stlCancelBtn');
  if (cancelBtn) cancelBtn.classList.remove('visible');

  // Show cancelled state on STL button — fresh span ensures animation restarts
  var btn = document.getElementById('stlGenerateBtn');
  btn.className = 'btn-launch cancelled';
  btn.innerHTML = '&#10005; CANCELLED<span class="cancel-drain-bar"></span>';

  // After 1s reset
  setTimeout(function() {
    btn.classList.remove('cancelled');
    btn.innerHTML = 'GENERATE STL';
    btn.disabled  = false;
    // Restore stlRunInfo to neutral
    var ri = document.getElementById('stlRunInfo');
    if (ri) { ri.style.display = 'none'; ri.className = 'run-info'; }
  }, 1000);
}

function setRunningState() {
  isRunning = true;
  _stlPageVisited = false;  // allow STL popup to re-trigger for this new run
  var btn = document.getElementById('launchBtn');
  btn.disabled  = true;
  btn.className = 'btn-launch running sweeping';
  btn.innerHTML = '<span class="pulse-dot"></span> RUNNING';
  // Remove sweep class after animation completes
  setTimeout(() => btn.classList.remove('sweeping'), 600);
  var ri = document.getElementById('runInfo');
  ri.style.display = 'block';
  ri.className = 'run-info ri-running';
  document.getElementById('runInfoMetrics').textContent = 'Waiting for first iteration...';
  document.getElementById('runInfoElapsed').textContent = '';
  document.getElementById('relaunchBtn').style.display = 'block';
  document.querySelectorAll('input, button:not(#relaunchBtn):not(.btn-banner):not(.btn-extend):not(.log-modal-close):not(.nav-tab):not(#launchCancelBtn):not(.btn-keep-running):not(.btn-confirm-cancel):not(.btn-stale-abort):not(.btn-stale-confirm)').forEach(function(el) {
    el.disabled = true;
  });
  // Show cancel button
  var cancelBtn = document.getElementById('launchCancelBtn');
  if (cancelBtn) cancelBtn.classList.add('visible');
  startPolling();
}

function resetForRelaunch() {
  isRunning = false;
  stopPolling();
  stopLogPoll();
  _runStatus = 'idle';
  _logCursor = 0;
  var btn = document.getElementById('launchBtn');
  btn.className = 'btn-launch';
  btn.innerHTML = currentEngine === 'lattice' ? 'LAUNCH LATTICE OPTIMIZATION' : 'LAUNCH OPTIMIZATION';
  document.getElementById('relaunchBtn').style.display = 'none';
  document.getElementById('runInfo').style.display     = 'none';
  var cancelBtn = document.getElementById('launchCancelBtn');
  if (cancelBtn) cancelBtn.classList.remove('visible');
  document.querySelectorAll('input, button').forEach(function(el) { el.disabled = false; });
  updateLaunchButton();
}

// =============================================================================
//   RUN MONITORING - polling, run-info panel, log modal
// =============================================================================

var _pollTimer    = null;
var _logPollTimer = null;
var _logCursor    = 0;
var _logOpen      = false;
var _runStatus    = 'idle';

function startPolling() {
  stopPolling();
  pollRunStatus();
  _pollTimer = setInterval(pollRunStatus, 500);
}

function stopPolling() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

function startLogPoll() {
  stopLogPoll();
  fetchLogLines();
  _logPollTimer = setInterval(fetchLogLines, 500);
}

function stopLogPoll() {
  if (_logPollTimer) { clearInterval(_logPollTimer); _logPollTimer = null; }
}

function pollRunStatus() {
  fetch('/api/run_status')
    .then(function(r) { return r.json(); })
    .then(function(d) { handleRunStatus(d); })
    .catch(function() {});
}

function handleRunStatus(d) {
  if (_cancelInProgress) return;  // cancel animation owns the button — ignore poll responses
  _runStatus = d.status;

  // Previous-session detection (only when idle and not in a fresh run)
  if (d.status === 'idle' && !isRunning) {
    if (d.previous_session) showPrevSessionBanner(d.previous_session);
    return;
  }

  var btn     = document.getElementById('launchBtn');
  var ri      = document.getElementById('runInfo');
  var metrics = document.getElementById('runInfoMetrics');
  var elapsed = document.getElementById('runInfoElapsed');
  var badge   = document.getElementById('logStatusBadge');

  if (d.status === 'running') {
    btn.className = 'btn-launch running';
    btn.innerHTML = '<span class="pulse-dot"></span> RUNNING';
    ri.className  = 'run-info ri-running';
    if (badge) { badge.textContent = 'RUNNING'; badge.className = 'log-status-badge running'; }
  } else if (d.status === 'completed') {
    btn.className = 'btn-launch completed';
    btn.innerHTML = '&#10003; COMPLETED';
    ri.className  = 'run-info ri-completed';
    if (badge) { badge.textContent = 'DONE'; badge.className = 'log-status-badge completed'; }
    onRunFinished();
  } else if (d.status === 'crashed') {
    btn.className = 'btn-launch crashed';
    btn.innerHTML = '&#10007; CRASHED';
    ri.className  = 'run-info ri-crashed';
    if (badge) { badge.textContent = 'CRASHED'; badge.className = 'log-status-badge crashed'; }
    onRunFinished();
  }

  // Metrics line
  if (d.iteration > 0 || d.stiffness > 0) {
    var parts = [];
    if (d.iteration > 0) parts.push('Iteration ' + d.iteration);
    if (d.stiffness > 0) parts.push('Stiffness: ' + d.stiffness.toFixed(4));
    metrics.textContent = parts.join('   |   ');
  }

  // Elapsed time
  if (d.start_time) {
    var start = new Date(d.start_time);
    var end   = d.end_time ? new Date(d.end_time) : new Date();
    var sec   = Math.max(0, Math.floor((end - start) / 1000));
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = sec % 60;
    elapsed.textContent = (h ? h + 'h ' : '') + (m ? m + 'm ' : '') + s + 's';
  }
}

function onRunFinished() {
  if (isRunning) {
    isRunning = false;
    var cancelBtn = document.getElementById('launchCancelBtn');
    if (cancelBtn) cancelBtn.classList.remove('visible');
    document.querySelectorAll('input, button:not(.log-modal-close):not(.btn-banner):not(.btn-extend):not(.nav-tab)').forEach(function(el) {
      el.disabled = false;
    });
    updateLaunchButton();
  }
  // Flush final log lines then stop status polling (log poll stops when modal closes)
  setTimeout(stopPolling, 4000);
}

// ---- Log modal --------------------------------------------------------------

function openLogModal() {
  var modal = document.getElementById('logModal');
  modal.classList.add('open');
  _logOpen   = true;
  _logCursor = 0;
  document.getElementById('logLines').innerHTML = '';
  startLogPoll();
}

function closeLogModal() {
  document.getElementById('logModal').classList.remove('open');
  _logOpen = false;
  stopLogPoll();
}

function fetchLogLines() {
  fetch('/api/run_log', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({cursor: _logCursor})
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.lines && d.lines.length) {
      appendLogLines(d.lines);
      _logCursor = d.cursor;
    }
  })
  .catch(function() {});
}

function appendLogLines(lines) {
  var container = document.getElementById('logLines');
  var body      = document.getElementById('logBody');
  var atBottom  = body.scrollTop + body.clientHeight >= body.scrollHeight - 30;
  lines.forEach(function(line) {
    var div = document.createElement('div');
    div.className = 'log-line';
    var u = line.toUpperCase();
    if (/TRACEBACK|EXCEPTION|ABAQUSEXCEPTION|EXITED WITH ERROR/.test(u))    div.className += ' ll-error';
    else if (/WARNING|WARN/.test(u))                                         div.className += ' ll-warn';
    else if (/TERMINATED|COMPLETED|CONVERGED|DONE|SUCCESS/.test(u))         div.className += ' ll-success';
    else if (/={5,}/.test(line))                                             div.className += ' ll-section';
    div.textContent = line;
    container.appendChild(div);
  });
  if (atBottom) body.scrollTop = body.scrollHeight;
}

// ---- Previous-session banner ------------------------------------------------

function showPrevSessionBanner(prev) {
  var banner  = document.getElementById('prevSessionBanner');
  var statusEl = document.getElementById('prevSessionStatus');
  var metEl   = document.getElementById('prevSessionMetrics');
  if (prev.alive) {
    statusEl.textContent = 'A run from a previous session appears to still be running.';
    metEl.textContent    = 'Engine: ' + (prev.engine || 'unknown') + '   |   Started: ' + (prev.start || 'unknown');
    banner.style.display = 'flex';
  } else {
    statusEl.textContent = 'A run from a previous session ended while the launcher was offline.';
    metEl.textContent    = 'Engine: ' + (prev.engine || 'unknown') + '   |   Started: ' + (prev.start || 'unknown');
    banner.style.display = 'flex';
  }
}

function dismissPrevSessionBanner() {
  document.getElementById('prevSessionBanner').style.display = 'none';
}

// On page load: check whether a run is already in progress
document.addEventListener('DOMContentLoaded', function() {
  document.getElementById('targetVf').addEventListener('input', validateVF);
  document.getElementById('customWeights').addEventListener('input', validateWeights);
  // Check run state from server immediately
  pollRunStatus();
});

// =============================================================================
//   FOLDER BROWSER (shared modal for History + STL pages)
// =============================================================================
function loadFolderDrives() {
  fetch('/api/drives')
    .then(r => r.json())
    .then(data => {
      const bar = document.getElementById('folderDriveBar');
      if (data.drives && data.drives.length > 1) {
        bar.classList.add('visible');
        data.drives.forEach(d => {
          const btn = document.createElement('button');
          btn.className = 'btn-drive';
          btn.textContent = d.name;
          btn.onclick = () => folderBrowserNavigateTo(d.path);
          bar.appendChild(btn);
        });
      }
    }).catch(() => {});
}

function openFolderBrowser(target) {
  folderBrowserTarget = target;
  document.getElementById('folderBrowserModal').classList.add('open');
  folderBrowserNavigateTo(folderBrowserCurrentPath || '');
}

function closeFolderBrowser() {
  document.getElementById('folderBrowserModal').classList.remove('open');
}

function folderBrowserNavigateTo(path) {
  document.getElementById('folderFileList').innerHTML =
    '<div class="filelist-empty">Loading...</div>';

  fetch('/api/browse_folders', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: path || ''})
  })
  .then(r => r.json())
  .then(data => {
    folderBrowserCurrentPath = data.current_path;
    document.getElementById('folderToolbarPath').value = data.current_path;
    document.getElementById('folderBtnUp').disabled = data.at_root || !data.parent_path;
    renderFolderBreadcrumb(data.breadcrumbs || []);
    renderFolderList(data.entries || [], data.error);
    // Always allow selecting current folder
    document.getElementById('folderModalSelectedPath').textContent = data.current_path;
    document.getElementById('folderModalSelectedPath').className = 'modal-selected-path has-file';
  })
  .catch(() => {
    document.getElementById('folderFileList').innerHTML =
      '<div class="filelist-error">Error loading directory</div>';
  });
}

function renderFolderBreadcrumb(parts) {
  const bc = document.getElementById('folderBreadcrumb');
  bc.innerHTML = '';
  parts.forEach((p, i) => {
    const span = document.createElement('span');
    span.className = 'breadcrumb-part' + (i === parts.length - 1 ? ' last' : '');
    span.textContent = p.name;
    if (i < parts.length - 1) span.onclick = () => folderBrowserNavigateTo(p.path);
    bc.appendChild(span);
    if (i < parts.length - 1) {
      const sep = document.createElement('span');
      sep.className = 'breadcrumb-sep';
      sep.textContent = ' › ';
      bc.appendChild(sep);
    }
  });
}

function renderFolderList(entries, error) {
  const list = document.getElementById('folderFileList');
  list.innerHTML = '';
  if (error) { list.innerHTML = '<div class="filelist-error">⚠ ' + error + '</div>'; return; }
  if (entries.length === 0) {
    list.innerHTML = '<div class="filelist-empty">No subfolders or CSV files here</div>';
    return;
  }
  entries.forEach(entry => {
    const div = document.createElement('div');
    div.className = 'file-entry' + (entry.is_dir ? ' is-dir' : '');
    const icon = document.createElement('span');
    icon.className = 'file-icon';
    icon.textContent = entry.is_dir ? '📁' : '📄';
    const name = document.createElement('span');
    name.className = 'file-name';
    name.textContent = entry.name;
    div.appendChild(icon);
    div.appendChild(name);
    if (entry.is_dir) {
      div.onclick = div.ondblclick = () => folderBrowserNavigateTo(entry.full_path);
    }
    list.appendChild(div);
  });
}

function folderBrowserGoUp() {
  if (!folderBrowserCurrentPath) return;
  const parent = folderBrowserCurrentPath.replace(/[\\/][^\\/]+$/, '') || folderBrowserCurrentPath;
  folderBrowserNavigateTo(parent);
}

function confirmFolderSelection() {
  const path = folderBrowserCurrentPath;
  if (!path) return;
  closeFolderBrowser();
  if (folderBrowserTarget === 'history') {
    document.getElementById('historyFolderPath').value = path;
  } else if (folderBrowserTarget === 'stl') {
    document.getElementById('stlFolderPath').value = path;
    checkStlFolder(path);
  }
}

// =============================================================================
//   HISTORY PAGE
// =============================================================================
const CHART_COLORS = [
  '#6366f1','#4ade80','#f87171','#facc15','#a78bfa','#fb923c','#34d399','#60a5fa'
];

// Persistent runs store - survives page navigation
let loadedRuns  = [];
let colorIndex  = 0;

// Label modal state
let _pendingRuns       = [];
let _pendingFolderPath = '';
let _labelMode         = 'add';  // 'add' | 'rename'
let _renameIdx         = -1;

// Chart metric visibility
let _showStiffness = true;
let _showMassFrac  = false;
let _showSSvsMass  = false;

function addHistoryRun() {
  const path = document.getElementById('historyFolderPath').value.trim();
  if (!path) { alert('Select a folder first.'); return; }

  fetch('/api/scan_history', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: path})
  })
  .then(r => r.json())
  .then(data => {
    if (data.error || !data.runs || data.runs.length === 0) {
      alert('No recognized history CSV files found in that folder.');
      return;
    }
    showLabelModal(data.runs, path);
  })
  .catch(() => alert('Error scanning folder.'));
}

function showLabelModal(runs, folderPath) {
  _pendingRuns       = runs;
  _pendingFolderPath = folderPath;
  _labelMode         = 'add';

  document.getElementById('runLabelTitle').textContent =
    runs.length === 1 ? 'LABEL THIS RUN' : 'LABEL THESE RUNS';
  document.getElementById('runLabelConfirmBtn').textContent = 'Add';

  const rowsEl = document.getElementById('runLabelRows');
  rowsEl.innerHTML = '';
  runs.forEach(function(run, i) {
    const rowDiv = document.createElement('div');
    rowDiv.className = 'run-label-row';
    rowDiv.innerHTML =
      '<label>' + escHtml(run.file) + '</label>'
      + '<input type="text" id="runLabelInput_' + i + '" value="' + escHtml(run.default_label) + '" />'
      + '<div class="run-label-error" id="runLabelErr_' + i + '"></div>';
    rowsEl.appendChild(rowDiv);
  });

  document.getElementById('runLabelModal').classList.add('open');
  const first = document.getElementById('runLabelInput_0');
  if (first) { first.focus(); first.select(); }
}

function confirmLabelModal() {
  if (_labelMode === 'rename') {
    const input  = document.getElementById('runLabelInput_0');
    const errEl  = document.getElementById('runLabelErr_0');
    const newLbl = input.value.trim();
    input.classList.remove('error');
    errEl.classList.remove('visible');
    if (!newLbl) {
      input.classList.add('error');
      errEl.textContent = 'Label cannot be empty.';
      errEl.classList.add('visible');
      return;
    }
    const oldLbl = loadedRuns[_renameIdx] ? loadedRuns[_renameIdx].label : '';
    if (newLbl !== oldLbl && loadedRuns.find(function(r) { return r.label === newLbl; })) {
      input.classList.add('error');
      errEl.textContent = 'Name already in use - choose another.';
      errEl.classList.add('visible');
      return;
    }
    if (loadedRuns[_renameIdx]) loadedRuns[_renameIdx].label = newLbl;
    cancelLabelModal();
    renderHistoryChart();
    return;
  }

  // Add mode
  var newLabels = [];
  var hasError  = false;

  _pendingRuns.forEach(function(run, i) {
    var input = document.getElementById('runLabelInput_' + i);
    input.classList.remove('error');
    document.getElementById('runLabelErr_' + i).classList.remove('visible');
    newLabels.push(input ? input.value.trim() : run.default_label);
  });

  // Empty check
  newLabels.forEach(function(lbl, i) {
    if (!lbl) {
      document.getElementById('runLabelInput_' + i).classList.add('error');
      var e = document.getElementById('runLabelErr_' + i);
      e.textContent = 'Label cannot be empty.';
      e.classList.add('visible');
      hasError = true;
    }
  });

  // Intra-batch duplicate check
  newLabels.forEach(function(lbl, i) {
    if (lbl && newLabels.indexOf(lbl) !== i) {
      document.getElementById('runLabelInput_' + i).classList.add('error');
      var e = document.getElementById('runLabelErr_' + i);
      e.textContent = 'Duplicate label in this batch.';
      e.classList.add('visible');
      hasError = true;
    }
  });

  // Conflict with already loaded runs
  newLabels.forEach(function(lbl, i) {
    if (lbl && loadedRuns.find(function(r) { return r.label === lbl; })) {
      document.getElementById('runLabelInput_' + i).classList.add('error');
      var e = document.getElementById('runLabelErr_' + i);
      e.textContent = 'Name already in use - choose another.';
      e.classList.add('visible');
      hasError = true;
    }
  });

  if (hasError) return;

  // Commit runs
  _pendingRuns.forEach(function(run, i) {
    run.label       = newLabels[i];
    run.source_path = _pendingFolderPath;
    run.color       = CHART_COLORS[colorIndex % CHART_COLORS.length];
    colorIndex++;
    loadedRuns.push(run);
  });

  cancelLabelModal();
  renderHistoryChart();
}

function cancelLabelModal() {
  document.getElementById('runLabelModal').classList.remove('open');
  _pendingRuns = [];
}

function renameRunByIdx(idx) {
  if (idx < 0 || idx >= loadedRuns.length) return;
  _labelMode = 'rename';
  _renameIdx = idx;

  document.getElementById('runLabelTitle').textContent = 'RENAME RUN';
  document.getElementById('runLabelConfirmBtn').textContent = 'Rename';

  const rowsEl = document.getElementById('runLabelRows');
  rowsEl.innerHTML = '<div class="run-label-row">'
    + '<label>New name</label>'
    + '<input type="text" id="runLabelInput_0" value="' + escHtml(loadedRuns[idx].label) + '" />'
    + '<div class="run-label-error" id="runLabelErr_0"></div>'
    + '</div>';

  document.getElementById('runLabelModal').classList.add('open');
  const inp = document.getElementById('runLabelInput_0');
  if (inp) { inp.focus(); inp.select(); }
}

function showRunInfoByIdx(idx) {
  if (idx < 0 || idx >= loadedRuns.length) return;
  const run = loadedRuns[idx];
  document.getElementById('runInfoLabel').textContent = run.label;
  document.getElementById('runInfoPath').textContent  = run.source_path || 'Path not available';
  document.getElementById('runInfoModal').classList.add('open');
}

function closeRunInfo() {
  document.getElementById('runInfoModal').classList.remove('open');
}

function removeRunByIdx(idx) {
  loadedRuns.splice(idx, 1);
  renderHistoryChart();
}

// --- Metric toggle ---
function toggleMetric(metric) {
  if (metric === 'ssvsmass') {
    if (_showSSvsMass) { shakeChip('chipSSvsMass'); return; }
    _showSSvsMass = true;
    renderHistoryChart();
    return;
  }

  if (_showSSvsMass) {
    // Clicking a muted chip: switch back to time-series with that metric on
    _showSSvsMass = false;
    if (metric === 'stiffness') { _showStiffness = true; }
    else                        { _showMassFrac  = true; }
    renderHistoryChart();
    return;
  }

  // Normal time-series toggles
  if (metric === 'stiffness') {
    if (_showStiffness && !_showMassFrac) { shakeChip('chipStiffness'); return; }
    _showStiffness = !_showStiffness;
  } else {
    if (_showMassFrac && !_showStiffness) { shakeChip('chipMassFrac'); return; }
    _showMassFrac = !_showMassFrac;
  }
  updateChartVisibility();
}

function shakeChip(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove('shaking');
  void el.offsetWidth; // force reflow to restart animation
  el.classList.add('shaking');
  el.addEventListener('animationend', function() { el.classList.remove('shaking'); }, {once: true});
}

function updateChartVisibility() {
  if (!historyChart) return;

  const chipS = document.getElementById('chipStiffness');
  const chipM = document.getElementById('chipMassFrac');
  const chipX = document.getElementById('chipSSvsMass');
  if (chipS) chipS.className = 'metric-chip' + (_showStiffness ? ' active' : '');
  if (chipM) chipM.className = 'metric-chip' + (_showMassFrac  ? ' active' : '');
  if (chipX) chipX.className = 'metric-chip';

  historyChart.data.datasets.forEach(function(ds, i) {
    historyChart.setDatasetVisibility(i, ds._isMassFrac ? _showMassFrac : _showStiffness);
  });
  historyChart.options.scales.y.display  = _showStiffness;
  historyChart.options.scales.y2.display = _showMassFrac;
  historyChart.update();
}

function renderHistoryChart() {
  const container = document.getElementById('historyContent');

  if (loadedRuns.length === 0) {
    if (historyChart) { historyChart.destroy(); historyChart = null; }
    container.innerHTML = '<div class="history-empty">Browse to a results folder and click Add Run to begin.</div>';
    return;
  }

  // Badges
  let summaryHTML = '<div class="run-summary">';
  loadedRuns.forEach(function(run, idx) {
    summaryHTML += '<div class="run-badge">'
      + '<div class="run-badge-dot" style="background:' + run.color + '"></div>'
      + '<span>' + escHtml(run.label) + '</span>'
      + '<span style="color:var(--text3)">Best: <strong style="color:var(--text)">'
      + run.best_stiffness.toFixed(1) + '</strong> @ iter ' + run.best_iteration + '</span>'
      + '<span class="run-badge-icon pencil" title="Rename" onclick="renameRunByIdx(' + idx + ')">&#9998;</span>'
      + '<span class="run-badge-icon info-btn" title="Source folder" onclick="showRunInfoByIdx(' + idx + ')">&#9432;</span>'
      + '<span class="remove-run" onclick="removeRunByIdx(' + idx + ')">&#10005;</span>'
      + '</div>';
  });
  summaryHTML += '</div>';

  // Chip classes
  var chipSClass, chipMClass, chipXClass;
  if (_showSSvsMass) {
    chipSClass = 'metric-chip muted';
    chipMClass = 'metric-chip muted';
    chipXClass = 'metric-chip active';
  } else {
    chipSClass = 'metric-chip' + (_showStiffness ? ' active' : '');
    chipMClass = 'metric-chip' + (_showMassFrac  ? ' active' : '');
    chipXClass = 'metric-chip';
  }

  const chartPanelHTML = '<div class="chart-panel">'
    + '<div class="chart-toolbar"><div class="metric-chips">'
    + '<span class="' + chipSClass + '" id="chipStiffness"  onclick="toggleMetric(\'stiffness\')">Specific Stiffness</span>'
    + '<span class="' + chipMClass + '" id="chipMassFrac"   onclick="toggleMetric(\'massfrac\')">Mass Fraction</span>'
    + '<span class="' + chipXClass + '" id="chipSSvsMass"   onclick="toggleMetric(\'ssvsmass\')">S.S. vs Mass</span>'
    + '</div></div>'
    + '<div class="chart-wrapper"><canvas id="stiffnessChart"></canvas></div>'
    + '</div>';

  container.innerHTML = summaryHTML + chartPanelHTML;

  if (historyChart) { historyChart.destroy(); historyChart = null; }

  if (_showSSvsMass) {
    buildSSvsMassChart();
  } else {
    buildTimeSeriesChart();
  }
}

function buildTimeSeriesChart() {
  const datasets = [];
  loadedRuns.forEach(function(run) {
    datasets.push({
      label:           run.label,
      _isMassFrac:     false,
      yAxisID:         'y',
      data:            run.rows.map(function(r) { return {x: r.iteration, y: r.specific_stiffness}; }),
      borderColor:     run.color,
      backgroundColor: run.color + '22',
      borderWidth:     2,
      pointRadius:     3,
      pointHoverRadius: 5,
      tension:         0.3,
      fill:            false,
      hidden:          !_showStiffness
    });
    datasets.push({
      label:                    run.label + ' (mass %)',
      _isMassFrac:              true,
      yAxisID:                  'y2',
      data:                     run.rows.map(function(r) { return {x: r.iteration, y: r.volume_fraction * 100}; }),
      borderColor:              hexToRgba(run.color, 0.4),
      backgroundColor:          'transparent',
      borderWidth:              1,
      borderDash:               [6, 4],
      pointRadius:              0,
      pointHoverRadius:         4,
      pointHoverBackgroundColor: hexToRgba(run.color, 0.7),
      tension:                  0.3,
      fill:                     false,
      hidden:                   !_showMassFrac
    });
  });

  const ctx = document.getElementById('stiffnessChart').getContext('2d');
  historyChart = new Chart(ctx, {
    type: 'line',
    data: { datasets: datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: {
          type:  'linear',
          title: { display: true, text: 'Iteration', color: '#55556a' },
          grid:  { color: '#1e1e2a' },
          ticks: { color: '#55556a' }
        },
        y: {
          display: _showStiffness,
          title:   { display: true, text: 'Specific Stiffness (N/mm)/%', color: '#55556a' },
          grid:    { color: '#1e1e2a' },
          ticks:   { color: '#55556a' }
        },
        y2: {
          display:  _showMassFrac,
          position: 'right',
          title:    { display: true, text: 'Mass Fraction (%)', color: '#55556a' },
          grid:     { drawOnChartArea: false },
          ticks:    { color: '#55556a' },
          min: 0,
          max: 100
        }
      },
      plugins: {
        legend: {
          labels: {
            color: '#8888a0',
            font:  { family: 'DM Sans' },
            filter: function(legendItem, chartData) {
              return !chartData.datasets[legendItem.datasetIndex]._isMassFrac;
            }
          }
        },
        tooltip: {
          backgroundColor: '#111118', borderColor: '#2a2a3a', borderWidth: 1,
          titleColor: '#e8e8f0', bodyColor: '#8888a0',
          callbacks: {
            label: function(context) {
              if (context.dataset._isMassFrac) {
                return context.dataset.label.replace(' (mass %)', '') + ' mass: ' + context.parsed.y.toFixed(1) + '%';
              }
              return context.dataset.label + ': ' + context.parsed.y.toFixed(1);
            }
          }
        }
      }
    }
  });
}

function buildSSvsMassChart() {
  const datasets = loadedRuns.map(function(run) {
    return {
      label:           run.label,
      data:            run.rows.map(function(r) {
                         return {x: r.volume_fraction * 100, y: r.specific_stiffness, iteration: r.iteration};
                       }),
      borderColor:     run.color,
      backgroundColor: run.color + '22',
      borderWidth:     2,
      pointRadius:     3,
      pointHoverRadius: 5,
      tension:         0.3,
      fill:            false,
      showLine:        true
    };
  });

  const ctx = document.getElementById('stiffnessChart').getContext('2d');
  historyChart = new Chart(ctx, {
    type: 'scatter',
    data: { datasets: datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: {
          type:    'linear',
          reverse: true,
          min:     0,
          max:     100,
          title:   { display: true, text: 'Mass Fraction (%)', color: '#55556a' },
          grid:    { color: '#1e1e2a' },
          ticks:   { color: '#55556a' }
        },
        y: {
          title: { display: true, text: 'Specific Stiffness (N/mm)/%', color: '#55556a' },
          grid:  { color: '#1e1e2a' },
          ticks: { color: '#55556a' }
        }
      },
      plugins: {
        legend: { labels: { color: '#8888a0', font: { family: 'DM Sans' } } },
        tooltip: {
          backgroundColor: '#111118', borderColor: '#2a2a3a', borderWidth: 1,
          titleColor: '#e8e8f0', bodyColor: '#8888a0',
          callbacks: {
            title: function(items) {
              if (!items.length) return '';
              const raw = items[0].raw;
              return raw.iteration !== undefined ? 'Iteration ' + raw.iteration : '';
            },
            label: function(context) {
              return context.dataset.label
                + ' - ' + context.parsed.y.toFixed(1)
                + ' @ ' + context.parsed.x.toFixed(1) + '% mass';
            }
          }
        }
      }
    }
  });
}




// =============================================================================
//   STL PAGE
// =============================================================================

// --- State ---
var _stlEngine        = 'solid_void';  // current engine selection
var _preflightData    = null;          // last preflight JSON from server
var _stlPollInterval  = null;
var _stlLogInterval   = null;
var _stlLogCursor     = 0;
var _stlLogOpen       = false;
var _autopopPath      = '';

// --- Engine selector ---
function switchStlEngine(engine) {
  _stlEngine = engine;
  document.getElementById('stlSVFiles').style.display    = engine === 'solid_void' ? '' : 'none';
  document.getElementById('stlLatFiles').style.display   = engine === 'lattice'    ? '' : 'none';
  document.getElementById('stlSVOptions').style.display  = engine === 'solid_void' ? '' : 'none';
  document.getElementById('stlLatOptions').style.display = engine === 'lattice'    ? '' : 'none';
  if (engine === 'lattice' && window.initLatticePreview && !window._latticePreviewInited) {
    window._latticePreviewInited = true;
    setTimeout(window.initLatticePreview, 80);
  }
  _validateGenerateBtn();
  updateOutputPreview();
}

// --- Folder path typed (debounced) ---
function onStlPathTyped() {
  clearTimeout(window._stlTimer);
  window._stlTimer = setTimeout(() => {
    const p = document.getElementById('stlFolderPath').value.trim();
    if (p.length > 2) checkStlFolder(p);
  }, 600);
}

// --- Folder scan ---
// --- Lattice Gibson-Ashby table (mirror of LATTICE_PROPERTIES in the optimiser) ---
const LATTICE_PROPERTIES = {
  gyroid_ligament: {C1:0.9515, n1:2.174, C2:0.6182, n2:1.746},
  gyroid_sheet:    {C1:0.5909, n1:1.3454, C2:0.7298, n2:1.2066},
  primitive:       {C1:0.61, n1:1.57, C2:0.794, n2:1.36},
  diamond:         {C1:0.6438, n1:2.026, C2:0.6802, n2:1.614},
  iwp:             {C1:0.699, n1:1.217, C2:0.738, n2:1.151},
  neovius:         {C1:0.705, n1:1.236, C2:0.721, n2:1.5},
};
function onLatticeTypeChange() {
  const r = document.querySelector('input[name="latticeType"]:checked');
  if (!r) return;
  const p = LATTICE_PROPERTIES[r.value];
  if (!p) return;
  const set = function(id, v){ const el = document.getElementById(id); if (el) el.value = v; };
  set('stiffnessCoeff', p.C1.toFixed(4));
  set('gibsonAshbyExp', p.n1.toFixed(4));
  set('yieldCoeff',     p.C2.toFixed(4));
  set('yieldExponent',  p.n2.toFixed(4));
}

// --- STL lattice override (in-app modal, matches the stale-files warning) ---
let _stlDetectedLattice = null;
function onStlLatticeChange() {
  const sel = document.getElementById('stlLatticeType');
  if (_stlDetectedLattice && sel.value !== _stlDetectedLattice) {
    const txt = document.getElementById('latOverrideModalText');
    if (txt) {
      txt.innerHTML =
        "The density map was optimised for <strong>'" + _stlDetectedLattice + "'</strong>. " +
        "Rendering <strong>'" + sel.value + "'</strong> instead uses different Gibson-Ashby " +
        "stiffness (C1, n1) and yield (C2, n2) relationships, so the optimised density field no " +
        "longer maps to the mechanical response it was sized for. This invalidates the optimiser result.";
    }
    const m = document.getElementById('latOverrideModal');
    if (m) { m.classList.add('open'); return; }
  }
  _applyStlLattice();
}
function _applyStlLattice() {
  updateOutputPreview(); updateLatticePreview(); triggerLatticePreflight();
}
function latOverrideProceed() {
  const m = document.getElementById('latOverrideModal'); if (m) m.classList.remove('open');
  _applyStlLattice();
}
function latOverrideRevert() {
  const m = document.getElementById('latOverrideModal'); if (m) m.classList.remove('open');
  const sel = document.getElementById('stlLatticeType');
  if (sel && _stlDetectedLattice) sel.value = _stlDetectedLattice;
  _applyStlLattice();
}

function checkStlFolder(path) {
  fetch('/api/scan_stl', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: path})
  })
  .then(r => r.json())
  .then(data => {
    // Auto-switch engine if detected
    if (data.engine && data.engine !== _stlEngine) {
      const radioId = data.engine === 'lattice' ? 'stlEngLat' : 'stlEngSV';
      document.getElementById(radioId).checked = true;
      switchStlEngine(data.engine);
    }
    if (_stlEngine === 'solid_void') {
      updateCsvStatus('csv-nodes',    data.nodes);
      updateCsvStatus('csv-elements', data.elements);
      updateCsvStatus('csv-solid',    data.solid);
    } else {
      updateCsvStatus('csv-density-map', data.density_map);
      try {
        if (data.detected_lattice_type) {
          _stlDetectedLattice = data.detected_lattice_type;
          const _sel = document.getElementById('stlLatticeType');
          if (_sel && Array.prototype.some.call(_sel.options, function(o){ return o.value === _stlDetectedLattice; })) {
            _sel.value = _stlDetectedLattice;
          }
        }
      } catch (e) {}
      if (data.density_map) triggerLatticePreflight();
    }
    // Auto-fill output path
    const outputEl = document.getElementById('stlOutputPath');
    if (data.valid && !outputEl.value) {
      const base = path.replace(/[\\/]$/, '');
      outputEl.value = base + '/optimized_structure.stl';
      updateOutputPreview();
    }
    _validateGenerateBtn();
  })
  .catch(() => {
    ['csv-nodes','csv-elements','csv-solid','csv-density-map'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.className = 'csv-status-item missing';
    });
    document.getElementById('stlGenerateBtn').disabled = true;
  });
}

function updateCsvStatus(id, found) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = 'csv-status-item ' + (found ? 'found' : 'missing');
}

// --- Lattice pre-flight ---
function triggerLatticePreflight() {
  const folder = document.getElementById('stlFolderPath').value.trim();
  if (!folder) return;
  const csv    = folder.replace(/[\\/]$/, '') + '/Optimized_Density_Map.csv';
  const period = parseFloat(document.getElementById('stlPeriod').value) || 2.0;
  const subgrid= parseInt(document.getElementById('stlSubgrid').value)  || 6;
  const lattice= document.getElementById('stlLatticeType').value;
  const decim  = parseFloat(document.getElementById('stlDecimation').value) || 0.90;
  const decimMode = document.getElementById('stlDecimationMode').value;

  fetch('/api/preflight_lattice_stl', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({csv, lattice_type: lattice, period, subgrid, decimation: decim, decimation_mode: decimMode})
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) return;
    _preflightData = data;
    renderPreflightPanel(data);
    renderWarnings(data);
  })
  .catch(() => {});
}

function renderPreflightPanel(d) {
  document.getElementById('preflightPanel').classList.add('visible');

  // Elements
  document.getElementById('pfElemVal').textContent =
    d.elements.total.toLocaleString() +
    '  (Void: ' + d.elements.void +
    '  Solid: ' + d.elements.solid +
    '  Lattice: ' + d.elements.lattice + ')';

  // Grid
  document.getElementById('pfGridVal').textContent =
    d.grid.nx + ' \xd7 ' + d.grid.ny + ' \xd7 ' + d.grid.nz +
    '  (dx = ' + d.grid.dx.toFixed(2) + ' mm)';

  // Min feature size
  document.getElementById('pfFeatureVal').textContent =
    d.feature.predicted_size_mm.toFixed(3) + ' mm  (' + d.feature.name + ')';
  _setBadge('pfFeatureBadge', d.feature.status);

  // Subgrid
  const sgCurrent = document.getElementById('stlSubgrid').value;
  document.getElementById('pfSubgridVal').textContent =
    'current = ' + sgCurrent + '  \u2192  recommended \u2265 ' + d.subgrid.recommended;
  _setBadge('pfSubgridBadge', d.subgrid.status);

  // RAM + Decimation
  _renderRamDecimRows(d);

  // Stagger-animate each row
  const rows = document.querySelectorAll('#preflightPanel .preflight-row');
  rows.forEach((row, i) => {
    row.classList.remove('pf-row-animate');
    void row.offsetWidth;
    row.style.animationDelay = (i * 45) + 'ms';
    row.classList.add('pf-row-animate');
  });

  // Update subgrid icon now that preflight data is available
  updateSubgridIcon();
}

function _renderRamDecimRows(d) {
  // RAM - line 1
  const chunks  = d.ram.num_chunks;
  const peakGb  = d.ram.peak_per_chunk_gb.toFixed(2);
  const availGb = d.ram.available_gb.toFixed(2);
  const fieldGb = d.ram.total_field_gb.toFixed(2);
  document.getElementById('pfRamVal').textContent =
    'Full field: ' + fieldGb + ' GB  \u00b7  ' +
    chunks + ' chunk' + (chunks > 1 ? 's' : '') +
    '  \u00b7  Peak ' + peakGb + ' GB/chunk  \u00b7  ' + availGb + ' GB free';

  // RAM - line 2
  const nf = d.ram.n_chunks_field;
  const nd = d.ram.n_chunks_decim;
  document.getElementById('pfRamSub').textContent =
    '(field needs \u2265' + nf + ' chunk' + (nf > 1 ? 's' : '') +
    '; per-chunk decimation needs \u2265' + nd + ' - using ' + chunks + ')';
  _setBadge('pfRamBadge', d.ram.status);

  // Decimation - format raw triangle count compactly
  const rawTri = d.decimation.est_raw_triangles || 0;
  const rawStr = rawTri >= 1e6
    ? '~' + (rawTri / 1e6).toFixed(1) + 'M'
    : '~' + rawTri.toLocaleString();

  if (d.decimation.use_two_pass) {
    document.getElementById('pfDecimVal').textContent =
      'Two-pass  \u00b7  ' + rawStr + ' raw triangles' +
      '  \u00b7  Global would need ' + d.decimation.est_global_ram_gb.toFixed(2) + ' GB (OOM)';
    const p1Pct = Math.round(d.decimation.pc_reduction * 100);
    const p2Pct = Math.round(d.decimation.fg_reduction * 100);
    document.getElementById('pfDecimSub').textContent =
      'Phase 1: \u2212' + p1Pct + '% per chunk  \u2192  ' +
      'Phase 2: \u2212' + p2Pct + '% global  \u00b7  ' +
      'Assembled after P1: ' + d.decimation.est_assembled_gb.toFixed(2) + ' GB';
  } else {
    const keptPct = Math.round((1 - (d.decimation.fg_reduction || d.decimation.pc_reduction)) * 100);
    document.getElementById('pfDecimVal').textContent =
      'Global pass  \u00b7  ' + rawStr + ' raw triangles' +
      '  \u00b7  Est. ' + d.decimation.est_global_ram_gb.toFixed(2) + ' GB RAM needed';
    document.getElementById('pfDecimSub').textContent =
      'Keeps ~' + keptPct + '% of raw triangles after decimation';
  }
}

// Called whenever subgrid changes (toggle or manual edit) - re-fetches
// only the RAM + Decimation data without disturbing the warning states.
function _refreshRamDecim() {
  const folder = document.getElementById('stlFolderPath').value.trim();
  if (!folder || !_preflightData) return;
  const csv      = folder.replace(/[\\/]$/, '') + '/Optimized_Density_Map.csv';
  const period   = parseFloat(document.getElementById('stlPeriod').value)   || 2.0;
  const subgrid  = parseInt(document.getElementById('stlSubgrid').value)     || 6;
  const lattice  = document.getElementById('stlLatticeType').value;
  const decim    = parseFloat(document.getElementById('stlDecimation').value)|| 0.90;
  const decimMode= document.getElementById('stlDecimationMode').value;

  fetch('/api/preflight_lattice_stl', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({csv, lattice_type: lattice, period, subgrid,
                          decimation: decim, decimation_mode: decimMode})
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) return;
    // Merge RAM + Decimation into the cached preflight data
    if (_preflightData) {
      _preflightData.ram        = data.ram;
      _preflightData.decimation = data.decimation;
    }
    _renderRamDecimRows(data);
  })
  .catch(() => {});
}

// Debounced handler wired to the Subgrid number field's oninput
function onSubgridInput() {
  clearTimeout(window._subgridRefreshTimer);
  window._subgridRefreshTimer = setTimeout(() => {
    if (_preflightData) {
      // Update preflight panel subgrid row with new current value
      const current = parseInt(document.getElementById('stlSubgrid').value) || 6;
      const rec     = _preflightData.subgrid.recommended;
      const sgStatus = current >= rec ? 'pass'
        : current >= rec / 2 ? 'warning' : 'critical';
      document.getElementById('pfSubgridVal').textContent =
        'current = ' + current + '  \u2192  recommended \u2265 ' + rec;
      _setBadge('pfSubgridBadge', sgStatus);
      // Re-render the subgrid warning block (may show/hide depending on new value)
      _renderSubgridWarning(_preflightData);
    }
    updateSubgridIcon();
    _refreshRamDecim();
  }, 700);
}

function onPeriodInput() {
  clearTimeout(window._periodTimer);
  window._periodTimer = setTimeout(() => {
    triggerLatticePreflight();
    if (window._latticeBuildLattice) {
      const sel = document.getElementById('stlLatticeType');
      if (sel) window._latticeBuildLattice(sel.value);
    }
  }, 450);
}

function updateSubgridIcon() {
  const icon = document.getElementById('subgridStatusIcon');
  if (!icon) return;
  if (!_preflightData) { icon.textContent = ''; return; }
  const current = parseInt(document.getElementById('stlSubgrid').value) || 6;
  // Use the same floor-adjusted recommendation as the panel row
  const floorOn = document.querySelector('input[name="stlDensityFloor"]:checked') &&
                  document.querySelector('input[name="stlDensityFloor"]:checked').value === 'on';
  const rec = (floorOn && _preflightData.feature.density_floor_needed)
    ? Math.max(4, Math.ceil((2.5 * _preflightData.grid.dx) / _preflightData.feature.safe_size_mm))
    : _preflightData.subgrid.recommended;
  if (current >= rec) {
    icon.textContent = '\u2713';
    icon.style.color = 'var(--green)';
    icon.dataset.tip = 'Subgrid meets recommendation (\u2265 ' + rec + ')';
  } else {
    icon.textContent = '\u26a0';
    icon.style.color = '#fabd00';
    icon.dataset.tip = 'Minimum recommended: ' + rec;
  }
}

function _setBadge(id, status) {
  const el = document.getElementById(id);
  if (!el) return;
  const map = {pass:'pf-pass', warning:'pf-warning', critical:'pf-critical', overkill:'pf-overkill'};
  el.className = 'pf-badge ' + (map[status] || 'pf-pass');
  el.textContent = status.toUpperCase();
}

function renderWarnings(d) {
  // Warning 1 - density floor
  const wFloor = document.getElementById('warnDensityFloor');
  if (d.feature.density_floor_needed) {
    const body = document.getElementById('warnDensityFloorBody');
    const sev  = d.feature.status === 'critical' ? 'critical' : '';
    wFloor.className = 'stl-warning-block visible ' + sev;
    body.textContent =
      'Predicted ' + d.feature.name + ' is ' + d.feature.predicted_size_mm.toFixed(3) +
      ' mm. The absolute SLM minimum is 0.15 mm; 0.25 mm is recommended for structural performance. ' +
      'Raising the density floor to ' + (d.feature.recommended_density_floor * 100).toFixed(1) +
      '% would bring the minimum feature to the recommended target.';
    document.getElementById('stlFloorOff').checked = true;
  } else {
    wFloor.className = 'stl-warning-block';
  }
  // Warning 2 - subgrid (render from preflight data, dynamic)
  _renderSubgridWarning(d);
}

function _renderSubgridWarning(d) {
  const floorOn = document.querySelector('input[name="stlDensityFloor"]:checked') &&
                  document.querySelector('input[name="stlDensityFloor"]:checked').value === 'on';
  let recSubgrid = d.subgrid.recommended;
  let sgStatus   = d.subgrid.status;

  if (floorOn && d.feature.density_floor_needed) {
    const effectiveSize = d.feature.safe_size_mm;
    recSubgrid = Math.max(4, Math.ceil((2.5 * d.grid.dx) / effectiveSize));
    const current = parseInt(document.getElementById('stlSubgrid').value) || 6;
    if (current < recSubgrid)       sgStatus = current >= recSubgrid / 2 ? 'warning' : 'critical';
    else if (current > recSubgrid)  sgStatus = 'overkill';
    else                            sgStatus = 'pass';
  }

  const wSG     = document.getElementById('warnSubgrid');
  const current = parseInt(document.getElementById('stlSubgrid').value) || 6;

  if (sgStatus === 'warning' || sgStatus === 'critical') {
    wSG.className = 'stl-warning-block visible ' + (sgStatus === 'critical' ? 'critical' : '');
    document.getElementById('warnSubgridBody').textContent =
      'Current SUBGRID = ' + current + ' is insufficient for this geometry. ' +
      'A value of at least ' + recSubgrid + ' is needed to correctly capture features of this size.';
    wSG.dataset.recommended = recSubgrid;
    document.getElementById('stlSubgridFixOff').checked = true;
  } else if (sgStatus === 'overkill') {
    wSG.className = 'stl-warning-block visible';
    document.getElementById('warnSubgridBody').textContent =
      'Current SUBGRID = ' + current + ' is higher than needed (optimal = ' + recSubgrid + '). ' +
      'This uses more RAM and time with no geometric benefit.';
    wSG.dataset.recommended = recSubgrid;
    document.getElementById('stlSubgridFixOff').checked = true;
  } else {
    wSG.className = 'stl-warning-block';
  }
}

function toggleWarning(id, btn) {
  const block     = document.getElementById(id);
  const collapsed = block.classList.toggle('collapsed');
  btn.textContent = collapsed ? '\u25bc' : '\u2713';
  btn.title       = collapsed ? 'Expand' : 'Minimise';
}

// --- Warning toggles ---
function onFloorToggle() {
  if (!_preflightData) return;
  const on     = document.querySelector('input[name="stlDensityFloor"]:checked').value === 'on';
  const wFloor = document.getElementById('warnDensityFloor');
  if (on) {
    wFloor.classList.add('resolved');
    wFloor.classList.remove('critical');
    // Update feature size row
    const safe = _preflightData.feature.safe_size_mm;
    document.getElementById('pfFeatureVal').textContent =
      safe.toFixed(3) + ' mm  (' + _preflightData.feature.name + ')  - floor applied';
    _setBadge('pfFeatureBadge', 'pass');
  } else {
    wFloor.classList.remove('resolved');
    if (_preflightData.feature.status === 'critical') wFloor.classList.add('critical');
    document.getElementById('pfFeatureVal').textContent =
      _preflightData.feature.predicted_size_mm.toFixed(3) +
      ' mm  (' + _preflightData.feature.name + ')';
    _setBadge('pfFeatureBadge', _preflightData.feature.status);
  }

  // Recalculate effective recommended subgrid based on new floor state
  const currentSG = parseInt(document.getElementById('stlSubgrid').value) || 6;
  const newRec = (on && _preflightData.feature.density_floor_needed)
    ? Math.max(4, Math.ceil((2.5 * _preflightData.grid.dx) / _preflightData.feature.safe_size_mm))
    : _preflightData.subgrid.recommended;
  const sgStatus = currentSG >= newRec ? 'pass'
    : currentSG >= newRec / 2 ? 'warning' : 'critical';
  document.getElementById('pfSubgridVal').textContent =
    'current = ' + currentSG + '  \u2192  recommended \u2265 ' + newRec;
  _setBadge('pfSubgridBadge', sgStatus);
  updateSubgridIcon();

  _renderSubgridWarning(_preflightData);
  _refreshRamDecim();
}

function onSubgridFixToggle() {
  const on  = document.querySelector('input[name="stlSubgridFix"]:checked').value === 'on';
  const wSG = document.getElementById('warnSubgrid');
  const rec = parseInt(wSG.dataset.recommended) || 0;

  if (on && rec > 0) {
    document.getElementById('stlSubgrid').value = rec;
    wSG.classList.add('resolved');
    wSG.classList.remove('critical');
    document.getElementById('pfSubgridVal').textContent =
      'current = ' + rec + '  \u2192  recommended \u2265 ' + rec + '  (updated)';
    _setBadge('pfSubgridBadge', 'pass');
    _refreshRamDecim();
    updateSubgridIcon();
  } else if (!on && _preflightData) {
    document.getElementById('stlSubgrid').value = _preflightData.subgrid.current;
    wSG.classList.remove('resolved');
    _renderSubgridWarning(_preflightData);
    document.getElementById('pfSubgridVal').textContent =
      'current = ' + _preflightData.subgrid.current +
      '  \u2192  recommended \u2265 ' + _preflightData.subgrid.recommended;
    _setBadge('pfSubgridBadge', _preflightData.subgrid.status);
    updateSubgridIcon();
    _refreshRamDecim(); // Revert RAM/Decimation to original subgrid values
  }
}

// --- Output path live preview ---
function updateOutputPreview() {
  const outputEl  = document.getElementById('stlOutputPath');
  const previewEl = document.getElementById('stlOutputPreview');
  if (_stlEngine !== 'lattice') {
    previewEl.textContent = '';
    return;
  }
  const raw     = outputEl.value.trim();
  const lattice = document.getElementById('stlLatticeType').value;
  if (!raw) { previewEl.textContent = ''; return; }
  const dot   = raw.lastIndexOf('.');
  const base  = dot >= 0 ? raw.slice(0, dot) : raw;
  const ext   = dot >= 0 ? raw.slice(dot)    : '.stl';
  previewEl.textContent = 'Actual output: ' + base + '_' + lattice + ext;
}

// --- Smooth toggle (solid-void) ---
function toggleSmoothOptions() {
  const on = document.querySelector('input[name="stlSmooth"]:checked').value === 'on';
  document.getElementById('smoothIterField').style.display = on ? 'block' : 'none';
}

// --- Generate button validation ---
function _validateGenerateBtn() {
  const btn = document.getElementById('stlGenerateBtn');
  if (btn.disabled && btn.classList.contains('running')) return;  // already running
  let ready = false;
  if (_stlEngine === 'solid_void') {
    ready = document.getElementById('csv-nodes')    && document.getElementById('csv-nodes').classList.contains('found') &&
            document.getElementById('csv-elements') && document.getElementById('csv-elements').classList.contains('found') &&
            document.getElementById('csv-solid')    && document.getElementById('csv-solid').classList.contains('found');
  } else {
    ready = document.getElementById('csv-density-map') && document.getElementById('csv-density-map').classList.contains('found');
  }
  btn.disabled = !ready;
}

// --- Generate STL ---
function generateSTL() {
  const folder = document.getElementById('stlFolderPath').value.trim();
  const output = document.getElementById('stlOutputPath').value.trim();

  // Check for specific temp files (lattice only) before launching
  if (_stlEngine === 'lattice') {
    const lattice = document.getElementById('stlLatticeType').value;
    fetch('/api/check_temp_files', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({output_path: output, lattice_type: lattice})
    })
    .then(r => r.json())
    .then(data => {
      if (data.count > 0) {
        _showTempBanner('[TEMP FILES] ' + data.count + ' leftover .npy file(s) from a previous crashed run detected for this output path. They will be cleaned up automatically at the start of generation.', false);
      }
      _doGenerateSTL(folder, output);
    })
    .catch(() => _doGenerateSTL(folder, output));
  } else {
    _doGenerateSTL(folder, output);
  }
}

function _doGenerateSTL(folder, output) {
  const btn     = document.getElementById('stlGenerateBtn');
  const ri      = document.getElementById('stlRunInfo');
  const metrics = document.getElementById('stlRunInfoMetrics');
  const elapsed = document.getElementById('stlRunInfoElapsed');
  const logBtn  = document.getElementById('stlLogBtn');

  btn.disabled  = true;
  btn.className = 'btn-launch running';
  btn.innerHTML = '<span class="pulse-dot"></span> GENERATING';

  ri.style.display = '';
  ri.className     = 'run-info ri-running';
  metrics.textContent = 'Launching STL generator...';
  elapsed.textContent = '';
  logBtn.style.display = 'none';

  let payload;
  if (_stlEngine === 'solid_void') {
    const smooth = document.querySelector('input[name="stlSmooth"]:checked').value === 'on';
    const iter   = parseInt(document.getElementById('stlSmoothIter').value);
    const lam    = parseFloat(document.getElementById('stlSmoothLambda').value);
    payload = {engine: 'solid_void', folder, output, smooth, smooth_iter: iter, smooth_lambda: lam};
  } else {
    const lattice  = document.getElementById('stlLatticeType').value;
    const period   = parseFloat(document.getElementById('stlPeriod').value) || 2.0;
    const subgrid  = parseInt(document.getElementById('stlSubgrid').value)  || 6;
    const sigma    = parseFloat(document.getElementById('stlSmoothSigma').value) || 0.0;
    const decim    = parseFloat(document.getElementById('stlDecimation').value) || 0.90;
    const decimMode= document.getElementById('stlDecimationMode').value;
    const floorOn  = document.querySelector('input[name="stlDensityFloor"]:checked') &&
                     document.querySelector('input[name="stlDensityFloor"]:checked').value === 'on';
    const densityFloor = (floorOn && _preflightData) ? _preflightData.feature.recommended_density_floor : null;
    payload = {engine: 'lattice', folder, output, lattice_type: lattice, period, subgrid,
               smooth_sigma: sigma, decimation: decim, decimation_mode: decimMode, density_floor: densityFloor};
  }

  fetch('/api/generate_stl', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  })
  .then(r => r.json())
  .then(data => {
    if (data.success) {
      metrics.textContent = 'Generation running...';
      logBtn.style.display = '';
      var stlCancel = document.getElementById('stlCancelBtn');
      if (stlCancel) stlCancel.classList.add('visible');
      _startStlPoll();
    } else {
      btn.disabled  = false;
      btn.className = 'btn-launch';
      btn.innerHTML = 'GENERATE STL';
      ri.className  = 'run-info ri-crashed';
      metrics.textContent = 'Error: ' + (data.error || 'Unknown error');
    }
  })
  .catch(() => {
    btn.disabled  = false;
    btn.className = 'btn-launch';
    btn.innerHTML = 'GENERATE STL';
    ri.className  = 'run-info ri-crashed';
    metrics.textContent = 'Server error launching STL generator.';
  });
}

// --- STL status polling ---
function _startStlPoll() {
  if (_stlPollInterval) clearInterval(_stlPollInterval);
  _stlPollInterval = setInterval(_pollStlStatus, 500);
}

function _pollStlStatus() {
  fetch('/api/stl_status')
  .then(r => r.json())
  .then(data => {
    const btn     = document.getElementById('stlGenerateBtn');
    const ri      = document.getElementById('stlRunInfo');
    const metrics = document.getElementById('stlRunInfoMetrics');
    const elapsed = document.getElementById('stlRunInfoElapsed');

    if (data.status === 'running') {
      elapsed.textContent = 'Elapsed: ' + _elapsed(data.start_time);
    } else if (data.status === 'completed') {
      clearInterval(_stlPollInterval);
      var stlCancel = document.getElementById('stlCancelBtn');
      if (stlCancel) stlCancel.classList.remove('visible');
      btn.disabled  = false;
      btn.className = 'btn-launch completed';
      btn.innerHTML = 'GENERATE STL';
      ri.className  = 'run-info ri-completed';
      metrics.textContent = 'STL generation completed successfully.';
      elapsed.textContent = 'Done in ' + _elapsed(data.start_time);
    } else if (data.status === 'crashed') {
      clearInterval(_stlPollInterval);
      var stlCancel = document.getElementById('stlCancelBtn');
      if (stlCancel) stlCancel.classList.remove('visible');
      btn.disabled  = false;
      btn.className = 'btn-launch crashed';
      btn.innerHTML = 'GENERATE STL';
      ri.className  = 'run-info ri-crashed';
      metrics.textContent = 'STL generation crashed - check the log for details.';
      elapsed.textContent = _elapsed(data.start_time);
    }
    if (_stlLogOpen) _fetchStlLogLines();
  })
  .catch(() => {});
}

function _elapsed(startIso) {
  if (!startIso) return '';
  const sec = Math.round((Date.now() - new Date(startIso).getTime()) / 1000);
  const m = Math.floor(sec / 60), s = sec % 60;
  return m > 0 ? m + 'm ' + s + 's' : s + 's';
}

// --- STL log modal ---
function openStlLog() {
  document.getElementById('stlLogModal').classList.add('visible');
  _stlLogCursor = 0;
  document.getElementById('stlLogContent').innerHTML = '';
  _stlLogOpen = true;
  _fetchStlLogLines();
  if (_stlLogInterval) clearInterval(_stlLogInterval);
  _stlLogInterval = setInterval(_fetchStlLogLines, 500);
}

function closeStlLog() {
  document.getElementById('stlLogModal').classList.remove('visible');
  _stlLogOpen = false;
  if (_stlLogInterval) { clearInterval(_stlLogInterval); _stlLogInterval = null; }
}

function _fetchStlLogLines() {
  fetch('/api/stl_log', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({cursor: _stlLogCursor})
  })
  .then(r => r.json())
  .then(data => {
    if (!data.lines || !data.lines.length) return;
    const box = document.getElementById('stlLogContent');
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
    data.lines.forEach(line => {
      const div = document.createElement('div');
      div.className = _stlLineClass(line);
      div.textContent = line;
      box.appendChild(div);
    });
    _stlLogCursor = data.cursor;
    if (atBottom) box.scrollTop = box.scrollHeight;
  })
  .catch(() => {});
}

function _stlLineClass(line) {
  const l = line.toLowerCase();
  if (/^--\s*step/i.test(line))                           return 'stl-log-step';
  if (/^--\s*pre-flight/i.test(line))                     return 'stl-log-step';
  if (/done\.|saved.*->|completed/i.test(l))              return 'stl-log-done';
  if (/\[slm pass\]|\[mesh pass\]|\[ram pass\]|\[decimation\].*fits/i.test(line)) return 'stl-log-pass';
  if (/\[slm critical\]|\[mesh critical\]|\[ram critical\]/i.test(line))         return 'stl-log-critical';
  if (/\[slm warning\]|\[mesh warning\]|\[ram warning\]|\[mesh overkill\]/i.test(line)) return 'stl-log-warning';
  if (/phase \d+\s+chunk|chunk \d+\//i.test(line))        return 'stl-log-chunk';
  if (/\[startup\]|\[cleanup\]|\[system\]/i.test(line))   return 'stl-log-dim';
  if (/error|crash|failed/i.test(l))                      return 'stl-log-critical';
  return 'stl-log-info';
}

// --- Temp file banners ---
function _showTempBanner(msg, fromStartup) {
  const banner = document.getElementById('stlTempBanner');
  document.getElementById('stlTempBannerText').textContent = msg;
  banner.classList.add('visible');
}

function dismissTempBanner() {
  document.getElementById('stlTempBanner').classList.remove('visible');
}

// --- Auto-populate popup ---
function _maybeShowAutopop(engine, path) {
  _autopopPath = path;
  const body = document.getElementById('stlAutopopBody');
  const pathEl = document.getElementById('stlAutopopPath');
  const engineLabel = engine === 'lattice' ? 'Lattice BESO' : 'Solid-Void BESO';
  body.textContent = 'A successful ' + engineLabel + ' run was detected. Would you like to auto-populate the data folder?';
  pathEl.textContent = path;
  document.getElementById('stlAutopopOverlay').classList.add('visible');
}

function dismissAutopop() {
  document.getElementById('stlAutopopOverlay').classList.remove('visible');
  // Mark as shown so it doesn't reappear
  fetch('/api/mark_stl_popup_shown', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
}

function acceptAutopop() {
  document.getElementById('stlAutopopOverlay').classList.remove('visible');
  const folderEl = document.getElementById('stlFolderPath');
  folderEl.value = _autopopPath;
  fetch('/api/mark_stl_popup_shown', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
  checkStlFolder(_autopopPath);
}

// --- Called on STL page first visit ---
function onStlPageInit() {
  // Check for stale temp files detected at startup
  fetch('/api/stl_init_info')
  .then(r => r.json())
  .then(data => {
    if (data.stale_count > 0) {
      _showTempBanner(
        '[STALE TEMP FILES] ' + data.stale_count + ' leftover .npy file(s) from a previous crashed STL generation were found in the working directory. They will be cleaned up automatically when generation starts.',
        true
      );
    }
    if (data.show_popup && data.popup_engine && data.popup_path) {
      // Ensure we're on the STL page before showing popup
      _maybeShowAutopop(data.popup_engine, data.popup_path);
    }
  })
  .catch(() => {});
}
</script>

<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/objects/MarchingCubes.js"></script>
<script>
// =============================================================================
//   LATTICE PREVIEW -- Solid surface via THREE.MarchingCubes
//   Isometric camera, Y-axis-only slow rotation, Phong shading.
// =============================================================================
(function() {

  const TPMS = {
    iwp:            (x,y,z) => 2*(Math.cos(x)*Math.cos(y)+Math.cos(y)*Math.cos(z)+Math.cos(z)*Math.cos(x))-(Math.cos(2*x)+Math.cos(2*y)+Math.cos(2*z)),
    gyroid_sheet:   (x,y,z) => Math.sin(x)*Math.cos(y)+Math.sin(y)*Math.cos(z)+Math.sin(z)*Math.cos(x),
    gyroid_ligament:(x,y,z) => Math.sin(x)*Math.cos(y)+Math.sin(y)*Math.cos(z)+Math.sin(z)*Math.cos(x)-0.9,
    primitive:      (x,y,z) => Math.cos(x)+Math.cos(y)+Math.cos(z),
    diamond:        (x,y,z) => Math.sin(x)*Math.sin(y)*Math.sin(z)+Math.sin(x)*Math.cos(y)*Math.cos(z)+Math.cos(x)*Math.sin(y)*Math.cos(z)+Math.cos(x)*Math.cos(y)*Math.sin(z),
    neovius:        (x,y,z) => 3*(Math.cos(x)+Math.cos(y)+Math.cos(z))+4*Math.cos(x)*Math.cos(y)*Math.cos(z)
  };
  const LABELS = {iwp:'IWP', gyroid_sheet:'Gyroid Sheet', gyroid_ligament:'Gyroid Ligament',
    primitive:'Primitive', diamond:'Diamond', neovius:'Neovius'};

  let _scene, _camera, _renderer, _meshGroup;
  let _rotY = 0.6, _rotX = 0.44;
  let _dragging = false, _lastMX = 0, _lastMY = 0;
  let _autoRotating = true;

  function initPreview() {
    const canvas = document.getElementById('latticePreviewCanvas');
    if (!canvas || !window.THREE) return;
    const W = canvas.width, H = canvas.height;

    _scene  = new THREE.Scene();
    _camera = new THREE.PerspectiveCamera(38, W/H, 0.01, 100);
    _camera.position.set(0, 0, 3.0);
    _camera.lookAt(0, 0, 0);

    _renderer = new THREE.WebGLRenderer({canvas, antialias:true, alpha:true});
    _renderer.setSize(W, H);
    _renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    _renderer.setClearColor(0x06060c, 1);

    _scene.add(new THREE.AmbientLight(0x3a4466, 0.85));
    const d1 = new THREE.DirectionalLight(0xaabbee, 1.4);
    d1.position.set(2, 3, 2);
    _scene.add(d1);
    const d2 = new THREE.DirectionalLight(0x112244, 0.4);
    d2.position.set(-2, -1, -1);
    _scene.add(d2);

    _meshGroup = new THREE.Group();
    _meshGroup.rotation.x = _rotX;
    _scene.add(_meshGroup);

    const wrap = document.getElementById('latticePreviewWrap');
    wrap.addEventListener('mousedown', e => {
      _dragging = true; _autoRotating = false;
      _lastMX = e.clientX; _lastMY = e.clientY;
      wrap.style.cursor = 'grabbing';
    });
    window.addEventListener('mousemove', e => {
      if (!_dragging) return;
      _rotY += (e.clientX - _lastMX) * 0.013;
      _rotX += (e.clientY - _lastMY) * 0.013; // Full 2-axis when dragging
      _lastMX = e.clientX; _lastMY = e.clientY;
    });
    window.addEventListener('mouseup', () => {
      if (!_dragging) return;
      _dragging = false;
      document.getElementById('latticePreviewWrap').style.cursor = 'grab';
      setTimeout(() => { _autoRotating = true; }, 1800);
    });
    // Scroll to zoom
    wrap.addEventListener('wheel', e => {
      e.preventDefault();
      const factor = e.deltaY > 0 ? 1.12 : 0.89;
      _camera.position.z = Math.max(1.2, Math.min(8.0, _camera.position.z * factor));
    }, {passive: false});

    const type = document.getElementById('stlLatticeType')?.value || 'iwp';
    buildLattice(type);
    animate();
  }

  function buildLattice(type) {
    while (_meshGroup && _meshGroup.children.length) {
      _meshGroup.remove(_meshGroup.children[0]);
    }
    if (!_meshGroup || !window.THREE) return;
    const fn = TPMS[type];
    if (!fn) return;

    const period = parseFloat(document.getElementById('stlPeriod')?.value) || 2.0;
    // boxMM=10: shows ~(10/period) cells. period=2 → 5 cells, period=10 → 1 cell.
    const boxMM  = 10.0;
    const freq   = (2 * Math.PI) / period;
    const res    = 30;

    const mat = new THREE.MeshPhongMaterial({
      color:0x8899cc, emissive:0x111133, specular:0x4466aa, shininess:45, side:THREE.DoubleSide
    });

    if (window.THREE.MarchingCubes) {
      const mc = new THREE.MarchingCubes(res, mat, false, false, 100000);
      mc.isolation = 0;
      mc.reset = function() {};
      for (let k=0;k<res;k++) for (let j=0;j<res;j++) for (let i=0;i<res;i++) {
        mc.field[k*res*res + j*res + i] = fn(
          (i/(res-1)-0.5)*boxMM*freq, (j/(res-1)-0.5)*boxMM*freq, (k/(res-1)-0.5)*boxMM*freq);
      }
      mc.scale.setScalar(0.82);
      _meshGroup.add(mc);
    } else {
      _contourFallback(fn, freq, boxMM);
    }

    const lbl = document.getElementById('latticePreviewLabel');
    if (lbl) lbl.textContent = LABELS[type] || type;
  }

  function _contourFallback(fn, freq, boxMM) {
    const MS=[[],[0,3],[0,1],[1,3],[1,2],[[0,3],[1,2]],[0,2],[2,3],
               [2,3],[0,2],[[0,1],[2,3]],[1,2],[1,3],[0,1],[0,3],[]];
    const NXY=22,NZ=12,H=0.86,S=H*2;
    const tP=(u)=>(u/NXY-0.5)*boxMM*freq;
    const t2=(a,b)=>Math.abs(a-b)<1e-9?0.5:-a/(b-a);
    const L3=(p,q,t)=>[p[0]+t*(q[0]-p[0]),p[1]+t*(q[1]-p[1]),p[2]+t*(q[2]-p[2])];
    ['z','y','x'].forEach(ax=>{
      for(let s=0;s<=NZ;s++){
        const sf=(s/NZ-0.5)*boxMM*freq,sw=-H+(s/NZ)*S;
        const ef=ax==='z'?(ix,iy)=>[tP(ix),tP(iy),sf]:ax==='y'?(ix,iy)=>[tP(ix),sf,tP(iy)]:(ix,iy)=>[sf,tP(ix),tP(iy)];
        const pts=[];
        for(let iy=0;iy<NXY;iy++)for(let ix=0;ix<NXY;ix++){
          const p=[ef(ix,iy),ef(ix+1,iy),ef(ix+1,iy+1),ef(ix,iy+1)];
          const v=p.map(q=>fn(q[0],q[1],q[2]));
          let idx=0;if(v[0]<0)idx|=1;if(v[1]<0)idx|=2;if(v[2]<0)idx|=4;if(v[3]<0)idx|=8;
          const sg=MS[idx];if(!sg||!sg.length)continue;
          const ep=[L3(p[0],p[1],t2(v[0],v[1])),L3(p[1],p[2],t2(v[1],v[2])),L3(p[3],p[2],t2(v[3],v[2])),L3(p[0],p[3],t2(v[0],v[3]))];
          for(const s2 of sg){const[e0,e1]=Array.isArray(s2[0])?s2:[[s2[0],s2[1]]];pts.push(...ep[e0],...ep[e1]);}
        }
        if(!pts.length)continue;
        const fd=1-Math.pow(Math.abs(s/NZ-0.5)*2,2.5);
        const rm=(arr)=>{const r=new Float32Array(arr.length);for(let i=0;i<arr.length;i+=3){r[i]=ax==='z'?(arr[i]/(boxMM*freq)+0.5)*S-H:ax==='y'?(arr[i]/(boxMM*freq)+0.5)*S-H:sw;r[i+1]=ax==='z'?(arr[i+1]/(boxMM*freq)+0.5)*S-H:ax==='y'?sw:(arr[i+1]/(boxMM*freq)+0.5)*S-H;r[i+2]=ax==='z'?sw:ax==='y'?(arr[i+2]/(boxMM*freq)+0.5)*S-H:(arr[i+2]/(boxMM*freq)+0.5)*S-H;}return r;};
        const geo=new THREE.BufferGeometry();
        geo.setAttribute('position',new THREE.Float32BufferAttribute(rm(pts),3));
        _meshGroup.add(new THREE.LineSegments(geo,new THREE.LineBasicMaterial({color:0x818cf8,transparent:true,opacity:0.55*fd+0.05,blending:THREE.AdditiveBlending,depthWrite:false})));
      }
    });
  }

  function animate() {
    requestAnimationFrame(animate);
    if (_autoRotating) _rotY += 0.0022; // Y-only auto-rotation
    if (_meshGroup) {
      _meshGroup.rotation.y = _rotY;
      _meshGroup.rotation.x = _rotX; // Updated by drag; fixed at 0.44 when not dragging
    }
    if (_renderer) _renderer.render(_scene, _camera);
  }

  window.initLatticePreview   = initPreview;
  window._latticeBuildLattice = buildLattice;
  window.updateLatticePreview = function() {
    const sel = document.getElementById('stlLatticeType');
    if (sel && _meshGroup) buildLattice(sel.value);
  };

  document.addEventListener('DOMContentLoaded', () => {
    if (window.THREE) setTimeout(initPreview, 200);
  });
})();
</script>
</body>
</html>"""


# =============================================================================
#   HTTP REQUEST HANDLER
# =============================================================================

class BESOHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # Suppress default Apache-style logging

    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            self._serve_html()
        elif self.path == '/api/check_abaqus':
            self._api_check_abaqus()
        elif self.path == '/api/existing_config':
            self._api_existing_config()
        elif self.path == '/api/drives':
            self._api_drives()
        elif self.path == '/api/run_status':
            self._api_run_status()
        elif self.path == '/api/stl_status':
            self._api_stl_status()
        elif self.path == '/api/stl_init_info':
            self._api_stl_init_info()
        else:
            self._404()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except:
            self._json_response({"error": "Invalid JSON"}, 400)
            return

        if self.path == '/api/inspect_inp':
            self._api_inspect_inp(data)
        elif self.path == '/api/launch':
            self._api_launch(data)
        elif self.path == '/api/browse':
            self._api_browse(data)
        elif self.path == '/api/browse_folders':
            self._api_browse_folders(data)
        elif self.path == '/api/scan_history':
            self._api_scan_history(data)
        elif self.path == '/api/scan_stl':
            self._api_scan_stl(data)
        elif self.path == '/api/generate_stl':
            self._api_generate_stl(data)
        elif self.path == '/api/run_log':
            self._api_run_log(data)
        elif self.path == '/api/preflight_lattice_stl':
            self._api_preflight_lattice_stl(data)
        elif self.path == '/api/stl_log':
            self._api_stl_log(data)
        elif self.path == '/api/check_temp_files':
            self._api_check_temp_files(data)
        elif self.path == '/api/cancel_run':
            self._api_cancel_run(data)
        elif self.path == '/api/cancel_stl':
            self._api_cancel_stl(data)
        elif self.path == '/api/scan_abaqus_leftovers':
            self._api_scan_abaqus_leftovers(data)
        elif self.path == '/api/recycle_files':
            self._api_recycle_files(data)
        elif self.path == '/api/mark_stl_popup_shown':
            self._api_mark_stl_popup_shown(data)
        else:
            self._404()

    # -------------------------------------------------------------------------

    def _serve_html(self):
        encoded = HTML.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(encoded))
        self.end_headers()
        self.wfile.write(encoded)

    def _api_check_abaqus(self):
        found = check_abaqus_on_path()
        self._json_response({"found": found})

    def _api_existing_config(self):
        config = load_existing_config()
        if config:
            self._json_response({"exists": True, "config": config})
        else:
            self._json_response({"exists": False, "config": None})

    def _api_drives(self):
        self._json_response({"drives": get_drives()})

    def _api_browse(self, data):
        path    = data.get("path", "")
        go_up   = data.get("go_up", False)
        if go_up and path:
            path = os.path.dirname(os.path.abspath(path))
        result = browse_directory(path)
        self._json_response(result)

    def _api_inspect_inp(self, data):
        path = data.get("path", "")
        result = inspect_inp_file(path)
        self._json_response(result)

    def _api_browse_folders(self, data):
        path  = data.get("path", "")
        go_up = data.get("go_up", False)
        if go_up and path:
            path = os.path.dirname(os.path.abspath(path))
        result = browse_directory_folders_only(path)
        self._json_response(result)

    def _api_scan_history(self, data):
        path = data.get("path", "")
        result = scan_history_folder(path)
        self._json_response(result)

    def _api_scan_stl(self, data):
        path = data.get("path", "")
        result = scan_stl_folder(path)
        self._json_response(result)

    def _api_generate_stl(self, data):
        global _stl_state, _stl_popup_shown
        engine  = data.get("engine", "solid_void")
        folder  = data.get("folder", "")
        output  = data.get("output", "")
        work_dir = os.path.dirname(os.path.abspath(__file__))

        with _stl_lock:
            if _stl_state["status"] == "running":
                self._json_response({"success": False, "error": "STL generation already in progress."})
                return

        if engine == "solid_void":
            nodes_path = os.path.join(folder, "best_nodes.csv")
            elems_path = os.path.join(folder, "best_elements.csv")
            solid_path = os.path.join(folder, "best_solid_elements.csv")
            out_path   = output if output else os.path.join(folder, "optimized_structure.stl")

            stl_script = "beso_stl_generator.py"
            for d in [work_dir, os.getcwd()]:
                candidate = os.path.join(d, stl_script)
                if os.path.exists(candidate):
                    stl_script = candidate
                    break

            smooth      = data.get("smooth", False)
            smooth_iter = data.get("smooth_iter", 10)
            smooth_lam  = data.get("smooth_lambda", 0.5)
            cmd = 'python "{}" --nodes "{}" --elements "{}" --solid "{}" --output "{}"'.format(
                stl_script, nodes_path, elems_path, solid_path, out_path)
            if smooth:
                cmd += ' --smooth --smooth-iter {} --smooth-lambda {}'.format(smooth_iter, smooth_lam)

        else:  # lattice
            csv_path    = os.path.join(folder, "Optimized_Density_Map.csv")
            out_path    = output if output else os.path.join(folder, "lattice_output.stl")
            lattice_type= data.get("lattice_type", "iwp")
            period      = float(data.get("period", 2.0))
            subgrid     = int(data.get("subgrid", 6))
            sigma       = float(data.get("smooth_sigma", 0.0))
            decim       = float(data.get("decimation", 0.90))
            decim_mode  = data.get("decimation_mode", "auto")
            density_floor = data.get("density_floor", None)

            stl_script = "beso_lattice_stl_generator.py"
            for d in [work_dir, os.getcwd()]:
                candidate = os.path.join(d, stl_script)
                if os.path.exists(candidate):
                    stl_script = candidate
                    break

            cmd = ('python "{}" --csv "{}" --output "{}" --lattice-type {} '
                   '--subgrid {} --period {} --smooth-sigma {} '
                   '--decimation {} --decimation-mode {} --non-interactive').format(
                stl_script, csv_path, out_path, lattice_type,
                subgrid, period, sigma, decim, decim_mode)
            if density_floor is not None:
                cmd += ' --density-floor {}'.format(density_floor)

        with _stl_lock:
            _stl_state.update({
                "status":     "running",
                "start_time": datetime.now().isoformat(),
                "end_time":   None,
                "log":        [],
            })

        def run():
            global _stl_proc
            try:
                proc = subprocess.Popen(
                    cmd, shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=dict(os.environ, PYTHONUNBUFFERED='1', PYTHONIOENCODING='utf-8'),
                    cwd=work_dir
                )
                _stl_proc = proc
                _stl_reader_thread(proc)
            except Exception as e:
                with _stl_lock:
                    _stl_state["status"]   = "crashed"
                    _stl_state["end_time"] = datetime.now().isoformat()
                    _stl_state["log"].append("[Launcher] Failed to start STL generator: {}".format(e))
                print("[Launcher] STL start error: {}".format(e))

        threading.Thread(target=run, daemon=True).start()
        self._json_response({"success": True})

    def _api_preflight_lattice_stl(self, data):
        """Run beso_lattice_stl_generator.py --preflight-only and return the JSON it prints."""
        csv_path    = data.get("csv", "")
        lattice_type= data.get("lattice_type", "iwp")
        period      = float(data.get("period", 2.0))
        subgrid     = int(data.get("subgrid", 6))
        decim       = float(data.get("decimation", 0.90))
        decim_mode  = data.get("decimation_mode", "auto")

        work_dir   = os.path.dirname(os.path.abspath(__file__))
        stl_script = "beso_lattice_stl_generator.py"
        for d in [work_dir, os.getcwd()]:
            candidate = os.path.join(d, stl_script)
            if os.path.exists(candidate):
                stl_script = candidate
                break

        cmd = ('python "{}" --csv "{}" --lattice-type {} --subgrid {} '
               '--period {} --decimation {} --decimation-mode {} --preflight-only').format(
            stl_script, csv_path, lattice_type, subgrid, period, decim, decim_mode)
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=60,
                cwd=work_dir,
                env=dict(os.environ, PYTHONIOENCODING='utf-8'))
            # The script prints a single JSON line to stdout
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith('{'):
                    self._json_response(json.loads(line))
                    return
            self._json_response({"error": "No JSON output from preflight. stderr: " + result.stderr[-500:]})
        except subprocess.TimeoutExpired:
            self._json_response({"error": "Preflight timed out."})
        except Exception as e:
            self._json_response({"error": str(e)})

    def _api_stl_status(self):
        with _stl_lock:
            resp = {
                "status":     _stl_state["status"],
                "start_time": _stl_state["start_time"],
                "end_time":   _stl_state["end_time"],
            }
        self._json_response(resp)

    def _api_stl_init_info(self):
        """Called on first STL page visit. Returns stale temp file count and popup info."""
        global _stl_popup_shown
        work_dir = os.path.dirname(os.path.abspath(__file__))

        popup_engine = None
        popup_path   = None
        show_popup   = False
        if not _stl_popup_shown:
            with _run_lock:
                last_engine = _run_state.get("engine")
                last_status = _run_state.get("status")
            if last_status == "completed" and last_engine in ("solid_void", "lattice"):
                popup_engine = last_engine
                if last_engine == "lattice":
                    popup_path = os.path.join(work_dir, "Latest_Lattice_Run", "Data_Files")
                else:
                    popup_path = os.path.join(work_dir, "Latest_Classic_Run", "Data_Files")
                if os.path.isdir(popup_path):
                    show_popup = True

        self._json_response({
            "stale_count": len(_stale_temp_files),
            "show_popup":  show_popup,
            "popup_engine": popup_engine,
            "popup_path":   popup_path,
        })

    def _api_stl_log(self, data):
        cursor = int(data.get("cursor", 0))
        with _stl_lock:
            log   = _stl_state["log"]
            lines = log[cursor:]
            new_cursor = len(log)
        self._json_response({"lines": lines, "cursor": new_cursor})

    def _api_check_temp_files(self, data):
        output_path  = data.get("output_path", "")
        lattice_type = data.get("lattice_type", "iwp")
        files = scan_specific_temp_files(output_path, lattice_type)
        self._json_response({"count": len(files), "files": files})

    def _api_cancel_run(self, data):
        kill_run_process()
        with _run_lock:
            _run_state['status'] = 'idle'
        self._json_response({'success': True})

    def _api_cancel_stl(self, data):
        kill_stl_process()
        with _stl_lock:
            _stl_state['status'] = 'idle'
        self._json_response({'success': True})

    def _api_scan_abaqus_leftovers(self, data):
        inp_path = data.get('inp_path', '')
        files, directory = scan_abaqus_leftovers(inp_path)
        self._json_response({'files': files, 'directory': directory})

    def _api_recycle_files(self, data):
        items = data.get('files', [])   # list of {path, is_dir}
        results = []
        for item in items:
            path   = item.get('path', '')
            is_dir = item.get('is_dir', False)
            ok = send_to_recycle_bin(path, is_dir) if path else False
            results.append({'path': path, 'success': ok})
        all_ok = all(r['success'] for r in results)
        self._json_response({'success': all_ok, 'results': results})

    def _api_mark_stl_popup_shown(self, data):
        global _stl_popup_shown
        _stl_popup_shown = True
        self._json_response({"ok": True})

    def _api_launch(self, data):
        # Write config file
        data["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self._json_response({"success": False, "error": "Could not write config: " + str(e)})
            return

        # Determine which optimizer to run
        engine = data.get("engine", "solid_void")
        if engine == "lattice":
            optimizer_script = "beso_lattice_main.py"
            if not os.path.exists(optimizer_script):
                for name in ["beso_main_lattice.py", "beso_lattice.py"]:
                    if os.path.exists(name):
                        optimizer_script = name
                        break
        else:
            optimizer_script = "beso_main.py"
            if not os.path.exists(optimizer_script):
                for name in ["beso_main_comparison_pre_stlgenerator.py", "beso_optimizer.py"]:
                    if os.path.exists(name):
                        optimizer_script = name
                        break

        def run():
            with _run_lock:
                _run_state.update({
                    "status":     "running",
                    "engine":     engine,
                    "start_time": datetime.now().isoformat(),
                    "end_time":   None,
                    "iteration":  0,
                    "stiffness":  0.0,
                    "log":        [],
                })

            try:
                proc = subprocess.Popen(
                    "abaqus python {}".format(optimizer_script),
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=dict(os.environ, PYTHONUNBUFFERED='1'),
                    cwd=os.path.dirname(os.path.abspath(__file__))
                )
                write_pid_file(proc.pid, engine)

                for raw in iter(proc.stdout.readline, b''):
                    line = raw.decode('utf-8', errors='replace').rstrip('\r\n')
                    print(line)                     # launcher terminal
                    parsed = parse_log_line(line)
                    with _run_lock:
                        _run_state["log"].append(line)
                        if "iteration" in parsed:
                            _run_state["iteration"] = parsed["iteration"]
                        if "stiffness" in parsed:
                            _run_state["stiffness"] = parsed["stiffness"]

                proc.wait()
                final = "completed" if proc.returncode == 0 else "crashed"

            except Exception as exc:
                print("[Launcher] Process error: {}".format(exc))
                final = "crashed"
                with _run_lock:
                    _run_state["log"].append("[Launcher] Fatal error: {}".format(exc))

            with _run_lock:
                _run_state["status"]   = final
                _run_state["end_time"] = datetime.now().isoformat()

            delete_pid_file()
            print("[Launcher] Run {}.".format(final))

        threading.Thread(target=run, daemon=True).start()
        self._json_response({"success": True})

    def _api_run_status(self):
        with _run_lock:
            prev = check_previous_session() if _run_state["status"] == "idle" else None
            resp = {
                "status":           _run_state["status"],
                "engine":           _run_state["engine"],
                "start_time":       _run_state["start_time"],
                "end_time":         _run_state["end_time"],
                "iteration":        _run_state["iteration"],
                "stiffness":        _run_state["stiffness"],
                "log_length":       len(_run_state["log"]),
                "previous_session": prev,
            }
        self._json_response(resp)

    def _api_run_log(self, data):
        cursor = int(data.get("cursor", 0))
        with _run_lock:
            lines      = _run_state["log"][cursor:]
            new_cursor = len(_run_state["log"])
        self._json_response({"lines": lines, "cursor": new_cursor})

    def _json_response(self, data, code=200):
        encoded = json.dumps(data).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(encoded))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(encoded)

    def _404(self):
        self.send_response(404)
        self.end_headers()


# =============================================================================
#   MAIN
# =============================================================================

def main():
    print("\n" + "="*55)
    print("   BESO OPTIMIZATION LAUNCHER")
    print("="*55)
    print("   Starting local server on http://localhost:{}".format(PORT))

    # Check for existing config at startup
    config = load_existing_config()
    if config:
        print("   Previous config found: {}".format(config.get("timestamp", "unknown")))

    # Check whether a run from a previous launcher session is still active
    prev = check_previous_session()
    if prev:
        if prev["alive"]:
            print("   [WARNING] A run from a previous session (PID {}) appears to be still running.".format(prev["pid"]))
        else:
            print("   [INFO] A previous run ended while the launcher was offline.")

    # Scan for leftover STL temp files from previous crashed generations
    _init_stale_temp_scan()

    print("   Opening browser...")
    print("   Press Ctrl+C to stop the launcher.")
    print("="*55 + "\n")

    # Open browser after a short delay
    def open_browser():
        import time
        time.sleep(0.8)
        webbrowser.open("http://localhost:{}".format(PORT))

    threading.Thread(target=open_browser, daemon=True).start()

    with socketserver.TCPServer(("", PORT), BESOHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[Launcher] Server stopped.")


if __name__ == "__main__":
    main()
