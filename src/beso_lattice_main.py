# beso_lattice_main_final_v11.py
# Stage 1 of the solid-void (v17) -> lattice migration: Python 2/3 compatibility.
#   - raw_input shim (Python 3 removed raw_input)
#   - _csv_open helper (correct CSV newline handling on Python 2 and 3)
#   - list() wrap on odb .values()[index] access (Python 3 dict-view is not indexable)
# No change to optimisation behaviour. Single-part path must reproduce baseline results.
# Stage 2: speed - CSR sparse filter (apply_filter via W.dot) + weight-free
#   adjacency dict for the morphological skin (Option B). Filter results are
#   numerically equivalent to round-off (summation order changes), not bit-identical.
# Stage 3: v7 - evaluate_specific_stiffness now uses weighted-average displacement
#   across all load cases (was last-step only), consistent with the stress weighting.
#   Single-load-case runs are unchanged. Returns (weighted_disp, K, specific_K).
# Stage 4: multi-part awareness. Design part + its NON_DESIGN_SET are detected
#   from the *Part blocks; the design ODB instance is targeted explicitly
#   (stress via getSubset, no cross-instance label collision). Only the design
#   part's section is replaced by the lattice bins; non-design parts keep their
#   original section + material (Option A: base materials preserved, lattice
#   materials added). Design material now drives E (was last-in-file). Reserved-
#   name guard + no-frames guard added. Single-part path is unchanged.
# Stage 5: per-lattice Gibson-Ashby physics. A chosen lattice type sets the
#   stiffness (C1,n1) and yield (C2,n2) relationships: E=Es*C1*rho^n1 (FGL only;
#   SOLID stays bulk Es) and local_yield=sy*C2*rho^n2. The chosen type + values
#   are written as a manifest comment line in Optimized_Density_Map.csv for the
#   STL generator to auto-detect. Legacy configs (no lattice_type) keep C1=C2=1.
# Stage 6: two selectable lattice PROPERTY MODELS, both routed through a single
#   source of truth (lattice_yield_ratio / lattice_modulus_ratio / invert_yield_ratio)
#   so the yield and stiffness laws can never drift out of sync again.
#   'safe'        : Gibson-Ashby below a cap (default 0.50); at/above the cap the
#                   element is solid bulk material. Fixes the C2-at-solid bug and
#                   avoids extrapolating the power law where it under-predicts.
#   'experimental': endpoint-corrected blend C^(1-rho)*rho^n; reduces to Gibson-
#                   Ashby at low rho and returns bulk continuously at rho=1 (no cap).
#                   Illustrative estimate of FGL potential, NOT a validated law.
#   v6 intentionally does NOT reproduce v5 numbers (v5 applied C2 at full density).
# Stage 7: coefficient set replaced with the verified material-independent
#   homogenization table (AlSi10Mg target). Lattice keys are UNCHANGED (Option A);
#   only the C1/n1/C2/n2 values changed. gyroid_sheet is published by Pais 2023 as a
#   3rd-order polynomial, not a power law; it is approximated here by a single power
#   law fitted over the operating band [0.10, 0.50] (~2.5% RMS, worst case ~6.5% only
#   at the void edge and the cap). Manifest coefficient precision raised to 4 dp.
#   No logic change from v6.
# Stage 8: optional Sized-to-Load (absolute) mode. load_mode='scaled' (default)
#   keeps the load-normalised max-specific-stiffness behaviour byte-for-byte.
#   load_mode='absolute' sets scale_factor=1.0 so each element is sized so its
#   local yield equals its actual stress under the real load; safety_factor
#   multiplies only the sizing target (TRUE_UTILIZATION still reads vs true yield
#   and lands near 1/SF). Discrete philosophy is coupled to absolute (scaling is
#   inert in the discrete branch). Absolute runs also export the converged map to
#   Data_Files/Last_Iteration_Density_Map/ and print a design-set feasibility line.
# Stage 9: all terminal output (stdout + stderr) is mirrored to a plain-text run
#   log at Data_Files/Run_Log.txt for the whole run, via a lightweight tee on
#   sys.stdout/sys.stderr installed at the very start of main (flushed per write
#   and closed at exit). Console output is unchanged. No optimisation logic change.
# Stage 10 (v10): morphological skin rebuilt on an EXACT element adjacency graph
#   instead of borrowing the smoothing filter's neighbour sphere (which coupled
#   skin thickness to the filter radius and over-detected element corners).
#   build_skin_topology() builds a face-adjacency graph + boundary set once:
#   3D hex neighbours share a whole face (4 corner nodes), 2D quad neighbours
#   share a whole edge (2 corner nodes); an element is boundary iff it has a free
#   face/edge. 2D vs 3D is read from the centroid z-spread (planar -> 0); tet
#   meshes raise (not yet supported). get_mises_and_coords now also returns the
#   per-element connectivity and guards a missing z component (2D-safe).
#   Skinning is now: exact exposed layer (outer boundary OR face-adjacent to an
#   AIR void) -> grow inward skin_thickness-1 more lattice layers (uniform shell,
#   default 1) -> harden ONLY where target_rho > void_threshold. The last clause
#   is the fix: a low-stress surface whose target is void is left as transient
#   lattice so the contour can carve through it, instead of being frozen solid by
#   the old 'skip if in elements_to_remove' guard. Internal (trapped) void walls
#   are still left as lattice. A NOTE is printed when move_limit < 1 - void_thresh
#   (fresh surfaces then carve gradually over several passes, by design).
# Stage 11 (v11): the morphological skin is now a ONE-SHOT POST-PROCESS on the
#   converged design instead of a per-iteration override. The loop runs as a clean
#   pure-lattice solver (assign_lattice_bins no longer skins), so it converges to its
#   best specific-stiffness core; apply_final_skin then seals the shell once around the
#   geometry that survived. This removes the per-iteration skin/move-limit/quota
#   coupling (which could strand low-stress surface elements at intermediate density)
#   and gives higher specific stiffness, since the shell wraps only the optimised
#   material rather than being forced onto low-stress surfaces of the full block.
#   A single final FEA on the skinned geometry reports the true (skinned) stiffness,
#   plotted alongside the pre-skin optimum. The skinned map is the deliverable
#   (Optimized_Density_Map.csv); the pure-lattice map is archived under
#   Data_Files/Pre-Skinned Optimised Density Map/ with the same filename for the STL
#   generator. Sealing rule is dimensionality-aware: 3D seals the outer boundary +
#   air-connected void walls only (internal voids stay porous); 2D seals every void
#   wall too, because a planar internal void is physically a through-hole / tunnel
#   whose wall is a real surface. 2D vs 3D is read from the centroid z-spread.

import os
import sys
import time
import csv
import math
import shutil
import json
import hashlib
import numpy as np
from scipy.spatial import cKDTree
from scipy.sparse import csr_matrix 
import matplotlib.pyplot as plt
from odbAccess import openOdb
from abaqusConstants import SCALAR, CENTROID, INTEGRATION_POINT

# Python 2/3 compatibility: define raw_input on Python 3 where it was removed
try:
    raw_input
except NameError:
    raw_input = input

def _csv_open(path):
    if sys.version_info[0] >= 3:
        return open(path, 'w', newline='')
    return open(path, 'wb')

class _TeeLogger(object):
    # Mirror a stream (stdout or stderr) to both the console and a log file.
    def __init__(self, stream, logfile):
        self.stream = stream
        self.logfile = logfile
    def write(self, data):
        try:
            self.stream.write(data)
        except Exception:
            pass
        try:
            self.logfile.write(data)
            self.logfile.flush()
        except Exception:
            pass
    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass
        try:
            self.logfile.flush()
        except Exception:
            pass
    def isatty(self):
        try:
            return self.stream.isatty()
        except Exception:
            return False

# ============================================================================
# LATTICE GIBSON-ASHBY PROPERTIES  (SHARED SOURCE OF TRUTH)
#   E*/Es        = C1 * (rho)^n1    [stiffness]
#   sigma*y/sys  = C2 * (rho)^n2    [yield strength]
# Keep this table IDENTICAL to LATTICE_PROPERTIES in beso_launcher.py and the
# documentation table in beso_lattice_stl_generator.py.
# ============================================================================
LATTICE_PROPERTIES = {
    "gyroid_ligament": {"label": "Gyroid ligament",         "C1": 0.9515, "n1": 2.174, "C2": 0.6182, "n2": 1.746},
    "gyroid_sheet":    {"label": "Gyroid sheet",            "C1": 0.5909, "n1": 1.3454, "C2": 0.7298, "n2": 1.2066},
    "primitive":       {"label": "Schwarz Primitive sheet", "C1": 0.61, "n1": 1.57, "C2": 0.794, "n2": 1.36},
    "diamond":         {"label": "Diamond ligament",        "C1": 0.6438, "n1": 2.026, "C2": 0.6802, "n2": 1.614},
    "iwp":             {"label": "I-WP sheet",              "C1": 0.699, "n1": 1.217, "C2": 0.738, "n2": 1.151},
    "neovius":         {"label": "Neovius sheet",           "C1": 0.705, "n1": 1.236, "C2": 0.721, "n2": 1.5},
}
DEFAULT_LATTICE_TYPE = "iwp"

def resolve_lattice_properties(lattice_type):
    """Return (key, C1, n1, C2, n2) for a lattice type, falling back to the default."""
    key = (lattice_type or DEFAULT_LATTICE_TYPE).strip().lower()
    if key not in LATTICE_PROPERTIES:
        print("   [WARNING] Unknown lattice type '{}'. Falling back to '{}'.".format(lattice_type, DEFAULT_LATTICE_TYPE))
        key = DEFAULT_LATTICE_TYPE
    p = LATTICE_PROPERTIES[key]
    return key, p["C1"], p["n1"], p["C2"], p["n2"]

def format_lattice_manifest(lattice_type, C1, n1, C2, n2, e_solid, yield_stress, design_material):
    """Build the single-line CSV manifest comment read by the STL generator / launcher."""
    return ("# BESO_LATTICE lattice_type={}; C1={:.4f}; n1={:.4f}; C2={:.4f}; n2={:.4f}; "
            "E_solid={}; yield_stress={}; design_material={}").format(
            lattice_type, C1, n1, C2, n2, e_solid, yield_stress, design_material)

# ============================================================================
# LATTICE PROPERTY MODELS  (SINGLE SOURCE OF TRUTH)
#   Every yield and stiffness evaluation in the engine goes through these,
#   so the laws used for sizing, sorting, the FE modulus and the displayed
#   utilization can never disagree.
# ============================================================================
def lattice_yield_ratio(rho, C2, n2, model, cap):
    # sigma_y(rho) / sigma_s
    if model == "experimental":
        return (C2 ** (1.0 - rho)) * (rho ** n2)
    if rho >= cap:
        return 1.0
    return C2 * (rho ** n2)

def lattice_modulus_ratio(rho, C1, n1, model, cap):
    # E(rho) / Es
    if model == "experimental":
        return (C1 ** (1.0 - rho)) * (rho ** n1)
    if rho >= cap:
        return 1.0
    return C1 * (rho ** n1)

def invert_yield_ratio(target_ratio, C2, n2, model):
    # Return the relative density whose yield ratio matches target_ratio.
    if model == "safe":
        # Closed-form Gibson-Ashby inverse (may exceed 1.0; clamped downstream).
        return (target_ratio / C2) ** (1.0 / n2)
    # Experimental blend is not closed-form invertible; the curve is monotonic
    # in rho, so bisect between 0.0 and 1.0.
    if target_ratio >= 1.0:
        return 1.0
    lo = 0.0
    hi = 1.0
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        if (C2 ** (1.0 - mid)) * (mid ** n2) < target_ratio:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)

# --- FOLDER SETUP ---
MASTER_DIR = "Latest_Lattice_Run"
DIR_INP = os.path.join(MASTER_DIR, "INP_Files")
DIR_ODB = os.path.join(MASTER_DIR, "ODB_Files")
DIR_MISC = os.path.join(MASTER_DIR, "Misc_Abaqus_Files")
DIR_DATA = os.path.join(MASTER_DIR, "Data_Files")
DIR_SCRATCH = os.path.join(MASTER_DIR, "Scratch_Temp")

# --- CONFIG FILE ---
CONFIG_FILE = "beso_config.json"

# =============================================================
# BASE MODEL RESOLUTION AND STAGING  [v50]
# -------------------------------------------------------------
# Mirrors the same section in beso_main.py, so both engines resolve, name and
# stage the base model in exactly the same way. The model used to be a bare
# stem ("beam_beso_base") opened relative to the working directory, which
# forced the INP to live next to the scripts and made the file name double as
# the Abaqus job name. It is now resolved from an explicit path, given a
# sanitised job name, and copied into the working folder before solving.
#
# This matters more here than in the solid-void engine: inject_custom_fields
# reopens the base ODB with readOnly=False and writes TRUE_UTILIZATION and
# DENSITY into it. Working on a staged copy means that write lands in the run
# folder rather than beside the user's own model.
#
# Resolution order:
#   1. the archived copy in INP_Files (prefer_archive; the lattice engine has
#      no resume yet, so it always passes False, but the two engines are kept
#      symmetric so resume can be added without touching this code),
#   2. model.inp_path from beso_config.json,
#   3. base_job + ".inp" relative to the working directory (legacy behaviour).
# =============================================================

BASE_MANIFEST_NAME = "base_model.json"


def sanitize_job_name(name):
    """Return an Abaqus-safe job name: letters, digits and underscores only,
    never starting with a digit. Anything else becomes an underscore."""
    safe = ""
    for ch in str(name).strip():
        if ("a" <= ch <= "z") or ("A" <= ch <= "Z") or \
           ("0" <= ch <= "9") or ch == "_":
            safe += ch
        else:
            safe += "_"
    safe = safe.strip("_")
    if not safe:
        return "beso_job"
    if "0" <= safe[0] <= "9":
        safe = "job_" + safe
    return safe


def file_signature(path):
    """(size_in_bytes, md5_hex) for a file, or (None, None) if unreadable."""
    try:
        size = os.path.getsize(path)
        digest = hashlib.md5()
        handle = open(path, "rb")
        try:
            while True:
                chunk = handle.read(1048576)
                if not chunk:
                    break
                digest.update(chunk)
        finally:
            handle.close()
        return size, digest.hexdigest()
    except Exception:
        return None, None


def describe_size(n_bytes):
    if n_bytes is None:
        return "unknown size"
    if n_bytes >= 1048576:
        return "{:.1f} MB".format(n_bytes / 1048576.0)
    if n_bytes >= 1024:
        return "{:.1f} kB".format(n_bytes / 1024.0)
    return "{} B".format(n_bytes)


def find_archived_base(dir_inp, job_hint):
    """Locate the base model archived inside a run folder's INP_Files. Prefers
    <job_hint>.inp; otherwise accepts a single non-iteration .inp."""
    if not dir_inp or not os.path.isdir(dir_inp):
        return None
    if job_hint:
        candidate = os.path.join(dir_inp, sanitize_job_name(job_hint) + ".inp")
        if os.path.isfile(candidate):
            return candidate
    loose = []
    for name in os.listdir(dir_inp):
        if not name.lower().endswith(".inp"):
            continue
        if name.lower().startswith("iteration_"):
            continue
        loose.append(os.path.join(dir_inp, name))
    if len(loose) == 1:
        return loose[0]
    return None


def resolve_base_model(inp_path, base_job, dir_inp, prefer_archive):
    """Decide which INP file is the base model for this run.

    Returns {source, job_name, size, md5, origin} or None when nothing usable
    was found (the caller aborts)."""
    archived = find_archived_base(dir_inp, base_job) if prefer_archive else None

    candidates = []
    if archived:
        candidates.append(("archived copy in " + dir_inp, archived))
    if inp_path:
        candidates.append(("config model.inp_path", inp_path))
    if base_job:
        legacy = base_job if base_job.lower().endswith(".inp") \
            else base_job + ".inp"
        candidates.append(("base_job next to the scripts", legacy))

    chosen = None
    tried = []
    for origin, candidate in candidates:
        tried.append("{}  ->  {}".format(origin, os.path.abspath(candidate)))
        if chosen is None and os.path.isfile(candidate):
            chosen = (origin, candidate)

    if chosen is None:
        print("\n   [v50][ERROR] Base model INP file not found.")
        if tried:
            print("   Paths tried, in order:")
            for entry in tried:
                print("     - {}".format(entry))
        else:
            print("   No path was supplied at all.")
        print("   Set 'model' -> 'inp_path' in beso_config.json to the full path")
        print("   of your .inp file, or place the file next to the scripts.")
        return None

    origin, source = chosen
    source = os.path.abspath(source)

    if prefer_archive and archived and inp_path and os.path.isfile(inp_path):
        arch_size, arch_md5 = file_signature(archived)
        conf_size, conf_md5 = file_signature(inp_path)
        if arch_md5 is not None and conf_md5 is not None and arch_md5 != conf_md5:
            print("\n   [v50][ERROR] This run folder was built from a different")
            print("   model than the one now configured.")
            print("     archived:   {}  ({}, md5 {})".format(
                  archived, describe_size(arch_size), arch_md5))
            print("     configured: {}  ({}, md5 {})".format(
                  os.path.abspath(inp_path), describe_size(conf_size), conf_md5))
            print("   Aborting rather than guessing which one was meant.")
            return None

    size, md5 = file_signature(source)
    original_stem = os.path.splitext(os.path.basename(source))[0]
    job_name = sanitize_job_name(original_stem)

    print("\n-> [v50] BASE MODEL")
    print("   Source:    {}".format(source))
    print("   Found via: {}".format(origin))
    print("   Size:      {}   md5: {}".format(describe_size(size), md5))
    print("   Job name:  {}".format(job_name))
    if job_name != original_stem:
        print("   [note] '{}' is not a valid Abaqus job name, so this run uses"
              .format(original_stem))
        print("          '{}'. Your file keeps its own name and is never"
              .format(job_name))
        print("          modified.")

    return {"source": source, "job_name": job_name,
            "size": size, "md5": md5, "origin": origin}


def stage_base_model(source, job_name):
    """Copy the base model into the working directory as <job_name>.inp so the
    solver never touches the user's own file.

    Returns (local_path, staged_is_copy). staged_is_copy is False in the legacy
    layout, where the user's own file already sits at the working-copy path: in
    that case it is THEIR file, so it must never be moved away afterwards."""
    local = job_name + ".inp"
    if os.path.abspath(source) == os.path.abspath(local):
        print("-> [v50] Base model is already at the working path ({}).".format(local))
        print("         It will be archived by copy, not moved, so your file stays put.")
        return local, False
    shutil.copyfile(source, local)
    print("-> [v50] Staged working copy: {}".format(os.path.abspath(local)))
    print("         The original file is left untouched.")
    return local, True


def _safe_move(src, dst):
    """shutil.move that overwrites an existing destination instead of failing,
    so re-running in the same folder is not an error. Returns True if moved."""
    if not os.path.exists(src):
        return False
    try:
        if os.path.exists(dst):
            os.remove(dst)
        shutil.move(src, dst)
        return True
    except Exception:
        return False


def archive_preflight_artifacts(job_name, dir_inp, dir_odb, dir_misc,
                                staged_is_copy=True):
    """[v50] Tidy everything the base solve left in the working folder. The
    staged working copy goes to INP_Files (so the run folder keeps an exact
    snapshot of the model that produced it), the ODB to ODB_Files and the rest
    to Misc_Abaqus_Files. Overwrite-safe, unlike cleanup_files, so re-running
    in the same folder does not fail."""
    moved = []
    dest_inp = os.path.join(dir_inp, job_name + ".inp")
    if staged_is_copy:
        # Our own staged copy: move it, nothing else refers to it.
        if _safe_move(job_name + ".inp", dest_inp):
            moved.append(job_name + ".inp")
    else:
        # Legacy layout: this is the user's own file sitting at the working
        # path. Archive a copy and leave their file exactly where they put it.
        try:
            if os.path.isfile(job_name + ".inp"):
                if os.path.exists(dest_inp):
                    os.remove(dest_inp)
                shutil.copyfile(job_name + ".inp", dest_inp)
                print("-> [v50] Base model archived by copy; your file stays at {}"
                      .format(os.path.abspath(job_name + ".inp")))
        except Exception as e:
            print("   [v50][WARN] Could not archive the base model: {}".format(e))
    if _safe_move(job_name + ".odb", os.path.join(dir_odb, job_name + ".odb")):
        moved.append(job_name + ".odb")
    for ext in ['.dat', '.msg', '.sta', '.prt', '.com', '.sim',
                '.abq', '.mdl', '.stt']:
        if _safe_move(job_name + ext, os.path.join(dir_misc, job_name + ext)):
            moved.append(job_name + ext)
    print("-> [v50] Base solve artefacts tidied: {} file(s) moved out of the "
          "working folder.".format(len(moved)))
    return os.path.join(dir_inp, job_name + ".inp")


def write_base_manifest(dir_data, info):
    """Record which model this run folder belongs to."""
    try:
        with open(os.path.join(dir_data, BASE_MANIFEST_NAME), "w") as f:
            json.dump(info, f, indent=2)
    except Exception as e:
        print("   [v50][WARN] Could not write the base model manifest: {}".format(e))


def guess_default_inp():
    """A sensible default for the interactive prompt: the single .inp in the
    working folder, or beam_beso_base.inp when it is present."""
    try:
        found = []
        for name in os.listdir("."):
            if not name.lower().endswith(".inp"):
                continue
            if name.lower().startswith("iteration_"):
                continue
            found.append(name)
    except Exception:
        return None
    if len(found) == 1:
        return found[0]
    for name in found:
        if name.lower() == "beam_beso_base.inp":
            return name
    return None


def prompt_for_base_inp():
    """[v50] Ask for the base model in standalone (no config) mode. Accepts a
    full path, a relative path or a plain file name. Returns (inp_path,
    base_job). Previously this was hard-coded to beam_beso_base with no
    prompt at all."""
    default_inp = guess_default_inp()
    print("\nBASE MODEL")
    print("  Full path, relative path or file name. The file is copied into the")
    print("  working folder before solving and is never modified.")
    if default_inp:
        answer = raw_input("Base INP file (Enter for {}): ".format(default_inp))
    else:
        answer = raw_input("Base INP file: ")
    answer = answer.strip().strip('"').strip("'")
    if not answer:
        answer = default_inp if default_inp else "beam_beso_base.inp"
    if not answer.lower().endswith(".inp"):
        answer = answer + ".inp"
    if os.path.isfile(answer):
        print("-> Using {}".format(os.path.abspath(answer)))
    else:
        print("-> [WARNING] {} does not exist. The run will stop at the base "
              "model check.".format(os.path.abspath(answer)))
    job = sanitize_job_name(os.path.splitext(os.path.basename(answer))[0])
    print("-> Job name: {}".format(job))
    return answer, job


# --- 1. RUNNER FUNCTION ---
def run_abaqus_job(job_name, input_name, cpus=4, memory_percent=90):
    print("-> Running Abaqus Job: {}... (Please wait)".format(job_name))
    abs_scratch_path = os.path.abspath(DIR_SCRATCH)
    command = "abaqus job={} input=\"{}\" cpus={} memory={}% scratch=\"{}\" ask_delete=OFF interactive".format(
        job_name, input_name, cpus, memory_percent, abs_scratch_path)
    os.system(command)

# --- 2. EXTRACTOR FUNCTION (VON MISES STRESS UPGRADE) ---
def find_odb_design_instance(odb, design_part_name):
    """
    Return the ODB instance corresponding to the design part. Matches by instance
    name (exact, then PARTNAME-n); if ambiguous or absent, falls back to the
    instance with the most elements (the design domain is by far the largest),
    with a warning. design_part_name=None -> largest instance.
    """
    instances = odb.rootAssembly.instances
    if design_part_name is not None:
        target = design_part_name.upper()
        candidates = []
        for name in instances.keys():
            u = name.upper()
            if u == target or u.startswith(target + "-"):
                candidates.append(instances[name])
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            return max(candidates, key=lambda inst: len(inst.elements))
        print("   [WARNING] No ODB instance matched design part '{}'; using largest instance.".format(design_part_name))
    return max(instances.values(), key=lambda inst: len(inst.elements))

def get_mises_and_coords(odb_name, design_part_name, step_weights=None):
    print("-> Extracting Von Mises Stress and Coordinates from {}...".format(odb_name))
    odb = openOdb(odb_name)

    steps = list(odb.steps.values())
    if len(steps) == 0 or any(len(s.frames) == 0 for s in steps):
        odb.close()
        dat_name = odb_name.replace(".odb", ".dat")
        print("")
        print("[FATAL] Abaqus job for {} produced no usable results (no frames).".format(odb_name))
        print("        The analysis almost certainly failed - check {} for the error.".format(dat_name))
        print("        Common causes: path too long or with spaces, or an invalid generated INP.")
        sys.exit(1)

    design_instance = find_odb_design_instance(odb, design_part_name)
    num_steps = len(steps)
    
    if step_weights is not None and len(step_weights) != num_steps:
        print("\n[WARNING] You provided {} weights, but the ODB has {} load case(s)!".format(len(step_weights), num_steps))
        print("Reverting to Equal Weights to prevent a crash.")
        step_weights = None 

    if step_weights is None:
        step_weights = [1.0 / num_steps] * num_steps

    print("   Found {} Load Case(s). Building Master Stress Map...".format(num_steps))
    
    # --- 2.1 GET VON MISES STRESS ---
    master_mises = {}
    
    for step_idx, step in enumerate(steps):
        last_frame = step.frames[-1]
        stress_field = last_frame.fieldOutputs['S'].getSubset(region=design_instance)
        weight = step_weights[step_idx]
        
        # 1. Temporary dictionary to find the MAX integration point for THIS step
        step_max_mises = {}
        
        for block in stress_field.bulkDataBlocks:
            labels = block.elementLabels
            mises_data = block.mises 
            
            for i in range(len(labels)):
                eid = labels[i]
                stress_val = mises_data[i]
                
                # If we haven't seen this element yet, save its stress
                if eid not in step_max_mises:
                    step_max_mises[eid] = stress_val
                # If we have, only overwrite it if this integration point is HIGHER
                elif stress_val > step_max_mises[eid]:
                    step_max_mises[eid] = stress_val
                    
        # 2. Now that we found the max stress for each element in THIS step, 
        # mathematically merge it into the Master map
        for eid, max_stress in step_max_mises.items():
            if eid not in master_mises:
                master_mises[eid] = 0.0
            master_mises[eid] += max_stress * weight

    # GET GEOMETRY DATA
    instance = design_instance
    
    node_coords = {}
    for node in instance.nodes:
        node_coords[node.label] = node.coordinates
        
    coord_data = {}
    connectivity = {}
    for elem in instance.elements:
        
        # <--- THE GHOST ELEMENT FIX --->
        # If this element doesn't compute stress (like an RP or Coupling), ignore it entirely!
        if elem.label not in master_mises:
            continue
            
        elem_nodes = elem.connectivity
        connectivity[elem.label] = list(elem_nodes)
        x_sum, y_sum, z_sum = 0.0, 0.0, 0.0
        for node_label in elem_nodes:
            coords = node_coords[node_label]
            x_sum += coords[0]
            y_sum += coords[1]
            # 2D planar models may report only (x, y); treat a missing 3rd
            # component as 0.0 so centroids and the skin work either way.
            z_sum += coords[2] if len(coords) > 2 else 0.0
            
        num_nodes = len(elem_nodes)
        coord_data[elem.label] = (x_sum / num_nodes, y_sum / num_nodes, z_sum / num_nodes)

    odb.close()
    return master_mises, coord_data, connectivity

# --- EXPORT DENSITY MAP TO CSV ---
def export_density_to_csv(coord_data, density_dict, filename="Density_Map.csv", manifest_line=None):
    print("-> Exporting Final Density Map to CSV...")
    with open(filename, 'w') as f:
        if manifest_line:
            f.write(manifest_line + "\n")
        f.write("Element_ID,X,Y,Z,Density_Percentage\n")
        for eid, coords in coord_data.items():
            dens = density_dict.get(eid, 0)
            f.write("{},{},{},{},{}\n".format(eid, coords[0], coords[1], coords[2], dens))

# --- JANITOR FUNCTION ---
def cleanup_files(job_name, keep_inp=False):
    if not keep_inp and os.path.exists(job_name + ".inp"):
        shutil.move(job_name + ".inp", os.path.join(DIR_INP, job_name + ".inp"))
    if os.path.exists(job_name + ".odb"):
        shutil.move(job_name + ".odb", os.path.join(DIR_ODB, job_name + ".odb"))
        
    junk_extensions = ['.dat', '.msg', '.sta', '.prt', '.com', '.sim', '.abq', '.mdl', '.stt']
    for ext in junk_extensions:
        if os.path.exists(job_name + ext):
            try:
                shutil.move(job_name + ext, os.path.join(DIR_MISC, job_name + ext))
            except:
                pass 

# --- 3A. PRE-COMPUTE FILTER WEIGHTS ---
def prepare_filter_weights(coord_data, r_min, is_gaussian):
    if is_gaussian:
        print("-> Pre-computing Filter Weights using Gaussian SciPy KD-Tree (sparse CSR)...")
    else:
        print("-> Pre-computing Filter Weights using Linear SciPy KD-Tree (sparse CSR)...")

    elem_ids = np.array(list(coord_data.keys()))
    coords = np.array(list(coord_data.values()))
    elem_ids_py = [int(e) for e in elem_ids]   # python ints, same order as elem_ids

    tree = cKDTree(coords)
    neighbor_indices_list = tree.query_ball_point(coords, r_min)

    total_elements = len(elem_ids)

    # CSR triplets hold the NORMALISED filter weights (each row sums to 1),
    # so apply_filter reduces to a single sparse matrix-vector product.
    row_indices = []
    col_indices = []
    weight_values = []

    # Weight-free adjacency for the morphological skin / flood-fill. Built from
    # the exact same (dist < r_min) membership and ordering as the old neighbour
    # dict, so the skin behaves identically to the dict-of-dicts version.
    adjacency = {}

    # Calculate how often to update the screen (e.g., 50 times total)
    update_interval = max(1, total_elements // 50)

    for i in range(total_elements):
        target_coord = coords[i]
        neighbor_indices = neighbor_indices_list[i]

        raw_weights = []   # (column_index, weight) pairs for this row
        neigh_ids = []     # python-int neighbour ids for this element
        weight_sum = 0.0

        for idx in neighbor_indices:
            neighbor_coord = coords[idx]
            dist = np.linalg.norm(target_coord - neighbor_coord)

            if dist < r_min:
                if is_gaussian:
                    sigma = r_min / 1.25
                    weight = np.exp(-0.5 * (dist / sigma)**2)
                else:
                    weight = r_min - dist

                raw_weights.append((idx, weight))
                neigh_ids.append(elem_ids_py[idx])
                weight_sum += weight

        adjacency[elem_ids_py[i]] = neigh_ids

        if weight_sum > 0.0:
            for idx, w in raw_weights:
                row_indices.append(i)
                col_indices.append(idx)
                weight_values.append(w / weight_sum)

        # --- THE PROGRESS BAR LOGIC ---
        # Using sys.stdout to overwrite the same line using the carriage return (\r)
        if i % update_interval == 0 or i == total_elements - 1:
            percent = float(i + 1) / total_elements * 100.0
            bar_length = 40
            filled_length = int(bar_length * (percent / 100.0))
            bar = '=' * filled_length + '-' * (bar_length - filled_length)

            # The \r pulls the cursor back to the start of the line
            sys.stdout.write('\r   Progress: [{}] {:.1f}%'.format(bar, percent))
            sys.stdout.flush() # Forces the terminal to draw it immediately

    # Print a final newline so the next print statement doesn't overwrite our finished bar
    sys.stdout.write('\n')

    W = csr_matrix(
        (weight_values, (row_indices, col_indices)),
        shape=(total_elements, total_elements),
        dtype=np.float64
    )
    print("   Filter matrix: {}x{}, {} non-zeros.".format(total_elements, total_elements, W.nnz))

    # Returns BOTH: (CSR matrix, element-id order) for fast filtering, and a
    # weight-free adjacency dict {eid: [neighbour_eids]} for the skin logic.
    return (W, elem_ids), adjacency

# --- 3B. APPLY FILTER ---
def apply_filter(data_dict, filter_weights):
    W, elem_ids = filter_weights
    # .get(eid, 0.0) keeps this safe if a neighbour id is ever absent from the
    # input map (the old version raised KeyError); normal runs are unaffected.
    sens_array = np.array([data_dict.get(eid, 0.0) for eid in elem_ids], dtype=np.float64)
    result = W.dot(sens_array)
    return dict(zip(elem_ids.tolist(), result.tolist()))

# --- 3C. EXTRACT NON-DESIGN SPACE (EXACT MATCH UPGRADE) ---
def get_non_design_elements(inp_path, set_name="NON_DESIGN_SET"):
    """Flat reader for models with no *Part blocks. Reads NON_DESIGN_SET from
    anywhere in the file (structure-blind) and expands generate ranges."""
    print("-> Scanning INP for EXACT Non-Design Space ({})...".format(set_name))
    non_design_elements = set()
    try:
        with open(inp_path, 'r') as f:
            lines = f.readlines()

        is_reading_set = False
        set_is_generate = False
        for line in lines:
            upper_line = line.upper().strip()

            if upper_line.startswith("*ELSET"):
                is_reading_set = False
                set_is_generate = "GENERATE" in upper_line
                for part in upper_line.split(','):
                    clean_part = part.strip()
                    if clean_part.startswith("ELSET="):
                        actual_name = clean_part.split('=')[1].strip()
                        if actual_name == set_name.upper():
                            is_reading_set = True
                continue

            elif upper_line.startswith("*") and is_reading_set:
                is_reading_set = False

            if is_reading_set:
                nums = [p.strip() for p in line.strip().split(',') if p.strip()]
                if set_is_generate and len(nums) >= 2:
                    try:
                        start = int(nums[0]); end = int(nums[1])
                        step = int(nums[2]) if len(nums) >= 3 else 1
                        for e in range(start, end + 1, step):
                            non_design_elements.add(e)
                    except ValueError:
                        pass
                else:
                    for p in nums:
                        if p.isdigit():
                            non_design_elements.add(int(p))

        print("   Found {} protected elements.".format(len(non_design_elements)))
    except Exception as e:
        print("   [WARNING] Could not read non-design set: {}".format(e))

    return non_design_elements

def find_design_part_and_non_design(inp_path, set_name="NON_DESIGN_SET"):
    """
    Multi-part aware. Scans every *Part block (element count + part-level
    NON_DESIGN_SET) AND assembly-level NON_DESIGN_SET cards (*Elset ...,
    instance=Z), attributing the latter to their part via the *Instance
    instance->part map. Element IDs are part-local in both cases, which matches
    the design instance getSubset labels used at run time. generate ranges are
    expanded; duplicates are removed by the per-part set (an element listed at
    both levels is counted once). Design part = part with the most FREE elements
    (elements - frozen). Returns (design_part_name, frozen_set_of_design_part).
    If the model has no *Part blocks, returns (None, <global NON_DESIGN_SET>) so
    single-domain / flattened models keep working.
    """
    print("-> Scanning INP for parts and the design domain...")
    target = set_name.upper()

    with open(inp_path, 'r') as f:
        lines = f.readlines()

    parts = {}                # name -> {'elements': int, 'frozen': set()}
    instance_to_part = {}     # INSTANCE_NAME(upper) -> part_name(original case)
    current_part = None
    in_element = False
    reading_set = False       # reading a part-level NON_DESIGN_SET
    set_is_generate = False
    reading_asm_set = False   # reading an assembly-level NON_DESIGN_SET
    asm_set_generate = False
    asm_target_part = None    # part the current assembly-level set maps to

    def _add_frozen(part_name, nums, is_generate):
        if part_name is None or part_name not in parts:
            return
        fset = parts[part_name]['frozen']
        if is_generate and len(nums) >= 2:
            try:
                start = int(nums[0]); end = int(nums[1])
                step = int(nums[2]) if len(nums) >= 3 else 1
                for e in range(start, end + 1, step):
                    fset.add(e)
            except ValueError:
                pass
        else:
            for p in nums:
                if p.isdigit():
                    fset.add(int(p))

    for line in lines:
        stripped = line.strip()
        upper = stripped.upper()

        if upper.startswith("*PART"):
            current_part = None
            for tok in stripped.split(','):
                tk = tok.strip()
                if tk.upper().startswith("NAME="):
                    current_part = tk.split('=', 1)[1].strip()
            if current_part is not None:
                parts.setdefault(current_part, {'elements': 0, 'frozen': set()})
            in_element = False
            reading_set = False
            continue

        if upper.startswith("*END PART"):
            current_part = None
            in_element = False
            reading_set = False
            continue

        # ----- inside a *Part block (part-local element IDs) -----
        if current_part is not None:
            if upper.startswith("*ELEMENT"):
                in_element = True
                reading_set = False
                continue
            if upper.startswith("*ELSET"):
                in_element = False
                set_is_generate = "GENERATE" in upper
                reading_set = False
                for tok in upper.split(','):
                    tk = tok.strip()
                    if tk.startswith("ELSET=") and tk.split('=', 1)[1].strip() == target:
                        reading_set = True
                continue
            if upper.startswith("*"):
                in_element = False
                reading_set = False
                continue
            if not stripped:
                continue
            if in_element:
                parts[current_part]['elements'] += 1
            if reading_set:
                nums = [p.strip() for p in stripped.split(',') if p.strip()]
                _add_frozen(current_part, nums, set_is_generate)
            continue

        # ----- outside any *Part: assembly / model level -----
        if upper.startswith("*INSTANCE"):
            inst_name = None
            inst_part = None
            for tok in stripped.split(','):
                tk = tok.strip()
                if tk.upper().startswith("NAME="):
                    inst_name = tk.split('=', 1)[1].strip()
                elif tk.upper().startswith("PART="):
                    inst_part = tk.split('=', 1)[1].strip()
            if inst_name is not None and inst_part is not None:
                instance_to_part[inst_name.upper()] = inst_part
            reading_asm_set = False
            continue

        if upper.startswith("*ELSET"):
            asm_set_generate = "GENERATE" in upper
            reading_asm_set = False
            asm_target_part = None
            is_target = False
            inst_ref = None
            for tok in upper.split(','):
                tk = tok.strip()
                if tk.startswith("ELSET=") and tk.split('=', 1)[1].strip() == target:
                    is_target = True
                if tk.startswith("INSTANCE="):
                    inst_ref = tk.split('=', 1)[1].strip()
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
            nums = [p.strip() for p in stripped.split(',') if p.strip()]
            _add_frozen(asm_target_part, nums, asm_set_generate)

    if not parts:
        print("   [INFO] No *Part blocks found; using single-domain mode.")
        return None, get_non_design_elements(inp_path, set_name)

    best = None
    best_free = -1
    for name, d in parts.items():
        free = d['elements'] - len(d['frozen'])
        if free > best_free:
            best_free = free
            best = name

    design_frozen = parts[best]['frozen']
    others = [n for n in parts if n != best]
    print("   Design part: '{}' ({} elements, {} frozen, {} free).".format(
        best, parts[best]['elements'], len(design_frozen), best_free))
    if others:
        print("   Non-design part(s): {}".format(", ".join(others)))
    print("   Found {} protected elements in the design part.".format(len(design_frozen)))
    return best, design_frozen

# --- THE MASTER DATA PARSER ---
def extract_global_inp_data(inp_path, design_part_name=None):
    """
    Scans the master Abaqus INP and extracts the DESIGN part's physics: its
    section material's E/nu, the section thickness line, and the total applied
    load. When design_part_name is None (single-domain / no *Part), falls back to
    the last section/material found (original behaviour). Also guards against a
    base material colliding with our injected names (SOLID / MAT_VOID / FGL_xx).
    """
    print("-> Scanning Master INP File for Global Physics Parameters...")

    model_data = {
        'E_modulus': 110000.0,
        'Poisson': 0.33,
        'Yield_Stress': 900.0,
        'Material_Name': 'UNKNOWN_MAT',
        'Section_Name': 'UNKNOWN_SEC',
        'Total_Force': 0.0,
        'Thickness_Line': ",\n"
    }

    try:
        with open(inp_path, 'r') as f:
            lines = f.readlines()

        materials = {}        # NAME(upper) -> (E, nu)
        all_mat_names = []    # original-case, for the reserved-name guard
        current_mat = None
        current_part = None
        design_mat_name = None
        legacy_mat_name = None
        is_reading_cload = False

        target_part = design_part_name.upper() if design_part_name else None

        for i, line in enumerate(lines):
            upper_line = line.upper().strip()

            if upper_line.startswith("*PART"):
                current_part = None
                for tok in line.split(','):
                    t = tok.strip()
                    if t.upper().startswith("NAME="):
                        current_part = t.split('=', 1)[1].strip()
                continue
            if upper_line.startswith("*END PART"):
                current_part = None
                continue

            if upper_line.startswith("*MATERIAL"):
                current_mat = None
                for tok in line.split(','):
                    t = tok.strip()
                    if t.upper().startswith("NAME="):
                        current_mat = t.split('=', 1)[1].strip()
                if current_mat:
                    all_mat_names.append(current_mat)
                continue

            if upper_line.startswith("*ELASTIC"):
                if current_mat is not None and i + 1 < len(lines):
                    mat_data = lines[i+1].split(',')
                    if len(mat_data) >= 2:
                        try:
                            materials[current_mat.upper()] = (
                                float(mat_data[0].strip()), float(mat_data[1].strip()))
                        except ValueError:
                            pass
                continue

            if upper_line.startswith("*SOLID SECTION"):
                sec_mat = None
                for tok in line.split(','):
                    t = tok.strip()
                    if t.upper().startswith("MATERIAL="):
                        sec_mat = t.split('=', 1)[1].strip()
                legacy_mat_name = sec_mat
                if target_part is not None and current_part is not None \
                        and current_part.upper() == target_part:
                    design_mat_name = sec_mat
                    if i + 1 < len(lines):
                        model_data['Thickness_Line'] = lines[i+1]
                elif target_part is None:
                    if i + 1 < len(lines):
                        model_data['Thickness_Line'] = lines[i+1]
                continue

            if upper_line.startswith("*CLOAD"):
                is_reading_cload = True
                continue
            elif upper_line.startswith("*") and is_reading_cload:
                is_reading_cload = False

            if is_reading_cload and line.strip():
                parts = line.split(',')
                if len(parts) >= 3:
                    try:
                        model_data['Total_Force'] += abs(float(parts[2].strip()))
                    except ValueError:
                        pass

        # --- Reserved-name guard (Option A safety) ---
        import re as _re
        for nm in all_mat_names:
            u = nm.upper()
            if u == "SOLID" or u == "MAT_VOID" or _re.match(r'^FGL_\d+$', u):
                print("   [FATAL] Base INP defines a material named '{}', which collides".format(nm))
                print("           with an injected name (SOLID / MAT_VOID / FGL_xx).")
                print("           Rename that material in the base model and re-run.")
                sys.exit(1)

        # --- Resolve the design material's properties ---
        chosen_mat = design_mat_name if design_mat_name is not None else legacy_mat_name
        if target_part is not None and design_mat_name is None and chosen_mat is not None:
            print("   [WARNING] No *Solid Section found inside the design part; "
                  "falling back to the last section's material '{}'. "
                  "Verify the design material is correct.".format(chosen_mat))
        if chosen_mat is not None:
            model_data['Material_Name'] = chosen_mat
            model_data['Section_Name'] = chosen_mat
            props = materials.get(chosen_mat.upper())
            if props is not None:
                model_data['E_modulus'] = props[0]
                model_data['Poisson'] = props[1]

        # --- Yield Stress heuristic from the (design) E ---
        E = model_data['E_modulus']
        if E < 80000:
            model_data['Yield_Stress'] = 300.0
        elif E > 180000:
            model_data['Yield_Stress'] = 400.0
        else:
            model_data['Yield_Stress'] = 900.0

        print("   [FOUND] Material: {} | E: {} MPa | Assumed Yield: {} MPa".format(
            model_data['Material_Name'], model_data['E_modulus'], model_data['Yield_Stress']))
        print("   [FOUND] Linked Section: {}".format(model_data['Section_Name']))
        print("   [FOUND] Total Applied Force: {} N".format(model_data['Total_Force']))

        if model_data['Total_Force'] == 0.0:
            print("   [WARNING] No concentrated force found! Using 1.0 N for stiffness ratios.")
            model_data['Total_Force'] = 1.0

        return model_data

    except SystemExit:
        raise
    except Exception as e:
        print("   [CRITICAL WARNING] Master Parser Failed: {}. Using default fallbacks.".format(e))
        model_data['Total_Force'] = 1.0
        return model_data

# --- NEW: AUTO-DETECT MESH SIZE & FILTER RADIUS ---
def auto_detect_filter_radius(coord_data, multiplier=3):
    """
    Calculates the exact average element size by measuring the distance 
    between nearest-neighbor element centroids using a high-speed KD-Tree.
    """
    print("-> Auto-calculating Mesh Size and Filter Radius...")
    
    # Extract the 3D coordinates of all element centroids
    coords = np.array(list(coord_data.values()))
    
    # Build a fast spatial tree
    tree = cKDTree(coords)
    
    # Query the tree for the 2 closest points to every element.
    # (k=2 because the 1st closest point is the element itself at distance 0)
    distances, _ = tree.query(coords, k=2)
    
    # Extract the distance to the 2nd point (the actual nearest neighbor)
    nearest_neighbor_distances = distances[:, 1]
    
    # The average distance between centroids is our effective element size
    avg_element_size = np.mean(nearest_neighbor_distances)
    
    # Calculate the final filter radius
    filter_radius = avg_element_size * multiplier
    
    print("   [FOUND] Average Element Size: {:.3f} mm".format(avg_element_size))
    print("   [SET] Filter Radius ({}x multiplier): {:.3f} mm".format(multiplier, filter_radius))
    
    return filter_radius

# --- 4. THE MASTER LATTICE BINNER (SMART QUOTA + TARGET SEEKING) ---
def build_skin_topology(connectivity, centroids):
    """
    Build an exact element face-adjacency graph and the set of boundary
    (free-surface) elements for the morphological skin. Replaces the old practice
    of borrowing the smoothing filter's neighbour sphere, which coupled the skin
    thickness to the filter radius and over-detected element corners.

    Two elements are neighbours only if they share a whole face (3D hex: 4 shared
    corner nodes) or a whole edge (2D quad: 2 shared corner nodes). An element is
    on the boundary if at least one of its faces/edges has no neighbour across it.

    Dimensionality is detected from the z-spread of the centroids (exactly 0 for a
    planar model, > 0 for any solid). Tet meshes are not yet supported and raise.
    """
    zs = [c[2] for c in centroids.values()]
    is_2d = ((max(zs) - min(zs)) < 1e-6) if zs else False
    if is_2d:
        n_corner, n_sides, share_min, allowed, label = 4, 4, 2, (4, 8), "2D quad"
    else:
        n_corner, n_sides, share_min, allowed, label = 8, 6, 4, (8, 20), "3D hex"

    node_to_elems = {}
    corners = {}
    for eid, nodes in connectivity.items():
        if len(nodes) not in allowed:
            raise ValueError(
                "Smart Skin supports {} elements only (got a {}-node element, id {}). "
                "Tet meshes are not yet supported.".format(label, len(nodes), eid))
        cn = nodes[:n_corner]
        corners[eid] = cn
        for n in cn:
            if n not in node_to_elems:
                node_to_elems[n] = []
            node_to_elems[n].append(eid)

    face_adjacency = {}
    for eid in connectivity:
        face_adjacency[eid] = []
    boundary_set = set()
    for eid, cn in corners.items():
        shared = {}
        for n in cn:
            for other in node_to_elems[n]:
                if other != eid:
                    shared[other] = shared.get(other, 0) + 1
        nbrs = [o for o, c in shared.items() if c >= share_min]
        face_adjacency[eid] = nbrs
        if len(nbrs) < n_sides:
            boundary_set.add(eid)

    print("   Skin topology: {} design elements, {} on the boundary ({}).".format(
        len(connectivity), len(boundary_set), label))
    return face_adjacency, boundary_set, is_2d


def apply_final_skin(density_map, face_adjacency, boundary_set, is_2d, skin_thickness=1):
    """
    Apply the solid skin as a ONE-SHOT post-process on the converged density map.
    The optimisation itself runs as pure lattice; the shell is sealed only here, on
    the final geometry, so the optimiser keeps its best specific-stiffness core and
    the skin wraps just the material that survived (not the original block).

    An exposed MATERIAL element (on the outer boundary, or face-adjacent to a void
    that must be sealed) is hardened to solid; the shell then grows skin_thickness
    layers inward through material. The void-sealing rule depends on dimensionality:
      3D: seal the outer boundary + AIR-connected (external) void walls only;
          internal trapped voids stay porous lattice (powder escape, dead weight).
      2D: a planar 'internal' void is physically a through-hole / tunnel whose wall
          is a real surface, so seal EVERY void wall plus the outer boundary.
    Returns a NEW density map (the input is not mutated) and the skinned count.
    """
    skin_boundary = boundary_set if boundary_set is not None else set()
    material = set(eid for eid, d in density_map.items() if d > 0)
    voids = set(eid for eid, d in density_map.items() if d == 0)

    if is_2d:
        skin_voids = set(voids)
    else:
        # 3D: keep only air-connected voids via flood-fill from the boundary voids.
        skin_voids = set()
        queue = []
        for eid in voids:
            if eid in skin_boundary:
                skin_voids.add(eid)
                queue.append(eid)
        while queue:
            cur = queue.pop(0)
            for nb in face_adjacency.get(cur, []):
                if nb in voids and nb not in skin_voids:
                    skin_voids.add(nb)
                    queue.append(nb)

    exposed = set()
    for eid in material:
        if eid in skin_boundary:
            exposed.add(eid)
            continue
        for nb in face_adjacency.get(eid, []):
            if nb in skin_voids:
                exposed.add(eid)
                break

    skin = set(exposed)
    frontier = set(exposed)
    for _layer in range(max(0, skin_thickness - 1)):
        nxt = set()
        for eid in frontier:
            for nb in face_adjacency.get(eid, []):
                if nb in material and nb not in skin:
                    nxt.add(nb)
        if not nxt:
            break
        skin |= nxt
        frontier = nxt

    new_map = dict(density_map)
    for eid in skin:
        new_map[eid] = 100
    return new_map, len(skin)


def bins_from_density_map(density_map):
    """Convert a {element: density_pct} map into the SOLID/VOID/FGL_xx bin dict that
    create_lattice_inp and calculate_mass_fraction consume (mirrors the binner's
    Phase 4 finalisation)."""
    bins = {'SOLID': [], 'VOID': []}
    for i in range(5, 100):
        bins['FGL_{:02d}'.format(i)] = []
    for eid, dens in density_map.items():
        if dens == 100:
            bins['SOLID'].append(eid)
        elif dens == 0:
            bins['VOID'].append(eid)
        else:
            bins['FGL_{:02d}'.format(dens)].append(eid)
    return bins


def assign_lattice_bins(stabilized_mises_dict, iteration, yield_stress=900.0, is_continuous=True, non_design_list=[], previous_density_map=None, yield_exponent=1.5, yield_coeff=1.0, evolution_quota_pct=4.0, void_threshold_pct=10.0, move_limit=1.0, model="safe", cap=0.5, load_mode="scaled", safety_factor=1.0):
    if is_continuous:
        print("-> Applying Smart Target-Seeking Quota Binner ({:.0f}% Quota, {:.0f}% Move Limit)...".format(evolution_quota_pct, move_limit * 100))
    else:
        print("-> Applying Quota-Based Discrete Binner...")
    
    protected_elements = set(non_design_list)
    
    # 1. We still calculate this ONLY so your history graphs don't break!
    design_stresses = [stress for eid, stress in stabilized_mises_dict.items() if eid not in protected_elements]
    if not design_stresses: design_stresses = list(stabilized_mises_dict.values())
    reference_stress = np.percentile(design_stresses, 100)
    if load_mode == "absolute":
        scale_factor = 1.0
        sizing_multiplier = safety_factor
    else:
        scale_factor = yield_stress / reference_stress
        sizing_multiplier = 1.0
    
    # Initialize the dynamic 100-Bin Dictionary
    bins = {'SOLID': [], 'VOID': []}
    for i in range(5, 100):
        bins['FGL_{:02d}'.format(i)] = []

    # MASTER CONTROL PANEL (all values now come from parameters)
    upper_threshold = 1.0 if is_continuous else 0.65
    lower_threshold = void_threshold_pct / 100.0

    # Lattice/solid boundary depends on the active property model.
    if model == "safe":
        solid_threshold = cap
        lattice_ceil_pct = int(round(cap * 100.0)) - 1
    else:
        solid_threshold = 0.99
        lattice_ceil_pct = 99
        
    # =====================================================================
    # PHASE 1: UTILIZATION EVALUATION & SORTING
    # =====================================================================
    utilization_list = []
    # yield_exponent comes from parameter - must match inject_custom_fields

    for eid, raw_stress in stabilized_mises_dict.items():
        if eid in protected_elements:
            continue
            
        # Fetch previous density (Default to 1.0 for Iteration 1)
        prev_rho = (previous_density_map.get(eid, 100) / 100.0) if previous_density_map else 1.0
        
        # Clamp prev_rho slightly above zero to prevent dividing by absolute zero
        safe_prev_rho = max(prev_rho, 0.05)
        
        # Calculate Local Yield and Utilization
        local_yield = yield_stress * lattice_yield_ratio(safe_prev_rho, yield_coeff, yield_exponent, model, cap)
        utilization = raw_stress / local_yield
        
        # Store the data tuple: (Element ID, Utilization)
        utilization_list.append((eid, utilization))

    # SORT THE LIST from Lowest Utilization (Safest) to Highest (Failing)
    utilization_list.sort(key=lambda x: x[1])
    
    # =====================================================================
    # PHASE 2: CALCULATE QUOTAS & FILTER CANDIDATES (DEADLOCK FIX)
    # =====================================================================
    total_design_elements = len(utilization_list)
    
    # Set the quota limit from user parameter (e.g., 4% of total design space)
    quota_count = max(1, int((evolution_quota_pct / 100.0) * total_design_elements))
    
    # Step 1: Filter candidates to prevent Quota Starvation
    shrink_candidates = []
    grow_candidates = []
    
    for item in utilization_list:
        eid = item[0]
        # Get the current density percentage (Default to 100 if Iteration 1)
        prev_rho_pct = previous_density_map.get(eid, 100) if previous_density_map else 100
        
        # Only allow shrinking if it's not already a Void
        threshold_pct = int(lower_threshold * 100)
        if prev_rho_pct > threshold_pct:
            shrink_candidates.append(eid)
            
        # Only allow growing if it's not already a Solid (>= 100%)
        if prev_rho_pct < 100:
            grow_candidates.append(eid)

    # Step 2: Draft the elements safely using the filtered lists
    elements_to_remove = set(shrink_candidates[:quota_count])
    elements_to_add = set(grow_candidates[-quota_count:])

    # Step 3: Report the active physics to the console
    print("   Total Design Elements: {}".format(total_design_elements))
    print("   Active Candidates: {} Shrinkable | {} Growable".format(len(shrink_candidates), len(grow_candidates)))
    print("   Quota Limit: {} elements per phase.".format(quota_count))

    # =====================================================================
    # PHASE 3: EXECUTE THE SMART TARGET SEEKING (20% MOVE LIMIT)
    # =====================================================================
    provisional_lattice = set()
    provisional_void = set()
    element_density_map = {} 

    for eid, raw_stress in stabilized_mises_dict.items():
        
        # 1. Override for Non-Design Space
        if eid in protected_elements:
            element_density_map[eid] = 100
            continue
            
        # 2. Grab the previous density from memory
        prev_rho = (previous_density_map.get(eid, 100) / 100.0) if previous_density_map else 1.0
        
        # 3. Apply the Structural Logic
        if is_continuous:
            # --- CONTINUOUS FGL MODE (Target Seeking + 100% Move Limit) ---
            scaled_stress = raw_stress * scale_factor
            target_rho = invert_yield_ratio(scaled_stress * sizing_multiplier / yield_stress, yield_coeff, yield_exponent, model)
            
            if eid in elements_to_remove:
                if target_rho < prev_rho:
                    final_rho = max(target_rho, prev_rho - move_limit)
                else:
                    final_rho = prev_rho 
            elif eid in elements_to_add:
                if target_rho > prev_rho:
                    final_rho = min(target_rho, prev_rho + move_limit)
                else:
                    final_rho = prev_rho 
            else:
                final_rho = prev_rho 
                
        else:
            # --- PURE 2-BIN SOLID/VOID MODE (Classic BESO) ---
            if eid in elements_to_remove:
                final_rho = 0.0   # Instant Kill (Void)
            elif eid in elements_to_add:
                final_rho = 1.0   # Instant Solidify (Solid)
            else:
                final_rho = prev_rho # Mathematically Frozen
                
        # Clamp to ensure we don't accidentally get 110% or -10%
        final_rho = max(0.0, min(1.0, final_rho))
        
        # 4. Bin the Final Result
        if final_rho >= solid_threshold:       
            element_density_map[eid] = 100
        elif final_rho <= lower_threshold:
            provisional_void.add(eid)
            element_density_map[eid] = 0
        else:
            provisional_lattice.add(eid)
            density_pct = int(round(final_rho * 100.0))
            # The lowest FGL bin must be exactly 1% higher than the void threshold
            floor_pct = int(lower_threshold * 100) + 1
            density_pct = max(floor_pct, min(lattice_ceil_pct, density_pct)) 
            element_density_map[eid] = density_pct

    # The morphological skin is no longer applied per-iteration. It is now a
    # one-shot post-process (apply_final_skin) on the converged design, so the
    # optimiser runs as a clean pure-lattice solver and the binner stays pure.

    # =====================================================================
    # PHASE 4: FINALIZE BINS & TRACK DISTRIBUTION (Executes for both paths!)
    # =====================================================================
    for eid, dens in element_density_map.items():
        if dens == 100: bins['SOLID'].append(eid)
        elif dens == 0: bins['VOID'].append(eid)
        else: bins['FGL_{:02d}'.format(dens)].append(eid)

    # --- DISTRIBUTION TRACKER ---
    dist_counts = {
        'VOID (0%)': 0, '10-20%': 0, '21-30%': 0, '31-40%': 0, 
        '41-50%': 0, '51-60%': 0, '61-70%': 0, '71-80%': 0, 
        '81-90%': 0, '91-99%': 0, 'SOLID (100%)': 0
    }
    
    for dens in element_density_map.values():
        if dens == 0: dist_counts['VOID (0%)'] += 1
        elif dens <= 20: dist_counts['10-20%'] += 1
        elif dens <= 30: dist_counts['21-30%'] += 1
        elif dens <= 40: dist_counts['31-40%'] += 1
        elif dens <= 50: dist_counts['41-50%'] += 1
        elif dens <= 60: dist_counts['51-60%'] += 1
        elif dens <= 70: dist_counts['61-70%'] += 1
        elif dens <= 80: dist_counts['71-80%'] += 1
        elif dens <= 90: dist_counts['81-90%'] += 1
        elif dens <= 99: dist_counts['91-99%'] += 1
        elif dens == 100: dist_counts['SOLID (100%)'] += 1

    print("\n   --- ELEMENT DENSITY DISTRIBUTION ---")
    for label in ['VOID (0%)', '10-20%', '21-30%', '31-40%', '41-50%', '51-60%', '61-70%', '71-80%', '81-90%', '91-99%', 'SOLID (100%)']:
        print("      {:<15}: {}".format(label, dist_counts[label]))
    print("   ------------------------------------")

    return bins, reference_stress, scale_factor, element_density_map

# --- 5. HIGH-SPEED RAM INP GENERATOR (MULTI-BIN UPGRADE) ---
def write_elset(file_object, set_name, element_list):
    # Only write the set if it actually has elements inside it!
    if len(element_list) == 0:
        return
        
    file_object.write("*Elset, elset={}\n".format(set_name))
    count = 0
    for i, elem_id in enumerate(element_list):
        file_object.write(str(elem_id))
        count += 1
        if count == 16 or i == len(element_list) - 1:
            file_object.write("\n")
            count = 0
        else:
            file_object.write(", ")

def create_lattice_inp(base_inp_lines, new_inp_name, bins_dict, global_data, design_part_name=None, stiffness_exponent=2.0, stiffness_coeff=1.0, model="safe", cap=0.5):
    print("-> Generating next lattice input file: {}...".format(new_inp_name))

    thickness_line = global_data['Thickness_Line']
    E_solid = global_data['E_modulus']
    nu = global_data['Poisson']

    print("   -> Applying Design Material: E = {} MPa, v = {} | C1 = {} n1 = {} | model = {}".format(E_solid, nu, stiffness_coeff, stiffness_exponent, model))

    # Multi-part: only the DESIGN part's section is replaced by the lattice bins.
    # Non-design parts (e.g. the rocker) keep their original section + material.
    # Original materials are PRESERVED (Option A); we only ADD lattice materials.
    target_part = design_part_name.upper() if design_part_name else None

    with open(new_inp_name, 'w') as f_out:
        skip_next_line = False
        materials_written = False
        sections_written = False
        current_part = None

        for line in base_inp_lines:
            upper_line = line.strip().upper()

            # Consume the data line under a design-part section we just replaced.
            if skip_next_line:
                skip_next_line = False
                continue

            # --- PART TRACKING ---
            if upper_line.startswith("*PART"):
                current_part = None
                for tok in line.strip().split(','):
                    t = tok.strip()
                    if t.upper().startswith("NAME="):
                        current_part = t.split('=', 1)[1].strip()
                f_out.write(line)
                continue
            if upper_line.startswith("*END PART"):
                current_part = None
                f_out.write(line)
                continue

            # --- MATERIAL INJECTION (GIBSON-ASHBY) at the first *Step ---
            if upper_line.startswith("*STEP") and not materials_written:
                f_out.write("*Material, name=SOLID\n")
                f_out.write("*Elastic\n")
                f_out.write("{:.2f}, {}\n".format(E_solid, nu))

                f_out.write("*Material, name=MAT_VOID\n")
                f_out.write("*Elastic\n")
                f_out.write("1.0, {}\n".format(nu))

                used_mats = [k for k, v in bins_dict.items() if k.startswith('FGL_') and len(v) > 0]
                for mat_name in used_mats:
                    density_pct = int(mat_name.split('_')[1])
                    relative_density = density_pct / 100.0
                    e_modulus = E_solid * lattice_modulus_ratio(relative_density, stiffness_coeff, stiffness_exponent, model, cap)
                    f_out.write("*Material, name={}\n".format(mat_name))
                    f_out.write("*Elastic\n")
                    f_out.write("{:.2f}, {}\n".format(e_modulus, nu))

                materials_written = True
                f_out.write(line)
                continue

            # --- SECTION ASSIGNMENT ---
            if upper_line.startswith("*SOLID SECTION"):
                is_design = (target_part is None) or \
                            (current_part is not None and current_part.upper() == target_part)
                if is_design:
                    if not sections_written:
                        for bin_name, elem_list in bins_dict.items():
                            write_elset(f_out, "SET_" + bin_name, elem_list)
                        for bin_name, elem_list in bins_dict.items():
                            if len(elem_list) > 0:
                                mat_assign = bin_name
                                if bin_name == 'VOID': mat_assign = 'MAT_VOID'
                                if bin_name == 'SOLID': mat_assign = 'SOLID'
                                f_out.write("*Solid Section, elset=SET_{}, material={}\n".format(bin_name, mat_assign))
                                f_out.write(thickness_line)
                        sections_written = True
                    skip_next_line = True  # swallow the design section's data line
                    continue
                else:
                    # Non-design part: keep its section + material reference intact.
                    f_out.write(line)
                    continue

            f_out.write(line)

# --- 8. MASS EVALUATION FUNCTION ---
def calculate_mass_fraction(bins_dict):
    print("\n========================================")
    print("   CALCULATING FINAL OPTIMIZED MASS   ")
    print("========================================")
    
    total_elements = sum([len(lst) for lst in bins_dict.values()])
    
    # 1. Sum up Solid and Void explicitly
    equivalent_solid_elements = len(bins_dict.get('SOLID', [])) * 1.0
    equivalent_solid_elements += len(bins_dict.get('VOID', [])) * 0.0
    
    # 2. Mathematically integrate the Functionally Graded Core
    for bin_name, elem_list in bins_dict.items():
        if bin_name.startswith('FGL_'):
            density_pct = float(bin_name.split('_')[1])
            equivalent_solid_elements += len(elem_list) * (density_pct / 100.0)
    
    mass_fraction = equivalent_solid_elements / total_elements
    weight_reduction = (1.0 - mass_fraction) * 100.0
    
    print("   Original Solid Elements: {}".format(total_elements))
    print("   Optimized Equivalent Elements: {:.2f}".format(equivalent_solid_elements))
    print("   Final Mass Fraction: {:.2f} %".format(mass_fraction * 100))
    print("   Total Weight Reduction: {:.2f} %".format(weight_reduction))
    
    return mass_fraction, weight_reduction

# --- 9. STIFFNESS EVALUATION FUNCTION (v7: WEIGHTED-AVERAGE DISPLACEMENT) ---
def evaluate_specific_stiffness(odb_path, total_force, mass_fraction, step_weights=None):
    print("\n========================================")
    print("   CALCULATING SPECIFIC STIFFNESS   ")
    print("========================================")

    try:
        odb = openOdb(odb_path)

        # Lattice models carry no thermal step: every step is a mechanical load case.
        load_cases = list(odb.steps.values())
        num_lc = len(load_cases)
        if num_lc == 0:
            odb.close()
            print("[WARNING] No steps found in ODB!")
            return 0.0, 0.0, 0.0

        # Use the SAME weighting that get_mises_and_coords applies to the stress
        # field, so the metric that picks the global winner is consistent with the
        # field that drives the optimisation. Fall back to equal weights.
        if step_weights is None or len(step_weights) != num_lc:
            step_weights = [1.0 / num_lc] * num_lc

        # Weighted-average max displacement across all load cases (solid-void v7).
        weighted_disp = 0.0
        for i, step in enumerate(load_cases):
            last_frame = step.frames[-1]
            if 'U' not in last_frame.fieldOutputs.keys():
                print("   [WARNING] LC {} has no 'U' field. Skipping.".format(step.name))
                continue
            u_field = last_frame.fieldOutputs['U']
            max_u_step = max([val.magnitude for val in u_field.values])
            print("   LC {:30s} max disp = {:.5f} mm  (weight {:.3f})".format(
                step.name, max_u_step, step_weights[i]))
            weighted_disp += step_weights[i] * max_u_step

        odb.close()

        if weighted_disp == 0.0:
            print("[WARNING] Zero weighted displacement. Cannot calculate stiffness.")
            return 0.0, 0.0, 0.0

        # Absolute Stiffness (K = Force / Weighted Displacement)
        stiffness = total_force / weighted_disp

        # Specific Stiffness (K / Mass Fraction)
        specific_stiffness = stiffness / mass_fraction

        print("   Applied Force: {:.1f} N".format(total_force))
        print("   Weighted Displacement: {:.5f} mm".format(weighted_disp))
        print("   Absolute Stiffness (K): {:.2f} N/mm".format(stiffness))
        print("   Specific Stiffness (K/Mass): {:.2f} (N/mm)/%".format(specific_stiffness))

        return weighted_disp, stiffness, specific_stiffness

    except Exception as e:
        print("[ERROR] Could not calculate stiffness: {}".format(e))
        return 0.0, 0.0, 0.0

# --- 10. ODB FIELD INJECTOR (THE ADVISOR'S UPGRADE) ---
def inject_custom_fields(odb_name, design_part_name, stress_dict, yield_stress, scale_factor=1.0, density_dict=None, yield_exponent=1.5, yield_coeff=1.0, model="safe", cap=0.5):
    print("-> Injecting custom 'TRUE_UTILIZATION' and 'DENSITY' fields into {}...".format(odb_name))
    try:
        # Open ODB with Write Permissions!
        odb = openOdb(odb_name, readOnly=False)

        last_step = list(odb.steps.values())[-1]
        last_frame = last_step.frames[-1]
        instance = find_odb_design_instance(odb, design_part_name)

        # Define BOTH new variables
        util_field = last_frame.FieldOutput(name="TRUE_UTILIZATION", description="Scaled Stress / Local Lattice Yield", type=SCALAR)
        dens_field = last_frame.FieldOutput(name="DENSITY", description="Material Density Percentage", type=SCALAR)

        labels = []
        util_data = []
        dens_data = []

        # yield_exponent comes from parameter - MUST MATCH assign_lattice_bins!

        for eid, stress in stress_dict.items():
            labels.append(eid)

            # 1. Fetch density and prevent dividing by absolute zero
            dens_val = density_dict.get(eid, 100) if density_dict is not None else 100
            safe_rho = max(dens_val / 100.0, 0.05)
            
            # 2. Recreate the exact algorithmic logic!
            local_yield = yield_stress * lattice_yield_ratio(safe_rho, yield_coeff, yield_exponent, model, cap)
            scaled_stress = stress * scale_factor
            true_utilization = scaled_stress / local_yield

            util_data.append((true_utilization, )) 
            dens_data.append((float(dens_val), ))

        # Inject them into the elements at the integration point!
        util_field.addData(position=INTEGRATION_POINT, instance=instance, labels=tuple(labels), data=tuple(util_data))
        dens_field.addData(position=INTEGRATION_POINT, instance=instance, labels=tuple(labels), data=tuple(dens_data))

        odb.save()
        odb.close()
        print("   [SUCCESS] Custom fields permanently written to database.")

    except Exception as e:
        print("   [WARNING] Could not inject custom fields. Error: {}".format(e))
        try:
            odb.close()
        except:
            pass

def load_config_or_prompt():
    """
    Tries to load beso_config.json written by the launcher (with engine='lattice').
    Falls back to the original raw_input interactive menu if no config exists,
    preserving full backwards compatibility for direct script execution.
    """
    if os.path.exists(CONFIG_FILE):
        print("\n" + "="*50)
        print("   ADAMASTOR LATTICE OPTIMIZATION ENGINE")
        print("="*50)
        print("-> Loading configuration from {}...".format(CONFIG_FILE))

        try:
            with open(CONFIG_FILE, 'r') as f:
                cfg = json.load(f)

            # Only consume configs written for this engine
            if cfg.get("engine", "solid_void") != "lattice":
                print("   [WARNING] Config file is for the solid-void engine. Falling back to interactive menu.\n")
            else:
                opt = cfg.get("optimization", {})
                adv = cfg.get("advanced", {})
                mdl = cfg.get("model", {})

                is_gaussian_filter   = bool(opt.get("is_gaussian_filter", True))
                is_continuous        = bool(opt.get("is_continuous", True))
                enable_skin          = bool(opt.get("enable_skin", True))
                skin_thickness       = int(opt.get("skin_thickness", 1))
                custom_weights       = opt.get("custom_weights", None)
                yield_stress         = float(opt.get("yield_stress", 900.0))

                # --- Lattice Gibson-Ashby resolution ---
                # WYSIWYG: when the launcher wrote a lattice_type it also wrote the
                # four coefficients it showed on screen; use those verbatim, with the
                # shared table as the default source. Legacy configs (no lattice_type)
                # keep the original pure power law (C=1) so old runs reproduce exactly.
                if "lattice_type" in opt:
                    lattice_type = str(opt.get("lattice_type", DEFAULT_LATTICE_TYPE))
                    lt_key, dC1, dn1, dC2, dn2 = resolve_lattice_properties(lattice_type)
                    lattice_type    = lt_key
                    stiffness_coeff = float(opt.get("stiffness_coeff", dC1))
                    gibson_ashby_exp= float(opt.get("gibson_ashby_exponent", dn1))
                    yield_coeff     = float(opt.get("yield_coeff", dC2))
                    yield_exponent  = float(opt.get("yield_exponent", dn2))
                else:
                    lattice_type    = "custom"
                    stiffness_coeff = 1.0
                    gibson_ashby_exp= float(opt.get("gibson_ashby_exponent", 2.0))
                    yield_coeff     = 1.0
                    yield_exponent  = float(opt.get("yield_exponent", 1.5))

                evolution_quota_pct  = float(opt.get("evolution_quota_pct", 4.0))
                void_threshold_pct   = float(opt.get("void_threshold_pct", 10.0))
                move_limit           = float(opt.get("move_limit", 1.0))

                property_model       = str(opt.get("lattice_property_model", "safe")).strip().lower()
                if property_model not in ("safe", "experimental"):
                    property_model = "safe"
                lattice_cap          = float(opt.get("lattice_cap", 0.5))
                load_mode            = str(opt.get("load_mode", "scaled")).strip().lower()
                if load_mode not in ("scaled", "absolute"):
                    load_mode = "scaled"
                if not is_continuous:
                    load_mode = "absolute"
                safety_factor        = float(opt.get("safety_factor", 1.0))

                base_job             = str(mdl.get("base_job", "beam_beso_base"))
                inp_path             = str(mdl.get("inp_path", "")).strip()
                max_iterations       = int(adv.get("max_iterations", 200))
                filter_multiplier    = int(adv.get("filter_multiplier", 3))
                early_stop_warmup    = int(adv.get("early_stop_warmup", 15))
                early_stop_patience  = int(adv.get("early_stop_patience", 10))
                early_stop_flatline  = float(adv.get("early_stop_flatline", 0.001))
                cpus                 = int(adv.get("cpus", 4))
                memory_percent       = int(adv.get("memory_percent", 90))

                print("   Timestamp:           {}".format(cfg.get("timestamp", "unknown")))
                print("   Base Job:            {}".format(base_job))
                print("   Model INP:           {}".format(
                      inp_path if inp_path else "(not set, will look next to the scripts)"))
                print("   Filter:              {}".format("Gaussian" if is_gaussian_filter else "Linear"))
                print("   Lattice Philosophy:  {}".format("Continuous FGL" if is_continuous else "Discrete 3-Bin"))
                print("   Smart Skin:          {}".format("Enabled" if enable_skin else "Disabled"))
                if enable_skin:
                    print("   Skin Thickness:      {} element layer(s)".format(skin_thickness))
                print("   Lattice Type:        {}".format(lattice_type))
                print("   Stiffness (C1,n1):   {:.2f}, {:.2f}".format(stiffness_coeff, gibson_ashby_exp))
                print("   Yield     (C2,n2):   {:.2f}, {:.2f}".format(yield_coeff, yield_exponent))
                print("   Yield Stress:        {} MPa".format(yield_stress))
                print("   Evolution Quota:     {}%".format(evolution_quota_pct))
                print("   Void Threshold:      {}%".format(void_threshold_pct))
                print("   Move Limit:          {:.0f}%".format(move_limit * 100))
                print("   Property Model:      {}".format(property_model))
                print("   Lattice Cap:         {:.2f}".format(lattice_cap))
                print("   Sizing Mode:         {}".format("Sized to Load (absolute)" if load_mode == "absolute" else "Max Specific Stiffness (scaled)"))
                if load_mode == "absolute":
                    print("   Safety Factor:       {:.2f}".format(safety_factor))
                print("   Max Iterations:      {}".format(max_iterations))
                print("   Filter Multiplier:   {}x".format(filter_multiplier))
                print("   Early Stop Warm-up:  {} iters".format(early_stop_warmup))
                print("   Early Stop Patience: {} iters".format(early_stop_patience))
                print("   Early Stop Flatline: {:.1f}%".format(early_stop_flatline * 100))
                print("   CPUs:                {}".format(cpus))
                print("   Memory:              {}%".format(memory_percent))
                if custom_weights:
                    print("   Custom Weights:      {}".format(custom_weights))
                print("\n" + "="*50 + "\n")

                return (is_gaussian_filter, is_continuous, enable_skin,
                        custom_weights, base_job, inp_path, max_iterations, filter_multiplier, cpus,
                        memory_percent, yield_stress, gibson_ashby_exp, yield_exponent,
                        evolution_quota_pct, void_threshold_pct, move_limit,
                        early_stop_warmup, early_stop_patience, early_stop_flatline,
                        lattice_type, stiffness_coeff, yield_coeff,
                        property_model, lattice_cap,
                        load_mode, safety_factor, skin_thickness)

        except Exception as e:
            print("   [WARNING] Could not parse config file: {}".format(e))
            print("   Falling back to interactive menu.\n")

    # -------------------------------------------------------------------------
    # FALLBACK: original interactive menu
    # -------------------------------------------------------------------------
    print("\n" + "="*50)
    print("      ADAMASTOR LATTICE OPTIMIZATION ENGINE")
    print("="*50)

    # [v50] The base model is asked for FIRST, so a wrong path is caught before
    # working through every optimisation question.
    inp_path, base_job = prompt_for_base_inp()

    print("\nSelect Spatial Filter Philosophy:")
    print("  1. Linear (Sharper boundaries between lattice grades)")
    print("  2. Gaussian Bell (Smoother, wider stress gradients) - Better results with lattices")
    filter_choice = raw_input("Enter 1 or 2: ").strip()
    is_gaussian_filter = (filter_choice == '2')

    print("\nSelect Lattice Density Philosophy:")
    print("  1. Discrete 3-Bin (Solid, 20% Core, Void)")
    print("  2. Continuous Functionally Graded (15% to 100% Core)")
    phil_choice = raw_input("Enter 1 or 2: ").strip()
    is_continuous = (phil_choice == '2')

    if is_continuous:
        print("\nSelect Sizing Mode:")
        print("  1. Max Specific Stiffness (load-normalised; classic objective)")
        print("  2. Sized to Load (absolute; sizes to the real applied load and yield)")
        size_choice = raw_input("Enter 1 or 2: ").strip()
        load_mode = "absolute" if size_choice == "2" else "scaled"
    else:
        load_mode = "absolute"
        print("\n[Sizing Mode] Discrete philosophy -> Sized to Load assumed (scaling has no effect in discrete).")

    if load_mode == "absolute" and is_continuous:
        sf_raw = raw_input("Enter Safety Factor (default 1.0): ").strip()
        try:
            safety_factor = float(sf_raw) if sf_raw else 1.0
        except ValueError:
            safety_factor = 1.0
    else:
        safety_factor = 1.0

    print("\nEnable Morphological 'Smart Skin' (Flood Fill)?")
    print("  1. Yes (Generates a protective outer shell)")
    print("  2. No (Generates a naked lattice core - Stiffer but exposed)")
    skin_choice = raw_input("Enter 1 or 2: ").strip()
    enable_skin = (skin_choice == '1')

    print("\nSelect Lattice Type (sets the Gibson-Ashby C1/n1/C2/n2):")
    _keys = list(LATTICE_PROPERTIES.keys())
    for _idx, _k in enumerate(_keys, 1):
        _p = LATTICE_PROPERTIES[_k]
        print("  {}. {:24s} C1={:.2f} n1={:.2f} | C2={:.2f} n2={:.2f}".format(
            _idx, _p["label"], _p["C1"], _p["n1"], _p["C2"], _p["n2"]))
    lat_choice = raw_input("Enter 1-{}: ".format(len(_keys))).strip()
    try:
        lattice_type = _keys[int(lat_choice) - 1]
    except (ValueError, IndexError):
        lattice_type = DEFAULT_LATTICE_TYPE
        print("   Invalid choice; using default '{}'.".format(DEFAULT_LATTICE_TYPE))
    lattice_type, stiffness_coeff, gibson_ashby_exp, yield_coeff, yield_exponent = \
        resolve_lattice_properties(lattice_type)
    print("   Selected: {}  (C1={:.2f}, n1={:.2f}, C2={:.2f}, n2={:.2f})".format(
        lattice_type, stiffness_coeff, gibson_ashby_exp, yield_coeff, yield_exponent))

    print("\nSelect Lattice Property Model:")
    print("  1. Safe (Gibson-Ashby capped at 0.50, denser becomes solid bulk) - recommended")
    print("  2. Experimental (endpoint-corrected blend, continuous to bulk, no cap)")
    model_choice = raw_input("Enter 1 or 2: ").strip()
    property_model = "experimental" if model_choice == "2" else "safe"
    lattice_cap = 0.5

    print("\n" + "="*50 + "\n")

    # Hardcoded defaults for fallback mode - match the defaults in the launcher
    return (is_gaussian_filter, is_continuous, enable_skin,
            None, base_job, inp_path, 200, 3, 1,
            90, 400.0, gibson_ashby_exp, yield_exponent,
            1, 10.0, 1.0,
            25, 15, 0.0001,
            lattice_type, stiffness_coeff, yield_coeff,
            property_model, lattice_cap,
            load_mode, safety_factor, 1)


# ==========================================
#              MASTER LOOP
# ==========================================
if __name__ == "__main__":

    # --- Run log: mirror all terminal output (stdout + stderr) to a .txt in Data_Files ---
    if not os.path.exists(DIR_DATA):
        os.makedirs(DIR_DATA)
    _run_log_path = os.path.join(DIR_DATA, "Run_Log.txt")
    _run_log_file = open(_run_log_path, "w")
    sys.stdout = _TeeLogger(sys.stdout, _run_log_file)
    sys.stderr = _TeeLogger(sys.stderr, _run_log_file)
    import atexit
    def _close_run_log():
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        try:
            _run_log_file.close()
        except Exception:
            pass
    atexit.register(_close_run_log)
    print("-> Run log: mirroring terminal output to {}".format(_run_log_path))

    (IS_GAUSSIAN, IS_CONTINUOUS, ENABLE_SKIN,
     _custom_weights, base_job, inp_path, MAX_ITERATIONS, FILTER_MULTIPLIER, _cpus,
     _memory_percent, _yield_stress, _gibson_ashby_exp, _yield_exponent,
     _evolution_quota_pct, _void_threshold_pct, _move_limit,
     _early_stop_warmup, _early_stop_patience, _early_stop_flatline,
     _lattice_type, _stiffness_coeff, _yield_coeff,
     _property_model, _lattice_cap,
     _load_mode, _safety_factor, _skin_thickness) = load_config_or_prompt()

    # --- OPTIMIZATION SETTINGS ---
    # (All values now come from config/menu above)

    # 0. Generate the folders (including scratch)
    print("\n-> Setting up clean directories...")
    for directory in [DIR_INP, DIR_ODB, DIR_MISC, DIR_DATA, DIR_SCRATCH]:
        if not os.path.exists(directory):
            os.makedirs(directory)

    # [v50] Resolve the base model, then stage a working copy. The lattice
    # engine has no resume yet, so prefer_archive is False; the argument exists
    # so the two engines stay symmetric when resume is added.
    _base_info = resolve_base_model(inp_path, base_job, DIR_INP,
                                    prefer_archive=False)
    if _base_info is None:
        sys.exit(1)
    base_job      = _base_info["job_name"]
    base_inp_path = _base_info["source"]

    _local_base_inp, _staged_is_copy = stage_base_model(base_inp_path, base_job)
    write_base_manifest(DIR_DATA, {
        "original_path": _base_info["source"],
        "found_via":     _base_info["origin"],
        "job_name":      base_job,
        "size_bytes":    _base_info["size"],
        "md5":           _base_info["md5"],
        "recorded":      time.strftime("%Y-%m-%d %H:%M:%S")})

    current_job = base_job
    current_inp = base_job
    
    # --- 1. THE AUTONOMOUS EXTRACTION ---
    # We call the master parser on the base INP file before doing anything else!
    global_design_part, global_non_design_list = find_design_part_and_non_design(base_inp_path, set_name="NON_DESIGN_SET")
    if len(global_non_design_list) == 0:
        print("   [WARNING] No NON_DESIGN_SET elements detected - the ENTIRE mesh will be")
        print("             treated as design space. If you intended a protected region,")
        print("             check the set name and its definition in the INP.")
    global_model_data = extract_global_inp_data(base_inp_path, global_design_part)
    
    # Use config yield stress (launcher populates it from the same heuristic, so
    # this transparently becomes a user override when they change it)
    YIELD_STRESS = _yield_stress
    TOTAL_APPLIED_FORCE = global_model_data['Total_Force']
    
    print("   [CONFIG] Yield Stress: {} MPa (auto-detected: {} MPa)".format(
        YIELD_STRESS, global_model_data['Yield_Stress']))
    
    # --- GRAPH TRACKERS ---
    iteration_history = [0]        
    max_stress_history = []        
    scale_factor_history = []
    mass_fraction_history = [1.0] # Starts at 100% solid
    stiffness_history = []
    specific_stiffness_history = []
    
    # Global Winner Trackers
    best_iteration = 0
    max_specific_stiffness = 0.0
    best_density_map = None 

    current_density_map = None

    previous_stabilized_mises = None
    start_time = time.time()
    
    print("\n========================================")
    print("       INITIALIZING LATTICE ENGINE      ")
    print("========================================")
    
    # 1. Run the base 100% Solid simulation
    run_abaqus_job(current_job, current_inp, _cpus, _memory_percent)
    
    # 2. Extract initial Von Mises Stress (with custom load case weights if set)
    raw_mises_dict, initial_coords, initial_connectivity = get_mises_and_coords(current_job + ".odb", global_design_part, step_weights=_custom_weights)
    inject_custom_fields(current_job + ".odb", global_design_part, raw_mises_dict, YIELD_STRESS, scale_factor=1.0, density_dict=None, yield_exponent=_yield_exponent, yield_coeff=_yield_coeff, model=_property_model, cap=_lattice_cap)
    
    # 2.5 Extract Initial Stiffness Baseline!
    _, initial_k, initial_spec_k = evaluate_specific_stiffness(current_job + ".odb", TOTAL_APPLIED_FORCE, 1.0, step_weights=_custom_weights)
    stiffness_history.append(initial_k)
    specific_stiffness_history.append(initial_spec_k)
    
    # [v50] Was cleanup_files(keep_inp=True), which left the staged copy loose
    # in the working folder. The copy is now archived into INP_Files with the
    # rest of the artefacts, so nothing is left behind.
    archive_preflight_artifacts(base_job, DIR_INP, DIR_ODB, DIR_MISC, _staged_is_copy)

    # Track Initial Data
    initial_max_stress = max(raw_mises_dict.values())
    max_stress_history.append(initial_max_stress)
    scale_factor_history.append(YIELD_STRESS / initial_max_stress)

    # --- 3. AUTO-DETECT FILTER RADIUS ---
    DYNAMIC_FILTER_RADIUS = auto_detect_filter_radius(initial_coords, multiplier=FILTER_MULTIPLIER)

    # 4. Pre-compute the Spatial Filter using the new dynamic radius!
    master_weights, master_adjacency = prepare_filter_weights(initial_coords, DYNAMIC_FILTER_RADIUS, IS_GAUSSIAN)

    # Build the exact skin topology ONCE (face-adjacency + boundary). Independent of
    # the smoothing filter above; this is what the morphological skin uses.
    master_face_adjacency, master_boundary_set, master_is_2d = build_skin_topology(initial_connectivity, initial_coords)

    # Load Base INP into RAM for ultra-fast generation
    print("-> Loading base INP file into RAM...")
    # [v50] Reads the resolved source, not the staged copy: by this point the
    # copy has been archived into INP_Files by archive_preflight_artifacts.
    with open(base_inp_path, 'r') as f:
        ram_base_inp_lines = f.readlines()

    # (design part + non-design set already resolved above by find_design_part_and_non_design)

    # --- THE ITERATIVE LOOP ---
    for iteration in range(1, MAX_ITERATIONS + 1):
        print("\n========================================")
        print("      GENERATING DESIGN ITERATION {}      ".format(iteration))
        print("========================================")
        
        # 1. Apply Spatial Filter
        filtered_mises_dict = apply_filter(raw_mises_dict, master_weights)
        
        # 2. Apply Temporal Stabilization
        stabilized_mises_dict = {}
        if previous_stabilized_mises is None:
            stabilized_mises_dict = filtered_mises_dict
        else:
            for eid in filtered_mises_dict:
                stabilized_mises_dict[eid] = (filtered_mises_dict[eid] + previous_stabilized_mises[eid]) / 2.0
                
        previous_stabilized_mises = stabilized_mises_dict
        
        # 3. Sort elements into Lattice Bins & Apply Morphological Skin
        lattice_bins, current_reference_stress, current_scale_factor, current_density_map = assign_lattice_bins(
            stabilized_mises_dict,
            iteration,
            yield_stress=YIELD_STRESS,
            is_continuous=IS_CONTINUOUS,
            non_design_list=global_non_design_list,
            previous_density_map=current_density_map,
            yield_exponent=_yield_exponent,
            yield_coeff=_yield_coeff,
            evolution_quota_pct=_evolution_quota_pct,
            void_threshold_pct=_void_threshold_pct,
            move_limit=_move_limit,
            model=_property_model,
            cap=_lattice_cap,
            load_mode=_load_mode,
            safety_factor=_safety_factor
        )
        
        # 4. Generate the new INP file
        next_job = "iteration_{}".format(iteration)
        create_lattice_inp(ram_base_inp_lines, next_job + ".inp", lattice_bins, global_model_data, global_design_part, stiffness_exponent=_gibson_ashby_exp, stiffness_coeff=_stiffness_coeff, model=_property_model, cap=_lattice_cap)
        
        # 5. Run the new simulation
        run_abaqus_job(next_job, next_job, _cpus, _memory_percent)
        
        # 6. Extract the new stress field for the next loop (with custom weights if set)
        raw_mises_dict, _, _ = get_mises_and_coords(next_job + ".odb", global_design_part, step_weights=_custom_weights)
        inject_custom_fields(next_job + ".odb", global_design_part, raw_mises_dict, YIELD_STRESS, scale_factor=current_scale_factor, density_dict=current_density_map, yield_exponent=_yield_exponent, yield_coeff=_yield_coeff, model=_property_model, cap=_lattice_cap)
        
        # ---> THE NEW IN-LOOP EVALUATION <---
        current_mass_frac, _ = calculate_mass_fraction(lattice_bins)
        _, current_k, current_spec_k = evaluate_specific_stiffness(next_job + ".odb", TOTAL_APPLIED_FORCE, current_mass_frac, step_weights=_custom_weights)
        
        # Check if this is the new global winner!
        if current_spec_k > max_specific_stiffness:
            max_specific_stiffness = current_spec_k
            best_iteration = iteration
            best_density_map = current_density_map.copy()

        cleanup_files(next_job, keep_inp=False)
        
        # 7. Update Trackers 
        iteration_history.append(iteration)
        max_stress_history.append(current_reference_stress)
        scale_factor_history.append(current_scale_factor)
        mass_fraction_history.append(current_mass_frac)
        stiffness_history.append(current_k)
        specific_stiffness_history.append(current_spec_k)
        
        # --- THE DUAL-TRIGGER EARLY STOPPING BREAKER ---
        # Only start checking after the user-configured warm-up period
        if iteration > _early_stop_warmup:
            
            # TRIGGER 1: THE FLATLINE (Has it stopped meaningfully changing?)
            # Look back 5 iterations. Is the relative change below the flatline threshold?
            if len(specific_stiffness_history) >= 6:
                past_spec_k = specific_stiffness_history[-6]
                if past_spec_k > 0:
                    absolute_change = abs(current_spec_k - past_spec_k)
                    relative_change = absolute_change / past_spec_k
                    is_flatlined = (relative_change < _early_stop_flatline)
                else:
                    is_flatlined = False
            else:
                is_flatlined = False

            # TRIGGER 2: THE DISINTEGRATION (Is it actively destroying itself?)
            # Look at the last 5 steps. Did the score drop every single time?
            if len(specific_stiffness_history) >= 6:
                recent_scores = specific_stiffness_history[-6:] 
                is_dropping = all(recent_scores[i] < recent_scores[i-1] for i in range(1, 6))
            else:
                is_dropping = False

            # TRIGGER 3: THE STAGNATION (Has it lost the path?)
            # Has it failed to beat the global high score in patience iterations?
            iterations_since_best = iteration - best_iteration
            is_stagnant = (iterations_since_best >= _early_stop_patience)
            
            # --- THE "OR" GATE ---
            # If ANY of these conditions are True, kill the script!
            if is_flatlined or is_dropping or is_stagnant:
                print("\n" + "!"*50)
                print("*** OPTIMIZATION TERMINATED AUTOMATICALLY ***")
                
                # Tell the user exactly WHICH trigger killed the run
                if is_flatlined:
                    print(" Reason: Convergence. Performance flatlined (<{:.1f}% change).".format(_early_stop_flatline * 100))
                elif is_dropping:
                    print(" Reason: Disintegration. Score dropped for 5 consecutive iterations.")
                elif is_stagnant:
                    print(" Reason: Patience Exceeded. Failed to beat high score for {} iterations.".format(_early_stop_patience))
                
                print(" Global Peak Performance: Iteration {}.".format(best_iteration))
                print("!"*50)
                break

    # --- WRAP UP & GRAPH GENERATION ---
    total_time = round((time.time() - start_time) / 60, 2)

    print("\n========================================")
    print("             FINAL VERDICT              ")
    print("========================================")
    print("  Global Best Iteration: {}".format(best_iteration))
    print("  Peak Specific Stiffness: {:.2f} (N/mm)/%".format(max_specific_stiffness))
    print("  Open 'iteration_{}.odb' in Abaqus to view the optimal structure!".format(best_iteration))

    # =====================================================================
    # POST-PROCESS SKIN: seal the shell ONCE on the converged design.
    # The loop above ran as pure lattice, so it reached its best specific-stiffness
    # core. We now wrap the skin around that converged geometry, run one final FEA to
    # report the true (skinned) stiffness, and keep the skinned map as the deliverable.
    # =====================================================================
    final_density_map = best_density_map
    skinned_spec_k = None
    skinned_mass_frac = None
    if ENABLE_SKIN and best_density_map is not None:
        print("\n========================================")
        print("   APPLYING FINAL SKIN (POST-PROCESS)   ")
        print("========================================")
        skinned_map, skin_count = apply_final_skin(
            best_density_map, master_face_adjacency, master_boundary_set,
            master_is_2d, skin_thickness=_skin_thickness)
        print("   Mode: {} | Thickness: {} layer(s) | Elements hardened into skin: {}".format(
            "2D (every void wall sealed)" if master_is_2d else "3D (outer + air-void walls sealed)",
            _skin_thickness, skin_count))
        skinned_bins = bins_from_density_map(skinned_map)
        final_job = "final_skinned"
        create_lattice_inp(ram_base_inp_lines, final_job + ".inp", skinned_bins, global_model_data,
            global_design_part, stiffness_exponent=_gibson_ashby_exp, stiffness_coeff=_stiffness_coeff,
            model=_property_model, cap=_lattice_cap)
        run_abaqus_job(final_job, final_job, _cpus, _memory_percent)
        _raw_final, _, _ = get_mises_and_coords(final_job + ".odb", global_design_part, step_weights=_custom_weights)
        inject_custom_fields(final_job + ".odb", global_design_part, _raw_final, YIELD_STRESS,
            scale_factor=1.0, density_dict=skinned_map, yield_exponent=_yield_exponent,
            yield_coeff=_yield_coeff, model=_property_model, cap=_lattice_cap)
        skinned_mass_frac, _ = calculate_mass_fraction(skinned_bins)
        _, skinned_k, skinned_spec_k = evaluate_specific_stiffness(
            final_job + ".odb", TOTAL_APPLIED_FORCE, skinned_mass_frac, step_weights=_custom_weights)
        final_density_map = skinned_map
        _preskin_mf = (mass_fraction_history[best_iteration] * 100.0
                       if 0 <= best_iteration < len(mass_fraction_history) else float("nan"))
        print("\n   --- SKINNED DESIGN (final deliverable) ---")
        print("   Mass Fraction:        {:.2f} %  (pre-skin best: {:.2f} %)".format(
            skinned_mass_frac * 100.0, _preskin_mf))
        print("   Specific Stiffness:   {:.2f}  (pre-skin: {:.2f})".format(skinned_spec_k, max_specific_stiffness))
        print("   Open 'final_skinned.odb' in Abaqus to view the deliverable.")

    print("\n========================================")
    print("   GENERATING LATTICE HISTORY GRAPHS  ")
    print("========================================")
    
    # Draw Graph 1: Maximum Stress History 
    plt.figure(figsize=(10, 6))
    plt.plot(iteration_history, max_stress_history, marker='o', linestyle='-', color='r', linewidth=2)
    plt.axhline(y=YIELD_STRESS, color='k', linestyle='--', label='Target Yield Stress ({} MPa)'.format(YIELD_STRESS))
    plt.title('Reference Stress per Iteration')
    plt.xlabel('Iteration Number')
    plt.ylabel('Reference Stress (MPa)')
    plt.legend()
    plt.grid(True)
    plt.savefig(os.path.join(DIR_DATA, 'Max_Stress_History.png'), dpi=300)
    
    # Draw Graph 2: Specific Stiffness & Mass Fraction History (Dual Axis)
    fig, ax1 = plt.subplots(figsize=(10, 6))

    # --- AXIS 1: Specific Stiffness (Blue) ---
    color1 = 'b'
    ax1.set_xlabel('Iteration Number')
    ax1.set_ylabel('Specific Stiffness ((N/mm)/%)', color=color1)
    ax1.plot(iteration_history, specific_stiffness_history, marker='s', linestyle='-', color=color1, linewidth=2, label='Specific Stiffness')
    
    # Highlight the absolute best iteration with a gold star
    ax1.plot(best_iteration, max_specific_stiffness, marker='*', markersize=15, color='gold', label='Global Optimum (Iter {})'.format(best_iteration))
    if skinned_spec_k is not None:
        ax1.plot(best_iteration, skinned_spec_k, marker='D', markersize=10, color='darkred', label='Skinned final ({:.2f})'.format(skinned_spec_k))
    ax1.tick_params(axis='y', labelcolor=color1)
    ax1.grid(True)

    # --- AXIS 2: Mass Fraction (Green) ---
    ax2 = ax1.twinx()  # This creates a second Y-axis that shares the same X-axis
    color2 = 'g'
    ax2.set_ylabel('Mass Fraction (%)', color=color2)
    
    # Convert decimal mass fraction to percentage for the plot
    mass_pct_history = [m * 100.0 for m in mass_fraction_history]
    ax2.plot(iteration_history, mass_pct_history, marker='^', linestyle='--', color=color2, linewidth=2, label='Mass Fraction')
    if skinned_mass_frac is not None:
        ax2.plot(best_iteration, skinned_mass_frac * 100.0, marker='D', markersize=8, color='darkgreen', label='Skinned mass')
    ax2.tick_params(axis='y', labelcolor=color2)

    plt.title('Evolution of Specific Stiffness and Mass Fraction')
    
    # Place legends safely in the top corners so they don't overlap
    ax1.legend(loc='upper left')
    ax2.legend(loc='upper right')
    
    fig.tight_layout() # Ensures the right-side Y-label isn't cut off by the image border
    plt.savefig(os.path.join(DIR_DATA, 'Performance_History.png'), dpi=300)
    
    # Save Comprehensive CSV Data 
    csv_filename = os.path.join(DIR_DATA, 'Lattice_Optimization_Data.csv')
    with _csv_open(csv_filename) as file:
        writer = csv.writer(file)
        writer.writerow(['Iteration', 'Reference_Stress_MPa', 'Scale_Factor', 'Mass_Fraction', 'Absolute_Stiffness_N/mm', 'Specific_Stiffness'])
        for i in range(len(iteration_history)):
            writer.writerow([
                iteration_history[i], 
                round(max_stress_history[i], 2), 
                round(scale_factor_history[i], 4),
                round(mass_fraction_history[i], 4),
                round(stiffness_history[i], 2),
                round(specific_stiffness_history[i], 2)
            ])

    # --- EXPORT THE 3D POINT CLOUD FOR THE GYROID GENERATOR ---
    manifest_line = format_lattice_manifest(
        _lattice_type, _stiffness_coeff, _gibson_ashby_exp, _yield_coeff, _yield_exponent,
        global_model_data['E_modulus'], YIELD_STRESS, global_model_data['Material_Name'])
    csv_export_path = os.path.join(DIR_DATA, 'Optimized_Density_Map.csv')
    if ENABLE_SKIN and best_density_map is not None:
        pre_dir = os.path.join(DIR_DATA, "Pre-Skinned Optimised Density Map")
        if not os.path.exists(pre_dir):
            os.makedirs(pre_dir)
        pre_path = os.path.join(pre_dir, 'Optimized_Density_Map.csv')
        export_density_to_csv(initial_coords, best_density_map, pre_path, manifest_line=manifest_line)
        print("   [SKIN] Pre-skinned (pure lattice) map archived to {}".format(pre_path))
    export_density_to_csv(initial_coords, final_density_map, csv_export_path, manifest_line=manifest_line)

    # --- SIZED-TO-LOAD EXTRAS: converged map export + design-set feasibility report ---
    if _load_mode == "absolute":
        if current_density_map is not None:
            last_dir = os.path.join(DIR_DATA, "Last_Iteration_Density_Map")
            if not os.path.exists(last_dir):
                os.makedirs(last_dir)
            last_export_path = os.path.join(last_dir, "Optimized_Density_Map.csv")
            export_density_to_csv(initial_coords, current_density_map, last_export_path, manifest_line=manifest_line)
            print("   [SIZED-TO-LOAD] Converged (last iteration) map saved to {}".format(last_export_path))

        if current_density_map is not None and raw_mises_dict:
            protected_ids = set(global_non_design_list)
            all_utils = []
            design_utils = []
            for _eid, _stress in raw_mises_dict.items():
                _rho = current_density_map.get(_eid, 100)
                _safe_rho = max(_rho / 100.0, 0.05)
                _ly = YIELD_STRESS * lattice_yield_ratio(_safe_rho, _yield_coeff, _yield_exponent, _property_model, _lattice_cap)
                _u = _stress / _ly
                all_utils.append(_u)
                if _eid not in protected_ids:
                    design_utils.append(_u)
            peak_overall = max(all_utils) if all_utils else 0.0
            peak_design = max(design_utils) if design_utils else 0.0
            pct99_design = float(np.percentile(design_utils, 99)) if design_utils else 0.0
            if peak_design <= 1.0:
                verdict = "Feasible: the converged design does not yield under the applied load."
            elif pct99_design <= 1.0:
                verdict = "Localised: a small fraction of design elements exceed yield, typically at support/load boundaries (boundary-condition artefact); the bulk of the structure is within yield. Inspect the TRUE_UTILIZATION field at those regions."
            else:
                verdict = "Overloaded: yielding is widespread across the design region."
            print("\n   --- FEASIBILITY (Sized-to-Load, converged design) ---")
            print("   Peak Utilisation (overall):               {:.3f}".format(peak_overall))
            print("   Peak Utilisation (design set):            {:.3f}".format(peak_design))
            print("   99th-percentile Utilisation (design set): {:.3f}".format(pct99_design))
            print("   Verdict: {}".format(verdict))

    print("\n*** OPTIMIZATION COMPLETED SUCCESSFULLY IN {} MINUTES! ***".format(total_time))
