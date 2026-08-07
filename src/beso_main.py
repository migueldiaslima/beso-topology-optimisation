import os
import sys
import time
import csv

# Python 2/3 compatibility: define raw_input on Python 3 where it was removed
try:
    raw_input
except NameError:
    raw_input = input

def _csv_open(path):
    if sys.version_info[0] >= 3:
        return open(path, 'w', newline='')
    return open(path, 'wb')

_design_material_fallback_warned = False

# =============================================================
# TEE LOGGER  -- mirrors stdout to a plain-text log file
# =============================================================
class _Tee(object):
    """Writes every print() call to both the terminal and a log file.
    Python 2.7 compatibility: softspace is required on anything assigned to sys.stdout."""
    softspace = 0   # Python 2.7 print statement checks for this attribute

    def __init__(self, log_path):
        self._terminal = sys.stdout
        self._log      = open(log_path, 'w')   # ASCII text mode; fine on both py2 and py3

    def write(self, message):
        self._terminal.write(message)
        self._log.write(message)

    def flush(self):
        self._terminal.flush()
        self._log.flush()

    def close(self):
        sys.stdout = self._terminal
        self._log.flush()
        self._log.close()

import math
import shutil
import json
import hashlib
from beso_geometry_dump import extract_element_connectivity, dump_best_geometry

# --- FOLDER SETUP ---
MASTER_DIR = "Latest_Classic_Run"
DIR_INP    = os.path.join(MASTER_DIR, "INP_Files")
DIR_ODB    = os.path.join(MASTER_DIR, "ODB_Files")
DIR_MISC   = os.path.join(MASTER_DIR, "Misc_Abaqus_Files")
DIR_DATA   = os.path.join(MASTER_DIR, "Data_Files")
DIR_SCRATCH= os.path.join(MASTER_DIR, "Scratch_Temp")

# =============================================================
# 0.5 BASE MODEL RESOLUTION AND STAGING  [v50]
# -------------------------------------------------------------
# The base model used to be a bare stem ("beam_beso_base") opened relative to
# the working directory. That forced the INP to sit next to the scripts, and
# the same string doubled as the Abaqus job name, so any file name with a
# space or a leading digit broke the solver call and every Abaqus artefact was
# written beside the user's own model.
#
# It is now resolved from an explicit path, given a sanitised job name, and
# copied into the working directory before solving, so the user's file can
# live anywhere, can be called anything, and is never written to.
#
# Resolution order:
#   1. the archived copy in INP_Files (resume/continue only: a run folder owns
#      the model it was built from, so moving or renaming the original later
#      cannot break a resume),
#   2. model.inp_path from beso_config.json,
#   3. base_job + ".inp" relative to the working directory (legacy behaviour,
#      so every existing config and run folder keeps working unchanged).
# =============================================================

BASE_MANIFEST_NAME  = "base_model.json"
PREFLIGHT_CACHE_NAME = "preflight_geometry.pkl"


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

    # A resume or continue pointed at a different model than the one this run
    # folder was built from is never recoverable, so stop instead of guessing.
    if prefer_archive and archived and inp_path and os.path.isfile(inp_path):
        arch_size, arch_md5 = file_signature(archived)
        conf_size, conf_md5 = file_signature(inp_path)
        if arch_md5 is not None and conf_md5 is not None and arch_md5 != conf_md5:
            print("\n   [v50][ERROR] Resume/continue conflict. This run folder was")
            print("   built from a different model than the one now configured.")
            print("     archived:   {}  ({}, md5 {})".format(
                  archived, describe_size(arch_size), arch_md5))
            print("     configured: {}  ({}, md5 {})".format(
                  os.path.abspath(inp_path), describe_size(conf_size), conf_md5))
            print("   Continuing across two different models would silently corrupt")
            print("   the result. Point inp_path back at the original model, or")
            print("   start a fresh run in a new folder.")
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
    """[v50] Tidy everything the pre-flight solve left in the working folder.
    The staged working copy goes to INP_Files (so the run folder keeps an exact
    snapshot of the model that produced it), the ODB to ODB_Files and the rest
    to Misc_Abaqus_Files. Previously the pre-flight artefacts were simply left
    loose, which is what the launcher's leftover cleanup existed to mop up."""
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
    print("-> [v50] Pre-flight artefacts tidied: {} file(s) moved out of the "
          "working folder.".format(len(moved)))
    return os.path.join(dir_inp, job_name + ".inp")


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


def write_base_manifest(dir_data, info):
    """Record which model this run folder belongs to, for resume provenance."""
    try:
        with open(os.path.join(dir_data, BASE_MANIFEST_NAME), "w") as f:
            json.dump(info, f, indent=2)
    except Exception as e:
        print("   [v50][WARN] Could not write the base model manifest: {}".format(e))


def read_base_manifest(dir_data):
    path = os.path.join(dir_data, BASE_MANIFEST_NAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None


# -------------------------------------------------------------
# PRE-FLIGHT GEOMETRY CACHE  [v50]
# The pre-flight solve exists only to read node coordinates, connectivity,
# element centroids and EVOL off the base model. None of that changes between
# launches, so it is cached and a resume/continue can skip the solve entirely.
#
# The cache is keyed on the SIZE AND MD5 OF THE BASE MODEL FILE ITSELF, checked
# against the file on disk at load time. A cache hit therefore proves the base
# model is byte-identical to the one that produced it, which proves the mesh is
# identical. Any difference at all, including a mesh change, misses the cache
# and falls back to a full pre-flight solve, after which the ordinary mesh
# fingerprint check runs as usual. The cache can never mask a changed mesh.
#
# It is only ever read on a resume or continue. A fresh run always re-solves
# and rewrites it, so a first run can never be affected by a stale cache.
# -------------------------------------------------------------
def _plain_float_map(source):
    """{id: float} with no Abaqus or numpy scalars, so the cache pickles small
    and reloads without depending on the solver session."""
    out = {}
    for key in source:
        out[key] = float(source[key])
    return out


def _plain_tuple_map(source):
    """{id: (x, y, z)} as plain floats."""
    out = {}
    for key in source:
        value = source[key]
        out[key] = (float(value[0]), float(value[1]), float(value[2]))
    return out


def _plain_connectivity(source):
    """{eid: (type_string, [node ids])} as plain str and int."""
    out = {}
    for key in source:
        etype, nodes = source[key]
        out[key] = (str(etype), [int(n) for n in nodes])
    return out


def save_preflight_cache(dir_data, key, payload):
    path = os.path.join(dir_data, PREFLIGHT_CACHE_NAME)
    try:
        handle = open(path + ".tmp", "wb")
        try:
            pickle.dump({"key": key, "payload": payload}, handle, protocol=2)
        finally:
            handle.close()
        if os.path.exists(path):
            os.remove(path)
        os.rename(path + ".tmp", path)
        print("-> [v50] Pre-flight geometry cached ({} elements, key md5 {}).".format(
              key.get("n_elements", "?"), key.get("md5")))
    except Exception as e:
        print("   [v50][WARN] Could not cache the pre-flight geometry: {}".format(e))


def load_preflight_cache(dir_data, key):
    """Return the cached pre-flight geometry ONLY if it was built from a
    byte-identical base model. Any mismatch returns None so the caller
    re-solves."""
    path = os.path.join(dir_data, PREFLIGHT_CACHE_NAME)
    if not os.path.isfile(path):
        print("   [v50] No pre-flight cache in {}; running the base solve."
              .format(dir_data))
        return None
    try:
        handle = open(path, "rb")
        try:
            blob = pickle.load(handle)
        finally:
            handle.close()
    except Exception as e:
        print("   [v50][WARN] Pre-flight cache unreadable ({}); running the base "
              "solve.".format(e))
        return None

    cached_key = blob.get("key", {})
    if cached_key.get("md5") != key.get("md5") or \
       cached_key.get("size") != key.get("size"):
        print("   [v50] Pre-flight cache was built from a DIFFERENT base model")
        print("         (cached md5 {}, current md5 {}). Ignoring the cache and".format(
              cached_key.get("md5"), key.get("md5")))
        print("         running the full base solve, so the mesh is measured for real.")
        return None

    payload = blob.get("payload")
    if not payload:
        return None
    print("-> [v50] Pre-flight cache hit: the base model is byte-identical to the")
    print("         one this folder was built from (md5 {}), so the base solve is".format(
          key.get("md5")))
    print("         skipped. {} elements restored from cache.".format(
          cached_key.get("n_elements", "?")))
    return payload


def abort_mesh_mismatch(context, cached_n, current_n, print_dir):
    """A checkpoint belonging to a different mesh is not recoverable. Stop,
    rather than falling through to a path with no fingerprint check."""
    print("\n   [v50][ERROR] Mesh fingerprint mismatch during {} for direction {}."
          .format(context, print_dir))
    print("   The saved state was built from a mesh with {} elements; the model".format(
          cached_n))
    print("   launched now has {} elements. These are different meshes, so the".format(
          current_n))
    print("   saved state cannot be applied to it.")
    print("   Either point the run back at the original model, or start a fresh")
    print("   run in a new folder. Aborting instead of guessing.")
    sys.exit(1)


# =============================================================
# 1. RUNNER
# =============================================================
def run_abaqus_job(job_name, input_name, cpus, memory_percent):
    print("-> Running Abaqus Job: {}... (Engaging {} CPU Cores)".format(job_name, cpus))
    abs_scratch_path = os.path.abspath(DIR_SCRATCH)
    command = ('abaqus job={} input="{}" cpus={} memory={}% scratch="{}" '
               'ask_delete=OFF interactive').format(
                   job_name, input_name, cpus, memory_percent, abs_scratch_path)
    os.system(command)

# =============================================================
# 2. MULTI-PART HELPERS  (NEW - replaces get_non_design_elements)
# =============================================================

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
                        if clean_part.split('=')[1].strip() == set_name.upper():
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
    Parses ALL *PART blocks to find the design domain, and ALSO reads
    assembly-level NON_DESIGN_SET cards (*Elset ..., instance=Z), attributing
    them to their part via the *Instance instance->part map. Element IDs are
    part-local in both cases (matching the design instance getSubset labels at
    run time). generate ranges are expanded; duplicates are removed by the
    per-part set. Design part = part with the most elements NOT in NON_DESIGN_SET.
    Part names are kept uppercase (matches the rest of this script).
    Returns: (design_part_name_str, set_of_non_design_element_labels).
    If the model has no *PART blocks, falls back to a flat global read so
    flattened / orphan-mesh models keep working (matches the lattice engine).
    """
    print("-> Scanning INP for parts and Non-Design Space ({})...".format(set_name))
    parts            = {}        # {PART_NAME: {'count': int, 'non_design': set()}}
    instance_to_part = {}        # INSTANCE_NAME -> PART_NAME (both uppercase)
    current_part     = None
    in_elem          = False
    in_nds           = False     # reading a part-level NON_DESIGN_SET
    nds_generate     = False
    in_asm_nds       = False     # reading an assembly-level NON_DESIGN_SET
    asm_generate     = False
    asm_target_part  = None

    def _add(part_name, line_text, is_generate):
        if part_name is None or part_name not in parts:
            return
        nds = parts[part_name]['non_design']
        nums = [int(tok.strip()) for tok in line_text.strip().split(',') if tok.strip().isdigit()]
        if is_generate and len(nums) >= 2:
            step = nums[2] if len(nums) >= 3 else 1
            for eid in range(nums[0], nums[1] + 1, step):
                nds.add(eid)
        else:
            for eid in nums:
                nds.add(eid)

    try:
        with open(inp_path, 'r') as f:
            for line in f:
                ul = line.upper().strip()

                if ul.startswith("*PART"):
                    in_elem = False; in_nds = False; nds_generate = False
                    current_part = None
                    for tok in ul.split(','):
                        tok = tok.strip()
                        if tok.startswith("NAME="):
                            current_part = tok.split('=', 1)[1].strip()
                    if current_part is not None:
                        parts.setdefault(current_part, {'count': 0, 'non_design': set()})
                    continue

                if ul.startswith("*END PART"):
                    current_part = None; in_elem = False; in_nds = False; nds_generate = False
                    continue

                # ----- inside a *Part block -----
                if current_part is not None:
                    if ul.startswith("*ELEMENT"):
                        in_elem = True; in_nds = False; nds_generate = False; continue
                    if ul.startswith("*ELSET"):
                        in_elem = False; in_nds = False; nds_generate = "GENERATE" in ul
                        for tok in ul.split(','):
                            tok = tok.strip()
                            if tok.startswith("ELSET=") and tok.split('=', 1)[1].strip() == set_name:
                                in_nds = True
                        continue
                    if ul.startswith("*"):
                        in_elem = False; in_nds = False; nds_generate = False; continue
                    if in_elem:
                        toks = line.strip().split(',')
                        if toks and toks[0].strip().isdigit():
                            parts[current_part]['count'] += 1
                    if in_nds:
                        _add(current_part, line, nds_generate)
                    continue

                # ----- outside any *Part: assembly / model level -----
                if ul.startswith("*INSTANCE"):
                    inst_name = None; inst_part = None
                    for tok in ul.split(','):
                        tok = tok.strip()
                        if tok.startswith("NAME="):
                            inst_name = tok.split('=', 1)[1].strip()
                        elif tok.startswith("PART="):
                            inst_part = tok.split('=', 1)[1].strip()
                    if inst_name is not None and inst_part is not None:
                        instance_to_part[inst_name] = inst_part
                    in_asm_nds = False
                    continue

                if ul.startswith("*ELSET"):
                    asm_generate = "GENERATE" in ul
                    in_asm_nds = False; asm_target_part = None
                    is_target = False; inst_ref = None
                    for tok in ul.split(','):
                        tok = tok.strip()
                        if tok.startswith("ELSET=") and tok.split('=', 1)[1].strip() == set_name:
                            is_target = True
                        if tok.startswith("INSTANCE="):
                            inst_ref = tok.split('=', 1)[1].strip()
                    if is_target and inst_ref is not None:
                        mapped = instance_to_part.get(inst_ref)
                        if mapped is not None and mapped in parts:
                            in_asm_nds = True; asm_target_part = mapped
                    continue

                if ul.startswith("*"):
                    in_asm_nds = False
                    continue

                if in_asm_nds:
                    _add(asm_target_part, line, asm_generate)

    except Exception as e:
        print("   [WARNING] Could not parse .inp: {}".format(e))

    if not parts:
        print("   [INFO] No *PART blocks found; using single-domain mode.")
        return None, get_non_design_elements(inp_path, set_name)

    design_part = max(parts, key=lambda p: parts[p]['count'] - len(parts[p]['non_design']))

    print("-> Multi-part detection: {} part(s) found.".format(len(parts)))
    for pname, pdata in parts.items():
        design_count = pdata['count'] - len(pdata['non_design'])
        role = "DESIGN" if pname == design_part else "non-design"
        print("   [{}] {}: {} total | {} frozen | {} design".format(
              role, pname, pdata['count'], len(pdata['non_design']), design_count))

    return design_part, parts[design_part]['non_design']


def find_odb_design_instance(odb, design_part_name):
    """
    Finds the ODB assembly instance corresponding to design_part_name.
    ODB instance names are typically 'PARTNAME-1' but vary.
    Falls back gracefully to first instance if no match is found.
    """
    target = design_part_name.upper()
    for inst in odb.rootAssembly.instances.values():
        if inst.name.upper().startswith(target):
            return inst
    for inst in odb.rootAssembly.instances.values():
        if target in inst.name.upper():
            return inst
    print("   [WARNING] No ODB instance matched part '{}'. Using first.".format(design_part_name))
    return list(odb.rootAssembly.instances.values())[0]

# =============================================================
# 3. EXTRACTOR  (multi-part aware)
# =============================================================
def get_sener_evol_temp_and_coords(odb_name, step_weights=None, design_part_name=None):
    print("-> Extracting SENER, EVOL, Temperature, and Coordinates from {}...".format(odb_name))
    odb   = openOdb(odb_name)
    steps = odb.steps.values()

    # --- 3.1 Separate mechanical steps from thermal step ---
    mechanical_steps = []
    thermal_step     = None
    for step in steps:
        if 'NT11' in step.frames[-1].fieldOutputs.keys():
            thermal_step = step
        else:
            mechanical_steps.append(step)

    num_mech_steps = len(mechanical_steps)
    # FIX (minor): only mention thermal case if one actually exists
    if thermal_step is not None:
        print("   Found {} Mechanical Load Case(s) and 1 Thermal Case.".format(num_mech_steps))
    else:
        print("   Found {} Mechanical Load Case(s). No thermal step detected.".format(num_mech_steps))

    # --- 3.2 Safety-check weights ---
    if step_weights is not None and len(step_weights) != num_mech_steps:
        print("\n[WARNING] {} weights provided but {} mechanical step(s) found! "
              "Reverting to equal weights.".format(len(step_weights), num_mech_steps))
        step_weights = None
    if step_weights is None:
        step_weights = [1.0 / num_mech_steps] * num_mech_steps

    # --- 3.3 Identify the design instance (FIX: multi-part support) ---
    if design_part_name is not None:
        design_instance = find_odb_design_instance(odb, design_part_name)
        print("   Design instance: {}".format(design_instance.name))
    else:
        design_instance = list(odb.rootAssembly.instances.values())[0]

    # --- 3.4 SENER: read from design instance ONLY (FIX: avoids label collision) ---
    master_sener = {}
    for step_idx, step in enumerate(mechanical_steps):
        last_frame   = step.frames[-1]
        # getSubset restricts field output to this instance's elements only
        if 'SENER' not in last_frame.fieldOutputs.keys():
            print("\n[ERROR] 'SENER' not found in ODB step '{}'.".format(step.name))
            print("  Check your .inp output block. The correct syntax is:")
            print("    *Output, field, variable=PRESELECT")
            print("    *Element Output")
            print("    SENER, EVOL")
            print("  Note: placing 'SENER, EVOL' directly after *Output, field, variable=PRESELECT")
            print("  (without a *Element Output card) is silently ignored by Abaqus.")
            print("  Available field outputs:", list(last_frame.fieldOutputs.keys()))
            odb.close()
            raise KeyError("'SENER' missing from ODB - fix the .inp Output block (see message above).")
        sener_field  = last_frame.fieldOutputs['SENER'].getSubset(region=design_instance)
        weight       = step_weights[step_idx]
        for block in sener_field.bulkDataBlocks:
            labels = block.elementLabels
            data   = block.data
            for i in range(len(labels)):
                eid = labels[i]
                if eid not in master_sener:
                    master_sener[eid] = 0.0
                master_sener[eid] += data[i][0] * weight

    # --- 3.5 EVOL: from design instance only ---
    evol_data        = {}
    first_last_frame = mechanical_steps[0].frames[-1]
    if 'EVOL' in first_last_frame.fieldOutputs.keys():
        evol_field = first_last_frame.fieldOutputs['EVOL'].getSubset(region=design_instance)
        for block in evol_field.bulkDataBlocks:
            labels = block.elementLabels
            data   = block.data
            for i in range(len(labels)):
                evol_data[labels[i]] = data[i][0]
    else:
        print("\n[WARNING] 'EVOL' not found! Defaulting to 0.25.")
        for eid in master_sener:
            evol_data[eid] = 0.25

    # --- 3.6 Geometry: from design instance only ---
    node_coords = {}
    for node in design_instance.nodes:
        node_coords[node.label] = node.coordinates

    coord_data = {}
    for elem in design_instance.elements:
        if elem.label not in master_sener:
            continue
        elem_nodes        = elem.connectivity
        x_sum = y_sum = z_sum = 0.0
        for node_label in elem_nodes:
            coords  = node_coords[node_label]
            x_sum  += coords[0]
            y_sum  += coords[1]
            z_sum  += coords[2]
        n = len(elem_nodes)
        coord_data[elem.label] = (x_sum / n, y_sum / n, z_sum / n)

    # --- 3.7 Thermal data: from design instance only ---
    elem_temps = {}
    if thermal_step is not None:
        last_frame_therm = thermal_step.frames[-1]
        nt_field         = last_frame_therm.fieldOutputs['NT11']
        node_temps       = {}
        for val in nt_field.values:
            node_temps[val.nodeLabel] = val.data
        for elem in design_instance.elements:
            if elem.label in master_sener:
                temps = [node_temps[n] for n in elem.connectivity]
                elem_temps[elem.label] = sum(temps) / len(temps)
    else:
        print("   [WARNING] Thermal Step not found. Setting temps to 0.")
        for eid in master_sener:
            elem_temps[eid] = 0.0

    # --- 3.8 Connectivity: from design instance only ---
    connectivity = {}
    for elem in design_instance.elements:
        connectivity[elem.label] = (elem.type, list(elem.connectivity))
    print("   [Dump] Connectivity captured: {} elements.".format(len(connectivity)))

    odb.close()
    return master_sener, evol_data, elem_temps, coord_data, node_coords, connectivity

# =============================================================
# 3.9 ELEMENT VOLUME / AREA MEASURES  [v47]
# -------------------------------------------------------------
# Geometric per-element measure, computed ONCE from the reference (undeformed)
# mesh and reused every iteration to drive a VOLUME-true volume-fraction target
# instead of the old element-COUNT target. On an irregular mesh the two differ
# and count-based removal systematically overshoots the intended volume. These
# measures are load-independent and match the CAD/mesh geometry.
#   tet   (C3D4*, C3D10*)        : exact corner-tetrahedron volume.
#   hex   (C3D8*, C3D20*, SC8*)  : exact volume by 2x2x2 Gauss quadrature of the
#                                  trilinear map (same integration Abaqus uses
#                                  for EVOL; single-valued even for warped hexes,
#                                  unlike a face-triangulation decomposition).
#   wedge (C3D6*, C3D15*, SC6*)  : 3-point-triangle x 2-point-Gauss quadrature.
#   2D / shell / membrane quads and tris : surface AREA. NOTE a 2D element has no
#     volume from its nodes alone (true volume = area x section thickness), so
#     area is a correct volume-fraction surrogate ONLY at uniform thickness.
#   any other type               : falls back to the supplied EVOL value.
# No numpy and no generator expressions in the hot math (Abaqus 2.7 friendly).
# =============================================================
_V47_GP = (-0.5773502691896257, 0.5773502691896257)   # 2-point Gauss, weights 1
_V47_HEX_NAT = ((-1.0,-1.0,-1.0),(1.0,-1.0,-1.0),(1.0,1.0,-1.0),(-1.0,1.0,-1.0),
                (-1.0,-1.0,1.0),(1.0,-1.0,1.0),(1.0,1.0,1.0),(-1.0,1.0,1.0))
_V47_TRI_QP = ((0.16666666666666666, 0.16666666666666666),
               (0.6666666666666666, 0.16666666666666666),
               (0.16666666666666666, 0.6666666666666666))   # weight 1/6 each
_V47_Q_NAT = ((-1.0,-1.0),(1.0,-1.0),(1.0,1.0),(-1.0,1.0))

def _v47_tet_volume(p0, p1, p2, p3):
    ax = p1[0]-p0[0]; ay = p1[1]-p0[1]; az = p1[2]-p0[2]
    bx = p2[0]-p0[0]; by = p2[1]-p0[1]; bz = p2[2]-p0[2]
    cx = p3[0]-p0[0]; cy = p3[1]-p0[1]; cz = p3[2]-p0[2]
    crx = by*cz - bz*cy
    cry = bz*cx - bx*cz
    crz = bx*cy - by*cx
    return abs(ax*crx + ay*cry + az*crz) / 6.0

def _v47_hex_volume(P):
    vol = 0.0
    for xi in _V47_GP:
        for eta in _V47_GP:
            for ze in _V47_GP:
                j00 = 0.0; j01 = 0.0; j02 = 0.0
                j10 = 0.0; j11 = 0.0; j12 = 0.0
                j20 = 0.0; j21 = 0.0; j22 = 0.0
                for i in range(8):
                    xi_i  = _V47_HEX_NAT[i][0]
                    eta_i = _V47_HEX_NAT[i][1]
                    ze_i  = _V47_HEX_NAT[i][2]
                    a = 1.0 + xi*xi_i
                    b = 1.0 + eta*eta_i
                    c = 1.0 + ze*ze_i
                    d_xi  = 0.125*xi_i *b*c
                    d_eta = 0.125*eta_i*a*c
                    d_ze  = 0.125*ze_i *a*b
                    x = P[i][0]; y = P[i][1]; z = P[i][2]
                    j00 += d_xi*x; j01 += d_eta*x; j02 += d_ze*x
                    j10 += d_xi*y; j11 += d_eta*y; j12 += d_ze*y
                    j20 += d_xi*z; j21 += d_eta*z; j22 += d_ze*z
                det = (j00*(j11*j22 - j12*j21)
                     - j01*(j10*j22 - j12*j20)
                     + j02*(j10*j21 - j11*j20))
                vol += abs(det)
    return vol

def _v47_wedge_volume(P):
    vol = 0.0
    for qp in _V47_TRI_QP:
        r = qp[0]; s = qp[1]
        for t in _V47_GP:
            dr = (-0.5*(1.0-t), 0.5*(1.0-t), 0.0,
                  -0.5*(1.0+t), 0.5*(1.0+t), 0.0)
            ds = (-0.5*(1.0-t), 0.0, 0.5*(1.0-t),
                  -0.5*(1.0+t), 0.0, 0.5*(1.0+t))
            dt = (-0.5*(1.0-r-s), -0.5*r, -0.5*s,
                   0.5*(1.0-r-s),  0.5*r,  0.5*s)
            j00 = 0.0; j01 = 0.0; j02 = 0.0
            j10 = 0.0; j11 = 0.0; j12 = 0.0
            j20 = 0.0; j21 = 0.0; j22 = 0.0
            for i in range(6):
                x = P[i][0]; y = P[i][1]; z = P[i][2]
                j00 += dr[i]*x; j01 += ds[i]*x; j02 += dt[i]*x
                j10 += dr[i]*y; j11 += ds[i]*y; j12 += dt[i]*y
                j20 += dr[i]*z; j21 += ds[i]*z; j22 += dt[i]*z
            det = (j00*(j11*j22 - j12*j21)
                 - j01*(j10*j22 - j12*j20)
                 + j02*(j10*j21 - j11*j20))
            vol += 0.16666666666666666 * abs(det)
    return vol

def _v47_tri_area(p0, p1, p2):
    ax = p1[0]-p0[0]; ay = p1[1]-p0[1]; az = p1[2]-p0[2]
    bx = p2[0]-p0[0]; by = p2[1]-p0[1]; bz = p2[2]-p0[2]
    crx = ay*bz - az*by
    cry = az*bx - ax*bz
    crz = ax*by - ay*bx
    return 0.5 * math.sqrt(crx*crx + cry*cry + crz*crz)

def _v47_quad_area(P):
    area = 0.0
    for xi in _V47_GP:
        for eta in _V47_GP:
            dxi0 = 0.0; dxi1 = 0.0; dxi2 = 0.0
            det0 = 0.0; det1 = 0.0; det2 = 0.0
            for i in range(4):
                xi_i = _V47_Q_NAT[i][0]; eta_i = _V47_Q_NAT[i][1]
                a = 0.25*xi_i *(1.0 + eta*eta_i)
                b = 0.25*eta_i*(1.0 + xi*xi_i)
                x = P[i][0]; y = P[i][1]; z = P[i][2]
                dxi0 += a*x; dxi1 += a*y; dxi2 += a*z
                det0 += b*x; det1 += b*y; det2 += b*z
            cx = dxi1*det2 - dxi2*det1
            cy = dxi2*det0 - dxi0*det2
            cz = dxi0*det1 - dxi1*det0
            area += math.sqrt(cx*cx + cy*cy + cz*cz)
    return area

_V47_FAM_3D = ("C3D", "SC")
_V47_FAM_2D = ("CPEG", "CGAX", "CPS", "CPE", "CAX", "STRI", "SFM3D", "M3D", "S")

def _v47_lead_int(s):
    digits = ""
    for ch in s:
        if ch.isdigit():
            digits += ch
        else:
            break
    if digits == "":
        return None
    return int(digits)

def _v47_classify(etype):
    """Map an Abaqus element type string to (kind, n_corner) or (None, 0)."""
    et = (etype or "").upper()
    for fam in _V47_FAM_3D:
        if et.startswith(fam):
            n = _v47_lead_int(et[len(fam):])
            if n in (4, 10):  return ("tet",   4)
            if n in (6, 15):  return ("wedge", 6)
            if n in (8, 20):  return ("hex",   8)
            return (None, 0)
    for fam in _V47_FAM_2D:
        if et.startswith(fam):
            n = _v47_lead_int(et[len(fam):])
            if n in (3, 6):    return ("tri",  3)
            if n in (4, 8, 9): return ("quad", 4)
            return (None, 0)
    return (None, 0)

def compute_element_volumes(connectivity, node_coords, evol_fallback=None):
    """
    v47: geometric per-element measure {eid: value} from the reference mesh.
    3D solids return volume (mm^3); 2D/shell/membrane return area (mm^2); any
    other type uses the EVOL fallback (or 0.0). See the section header for the
    2D area-vs-volume caveat under non-uniform thickness.
    """
    volumes   = {}
    warned_2d = [False]
    warned_fb = [False]
    saw_3d    = [False]
    saw_2d    = [False]

    def _warn_2d():
        if not warned_2d[0]:
            print("   [v47] 2D element(s) present: using surface AREA as the measure "
                  "(a valid volume fraction only at UNIFORM section thickness).")
            warned_2d[0] = True

    for eid in connectivity:
        etype, nodes = connectivity[eid]
        kind, ncorner = _v47_classify(etype)
        value = None
        if kind is not None and len(nodes) >= ncorner:
            P = []
            ok = True
            for k in range(ncorner):
                nc = node_coords.get(nodes[k])
                if nc is None:
                    ok = False
                    break
                P.append(nc)
            if ok:
                try:
                    if kind == "tet":
                        value = _v47_tet_volume(P[0], P[1], P[2], P[3]); saw_3d[0] = True
                    elif kind == "hex":
                        value = _v47_hex_volume(P); saw_3d[0] = True
                    elif kind == "wedge":
                        value = _v47_wedge_volume(P); saw_3d[0] = True
                    elif kind == "tri":
                        value = _v47_tri_area(P[0], P[1], P[2]); saw_2d[0] = True; _warn_2d()
                    elif kind == "quad":
                        value = _v47_quad_area(P); saw_2d[0] = True; _warn_2d()
                except (IndexError, TypeError):
                    value = None
        if value is None:
            if evol_fallback is not None and eid in evol_fallback:
                value = evol_fallback[eid]
            else:
                value = 0.0
            if not warned_fb[0]:
                print("   [v47][WARN] element {} type '{}' has no geometric rule; using EVOL "
                      "fallback. Volume-target accuracy may be reduced for such elements."
                      .format(eid, etype))
                warned_fb[0] = True
        volumes[eid] = value

    if saw_3d[0] and saw_2d[0]:
        print("   [v47][WARN] mesh mixes 3D (volume) and 2D (area) elements; a single volume "
              "fraction over mixed measures is not well defined. Check the design domain.")
    return volumes

# =============================================================
# 4. JANITOR
# =============================================================
def cleanup_files(job_name, keep_inp=False):
    if not keep_inp and os.path.exists(job_name + ".inp"):
        shutil.move(job_name + ".inp", os.path.join(DIR_INP, job_name + ".inp"))
    if os.path.exists(job_name + ".odb"):
        shutil.move(job_name + ".odb", os.path.join(DIR_ODB, job_name + ".odb"))
    junk_extensions = ['.dat', '.msg', '.sta', '.prt', '.com', '.sim',
                       '.abq', '.mdl', '.stt']
    for ext in junk_extensions:
        if os.path.exists(job_name + ext):
            try:
                shutil.move(job_name + ext, os.path.join(DIR_MISC, job_name + ext))
            except:
                pass

# =============================================================
# 5. FILTER WEIGHTS  (unchanged)
# =============================================================
def prepare_filter_weights(coord_data, r_min, is_gaussian):
    if is_gaussian:
        print("-> Pre-computing Filter Weights using Gaussian KD-Tree (sparse)...")
    else:
        print("-> Pre-computing Filter Weights using Linear KD-Tree (sparse)...")

    elem_ids = np.array(list(coord_data.keys()))
    coords   = np.array(list(coord_data.values()))
    tree     = cKDTree(coords)
    neighbor_indices_list = tree.query_ball_point(coords, r_min)

    total_elements = len(elem_ids)

    row_indices   = []
    col_indices   = []
    weight_values = []

    for i in range(total_elements):
        target_coord     = coords[i]
        neighbor_indices = neighbor_indices_list[i]
        raw_weights      = {}
        weight_sum       = 0.0

        for idx in neighbor_indices:
            neighbor_coord = coords[idx]
            dist = np.linalg.norm(target_coord - neighbor_coord)
            if dist < r_min:
                if is_gaussian:
                    sigma = r_min / 1.25
                    w     = np.exp(-0.5 * (dist / sigma)**2)
                else:
                    w = r_min - dist
                raw_weights[idx] = w
                weight_sum += w

        if weight_sum > 0.0:
            for idx, w in raw_weights.items():
                row_indices.append(i)
                col_indices.append(idx)
                weight_values.append(w / weight_sum)

        if (i + 1) % 2500 == 0:
            print("   Mapped {}/{} elements...".format(i + 1, total_elements))

    W = csr_matrix(
        (weight_values, (row_indices, col_indices)),
        shape=(total_elements, total_elements),
        dtype=np.float64
    )

    print("   Filter matrix: {}x{}, {} non-zeros.".format(
          total_elements, total_elements, W.nnz))
    return W, elem_ids

# =============================================================
# 6. SENSITIVITY BRANCHES
# =============================================================
def process_mechanical_branch(raw_sener, master_weights):
    filtered_sener = apply_filter(raw_sener, master_weights)
    sener_values   = filtered_sener.values()
    min_s, max_s   = min(sener_values), max(sener_values)
    if max_s == min_s:
        max_s = min_s + 1e-6
    norm_mech = {}
    for eid in filtered_sener:
        norm_mech[eid] = (filtered_sener[eid] - min_s) / (max_s - min_s)
    return norm_mech

def process_thermal_branch(raw_temp, active_violators, micro_weights,
                           solid_list, percentile=95.0):
    """
    v21: thermal overhang PENALTY for solid violators.

    Robust normalisation: temperature is divided by a high percentile (default
    95th) of the SOLID temperatures and clipped to [0,1], instead of min-max.
    Isolated solid fragments that run away to thousands of degrees sit above the
    percentile and simply saturate at penalty 1, rather than stretching a min-max
    scale and crushing every genuine overhang toward zero. Penalty is gated to
    violators and smeared by the thermal filter, exactly as before.
    """
    solid_temps = [raw_temp[eid] for eid in solid_list if eid in raw_temp]
    if solid_temps:
        t_ref = float(np.percentile(np.array(solid_temps, dtype=np.float64),
                                    percentile))
    else:
        t_ref = 1.0
    if t_ref < 1e-9:
        t_ref = 1.0
    isolated_penalty = {}
    for eid in raw_temp:
        if eid in active_violators:
            val = raw_temp[eid] / t_ref
            if val < 0.0:
                val = 0.0
            elif val > 1.0:
                val = 1.0
            isolated_penalty[eid] = val
        else:
            isolated_penalty[eid] = 0.0
    return apply_filter(isolated_penalty, micro_weights)

def compute_grounded_set(solid_list, coord_data, print_dir, elem_size,
                         adj_factor=1.5):
    """
    v23: set of solid element IDs that trace to the build plate through touching
    solid (face or diagonal, so voxel-staircase struts count), via a flood-fill
    seeded from the solids on the plate. The plate is the domain floor along the
    build axis (min for a + direction, max for a - direction). A solid the fill
    never reaches is a floating island and must not be used as a support anchor.
    """
    axis_map = {"+X": 0, "-X": 0, "+Y": 1, "-Y": 1, "+Z": 2, "-Z": 2}
    axis     = axis_map[print_dir]
    positive = print_dir in ("+X", "+Y", "+Z")
    if not solid_list:
        return set()
    solid_coords = np.array([coord_data[e] for e in solid_list], dtype=np.float64)
    all_axis  = np.array([coord_data[e][axis] for e in coord_data],
                         dtype=np.float64)
    plate_val = float(all_axis.min()) if positive else float(all_axis.max())
    tol = 0.6 * elem_size
    if positive:
        seeds = [i for i in range(len(solid_list))
                 if solid_coords[i][axis] <= plate_val + tol]
    else:
        seeds = [i for i in range(len(solid_list))
                 if solid_coords[i][axis] >= plate_val - tol]
    if not seeds:
        return set()
    tree  = cKDTree(solid_coords)
    adj_r = adj_factor * elem_size
    grounded = set(seeds)
    stack    = list(seeds)
    while stack:
        i = stack.pop()
        for j in tree.query_ball_point(solid_coords[i], adj_r):
            if j not in grounded:
                grounded.add(j)
                stack.append(j)
    return set(solid_list[i] for i in grounded)


def find_node_contacts(solid_set, coord_data, elem_size):
    """
    v41: single-node diagonal contacts -- two solid cells meeting at one node
    with both shared-face cells non-solid. This one detector covers BOTH the
    checkerboard hinge and the node-only-cluster attachment (a cluster joined
    to the structure by a corner is exactly such a contact). For each it also
    returns the eids of the two shared-face cells, which are the candidate
    BRIDGE cells: promoting one converts the point contact into a face joint.
    Returns a list of (cell_a, cell_b, bridge1, bridge2); bridge eids are None
    if that shared-face position is outside the mesh.
    """
    if len(solid_set) < 2 or elem_size <= 0:
        return []
    all_coords = np.array([coord_data[e] for e in coord_data], dtype=np.float64)
    spreads = all_coords.max(axis=0) - all_coords.min(axis=0)
    order = list(np.argsort(spreads))
    thick_axis = int(order[0])
    ax0 = int(order[1])
    ax1 = int(order[2])
    mins = all_coords.min(axis=0)
    inv = 1.0 / elem_size

    def to_ix(eid):
        c = coord_data[eid]
        return (int(round((c[ax0] - mins[ax0]) * inv)),
                int(round((c[ax1] - mins[ax1]) * inv)),
                int(round((c[thick_axis] - mins[thick_axis]) * inv)))

    all_ix = {}
    for eid in coord_data:
        all_ix[to_ix(eid)] = eid
    solid_ix = set(to_ix(eid) for eid in solid_set)

    diags = [((1, 1), (1, 0), (0, 1)), ((-1, 1), (-1, 0), (0, 1))]
    out = []
    for eid in solid_set:
        p, q, r = to_ix(eid)
        for dd, s1, s2 in diags:
            diag = (p + dd[0], q + dd[1], r)
            if diag in solid_ix:
                f1 = (p + s1[0], q + s1[1], r)
                f2 = (p + s2[0], q + s2[1], r)
                if f1 not in solid_ix and f2 not in solid_ix:
                    out.append((eid, all_ix[diag],
                                all_ix.get(f1), all_ix.get(f2)))
    return out

def bridge_repair_phased(solid_set, void_ranked, coord_data, print_dir,
                         face_factor, diag_factor, max_steps, design_set,
                         sens_lookup=None):
    """
    v41: ADDITIVE bridge-builder repair. Two phases.

      Phase A -- genuine floaters, BATCH (diag_factor ~1.5), removed and
        rescued. A fully detached island has nothing to bridge to, so it is
        deleted exactly as before.

      Bridge phase -- one operator heals BOTH checkerboards and node-only
        clusters, because both are single-node contacts (find_node_contacts).
        For each contact, PROMOTE the better shared-face void cell (higher
        total sensitivity when both can bridge), turning the point contact
        into a face joint. Volume fraction is held by removing the lowest
        total-sensitivity solid design cell whose removal is sound (a look
        ahead rejects any removal that would create a new floater or contact,
        trying the next-lowest instead). Re-checked every step. If no design
        void cell can bridge a contact (rare), fall back to demoting it.

    Unlike v40 (which DELETES unsound junctions), v41 KEEPS the support mass
    and completes it into a printable face-connected support -- it actively
    helps build the supports rather than only forbidding bad ones.

    RE-ADMISSION / RE-DELETION BARS: cells removed this call (removed_this_
    call) are never re-promoted; bridge cells added this call (added_this_
    call) are never chosen as the balancing deletion.

    returns (new_solid, bridges_built, balance_removed, fallback_demotes,
             vf_overshoot, cleaned_A, unresolved, steps_used,
             removed_this_call, added_this_call)
    """
    all_array = np.array(list(coord_data.values()), dtype=np.float64)
    if len(all_array) < 2:
        return (set(solid_set), 0, 0, 0, 0, 0, 0, 0, set(), set())
    nn_tree  = cKDTree(all_array)
    d_all, _ = nn_tree.query(all_array, k=2)
    elem_size = float(np.median(d_all[:, 1]))
    if elem_size < 1e-9:
        elem_size = 1.0
    face_r = face_factor * elem_size

    axis_map = {"+X": 0, "-X": 0, "+Y": 1, "-Y": 1, "+Z": 2, "-Z": 2}
    axis     = axis_map[print_dir]
    positive = print_dir in ("+X", "+Y", "+Z")
    axis_vals = np.array([coord_data[e][axis] for e in coord_data],
                         dtype=np.float64)
    plate_val = float(axis_vals.min()) if positive else float(axis_vals.max())
    plate_tol = 0.6 * elem_size

    def on_plate(eid):
        v = coord_data[eid][axis]
        if positive:
            return v <= plate_val + plate_tol
        return v >= plate_val - plate_tol

    def fground(ss):
        return compute_grounded_set(sorted(ss), coord_data, print_dir,
                                    elem_size, adj_factor=face_factor)

    def dground(ss):
        return compute_grounded_set(sorted(ss), coord_data, print_dir,
                                    elem_size, adj_factor=diag_factor)

    solid = set(solid_set)
    pool  = list(void_ranked)
    removed_this_call = set()
    added_this_call   = set()

    def sens(e):
        return sens_lookup.get(e, 0.0) if sens_lookup else 0.0

    def rescue_one():
        fg = fground(solid)
        if fg:
            gc = np.array([coord_data[e] for e in fg], dtype=np.float64)
            gt = cKDTree(gc)
        else:
            gt = None
        for idx in range(len(pool)):
            cand = pool[idx]
            if cand in solid or cand in removed_this_call:
                continue
            ok = on_plate(cand)
            if not ok and gt is not None:
                if gt.query_ball_point(coord_data[cand], face_r):
                    ok = True
            if ok:
                pool.pop(idx)
                return cand
        return None

    removable_ranked = sorted((e for e in solid if e in design_set),
                              key=lambda e: (sens(e), e))

    def pick_balance_deletion():
        base_f = solid - fground(solid)
        base_c = set(frozenset((a, bb)) for a, bb, _, _ in
                     find_node_contacts(solid, coord_data, elem_size))
        tried = 0
        for c in removable_ranked:
            if c not in solid or c in added_this_call or c in removed_this_call:
                continue
            tried += 1
            if tried > 50:
                break
            trial = solid - set([c])
            tf = trial - fground(trial)
            if (tf - base_f) - set([c]):
                continue
            tc = set(frozenset((a, bb)) for a, bb, _, _ in
                     find_node_contacts(trial, coord_data, elem_size))
            if tc - base_c:
                continue
            return c
        return None

    def cleanup_face():
        rem = 0
        for _i in range(max_steps):
            fl = solid - fground(solid)
            if not fl:
                break
            v = min(fl, key=lambda e: (sens(e), e))
            solid.discard(v)
            removed_this_call.add(v)
            rem += 1
            r = rescue_one()
            if r is not None:
                solid.add(r)
        return rem

    cleaned_A = 0; rescued_A = 0
    bridges_built = 0; balance_removed = 0; fallback_demotes = 0
    vf_overshoot = 0; cleanup_total = 0; unresolved = 0; steps_used = 0

    print("   [v41] Bridge-builder repair (Phase A delete, then additive bridge phase):")

    # ---- Phase A: genuine floaters, batch --------------------------------
    for _a in range(max_steps):
        gen = solid - dground(solid)
        if not gen:
            break
        solid -= gen
        for ee in gen:
            removed_this_call.add(ee)
        cleaned_A += len(gen)
        for _k in range(len(gen)):
            r = rescue_one()
            if r is None:
                unresolved += 1
                break
            solid.add(r); rescued_A += 1
    print("   [v41]   Phase A (genuine floaters, 1.5 flood, batch): removed {}, rescued {}".format(cleaned_A, rescued_A))

    # ---- Bridge phase: heal every single-node contact additively ---------
    for _outer in range(max_steps):
        changed = False
        for _s in range(max_steps):
            contacts = find_node_contacts(solid, coord_data, elem_size)
            if not contacts:
                break
            changed = True
            pa, pb, b1, b2 = contacts[0]
            valid = [bc for bc in (b1, b2) if bc is not None
                     and bc in design_set and bc not in solid
                     and bc not in removed_this_call]
            if valid:
                bridge = max(valid, key=lambda c: (sens(c), c))
                solid.add(bridge)
                added_this_call.add(bridge)
                bridges_built += 1
                delc = pick_balance_deletion()
                if delc is not None:
                    solid.discard(delc)
                    removed_this_call.add(delc)
                    balance_removed += 1
                else:
                    vf_overshoot += 1
            else:
                base = fground(solid)
                if pa in base or pb in base:
                    ga = len(fground(solid - set([pa])))
                    gb = len(fground(solid - set([pb])))
                    if ga > gb:
                        victim = pa
                    elif gb > ga:
                        victim = pb
                    else:
                        victim = pa if sens(pa) <= sens(pb) else pb
                else:
                    victim = pa if sens(pa) <= sens(pb) else pb
                solid.discard(victim)
                removed_this_call.add(victim)
                fallback_demotes += 1
                r = rescue_one()
                if r is None:
                    unresolved += 1
            steps_used += 1
        c_rem = cleanup_face()
        cleanup_total += c_rem
        if c_rem:
            changed = True
        if not changed:
            break
    print("   [v41]   Bridge phase: built {} bridge(s) | balance-removed {} | fallback-demotes {} | VF-overshoot {} | cleanup {} | steps {}".format(bridges_built, balance_removed, fallback_demotes, vf_overshoot,
        cleanup_total, steps_used))

    final_c = find_node_contacts(solid, coord_data, elem_size)
    final_f = solid - fground(solid)
    if final_c or final_f:
        print("   [v41]   [WARNING] residual: {} contact(s), {} floater(s) -- step cap may have been hit".format(len(final_c), len(final_f)))
    print("   [v41]   Totals: bridges-added {} | cells-removed {} | unresolved {}".format(len(added_this_call), len(removed_this_call), unresolved))

    return (solid, bridges_built, balance_removed, fallback_demotes,
            vf_overshoot, cleaned_A, unresolved, steps_used,
            removed_this_call, added_this_call)

# ==========================================================================
# [v42] TRIM STRATEGY functions, grafted verbatim from v40 (soundness repair).
# Selected at run time via overhang_correction_strategy == "trim". The Bridge
# strategy (find_node_contacts / bridge_repair_phased, above) is the v41 path.
# Both families coexist; the refine loop dispatches to one. Results-neutral
# until selected. No name collisions between the two families.
# ==========================================================================
def find_checkerboard_pairs(solid_set, coord_data, elem_size):
    """
    v39: find pairs of solid cells that meet ONLY at a single node, i.e. a
    diagonal contact whose two shared-face cells are both non-solid. On a
    structured grid such a pair is a moment-free pin: a hinge when it sits on
    the load path, and an unprintable point contact in either case. Detection
    is a local 2x2 nodal stencil in the plane of the two largest-spread axes
    (exact for a 2D build plane; applied per layer for a stacked 3D mesh).
    Returns a list of (cell_a, cell_b) solid-id pairs, one per checkerboard
    node, with no duplicate pairs.
    """
    if len(solid_set) < 2 or elem_size <= 0:
        return []
    all_coords = np.array([coord_data[e] for e in coord_data], dtype=np.float64)
    spreads = all_coords.max(axis=0) - all_coords.min(axis=0)
    order = list(np.argsort(spreads))     # ascending spread
    thick_axis = int(order[0])
    ax0 = int(order[1])
    ax1 = int(order[2])
    mins = all_coords.min(axis=0)
    inv = 1.0 / elem_size

    def to_ix(eid):
        c = coord_data[eid]
        return (int(round((c[ax0] - mins[ax0]) * inv)),
                int(round((c[ax1] - mins[ax1]) * inv)),
                int(round((c[thick_axis] - mins[thick_axis]) * inv)))

    solid_ix = {}
    for eid in solid_set:
        solid_ix[to_ix(eid)] = eid

    pairs = []
    for eid in solid_set:
        p, q, r = to_ix(eid)
        # up-right diagonal; shared-face cells are (p+1,q,r) and (p,q+1,r)
        diag = (p + 1, q + 1, r)
        if diag in solid_ix:
            if (p + 1, q, r) not in solid_ix and (p, q + 1, r) not in solid_ix:
                pairs.append((eid, solid_ix[diag]))
        # up-left diagonal; shared-face cells are (p-1,q,r) and (p,q+1,r)
        diag = (p - 1, q + 1, r)
        if diag in solid_ix:
            if (p - 1, q, r) not in solid_ix and (p, q + 1, r) not in solid_ix:
                pairs.append((eid, solid_ix[diag]))
    return pairs


def soundness_repair_phased(solid_set, void_ranked, coord_data, print_dir,
                            face_factor, diag_factor, max_steps,
                            sens_lookup=None):
    """
    v40: structural-soundness repair in THREE phases. Phase A batch-removes
    genuine floaters; Phases B and C are SEQUENTIAL and re-flood the whole
    structure at 1.05 after EVERY single delete-and-swap, so a problem (a
    floater, a node-only cluster cell, or an orphan a deletion strands) is
    fully resolved before the next step is chosen. Branched from v39.

      Phase A -- genuine floaters, BATCH (diag_factor ~1.5). No attachment
        at all; removing one can never strand another, so batch is exact.

      Phase B -- node-only clusters, SEQUENTIAL (face_factor ~1.05). Repeat:
        remove the lowest total-sensitivity face-floater, rescue the highest
        ranked grounded-if-added candidate, re-flood; a rescue that bridges a
        neighbour grounds it and it drops out untouched. Continues until the
        1.05 flood is clean (so it also absorbs any cluster a rescue makes).

      Phase C -- checkerboard hinges, SEQUENTIAL. Repeat: take one node,
        demote the connectivity-impact cell (lowest total-sensitivity on a
        tie), rescue, THEN run the Phase-B cleanup to completion so any cell
        the demotion stranded (an orphan) is removed before the next node is
        evaluated -- so connectivity-impact always reads a sound structure.

    RE-ADMISSION BAR: every cell demoted anywhere in this call is recorded in
    removed_this_call and can NEVER be chosen as a rescue candidate again for
    the rest of the call. Without this the just-deleted (high-rank) cell would
    be the very next rescue pick and the problem would recur.

    Output is floater-free, cluster-free and checkerboard-free, established
    step by step. Diagnostics quantify the path; orphans counts deletion-
    stranded cells (a rescue ADDS material so it can only ground, never
    orphan).

    returns (new_solid_set, total_swapped, unresolved, steps_used,
             cleaned_A, cleaned_B, cleaned_C, resolved_by_rescue, orphans,
             removed_this_call)
    """
    all_array = np.array(list(coord_data.values()), dtype=np.float64)
    if len(all_array) < 2:
        return (set(solid_set), 0, 0, 0, 0, 0, 0, 0, 0, set())
    nn_tree  = cKDTree(all_array)
    d_all, _ = nn_tree.query(all_array, k=2)
    elem_size = float(np.median(d_all[:, 1]))
    if elem_size < 1e-9:
        elem_size = 1.0
    face_r = face_factor * elem_size

    axis_map = {"+X": 0, "-X": 0, "+Y": 1, "-Y": 1, "+Z": 2, "-Z": 2}
    axis     = axis_map[print_dir]
    positive = print_dir in ("+X", "+Y", "+Z")
    axis_vals = np.array([coord_data[e][axis] for e in coord_data],
                         dtype=np.float64)
    plate_val = float(axis_vals.min()) if positive else float(axis_vals.max())
    plate_tol = 0.6 * elem_size

    def on_plate(eid):
        v = coord_data[eid][axis]
        if positive:
            return v <= plate_val + plate_tol
        return v >= plate_val - plate_tol

    def fground(s):
        return compute_grounded_set(sorted(s), coord_data, print_dir,
                                    elem_size, adj_factor=face_factor)

    def dground(s):
        return compute_grounded_set(sorted(s), coord_data, print_dir,
                                    elem_size, adj_factor=diag_factor)

    solid = set(solid_set)
    pool  = list(void_ranked)
    removed_this_call = set()

    def rescue_one():
        # highest-ranked pool cell that is face-grounded once added (or on the
        # plate), never one demoted earlier in this call (re-admission bar).
        fg = fground(solid)
        if fg:
            gc = np.array([coord_data[e] for e in fg], dtype=np.float64)
            gt = cKDTree(gc)
        else:
            gt = None
        for idx in range(len(pool)):
            cand = pool[idx]
            if cand in solid or cand in removed_this_call:
                continue
            ok = on_plate(cand)
            if not ok and gt is not None:
                if gt.query_ball_point(coord_data[cand], face_r):
                    ok = True
            if ok:
                pool.pop(idx)
                return cand
        return None

    def cleanup_face():
        # sequential face-soundness cleanup at 1.05; re-floods every step.
        # returns (removed, steps, resolved, orphans, swapped, unresolved)
        rem = 0; stp = 0; res = 0; orp = 0; sw = 0; unr = 0
        for _i in range(max_steps):
            floaters = solid - fground(solid)
            if not floaters:
                break
            victim = min(floaters,
                         key=lambda e: (sens_lookup.get(e, 0.0)
                                        if sens_lookup else 0.0, e))
            pre = floaters
            solid.discard(victim)
            removed_this_call.add(victim)
            rem += 1
            mid = solid - fground(solid)
            orp += len(mid - (pre - set([victim])))
            r = rescue_one()
            if r is not None:
                solid.add(r); sw += 1
            else:
                unr += 1
            post = solid - fground(solid)
            res += len(mid - post)
            stp += 1
        return rem, stp, res, orp, sw, unr

    total_swapped = 0; unresolved = 0; steps_used = 0
    cleaned_A = 0; cleaned_B = 0; cleaned_C = 0; cleaned_orphan = 0
    resolved_byrescue = 0; orphans = 0

    print("   [v40] Soundness repair (phased; B and C sequential, re-checked each step):")

    # ---- Phase A: genuine floaters, batch (1.5 flood) -------------------
    rescued_A = 0
    for _a in range(max_steps):
        gen = solid - dground(solid)
        if not gen:
            break
        solid -= gen
        for e in gen:
            removed_this_call.add(e)
        cleaned_A += len(gen)
        need = len(gen); got = 0
        for _k in range(need):
            r = rescue_one()
            if r is None:
                break
            solid.add(r); got += 1; total_swapped += 1; rescued_A += 1
        if got < need:
            unresolved += (need - got)
            break
    print("   [v40]   Phase A (genuine floaters, 1.5 flood, batch): removed {}, rescued {}".format(cleaned_A, rescued_A))

    # ---- Phase B: node-only clusters, sequential (1.05 flood) -----------
    remB, stpB, resB, orpB, swB, unrB = cleanup_face()
    cleaned_B += remB; steps_used += stpB; resolved_byrescue += resB
    orphans += orpB; total_swapped += swB; unresolved += unrB
    print("   [v40]   Phase B (node-only clusters, 1.05 flood, sequential): removed {} over {} step(s), rescued {}, resolved-by-rescue {}".format(remB, stpB, swB, resB))

    # ---- Phase C: checkerboards, sequential; each followed by an embedded
    #      Phase-B cleanup so the next node is judged on a sound structure ---
    c_removed = 0; c_steps = 0; c_rescued = 0; embedded_removed = 0
    for _c in range(max_steps):
        fg = fground(solid)
        pairs = find_checkerboard_pairs(fg, coord_data, elem_size)
        if not pairs:
            break
        pa, pb = pairs[0]
        base = set(fg)
        ga = fground(base - set([pa]))
        gb = fground(base - set([pb]))
        if len(ga) > len(gb):
            victim = pa
        elif len(gb) > len(ga):
            victim = pb
        else:
            sa = sens_lookup.get(pa, 0.0) if sens_lookup else 0.0
            sb = sens_lookup.get(pb, 0.0) if sens_lookup else 0.0
            victim = pa if sa <= sb else pb
        f_pre = solid - fg
        solid.discard(victim)
        removed_this_call.add(victim)
        cleaned_C += 1; c_removed += 1
        f_mid = solid - fground(solid)
        orphans += len(f_mid - f_pre)
        r = rescue_one()
        if r is not None:
            solid.add(r); total_swapped += 1; c_rescued += 1
        else:
            unresolved += 1
        remE, stpE, resE, orpE, swE, unrE = cleanup_face()
        cleaned_orphan += remE; embedded_removed += remE
        steps_used += stpE + 1; c_steps += 1
        resolved_byrescue += resE; orphans += orpE
        total_swapped += swE; unresolved += unrE
    print("   [v40]   Phase C (checkerboards, sequential): removed {} checkerboard cell(s) over {} step(s), rescued {}, embedded floater cleanup removed {}".format(c_removed, c_steps, c_rescued, embedded_removed))

    # ---- final soundness assertion (should already hold) ----------------
    final_float = solid - fground(solid)
    final_check = find_checkerboard_pairs(fground(solid), coord_data, elem_size)
    if final_float or final_check:
        print("   [v40]   [WARNING] residual after repair: {} floater(s), {} checkerboard(s) -- step cap may have been hit".format(len(final_float), len(final_check)))
    print("   [v40]   Totals: removed {} (A {} | B {} | C {} | orphan-cleanup {}), all barred from re-admission | rescued {} | resolved-by-rescue {} | orphans {} | unresolved {}".format(len(removed_this_call), cleaned_A, cleaned_B, cleaned_C, cleaned_orphan,
        total_swapped, resolved_byrescue, orphans, unresolved))

    return (solid, total_swapped, unresolved, steps_used,
            cleaned_A, cleaned_B, cleaned_C, resolved_byrescue, orphans,
            removed_this_call)

def process_thermal_promotion(raw_temp, active_violators, solid_list, coord_data,
                              print_dir, micro_weights, norm_mech, max_promote,
                              support_reach=6.0, cone_half_angle_deg=45.0,
                              percentile=95.0, contiguity_factor=1.1,
                              directional_sharpness=2.0):
    """
    v23: directional, grounded support-promotion.

    v37: refine phase combines consecutive-state lock-in (separate_solid_void_locked)
    and soundness repair; streaks update post-swap. v41: the repair is the additive
    bridge_repair_phased (batch floaters, then a bridge phase that heals checkerboards
    and node-only clusters by promoting a face-bridging cell).

    v33: two-phase build-then-refine controller in the main loop. The function
    itself is unchanged from v31/v32; only the cap passed in differs by phase.

    v32: adds a solid-fraction delay gate in the main loop (see
    thermal_start_solid_fraction); this function is unchanged from v31.

    v31: support_reach reduced from 6.0 to 3.0. The anchor search and the
    cast share this single reach (they are coupled in this version), so both
    shrink together: promotion stays fully local with no gap between where
    support is cast and where the anchor foot sits. This is the both-short
    control; it deliberately avoids the v30 cast<anchor gap that caused the
    transient severance and near-mechanism stiffness crashes.

    For each load-bearing hot overhang violator, find the nearest GROUNDED solid
    inside its downward 45-degree cone (the strut foot it can lean on) within
    support_reach element-lengths, then cast support demand into the void
    preferentially along the violator-to-anchor line, weighting each candidate by
    cos(angle)^directional_sharpness so the cast leans toward the foot and starves
    the far side. The flat span on the far side is left to the penalty; the net is
    a rotation of the surface toward an incline.

    Gates from v22 are kept: mechanical coupling (demand = clip(T/t_ref) *
    norm_mech[violator]), absolute clip, contiguity (must touch current solid),
    quantity cap. Bonus is non-zero only on void. Returns (bonus_dict,
    promoted_eid_set) and prints per-stage diagnostics.
    """
    empty = ({eid: 0.0 for eid in raw_temp}, set(), set())
    if not active_violators or max_promote is None or max_promote <= 0:
        return empty
    try:
        solid_temps = [raw_temp[eid] for eid in solid_list if eid in raw_temp]
        if not solid_temps:
            return empty
        t_ref = float(np.percentile(np.array(solid_temps, dtype=np.float64),
                                    percentile))
        if t_ref < 1e-9:
            t_ref = 1.0

        all_array = np.array(list(coord_data.values()), dtype=np.float64)
        all_tree  = cKDTree(all_array)
        d_all, _  = all_tree.query(all_array, k=2)
        elem_size = float(np.median(d_all[:, 1]))
        if elem_size < 1e-9:
            elem_size = 1.0
        reach    = support_reach * elem_size
        tan_half = math.tan(math.radians(cone_half_angle_deg))
        cone_tol = 1e-6 * elem_size
        contig_r = contiguity_factor * elem_size

        grounded = compute_grounded_set(solid_list, coord_data, print_dir,
                                        elem_size, adj_factor=1.5)
        solid_count = len(solid_list)
        print("   [v23] Grounded: {}/{} solids ({} floating)".format(
              len(grounded), solid_count, solid_count - len(grounded)))
        if not grounded:
            print("   [v23] No grounded solids; skipping promotion.")
            return empty

        grounded_list   = list(grounded)
        grounded_coords = np.array([coord_data[e] for e in grounded_list],
                                   dtype=np.float64)
        grounded_tree   = cKDTree(grounded_coords)

        solid_set    = set(solid_list)
        solid_coords = np.array([coord_data[e] for e in solid_list],
                                dtype=np.float64)
        solid_tree   = cKDTree(solid_coords)

        void_eids = [eid for eid in coord_data if eid not in solid_set]
        if not void_eids:
            return empty
        void_coords = np.array([coord_data[e] for e in void_eids],
                               dtype=np.float64)
        void_tree   = cKDTree(void_coords)

        axis_map = {"+X": 0, "-X": 0, "+Y": 1, "-Y": 1, "+Z": 2, "-Z": 2}
        axis     = axis_map[print_dir]
        positive = print_dir in ("+X", "+Y", "+Z")
        other    = [k for k in (0, 1, 2) if k != axis]
        query_r  = reach * math.sqrt(1.0 + tan_half * tan_half)

        raw_bonus     = {}
        contrib_count = {}
        anchored      = 0
        demand_vals   = []
        anchor_depths = []

        for s in active_violators:
            if s not in coord_data:
                continue
            therm = raw_temp.get(s, 0.0) / t_ref
            if therm > 1.0:
                therm = 1.0
            # v24: demand is the clipped thermal ratio only. The v22/v23 factor
            # norm_mech[violator] crushed it, because an overhang is a low-energy
            # surface element; flooding is now held by grounding, the directional
            # cone, contiguity, and the cap, so the mechanical factor is dropped.
            # norm_mech stays on the signature for a possible anchor-energy coupling.
            demand = therm
            if demand <= 0.0:
                continue
            demand_vals.append(demand)
            sc = coord_data[s]

            # nearest grounded solid in the downward cone = the strut foot
            anchor_c = None
            anchor_d = None
            best = query_r * 2.0
            for gj in grounded_tree.query_ball_point(sc, query_r):
                gc = grounded_coords[gj]
                axial = (sc[axis] - gc[axis]) if positive else (gc[axis] - sc[axis])
                if axial <= 0.5 * elem_size or axial > reach:
                    continue
                lat = math.sqrt((gc[other[0]] - sc[other[0]]) ** 2 +
                                (gc[other[1]] - sc[other[1]]) ** 2)
                if lat > axial * tan_half + cone_tol:
                    continue
                dist = math.sqrt(axial * axial + lat * lat)
                if dist < best:
                    best = dist
                    anchor_c = gc
                    anchor_d = axial
            if anchor_c is None:
                continue                       # stranded overhang, leave to penalty
            anchored += 1
            anchor_depths.append(anchor_d)

            # support direction = unit violator-to-anchor vector (full, not lateral)
            sdx = float(anchor_c[0] - sc[0])
            sdy = float(anchor_c[1] - sc[1])
            sdz = float(anchor_c[2] - sc[2])
            snorm = math.sqrt(sdx * sdx + sdy * sdy + sdz * sdz)
            if snorm < 1e-9:
                continue
            sdx /= snorm; sdy /= snorm; sdz /= snorm

            # cast into the void, leaning toward the anchor
            for j in void_tree.query_ball_point(sc, query_r):
                vc = void_coords[j]
                axial = (sc[axis] - vc[axis]) if positive else (vc[axis] - sc[axis])
                if axial <= 1e-9 or axial > reach:
                    continue
                lat = math.sqrt((vc[other[0]] - sc[other[0]]) ** 2 +
                                (vc[other[1]] - sc[other[1]]) ** 2)
                if lat > axial * tan_half + cone_tol:
                    continue
                if not solid_tree.query_ball_point(vc, contig_r):
                    continue                   # must touch existing solid
                cdx = float(vc[0] - sc[0])
                cdy = float(vc[1] - sc[1])
                cdz = float(vc[2] - sc[2])
                cnorm = math.sqrt(cdx * cdx + cdy * cdy + cdz * cdz)
                if cnorm < 1e-9:
                    continue
                cosang = (cdx * sdx + cdy * sdy + cdz * sdz) / cnorm
                if cosang <= 0.0:
                    continue                   # far side of the anchor
                dir_w = cosang ** directional_sharpness
                w = demand * (1.0 - axial / reach) * dir_w
                if w <= 0.0:
                    continue
                veid = void_eids[j]
                raw_bonus[veid] = raw_bonus.get(veid, 0.0) + w
                contrib_count[veid] = contrib_count.get(veid, 0) + 1

        nviol = len(demand_vals)
        dmax  = max(demand_vals) if demand_vals else 0.0
        dmean = (sum(demand_vals) / len(demand_vals)) if demand_vals else 0.0
        print("   [v23] Anchored violators: {}/{} | demand max {:.3f} "
              "mean {:.3f}".format(anchored, nviol, dmax, dmean))

        # v26: the candidate pool (all void cells that received any bonus, pre-cap)
        # and how many of them stacked contributions from more than one violator.
        candidate_set = set(raw_bonus.keys())
        n_pool    = len(candidate_set)
        n_stacked = sum(1 for c in contrib_count.values() if c > 1)
        max_stack = max(contrib_count.values()) if contrib_count else 0
        print("   [v26] Pool: {} candidates | stacked (>1 violator): {} | "
              "max stack {}".format(n_pool, n_stacked, max_stack))

        if not raw_bonus:
            print("   [v23] Promoted: 0 (cap {})".format(max_promote))
            return ({eid: 0.0 for eid in raw_temp}, set(), candidate_set)

        clipped = {}
        for veid, val in raw_bonus.items():
            clipped[veid] = 1.0 if val > 1.0 else val
        if len(clipped) > max_promote:
            top = sorted(clipped.items(), key=lambda kv: kv[1],
                         reverse=True)[:max_promote]
            clipped = dict(top)

        if anchor_depths:
            mean_depth = sum(anchor_depths) / len(anchor_depths) / elem_size
        else:
            mean_depth = 0.0
        print("   [v23] Promoted: {} (cap {}) | mean anchor depth {:.2f} "
              "elem".format(len(clipped), max_promote, mean_depth))

        # v25: report the bonus that actually competes at the sort. clipped is the
        # per-cell value pre-smear; smeared is what enters total_sens (tw * bonus).
        if clipped:
            cvals = list(clipped.values())
            cmax  = max(cvals)
            cmean = sum(cvals) / len(cvals)
        else:
            cmax = 0.0; cmean = 0.0
        # v29: smear, then zero bonus ONLY on cells with no solid within contig_r
        # (1.1, face-adjacency). This removes the non-contiguous floating leak while
        # keeping the protective spread onto solid (including the violators) and onto
        # contiguous void, which the v28 capped-set mask had stripped, stranding the
        # promoted supports. Solid cells pass trivially, so their protection is kept.
        bonus_full = apply_filter(clipped, micro_weights)
        bonus_out  = {}
        for e, val in bonus_full.items():
            if val <= 1e-12:
                continue
            if solid_tree.query_ball_point(coord_data[e], contig_r):
                bonus_out[e] = val
        capped_keys = set(clipped.keys())
        nz   = [v for v in bonus_out.values() if v > 1e-12]
        ncap = sum(1 for e in bonus_out if e in capped_keys)
        if nz:
            smax  = max(nz)
            smean = sum(nz) / len(nz)
        else:
            smax = 0.0; smean = 0.0
        print("   [v29] Bonus clipped: max {:.3f} mean {:.3f} | smeared gated: "
              "max {:.3f} mean {:.3f} ({} cells, {} capped, {} spread)".format(
                  cmax, cmean, smax, smean, len(nz), ncap, len(nz) - ncap))

        promoted_set = set(clipped.keys())
        return (bonus_out, promoted_set, candidate_set)

    except Exception as e:
        print("   [ERROR] Could not compute support promotion: {}".format(e))
        return ({eid: 0.0 for eid in raw_temp}, set(), set())

# =============================================================
# 7. APPLY FILTER  (FIX: safe .get + missing-element warning)
# =============================================================
def apply_filter(sener_data, filter_weights):
    W, elem_ids = filter_weights
    sens_array  = np.array([sener_data.get(eid, 0.0) for eid in elem_ids],
                           dtype=np.float64)
    result      = W.dot(sens_array)
    return dict(zip(elem_ids.tolist(), result.tolist()))

# =============================================================
# 8. SORTING  (unchanged)
# =============================================================
def separate_solid_void(sener_data, current_void_ratio, non_design_set, elem_volumes):
    # [v47] Volume-true partition. Design-domain elements are still ranked by
    # sensitivity (ascending; lowest removed first -- unchanged), but the removal
    # budget is now a fraction of the design-domain VOLUME, not the element COUNT.
    # On an irregular mesh, count-based removal overshoots the intended volume;
    # a volume budget makes the achieved VF match the input regardless of element
    # size scatter. Discretisation error is at most one element's volume.
    design_items = [(eid, val) for eid, val in sener_data.items()
                    if eid not in non_design_set]
    design_items.sort(key=lambda item: item[1])

    total_design_volume = 0.0
    for eid, _val in design_items:
        total_design_volume += elem_volumes.get(eid, 0.0)
    target_removed_volume = total_design_volume * current_void_ratio

    void_elements  = []
    solid_elements = list(non_design_set)
    removed_volume = 0.0
    for eid, _val in design_items:
        if removed_volume < target_removed_volume:
            void_elements.append(eid)
            removed_volume += elem_volumes.get(eid, 0.0)
        else:
            solid_elements.append(eid)

    kept_design_volume = total_design_volume - removed_volume
    if total_design_volume > 0.0:
        kept_pct = 100.0 * kept_design_volume / total_design_volume
    else:
        kept_pct = 0.0
    print("   Kept Solid: {} | Turned Void: {} | design-domain volume kept "
          "{:.4e}/{:.4e} ({:.2f}%)".format(
          len(solid_elements), len(void_elements),
          kept_design_volume, total_design_volume, kept_pct))
    return solid_elements, void_elements

def separate_solid_void_locked(sener_data, current_void_ratio, non_design_set,
                               frozen_solid, frozen_void, elem_volumes):
    """
    v36 + v47: lock-in partition for the refine phase, now VOLUME-true. The solid
    budget is a fraction of the design-domain VOLUME (not count); frozen-solid and
    frozen-void cells are held, and only the un-frozen remainder is ranked and
    filled by highest sensitivity until the remaining solid VOLUME budget is met.
    Because the freeze is read off the current at-VF partition, frozen_solid never
    exceeds the solid target, so the volume fraction is preserved while the
    stabilised core stops flipping and the per-pass churn decays to zero.
    """
    design_sener = {eid: val for eid, val in sener_data.items()
                    if eid not in non_design_set}
    total_design_volume = 0.0
    for eid in design_sener:
        total_design_volume += elem_volumes.get(eid, 0.0)
    target_solid_volume = total_design_volume * (1.0 - current_void_ratio)

    fs = [e for e in frozen_solid if e in design_sener]
    fv = [e for e in frozen_void if e in design_sener]
    frozen = set(fs)
    frozen.update(fv)
    fs_volume = 0.0
    for e in fs:
        fs_volume += elem_volumes.get(e, 0.0)
    remaining_solid_volume = target_solid_volume - fs_volume
    if remaining_solid_volume < 0.0:
        remaining_solid_volume = 0.0

    unfrozen = [(eid, val) for eid, val in design_sener.items()
                if eid not in frozen]
    unfrozen.sort(key=lambda kv: kv[1], reverse=True)

    solid_elements = list(non_design_set) + fs
    void_elements  = list(fv)
    filled_volume  = 0.0
    for i in range(len(unfrozen)):
        if filled_volume < remaining_solid_volume:
            solid_elements.append(unfrozen[i][0])
            filled_volume += elem_volumes.get(unfrozen[i][0], 0.0)
        else:
            void_elements.append(unfrozen[i][0])
    print("   Kept Solid: {} | Turned Void: {} | frozen {} (solid {} void {}) | "
          "design solid vol {:.4e}/{:.4e}".format(
          len(solid_elements), len(void_elements), len(frozen), len(fs), len(fv),
          fs_volume + filled_volume, total_design_volume))
    return solid_elements, void_elements


# =============================================================
# 9. WRITER HELPERS  (unchanged)
# =============================================================
def write_elset(file_object, set_name, element_list):
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

# =============================================================
# 10. INP GENERATOR  (FIX: multi-part section handling)
# =============================================================
def create_new_inp(base_inp_lines, new_inp_name, solid_list, void_list,
                   print_dir, node_coords, target_k22, use_thermal=True,
                   design_part_name=None, violator_set=None,
                   promoted_set=None, readmitted_set=None,
                   candidate_set=None, removed_set=None, frozen_set=None,
                   repair_removed_set=None, bridge_added_set=None):
    if use_thermal:
        print("-> Generating Dual-Physics INP: {} (Dir: {})...".format(
              new_inp_name, print_dir))
    else:
        print("-> Generating INP: {}...".format(new_inp_name))

    thickness_line       = ",\n"
    e_modulus            = "110000.0"
    poisson              = "0.33"
    has_solid            = False
    has_void             = False
    instance_name        = "Part-1-1"
    design_instance_name = "Part-1-1"
    angle                = 0.0

    # --- SCAN PHASE ---
    # Step 1: find which material is assigned to the design part section
    # Step 2: read elastic modulus only from that material block
    current_scan_part  = None
    design_material    = None   # material name used by the design part section
    current_material   = None   # material name currently being scanned
    in_target_material = False  # True when inside the design material block

    for i, line in enumerate(base_inp_lines):
        ul = line.strip().upper()

        # Track which part block we are in
        if ul.startswith("*PART"):
            current_scan_part = None
            for tok in ul.split(','):
                tok = tok.strip()
                if tok.startswith("NAME="):
                    current_scan_part = tok.split('=', 1)[1].strip()
            continue

        if ul.startswith("*END PART"):
            current_scan_part = None
            continue

        # Track instance names
        if ul.startswith("*INSTANCE"):
            inst_n = None
            inst_p = None
            for tok in line.split(','):
                tok = tok.strip()
                if tok.upper().startswith("NAME="):
                    inst_n = tok.split('=', 1)[1].strip()
                elif tok.upper().startswith("PART="):
                    inst_p = tok.split('=', 1)[1].strip().upper()
            if inst_n:
                instance_name = inst_n
                if design_part_name and inst_p and inst_p == design_part_name.upper():
                    design_instance_name = inst_n

        if ul.startswith("*SOLID SECTION"):
            thickness_line = base_inp_lines[i + 1]
            # Check if this section belongs to the design part
            # For single-part models design_part_name is None -- always grab it
            # For multi-part models only grab if we are inside the design part block
            is_design_section = (
                design_part_name is None or
                current_scan_part is None or
                current_scan_part == design_part_name.upper()
            )
            if is_design_section and design_material is None:
                # Extract the material name from this section line
                for tok in ul.split(','):
                    tok = tok.strip()
                    if tok.startswith("MATERIAL="):
                        candidate = tok.split('=', 1)[1].strip()
                        # Only record if it is not the BESO-injected material names
                        # (those don't exist in the base INP yet)
                        if candidate not in ("SOLID", "MAT_VOID"):
                            design_material = candidate
                            break

        # Track which material block we are currently reading
        if ul.startswith("*MATERIAL"):
            current_material   = None
            in_target_material = False
            for tok in ul.split(','):
                tok = tok.strip()
                if tok.startswith("NAME="):
                    current_material = tok.split('=', 1)[1].strip()
            # Check if this is the material we are looking for
            if design_material and current_material == design_material:
                in_target_material = True
            # For single-part models where design_material was never found
            # (base INP has no *Solid Section yet), fall back to reading
            # any material that is not the rocker material
            if "NAME=SOLID"    in ul: has_solid = True
            if "NAME=MAT_VOID" in ul: has_void  = True

        # Any keyword other than *Elastic closes the material block tracking
        if ul.startswith("*") and not ul.startswith("*ELASTIC"):
            if not ul.startswith("*MATERIAL"):
                in_target_material = False

        # Read elastic modulus only from the target material
        if ul.startswith("*ELASTIC"):
            should_read = False
            if design_material is not None:
                # Multi-part or single-part with section found: use target material
                should_read = in_target_material
            else:
                # Fallback: no section found in base INP (e.g. first iteration
                # of a brand new model before sections are assigned).
                # Read from design part block or global level, skip rocker.
                should_read = (
                    design_part_name is None or
                    current_scan_part is None or
                    current_scan_part == design_part_name.upper()
                )
            if should_read:
                mat_data = base_inp_lines[i + 1].split(',')
                if len(mat_data) >= 2:
                    e_modulus = mat_data[0].strip()
                    poisson   = mat_data[1].strip()

    global _design_material_fallback_warned
    if design_part_name is not None and design_material is None and not _design_material_fallback_warned:
        print("   [WARNING] No *Solid Section found inside the design part '{}'; ".format(design_part_name))
        print("             falling back to global elastic properties. Verify the design material.")
        _design_material_fallback_warned = True

    # For thermal mode, use the design instance name for node-set qualification
    thermal_instance_name = design_instance_name

    # --- HEAT SINK NODES (thermal mode only) ---
    x_vals = [c[0] for c in node_coords.values()]
    y_vals = [c[1] for c in node_coords.values()]
    z_vals = [c[2] for c in node_coords.values()]
    min_x, max_x = min(x_vals), max(x_vals)
    min_y, max_y = min(y_vals), max(y_vals)
    min_z, max_z = min(z_vals), max(z_vals)
    tol = 0.01

    if   print_dir == "+X": heat_sink_nodes = [nid for nid, c in node_coords.items() if abs(c[0] - min_x) < tol]
    elif print_dir == "-X": heat_sink_nodes = [nid for nid, c in node_coords.items() if abs(c[0] - max_x) < tol]
    elif print_dir == "+Y": heat_sink_nodes = [nid for nid, c in node_coords.items() if abs(c[1] - min_y) < tol]
    elif print_dir == "-Y": heat_sink_nodes = [nid for nid, c in node_coords.items() if abs(c[1] - max_y) < tol]
    elif print_dir == "+Z": heat_sink_nodes = [nid for nid, c in node_coords.items() if abs(c[2] - min_z) < tol]
    elif print_dir == "-Z": heat_sink_nodes = [nid for nid, c in node_coords.items() if abs(c[2] - max_z) < tol]
    else:                   heat_sink_nodes = []

    # --- WRITE PHASE ---
    with open(new_inp_name, 'w') as f_out:
        skip_next_line     = False
        materials_written  = False
        sections_written   = False
        skip_thermal_block = False
        is_in_solid        = False
        past_end_assembly  = False
        current_write_part = None   # FIX: track current part during writing

        for line in base_inp_lines:
            # When thermal is active, upgrade plane-stress elements to
            # coupled temp-disp variants so DOF 11 is active in the model
            if use_thermal and line.strip().upper().startswith("*ELEMENT"):
                line = line.replace("CPS4R", "CPS4RT")
            upper_line_strip = line.strip().upper()

            if skip_next_line:
                skip_next_line = False
                continue

            # FIX: track which part we are writing
            if upper_line_strip.startswith("*PART"):
                current_write_part = None
                for tok in upper_line_strip.split(','):
                    tok = tok.strip()
                    if tok.startswith("NAME="):
                        current_write_part = tok.split('=', 1)[1].strip()

            if upper_line_strip.startswith("*END PART"):
                current_write_part = None

            if upper_line_strip.startswith("*END ASSEMBLY"):
                past_end_assembly = True

            # Strip old GUI thermal step
            if upper_line_strip.startswith("*STEP") and "THERMALSTEP" in upper_line_strip:
                skip_thermal_block = True
                continue
            if skip_thermal_block:
                if upper_line_strip.startswith("*END STEP"):
                    skip_thermal_block = False
                continue

            # Hijack conductivity (only for SOLID material, only when thermal active)
            if upper_line_strip.startswith("*MATERIAL"):
                is_in_solid = ("NAME=SOLID" in upper_line_strip)

            if upper_line_strip.startswith("*CONDUCTIVITY") and is_in_solid:
                if print_dir in ["+X", "-X"]:
                    k_string = "100., {}, {}".format(target_k22, target_k22)
                elif print_dir in ["+Y", "-Y"]:
                    k_string = "{}, 100., {}".format(target_k22, target_k22)
                else:
                    k_string = "{}, {}, 100.".format(target_k22, target_k22)
                f_out.write("*Conductivity, type=ORTHO\n{}\n".format(k_string))
                skip_next_line = True
                continue

            # Inject materials + heat-sink node set after *End Assembly,
            # triggered by the first ** STEP: comment or *Step keyword
            if (past_end_assembly and not materials_written and
                    (upper_line_strip.startswith("** STEP:") or
                     upper_line_strip.startswith("*STEP"))):
                if print_dir in ["+X", "-X"]:
                    k_string = "100., {}, {}".format(target_k22, target_k22)
                elif print_dir in ["+Y", "-Y"]:
                    k_string = "{}, 100., {}".format(target_k22, target_k22)
                else:
                    k_string = "{}, {}, 100.".format(target_k22, target_k22)

                if not has_solid:
                    f_out.write("*Material, name=SOLID\n*Elastic\n{}, {}\n".format(
                                e_modulus, poisson))
                    f_out.write("*Conductivity, type=ORTHO\n{}\n".format(k_string))
                if not has_void:
                    f_out.write("*Material, name=MAT_VOID\n*Elastic\n1.0, {}\n".format(poisson))
                    f_out.write("*Conductivity, type=ORTHO\n0.001, 0.001, 0.001\n")
                materials_written = True

                if use_thermal and heat_sink_nodes:
                    f_out.write("*Nset, nset=HEAT_SINK_NODES, instance={}\n".format(
                                thermal_instance_name))
                    count = 0
                    for i, nid in enumerate(heat_sink_nodes):
                        f_out.write(str(nid))
                        count += 1
                        if count == 16 or i == len(heat_sink_nodes) - 1:
                            f_out.write("\n")
                            count = 0
                        else:
                            f_out.write(", ")

                f_out.write(line)
                continue

            # FIX: handle *SOLID SECTION differently per part
            if line.strip().upper().startswith("*SOLID SECTION"):
                in_design_part = (design_part_name is None) or \
                                 (current_write_part is not None and
                                  current_write_part == design_part_name.upper())

                if in_design_part:
                    # Replace with BESO SOLID_SET / VOID_SET sections
                    if not sections_written:
                        f_out.write("*Orientation, name=PRINT_ORI\n"
                                    "1., 0., 0., 0., 1., 0.\n3, {}\n".format(angle))
                        if len(solid_list) > 0:
                            write_elset(f_out, "SOLID_SET", solid_list)
                            f_out.write("*Solid Section, elset=SOLID_SET, "
                                        "material=SOLID, orientation=PRINT_ORI\n")
                            f_out.write(thickness_line)
                        if len(void_list) > 0:
                            write_elset(f_out, "VOID_SET", void_list)
                            f_out.write("*Solid Section, elset=VOID_SET, "
                                        "material=MAT_VOID, orientation=PRINT_ORI\n")
                            f_out.write(thickness_line)
                        # Write active violator elset for visualisation in Abaqus/CAE
                        if use_thermal and violator_set:
                            violators_in_solid = [eid for eid in solid_list
                                                  if eid in violator_set]
                            if violators_in_solid:
                                write_elset(f_out, "ACTIVE_VIOLATORS",
                                            violators_in_solid)
                        # v25: promoted/re-admitted elsets for CAE inspection of
                        # where promotion acts and where it wins re-admission
                        if use_thermal and (promoted_set or readmitted_set
                                            or candidate_set or removed_set
                                            or frozen_set or repair_removed_set
                                            or bridge_added_set):
                            solid_set_local = set(solid_list)
                            mesh_ids = set(void_list)
                            mesh_ids.update(solid_set_local)
                            if candidate_set:
                                cand_in_mesh = sorted(
                                    eid for eid in candidate_set if eid in mesh_ids)
                                if cand_in_mesh:
                                    write_elset(f_out, "CANDIDATE_SET",
                                                cand_in_mesh)
                            if promoted_set:
                                promoted_in_mesh = sorted(
                                    eid for eid in promoted_set if eid in mesh_ids)
                                if promoted_in_mesh:
                                    write_elset(f_out, "PROMOTED_SET",
                                                promoted_in_mesh)
                            if readmitted_set:
                                readmitted_in_solid = sorted(
                                    eid for eid in readmitted_set
                                    if eid in solid_set_local)
                                if readmitted_in_solid:
                                    write_elset(f_out, "READMITTED_SET",
                                                readmitted_in_solid)
                            if removed_set:
                                removed_in_mesh = sorted(
                                    eid for eid in removed_set if eid in mesh_ids)
                                if removed_in_mesh:
                                    write_elset(f_out, "REMOVED_SET",
                                                removed_in_mesh)
                            # v39: per-pass locked (frozen) solid cells, for
                            # colouring the lock front in CAE. Results-neutral:
                            # no section or BC references this elset.
                            if frozen_set:
                                frozen_in_solid = sorted(
                                    eid for eid in frozen_set
                                    if eid in solid_set_local)
                                if frozen_in_solid:
                                    write_elset(f_out, "FROZEN_LOCKED",
                                                frozen_in_solid)
                            # v40: cells the soundness repair demoted this
                            # pass (barred from re-admission). For CAE review
                            # of where the repair acted. Results-neutral.
                            if repair_removed_set:
                                rr_in_mesh = sorted(
                                    eid for eid in repair_removed_set
                                    if eid in mesh_ids)
                                if rr_in_mesh:
                                    write_elset(f_out, "REPAIR_REMOVED",
                                                rr_in_mesh)
                            # v41: cells the bridge-builder PROMOTED this pass
                            # (the support-completing bridges). Results-neutral.
                            if bridge_added_set:
                                ba_in_solid = sorted(
                                    eid for eid in bridge_added_set
                                    if eid in solid_set_local)
                                if ba_in_solid:
                                    write_elset(f_out, "BRIDGE_ADDED",
                                                ba_in_solid)
                        sections_written = True
                    skip_next_line = True   # skip original thickness line
                    continue                # skip original *SOLID SECTION line
                else:
                    # Non-design part: keep original *SOLID SECTION unchanged
                    f_out.write(line)
                    # Do NOT set skip_next_line -- thickness passes through naturally
                    continue

            f_out.write(line)

        # Inject thermal step at the end if requested
        if use_thermal:
            f_out.write("** \n** STEP: ThermalStep\n** \n")
            f_out.write("*Step, name=ThermalStep, nlgeom=NO\n")
            f_out.write("*Coupled Temperature-displacement, creep=none, steady state\n"
                        "1., 1., 1e-05, 1.\n")
            f_out.write("*Boundary\nHEAT_SINK_NODES, 11, 11, 0.0\n")
            f_out.write("*Dflux\n{}.SOLID_SET, BF, 1.0\n".format(thermal_instance_name))
            f_out.write("*Output, field\n*Node Output\nNT\n*End Step\n")

# =============================================================
# 11. STIFFNESS EVALUATION  (FIX: use all mechanical steps)
# =============================================================

def evaluate_specific_stiffness(odb_path, total_force, mass_fraction, step_weights=None):
    try:
        odb = openOdb(odb_path)

        mechanical_steps = []
        for step in odb.steps.values():
            if 'NT11' not in step.frames[-1].fieldOutputs.keys():
                mechanical_steps.append(step)

        if not mechanical_steps:
            odb.close()
            return 0.0

        # Normalise weights - fall back to equal if not provided or mismatched
        if step_weights is None or len(step_weights) != len(mechanical_steps):
            step_weights = [1.0 / len(mechanical_steps)] * len(mechanical_steps)

        # Weighted average displacement across all mechanical load cases
        weighted_disp = 0.0
        for i, step in enumerate(mechanical_steps):
            last_frame = step.frames[-1]
            if 'U' not in last_frame.fieldOutputs.keys():
                continue
            u_field       = last_frame.fieldOutputs['U']
            max_u_step    = max([val.magnitude for val in u_field.values])
            print("   LC {:30s}  disp = {:.5f} mm  (weight {:.3f})".format(
                  step.name, max_u_step, step_weights[i]))
            weighted_disp += step_weights[i] * max_u_step

        odb.close()

        if weighted_disp == 0.0:
            print("   [WARNING] Zero weighted displacement. Cannot calculate stiffness.")
            return 0.0

        stiffness          = total_force / weighted_disp
        specific_stiffness = stiffness / mass_fraction

        print("   Weighted displacement:   {:.5f} mm".format(weighted_disp))
        print("   Absolute stiffness (K):  {:.2f} N/mm".format(stiffness))
        print("   Specific stiffness:      {:.2f} (N/mm)/%".format(specific_stiffness))
        return specific_stiffness

    except Exception as e:
        print("   [ERROR] Could not calculate stiffness: {}".format(e))
        return 0.0

# =============================================================
# 12. OVERHANG DETECTOR  (v20: fixed elem_size estimation and search radius)
# =============================================================
def get_active_violators(solid_list, coord_data, print_dir, label="Printability Check"):
    """
    Detects solid elements with no solid neighbour in the build direction below them.

    Key fixes vs previous version:
    - elem_size is estimated from the FULL coord_data (all mesh elements, stable
      throughout the run), not from the current solid subset. The solid set thins
      as the run progresses, causing the previous NN-distance estimate to drift
      upward and making search_radius unreliable.
    - search_radius is capped at 1.1 * elem_size. On a regular voxel mesh the
      directly-below neighbour is at exactly 1.0 * elem_size; diagonal neighbours
      are at sqrt(2) * elem_size ~ 1.414. A cap of 1.1 admits only the true
      below-neighbour, preventing diagonal solids from falsely satisfying the
      support test (false negatives) or interior elements from being reached by a
      ballooned radius (false positives).
    - The floor-skip guard uses bounds from the CURRENT SOLID set, not the global
      mesh. An element at the newly-exposed bottom of a step that is not at the
      global mesh floor must not be exempted.
    """
    violating_eids = set()
    try:
        # --- 1. Stable element size from the full mesh (all elements, every iter) ---
        all_coords  = list(coord_data.values())
        all_array   = np.array(all_coords)
        all_tree    = cKDTree(all_array)
        dists_all, _ = all_tree.query(all_array, k=2)
        elem_size   = float(np.median(dists_all[:, 1]))
        if elem_size < 1e-9:
            elem_size = 1.0
        # Radius of 1.5 * elem_size reaches:
        #   - directly-below neighbours at 1.0 * elem_size  (always an overhang)
        #   - diagonal-below neighbours at ~1.414 * elem_size (45 deg, printable)
        # The direction test (neighbor_c[1] < target_c[1] - tol) rejects purely
        # lateral neighbours regardless of radius, so 1.5 is self-policing.
        search_radius = elem_size * 1.5
        tol           = elem_size * 0.1

        # --- 2. Floor guard from CURRENT SOLID bounds only ---
        solid_coords = [coord_data[eid] for eid in solid_list]
        solid_array  = np.array(solid_coords)

        solid_x = solid_array[:, 0]
        solid_y = solid_array[:, 1]
        solid_z = solid_array[:, 2]
        s_min_x, s_max_x = float(solid_x.min()), float(solid_x.max())
        s_min_y, s_max_y = float(solid_y.min()), float(solid_y.max())
        s_min_z, s_max_z = float(solid_z.min()), float(solid_z.max())

        # --- 3. KD-tree from current solids only (support lookup) ---
        solid_tree = cKDTree(solid_array)

        # --- 4. Main loop ---
        for i, target_c in enumerate(solid_array):
            target_eid = solid_list[i]

            # Skip elements on the build-plate face (they are inherently supported)
            if print_dir == "+X" and target_c[0] <= s_min_x + tol: continue
            if print_dir == "-X" and target_c[0] >= s_max_x - tol: continue
            if print_dir == "+Y" and target_c[1] <= s_min_y + tol: continue
            if print_dir == "-Y" and target_c[1] >= s_max_y - tol: continue
            if print_dir == "+Z" and target_c[2] <= s_min_z + tol: continue
            if print_dir == "-Z" and target_c[2] >= s_max_z - tol: continue

            # Look for a solid neighbour strictly in the build-below direction
            neighbor_indices = solid_tree.query_ball_point(target_c, search_radius)
            is_supported = False
            for idx in neighbor_indices:
                neighbor_c = solid_array[idx]
                if print_dir == "+X" and neighbor_c[0] < target_c[0] - tol:
                    is_supported = True; break
                if print_dir == "-X" and neighbor_c[0] > target_c[0] + tol:
                    is_supported = True; break
                if print_dir == "+Y" and neighbor_c[1] < target_c[1] - tol:
                    is_supported = True; break
                if print_dir == "-Y" and neighbor_c[1] > target_c[1] + tol:
                    is_supported = True; break
                if print_dir == "+Z" and neighbor_c[2] < target_c[2] - tol:
                    is_supported = True; break
                if print_dir == "-Z" and neighbor_c[2] > target_c[2] + tol:
                    is_supported = True; break

            if not is_supported:
                violating_eids.add(target_eid)

        violation_pct = (float(len(violating_eids)) / len(solid_list)) * 100.0
        print("   {}: {} active violators ({:.2f}% of structure)".format(
              label, len(violating_eids), violation_pct))
        return violating_eids, violation_pct

    except Exception as e:
        print("   [ERROR] Could not evaluate overhangs: {}".format(e))
        return set(), 0.0

# =============================================================
# 13. CONFIG LOADER  (unchanged)
# =============================================================
def load_config_or_prompt():
    """
    Loads beso_config.json if present, otherwise falls back to interactive menu.
    """
    if os.path.exists(CONFIG_FILE):
        print("\n" + "="*50)
        print("   BESO OPTIMIZATION SETTINGS")
        print("="*50)
        print("-> Loading configuration from {}...".format(CONFIG_FILE))
        try:
            with open(CONFIG_FILE, 'r') as f:
                cfg = json.load(f)

            opt = cfg.get("optimization", {})
            adv = cfg.get("advanced",    {})
            mdl = cfg.get("model",       {})

            is_dynamic_er      = bool(opt.get("is_dynamic_er",        False))
            is_gaussian_filter = bool(opt.get("is_gaussian_filter",   False))
            custom_weights     =      opt.get("custom_weights",        None)
            t_weight           = float(opt.get("thermal_weight",       0.2))
            direction_choice   =  str(opt.get("print_direction",      "+Z"))
            target_k22         = float(opt.get("target_k22",           5.0))
            use_thermal        = bool(opt.get("use_thermal",           True))

            # [v42] Overhang correction strategy: "trim" (v40 soundness
            # repair) or "bridge" (v41 bridge-builder). Default trim.
            overhang_strategy = str(
                opt.get("overhang_correction_strategy", "trim")).strip().lower()
            if overhang_strategy not in ("trim", "bridge"):
                print("   [v42][WARNING] Invalid overhang strategy '{}'. "
                      "Defaulting to Trim.".format(overhang_strategy))
                overhang_strategy = "trim"

            # [v43] Which geometries to auto-save when mitigation is ON:
            # any of "best_stiffness", "most_mitigation", "final". Default
            # all three. Ignored when thermal is off (single best-stiffness
            # dump only, as before).
            _geom_default = ["best_stiffness", "most_mitigation", "final"]
            geometries_to_save = opt.get("geometries_to_save", _geom_default)
            if not isinstance(geometries_to_save, list):
                geometries_to_save = _geom_default
            geometries_to_save = [str(g).strip().lower()
                                  for g in geometries_to_save]
            geometries_to_save = [g for g in geometries_to_save
                                  if g in _geom_default]
            if not geometries_to_save:
                print("   [v43][WARNING] No valid geometries selected. "
                      "Defaulting to all three.")
                geometries_to_save = list(_geom_default)

            # [v44] ER values (build phase). static_er drives Fixed runs;
            # the initial/final pair drives Dynamic runs. The refine cap
            # base tracks the effective final ER (static_er if Fixed, else
            # final_er) -- see er_final in the master loop.
            static_er  = float(opt.get("static_er",        0.02))
            initial_er = float(opt.get("dynamic_er_start", 0.02))
            final_er   = float(opt.get("dynamic_er_end",   0.005))
            if not (initial_er >= final_er > 0):
                print("   [v44][WARNING] Dynamic ER needs start >= end > 0. "
                      "Using 0.02,0.005.")
                initial_er, final_er = 0.02, 0.005
            if static_er <= 0:
                print("   [v44][WARNING] static_er must be > 0. Using 0.02.")
                static_er = 0.02

            # [v44] Refine/mitigation knobs (previously hard-coded consts).
            # Defaults reproduce the prior values except support_reach (now
            # 3.0, the conservative production reach) and refine_cap_lo (0.5).
            support_reach   = float(opt.get("support_reach",   3.0))
            lock_threshold  =   int(adv.get("lock_threshold",  5))
            n_refine_passes =   int(adv.get("n_refine_passes", 35))
            refine_cap_hi   = float(adv.get("refine_cap_hi",   2.0))
            refine_cap_lo   = float(adv.get("refine_cap_lo",   0.5))
            if not (refine_cap_hi >= refine_cap_lo > 0):
                print("   [v44][WARNING] Need cap hi >= lo > 0. Using 2.0,0.5.")
                refine_cap_hi, refine_cap_lo = 2.0, 0.5

            if not use_thermal:
                t_weight         = 0.0
                direction_choice = "Traditional"

            target_volume_fraction = float(adv.get("target_volume_fraction", 0.48))
            filter_radius          = float(adv.get("filter_radius",           2.0))
            micro_radius           = float(adv.get("micro_radius",            2.0))
            TOTAL_APPLIED_FORCE    = float(adv.get("total_applied_force",  1000.0))
            cpus                   =   int(adv.get("cpus",                     6))
            memory_percent         =   int(adv.get("memory_percent",          90))
            base_job               =   str(mdl.get("base_job",   "beam_beso_base"))
            inp_path               =   str(mdl.get("inp_path",   "")).strip()

            if direction_choice not in ["+X", "-X", "+Y", "-Y", "+Z", "-Z"]:
                print("   [WARNING] Invalid print direction. Defaulting to +Z.")
                direction_choice = "+Z"

            print("   Timestamp:         {}".format(cfg.get("timestamp", "unknown")))
            print("   Base Job:          {}".format(base_job))
            print("   Model INP:         {}".format(
                  inp_path if inp_path else "(not set, will look next to the scripts)"))
            print("   Evolution Ratio:   {}".format("Dynamic" if is_dynamic_er else "Fixed"))
            if is_dynamic_er:
                print("   ER (start->end):   {} -> {}".format(initial_er, final_er))
            else:
                print("   ER (fixed):        {}".format(static_er))
            print("   Filter:            {}".format("Gaussian" if is_gaussian_filter else "Linear"))
            print("   Thermal Analysis:  {}".format(
                "Enabled (weight={}, dir={}, K22={})".format(t_weight, direction_choice, target_k22)
                if use_thermal else "Disabled (Traditional BESO)"))
            if use_thermal:
                print("   Print Direction:   {}".format(direction_choice))
                print("   K22:               {}".format(target_k22))
            print("   Overhang Strategy: {}".format(
                  "Trim (v40)" if overhang_strategy == "trim" else "Bridge (v41)"))
            if use_thermal:
                print("   Save Geometries:   {}".format(
                      ", ".join(geometries_to_save)))
            if use_thermal:
                print("   Support Reach:     {}".format(support_reach))
                print("   Lock Threshold:    {}".format(lock_threshold))
                print("   Refine Passes:     {}".format(n_refine_passes))
                print("   Cap Range (hi,lo): {}, {}".format(
                      refine_cap_hi, refine_cap_lo))
            print("   Volume Fraction (whole part): {}".format(target_volume_fraction))
            print("   Filter Radius:     {}".format(filter_radius))
            print("   Total Force:       {}".format(TOTAL_APPLIED_FORCE))
            print("   CPUs:              {}".format(cpus))
            print("   Memory:            {}%".format(memory_percent))
            if custom_weights:
                print("   Custom Weights:    {}".format(custom_weights))
            print("\n" + "="*50 + "\n")

            return (is_dynamic_er, is_gaussian_filter, custom_weights, t_weight,
                    direction_choice, target_k22, target_volume_fraction,
                    filter_radius, micro_radius, TOTAL_APPLIED_FORCE,
                    cpus, memory_percent, base_job, inp_path, use_thermal,
                    overhang_strategy, geometries_to_save,
                    static_er, initial_er, final_er,
                    support_reach, lock_threshold, n_refine_passes,
                    refine_cap_hi, refine_cap_lo)

        except Exception as e:
            print("   [WARNING] Could not parse config: {}".format(e))
            print("   Falling back to interactive menu.\n")

    # --- FALLBACK: interactive menu ---
    print("\n" + "="*50)
    print("           BESO OPTIMIZATION SETTINGS")
    print("="*50)

    # [v50] The base model is asked for FIRST, so a wrong path is caught before
    # working through every optimisation question.
    inp_path, base_job = prompt_for_base_inp()

    er_choice          = raw_input("\n1=Fixed ER, 2=Dynamic ER: ").strip()
    is_dynamic_er      = (er_choice == '2')
    # [v44] ER values. Fixed -> one constant; Dynamic -> a start,end pair.
    static_er  = 0.02
    initial_er = 0.02
    final_er   = 0.005
    if is_dynamic_er:
        er_in = raw_input("Dynamic ER start,end (default 0.02,0.005): ").strip()
        if er_in:
            try:
                _ep = [float(x.strip()) for x in er_in.split(',')]
                if len(_ep) == 2 and _ep[0] >= _ep[1] > 0:
                    initial_er, final_er = _ep[0], _ep[1]
                else:
                    print("-> [v44][WARNING] Need start >= end > 0. "
                          "Using 0.02,0.005.")
            except:
                print("-> [v44][WARNING] Invalid ER interval. Using 0.02,0.005.")
    else:
        er_in = raw_input("Fixed ER value (default 0.02): ").strip()
        if er_in:
            try:
                _ev = float(er_in)
                if _ev > 0:
                    static_er = _ev
                else:
                    print("-> [v44][WARNING] ER must be > 0. Using 0.02.")
            except:
                print("-> [v44][WARNING] Invalid ER. Using 0.02.")
    filter_choice      = raw_input("1=Linear filter, 2=Gaussian filter: ").strip()
    is_gaussian_filter = (filter_choice == '2')

    weight_choice  = raw_input("1=Equal weights, 2=Custom weights: ").strip()
    custom_weights = None
    if weight_choice == '2':
        w_input = raw_input("Enter weights separated by commas: ").strip()
        try:
            custom_weights = [float(x.strip()) for x in w_input.split(',')]
            total_w        = sum(custom_weights)
            custom_weights = [w / total_w for w in custom_weights]
            print("-> Normalised Weights: {}".format(custom_weights))
        except:
            print("-> [WARNING] Invalid input. Defaulting to Equal Weights.")
            custom_weights = None

    # [v44] Mitigation/refine defaults. Used as-is when thermal is off;
    # overridden by the prompts below when thermal is on.
    overhang_strategy   = "trim"
    geometries_to_save  = ["best_stiffness", "most_mitigation", "final"]
    support_reach       = 3.0
    lock_threshold      = 5
    n_refine_passes     = 35
    refine_cap_hi       = 2.0
    refine_cap_lo       = 0.5

    thermal_choice = raw_input("Enable thermal overhang mitigation? (y/n, default n): ").strip().lower()
    use_thermal    = (thermal_choice == 'y')

    if use_thermal:
        t_weight_in      = raw_input("Thermal weight (default 0.2): ").strip()
        t_weight         = float(t_weight_in) if t_weight_in else 0.2
        direction_choice = raw_input("Print direction (+X/-X/+Y/-Y/+Z/-Z, default +Z): ").strip().upper()
        if direction_choice not in ["+X", "-X", "+Y", "-Y", "+Z", "-Z"]:
            direction_choice = "+Z"
        k22_in    = raw_input("Transverse conductivity K22 (default 5.0): ").strip()
        target_k22 = float(k22_in) if k22_in else 5.0
        # [v44] Mitigation/refine parameters (refine phase only).
        sr_in = raw_input("Support cast reach in element-lengths "
                          "(3=tight/stiffer, 6=long, default 3.0): ").strip()
        if sr_in:
            try:
                _sr = float(sr_in)
                if _sr > 0:
                    support_reach = _sr
                else:
                    print("-> [v44][WARNING] Reach must be > 0. Using 3.0.")
            except:
                print("-> [v44][WARNING] Invalid reach. Using 3.0.")
        strat_in = raw_input("Overhang correction strategy "
                             "(1=Trim, 2=Bridge, default 1): ").strip()
        if strat_in == '2':
            overhang_strategy = "bridge"
        elif strat_in not in ('', '1'):
            print("-> [v44][WARNING] Invalid strategy. Using Trim.")
        geom_in = raw_input("Save geometries (comma list: 1=best stiffness, "
                            "2=most mitigation, 3=final; default all): ").strip()
        if geom_in:
            _gmap = {"1": "best_stiffness", "2": "most_mitigation", "3": "final"}
            _sel = [_gmap[x.strip()] for x in geom_in.split(',')
                    if x.strip() in _gmap]
            if _sel:
                geometries_to_save = _sel
            else:
                print("-> [v44][WARNING] No valid geometries. Saving all three.")
        lt_in = raw_input("Lock-in threshold, stable passes before freeze "
                          "(default 5): ").strip()
        if lt_in:
            try:
                lock_threshold = max(1, int(lt_in))
            except:
                print("-> [v44][WARNING] Invalid threshold. Using 5.")
        nr_in = raw_input("Number of refine passes (default 35): ").strip()
        if nr_in:
            try:
                n_refine_passes = max(1, int(nr_in))
            except:
                print("-> [v44][WARNING] Invalid passes. Using 35.")
        cap_in = raw_input("Refine cap anneal range hi,lo "
                           "(default 2.0,0.5): ").strip()
        if cap_in:
            try:
                _cp = [float(x.strip()) for x in cap_in.split(',')]
                if len(_cp) == 2 and _cp[0] >= _cp[1] > 0:
                    refine_cap_hi, refine_cap_lo = _cp[0], _cp[1]
                else:
                    print("-> [v44][WARNING] Need hi >= lo > 0. Using 2.0,0.5.")
            except:
                print("-> [v44][WARNING] Invalid cap range. Using 2.0,0.5.")
    else:
        t_weight         = 0.0
        direction_choice = "Traditional"
        target_k22       = 5.0

    target_volume_fraction = 0.4
    filter_radius          = 3.0
    micro_radius           = 3.0
    TOTAL_APPLIED_FORCE    = 1000
    cpus                   = 1
    memory_percent         = 10
    # [v50] base_job / inp_path now come from prompt_for_base_inp() above.
    # [v44] strategy/geometry defaults now set before the thermal block.

    print("\n" + "="*50 + "\n")
    return (is_dynamic_er, is_gaussian_filter, custom_weights, t_weight,
            direction_choice, target_k22, target_volume_fraction,
            filter_radius, micro_radius, TOTAL_APPLIED_FORCE,
            cpus, memory_percent, base_job, inp_path, use_thermal,
            overhang_strategy, geometries_to_save,
            static_er, initial_er, final_er,
            support_reach, lock_threshold, n_refine_passes,
            refine_cap_hi, refine_cap_lo)

# =============================================================
# 13.5 RESUME + CHECKPOINT  [v48]
# -------------------------------------------------------------
# Exact resume of an interrupted run. Two paths:
#   1. Checkpoint fast-path: each iteration writes a small JSON (scalars +
#      histories) plus a pickle (previous_stabilized_sens, raw_sener_dict,
#      solid_list, void_list). Resume reloads them directly -- instant and
#      self-sufficient, no ODBs required.
#   2. ODB replay: if no checkpoint exists (a run that crashed before v48), the
#      stabilised-sensitivity buffer is a running average, so it is rebuilt by
#      replaying the stabilisation recurrence over ODBs 0..N and the design
#      state is parsed from iteration_<dir>_N.inp. Byte-for-byte identical to the
#      uninterrupted run for plain BESO.
# Files (per print direction) live in Data_Files/:
#   checkpoint_<dir>.json  and  checkpoint_<dir>.pkl
# =============================================================
import pickle   # [v48] protocol 2 -> readable by both Python 2.7 and 3.x

def _v48_ckpt_paths(dir_data, print_dir):
    base = os.path.join(dir_data, "checkpoint_{}".format(print_dir))
    return base + ".json", base + ".pkl"

def save_checkpoint(dir_data, print_dir, scalars, heavy):
    """Atomically write the per-direction checkpoint (JSON scalars + PKL heavy).
    The PKL is committed first and the JSON last, so a crash mid-write never
    leaves a JSON pointing at a stale or missing PKL."""
    json_path, pkl_path = _v48_ckpt_paths(dir_data, print_dir)
    try:
        with open(pkl_path + ".tmp", "wb") as f:
            pickle.dump(heavy, f, protocol=2)
        with open(json_path + ".tmp", "w") as f:
            json.dump(scalars, f)
        if os.path.exists(pkl_path):
            os.remove(pkl_path)
        os.rename(pkl_path + ".tmp", pkl_path)
        if os.path.exists(json_path):
            os.remove(json_path)
        os.rename(json_path + ".tmp", json_path)
    except Exception as e:
        print("   [v48][WARN] checkpoint write failed: {}".format(e))

def load_checkpoint(dir_data, print_dir):
    """Return (scalars, heavy) or None if no valid checkpoint is present."""
    json_path, pkl_path = _v48_ckpt_paths(dir_data, print_dir)
    if not (os.path.exists(json_path) and os.path.exists(pkl_path)):
        return None
    try:
        with open(json_path, "r") as f:
            scalars = json.load(f)
        with open(pkl_path, "rb") as f:
            heavy = pickle.load(f)
        return scalars, heavy
    except Exception as e:
        print("   [v48][WARN] checkpoint read failed ({}); ignoring it.".format(e))
        return None

def parse_elset_from_inp(inp_path, set_name):
    """Read an *Elset written by write_elset (16 ids/line) back into a list."""
    ids = []
    target = "*ELSET, ELSET={}".format(set_name.upper())
    in_set = False
    with open(inp_path, "r") as f:
        for line in f:
            us = line.strip().upper()
            if us.startswith("*ELSET"):
                in_set = us.startswith(target)
                continue
            if in_set:
                if us.startswith("*"):
                    in_set = False
                    continue
                for tok in line.split(","):
                    tok = tok.strip()
                    if tok:
                        ids.append(int(tok))
    return ids

def read_solid_void_from_inp(inp_path):
    solid = parse_elset_from_inp(inp_path, "SOLID_SET")
    void  = parse_elset_from_inp(inp_path, "VOID_SET")
    return solid, void

def replay_void_ratio(n_build_steps, max_void_ratio,
                      is_dynamic_er, initial_er, final_er, static_er):
    """Reproduce current_void_ratio after n_build_steps build increments,
    exactly as the master loop's build branch computes it."""
    vr = 0.0
    for _ in range(n_build_steps):
        if is_dynamic_er:
            if max_void_ratio > 0.0:
                progress = vr / max_void_ratio
            else:
                progress = 1.0
            er = initial_er - (progress * (initial_er - final_er))
        else:
            er = static_er
        vr += er
        if vr > max_void_ratio:
            vr = max_void_ratio
    return vr

def find_latest_iteration(dir_inp, dir_odb, print_dir):
    """Highest N (>=1) with BOTH iteration_<dir>_N.inp and a non-empty .odb."""
    best = None
    prefix = "iteration_{}_".format(print_dir)
    if not os.path.isdir(dir_inp):
        return None
    for name in os.listdir(dir_inp):
        if name.startswith(prefix) and name.endswith(".inp"):
            num = name[len(prefix):-4]
            try:
                n = int(num)
            except ValueError:
                continue
            if n < 1:
                continue
            odb = os.path.join(dir_odb, "iteration_{}_{}.odb".format(print_dir, n))
            if os.path.exists(odb) and os.path.getsize(odb) > 0:
                if best is None or n > best:
                    best = n
    return best

def _resume_from_checkpoint(scalars, heavy, expected_n_total):
    # [v50] Was a warning that returned None, which the caller could not tell
    # apart from "no checkpoint at all", so it fell through to ODB replay. ODB
    # replay has no fingerprint check of its own, so a mesh change could be
    # carried into the run on nothing but a single warning line. Now a hard stop.
    if scalars.get("n_elements_total") != expected_n_total:
        abort_mesh_mismatch("resume", scalars.get("n_elements_total"),
                            expected_n_total, scalars.get("print_dir", "?"))
    N   = int(scalars["iteration_completed"])
    lmv = scalars.get("local_min_violation_pct")
    if lmv is None:
        lmv = float("inf")
    print("   [v48] Resuming {} from CHECKPOINT at completed iteration {} "
          "(void ratio {:.4f}).".format(
          scalars["print_dir"], N, scalars["current_void_ratio"]))
    return (N + 1,
            float(scalars["current_void_ratio"]),
            str(scalars["run_phase"]),
            int(scalars["refine_pass"]),
            heavy["solid_list"], heavy["void_list"],
            heavy["raw_sener_dict"], heavy["previous_stabilized_sens"],
            list(scalars["iteration_history"]),
            list(scalars["compliance_history"]),
            list(scalars["volume_history"]),
            list(scalars["stiffness_history"]),
            list(scalars["violation_history"]),
            float(scalars["local_best_stiffness"]),
            int(scalars["local_best_iteration"]),
            lmv,
            int(scalars["local_best_mit_iteration"]),
            "iteration_{}_{}".format(scalars["print_dir"], N))

def _resume_from_odb_replay(print_dir, N, dir_inp, dir_odb,
                            master_weights, custom_weights, t_weight,
                            elem_volumes, V_TOTAL, global_design_part,
                            total_force, max_void_ratio, is_dynamic_er,
                            initial_er, final_er, static_er, use_thermal):
    print("   [v48] No checkpoint for {}. Rebuilding EXACT state by replaying "
          "ODBs 0..{} (reads each iteration's ODB once).".format(print_dir, N))
    vr_N = replay_void_ratio(N, max_void_ratio, is_dynamic_er,
                             initial_er, final_er, static_er)
    if use_thermal and vr_N >= max_void_ratio - 1e-12:
        print("   [v48][ERROR] Run appears to have entered the thermal REFINE "
              "phase; ODB replay supports the build phase only. A v48 checkpoint "
              "is required to resume a refine-phase run. Aborting resume.")
        return None

    def _read(odb_name):
        return get_sener_evol_temp_and_coords(
            odb_name, step_weights=custom_weights,
            design_part_name=global_design_part)

    iteration_history = []; compliance_history = []; volume_history = []
    stiffness_history = []; violation_history = []
    local_best_stiffness = 0.0; local_best_iteration = 0

    odb0 = os.path.join(dir_odb, "iteration_{}_0.odb".format(print_dir))
    raw_prev, evol0, _, _, _, _ = _read(odb0)
    comp0 = sum(raw_prev[e] * evol0.get(e, 0.25) for e in raw_prev)
    st0 = evaluate_specific_stiffness(odb0, total_force, 1.0,
                                      step_weights=custom_weights)
    iteration_history.append(0); compliance_history.append(comp0)
    volume_history.append(1.0); stiffness_history.append(st0)
    violation_history.append(0.0)
    if st0 > local_best_stiffness:
        local_best_stiffness = st0; local_best_iteration = 0

    stab = None
    for k in range(1, N + 1):
        # total_k is built from the sener of iteration k-1 (raw_prev), exactly as
        # the master loop does, then blended into the running stabilised buffer.
        norm = process_mechanical_branch(raw_prev, master_weights)
        total_k = {}
        for eid in norm:
            total_k[eid] = (1.0 - t_weight) * norm[eid]
        if stab is None:
            stab = total_k
        else:
            blended = {}
            for eid in total_k:
                blended[eid] = (total_k[eid] + stab[eid]) / 2.0
            stab = blended
        odbk = os.path.join(dir_odb, "iteration_{}_{}.odb".format(print_dir, k))
        rawk, evolk, _, _, _, _ = _read(odbk)
        compk = sum(rawk[e] * evolk.get(e, 0.25) for e in rawk)
        inpk = os.path.join(dir_inp, "iteration_{}_{}.inp".format(print_dir, k))
        solidk, voidk = read_solid_void_from_inp(inpk)
        solid_vol_k = 0.0
        for e in solidk:
            solid_vol_k += elem_volumes.get(e, 0.0)
        wp_k = solid_vol_k / V_TOTAL if V_TOTAL > 0.0 else 0.0
        stk = evaluate_specific_stiffness(odbk, total_force, wp_k,
                                          step_weights=custom_weights)
        iteration_history.append(k); compliance_history.append(compk)
        volume_history.append(wp_k); stiffness_history.append(stk)
        violation_history.append(0.0)
        if stk > local_best_stiffness:
            local_best_stiffness = stk; local_best_iteration = k
        raw_prev = rawk

    solidN, voidN = read_solid_void_from_inp(
        os.path.join(dir_inp, "iteration_{}_{}.inp".format(print_dir, N)))
    print("   [v48] Replay complete: stabilised buffer and histories rebuilt "
          "through iteration {} (void ratio {:.4f}).".format(N, vr_N))
    return (N + 1, vr_N, "build", 0,
            solidN, voidN, raw_prev, stab,
            iteration_history, compliance_history, volume_history,
            stiffness_history, violation_history,
            local_best_stiffness, local_best_iteration,
            float("inf"), 0,
            "iteration_{}_{}".format(print_dir, N))

def load_checkpoint_scalars(dir_data, print_dir):
    """Read ONLY the JSON scalars of a checkpoint (skips the heavy pickle)."""
    json_path, _ = _v48_ckpt_paths(dir_data, print_dir)
    if not os.path.exists(json_path):
        return None
    try:
        with open(json_path, "r") as f:
            return json.load(f)
    except Exception:
        return None

def _make_stage(kind, target_vf, iter_start, iter_end, er_desc):
    return {"kind": kind, "target_vf": target_vf,
            "iter_start": iter_start, "iter_end": iter_end, "er": er_desc}

def try_resume_direction(print_dir, dir_inp, dir_odb, dir_data,
                         max_void_ratio, expected_n_total,
                         master_weights, custom_weights, t_weight,
                         elem_volumes, V_TOTAL, global_design_part, total_force,
                         is_dynamic_er, initial_er, final_er, static_er,
                         use_thermal, base_target_vf):
    """Return (resume-state tuple + stage_ledger) for this direction, or None to
    start fresh. Prefers the checkpoint fast-path; otherwise replays the ODBs."""
    ck = load_checkpoint(dir_data, print_dir)
    if ck is not None:
        res = _resume_from_checkpoint(ck[0], ck[1], expected_n_total)
        if res is not None:
            stages = ck[0].get("stages", None)
            if not stages:
                stages = [_make_stage("base", base_target_vf, 0,
                                      int(ck[0]["iteration_completed"]), "unknown")]
            return res + (stages,)
    N = find_latest_iteration(dir_inp, dir_odb, print_dir)
    if N is None:
        print("   [v48] Resume requested but no checkpoint or completed "
              "iterations found for {}. Starting this direction fresh.".format(print_dir))
        return None
    try:
        res = _resume_from_odb_replay(
            print_dir, N, dir_inp, dir_odb, master_weights, custom_weights,
            t_weight, elem_volumes, V_TOTAL, global_design_part, total_force,
            max_void_ratio, is_dynamic_er, initial_er, final_er, static_er,
            use_thermal)
        if res is None:
            return None
        return res + ([_make_stage("base", base_target_vf, 0, N, "unknown")],)
    except Exception as e:
        print("   [v48][ERROR] ODB replay failed near iteration {} ({}). The last "
              "iteration's .odb/.inp may be truncated -- delete that pair so resume "
              "can fall back to N-1, then rerun.".format(N, e))
        return None

# =============================================================
# 13.6 CONTINUE (STAGED DESCENT)  [v49]
# -------------------------------------------------------------
# "continue" takes a FINISHED run and carves further, to a new lower whole-part
# target, on one continuous (typically fixed-ER) schedule -- a clean staged
# descent, not a resume. The finished topology becomes the new baseline: its
# solid set is loaded, the ramp is RE-BASED to the currently achieved domain void
# ratio (measured from that solid set by true element volume), and the stabilised
# buffer is seeded so the seam has no averaging artifact. Seeding uses the live
# checkpoint if present (fast path; a completed run's FINAL iteration never wrote
# a checkpoint, so the buffer is advanced one exact step from the penultimate
# one), else it is rebuilt by ODB replay 0..N. Element removal stays volume-true
# (separate_solid_void), so the new target is met by cumulative element volume
# exactly as in a normal run. A "stages" ledger in the checkpoint records every
# segment (base + each continue) with its iteration range, target and ER, so
# multiple continuations stack in one folder with full provenance.
# =============================================================
def _advance_stab_one(stab_prev, raw_prev, master_weights, t_weight):
    """One exact stabilisation step: stab_k = (total_k + stab_{k-1})/2, with
    total_k = (1 - t_weight) * filtered(raw_prev). Reproduces the master loop's
    recurrence for a single advance (to reach a completed run's final iteration,
    which itself never wrote a checkpoint)."""
    norm = process_mechanical_branch(raw_prev, master_weights)
    stab = {}
    for eid in norm:
        total_e = (1.0 - t_weight) * norm[eid]
        stab[eid] = (total_e + stab_prev[eid]) / 2.0 if eid in stab_prev else total_e
    return stab

def continue_seed(print_dir, dir_inp, dir_odb, dir_data, master_weights,
                  custom_weights, t_weight, elem_volumes, V_TOTAL, V_FROZEN,
                  V_DOMAIN, global_design_part, global_non_design_set, total_force,
                  max_void_ratio, is_dynamic_er, initial_er, final_er, static_er,
                  use_thermal):
    """Reconstruct the EXACT state of the source run's final archived iteration
    (solid set, raw sener, stabilised buffer, histories, trackers) plus the seam
    void ratio. Returns a dict, or None if there is nothing to continue from."""
    N = find_latest_iteration(dir_inp, dir_odb, print_dir)
    if N is None:
        print("   [v49][ERROR] continue: no completed iterations found for {}. "
              "Nothing to continue.".format(print_dir))
        return None

    inpN = os.path.join(dir_inp, "iteration_{}_{}.inp".format(print_dir, N))
    odbN = os.path.join(dir_odb, "iteration_{}_{}.odb".format(print_dir, N))
    solid_final, void_final = read_solid_void_from_inp(inpN)
    raw_final, evolN, _, _, _, _ = get_sener_evol_temp_and_coords(
        odbN, step_weights=custom_weights, design_part_name=global_design_part)

    ck = load_checkpoint(dir_data, print_dir)
    seeded = False
    if ck is not None:
        scalars, heavy = ck
        if scalars.get("n_elements_total") == len(elem_volumes):
            N_ck = int(scalars["iteration_completed"])
            ih = list(scalars["iteration_history"]); ch = list(scalars["compliance_history"])
            vh = list(scalars["volume_history"]);    sh = list(scalars["stiffness_history"])
            vih = list(scalars["violation_history"])
            lbs = float(scalars["local_best_stiffness"]); lbi = int(scalars["local_best_iteration"])
            lmv = scalars.get("local_min_violation_pct")
            lmv = float("inf") if lmv is None else float(lmv)
            lbmi = int(scalars["local_best_mit_iteration"])
            stages = list(scalars.get("stages", []))
            if N_ck == N:
                stab_final = heavy["previous_stabilized_sens"]
                print("   [v49] continue seed: checkpoint at final iteration {} "
                      "(fast path).".format(N))
                seeded = True
            elif N_ck == N - 1:
                stab_final = _advance_stab_one(
                    heavy["previous_stabilized_sens"], heavy["raw_sener_dict"],
                    master_weights, t_weight)
                compN = 0.0
                for e in raw_final:
                    compN += raw_final[e] * evolN.get(e, 0.25)
                solid_vol_N = 0.0
                for e in solid_final:
                    solid_vol_N += elem_volumes.get(e, 0.0)
                wpN = solid_vol_N / V_TOTAL if V_TOTAL > 0.0 else 0.0
                stN = evaluate_specific_stiffness(odbN, total_force, wpN,
                                                  step_weights=custom_weights)
                ih.append(N); ch.append(compN); vh.append(wpN); sh.append(stN); vih.append(0.0)
                if stN > lbs:
                    lbs = stN; lbi = N
                print("   [v49] continue seed: checkpoint at penultimate iteration "
                      "{}; advanced buffer one exact step to final iteration {} "
                      "(fast path).".format(N_ck, N))
                seeded = True
            else:
                print("   [v49] continue seed: checkpoint iteration {} not adjacent "
                      "to final {}; using ODB replay.".format(N_ck, N))
        else:
            # [v50] Same reasoning as the resume path: a checkpoint built from a
            # different mesh must not be papered over by falling back to replay.
            abort_mesh_mismatch("continue", scalars.get("n_elements_total"),
                                len(elem_volumes), print_dir)

    if not seeded:
        rep = _resume_from_odb_replay(
            print_dir, N, dir_inp, dir_odb, master_weights, custom_weights,
            t_weight, elem_volumes, V_TOTAL, global_design_part, total_force,
            max_void_ratio, is_dynamic_er, initial_er, final_er, static_er,
            use_thermal)
        if rep is None:
            return None
        solid_final = rep[4]; void_final = rep[5]; raw_final = rep[6]; stab_final = rep[7]
        ih, ch, vh, sh, vih = rep[8], rep[9], rep[10], rep[11], rep[12]
        lbs, lbi, lmv, lbmi = rep[13], rep[14], rep[15], rep[16]
        stages = []
        print("   [v49] continue seed: rebuilt final state via ODB replay 0..{}."
              .format(N))

    kept_dom = 0.0
    for e in solid_final:
        if e not in global_non_design_set:
            kept_dom += elem_volumes.get(e, 0.0)
    retention_seam = kept_dom / V_DOMAIN if V_DOMAIN > 0.0 else 0.0
    seam_void_ratio = 1.0 - retention_seam
    seam_wp = (V_FROZEN + kept_dom) / V_TOTAL if V_TOTAL > 0.0 else 0.0
    if not stages:
        stages = [_make_stage("base", None, 0, N, "unknown")]

    return {"N": N, "solid": solid_final, "void": void_final,
            "raw_sener": raw_final, "stab": stab_final,
            "ih": ih, "ch": ch, "vh": vh, "sh": sh, "vih": vih,
            "lbs": lbs, "lbi": lbi, "lmv": lmv, "lbmi": lbmi,
            "stages": stages, "seam_void_ratio": seam_void_ratio,
            "seam_wp": seam_wp, "retention_seam": retention_seam}

def try_continue_direction(print_dir, dir_inp, dir_odb, dir_data, master_weights,
                           custom_weights, t_weight, elem_volumes, V_TOTAL,
                           V_FROZEN, V_DOMAIN, global_design_part,
                           global_non_design_set, total_force, max_void_ratio,
                           continue_target_vf, er_desc, is_dynamic_er, initial_er,
                           final_er, static_er, use_thermal):
    """Build the loop-state tuple (+ stage ledger) for a new continue stage, or
    None on failure / infeasibility."""
    seed = continue_seed(
        print_dir, dir_inp, dir_odb, dir_data, master_weights, custom_weights,
        t_weight, elem_volumes, V_TOTAL, V_FROZEN, V_DOMAIN, global_design_part,
        global_non_design_set, total_force, max_void_ratio, is_dynamic_er,
        initial_er, final_er, static_er, use_thermal)
    if seed is None:
        return None

    # Feasibility: the new target must remove material -- its domain void ratio
    # (max_void_ratio, derived from continue_target) must exceed the seam.
    if max_void_ratio <= seed["seam_void_ratio"] + 1e-9:
        print("   [v49][ERROR] continue target VF {:.4f} (whole part) is not below "
              "the current achieved VF {:.4f}. BESO only removes material, so there "
              "is nothing to carve. Choose a lower target.".format(
              continue_target_vf, seed["seam_wp"]))
        return None

    N = seed["N"]
    stages = seed["stages"]
    stages.append(_make_stage("continue", continue_target_vf, N + 1, None, er_desc))
    print("   [v49] CONTINUE stage: from iteration {} (whole-part VF {:.4f}) down "
          "to target {:.4f} on {}. Seam domain void ratio {:.4f} -> {:.4f}.".format(
          N, seed["seam_wp"], continue_target_vf, er_desc,
          seed["seam_void_ratio"], max_void_ratio))

    return (N + 1, seed["seam_void_ratio"], "build", 0,
            seed["solid"], seed["void"], seed["raw_sener"], seed["stab"],
            seed["ih"], seed["ch"], seed["vh"], seed["sh"], seed["vih"],
            seed["lbs"], seed["lbi"], seed["lmv"], seed["lbmi"],
            "iteration_{}_{}".format(print_dir, N), stages)

# =============================================================
# 14. MASTER LOOP
# =============================================================
if __name__ == "__main__":

    CONFIG_FILE = "beso_config.json"

    print("-> Loading Abaqus API and Math Libraries... (Please wait a few seconds)")
    import numpy as np
    from scipy.spatial import cKDTree
    from scipy.sparse import csr_matrix
    import matplotlib.pyplot as plt
    from odbAccess import openOdb

    print("\n-> Setting up clean directories...")
    for directory in [DIR_INP, DIR_ODB, DIR_MISC, DIR_DATA, DIR_SCRATCH]:
        if not os.path.exists(directory):
            os.makedirs(directory)

    # --- v20: start logging all terminal output to a file ---
    _log_timestamp = time.strftime("%Y%m%d_%H%M%S")
    _log_path = os.path.join(DIR_DATA, "beso_run_log_{}.txt".format(_log_timestamp))
    _tee = _Tee(_log_path)
    sys.stdout = _tee
    print("-> Log file: {}".format(_log_path))

    (is_dynamic_er, is_gaussian_filter, custom_weights, t_weight,
     direction_choice, target_k22, target_volume_fraction,
     filter_radius, micro_radius, TOTAL_APPLIED_FORCE,
     cpus, memory_percent, base_job, inp_path, use_thermal,
     overhang_correction_strategy, geometries_to_save,
     static_er, initial_er, final_er,
     support_reach, lock_threshold, n_refine_passes,
     refine_cap_hi, refine_cap_lo) = load_config_or_prompt()

    # [v48] Resume flag (read separately so the settings tuple is untouched).
    # [v49] Continue flag + target read the same way.
    RESUME = False
    CONTINUE = False
    continue_target_volume_fraction = None
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as _rf:
                _cfg_raw = json.load(_rf)
            RESUME = bool(_cfg_raw.get("resume", False))
            CONTINUE = bool(_cfg_raw.get("continue", False))
            continue_target_volume_fraction = _cfg_raw.get(
                "continue_target_volume_fraction", None)
        except Exception:
            RESUME = False; CONTINUE = False
            continue_target_volume_fraction = None

    if RESUME and CONTINUE:
        print("   [v49][ERROR] Both 'resume' and 'continue' are true. These are "
              "different operations (resume = pick up an interrupted stage as-is; "
              "continue = start a NEW lower-VF stage). Set exactly one. Aborting.")
        sys.exit(1)
    if CONTINUE:
        if use_thermal:
            print("   [v49][ERROR] 'continue' is a single-direction staged descent "
                  "and does not support thermal multi-direction runs. Aborting.")
            sys.exit(1)
        if continue_target_volume_fraction is None:
            print("   [v49][ERROR] 'continue' is true but 'continue_target_volume_"
                  "fraction' is missing. Aborting.")
            sys.exit(1)
        print("-> [v49] CONTINUE MODE ENABLED: staging a new descent to whole-part "
              "VF {} on the finished topology in this folder.".format(
              continue_target_volume_fraction))
    if RESUME:
        print("-> [v48] RESUME MODE ENABLED: interrupted direction(s) will "
              "continue from checkpoint or exact ODB replay.")

    # [v44] Effective final ER -- the refine promotion cap base scales with
    # this. Fixed -> the chosen static ER; Dynamic -> the smaller (end) value.
    er_final = static_er if not is_dynamic_er else final_er
    print("-> [v44] Build ER: {} | refine-cap base ER: {}".format(
          ("fixed {}".format(static_er) if not is_dynamic_er
           else "dynamic {}->{}".format(initial_er, final_er)), er_final))

    # [v42] Announce the active overhang correction strategy once at startup.
    print("-> [v42] Overhang correction strategy: {}".format(
          "Trim (v40 soundness repair)" if overhang_correction_strategy == "trim"
          else "Bridge (v41 bridge-builder)"))

    # [v43] Output subfolders for the multi-geometry save (handoff 18.1).
    # Each holds the standard CSV triplet (fixed names), so they cannot
    # collide. Only created (and only used) when mitigation is enabled.
    DIR_GEOM_STIFF = os.path.join(DIR_DATA, "geom_best_stiffness")
    DIR_GEOM_MIT   = os.path.join(DIR_DATA, "geom_most_mitigation")
    DIR_GEOM_FINAL = os.path.join(DIR_DATA, "geom_final")
    # [v45] Create the geometry subfolders that will actually be used.
    # Thermal runs can save all three; plain BESO uses only geom_final (best
    # stiffness still goes to the Data_Files root). most_mitigation is
    # thermal-only and is ignored for plain runs.
    if use_thermal:
        _geom_dirs = [(DIR_GEOM_STIFF, "best_stiffness"),
                      (DIR_GEOM_MIT,   "most_mitigation"),
                      (DIR_GEOM_FINAL, "final")]
        _effective_saves = list(geometries_to_save)
    else:
        _geom_dirs = [(DIR_GEOM_FINAL, "final")]
        _effective_saves = [g for g in geometries_to_save
                            if g in ("best_stiffness", "final")]
    for _gd, _gk in _geom_dirs:
        if _gk in geometries_to_save and not os.path.isdir(_gd):
            os.makedirs(_gd)   # py2/3-safe guarded create
    print("-> [v45] Geometry saves enabled ({}): {}".format(
          "thermal" if use_thermal else "plain BESO",
          ", ".join(_effective_saves) if _effective_saves else "(none)"))

    # [v47] max_void_ratio is now DERIVED from the measured mesh volumes after
    # pre-flight, because target_volume_fraction is interpreted as a WHOLE-PART
    # volume fraction. It is computed further below once element volumes exist.

    # v32: delay the overhang mitigation until the structure has carved down
    # to this solid fraction. While the current solid fraction is above this
    # value the promotion and penalty are inert and the run behaves as pure
    # mechanical BESO (the thermal step still solves, so NT11 is ready when the
    # gate opens). Once the solid fraction falls to the threshold they engage
    # at full strength. Rationale: early high-cap promotion otherwise fragments
    # the natural few-large void carving into a many-small pattern that then
    # perforates; delaying lets the topology form first and treats overhangs as
    # a later refining stage. Set to 1.0 to recover the v31 (always-on) behaviour.
    thermal_start_solid_fraction = 0.65

    # v33: refine-pass settings. After the build phase reaches the target VF,
    # run this many constant-VF passes with the overhang mitigation on. The
    # refine promotion cap is tied to the final evolution rate (no net removal
    # at constant VF) and annealed from refine_cap_hi down to refine_cap_lo of
    # that base across the passes.
    # [v44] n_refine_passes, lock_threshold, refine_cap_hi, refine_cap_lo are
    # now user-set (load_config_or_prompt) and unpacked above. The cap base
    # tracks er_final, not a hard-coded 0.005.
    face_adj_factor = 1.05  # v39: face-only grounding for the soundness repair.
                            # Includes face neighbours at 1.0*elem_size, excludes
                            # diagonals at 1.414. The swap uses this; the thermal
                            # promotion anchor keeps its own 1.5 (unchanged).
    diag_adj_factor = 1.5   # v40: diagonal-inclusive grounding for Phase A
                            # (genuine-floater batch removal, the v37 definition).

    # [v50] Resolve the base model before anything reads it. On a resume or a
    # continue the archived copy inside this run folder wins, so moving or
    # renaming the original afterwards cannot break the run.
    _base_info = resolve_base_model(inp_path, base_job, DIR_INP,
                                    prefer_archive=(RESUME or CONTINUE))
    if _base_info is None:
        sys.exit(1)
    base_job      = _base_info["job_name"]
    base_inp_path = _base_info["source"]

    _prev_manifest = read_base_manifest(DIR_DATA)
    if (RESUME or CONTINUE) and _prev_manifest is not None:
        print("-> [v50] This run folder was started from {} on {}.".format(
              _prev_manifest.get("original_path", "an unknown path"),
              _prev_manifest.get("recorded", "an unknown date")))
        if _prev_manifest.get("md5") and _base_info["md5"] and \
           _prev_manifest.get("md5") != _base_info["md5"]:
            print("\n   [v50][ERROR] The model being used now is NOT the model this")
            print("   folder was built from.")
            print("     recorded: {}  (md5 {})".format(
                  _prev_manifest.get("original_path"), _prev_manifest.get("md5")))
            print("     current:  {}  (md5 {})".format(
                  base_inp_path, _base_info["md5"]))
            print("   Resuming or continuing across two different models would")
            print("   silently corrupt the result. Aborting.")
            sys.exit(1)

    print("-> Loading base INP file into RAM...")
    with open(base_inp_path, 'r') as f:
        ram_base_inp_lines = f.readlines()

    # FIX: use smart multi-part detection instead of get_non_design_elements
    global_design_part, global_non_design_set = find_design_part_and_non_design(
        base_inp_path, set_name="NON_DESIGN_SET")
    if len(global_non_design_set) == 0:
        print("   [WARNING] No NON_DESIGN_SET elements detected - the ENTIRE mesh will be")
        print("             treated as design space. If you intended a protected region,")
        print("             check the set name and its definition in the INP.")

    # --- One-time export of the non-design set for the STL generator ---
    nd_csv_path = os.path.join(DIR_DATA, "non_design_elements.csv")
    with _csv_open(nd_csv_path) as nd_file:
        nd_writer = csv.writer(nd_file)
        nd_writer.writerow(["element_id"])
        for eid in sorted(global_non_design_set):
            nd_writer.writerow([eid])
    print("-> Wrote {} non-design element IDs to {}".format(
        len(global_non_design_set), nd_csv_path))

    # PRE-FLIGHT: run base model once to extract geometry
    # [v50] A resume or continue serves this from the cache in Data_Files when
    # the base model is byte-identical to the one the cache was built from. The
    # key is the size and md5 of the file on disk, re-read now, so a cache hit
    # proves the mesh is unchanged. Any difference misses the cache and the full
    # solve runs, after which the usual fingerprint checks apply as normal. A
    # fresh run always solves and always rewrites the cache.
    _preflight_key = {"md5": _base_info["md5"], "size": _base_info["size"]}
    _preflight = None
    if RESUME or CONTINUE:
        print("\n-> PRE-FLIGHT: looking for cached geometry for this run folder...")
        _preflight = load_preflight_cache(DIR_DATA, _preflight_key)

    if _preflight is not None:
        preflight_evol      = _preflight["evol"]
        initial_coords      = _preflight["coord_data"]
        node_coords         = _preflight["node_coords"]
        global_connectivity = _preflight["connectivity"]
        elem_volumes        = _preflight["elem_volumes"]
        print("   Restored {} elements and {} nodes without solving.".format(
              len(global_connectivity), len(node_coords)))
    else:
        print("\n-> PRE-FLIGHT: Extracting Global Geometry from Base File...")
        _local_base_inp, _staged_is_copy = stage_base_model(base_inp_path, base_job)
        run_abaqus_job(base_job, base_job, cpus, memory_percent)
        _, preflight_evol, _, initial_coords, node_coords, global_connectivity = \
            get_sener_evol_temp_and_coords(base_job + ".odb",
                                           step_weights=custom_weights,
                                           design_part_name=global_design_part)   # FIX

        # [v47] True geometric per-element volumes from the reference mesh, computed
        # once and reused every iteration to make the volume fraction VOLUME-true.
        elem_volumes = compute_element_volumes(global_connectivity, node_coords,
                                               evol_fallback=preflight_evol)

        # [v50] Tidy the pre-flight artefacts (the working copy is archived into
        # INP_Files, the ODB into ODB_Files, the rest into Misc_Abaqus_Files),
        # then cache the geometry and record which model this folder belongs to.
        _archived_base = archive_preflight_artifacts(base_job, DIR_INP, DIR_ODB,
                                                     DIR_MISC, _staged_is_copy)
        _preflight_key["n_elements"] = len(global_connectivity)
        save_preflight_cache(DIR_DATA, _preflight_key, {
            "evol":         _plain_float_map(preflight_evol),
            "coord_data":   _plain_tuple_map(initial_coords),
            "node_coords":  _plain_tuple_map(node_coords),
            "connectivity": _plain_connectivity(global_connectivity),
            "elem_volumes": _plain_float_map(elem_volumes)})
        write_base_manifest(DIR_DATA, {
            "original_path": _base_info["source"],
            "found_via":     _base_info["origin"],
            "job_name":      base_job,
            "size_bytes":    _base_info["size"],
            "md5":           _base_info["md5"],
            "n_elements":    len(global_connectivity),
            "archived_copy": _archived_base,
            "recorded":      time.strftime("%Y-%m-%d %H:%M:%S")})

    # [v47] Whole-part / frozen / design-domain volume split (measured, mm^3).
    V_FROZEN = 0.0
    V_DOMAIN = 0.0
    for _eid in elem_volumes:
        if _eid in global_non_design_set:
            V_FROZEN += elem_volumes[_eid]
        else:
            V_DOMAIN += elem_volumes[_eid]
    V_TOTAL = V_FROZEN + V_DOMAIN
    if V_TOTAL <= 0.0:
        print("   [v47][ERROR] Total measured element volume is zero. Cannot target a "
              "volume fraction. Check that EVOL is present or the mesh is supported.")
        sys.exit(1)
    vf_floor = V_FROZEN / V_TOTAL

    # [v47] The user's target_volume_fraction is a fraction of the WHOLE optimised
    # part (design space = frozen + design domain). Derive the internal
    # design-domain retention (and hence void ratio) from the measured volumes:
    #   kept_domain = VF_total*V_TOTAL - V_FROZEN ;  vf_domain = kept_domain/V_DOMAIN
    # The frozen NON_DESIGN_SET is always kept, so VF_total cannot go below vf_floor.
    # [v49] Resolve the effective whole-part target for THIS launch. Continue uses
    # its new lower target; a resume of an in-progress continuation reads the last
    # stage's target from the checkpoint ledger (scalars only, no heavy pickle) so
    # max_void_ratio matches the stage that was running; otherwise the config value.
    effective_target_vf = target_volume_fraction
    er_stage_desc = ("fixed {}".format(static_er) if not is_dynamic_er
                     else "dynamic {}->{}".format(initial_er, final_er))
    if CONTINUE:
        effective_target_vf = continue_target_volume_fraction
    elif RESUME:
        _peek = load_checkpoint_scalars(DIR_DATA, direction_choice)
        if _peek is not None:
            _st = _peek.get("stages", [])
            if _st and _st[-1].get("target_vf") is not None:
                effective_target_vf = _st[-1]["target_vf"]
    vf_total_requested = effective_target_vf
    if vf_total_requested > 1.0:
        print("   [v47][WARN] target VF {:.4f} > 1.0; clamping to 1.0 (keep whole "
              "part).".format(vf_total_requested))
        vf_total_requested = 1.0
    if vf_total_requested < vf_floor - 1e-12:
        print("   [v47][WARN] target whole-part VF {:.4f} is below the feasible floor "
              "{:.4f} (= V_frozen/V_total). The frozen NON_DESIGN_SET can never be "
              "removed, so the run will keep ONLY the frozen set (achieved VF {:.4f}). "
              "Raise the target above the floor to keep design material."
              .format(vf_total_requested, vf_floor, vf_floor))
        vf_domain = 0.0
    else:
        vf_domain = (vf_total_requested * V_TOTAL - V_FROZEN) / V_DOMAIN
        if vf_domain < 0.0:
            vf_domain = 0.0
        if vf_domain > 1.0:
            vf_domain = 1.0

    max_void_ratio  = 1.0 - vf_domain
    predicted_final = V_FROZEN + vf_domain * V_DOMAIN

    print("-> [v47] Measured mesh volumes:")
    print("     V_total (design space) : {:.6e} mm^3".format(V_TOTAL))
    print("     V_frozen (non-design)  : {:.6e} mm^3 ({:.2f}% of total)".format(
          V_FROZEN, 100.0 * V_FROZEN / V_TOTAL))
    print("     V_design_domain        : {:.6e} mm^3 ({:.2f}% of total)".format(
          V_DOMAIN, 100.0 * V_DOMAIN / V_TOTAL))
    print("     Feasible whole-part VF floor (keep frozen only): {:.4f}".format(vf_floor))
    print("-> [v47] Target whole-part VF {:.4f} -> design-domain retention {:.4f} "
          "(domain void ratio {:.4f}).".format(
          vf_total_requested, vf_domain, max_void_ratio))
    print("-> [v47] Predicted FINAL solid volume: {:.6e} mm^3 ({:.2f}% of the design "
          "space).".format(predicted_final, 100.0 * predicted_final / V_TOTAL))
    if max_void_ratio <= 0.0:
        print("-> [v47] Target keeps the entire part; no material will be removed.")

    master_weights = prepare_filter_weights(
        initial_coords,
        filter_radius,
        is_gaussian_filter
    )

    if filter_radius == micro_radius:
        print("-> Reusing structural neighbour map for thermal filter (same radius).")
        micro_weights = master_weights
    else:
        micro_weights = prepare_filter_weights(
            initial_coords,
            micro_radius,
            is_gaussian_filter
        )

    all_design_elements = [eid for eid in initial_coords.keys()
                           if eid not in global_non_design_set]
    initial_solid_list  = list(global_non_design_set) + all_design_elements
    initial_void_list   = []

    print_directions     = [direction_choice]
    global_best_stiffness  = 0.0
    global_best_direction  = None
    global_best_iteration  = 0
    start_time             = time.time()

    for print_dir in print_directions:
        print("\n" + "#"*60)
        print("   TESTING PRINT ORIENTATION: {}".format(print_dir))
        print("#"*60)

        current_void_ratio       = 0.0
        iteration                = 1
        iteration_history        = []
        compliance_history       = []
        volume_history           = []
        stiffness_history        = []
        violation_history        = []
        previous_stabilized_sens = None
        lock_streak = {}   # v36: eid -> [state(1 solid/0 void), consecutive count]
        solid_list               = list(initial_solid_list)
        local_best_stiffness     = 0.0
        local_best_iteration     = 0
        local_min_violation_pct  = float("inf")   # [v43] most-mitigation track
        local_best_mit_iteration = 0               # [v43]

        # [v48/v49] Reconstruct this direction's state. CONTINUE starts a new
        # staged descent from the finished topology; RESUME picks up an interrupted
        # stage; otherwise a normal fresh start. All skip the iteration-0 baseline
        # when they fire.
        resumed        = False
        run_phase      = "build"
        refine_pass    = 0
        raw_sener_dict = None
        evol_dict      = {}
        temp_dict      = {}
        void_list      = []
        stage_ledger   = None
        if CONTINUE:
            _cs = try_continue_direction(
                    print_dir, DIR_INP, DIR_ODB, DIR_DATA, master_weights,
                    custom_weights, t_weight, elem_volumes, V_TOTAL, V_FROZEN,
                    V_DOMAIN, global_design_part, global_non_design_set,
                    TOTAL_APPLIED_FORCE, max_void_ratio,
                    continue_target_volume_fraction, er_stage_desc,
                    is_dynamic_er, initial_er, final_er, static_er, use_thermal)
            if _cs is None:
                print("   [v49] Continue could not start for {}. Aborting.".format(print_dir))
                sys.exit(1)
            (iteration, current_void_ratio, run_phase, refine_pass,
             solid_list, void_list, raw_sener_dict, previous_stabilized_sens,
             iteration_history, compliance_history, volume_history,
             stiffness_history, violation_history,
             local_best_stiffness, local_best_iteration,
             local_min_violation_pct, local_best_mit_iteration,
             current_job, stage_ledger) = _cs
            resumed = True
        elif RESUME:
            _rs = try_resume_direction(
                    print_dir, DIR_INP, DIR_ODB, DIR_DATA,
                    max_void_ratio, len(elem_volumes),
                    master_weights, custom_weights, t_weight,
                    elem_volumes, V_TOTAL, global_design_part,
                    TOTAL_APPLIED_FORCE, is_dynamic_er, initial_er, final_er,
                    static_er, use_thermal, target_volume_fraction)
            if _rs is not None:
                (iteration, current_void_ratio, run_phase, refine_pass,
                 solid_list, void_list, raw_sener_dict, previous_stabilized_sens,
                 iteration_history, compliance_history, volume_history,
                 stiffness_history, violation_history,
                 local_best_stiffness, local_best_iteration,
                 local_min_violation_pct, local_best_mit_iteration,
                 current_job, stage_ledger) = _rs
                resumed = True

        if stage_ledger is None:
            stage_ledger = [_make_stage("base", target_volume_fraction, 0, None,
                                        er_stage_desc)]

        if not resumed:
            # ITERATION 0: baseline
            current_job = "iteration_{}_0".format(print_dir)
            create_new_inp(ram_base_inp_lines, current_job + ".inp",
                           initial_solid_list, initial_void_list,
                           print_dir, node_coords, target_k22,
                           use_thermal=use_thermal,
                           design_part_name=global_design_part)               # FIX
            run_abaqus_job(current_job, current_job, cpus, memory_percent)

            raw_sener_dict, evol_dict, temp_dict, _, _, _ = \
                get_sener_evol_temp_and_coords(current_job + ".odb",
                                               step_weights=custom_weights,
                                               design_part_name=global_design_part)  # FIX

            initial_compliance = sum(raw_sener_dict[eid] * evol_dict.get(eid, 0.25)
                                     for eid in raw_sener_dict)
            iteration_history.append(0)
            compliance_history.append(initial_compliance)
            volume_history.append(1.0)

            print("\n   --- ITERATION 0 (BASELINE) METRICS ---")
            init_stiffness = evaluate_specific_stiffness(
                current_job + ".odb", TOTAL_APPLIED_FORCE, 1.0,
                step_weights=custom_weights)
            stiffness_history.append(init_stiffness)
            violation_history.append(0.0)
            cleanup_files(current_job, keep_inp=False)

        # MAIN BESO LOOP  (v33 two-phase: build carves to target VF with
        # mitigation OFF; refine holds VF and runs the mitigation passes. For
        # plain BESO only the build phase runs. run_phase/refine_pass are set
        # above -- fresh = build/0, or carried over by a resume.)
        # [v44] initial_er / final_er / static_er come from settings (above).
        while True:
            if run_phase == "build":
                if is_dynamic_er:
                    if max_void_ratio > 0.0:                     # [v47] div-by-zero guard
                        progress = current_void_ratio / max_void_ratio
                    else:
                        progress = 1.0
                    current_er = initial_er - (progress * (initial_er - final_er))
                else:
                    current_er = static_er
                current_void_ratio += current_er
                if current_void_ratio > max_void_ratio:
                    current_void_ratio = max_void_ratio
                thermal_active = False
                phase_label    = "BUILD"
            else:
                refine_pass       += 1
                current_void_ratio = max_void_ratio
                current_er         = er_final
                thermal_active     = bool(use_thermal)
                phase_label        = "REFINE {}/{}".format(refine_pass, n_refine_passes)

            print("\n" + "="*40)
            print("      GENERATING DESIGN ITERATION {} [{}]".format(iteration, phase_label))
            print("="*40)
            print("-> Target Void Fraction: {:.1f}%".format(current_void_ratio * 100))
            # [v47] Also report the equivalent WHOLE-PART solid volume fraction.
            _wp_target = (V_FROZEN + (1.0 - current_void_ratio) * V_DOMAIN) / V_TOTAL
            print("-> Target whole-part volume fraction: {:.2f}% ({:.4e} mm^3)".format(
                  _wp_target * 100.0, _wp_target * V_TOTAL))
            print("-> Evolution Speed:      {:.3f}%".format(current_er * 100))
            if run_phase == "build" and use_thermal:
                print("-> Overhang mitigation: INACTIVE (build phase)")
            elif thermal_active:
                print("-> Overhang mitigation: ACTIVE (refine pass {}/{})".format(
                      refine_pass, n_refine_passes))

            # Mechanical sensitivity is needed by the promotion gate, so compute
            # it first (its value is independent of the thermal terms).
            norm_mech_dict = process_mechanical_branch(raw_sener_dict, master_weights)

            if thermal_active:
                active_violators, current_violation_pct = get_active_violators(
                    solid_list, initial_coords, print_dir)
                penalty_dict = process_thermal_branch(
                    temp_dict, active_violators, micro_weights, solid_list)
                # v33: in the refine phase there is no net removal, so tie the
                # promotion cap to the final evolution rate and anneal it down
                # across the passes (larger early to reshape, smaller late so it
                # settles to a fixed point instead of churning).
                n_design     = len(norm_mech_dict)
                base_cap     = 0.5 * er_final * n_design
                if n_refine_passes > 1:
                    anneal_frac = float(refine_pass - 1) / float(n_refine_passes - 1)
                else:
                    anneal_frac = 0.0
                cap_scale    = refine_cap_hi - anneal_frac * (refine_cap_hi - refine_cap_lo)
                max_promote  = max(1, int(cap_scale * base_cap))
                print("   [v33] Refine cap: {} (scale {:.2f} x base {:.0f}, pass {}/{})".format(
                      max_promote, cap_scale, base_cap, refine_pass, n_refine_passes))
                bonus_dict, promoted_set, candidate_set = process_thermal_promotion(
                    temp_dict, active_violators, solid_list, initial_coords,
                    print_dir, micro_weights, norm_mech_dict, max_promote,
                    support_reach=support_reach)
            else:
                current_violation_pct = 0.0
                penalty_dict = {}
                bonus_dict   = {}
                promoted_set = set()
                candidate_set = set()

            # v22: multiplicative penalty (heat can demote a load path but never
            # delete it -- a solid element keeps at least norm_mech*(1-t_weight))
            # plus a gated additive support-promotion bonus on void elements. When
            # thermal is off, or when t_weight = 0, this reduces exactly to the
            # pure-mechanical ranking (1 - t_weight) * norm_mech, preserving the
            # baseline byte-for-byte.
            total_sens_dict = {}
            for eid in norm_mech_dict:
                m = norm_mech_dict[eid]
                if thermal_active:
                    p = penalty_dict.get(eid, 0.0)
                    b = bonus_dict.get(eid, 0.0)
                    total_sens_dict[eid] = m * (1.0 - t_weight * p) + (t_weight * b)
                elif use_thermal:
                    # v32 delay phase: thermal solved but not applied -> pure mechanical
                    total_sens_dict[eid] = m
                else:
                    total_sens_dict[eid] = (1.0 - t_weight) * norm_mech_dict[eid]

            if previous_stabilized_sens is None:
                stabilized_sens_dict = total_sens_dict
            else:
                stabilized_sens_dict = {
                    eid: (total_sens_dict[eid] + previous_stabilized_sens[eid]) / 2.0
                    for eid in total_sens_dict}
            previous_stabilized_sens = stabilized_sens_dict

            prev_solid_set = set(solid_list)
            frozen_for_inp = None   # v39: per-pass locked-cell elset (refine only)
            repair_removed_for_inp = None   # v40: repair-removed elset (refine)
            bridge_added_for_inp = None   # v41: bridge-added elset (refine)
            if run_phase == "refine":
                # v36: freeze cells stable for >= lock_threshold passes; partition
                # only the un-frozen remainder so the core stops churning.
                frozen_solid = set(e for e, sc in lock_streak.items()
                                   if sc[0] == 1 and sc[1] >= lock_threshold)
                frozen_void  = set(e for e, sc in lock_streak.items()
                                   if sc[0] == 0 and sc[1] >= lock_threshold)
                frozen_for_inp = set(frozen_solid)   # v39: locked solids for elset
                solid_list, void_list = separate_solid_void_locked(
                    stabilized_sens_dict, current_void_ratio,
                    global_non_design_set, frozen_solid, frozen_void, elem_volumes)
                # v35: floating-swap repair on the locked partition. A swap may
                # demote a frozen-solid cell that became a floater (or rescue a
                # frozen-void cell), overriding the freeze; the streak update below
                # runs on the POST-swap design and resets any such cell, so the two
                # features stay consistent.
                void_ranked = sorted(void_list,
                                     key=lambda e: stabilized_sens_dict.get(e, 0.0),
                                     reverse=True)
                # [v42] Dispatch to the selected overhang correction strategy.
                # Both repair families share the call shape except for the one
                # extra design-set argument the Bridge builder needs and their
                # distinct return tuples; the diagnostic elsets they feed into
                # create_new_inp are guarded, so the unused one stays None.
                if overhang_correction_strategy == "trim":
                    swap_result = soundness_repair_phased(
                        set(solid_list), void_ranked, initial_coords, print_dir,
                        face_adj_factor, diag_adj_factor, 500,
                        sens_lookup=stabilized_sens_dict)
                    (solid_fixed, n_swapped, swap_unresolved, swap_steps,
                     clean_A, clean_B, clean_C, resolved_rr, orphans_n,
                     repair_removed) = swap_result
                    repair_removed_for_inp = set(repair_removed)
                    msg = ("   [v37] Lock-in {} frozen | [v40] floaters(A) {} | "
                           "node-clusters(B) {} | checker(C) {} | rescued {} | "
                           "resolved-by-rescue {} | rescue-orphans {} | "
                           "steps {}").format(
                               len(frozen_solid) + len(frozen_void),
                               clean_A, clean_B, clean_C, n_swapped,
                               resolved_rr, orphans_n, swap_steps)
                else:
                    swap_result = bridge_repair_phased(
                        set(solid_list), void_ranked, initial_coords, print_dir,
                        face_adj_factor, diag_adj_factor, 500,
                        set(all_design_elements),
                        sens_lookup=stabilized_sens_dict)
                    (solid_fixed, n_bridges, n_balance, n_fallback, n_overshoot,
                     clean_A, swap_unresolved, swap_steps,
                     repair_removed, bridge_added) = swap_result
                    repair_removed_for_inp = set(repair_removed)
                    bridge_added_for_inp = set(bridge_added)
                    msg = ("   [v37] Lock-in {} frozen | [v41] floaters(A) {} | "
                           "bridges {} | balance-removed {} | fallback {} | "
                           "VF-overshoot {} | steps {}").format(
                               len(frozen_solid) + len(frozen_void),
                               clean_A, n_bridges, n_balance, n_fallback,
                               n_overshoot, swap_steps)
                solid_list = sorted(solid_fixed)
                void_list  = [e for e in all_design_elements
                              if e not in solid_fixed]
                new_solid = solid_fixed
                for eid in stabilized_sens_dict:
                    if eid in global_non_design_set:
                        continue
                    st = 1 if eid in new_solid else 0
                    if eid in lock_streak and lock_streak[eid][0] == st:
                        lock_streak[eid][1] += 1
                    else:
                        lock_streak[eid] = [st, 1]
                if swap_unresolved:
                    msg += " | {} unresolved (VF undershoot)".format(swap_unresolved)
                print(msg)
            else:
                solid_list, void_list = separate_solid_void(
                    stabilized_sens_dict, current_void_ratio,
                    global_non_design_set, elem_volumes)
            # v26: elements that were solid last iteration and are void now -- the
            # displacement (evolution removal and promotion-displaced together).
            removed_set = prev_solid_set - set(solid_list)

            # v33: convergence metric -- cells that flipped solid<->void since
            # the previous pass. At constant VF (refine) it should decay toward
            # zero as the passes settle; a non-decaying value means churn.
            n_flipped = len(prev_solid_set ^ set(solid_list))
            if run_phase == "refine":
                print("   [v33] REFINE {}/{}: {} cells flipped | violators {} | "
                      "VF {:.1f}%".format(refine_pass, n_refine_passes, n_flipped,
                      len(active_violators), (1.0 - current_void_ratio) * 100.0))

            # v23: how many of the promoted void elements actually won re-admission
            # to solid. If this is near zero, the bonus is too weak vs the threshold.
            readmitted_set = set()
            if use_thermal and promoted_set:
                readmitted_set = promoted_set & set(solid_list)
                print("   [v23] Re-admitted from promotion: {}/{}".format(
                      len(readmitted_set), len(promoted_set)))

            # v20: re-detect overhangs on the POST-removal geometry purely for the
            # ACTIVE_VIOLATORS elset, so the overlay aligns exactly with the solid
            # geometry written into this INP. The penalty above intentionally uses
            # the PRE-removal violators (active_violators) -- the penalty must be
            # known before deciding removal, so it cannot use this post-removal set.
            if use_thermal:
                viz_violators, _ = get_active_violators(
                    solid_list, initial_coords, print_dir,
                    label="ACTIVE_VIOLATORS elset (post-removal)")
            else:
                viz_violators = set()

            next_job = "iteration_{}_{}".format(print_dir, iteration)
            create_new_inp(ram_base_inp_lines, next_job + ".inp",
                           solid_list, void_list,
                           print_dir, node_coords, target_k22,
                           use_thermal=use_thermal,
                           design_part_name=global_design_part,
                           violator_set=viz_violators if use_thermal else None,  # v20
                           promoted_set=promoted_set if use_thermal else None,
                           readmitted_set=readmitted_set if use_thermal else None,
                           candidate_set=candidate_set if use_thermal else None,
                           removed_set=removed_set if use_thermal else None,
                           frozen_set=frozen_for_inp,
                           repair_removed_set=repair_removed_for_inp,
                           bridge_added_set=bridge_added_for_inp)
            run_abaqus_job(next_job, next_job, cpus, memory_percent)

            raw_sener_dict, evol_dict, temp_dict, _, _, _ = \
                get_sener_evol_temp_and_coords(next_job + ".odb",
                                               step_weights=custom_weights,
                                               design_part_name=global_design_part)  # FIX

            current_compliance = sum(raw_sener_dict[eid] * evol_dict.get(eid, 0.25)
                                     for eid in raw_sener_dict)
            iteration_history.append(iteration)
            compliance_history.append(current_compliance)

            print("\n   --- ITERATION {} METRICS ---".format(iteration))
            # [v47] Mass fraction is now the achieved WHOLE-PART volume fraction
            # (frozen + kept design, over the total design-space volume), so the
            # specific stiffness is normalised by the true relative part mass. The
            # same value is stored in volume_history for the CSV columns.
            _solid_volume = 0.0
            for _e in solid_list:
                _solid_volume += elem_volumes.get(_e, 0.0)
            current_mass_fraction = _solid_volume / V_TOTAL if V_TOTAL > 0.0 else 0.0
            volume_history.append(current_mass_fraction)
            print("   Achieved whole-part volume fraction: {:.2f}% ({:.4e} mm^3)".format(
                  current_mass_fraction * 100.0, _solid_volume))
            current_specific_stiffness = evaluate_specific_stiffness(
                next_job + ".odb", TOTAL_APPLIED_FORCE, current_mass_fraction,
                step_weights=custom_weights)
            stiffness_history.append(current_specific_stiffness)
            violation_history.append(current_violation_pct)

            # [v43] Track the best specific stiffness ALWAYS -- the
            # per-direction peak report and the global-best summary use it,
            # independent of whether the geometry is being dumped.
            new_best_stiffness = current_specific_stiffness > local_best_stiffness
            if new_best_stiffness:
                local_best_stiffness = current_specific_stiffness
                local_best_iteration = iteration

            # [v43] Track the most-mitigated pass (fewest overhang
            # violators). Only while mitigation is acting (thermal_active),
            # so the build-phase placeholder 0.0% can never win.
            new_best_mitigation = False
            if thermal_active and current_violation_pct < local_min_violation_pct:
                local_min_violation_pct  = current_violation_pct
                local_best_mit_iteration = iteration
                new_best_mitigation = True

            # [v43] Geometry save. Plain BESO (thermal off) keeps the
            # original single best-stiffness dump into DIR_DATA, byte-
            # identical to v42. With mitigation on, save up to three
            # geometries, each into its own subfolder (fixed CSV names).
            if not use_thermal:
                # [v45] Plain BESO honours the geometry selection. Best
                # stiffness stays in the Data_Files root (unchanged); the
                # final geometry is saved into geom_final/ at the build-
                # complete break below.
                if ("best_stiffness" in geometries_to_save) and new_best_stiffness:
                    dump_best_geometry(
                        node_coords   = node_coords,
                        connectivity  = global_connectivity,
                        solid_list    = solid_list,
                        output_dir    = DIR_DATA,
                        iteration     = iteration,
                        print_dir     = print_dir)
            else:
                if ("best_stiffness" in geometries_to_save) and new_best_stiffness:
                    print("   [v43] Saving best-stiffness geometry "
                          "(stiffness {:.2f}).".format(local_best_stiffness))
                    dump_best_geometry(
                        node_coords   = node_coords,
                        connectivity  = global_connectivity,
                        solid_list    = solid_list,
                        output_dir    = DIR_GEOM_STIFF,
                        iteration     = iteration,
                        print_dir     = print_dir)
                if ("most_mitigation" in geometries_to_save) and new_best_mitigation:
                    print("   [v43] Saving most-mitigation geometry "
                          "({:.2f}% violators).".format(local_min_violation_pct))
                    dump_best_geometry(
                        node_coords   = node_coords,
                        connectivity  = global_connectivity,
                        solid_list    = solid_list,
                        output_dir    = DIR_GEOM_MIT,
                        iteration     = iteration,
                        print_dir     = print_dir)

            cleanup_files(next_job, keep_inp=False)
            current_job = next_job
            iteration  += 1

            # v33: phase transition / termination.
            if run_phase == "build":
                if current_void_ratio >= max_void_ratio:
                    if use_thermal:
                        run_phase = "refine"
                        print("\n" + "#"*52)
                        print("   BUILD COMPLETE at VF {:.1f}% -- starting {} "
                              "refine passes".format(current_void_ratio * 100,
                                                     n_refine_passes))
                        print("#"*52)
                    else:
                        # [v45] Plain BESO: optionally save the FINAL geometry
                        # (the build iteration that reached target VF) into
                        # geom_final/. iteration was incremented above, so the
                        # last solved build pass is iteration - 1.
                        if "final" in geometries_to_save:
                            print("   [v45] Saving final-iteration geometry "
                                  "(iteration {}).".format(iteration - 1))
                            dump_best_geometry(
                                node_coords   = node_coords,
                                connectivity  = global_connectivity,
                                solid_list    = solid_list,
                                output_dir    = DIR_GEOM_FINAL,
                                iteration     = iteration - 1,
                                print_dir     = print_dir)
                        break   # thermal off: behave like traditional BESO, stop here
            else:
                if refine_pass >= n_refine_passes:
                    print("\n   REFINE COMPLETE ({} passes)".format(n_refine_passes))
                    # [v43] Save the FINAL geometry unconditionally (the last
                    # solved refine pass), if selected. iteration was already
                    # incremented past it, so label it iteration - 1.
                    if use_thermal and ("final" in geometries_to_save):
                        print("   [v43] Saving final-iteration geometry "
                              "(iteration {}).".format(iteration - 1))
                        dump_best_geometry(
                            node_coords   = node_coords,
                            connectivity  = global_connectivity,
                            solid_list    = solid_list,
                            output_dir    = DIR_GEOM_FINAL,
                            iteration     = iteration - 1,
                            print_dir     = print_dir)
                    break

            # [v48] Persist an exact-resume checkpoint for this direction, written
            # here (after the phase transition, before the next pass) so run_phase
            # and refine_pass already reflect the upcoming iteration. Overwritten
            # in place each pass; on the final iteration the loop breaks above so
            # no stale checkpoint is left behind.
            # [v49] The current stage's end iteration is stamped each pass, and the
            # full stages ledger (base + any continuations) is carried along.
            if stage_ledger:
                stage_ledger[-1]["iter_end"] = iteration - 1
            _ckpt_scalars = {
                "version": "v49",
                "print_dir": print_dir,
                "iteration_completed": iteration - 1,
                "current_void_ratio": current_void_ratio,
                "run_phase": run_phase,
                "refine_pass": refine_pass,
                "max_void_ratio": max_void_ratio,
                "n_elements_total": len(elem_volumes),
                "base_model": {"path": _base_info["source"],
                               "job_name": base_job,
                               "md5": _base_info["md5"],
                               "size_bytes": _base_info["size"]},
                "stages": stage_ledger,
                "iteration_history": iteration_history,
                "compliance_history": compliance_history,
                "volume_history": volume_history,
                "stiffness_history": stiffness_history,
                "violation_history": violation_history,
                "local_best_stiffness": local_best_stiffness,
                "local_best_iteration": local_best_iteration,
                "local_min_violation_pct": (None if local_min_violation_pct == float("inf")
                                            else local_min_violation_pct),
                "local_best_mit_iteration": local_best_mit_iteration}
            _ckpt_heavy = {
                "previous_stabilized_sens": previous_stabilized_sens,
                "raw_sener_dict": raw_sener_dict,
                "solid_list": solid_list,
                "void_list": void_list}
            save_checkpoint(DIR_DATA, print_dir, _ckpt_scalars, _ckpt_heavy)

        # --- PER-DIRECTION OUTPUTS ---
        print("\n-> Generating Data for Print Direction: {}".format(print_dir))

        plt.figure(figsize=(10, 6))
        plt.plot(iteration_history, compliance_history, marker='o',
                 linestyle='-', color='b', linewidth=2)
        plt.title('Compliance History (Direction {})'.format(print_dir))
        plt.xlabel('Iteration Number')
        plt.ylabel('Total Strain Energy')
        plt.yscale('log')
        plt.grid(True, which="both", ls="--")
        graph_path = os.path.join(DIR_DATA, 'Compliance_History_{}.png'.format(print_dir))
        plt.savefig(graph_path, dpi=300)
        plt.close()

        csv_filename = os.path.join(DIR_DATA, 'BESO_History_{}.csv'.format(print_dir))
        with _csv_open(csv_filename) as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['Iteration', 'Compliance', 'Volume_Fraction',
                             'Void_Ratio', 'Mass_Fraction',
                             'Specific_Stiffness', 'Overhang_Violations'])
            for i in range(len(iteration_history)):
                void_ratio = 1.0 - volume_history[i]
                writer.writerow([
                    iteration_history[i],
                    compliance_history[i],
                    volume_history[i],
                    void_ratio,
                    volume_history[i],
                    stiffness_history[i],
                    violation_history[i]])

        print("\n-> Peak for {}: {:.2f} (Iteration {})".format(
              print_dir, local_best_stiffness, local_best_iteration))

        if local_best_stiffness > global_best_stiffness:
            global_best_stiffness  = local_best_stiffness
            global_best_direction  = print_dir
            global_best_iteration  = local_best_iteration

    # --- FINAL SUMMARY ---
    total_time = round((time.time() - start_time) / 60, 2)
    print("\n" + "="*50)
    print("   HEURISTIC SWEEP COMPLETE!")
    print("   OPTIMAL 2D PRINTING ORIENTATION: {}".format(global_best_direction))
    print("   ABSOLUTE HIGHEST SPECIFIC STIFFNESS: {:.2f}".format(global_best_stiffness))
    print("   (Found at Iteration {} of the {} run)".format(
          global_best_iteration, global_best_direction))
    print("   Total execution time: {} minutes".format(total_time))
    print("="*50)
    print("[Launcher] Run completed.")
    _tee.close()
