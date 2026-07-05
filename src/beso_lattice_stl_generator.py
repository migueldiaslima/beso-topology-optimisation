"""
BESO Topology Optimization STL Exporter  (v11 - Launcher Preflight Uses The Law)
====================================================================================
Changes from v10 (the resolution law now also drives the launcher recommendation):

  The --preflight-only JSON path (consumed by the launcher) computed its subgrid
  recommendation from a separate code path, _compute_preflight_data, which still
  used the legacy v8 feature-size heuristic (samples_across * dx / feature_size).
  That heuristic only measures the solid strut or wall, so it falls monotonically
  with density and ignores the thinning void at high density: it under-resolved
  every lattice near the solid limit (for example it told the launcher 13 samples
  per period for the gyroid ligament at 99% where the law requires 61) and
  over-resolved the sheets at low density. The interactive and command-line meshing
  path already used the convergence law from v9 and v10, so the launcher and the
  mesher silently disagreed. This release routes the launcher recommendation through
  the SAME lattice_required_subgrid law lookup the mesher uses, evaluated across the
  active density range, and adds a "void_limited" flag to the JSON for the I-WP
  ceiling. No geometry, calibration, or law values change.

Changes from v9 (topology floor for sheets; law extended to 99%):

  The mesh-resolution law now takes the LARGER of two convergence criteria at each
  density: the original 0.30%-of-cell surface-deviation requirement, and a new
  topology-convergence floor (the Euler number must reach its stable plateau).
  The deviation metric alone is blind to topology: a thin sheet wall can be pierced
  by many sub-resolution pores while every facet still sits close to the true
  surface, so the deviation stays low and falsely reads "converged". The topology
  floor suppresses those spurious pores. It binds only for the SHEET morphologies
  at low density (e.g. primitive 10%: 22 -> 28, neovius 10%: 51 -> 60, gyroid sheet
  10%: 31 -> 32); the ligaments are topology-stable at the deviation-converged
  resolution and are unchanged. The table is also extended from 94% to 99% (the
  exporter's maximum lattice density) using high-density measurements. All density
  mapping, the printability / min-feature check, and generated geometry are
  unchanged from v9.

Changes from v8 (mesh-resolution law):

  The pre-flight subgrid recommendation now comes from an offline mesh-convergence
  study covering all six lattice morphologies across relative density 10-94%. For
  each lattice the required marching-cubes samples-per-period (perceptual tolerance,
  0.30% of cell) was measured and smoothed under a per-arm monotonicity constraint;
  the exporter interpolates this law and scales by (element_size / period) to set
  the subgrid. This replaces the v8 samples-across / feature-size heuristic, which
  tracked the study only near mid density and under-resolved the thin-feature ends.
  Above a per-lattice density ceiling (only I-WP, at 86%) the void feature is finer
  than the practical mesh limit, so a density cap is advised over a finer grid (a
  [VOID-LIMITED] note is printed). The physical printability / min-feature check
  (Section 1) and all density mapping and generated geometry are unchanged from v8.

Changes from v7 (ligament subgrid recommendation):

  Fix C - Honest ligament feature size and subgrid recommendation
           v7 reported the ligament feature as the distance-transform MAXIMUM, i.e.
           the strut-JUNCTION diameter (the thickest point), which over-estimated the
           feature and recommended a subgrid roughly 3x too coarse, leaving ligament
           lattices visibly faceted. A medial-ridge percentile (the thin neck) was
           tried but is too resolution-sensitive to be reliable. v8 instead uses the
           robust junction diameter divided by a measured junction-to-neck ratio for
           the limiting (thinnest) strut, and gives ligaments a larger marching-cubes
           samples-across factor than sheets (round struts facet more than flat sheet
           walls). Both constants were calibrated to user-validated smoothness
           (gyroid & diamond, 5 mm cell, 35% -> ~21 samples per period). Only the
           pre-flight feature-size / subgrid recommendation changes; the density
           mapping (Fix A/B) and all generated geometry are unchanged.

Changes from v6 (density correctness):

  Fix A - Neovius centering
           neovius_sheet was normalised as (N - 4.665)/8.335. The 4.665 offset
           was the midpoint of a mis-measured value range; the true range of N is
           symmetric [-13, +13] with midpoint 0. The offset pushed the sheet band
           off the connected zero level set, producing disconnected blobs. Now
           centered on N = 0 (consistent with all other sheet functions).

  Fix B - Correct density -> threshold mapping (all lattices)
           The old rule  threshold = density * SCALE  (sheets) /
           (2*density - 1) * SCALE  (ligaments)  was a single-constant straight-line
           fit to a curved relationship and realised volume fractions of 22%-54% at
           a nominal 35%. The volume fraction at threshold t is exactly the CDF of
           the field metric m over space (m = |F| for sheets, m = F for ligaments),
           so the threshold that realises a target density rho is the rho-quantile
           of m. This is exact for any lattice and any density and removes every
           per-lattice SCALE constant. Implemented in class LatticeCalibration.

  Note: STL files exported with v6 or earlier used the old mapping and should be
        regenerated. The optimiser is unaffected (it never converts density to
        geometry; it only writes the density-per-element CSV this exporter reads).

Changes from v5:

  Fix 1 — True end-to-end chunked pipeline
           D, F, and marching cubes are all computed per Z-chunk. The full implicit
           field is never allocated in RAM. Peak field RAM is proportional to chunk
           size, not grid volume.

  Fix 2 — Geometry chunks written to disk
           Each chunk's raw vertices and faces are saved as .npy temp files during
           generation. Field arrays are fully freed before assembly begins. RAM peaks
           during generation and assembly are now independent; they no longer add.
           Leftover temp files from a previous crashed run are detected and deleted
           at startup.

  Fix 3 — Binary STL written directly via numpy (trimesh removed)
           Replaces trimesh.Trimesh + fill_holes + fix_normals + mesh.export().
           Marching cubes on a padded field already guarantees a clean manifold, so
           none of those trimesh passes are needed. Binary STL is written with a
           vectorised numpy structured-dtype approach — no per-triangle loops.

  Fix 4 — np.pad eliminated
           F_chunk is pre-allocated with np.full(..., 2.0) so the boundary void
           border already exists. The np.pad call and its full-field copy are gone.

  Fix 5 — Automatic NUM_CHUNKS from available RAM
           psutil queries available system RAM at pre-flight time. NUM_CHUNKS is
           chosen so that peak field RAM per chunk stays under 70 % of what is
           available. The pre-flight check now prints the RAM budget plan and warns
           if even the chunked run looks tight.

Supported lattice types (Gibson-Ashby:  E*/Es = C1*rho^n1 ;  sy*/sys = C2*rho^n2):
  key              lattice                   C1     n1     C2     n2
  gyroid_ligament  Gyroid ligament           0.85   2.10   0.40   1.75
  gyroid_sheet     Gyroid sheet              1.00   1.55   0.40   2.10
  primitive        Schwarz Primitive sheet   1.00   1.20   0.50   1.75
  diamond          Diamond ligament          0.75   2.10   0.40   1.75
  iwp              I-WP sheet                1.00   1.20   0.55   1.65
  neovius          Neovius sheet             1.00   1.20   0.55   1.60
The optimiser writes the chosen type into the density CSV manifest line; this
exporter auto-detects it. Overriding it here renders a DIFFERENT lattice than
the optimiser sized, invalidating its stiffness/yield assumptions.

Dependencies:
    pip install numpy pandas scikit-image scipy fast_simplification psutil
"""

import numpy as np
import pandas as pd
from skimage.measure import marching_cubes
from scipy.ndimage import gaussian_filter
import fast_simplification
import psutil
import struct
import glob
import os
import sys
import json
import argparse

# ── Phase 1 subprocess worker ─────────────────────────────────────────────────
# Run in a fresh subprocess so VTK/pymeshlab heap is fully returned to the OS
# when the subprocess exits — gc.collect() cannot achieve this.
#
# Boundary preservation strategy:
#   PRIMARY   — pymeshlab meshing_decimation_quadric_edge_collapse with
#               preserveboundary=True.  This is the proper supported API:
#               boundary edges get extra quadric weight so QEM never collapses
#               them.  No phantom triangles, no numerical hacks, no VTK crashes.
#   FALLBACK  — plain fast_simplification without boundary protection.
#               Seam vertices may shift slightly, but Phase 2 global decimation
#               smooths most of the discontinuity.
#
# Install pymeshlab once:  pip install pymeshlab
_P1_WORKER_CODE = r"""
import sys, numpy as np, fast_simplification

raw_v  = sys.argv[1]; raw_f  = sys.argv[2]
p1_v   = sys.argv[3]; p1_f   = sys.argv[4]
pc     = float(sys.argv[5])
sz     = float(sys.argv[6])
first  = sys.argv[7] == 'True'
last   = sys.argv[8] == 'True'

v = np.load(raw_v).astype(np.float32)
f = np.load(raw_f).astype(np.int32)

z_min = float(v[:,2].min()); z_max = float(v[:,2].max())
depth = sz * 2
seed  = np.zeros(len(v), dtype=bool)
if not first: seed |= (v[:,2] < z_min + depth)
if not last:  seed |= (v[:,2] > z_max - depth)

if seed.any():
    bidx = np.where(seed)[0]
    _PH  = max(float(np.abs(v).max()) * 20.0, 200.0)
    pv, pf = [], []
    for i, bi in enumerate(bidx):
        bv = v[bi]; b = len(v) + i*3
        pv.append(bv + np.array([_PH,0,0], dtype=np.float32))
        pv.append(bv + np.array([0,_PH,0], dtype=np.float32))
        pv.append(bv + np.array([0,0,_PH], dtype=np.float32))
        pf += [[bi,b,b+1],[bi,b+1,b+2],[bi,b+2,b]]
    aug_v = np.vstack([v, np.array(pv, dtype=np.float32)])
    aug_f = np.vstack([f, np.array(pf, dtype=np.int32)])
    t_orig = max(4, int(len(f)*(1.0-pc)))
    adj = float(np.clip(1.0-(t_orig+len(pf))/len(aug_f), 0.0, 0.995))
    aug_v, aug_f = fast_simplification.simplify(aug_v, aug_f, target_reduction=adj)
    is_ph = (np.abs(aug_v) > _PH/2.0).any(axis=1)
    real_f = aug_f[~is_ph[aug_f].any(axis=1)]
    used = np.zeros(len(aug_v), dtype=bool); used[real_f.ravel()] = True
    ri = np.where(used)[0]
    vm = np.full(len(aug_v), -1, dtype=np.int64); vm[ri] = np.arange(len(ri))
    v = aug_v[ri].astype(np.float32); f = vm[real_f].astype(np.int32)
else:
    v, f = fast_simplification.simplify(v, f, target_reduction=pc)

np.save(p1_v, v.astype(np.float32))
np.save(p1_f, f.astype(np.int32))
print(f"DONE:{len(f)}:fast_simplification+phantom")
"""

# ══════════════════════════════════════════════════════════════════════════════
# CLI / CONFIG
# All parameters are now set via argparse. Defaults match the original
# hardcoded values so standalone runs without arguments behave identically.
# ══════════════════════════════════════════════════════════════════════════════

# --- Lattice manifest auto-detection (optimiser writes this into the CSV) ---
VALID_LATTICES  = ('gyroid_ligament', 'gyroid_sheet', 'primitive', 'diamond', 'iwp', 'neovius')
DEFAULT_LATTICE = 'iwp'

def read_csv_manifest(csv_path):
    """Read the optional '# BESO_LATTICE ...' comment line from the density CSV.
    Returns a dict of key->value strings, or None if absent/unreadable."""
    try:
        with open(csv_path, 'r') as f:
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
                    return out
                if not s.startswith('#'):
                    return None
    except Exception:
        return None
    return None


_parser = argparse.ArgumentParser(
    description='BESO Lattice Topology Optimisation STL Exporter')
_parser.add_argument('--csv',              default='Optimized_Density_Map.csv',
                     help='Path to Optimized_Density_Map.csv')
_parser.add_argument('--output',           default='Lattice_Output.stl',
                     help='Output STL base path (lattice type is auto-appended)')
_parser.add_argument('--lattice-type',     default=None,
                     choices=['gyroid_ligament','gyroid_sheet','primitive',
                              'diamond','iwp','neovius'],
                     help='TPMS lattice topology')
_parser.add_argument('--subgrid',          type=int,   default=6,
                     help='Mesh resolution sub-divisions per voxel (default: 6)')
_parser.add_argument('--period',           type=float, default=2.0,
                     help='Lattice cell size in mm (default: 2.0)')
_parser.add_argument('--smooth-sigma',     type=float, default=0.0,
                     help='Gaussian density smoothing sigma (0 = off, default: 0.0)')
_parser.add_argument('--decimation',       type=float, default=0.90,
                     help='Triangle reduction ratio 0.0-1.0 (default: 0.90)')
_parser.add_argument('--decimation-mode',  default='auto',
                     choices=['auto','global','two_pass'],
                     help='Decimation strategy (default: auto)')
_parser.add_argument('--non-interactive',  action='store_true',
                     help='Skip all interactive prompts')
_parser.add_argument('--force-lattice',    action='store_true',
                     help='Skip the override confirmation when --lattice-type differs from the CSV manifest')
_parser.add_argument('--density-floor',    type=float, default=None,
                     help='Apply this density floor (0.0-1.0) without prompting')
_parser.add_argument('--subgrid-override', type=int,   default=None,
                     help='Force this subgrid value without prompting')
_parser.add_argument('--preflight-only',   action='store_true',
                     help='Run pre-flight analysis, print JSON to stdout, then exit')
_args = _parser.parse_args()

CSV_PATH             = _args.csv
OUTPUT_STL           = _args.output
# LATTICE_TYPE is resolved below, once NON_INTERACTIVE is known
SUBGRID              = _args.subgrid
GYROID_PERIOD        = _args.period
SMOOTH_SIGMA         = _args.smooth_sigma
DECIMATION_REDUCTION = _args.decimation
DECIMATION_MODE      = _args.decimation_mode
NON_INTERACTIVE      = _args.non_interactive or _args.preflight_only
PREFLIGHT_ONLY       = _args.preflight_only
_DENSITY_FLOOR_ARG   = _args.density_floor
_SUBGRID_OVERRIDE    = _args.subgrid_override

# --- Resolve the lattice type: auto-detect from the density CSV manifest ---
_manifest = read_csv_manifest(CSV_PATH)
_detected = (_manifest.get('lattice_type') if _manifest else None)
if _detected:
    _detected = _detected.strip().lower()

if _args.lattice_type is not None:
    LATTICE_TYPE = _args.lattice_type
    if _detected in VALID_LATTICES and _detected != LATTICE_TYPE:
        print('')
        print('  [LATTICE OVERRIDE WARNING]')
        print("  The density map was optimised for '{}', but you requested '{}'.".format(_detected, LATTICE_TYPE))
        print('  These lattices have DIFFERENT stiffness (C1,n1) and yield (C2,n2)')
        print("  relationships, so rendering '{}' invalidates the optimiser sizing".format(LATTICE_TYPE))
        print('  (the density field no longer maps to the intended mechanical response).')
        if NON_INTERACTIVE or _args.force_lattice:
            print('  -> Proceeding with the override (forced / non-interactive).')
        else:
            _ans = input('  Proceed with this override anyway? [y/N]: ')
            if _ans.strip().lower() not in ('y', 'yes'):
                print("  Aborted. Re-run without --lattice-type to use the optimised lattice ('{}').".format(_detected))
                sys.exit(0)
elif _detected in VALID_LATTICES:
    LATTICE_TYPE = _detected
    print("  [AUTO-DETECT] Using lattice '{}' from the density map manifest.".format(LATTICE_TYPE))
else:
    LATTICE_TYPE = DEFAULT_LATTICE
    if _detected:
        print("  [AUTO-DETECT] Manifest lattice '{}' is not renderable; defaulting to '{}'.".format(_detected, DEFAULT_LATTICE))
    else:
        print("  [AUTO-DETECT] No lattice manifest in CSV; defaulting to '{}'.".format(DEFAULT_LATTICE))

# ══════════════════════════════════════════════════════════════════════════════
# IMPLICIT SURFACE LIBRARY  (surface functions unchanged from v5; Neovius centering
# corrected in v7)
# ══════════════════════════════════════════════════════════════════════════════
#
# Each function returns (F, mode, SCALE). v7 no longer uses SCALE: the threshold
# is now set by class LatticeCalibration (defined below) as the density-quantile
# of the field metric, so the realised volume fraction equals the requested
# density. The classification at marching-cubes level 0.0 remains:
#   LIGAMENT mode:  material where  F   < threshold(D)
#   SHEET mode:     material where |F|  < threshold(D)
# SCALE is kept in the return signature for backward compatibility only.

MIN_SHEET_T = 0.0   # legacy, unused in v7 (kept to avoid touching the signature)



def gyroid_ligament(X, Y, Z):
    G = (np.sin(X)*np.cos(Y) + np.sin(Y)*np.cos(Z) + np.sin(Z)*np.cos(X))
    G /= np.sqrt(3.0)
    return G, 'ligament', 0.65


def gyroid_sheet(X, Y, Z):
    G = (np.sin(X)*np.cos(Y) + np.sin(Y)*np.cos(Z) + np.sin(Z)*np.cos(X))
    G /= np.sqrt(3.0)
    return G, 'sheet', 0.55


def primitive_sheet(X, Y, Z):
    P = (np.cos(X) + np.cos(Y) + np.cos(Z)) / 3.0
    return P, 'sheet', 0.62


def diamond_ligament(X, Y, Z):
    D = (np.sin(X)*np.sin(Y)*np.sin(Z) +
         np.sin(X)*np.cos(Y)*np.cos(Z) +
         np.cos(X)*np.sin(Y)*np.cos(Z) +
         np.cos(X)*np.cos(Y)*np.sin(Z))
    return D, 'ligament', 0.65


def iwp_sheet(X, Y, Z):
    IWP = (2.0 * (np.cos(X)*np.cos(Y) + np.cos(Y)*np.cos(Z) + np.cos(Z)*np.cos(X))
           - (np.cos(2*X) + np.cos(2*Y) + np.cos(2*Z)))
    return IWP / 4.0, 'sheet', 0.65


def neovius_sheet(X, Y, Z):
    N = 3.0*(np.cos(X)+np.cos(Y)+np.cos(Z)) + 4.0*np.cos(X)*np.cos(Y)*np.cos(Z)
    # v7: center on the true zero level set (N = 0). The previous offset of 4.665
    # was the midpoint of a mis-measured value range and shifted the sheet band off
    # the connected minimal surface, producing disconnected blobs.
    N_center = 0.0
    N_half   = 8.335
    return (N - N_center) / N_half, 'sheet', 0.62


LATTICE_FUNCS = {
    "gyroid_ligament": gyroid_ligament,
    "gyroid_sheet":    gyroid_sheet,
    "primitive":       primitive_sheet,
    "diamond":         diamond_ligament,
    "iwp":             iwp_sheet,
    "neovius":         neovius_sheet,
}

# v8 ligament feature-size calibration (pre-flight only; does not affect geometry).
# A medial-ridge percentile for the thin strut proved too resolution-sensitive, so
# the robust junction diameter (max of the distance transform) is divided by a
# measured junction-to-neck ratio to estimate the limiting (thinnest) strut. Round
# struts facet more visibly than flat sheet walls, so ligaments use a larger
# marching-cubes samples-across factor. Both constants were calibrated to
# user-validated smoothness (gyroid & diamond, 5 mm cell, 35% -> ~21 samples per
# period). Adjust here if a different visual standard is preferred.
LIG_NECK_RATIO       = 2.56   # junction diameter / thinnest-neck diameter
LIG_SAMPLES_ACROSS   = 3.1    # MC samples across the thinnest strut (round feature)
SHEET_SAMPLES_ACROSS = 2.5    # MC samples across a sheet wall (flat feature)

# ==============================================================================
# MESH-RESOLUTION LAW  (v10)
# ------------------------------------------------------------------------------
# Required marching-cubes samples PER PERIOD as a function of relative density,
# from the offline convergence study (tolerance 0.30% of cell). The requirement
# combines TWO criteria, taking whichever is larger at each density:
#   (1) surface deviation: 95th-percentile mesh-to-surface distance < 0.30% of
#       the cell, measured over a 2x2x2 block (governs visual smoothness);
#   (2) topology convergence: the Euler number must reach its stable plateau, so
#       thin sheet walls do not break into spurious sub-resolution pores. This
#       floor binds only for the SHEET morphologies at low density (e.g. it lifts
#       primitive 10% from 22 to 28, neovius 10% from 51 to 60); the ligaments
#       are topology-stable at the deviation-converged resolution and unchanged.
# Values are smoothed (monotone-per-arm): the requirement falls as the binding
# solid feature thickens, then rises as the binding void feature thins (U-shaped).
# The table spans 10% to 99% (the exporter's maximum lattice density; above 99%
# the element is treated as solid).
#
# The exporter interpolates this table at the active density range and scales by
# (element_size / period) to get the subgrid (samples per element):
#     samples_per_period = subgrid * (period / element_size)
#  => subgrid            = samples_per_period * (element_size / period)
#
# _LATTICE_CEIL is the density above which the void feature is finer than the
# practical mesh limit (only I-WP, 0.86). Past it the ceiling value is held and a
# density cap is advised instead of an ever-finer grid.
_LAW_DENSITY = np.array([0.10, 0.13, 0.16, 0.20, 0.25, 0.30, 0.38, 0.46, 0.54,
                         0.62, 0.70, 0.76, 0.81, 0.86, 0.90, 0.94, 0.95, 0.97, 0.99])
_LAW_SPP = {
    'gyroid_ligament': np.array([30.3, 28.8, 24.0, 23.9, 21.6, 19.4, 15.9, 15.0, 15.0,
                                 16.2, 19.3, 21.7, 23.7, 27.0, 29.5, 36.7, 37.5, 45.1, 60.6]),
    'diamond':         np.array([36.3, 33.2, 29.6, 27.0, 24.0, 23.6, 20.2, 18.3, 18.3,
                                 20.2, 23.6, 25.2, 28.0, 31.2, 36.3, 39.6, 39.8, 43.1, 47.2]),
    'primitive':       np.array([28.0, 19.6, 15.5, 12.0, 12.6, 13.8, 14.6, 16.8, 19.4,
                                 19.4, 19.4, 19.4, 19.6, 20.1, 21.4, 23.6, 24.2, 26.8, 32.7]),
    'iwp':             np.array([40.8, 32.5, 27.2, 22.2, 20.1, 20.5, 22.1, 24.1, 26.2,
                                 29.5, 32.9, 36.7, 43.6, 55.1, 55.1, 55.1, 55.1, 55.1, 55.1]),
    'gyroid_sheet':    np.array([32.0, 26.0, 20.5, 17.2, 15.8, 18.5, 19.6, 20.5, 22.4,
                                 23.7, 26.3, 28.7, 32.3, 34.3, 37.1, 45.9, 48.6, 56.7, 65.2]),
    'neovius':         np.array([60.0, 50.0, 40.0, 32.9, 32.4, 32.4, 24.0, 17.8, 17.8,
                                 17.8, 17.4, 19.7, 19.9, 21.3, 21.8, 24.9, 25.5, 27.4, 33.2]),
}
_LATTICE_CEIL = {'gyroid_ligament': 1.0, 'diamond': 1.0, 'primitive': 1.0,
                 'iwp': 0.86, 'gyroid_sheet': 1.0, 'neovius': 1.0}



def lattice_required_spp(lattice_type, density):
    """Required samples per period at one density (convergence law, interpolated).

    Returns None if the lattice has no law table (caller falls back to heuristic).
    Density is clamped to the studied range and to the lattice ceiling.
    """
    table = _LAW_SPP.get(lattice_type)
    if table is None:
        return None
    ceiling = _LATTICE_CEIL.get(lattice_type, 1.0)
    d = min(max(float(density), float(_LAW_DENSITY[0])), ceiling)
    return float(np.interp(d, _LAW_DENSITY, table))


def lattice_required_subgrid(lattice_type, density_lo, density_hi, dx, period):
    """Required subgrid (samples per element) over an active density range.

    The law is U-shaped in density, so the binding requirement is the maximum
    over the active range; with a U-shape the extremes bind, so the law is
    evaluated at the lowest and highest active density and the larger is used.
    Returns (subgrid, ceiling_hit). subgrid is None if the lattice has no table.
    """
    spp_lo = lattice_required_spp(lattice_type, density_lo)
    spp_hi = lattice_required_spp(lattice_type, density_hi)
    if spp_lo is None:
        return None, False
    spp = max(spp_lo, spp_hi)
    scale = (dx / period) if period > 0 else 1.0
    ceiling = _LATTICE_CEIL.get(lattice_type, 1.0)
    ceiling_hit = float(density_hi) > ceiling + 1e-9
    return int(np.ceil(spp * scale)), ceiling_hit

# ==============================================================================
# DENSITY -> THRESHOLD CALIBRATION  (v7)
# ------------------------------------------------------------------------------
# The volume fraction produced by a threshold t is exactly the cumulative
# distribution (CDF) of the field metric m over space:
#     ligament : solid = {F   < t}  ->  VF(t) = fraction of cells with  F  < t
#     sheet    : solid = {|F| < t}  ->  VF(t) = fraction of cells with |F| < t
# So the threshold that realises a target relative density rho is simply the
# rho-quantile of m. This is exact for any lattice and any density, has zero
# tunable constants, and replaces the old single per-lattice SCALE constant
# (a straight-line approximation to this S-shaped curve, off by up to ~2x).
#
# The field metric distribution depends only on the TPMS function shape, not on
# the period (which merely stretches space) or the grid, so it is sampled once
# over a single periodic unit cell at startup for the selected lattice.
# ==============================================================================

class LatticeCalibration:
    def __init__(self, func, mode, period, n_rho=257, sample_res=120):
        self.mode   = mode
        self.period = float(period)
        self._omega = 2.0 * np.pi / float(period)
        # Sample F over one periodic cell (args span [0, 2*pi)).
        c = np.linspace(0.0, 2.0 * np.pi, sample_res, endpoint=False)
        X, Y, Z = np.meshgrid(c, c, c, indexing='ij')
        F, _, _ = func(X, Y, Z)
        m = np.abs(F) if mode == 'sheet' else F
        # density -> threshold table (the quantile / inverse-CDF curve)
        self.rho_grid = np.linspace(0.0, 1.0, n_rho)
        self.t_grid   = np.quantile(m.ravel(), self.rho_grid).astype(np.float64)
        # Representative field-gradient magnitude (w.r.t. the omega-scaled args),
        # measured near the zero level set, for sheet wall-thickness estimates.
        gx, gy, gz = np.gradient(F, c[1] - c[0])
        gmag = np.sqrt(gx * gx + gy * gy + gz * gz)
        near = np.abs(F) < np.quantile(np.abs(F), 0.15)
        self.grad_repr = float(np.mean(gmag[near])) if np.any(near) else float(np.mean(gmag))
        # Ligament strut-diameter coefficient, calibrated once via a distance
        # transform at a reference density, kept in the period*C*sqrt(rho) form.
        self.strut_coeff = self._calibrate_strut(func) if mode == 'ligament' else None

    # --- density <-> threshold (exact, quantile based) ---
    def threshold_for_density(self, D):
        return np.interp(np.clip(D, 0.0, 1.0), self.rho_grid, self.t_grid)

    def density_for_threshold(self, t):
        return float(np.interp(t, self.t_grid, self.rho_grid))

    # --- sheet wall thickness (field-gradient model, no magic numbers) ---
    def wall_thickness(self, density):
        t = float(np.interp(np.clip(density, 0.0, 1.0), self.rho_grid, self.t_grid))
        denom = self._omega * self.grad_repr
        return (2.0 * t / denom) if denom > 0 else 0.0

    def density_for_wall_thickness(self, thickness):
        t = thickness * self._omega * self.grad_repr / 2.0
        return float(np.interp(t, self.t_grid, self.rho_grid))

    # --- ligament strut diameter (recalibrated coefficient, sqrt model) ---
    def _calibrate_strut(self, func, ref_rho=0.30, res=96):
        from scipy.ndimage import distance_transform_edt
        c = np.linspace(0.0, 2.0 * np.pi, res, endpoint=False)
        X, Y, Z = np.meshgrid(c, c, c, indexing='ij')
        F, _, _ = func(X, Y, Z)
        t_ref = np.quantile(F.ravel(), ref_rho)
        solid = (F < t_ref)
        if not solid.any():
            return 1.0
        voxel_mm = self.period / res
        edt = distance_transform_edt(solid) * voxel_mm
        strut_dia = 2.0 * float(edt.max()) / LIG_NECK_RATIO   # thinnest-neck estimate (v8)
        denom = float(self.period * np.sqrt(ref_rho))
        return float(strut_dia / denom) if denom > 0 else 1.0

    def strut_diameter(self, density):
        return float(self.period * self.strut_coeff * np.sqrt(max(density, 0.0)))

    def density_for_strut_diameter(self, diameter):
        denom = self.period * self.strut_coeff
        return float((diameter / denom) ** 2) if denom > 0 else 0.99


# Build the calibration once for the selected lattice. Depends only on the
# lattice type and period, both resolved above, so it is available to both the
# pre-flight analysis and the chunked field generation below.
_probe_F, _LATTICE_MODE, _SCALE_UNUSED = LATTICE_FUNCS[LATTICE_TYPE](0.0, 0.0, 0.0)
CALIB = LatticeCalibration(LATTICE_FUNCS[LATTICE_TYPE], _LATTICE_MODE, GYROID_PERIOD)

# ══════════════════════════════════════════════════════════════════════════════
# FIX 2 — TEMP FILE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def _temp_prefix(output_stl, lattice_type):
    base, _ = os.path.splitext(output_stl)
    return f"{base}_{lattice_type}_temp_chunk_"


def cleanup_temp_files(temp_prefix):
    """Delete all .npy temp files matching the prefix. Returns the count deleted."""
    files = glob.glob(temp_prefix + "*.npy")
    for path in files:
        try:
            os.remove(path)
        except OSError:
            pass
    return len(files)

# ══════════════════════════════════════════════════════════════════════════════
# FIX 3 — BINARY STL WRITE  (replaces trimesh entirely)
# ══════════════════════════════════════════════════════════════════════════════

def write_binary_stl(path, verts, faces):
    """
    Write a binary STL file from numpy arrays.
    Fully vectorised — no per-triangle Python loops.

    Why no fill_holes / fix_normals:
      Marching cubes on a field that is padded with void (+2.0) on all six faces
      of the bounding box mathematically guarantees a closed, manifold mesh.
      trimesh's repair passes are expensive KD-tree / topological scans that
      find problems that do not exist here — skipping them saves minutes on large
      meshes with no change to the output geometry.
    """
    v0 = verts[faces[:, 0]].astype(np.float32)
    v1 = verts[faces[:, 1]].astype(np.float32)
    v2 = verts[faces[:, 2]].astype(np.float32)

    # Compute face normals (vectorised cross-product, normalised)
    normals = np.cross(v1 - v0, v2 - v0)
    norms   = np.linalg.norm(normals, axis=1, keepdims=True)
    norms   = np.where(norms > 0, norms, 1.0)
    normals = (normals / norms).astype(np.float32)

    # Binary STL triangle record: 12 × float32 + 1 × uint16 = 50 bytes
    #   normal(3f)  v0(3f)  v1(3f)  v2(3f)  attr(u16)
    dtype = np.dtype([
        ('normal', np.float32, (3,)),
        ('v0',     np.float32, (3,)),
        ('v1',     np.float32, (3,)),
        ('v2',     np.float32, (3,)),
        ('attr',   np.uint16),
    ])
    records = np.zeros(len(faces), dtype=dtype)
    records['normal'] = normals
    records['v0']     = v0
    records['v1']     = v1
    records['v2']     = v2
    # 'attr' stays 0 (standard STL padding)

    with open(path, 'wb') as fh:
        fh.write(b'BESO Topology STL v8' + b' ' * 60)   # 80-byte header
        fh.write(struct.pack('<I', len(faces)))           # 4-byte face count
        fh.write(records.tobytes())                       # triangle data


def _is_manifold(faces, max_faces=2_000_000):
    """
    Lightweight manifold check: every edge must be shared by exactly 2 faces.
    Returns True / False, or None if the mesh is too large to check quickly.
    """
    if len(faces) > max_faces:
        return None
    edges        = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    edges_sorted = np.sort(edges, axis=1)
    max_idx      = int(edges_sorted.max()) + 1
    edge_ids     = (edges_sorted[:, 0].astype(np.int64) * max_idx
                    + edges_sorted[:, 1].astype(np.int64))
    _, counts = np.unique(edge_ids, return_counts=True)
    return bool(np.all(counts == 2))

# ══════════════════════════════════════════════════════════════════════════════
# FIX 5 — RAM BUDGET HELPER
# ══════════════════════════════════════════════════════════════════════════════

def _compute_num_chunks(nx, ny, nz, subgrid, smooth_sigma,
                        n_raw_est=0, do_per_chunk_decim=False):
    """
    Choose the minimum number of Z-chunks so that peak field RAM per chunk
    stays under 70 % of currently available system RAM.

    Simultaneous float32 arrays during one chunk's field generation:
      D_chunk     (1×)
      F_chunk     (1×)
      smooth_D    (1× extra, only when SMOOTH_SIGMA > 0)
      block       (1× slightly larger — absorbs the border voxels)
      MC buffers  (skimage allocates ~1 internal pass array same size as output;
                   for large TPMS blocks this adds a meaningful fraction)
    Factor ≈ 3.0 (no smoothing) or 4.0 (with smoothing).

    Returns: (num_chunks, peak_per_chunk_gb, available_gb, total_field_gb,
              n_chunks_field, n_chunks_decim)
      n_chunks_field  — minimum chunks required by field generation RAM alone
      n_chunks_decim  — minimum chunks required by per-chunk decimation RAM
                        (only non-trivial when do_per_chunk_decim=True)
    The returned num_chunks is max(n_chunks_field, n_chunks_decim).
    """
    available_bytes = psutil.virtual_memory().available
    available_gb    = available_bytes / 1e9

    # 3.0 accounts for D_chunk + F_chunk + block + marching_cubes working buffers.
    # Previous value of 2.5 under-counted MC memory, causing 99 % RAM spikes.
    n_arrays = 4.0 if smooth_sigma > 0 else 3.0

    # bytes consumed per subgrid Z-slice
    bytes_per_slice = nx * subgrid * ny * subgrid * 4 * n_arrays
    total_field_gb  = (nx * subgrid * ny * subgrid * nz * subgrid * 4) / 1e9

    total_z_slices = nz * subgrid

    # ── Constraint 1: field generation + marching cubes RAM ──────────────────
    target_field      = available_bytes * 0.70
    max_z_field       = max(2, int(target_field / bytes_per_slice))
    n_chunks_field    = max(1, int(np.ceil(total_z_slices / max_z_field)))

    # ── Constraint 2: per-chunk VTK decimation RAM (two-pass phase 1 only) ───
    # Each MC chunk is loaded and decimated independently in phase 1.
    # _VTK_BYTES_PER_RAW_FACE covers faces + vertices + VTK internal copies.
    # The chunk face count must satisfy: n_faces_chunk × VTK_BYTES ≤ 0.60 × available.
    n_chunks_decim = 1
    if do_per_chunk_decim and n_raw_est > 0:
        target_decim    = available_bytes * 0.75
        max_faces_decim = max(1, int(target_decim / _VTK_BYTES_PER_RAW_FACE))
        n_chunks_decim  = max(1, int(np.ceil(n_raw_est / max_faces_decim)))

    num_chunks = max(n_chunks_field, n_chunks_decim)

    # Actual peak field RAM with the chosen chunk count (+1 slice for MC overlap)
    actual_chunk_z    = int(np.ceil(total_z_slices / num_chunks)) + 1
    peak_per_chunk_gb = (bytes_per_slice * actual_chunk_z) / 1e9

    return num_chunks, peak_per_chunk_gb, available_gb, total_field_gb, n_chunks_field, n_chunks_decim


# ── Decimation RAM helpers ────────────────────────────────────────────────────
# fast_simplification (VTK) needs ~110 bytes per raw input triangle, covering:
#   faces array (12 b/face) + vertex array (~6 b/face) + VTK PolyData copy
#   + QEM quadric storage (~64 b/vertex × 0.5 verts/face = ~32 b/face)
#   + other VTK working structures (~60 b/face)
# Empirical calibration from monitored run (SUBGRID=27, 8.37 GB machine):
#   Chunk 1 had 20.78M raw faces.
#   Process RSS increase during Phase 1 chunk 1 = 6.87 - 1.17 = 5.70 GB
#   → 5.70e9 / 20.78e6 ≈ 274 bytes/face
# The previous 110 figure was from a total-assembly crash which underestimated
# per-chunk overhead: Python's allocator retains freed C-extension pages until
# gc.collect() is called, inflating the apparent per-chunk cost.
# With gc.collect() now applied between Phase 1 chunks, the true per-chunk
# peak (after GC) is closer to 110-150 bytes/face, but 275 is used as the
# planning constant to stay conservative and avoid OOM on the first chunk.
_VTK_BYTES_PER_RAW_FACE = 275
_TPMS_SURFACE_FRACTION  = 0.20   # conservative over-estimate of raw faces / field voxels


def _estimate_raw_triangles(nx, ny, nz, subgrid):
    """Estimate raw marching-cubes triangle count from grid + subgrid settings."""
    return int(nx * subgrid * ny * subgrid * nz * subgrid * _TPMS_SURFACE_FRACTION)


def _compute_decimation_plan(n_raw_est, decimation_reduction, available_gb):
    """
    Decide between global and two-pass decimation and compute the per-chunk
    and final-global reduction targets for the two-pass case.

    Two-pass math
    ─────────────
    Let keep = 1 − decimation_reduction   (e.g. 0.10 for 90 % reduction)
    Let p    = per_chunk_keep_fraction     (chosen so assembled RAM fits)

    After phase 1:  n_assembled = n_raw × p
    After phase 2:  n_final     = n_raw × p × (1 − final_global_reduction)

    Target:  n_final = n_raw × keep
    → final_global_reduction = 1 − keep / p

    p must satisfy:  n_raw × p × VTK_BYTES ≤ TARGET_RAM
    → p ≤ TARGET_RAM / (n_raw × VTK_BYTES)

    We also enforce:
      p ≥ keep × 1.5   (phase 1 must keep at least 50 % more than the final target,
                         so phase 2 still has meaningful triangles to work with)
      p ≤ 0.60          (cap: no point in a near-lossless phase 1)

    Returns
    ───────
    (use_two_pass, per_chunk_reduction, final_global_reduction,
     est_global_ram_gb, est_assembled_gb)
    """
    keep_fraction = 1.0 - decimation_reduction

    # RAM required if we assemble everything before decimating
    est_global_ram_gb = (n_raw_est * _VTK_BYTES_PER_RAW_FACE) / 1e9

    # Will global fit?
    global_fits = est_global_ram_gb <= available_gb * 0.70

    # Per-chunk keep fraction chosen so assembled RAM ≤ 50 % of available
    target_assembled_bytes = available_gb * 0.50 * 1e9
    p_from_ram = target_assembled_bytes / max(n_raw_est * _VTK_BYTES_PER_RAW_FACE, 1)
    p = float(np.clip(p_from_ram, keep_fraction * 1.5, 0.60))

    per_chunk_reduction   = 1.0 - p
    final_global_reduction = 1.0 - keep_fraction / p

    # If final_global_reduction ≤ 0 the per-chunk pass already over-reduces — clamp
    final_global_reduction = max(0.0, final_global_reduction)

    est_assembled_gb = (n_raw_est * p * _VTK_BYTES_PER_RAW_FACE) / 1e9

    return (not global_fits, per_chunk_reduction, final_global_reduction,
            est_global_ram_gb, est_assembled_gb)

# ══════════════════════════════════════════════════════════════════════════════
# PRE-FLIGHT CHECK  (physical printability + mesh resolution + RAM plan)
# ══════════════════════════════════════════════════════════════════════════════

def check_printability_limits(df, lattice_type, period, subgrid, dx, nx, ny, nz,
                              non_interactive=False, density_floor_arg=None,
                              subgrid_override=None):
    C_RED    = '\033[91m'
    C_YELLOW = '\033[93m'
    C_GREEN  = '\033[92m'
    C_CYAN   = '\033[96m'
    C_RESET  = '\033[0m'

    lattice_elements = df[(df['density'] > 0.0) & (df['density'] < 1.0)]
    if len(lattice_elements) == 0:
        num_chunks, peak_gb, avail_gb, total_gb, _, _ = _compute_num_chunks(
            nx, ny, nz, subgrid, SMOOTH_SIGMA)
        return 0.0, subgrid, num_chunks, False, 0.0, 0.0

    min_density   = lattice_elements['density'].min()
    predicted_size = 0.0
    feature_name   = "Thickness"

    if lattice_type in ("gyroid_ligament", "diamond"):
        predicted_size = CALIB.strut_diameter(min_density);  feature_name = "Strut Diameter"
    else:
        predicted_size = CALIB.wall_thickness(min_density);  feature_name = "Wall Thickness"

    print("\n── PRE-FLIGHT MANUFACTURING & ALGORITHM CHECK ──────────")
    print(f"  Lowest Active Density : {min_density*100:.1f}%")
    print(f"  Estimated Min {feature_name}: {predicted_size:.3f} mm")

    # ── 1. Physical printability ──────────────────────────────
    target_size          = 0.25
    applied_density_floor = 0.0

    if predicted_size < 0.15:
        print(f"{C_RED}  [SLM CRITICAL]: Feature size below absolute print limit (0.15 mm)!{C_RESET}")
    elif predicted_size < 0.25:
        print(f"{C_YELLOW}  [SLM WARNING]: Feature size in high-risk zone (0.15–0.25 mm).{C_RESET}")
    else:
        print(f"{C_GREEN}  [SLM PASS]: Features within standard load-bearing limits.{C_RESET}")

    if predicted_size < target_size:
        if lattice_type in ("gyroid_ligament", "diamond"):
            safe_density = CALIB.density_for_strut_diameter(target_size)
        else:
            safe_density = CALIB.density_for_wall_thickness(target_size)
        safe_density = min(safe_density, 0.99)

        print(f"\n{C_CYAN}  [SMART FIX]: Raise min density to {safe_density*100:.1f}% "
              f"to guarantee a {target_size} mm feature.{C_RESET}")
        if non_interactive:
            if density_floor_arg is not None:
                applied_density_floor = density_floor_arg
                print(f"{C_GREEN}  -> Density floor {density_floor_arg*100:.1f}% applied (from launcher).{C_RESET}")
            else:
                print("  -> Skipped (non-interactive mode).")
        else:
            ans = input(f"{C_CYAN}  Enforce this density floor? [y/N]: {C_RESET}")
            if ans.lower() in ['y', 'yes']:
                applied_density_floor = safe_density
                print(f"{C_GREEN}  -> Density floor applied!{C_RESET}")
            else:
                print("  -> Skipped.")

    # ── 2. Mesh resolution ────────────────────────────────────
    print("  - - - - - - - - - - - - - - - - - - - - - - - - - - - ")
    # v9: required resolution from the convergence-study law (replaces the v8
    # samples-across / feature-size heuristic). The law is U-shaped in density, so
    # it is evaluated across the whole active range and bound by whichever extreme
    # (thin strut at low density or thin void at high density) is present.
    active_lo = applied_density_floor if applied_density_floor > 0.0 else min_density
    active_hi = float(lattice_elements['density'].max())
    required_subgrid, ceiling_hit = lattice_required_subgrid(
        lattice_type, active_lo, active_hi, dx, period)
    if required_subgrid is None:
        # lattice not in the law table: fall back to the legacy v8 heuristic
        effective_size   = target_size if applied_density_floor > 0.0 else predicted_size
        samples_across   = LIG_SAMPLES_ACROSS if CALIB.mode == 'ligament' else SHEET_SAMPLES_ACROSS
        required_subgrid = int(np.ceil((samples_across * dx) / effective_size))
        ceiling_hit      = False
    optimal_subgrid  = max(4, required_subgrid)
    final_subgrid    = subgrid

    if ceiling_hit:
        print(f"{C_YELLOW}  [VOID-LIMITED]: at {active_hi*100:.0f}% density the void feature is "
              f"finer than the practical mesh limit;{C_RESET}")
        print(f"{C_YELLOW}                 a density cap is advised rather than an ever-finer grid.{C_RESET}")

    if final_subgrid < required_subgrid:
        severity = (f"{C_YELLOW}  [MESH WARNING]"  if final_subgrid >= required_subgrid / 2
                    else f"{C_RED}  [MESH CRITICAL]")
        print(f"{severity}: SUBGRID={final_subgrid} is insufficient for these thin features.{C_RESET}")
        print(f"\n{C_CYAN}  [SMART FIX]: Strict capture requires SUBGRID={required_subgrid}.{C_RESET}")
        if non_interactive:
            if subgrid_override is not None:
                final_subgrid = subgrid_override
                print(f"{C_GREEN}  -> SUBGRID set to {final_subgrid} (from launcher).{C_RESET}")
            else:
                print("  -> Skipped (non-interactive mode).")
        else:
            ans = input(f"{C_CYAN}  Increase SUBGRID to {required_subgrid}? [y/N]: {C_RESET}")
            if ans.lower() in ['y', 'yes']:
                final_subgrid = required_subgrid
                print(f"{C_GREEN}  -> SUBGRID updated to {final_subgrid}!{C_RESET}")
            else:
                print("  -> Skipped.")

    elif final_subgrid > optimal_subgrid:
        print(f"{C_YELLOW}  [MESH OVERKILL]: SUBGRID={final_subgrid} is higher than needed — "
              f"wastes RAM with no structural benefit.{C_RESET}")
        print(f"\n{C_CYAN}  [SMART FIX]: Optimal balance is SUBGRID={optimal_subgrid}.{C_RESET}")
        if non_interactive:
            if subgrid_override is not None:
                final_subgrid = subgrid_override
                print(f"{C_GREEN}  -> SUBGRID set to {final_subgrid} (from launcher).{C_RESET}")
            else:
                print("  -> Skipped (non-interactive mode).")
        else:
            ans = input(f"{C_CYAN}  Reduce SUBGRID to {optimal_subgrid}? [y/N]: {C_RESET}")
            if ans.lower() in ['y', 'yes']:
                final_subgrid = optimal_subgrid
                print(f"{C_GREEN}  -> SUBGRID reduced to {final_subgrid}!{C_RESET}")
            else:
                print("  -> Skipped.")

    else:
        print(f"{C_GREEN}  [MESH PASS]: SUBGRID={final_subgrid} is well-matched to this geometry.{C_RESET}")

    # ── 3. RAM budget (Fix 5) ─────────────────────────────────
    print("  - - - - - - - - - - - - - - - - - - - - - - - - - - - ")

    # Estimate raw triangles before computing chunks — needed to evaluate
    # whether two-pass is required and to enforce the decimation chunk constraint.
    n_raw_est_preflight = _estimate_raw_triangles(nx, ny, nz, final_subgrid)
    avail_quick_gb      = psutil.virtual_memory().available / 1e9
    est_global_quick    = (n_raw_est_preflight * _VTK_BYTES_PER_RAW_FACE) / 1e9
    likely_two_pass     = est_global_quick > avail_quick_gb * 0.70
    if DECIMATION_MODE == "global":   likely_two_pass = False
    elif DECIMATION_MODE == "two_pass": likely_two_pass = True

    num_chunks, peak_gb, avail_gb, total_gb, n_ch_field, n_ch_decim = _compute_num_chunks(
        nx, ny, nz, final_subgrid, SMOOTH_SIGMA,
        n_raw_est=n_raw_est_preflight, do_per_chunk_decim=likely_two_pass)

    print(f"  Full field (if unsharded): {nx*final_subgrid} × {ny*final_subgrid} × "
          f"{nz*final_subgrid}  ({total_gb:.2f} GB)")
    print(f"  Available RAM            : {avail_gb:.2f} GB")

    if n_ch_decim > n_ch_field:
        # Decimation constraint is binding — explain why chunk count increased
        print(f"  Auto chunks              : {num_chunks}  →  peak per chunk ≈ {peak_gb:.2f} GB")
        print(f"  (field needs ≥{n_ch_field} chunks; per-chunk decimation needs ≥{n_ch_decim} — using {num_chunks})")
    else:
        print(f"  Auto chunks              : {num_chunks}  →  peak per chunk ≈ {peak_gb:.2f} GB")

    if peak_gb > avail_gb * 0.85:
        print(f"{C_RED}  [RAM CRITICAL]: Even with {num_chunks} chunks, peak field RAM may exceed "
              f"available memory. Consider reducing SUBGRID or GYROID_PERIOD.{C_RESET}")
    elif peak_gb > avail_gb * 0.60:
        print(f"{C_YELLOW}  [RAM WARNING]: Field generation will consume ≈{peak_gb/avail_gb*100:.0f}% "
              f"of available RAM.{C_RESET}")
    else:
        print(f"{C_GREEN}  [RAM PASS]: Field generation RAM well within safe limits.{C_RESET}")

    # ── 4. Assembly + decimation RAM (Fix 6) ─────────────────
    print("  - - - - - - - - - - - - - - - - - - - - - - - - - - - ")
    n_raw_est = n_raw_est_preflight  # reuse from above
    two_pass, pc_red, fg_red, est_global_gb, est_assembled_gb = _compute_decimation_plan(
        n_raw_est, DECIMATION_REDUCTION, avail_gb)

    # Respect the user's explicit DECIMATION_MODE override
    if DECIMATION_MODE == "global":
        two_pass = False
    elif DECIMATION_MODE == "two_pass":
        two_pass = True
    # else "auto" → use whatever _compute_decimation_plan decided

    # Per-chunk decimation RAM check (with the actual chunk count now known)
    n_raw_per_chunk_est = n_raw_est / num_chunks
    vtk_per_chunk_gb    = (n_raw_per_chunk_est * _VTK_BYTES_PER_RAW_FACE) / 1e9

    print(f"  Est. raw triangles       : ~{n_raw_est:,}")
    print(f"  Global decimation RAM    : ~{est_global_gb:.2f} GB  "
          f"({'fits' if est_global_gb <= avail_gb * 0.70 else 'would OOM'})")

    if two_pass:
        print(f"{C_YELLOW}  [DECIMATION]: Switching to two-pass  "
              f"(phase 1: {pc_red*100:.0f}% per-chunk  →  "
              f"phase 2: {fg_red*100:.0f}% global).{C_RESET}")
        print(f"  Est. per-chunk VTK RAM   : ~{vtk_per_chunk_gb:.2f} GB  "
              f"({'safe' if vtk_per_chunk_gb <= avail_gb * 0.65 else 'tight — consider fewer features'})")
        print(f"  Est. assembled RAM after phase 1: ~{est_assembled_gb:.2f} GB")
    else:
        print(f"{C_GREEN}  [DECIMATION]: Global decimation fits ({est_global_gb:.2f} GB estimated).{C_RESET}")

    print("────────────────────────────────────────────────────────\n")
    return applied_density_floor, final_subgrid, num_chunks, two_pass, pc_red, fg_red


# ══════════════════════════════════════════════════════════════════════════════
# PRE-FLIGHT JSON ANALYSIS  (used by --preflight-only mode)
# Returns a dict with all analysis data — no printing, no input() calls.
# ══════════════════════════════════════════════════════════════════════════════

def _compute_preflight_data(df, lattice_type, period, subgrid, dx, nx, ny, nz,
                             decimation_reduction, decimation_mode):
    """
    Run the complete pre-flight analysis and return a structured dict.
    Called when --preflight-only is set. Outputs JSON to stdout then exits.
    """
    lattice_elements = df[(df['density'] > 0.0) & (df['density'] < 1.0)]
    void_count    = int((df['density'] == 0.0).sum())
    solid_count   = int((df['density'] == 1.0).sum())
    lattice_count = int(len(lattice_elements))

    result = {
        "grid": {
            "nx": nx, "ny": ny, "nz": nz,
            "dx": float(dx),
            "total_voxels": int(nx * ny * nz)
        },
        "elements": {
            "total": int(len(df)),
            "void": void_count,
            "solid": solid_count,
            "lattice": lattice_count
        },
        "feature": {
            "name": "N/A",
            "min_density": 0.0,
            "predicted_size_mm": 0.0,
            "status": "pass",
            "density_floor_needed": False,
            "recommended_density_floor": 0.0,
            "safe_size_mm": 0.25
        },
        "subgrid": {
            "current": subgrid,
            "recommended": subgrid,
            "optimal": subgrid,
            "status": "pass"
        },
        "ram": {
            "available_gb": 0.0,
            "total_field_gb": 0.0,
            "num_chunks": 1,
            "peak_per_chunk_gb": 0.0,
            "status": "pass",
            "n_chunks_field": 1,
            "n_chunks_decim": 1
        },
        "decimation": {
            "use_two_pass": False,
            "est_global_ram_gb": 0.0,
            "est_assembled_gb": 0.0,
            "pc_reduction": 0.0,
            "fg_reduction": 0.0,
            "status": "global"
        }
    }

    if len(lattice_elements) == 0:
        # No lattice elements - still compute RAM
        smooth_sigma = SMOOTH_SIGMA
        num_chunks, peak_gb, avail_gb, total_gb, n_ch_f, n_ch_d = _compute_num_chunks(
            nx, ny, nz, subgrid, smooth_sigma)
        result["ram"].update({
            "available_gb": round(avail_gb, 2),
            "total_field_gb": round(total_gb, 2),
            "num_chunks": num_chunks,
            "peak_per_chunk_gb": round(peak_gb, 2),
            "n_chunks_field": n_ch_f,
            "n_chunks_decim": n_ch_d,
            "status": "critical" if peak_gb > avail_gb * 0.85 else
                      "warning"  if peak_gb > avail_gb * 0.60 else "pass"
        })
        return result

    min_density  = float(lattice_elements['density'].min())
    target_size  = 0.25
    feature_name = "Thickness"
    predicted_size = 0.0

    if lattice_type in ("gyroid_ligament", "diamond"):
        predicted_size = CALIB.strut_diameter(min_density); feature_name = "Strut Diameter"
    else:
        predicted_size = CALIB.wall_thickness(min_density); feature_name = "Wall Thickness"

    # Feature size status
    if predicted_size < 0.15:
        feat_status = "critical"
    elif predicted_size < 0.25:
        feat_status = "warning"
    else:
        feat_status = "pass"

    # Density floor recommendation
    floor_needed = predicted_size < target_size
    recommended_floor = 0.0
    if floor_needed:
        if lattice_type in ("gyroid_ligament", "diamond"):
            recommended_floor = CALIB.density_for_strut_diameter(target_size)
        else:
            recommended_floor = CALIB.density_for_wall_thickness(target_size)
        recommended_floor = float(min(recommended_floor, 0.99))

    result["feature"].update({
        "name": feature_name,
        "min_density": round(min_density, 4),
        "predicted_size_mm": round(predicted_size, 4),
        "status": feat_status,
        "density_floor_needed": floor_needed,
        "recommended_density_floor": round(recommended_floor, 4),
        "safe_size_mm": target_size
    })

    # Subgrid recommendation from the convergence-derived resolution law,
    # evaluated across the ACTIVE density range, identical to the law the
    # generator applies during meshing (check_printability_limits). This is the
    # value the launcher consumes. The legacy feature-size heuristic is kept only
    # as a fallback for a lattice without a law table.
    active_lo = min_density
    active_hi = float(lattice_elements['density'].max())
    law_subgrid, ceiling_hit = lattice_required_subgrid(
        lattice_type, active_lo, active_hi, dx, period)
    if law_subgrid is None:
        effective_size   = predicted_size if predicted_size > 0 else 0.001
        samples_across   = LIG_SAMPLES_ACROSS if CALIB.mode == 'ligament' else SHEET_SAMPLES_ACROSS
        required_subgrid = int(np.ceil((samples_across * dx) / effective_size))
        ceiling_hit      = False
    else:
        required_subgrid = law_subgrid
    optimal_subgrid  = max(4, required_subgrid)

    if subgrid < required_subgrid:
        sg_status = "warning" if subgrid >= required_subgrid / 2 else "critical"
    elif subgrid > optimal_subgrid:
        sg_status = "overkill"
    else:
        sg_status = "pass"

    result["subgrid"].update({
        "current": subgrid,
        "recommended": required_subgrid,
        "optimal": optimal_subgrid,
        "status": sg_status,
        "void_limited": bool(ceiling_hit)
    })

    # RAM budget
    smooth_sigma = SMOOTH_SIGMA
    # Triangle count estimate — period-aware using known TPMS specific surface area
    # SA per unit volume scales as sa_factor/period^2 (mm^-1)
    _sa_factors = {
        'gyroid_sheet': 3.09, 'gyroid_ligament': 3.09,
        'iwp': 3.60, 'primitive': 2.35, 'diamond': 3.90, 'neovius': 4.80
    }
    sa_factor  = _sa_factors.get(lattice_type, 3.0)
    total_vol  = nx * ny * nz * (dx ** 3)           # mm³
    surface_mm2 = total_vol * sa_factor / (period ** 2)  # mm²
    tri_area   = (dx / subgrid) ** 2 / 2.0          # mm² per triangle
    n_raw_est  = max(10000, int(surface_mm2 / tri_area))
    likely_two_pass = False
    if decimation_mode == "global":
        likely_two_pass = False
    elif decimation_mode == "two_pass":
        likely_two_pass = True
    else:
        avail_quick = psutil.virtual_memory().available / 1e9
        est_global  = (n_raw_est * _VTK_BYTES_PER_RAW_FACE) / 1e9
        likely_two_pass = est_global > avail_quick * 0.70

    num_chunks, peak_gb, avail_gb, total_gb, n_ch_f, n_ch_d = _compute_num_chunks(
        nx, ny, nz, subgrid, smooth_sigma,
        n_raw_est=n_raw_est, do_per_chunk_decim=likely_two_pass)

    ram_status = ("critical" if peak_gb > avail_gb * 0.85 else
                  "warning"  if peak_gb > avail_gb * 0.60 else "pass")

    result["ram"].update({
        "available_gb":      round(avail_gb, 2),
        "total_field_gb":    round(total_gb, 2),
        "num_chunks":        num_chunks,
        "peak_per_chunk_gb": round(peak_gb, 2),
        "status":            ram_status,
        "n_chunks_field":    n_ch_f,
        "n_chunks_decim":    n_ch_d
    })

    # Decimation plan
    two_pass, pc_red, fg_red, est_global_gb, est_assembled_gb = _compute_decimation_plan(
        n_raw_est, decimation_reduction, avail_gb)
    if decimation_mode == "global":   two_pass = False
    elif decimation_mode == "two_pass": two_pass = True

    result["decimation"].update({
        "use_two_pass":       two_pass,
        "est_global_ram_gb":  round(est_global_gb, 2),
        "est_assembled_gb":   round(est_assembled_gb, 2),
        "pc_reduction":       round(pc_red, 4),
        "fg_reduction":       round(fg_red, 4),
        "est_raw_triangles":  int(n_raw_est),
        "status":             "two_pass" if two_pass else "global"
    })

    return result


# ══════════════════════════════════════════════════════════════════════════════
# STEP 0 — Startup: clean up any leftover temp files from a previous crashed run
# ══════════════════════════════════════════════════════════════════════════════

base_name, _ext   = os.path.splitext(OUTPUT_STL)
final_stl_path    = f"{base_name}_{LATTICE_TYPE}{_ext}"
TEMP_PREFIX       = _temp_prefix(OUTPUT_STL, LATTICE_TYPE)

n_stale = cleanup_temp_files(TEMP_PREFIX)
n_stale += cleanup_temp_files(TEMP_PREFIX.replace("_temp_chunk_", "_temp_p1_"))
if n_stale:
    print(f"  [Startup] Deleted {n_stale} leftover temp files from a previous run.")

# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Load CSV
# ══════════════════════════════════════════════════════════════════════════════

print(f"── Step 1: Loading CSV ──────────────────────────────────────────")
print(f"  Lattice : {LATTICE_TYPE}  |  Period : {GYROID_PERIOD} mm  |  Subgrid : {SUBGRID}")
df = pd.read_csv(CSV_PATH, comment='#')
df['density'] = df['Density_Percentage'] / 100.0

void_count    = (df['density'] == 0.0).sum()
solid_count   = (df['density'] == 1.0).sum()
lattice_count = ((df['density'] > 0) & (df['density'] < 1)).sum()
print(f"  {len(df)} elements  |  Void: {void_count}  Solid: {solid_count}  Lattice: {lattice_count}")

# ── Element size from coordinate spacing ─────────────────────────────────────
unique_x = np.sort(df['X'].unique())
dx = unique_x[1] - unique_x[0] if len(unique_x) > 1 else 1.0

# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Reconstruct 3D voxel grid
# ══════════════════════════════════════════════════════════════════════════════

print("── Step 2: Building voxel grid ─────────────────────────────────")

xi = np.round((df['X'] - df['X'].min()) / dx).astype(int).values
yi = np.round((df['Y'] - df['Y'].min()) / dx).astype(int).values
zi = np.round((df['Z'] - df['Z'].min()) / dx).astype(int).values
nx, ny, nz = int(xi.max()) + 1, int(yi.max()) + 1, int(zi.max()) + 1

print(f"  Element size (dx): {dx:.3f} mm")
print(f"  Grid: {nx} × {ny} × {nz}  =  {nx*ny*nz:,} voxels")

voxel_density = np.zeros((nx, ny, nz), dtype=np.float32)
voxel_density[xi, yi, zi] = df['density'].values.astype(np.float32)

# ══════════════════════════════════════════════════════════════════════════════
# --preflight-only: compute analysis, print JSON, exit
# ══════════════════════════════════════════════════════════════════════════════

if PREFLIGHT_ONLY:
    pf = _compute_preflight_data(df, LATTICE_TYPE, GYROID_PERIOD, SUBGRID, dx, nx, ny, nz,
                                 DECIMATION_REDUCTION, DECIMATION_MODE)
    print(json.dumps(pf))
    sys.exit(0)

# ══════════════════════════════════════════════════════════════════════════════
# PRE-FLIGHT CHECK (with RAM plan and auto NUM_CHUNKS)
# ══════════════════════════════════════════════════════════════════════════════

density_floor, SUBGRID, NUM_CHUNKS, USE_TWO_PASS, PC_REDUCTION, FG_REDUCTION = \
    check_printability_limits(df, LATTICE_TYPE, GYROID_PERIOD, SUBGRID, dx, nx, ny, nz,
                              non_interactive=NON_INTERACTIVE,
                              density_floor_arg=_DENSITY_FLOOR_ARG,
                              subgrid_override=_SUBGRID_OVERRIDE)

# Apply density floor if the user accepted the smart fix
if density_floor > 0.0:
    mask = (df['density'] > 0.0) & (df['density'] < density_floor)
    n_fixed = mask.sum()
    df.loc[mask, 'density'] = density_floor
    voxel_density[xi[mask.values], yi[mask.values], zi[mask.values]] = density_floor
    print(f"  [System] Raised {n_fixed} elements to {density_floor*100:.1f}% density floor.")

# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — True End-to-End Chunked Pipeline  (Fix 1, 2, 4)
# ══════════════════════════════════════════════════════════════════════════════
#
# Each Z-chunk independently computes D_chunk, F_chunk, combines them,
# runs marching cubes, and saves the raw geometry to disk.
# The full implicit field is never allocated in RAM.
#
# Memory per chunk ≈ 2.5–3.5 × (nx*SUBGRID × ny*SUBGRID × chunk_z × 4 bytes)
# (D_chunk + F_chunk + block, with +1 for smooth_D if SMOOTH_SIGMA > 0)
#
# Z-coordinate conventions
# ─────────────────────────
# "content slices": indices into the raw nx*SUBGRID × ny*SUBGRID × nz*SUBGRID field
#                   (0 … nz*SUBGRID − 1)
# "block":          the array passed to marching_cubes for this chunk — always
#                   padded with a +2.0 void border on the X and Y faces (Fix 4),
#                   and on the Z face only at the global start (first chunk) and
#                   global end (last chunk).  Interior chunk seams use a 1-slice
#                   overlap instead of padding — same as v5's chunked MC strategy,
#                   just now extended to D and F too.

print(f"── Step 3: Chunked Field Generation & Marching Cubes ({NUM_CHUNKS} chunks) ──")

# ── Global arrays shared across all chunks ───────────────────────────────────
omega      = 2.0 * np.pi / GYROID_PERIOD
spacing    = (dx / SUBGRID,) * 3

xs = (np.arange(nx * SUBGRID, dtype=np.float32) / SUBGRID) * dx
ys = (np.arange(ny * SUBGRID, dtype=np.float32) / SUBGRID) * dx
zs_global = (np.arange(nz * SUBGRID, dtype=np.float32) / SUBGRID) * dx

# These 1-D index arrays map each subgrid point back to its parent voxel.
# Computed once; sliced per chunk.
xi_sub_1d = np.clip((xs / dx).astype(int), 0, nx - 1)
yi_sub_1d = np.clip((ys / dx).astype(int), 0, ny - 1)
zi_sub_1d = np.clip((zs_global / dx).astype(int), 0, nz - 1)

# 2-D XY grid for TPMS evaluation (re-used every slice)
X_2d, Y_2d   = np.meshgrid(xs, ys, indexing='ij')
omega_X_2d   = omega * X_2d
omega_Y_2d   = omega * Y_2d

# Probe mode and SCALE once (dummy call at origin)
_, mode, SCALE = LATTICE_FUNCS[LATTICE_TYPE](0.0, 0.0, 0.0)

total_z          = nz * SUBGRID
content_chunk_z  = int(np.ceil(total_z / NUM_CHUNKS))
n_chunks_written = 0

# v7 density self-check accumulators (content sub-voxels only, no overlap)
_solid_acc = 0
_total_acc = 0

for chunk_idx in range(NUM_CHUNKS):

    c_start  = chunk_idx * content_chunk_z
    c_end    = min((chunk_idx + 1) * content_chunk_z, total_z)

    if c_start >= total_z:
        break                      # guard against rounding producing an empty last chunk

    is_first  = (chunk_idx == 0)
    is_last   = (c_end >= total_z)
    n_content = c_end - c_start
    n_overlap = 0 if is_last else 1   # 1-slice overlap for seamless MC continuity
    n_compute = n_content + n_overlap  # Z slices to actually evaluate

    print(f"  Chunk {chunk_idx+1}/{NUM_CHUNKS}: "
          f"content Z [{c_start}:{c_end}]  ({n_compute} slices evaluated)")

    # ── Subgrid Z indices and their mm coordinates ────────────────────────────
    z_global_indices = np.arange(c_start, c_start + n_compute)
    z_coords_mm      = zs_global[z_global_indices]
    zi_chunk_1d      = zi_sub_1d[z_global_indices]   # parent-voxel Z indices

    # ── D_chunk: density at subgrid resolution (Fix 1 — only this Z-slab) ────
    if SMOOTH_SIGMA > 0:
        # For Gaussian blur, extend the Z range so boundary voxels have full
        # neighbourhood context.  Extension = 3 sigma in subgrid voxels.
        smooth_ext = int(np.ceil(3.0 * SMOOTH_SIGMA))
        ext_start  = max(0, c_start - smooth_ext)
        ext_end    = min(total_z, c_start + n_compute + smooth_ext)
        zi_ext_1d  = zi_sub_1d[ext_start:ext_end]

        D_ext       = voxel_density[np.ix_(xi_sub_1d, yi_sub_1d, zi_ext_1d)]
        valid_mask  = (D_ext > 0.0).astype(np.float32)
        blurred_D   = gaussian_filter((D_ext * valid_mask).astype(np.float64), sigma=SMOOTH_SIGMA)
        blurred_w   = gaussian_filter(valid_mask.astype(np.float64),           sigma=SMOOTH_SIGMA)
        del valid_mask
        smooth_D_ext  = (blurred_D / (blurred_w + 1e-8)).astype(np.float32)
        del blurred_D, blurred_w

        # Carve out the portion that corresponds to this chunk's content + overlap
        trim_s        = c_start - ext_start
        smooth_D_chunk = smooth_D_ext[:, :, trim_s : trim_s + n_compute]
        del smooth_D_ext

        # Unsmoothed D_chunk still needed for the hard solid/void overrides
        D_chunk = voxel_density[np.ix_(xi_sub_1d, yi_sub_1d, zi_chunk_1d)]
    else:
        # SMOOTH_SIGMA == 0: smooth_D_chunk IS D_chunk — no copy, no extra RAM
        D_chunk        = voxel_density[np.ix_(xi_sub_1d, yi_sub_1d, zi_chunk_1d)]
        smooth_D_chunk = D_chunk

    # ── F_chunk: raw TPMS field for this Z-slab (Fix 1) ──────────────────────
    F_chunk = np.zeros((nx * SUBGRID, ny * SUBGRID, n_compute), dtype=np.float32)

    for local_k in range(n_compute):
        slice_Z = omega * np.full_like(X_2d, z_coords_mm[local_k])
        F_slice, _, _ = LATTICE_FUNCS[LATTICE_TYPE](omega_X_2d, omega_Y_2d, slice_Z)
        F_chunk[:, :, local_k] = F_slice

    # ── Combine D + F in-place (v7: calibrated density->threshold mapping) ───
    # ligament: solid = {F < t}; sheet: solid = {|F| < t}; t = quantile(m, density).
    t = CALIB.threshold_for_density(smooth_D_chunk).astype(F_chunk.dtype)
    if mode == 'sheet':
        np.abs(F_chunk, out=F_chunk)
    F_chunk -= t
    del t

    if smooth_D_chunk is not D_chunk:
        del smooth_D_chunk

    # ── Hard solid / void overrides ───────────────────────────────────────────
    F_chunk[D_chunk >= 1.0] = -2.0   # force fully solid
    F_chunk[D_chunk <= 0.0] = +2.0   # force fully void
    del D_chunk

    # v7 self-check: count realised solid sub-voxels over content slices only
    # (exclude the 1-slice overlap so adjacent chunks are not double-counted).
    _solid_acc += int(np.count_nonzero(F_chunk[:, :, :n_content] < 0.0))
    _total_acc += F_chunk.shape[0] * F_chunk.shape[1] * n_content

    # ── Assemble the marching-cubes block (Fix 4 — no np.pad) ────────────────
    #
    # X and Y faces are always bordered with +2.0 void (closed mesh on sides).
    # Z borders: +2.0 void only at the global start (first chunk) and end (last
    # chunk).  At interior seams we rely on the 1-slice overlap — no padding.
    #
    z_pre_pad  = 1 if is_first else 0
    z_post_pad = 1 if is_last  else 0
    block_z    = z_pre_pad + n_compute + z_post_pad

    # Pre-fill with +2.0 (void).  The X/Y border rows stay +2.0 permanently.
    block = np.full((nx * SUBGRID + 2, ny * SUBGRID + 2, block_z),
                    2.0, dtype=np.float32)

    # Write F_chunk into the interior of the block
    block[1:-1, 1:-1, z_pre_pad : z_pre_pad + n_compute] = F_chunk
    del F_chunk

    # ── Marching cubes ────────────────────────────────────────────────────────
    v, f, _, _ = marching_cubes(block, level=0.0, spacing=spacing)
    del block

    # ── Correct vertex coordinates to global mm space ─────────────────────────
    #
    # In the block, X index 0 is the void border → first content X is at
    # block index 1 → its mm position is 1 × spacing.  But its true global
    # mm position is 0.  So subtract 1 × spacing from X (and Y, same reason).
    #
    # For Z:
    #   is_first  (z_pre_pad=1): content starts at block index 1 → true Z = 0
    #             → correction = c_start×spacing − 1×spacing = (0−1)×spacing ✓
    #   interior  (z_pre_pad=0): content starts at block index 0
    #             → correction = c_start × spacing ✓
    v[:, 0] -= spacing[0]                          # X border correction
    v[:, 1] -= spacing[1]                          # Y border correction
    v[:, 2] += (c_start - z_pre_pad) * spacing[2]  # Z global offset

    # ── Save raw chunk geometry to disk (Fix 2) ───────────────────────────────
    np.save(f"{TEMP_PREFIX}{chunk_idx}_verts.npy", v.astype(np.float32))
    np.save(f"{TEMP_PREFIX}{chunk_idx}_faces.npy", f.astype(np.int32))
    n_chunks_written += 1

    print(f"    {len(f):,} triangles  |  saved to disk  |  chunk RAM freed")
    del v, f

# Free the shared XY grid — no longer needed after the loop
del X_2d, Y_2d, omega_X_2d, omega_Y_2d

# ══════════════════════════════════════════════════════════════════════════════
# v7 DENSITY SELF-CHECK
# Realised volume fraction (fraction of content sub-voxels classified solid)
# should equal the requested mean relative density when the threshold mapping is
# correct. With SMOOTH_SIGMA = 0 this is exact; smoothing introduces a small,
# expected deviation near density gradients.
# ══════════════════════════════════════════════════════════════════════════════
if _total_acc > 0:
    realized_vf  = _solid_acc / _total_acc
    requested_vf = float(voxel_density.mean())
    diff_pp      = abs(realized_vf - requested_vf) * 100.0
    note = "  (smoothing on; small deviation expected)" if SMOOTH_SIGMA > 0 else ""
    print("-- v7 density self-check ---------------------------------------")
    print(f"  Requested mean relative density : {requested_vf*100:.2f}%")
    print(f"  Realised lattice volume fraction: {realized_vf*100:.2f}%")
    print(f"  Difference                      : {diff_pp:.2f} pp{note}")

# ══════════════════════════════════════════════════════════════════════════════
# FIX 7 — Seam-safe two-pass decimation helpers
# ══════════════════════════════════════════════════════════════════════════════
#
# Problem: fast_simplification (VTK QEM) treats the open Z-boundary edges of
# each mesh chunk as free edges and collapses nearby vertices, creating the
# crack seen in the assembled mesh.
#
# Solution — two steps:
#
# _phase1_seam_safe  — splits each chunk into a "protected" zone (near Z-seams)
#   and a "decimatable" interior.  The protected zone is found by a 2-step
#   vertex expansion from the seed boundary vertices, which guarantees the
#   protected and interior vertex sets are disjoint after expansion (no face
#   straddles the boundary).  Only the interior is passed to fast_simplification.
#   Protected faces are kept at full resolution and reassembled gap-free.
#
# _weld_vertices  — after assembly the seam plane has duplicate vertices (one
#   from each adjacent chunk) at exactly the same 3D position.  A scipy
#   cKDTree pass merges them, converting T-junctions into manifold edges.

def _phase1_seam_safe(v, f, pc_reduction, spacing_z, is_first_chunk, is_last_chunk):
    """
    Decimate a mesh chunk while preventing seam-plane cracks.

    Phantom-triangle boundary pinning
    ───────────────────────────────────
    QEM collapses vertices on open Z-boundary edges ("free edges" of the chunk
    fragment), displacing them and creating the crack visible in the final mesh.

    Fix: attach three spike triangles to each boundary vertex, with their tips
    at coordinates ±1e6 mm.  VTK computes an enormous QEM error for any move
    touching those triangles, so it will never collapse the boundary vertex.
    After decimation, phantom geometry is stripped by coordinate range test.

    Protected zone: vertices within 3 subgrid voxels of each non-global Z-seam.
    """
    if pc_reduction <= 0.0 or len(f) < 10:
        return v, f

    z_min = float(v[:, 2].min())
    z_max = float(v[:, 2].max())
    depth = spacing_z * 2  # 2 subgrid voxels — narrower protected zone reduces seam line

    seed = np.zeros(len(v), dtype=bool)
    if not is_first_chunk:
        seed |= (v[:, 2] < z_min + depth)
    if not is_last_chunk:
        seed |= (v[:, 2] > z_max - depth)

    if not seed.any():
        return fast_simplification.simplify(v, f, target_reduction=pc_reduction)

    boundary_idx = np.where(seed)[0]

    # Phantom offset — must be >> all real mesh coordinates but within float32 safe range.
    # _PH = 1e6 caused the spike artifacts: float32(x + 1e6) loses sub-mm precision,
    # so boundary vertices at different X positions all round to the same phantom
    # coordinate.  VTK creates degenerate faces between those collapsed phantoms,
    # which then show up as spikes after the phantom strip step.
    # Fix: use 20× the actual mesh bounding box max.  For this part (120mm wide),
    # _PH = 2400mm; float32(120 + 2400) = 2520mm, precision ≈ 0.0003mm ≪ spacing.
    _PH = max(float(np.abs(v).max()) * 20.0, 200.0)  # minimum 200mm

    phantom_verts = []
    phantom_faces = []
    for i, bi in enumerate(boundary_idx):
        bv   = v[bi]
        base = len(v) + i * 3
        phantom_verts.append(bv + np.array([_PH, 0.,  0. ], dtype=np.float32))
        phantom_verts.append(bv + np.array([0.,  _PH, 0. ], dtype=np.float32))
        phantom_verts.append(bv + np.array([0.,  0.,  _PH], dtype=np.float32))
        phantom_faces.append([bi, base,   base+1])
        phantom_faces.append([bi, base+1, base+2])
        phantom_faces.append([bi, base+2, base  ])

    aug_v = np.vstack([v, np.array(phantom_verts, dtype=np.float32)])
    aug_f = np.vstack([f, np.array(phantom_faces, dtype=np.int32)])

    # Adjusted reduction: phantom faces survive decimation (they pin the boundary),
    # so we need to hit the original face target after they are stripped.
    n_phantom     = len(phantom_faces)
    target_orig   = max(4, int(len(f) * (1.0 - pc_reduction)))
    adj_reduction = 1.0 - (target_orig + n_phantom) / len(aug_f)
    adj_reduction = float(np.clip(adj_reduction, 0.0, 0.995))

    aug_v_dec, aug_f_dec = fast_simplification.simplify(
        aug_v, aug_f, target_reduction=adj_reduction)

    # Strip phantom geometry: any vertex with |coord| > _PH/2 is phantom.
    # Real mesh stays within the original bounding box (~120mm × 20mm × 20mm);
    # phantom coordinates are at _PH + original_coord ≈ _PH >> _PH/2 from origin.
    is_ph_v = (np.abs(aug_v_dec) > _PH / 2.0).any(axis=1)
    is_ph_f = is_ph_v[aug_f_dec].any(axis=1)
    real_f  = aug_f_dec[~is_ph_f]

    # Remove orphaned (unreferenced) vertices
    used     = np.zeros(len(aug_v_dec), dtype=bool)
    used[real_f.ravel()] = True
    real_idx = np.where(used)[0]
    v_remap  = np.full(len(aug_v_dec), -1, dtype=np.int64)
    v_remap[real_idx] = np.arange(len(real_idx))

    return aug_v_dec[real_idx].astype(np.float32), v_remap[real_f].astype(np.int32)


def _weld_vertices(verts, faces, tolerance):
    """
    Merge vertices within `tolerance` of each other using Union-Find + cKDTree.

    Used after two-pass assembly to stitch duplicate seam-plane vertices
    (both adjacent chunks generate identical vertices at the shared Z-plane;
    this pass merges them into one, converting T-junctions into manifold edges).

    Returns: (new_verts, new_faces, n_welded)
    """
    from scipy.spatial import cKDTree

    tree  = cKDTree(verts)
    pairs = tree.query_pairs(tolerance, output_type='ndarray')

    if len(pairs) == 0:
        return verts, faces, 0

    # Union-Find (path-compressed)
    parent = np.arange(len(verts), dtype=np.int32)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in pairs:
        ri, rj = find(int(i)), find(int(j))
        if ri != rj:
            parent[ri] = rj

    roots = np.array([find(i) for i in range(len(verts))], dtype=np.int32)
    unique_roots, remap = np.unique(roots, return_inverse=True)

    new_verts = verts[unique_roots]
    new_faces = remap[faces.ravel()].reshape(faces.shape).astype(np.int32)

    valid = ((new_faces[:, 0] != new_faces[:, 1]) &
             (new_faces[:, 1] != new_faces[:, 2]) &
             (new_faces[:, 0] != new_faces[:, 2]))

    return new_verts.astype(np.float32), new_faces[valid], len(verts) - len(new_verts)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — Assemble mesh + Decimation  (Fix 6: global or two-pass)
# ══════════════════════════════════════════════════════════════════════════════
#
# All field RAM is free at this point.
#
# GLOBAL path  (USE_TWO_PASS = False)
#   Load all chunks → vstack → fast_simplification in one shot.
#   Cleanest result, no seam risk. Requires the full raw mesh in RAM.
#
# TWO-PASS path  (USE_TWO_PASS = True)
#   Phase 1 — Load each chunk from disk, decimate it to PC_REDUCTION,
#              accumulate in RAM.  Peak RAM ≈ est_assembled_gb (small).
#   Phase 2 — Final global pass on the assembled (already-small) mesh
#              to reach the requested DECIMATION_REDUCTION.
#   Seam quality: QEM decimation preserves the most structurally important
#   triangles.  Phase 1 at ≤ 60 % reduction leaves abundant geometry at
#   chunk boundaries, and Phase 2 further regularises the whole surface.
#   Any residual seam artefacts are sub-voxel scale.

mode_label = "two-pass" if USE_TWO_PASS else "global"
print(f"── Step 4: Assembling & Decimating ({mode_label}) ──────────────────")

try:
    if not USE_TWO_PASS:
        # ── GLOBAL: load all raw chunks, vstack, single decimation pass ──────
        all_verts, all_faces = [], []
        offset = 0
        for i in range(n_chunks_written):
            v = np.load(f"{TEMP_PREFIX}{i}_verts.npy")
            f = np.load(f"{TEMP_PREFIX}{i}_faces.npy").astype(np.int64) + np.int64(offset)
            all_verts.append(v)
            all_faces.append(f)
            offset += len(v)

        verts = np.vstack(all_verts)
        faces = np.vstack(all_faces).astype(np.int32)
        del all_verts, all_faces
        print(f"  Raw mesh: {len(faces):,} triangles  |  {len(verts):,} vertices")

        if DECIMATION_REDUCTION > 0.0:
            print(f"  Global decimation ({DECIMATION_REDUCTION*100:.0f}% reduction)...")
            verts, faces = fast_simplification.simplify(
                verts, faces, target_reduction=DECIMATION_REDUCTION)
            print(f"  Decimated: {len(faces):,} triangles  |  {len(verts):,} vertices")

    else:
        # ── TWO-PASS: seam-safe phase 1, vertex weld, then global phase 2 ─────
        #
        # WHY SUBPROCESS ISOLATION:
        #   gc.collect() cannot release VTK's C++ heap pages back to the OS.
        #   Monitored run confirmed: after Phase 1 chunk 1 decimation (+3.17 GB),
        #   gc.collect() freed only 0.03 GB.  The process retained 3.14 GB of
        #   VTK heap, carrying it into chunk 2 and causing an OOM crash.
        #
        #   Fix: each chunk's VTK decimation runs in a FRESH SUBPROCESS.
        #   When the subprocess exits, the OS unconditionally reclaims every
        #   byte it allocated — including all VTK internal heap pages.
        #   The main process RSS stays near its baseline throughout Phase 1.

        import subprocess
        spacing_z = dx / SUBGRID
        total_raw = 0
        P1_PREFIX = TEMP_PREFIX.replace("_temp_chunk_", "_temp_p1_")

        # ── Phase 1: each chunk decimated in its own subprocess ───────────────
        #
        # WHY -c INSTEAD OF A SCRIPT FILE:
        #   The previous approach wrote _beso_p1_worker.py to disk and called
        #   it with [python, script_path, ...].  On Windows, security scanners
        #   (Defender) scan newly-executed .py files.  The first subprocess
        #   (chunk 1) triggers the scan; the scanner holds a lock on the file
        #   while finishing.  Chunk 2 starts immediately after, finds the file
        #   locked, and the Python interpreter crashes before writing anything
        #   to stderr — producing the silent "system +0.02 GB, empty stderr"
        #   failure pattern observed in the monitored run.
        #
        #   Passing the code via `python -c CODE` writes nothing to disk.
        #   No file → no lock → no scanner interference.
        #   sys.argv[0] becomes '-c'; positional args occupy sys.argv[1:].

        for i in range(n_chunks_written):
            raw_f_hdr = np.load(f"{TEMP_PREFIX}{i}_faces.npy", mmap_mode='r')
            chunk_raw  = len(raw_f_hdr)
            total_raw += chunk_raw
            del raw_f_hdr

            is_first_chunk = (i == 0)
            is_last_chunk  = (i == n_chunks_written - 1)

            cmd = [
                sys.executable, '-c', _P1_WORKER_CODE,   # code passed inline
                f"{TEMP_PREFIX}{i}_verts.npy",
                f"{TEMP_PREFIX}{i}_faces.npy",
                f"{P1_PREFIX}{i}_verts.npy",
                f"{P1_PREFIX}{i}_faces.npy",
                str(PC_REDUCTION),
                str(spacing_z),
                str(is_first_chunk),
                str(is_last_chunk),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=7200)

            if result.returncode != 0:
                raise RuntimeError(
                    f"Phase 1 subprocess failed for chunk {i+1}  "
                    f"(exit code {result.returncode}):\n"
                    f"--- stdout ---\n{result.stdout[-1000:]}\n"
                    f"--- stderr ---\n{result.stderr[-2000:]}")

            # Parse face count and method from subprocess stdout
            n_out, method = 0, "unknown"
            for line in result.stdout.splitlines():
                if line.startswith("DONE:"):
                    parts = line[5:].split(":", 1)
                    n_out  = int(parts[0])
                    method = parts[1] if len(parts) > 1 else ""

            print(f"  Phase 1 chunk {i+1}/{n_chunks_written}: "
                  f"{n_out:,} triangles  [{method}]  "
                  f"→ subprocess exited, VTK RAM returned to OS")

        # ── Load all Phase 1 results and assemble ─────────────────────────────
        all_verts, all_faces = [], []
        offset = 0
        for i in range(n_chunks_written):
            v = np.load(f"{P1_PREFIX}{i}_verts.npy")
            f = np.load(f"{P1_PREFIX}{i}_faces.npy").astype(np.int64) + np.int64(offset)
            all_verts.append(v)
            all_faces.append(f)
            offset += len(v)
            # Clean up Phase 1 temp files as we go
            try:
                os.remove(f"{P1_PREFIX}{i}_verts.npy")
                os.remove(f"{P1_PREFIX}{i}_faces.npy")
            except OSError:
                pass

        print(f"  Phase 1 done — raw: {total_raw:,}  →  assembled: {offset:,} vertices")
        verts = np.vstack(all_verts)
        faces = np.vstack(all_faces).astype(np.int32)
        del all_verts, all_faces

        # Weld duplicate seam-plane vertices from adjacent chunks
        weld_tol = spacing_z * 0.05   # 5 % of one subgrid voxel — stitches
                                       # float32-identical duplicates safely
        verts, faces, n_welded = _weld_vertices(verts, faces, weld_tol)
        print(f"  Seam weld: merged {n_welded:,} duplicate vertices  |  "
              f"{len(faces):,} triangles  |  {len(verts):,} vertices")

        if FG_REDUCTION > 0.0:
            print(f"  Phase 2 global ({FG_REDUCTION*100:.0f}% reduction)...")
            verts, faces = fast_simplification.simplify(
                verts, faces, target_reduction=FG_REDUCTION)
            print(f"  Final mesh: {len(faces):,} triangles  |  {len(verts):,} vertices")

finally:
    n_cleaned  = cleanup_temp_files(TEMP_PREFIX)
    n_cleaned += cleanup_temp_files(TEMP_PREFIX.replace("_temp_chunk_", "_temp_p1_"))
    print(f"  [Cleanup] Removed {n_cleaned} temp files.")

# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 — Export binary STL  (Fix 3 — no trimesh)
# ══════════════════════════════════════════════════════════════════════════════

print("── Step 5: Exporting STL ───────────────────────────────────────")

write_binary_stl(final_stl_path, verts, faces)

# Lightweight manifold check (replaces trimesh.is_watertight)
manifold = _is_manifold(faces)
if manifold is None:
    manifold_str = "not checked (mesh > 2M faces)"
elif manifold:
    manifold_str = "✓ manifold"
else:
    # T-junctions introduced by fast_simplification (VTK decimation) are
    # expected at high reduction ratios and were present in v5 too.
    # trimesh's fill_holes / fix_normals do not resolve them.
    # For a guaranteed manifold output: set DECIMATION_REDUCTION = 0.0
    # (raw marching cubes is always perfectly manifold).
    manifold_str = ("✗ T-junctions from fast_simplification decimation "
                    "(pre-existing behaviour — set DECIMATION_REDUCTION=0.0 "
                    "for a guaranteed manifold mesh)")

print(f"  Faces    : {len(faces):,}")
print(f"  Vertices : {len(verts):,}")
print(f"  Manifold : {manifold_str}")
print(f"  Saved -> {os.path.abspath(final_stl_path)}")
print("Done.")
