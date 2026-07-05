#!/usr/bin/env python3
"""
=============================================================================
  BESO STL GENERATOR  -  v5
  Standalone Python 3 script - zero Abaqus dependency.

  Reads the CSV files written by beso_geometry_dump.py and produces a clean,
  print-ready STL of the optimized solid structure.

  WHAT IS NEW IN v5 (vs v4):
    OPTIONAL NORMAL-DIRECTION RELAXATION ON LOCKED FLAT FACES
    (--lock-normal-cap, default 0.0 = unchanged from v4).  v4 kept locked flat
    faces exactly on their original plane, so any leftover voxel bumps on those
    surfaces survived. v5 lets planar-locked faces also move perpendicular to
    themselves, up to --lock-normal-cap mm, which flattens those bumps. The cap
    is a CEILING, not a forced move: already-flat faces (real mating surfaces)
    barely move because there is nothing to smooth; only bumpy artifact surfaces
    use the budget. Bore walls and sharp edges stay hard-pinned (0.000 mm)
    regardless. Recommended: --lock-normal-cap 0.5.

  WHAT WAS NEW IN v4:
    DE-SERRATED LOCK BOUNDARY. Planar-locked faces may slide WITHIN their plane
    (--slide-cap) to straighten the staircased lock boundary; bore and edges are
    hard-pinned. --rigid-lock restores the v3 pin-everything behaviour.

  WHAT WAS NEW IN v3:
    SMARTER LOCKING (--lock-mode original, default). A non-design face is locked
    only if it is original-exterior (no removed design element behind it); cut
    surfaces created by the optimization are smoothed. --lock-mode all reverts
    to the v2 behaviour (freeze every non-design face).

  CARRIED OVER:
    Shrink-free Taubin smoothing (vectorized sparse band-pass; HC also
    available via --smooth-method hc), and a dimensional validation report.

  All output is pure ASCII. Hex (C3D8*/C3D20*), tet (C3D4*/C3D10*) and wedge
  (C3D6*/C3D15*) families are all supported, including in the cut-face test.

  USAGE:
      python beso_stl_generator_v5.py [options]

  KEY LOCK OPTIONS (see --help for the full list):
      --lock-normal-cap FLT Max normal move for locked flat faces, mm (0.0).
                            Set ~0.5 to flatten voxel bumps on locked surfaces.
      --rigid-lock          Hard-pin every locked vertex (v3 behaviour).
      --slide-cap   FLT     Max in-plane slide for flat locked faces, mm (1.0).
      --plane-angle FLT     Normal spread (deg) for a locked vertex to count as
                            planar/relaxable (default 15).

  OPTIONS:
      --nodes        PATH   best_nodes.csv           (default: best_nodes.csv)
      --elements     PATH   best_elements.csv        (default: best_elements.csv)
      --solid        PATH   best_solid_elements.csv  (default: best_solid_elements.csv)
      --non-design   PATH   non_design_elements.csv  (default: non_design_elements.csv)
                            Optional. If absent, nothing is locked (a warning is printed).
      --output       PATH   Output STL filename       (default: optimized_structure.stl)

      --lock-mode    NAME   original | all           (default: original)
                            original = lock only original-exterior non-design
                                       faces; smooth cut surfaces (recommended).
                            all      = lock every non-design exposed face (v2).

      --smooth              Enable smoothing
      --smooth-method NAME  taubin | hc              (default: taubin)
      --smooth-iter  INT    Number of iterations     (default: 60)
      --smooth-lambda FLT   Positive step strength   (default: 0.5)
      --smooth-mu    FLT     Negative step (taubin)  (default: -0.53)
      --smooth-beta  FLT     HC correction strength  (default: 0.5, hc only)
      --no-lock             Disable locking entirely (smooth everything)

      --ascii               Write ASCII STL instead of binary

  EXAMPLES:
      # Print-ready: smart locking + shrink-free smoothing
      python beso_stl_generator_v5.py --smooth

      # Reproduce v2 locking (freeze all non-design surface)
      python beso_stl_generator_v5.py --smooth --lock-mode all

      # Reproduce the old hex behaviour (HC smoother)
      python beso_stl_generator_v5.py --smooth --smooth-method hc --smooth-iter 20
=============================================================================
"""

import argparse
import csv
import os
import struct
import sys
import time

import numpy as np

try:
    import scipy.sparse as sp
    _HAVE_SCIPY = True
except ImportError:
    _HAVE_SCIPY = False


# =============================================================================
#   FACE DEFINITION TABLES
#   For each supported element type, defines which local node indices form each
#   face. Indices are 0-based into the element connectivity list. Midside nodes
#   (on quadratic elements) are intentionally excluded - only corner nodes are
#   needed to define the face polygon for STL purposes.
# =============================================================================

FACE_TABLES = {
    # HEXAHEDRAL corners. Covers C3D8*, C3D20* (first 8 corner nodes).
    "HEX": [
        (0, 1, 2, 3),   # S1 bottom
        (4, 7, 6, 5),   # S2 top
        (0, 4, 5, 1),   # S3
        (1, 5, 6, 2),   # S4
        (2, 6, 7, 3),   # S5
        (3, 7, 4, 0),   # S6
    ],

    # TETRAHEDRAL corners. Covers C3D4*, C3D10* (first 4 corner nodes).
    "TET": [
        (0, 1, 2),      # S1
        (0, 3, 1),      # S2
        (1, 3, 2),      # S3
        (0, 2, 3),      # S4
    ],

    # WEDGE / PENTAHEDRAL corners. Covers C3D6*, C3D15* (first 6 corner nodes).
    "WEDGE": [
        (0, 1, 2),      # S1 triangular bottom
        (3, 5, 4),      # S2 triangular top
        (0, 3, 4, 1),   # S3 quad
        (1, 4, 5, 2),   # S4 quad
        (0, 2, 5, 3),   # S5 quad
    ],
}


def get_element_family(etype):
    """Maps an Abaqus element type string to a FACE_TABLES family, or None."""
    e = etype.upper().strip()
    if e.startswith("C3D8") or e.startswith("C3D20"):
        return "HEX"
    if e.startswith("C3D4") or e.startswith("C3D10"):
        return "TET"
    if e.startswith("C3D6") or e.startswith("C3D15"):
        return "WEDGE"
    return None


# =============================================================================
#   STEP 1 - DATA LOADING
# =============================================================================

def load_nodes(path):
    """Returns dict { node_id(int) : np.array([x, y, z]) }."""
    nodes = {}
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            nid = int(row["node_id"])
            nodes[nid] = np.array([float(row["x"]),
                                   float(row["y"]),
                                   float(row["z"])], dtype=np.float64)
    print("   Loaded {:,} nodes.".format(len(nodes)))
    return nodes


def load_elements(path):
    """Returns dict { elem_id(int) : (type_str, [node_ids]) }."""
    elements = {}
    with open(path, "r") as f:
        reader = csv.reader(f)
        next(reader)  # header
        for row in reader:
            if len(row) < 3:
                continue
            eid = int(row[0])
            etype = row[1].strip()
            conn = [int(x) for x in row[2:] if x.strip()]
            elements[eid] = (etype, conn)
    print("   Loaded {:,} elements.".format(len(elements)))
    return elements


def load_id_set(path, column="element_id"):
    """Returns a Python set of integer IDs from a single-column CSV."""
    ids = set()
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ids.add(int(row[column]))
    return ids


# =============================================================================
#   FACE KEYS AND THE CUT-FACE TEST
# =============================================================================

def face_key(node_ids, base):
    """
    Canonical integer key for a face, independent of node order.

    Corner node ids are sorted and packed into a single (big) integer using a
    positional encoding with the given base (base > max node id). Because all
    node ids are >= 1, a face of K nodes always has key >= base**(K-1), so faces
    of different node counts cannot collide. This keeps the void-face set lean
    (one integer per face instead of a tuple).
    """
    k = 0
    for v in sorted(node_ids):
        k = k * base + v
    return k


def build_void_face_set(elements, solid_set, base):
    """
    Builds the set of face keys belonging to VOIDED elements, i.e. elements that
    are present in the full mesh (best_elements) but were removed by the
    optimizer (not in best_solid). A non-design boundary face whose key is in
    this set has a removed design element on the other side -> it is a CUT face
    that the optimization created, and should be smoothed rather than locked.
    """
    print("\n-> Indexing cut surfaces (faces of removed design elements)...")
    void_faces = set()
    processed = 0
    n_void = 0
    skipped_types = set()
    for eid, (etype, conn) in elements.items():
        if eid in solid_set:
            continue                      # kept element, not a void
        fam = get_element_family(etype)
        if fam is None:
            skipped_types.add(etype)
            continue
        n_void += 1
        for face_local_idx in FACE_TABLES[fam]:
            ids = [conn[i] for i in face_local_idx]
            void_faces.add(face_key(ids, base))
        processed += 1
        if processed % 250000 == 0:
            print("   Indexed {:,} voided elements...".format(processed))
    if skipped_types:
        print("   [WARNING] Unrecognised voided element types skipped: {}".format(
            ", ".join(sorted(skipped_types))))
    print("   Voided elements: {:,}   unique cut faces: {:,}".format(
        n_void, len(void_faces)))
    return void_faces


# =============================================================================
#   STEP 2 - BOUNDARY FACE EXTRACTION
# =============================================================================

def extract_boundary_faces(elements, solid_set):
    """
    Finds all faces that belong to exactly one solid element.

    A face is the canonical (sorted) tuple of its corner node IDs. A face shared
    by two solid elements appears twice -> internal -> discarded. A face that
    appears once is on the boundary -> kept.

    Returns:
        boundary  : list of (ordered_node_ids, owner_elem_id)
    The owner element id is recorded so winding and non-design locking can be
    resolved without a second pass over the mesh.
    """
    print("\n-> Extracting boundary faces...")

    face_count = {}    # canonical tuple -> count
    face_ordered = {}  # canonical tuple -> ordered node list (first seen)
    face_owner = {}    # canonical tuple -> owner element id (first seen)

    skipped_types = set()
    processed = 0

    for eid in solid_set:
        if eid not in elements:
            continue
        etype, conn = elements[eid]
        family = get_element_family(etype)
        if family is None:
            skipped_types.add(etype)
            continue

        for face_local_idx in FACE_TABLES[family]:
            ordered = [conn[i] for i in face_local_idx]
            canonical = tuple(sorted(ordered))
            if canonical not in face_count:
                face_count[canonical] = 0
                face_ordered[canonical] = ordered
                face_owner[canonical] = eid
            face_count[canonical] += 1

        processed += 1
        if processed % 100000 == 0:
            print("   Processed {:,} / {:,} solid elements...".format(
                processed, len(solid_set)))

    if skipped_types:
        print("   [WARNING] Unrecognised element types skipped: {}".format(
            ", ".join(sorted(skipped_types))))

    boundary = [(face_ordered[k], face_owner[k])
                for k, count in face_count.items() if count == 1]

    print("   Boundary faces found: {:,}".format(len(boundary)))
    return boundary


# =============================================================================
#   STEP 3 - SURFACE MESH BUILD (node-id based) WITH OUTWARD NORMALS
# =============================================================================

# Per-vertex lock classification.
LOCK_FREE = 0    # not locked - smoothed normally
LOCK_PLANE = 1   # locked but on a coherent flat patch - may slide in-plane only
LOCK_PIN = 2     # locked on a bore / sharp edge - frozen completely

# A locked vertex counts as "planar" if every locked face touching it points
# within this angle of their average normal. Flat pads pass; bore walls and the
# creases between differently-oriented locked faces do not (they stay pinned).
PLANAR_ANGLE_DEG = 15.0


def _winding_outward(ids, nodes, elem_centroid):
    """
    Returns the (possibly swapped) triple of node IDs so its normal points away
    from elem_centroid. ids is a 3-tuple of node IDs.
    """
    v0 = nodes[ids[0]]
    v1 = nodes[ids[1]]
    v2 = nodes[ids[2]]
    normal = np.cross(v1 - v0, v2 - v0)
    face_centre = (v0 + v1 + v2) / 3.0
    if np.dot(normal, face_centre - elem_centroid) < 0.0:
        return (ids[0], ids[2], ids[1])
    return (ids[0], ids[1], ids[2])


def classify_locked_vertices(positions, faces, tri_locked, locked_mask,
                             plane_angle_deg=PLANAR_ANGLE_DEG):
    """
    Splits locked vertices into LOCK_PLANE (sit on a coherent flat patch; may be
    relaxed in-plane to remove the staircased lock boundary) and LOCK_PIN (bore
    walls and sharp edges; frozen). The constraint normal of a LOCK_PLANE vertex
    is the average of the locked faces touching it, computed from the ORIGINAL
    geometry, and defines the plane the vertex is allowed to slide within.

    Returns:
        lock_kind         : (N,) int8   one of LOCK_FREE / LOCK_PLANE / LOCK_PIN
        constraint_normal : (N,3) float  unit normal (only meaningful for PLANE)
    """
    n = len(positions)
    lock_kind = np.full(n, LOCK_FREE, dtype=np.int8)
    constraint_normal = np.zeros((n, 3), dtype=np.float64)
    if not locked_mask.any():
        return lock_kind, constraint_normal

    # Normals of the locked triangles (original geometry).
    lt = np.where(tri_locked)[0]
    T = positions[faces[lt]]
    fn = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
    L = np.linalg.norm(fn, axis=1, keepdims=True)
    L[L < 1e-12] = 1.0
    fn = fn / L

    # Accumulate, per vertex, the sum and the worst angular spread of the locked
    # face normals touching it.
    nsum = np.zeros((n, 3), dtype=np.float64)
    for k in range(3):
        np.add.at(nsum, faces[lt, k], fn)
    mean = nsum.copy()
    mlen = np.linalg.norm(mean, axis=1, keepdims=True)
    mlen[mlen < 1e-12] = 1.0
    mean = mean / mlen

    max_ang = np.zeros(n, dtype=np.float64)
    for k in range(3):
        v = faces[lt, k]
        dots = np.clip(np.einsum('ij,ij->i', fn, mean[v]), -1.0, 1.0)
        ang = np.degrees(np.arccos(dots))
        np.maximum.at(max_ang, v, ang)

    planar = locked_mask & (max_ang < plane_angle_deg)
    pinned = locked_mask & ~planar
    lock_kind[planar] = LOCK_PLANE
    lock_kind[pinned] = LOCK_PIN
    constraint_normal[planar] = mean[planar]
    return lock_kind, constraint_normal


def build_surface_mesh(boundary, elements, nodes, non_design_set,
                       void_faces=None, lock_mode="original", base=None,
                       plane_angle_deg=PLANAR_ANGLE_DEG):
    """
    Converts boundary faces into a node-id triangle mesh with correct winding
    and a per-vertex locked mask.

    Locking (which faces contribute locked vertices):
      lock_mode == "none"     : nothing is locked.
      lock_mode == "all"      : every exposed non-design face is locked (v2).
      lock_mode == "original" : a non-design face is locked only if it is
                                original exterior, i.e. its key is NOT in
                                void_faces (no removed design element behind it).
                                Cut faces are left free so they get smoothed.

    Working in node IDs (rather than rounded coordinates) means vertices are
    welded exactly and the classification is exact.

    Returns:
        positions         : (N,3) float64  vertex coordinates
        faces             : (M,3) int32      triangle vertex indices
        locked_mask       : (N,)  bool        True where the vertex is locked
        node_ids          : (N,)  int         original node id per vertex
        lock_kind         : (N,)  int8        LOCK_FREE / LOCK_PLANE / LOCK_PIN
        constraint_normal : (N,3) float       plane normal for LOCK_PLANE verts
    """
    print("\n-> Building surface mesh (node-id based, lock-mode={})...".format(
        lock_mode))

    # Element centroids are only needed for the owners of boundary faces.
    owner_ids = set(owner for _, owner in boundary)
    centroids = {}
    for eid in owner_ids:
        if eid not in elements:
            continue
        _, conn = elements[eid]
        coords = np.array([nodes[n] for n in conn if n in nodes])
        if len(coords):
            centroids[eid] = coords.mean(axis=0)

    tri_ids = []             # list of (n0, n1, n2) node-id triples
    tri_locked = []          # parallel: is this triangle on a locked face?
    locked_node_ids = set()  # node ids that sit on a locked face

    n_locked_faces = 0       # original-exterior non-design faces (locked)
    n_cut_faces = 0          # cut non-design faces (smoothed)
    n_design_faces = 0       # design faces (smoothed)

    for face, owner in boundary:
        # --- Decide whether this face is locked ---
        if lock_mode == "none" or owner not in non_design_set:
            face_locked = False
            if owner not in non_design_set:
                n_design_faces += 1
        elif lock_mode == "all":
            face_locked = True
            n_locked_faces += 1
        else:  # "original": locked unless a removed design element is behind it
            is_cut = (void_faces is not None and
                      face_key(face, base) in void_faces)
            face_locked = not is_cut
            if is_cut:
                n_cut_faces += 1
            else:
                n_locked_faces += 1

        centroid = centroids.get(owner)

        # --- Split polygon into triangles (corner-only faces: 3 or 4 nodes) ---
        if len(face) == 3:
            tris = [(face[0], face[1], face[2])]
        elif len(face) == 4:
            tris = [(face[0], face[1], face[2]),
                    (face[0], face[2], face[3])]
        else:
            tris = [(face[0], face[i], face[i + 1])
                    for i in range(1, len(face) - 1)]

        for t in tris:
            if centroid is not None:
                t = _winding_outward(t, nodes, centroid)
            tri_ids.append(t)
            tri_locked.append(face_locked)
            if face_locked:
                locked_node_ids.update(t)

    # Remap node ids -> contiguous vertex indices.
    used = {}
    positions_list = []
    node_id_list = []
    faces = []
    for (a, b, c) in tri_ids:
        idx = []
        for nid in (a, b, c):
            if nid not in used:
                used[nid] = len(positions_list)
                positions_list.append(nodes[nid])
                node_id_list.append(nid)
            idx.append(used[nid])
        faces.append(idx)

    positions = np.array(positions_list, dtype=np.float64)
    faces = np.array(faces, dtype=np.int32)
    node_ids = np.array(node_id_list, dtype=np.int64)
    tri_locked = np.array(tri_locked, dtype=bool)
    locked_mask = np.array([nid in locked_node_ids for nid in node_id_list],
                           dtype=bool)

    # Classify each locked vertex as PLANE (lies on a coherent flat patch, may
    # slide in-plane) or PIN (bore / sharp edge, frozen). See the function.
    lock_kind, constraint_normal = classify_locked_vertices(
        positions, faces, tri_locked, locked_mask, plane_angle_deg)
    n_plane = int((lock_kind == LOCK_PLANE).sum())
    n_pin = int((lock_kind == LOCK_PIN).sum())

    print("   Surface mesh: {:,} vertices, {:,} triangles".format(
        len(positions), len(faces)))
    print("   Boundary faces: {:,} design + {:,} non-design "
          "({:,} locked, {:,} cut->smoothed)".format(
              n_design_faces, n_locked_faces + n_cut_faces,
              n_locked_faces, n_cut_faces))
    print("   Locked vertices: {:,}  ({:,} planar-slide, {:,} hard-pin)".format(
        int(locked_mask.sum()), n_plane, n_pin))
    return positions, faces, locked_mask, node_ids, lock_kind, constraint_normal


# =============================================================================
#   STEP 4 - SMOOTHING (vectorized, sparse-matrix based)
# =============================================================================

def build_average_operator(faces, n_verts):
    """
    Builds a row-normalized neighbour-averaging operator A (sparse, n x n) from
    the triangle list, so that (A @ p)[i] is the mean of vertex i's 1-ring
    neighbours. The umbrella Laplacian is then L(p) = A @ p - p.
    """
    if not _HAVE_SCIPY:
        raise RuntimeError("scipy is required for smoothing. "
                           "Install it with: pip install scipy")

    e0 = faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1)
    e1 = faces[:, [1, 0, 2, 1, 0, 2]].reshape(-1)
    data = np.ones(e0.shape[0], dtype=np.float64)

    M = sp.coo_matrix((data, (e0, e1)), shape=(n_verts, n_verts)).tocsr()
    M.data[:] = 1.0          # binarize (collapse duplicate edges to adjacency)
    deg = np.asarray(M.sum(axis=1)).ravel()
    deg[deg == 0.0] = 1.0    # guard isolated vertices (none expected)
    Dinv = sp.diags(1.0 / deg)
    return (Dinv @ M).tocsr()


def smooth_surface(positions, faces, locked_mask, method="taubin",
                   iterations=60, lam=0.5, mu=-0.53, beta=0.5, lock=True,
                   lock_kind=None, constraint_normal=None,
                   rigid_lock=False, slide_cap=1.0, normal_cap=0.0):
    """
    Smooths the free vertices. Locked vertices are handled per their lock_kind:

      LOCK_PIN   : frozen at the original position (bore walls, sharp edges).
      LOCK_PLANE : relaxed with two independent caps relative to the original
                   position - up to slide_cap mm WITHIN the original plane
                   (straightens the staircased lock boundary) and up to
                   normal_cap mm PERPENDICULAR to it (flattens leftover voxel
                   bumps on the surface). normal_cap = 0 keeps the face exactly
                   on its original plane (v4 behaviour). The cap is a ceiling,
                   not a forced move: already-flat faces barely move because
                   there is little to smooth; only bumpy surfaces use it.

    rigid_lock=True forces every locked vertex to behave as LOCK_PIN (the v3
    behaviour). If lock_kind is None, all locked vertices are pinned.

    method == "taubin":  shrink-free band-pass (lambda then mu each iteration).
    method == "hc":      Humphrey's Classes HC Laplacian (volume-preserving).
    """
    print("\n-> Smoothing surface (method={}, iterations={})...".format(
        method, iterations))
    if method == "taubin":
        print("   lambda={}, mu={}".format(lam, mu))
    else:
        print("   lambda={}, beta={}".format(lam, beta))

    A = build_average_operator(faces, len(positions))
    p = positions.copy()
    original = positions.copy()

    has_locks = lock and bool(locked_mask.any())
    if lock and not locked_mask.any():
        print("   [NOTE] Locking requested but no locked vertices found; "
              "smoothing all vertices.")

    if lock_kind is None or rigid_lock:
        pin_mask = locked_mask.copy()
        plane_mask = np.zeros(len(positions), dtype=bool)
    else:
        pin_mask = (lock_kind == LOCK_PIN)
        plane_mask = (lock_kind == LOCK_PLANE)
    if rigid_lock and has_locks:
        print("   rigid-lock: all locked vertices hard-pinned.")
    elif has_locks:
        print("   plane-relax vertices: {:,} (in-plane cap {:.2f} mm, "
              "normal cap {:.2f} mm), hard-pinned: {:,}".format(
                  int(plane_mask.sum()), slide_cap, normal_cap,
                  int(pin_mask.sum())))

    pn = constraint_normal if constraint_normal is not None else None
    any_plane = bool(plane_mask.any())

    def apply_locks(arr):
        if not has_locks:
            return arr
        if any_plane:
            # Split the displacement-from-original into in-plane and normal
            # parts, cap each independently, recombine. normal_cap = 0 keeps the
            # vertex exactly on its original plane.
            d = arr[plane_mask] - original[plane_mask]
            nrm = pn[plane_mask]
            ncomp = np.einsum('ij,ij->i', d, nrm)
            inplane = d - ncomp[:, None] * nrm
            mag = np.linalg.norm(inplane, axis=1)
            over = mag > slide_cap
            if np.any(over):
                inplane[over] *= (slide_cap / mag[over])[:, None]
            ncomp = np.clip(ncomp, -normal_cap, normal_cap)
            arr[plane_mask] = original[plane_mask] + inplane + ncomp[:, None] * nrm
        arr[pin_mask] = original[pin_mask]
        return arr

    for it in range(iterations):
        if method == "taubin":
            p = apply_locks(p + lam * (A.dot(p) - p))
            p = apply_locks(p + mu * (A.dot(p) - p))
        elif method == "hc":
            Ap = A.dot(p)
            q = p + lam * (Ap - p)
            b = q - (beta * original + (1.0 - beta) * p)
            p = q - (beta * b + (1.0 - beta) * A.dot(b))
            p = apply_locks(p)
        else:
            raise ValueError("Unknown smoothing method: {}".format(method))

        if (it + 1) % 10 == 0 or it == iterations - 1:
            print("   Pass {}/{}...".format(it + 1, iterations))

    return p


def report_fidelity(original, smoothed, locked_mask,
                    lock_kind=None, constraint_normal=None):
    """Prints the dimensional-fidelity validation report."""
    disp = np.linalg.norm(smoothed - original, axis=1)
    n_locked = int(locked_mask.sum())
    n_free = int((~locked_mask).sum())

    print("\n-> Dimensional fidelity report")
    print("   Locked vertices              : {:,}".format(n_locked))
    print("   Free   vertices              : {:,}".format(n_free))

    if n_locked and lock_kind is not None:
        pin_mask = (lock_kind == LOCK_PIN)
        plane_mask = (lock_kind == LOCK_PLANE)
        if pin_mask.any():
            print("   Hard-pinned (bore/edges)     : {:,}  max move {:.6f} mm".format(
                int(pin_mask.sum()), float(disp[pin_mask].max())))
        if plane_mask.any():
            # off-plane component of the displacement
            d = smoothed[plane_mask] - original[plane_mask]
            nrm = constraint_normal[plane_mask]
            offp = np.abs(np.einsum('ij,ij->i', d, nrm))
            print("   Planar-relax (flat faces)    : {:,}".format(
                int(plane_mask.sum())))
            print("     off-plane move  median/max : {:.4f} / {:.4f} mm  "
                  "(flat faces barely move)".format(
                      float(np.median(offp)), float(offp.max())))
            print("     in-plane slide        max  : {:.4f} mm  "
                  "(serration removed)".format(float(disp[plane_mask].max())))
    elif n_locked:
        print("   Max displacement, LOCKED     : {:.6f} mm".format(
            float(disp[locked_mask].max())))

    if n_free:
        print("   Max displacement, free       : {:.4f} mm  "
              "(staircase removed)".format(
                  float(disp[~locked_mask].max())))
        print("   Mean displacement, free      : {:.4f} mm".format(
            float(disp[~locked_mask].mean())))

    bb0_min, bb0_max = original.min(axis=0), original.max(axis=0)
    bb1_min, bb1_max = smoothed.min(axis=0), smoothed.max(axis=0)
    span0 = bb0_max - bb0_min
    span1 = bb1_max - bb1_min
    dspan = span1 - span0
    print("   Bounding-box span before (mm): "
          "[{:.3f}, {:.3f}, {:.3f}]".format(*span0))
    print("   Bounding-box span after  (mm): "
          "[{:.3f}, {:.3f}, {:.3f}]".format(*span1))
    print("   Bounding-box delta       (mm): "
          "[{:+.4f}, {:+.4f}, {:+.4f}]".format(*dspan))


# =============================================================================
#   STEP 5 - STL WRITER
# =============================================================================

def compute_normal(v0, v1, v2):
    n = np.cross(v1 - v0, v2 - v0)
    length = np.linalg.norm(n)
    if length < 1e-12:
        return np.array([0.0, 0.0, 1.0])
    return n / length


def write_stl_binary(positions, faces, path):
    print("\n-> Writing binary STL: {}".format(path))
    n_tris = len(faces)
    with open(path, "wb") as f:
        header = "BESO Optimized Structure - beso_stl_generator_v5"
        f.write(header[:80].ljust(80, " ").encode("ascii"))
        f.write(struct.pack("<I", n_tris))
        for (i0, i1, i2) in faces:
            v0, v1, v2 = positions[i0], positions[i1], positions[i2]
            f.write(struct.pack("<fff", *compute_normal(v0, v1, v2)))
            f.write(struct.pack("<fff", *v0))
            f.write(struct.pack("<fff", *v1))
            f.write(struct.pack("<fff", *v2))
            f.write(struct.pack("<H", 0))
    size_kb = os.path.getsize(path) / 1024.0
    print("   Written: {:,} triangles  ({:.1f} KB)".format(n_tris, size_kb))


def write_stl_ascii(positions, faces, path):
    print("\n-> Writing ASCII STL: {}".format(path))
    n_tris = len(faces)
    with open(path, "w") as f:
        f.write("solid BESO_optimized\n")
        for (i0, i1, i2) in faces:
            v0, v1, v2 = positions[i0], positions[i1], positions[i2]
            f.write("  facet normal {:.6e} {:.6e} {:.6e}\n".format(
                *compute_normal(v0, v1, v2)))
            f.write("    outer loop\n")
            f.write("      vertex {:.6e} {:.6e} {:.6e}\n".format(*v0))
            f.write("      vertex {:.6e} {:.6e} {:.6e}\n".format(*v1))
            f.write("      vertex {:.6e} {:.6e} {:.6e}\n".format(*v2))
            f.write("    endloop\n")
            f.write("  endfacet\n")
        f.write("endsolid BESO_optimized\n")
    size_kb = os.path.getsize(path) / 1024.0
    print("   Written: {:,} triangles  ({:.1f} KB)".format(n_tris, size_kb))


# =============================================================================
#   MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Generate a print-ready STL from BESO optimizer CSV output.")

    parser.add_argument("--nodes", default="best_nodes.csv")
    parser.add_argument("--elements", default="best_elements.csv")
    parser.add_argument("--solid", default="best_solid_elements.csv")
    parser.add_argument("--non-design", dest="non_design",
                        default="non_design_elements.csv",
                        help="Elements the optimizer may never remove "
                             "(functional features). Optional.")
    parser.add_argument("--output", default="optimized_structure.stl")

    parser.add_argument("--lock-mode", dest="lock_mode",
                        choices=["original", "all"], default="original",
                        help="original = lock only original-exterior non-design "
                             "faces (cut surfaces are smoothed); "
                             "all = lock every non-design face (v2 behaviour)")

    parser.add_argument("--smooth", action="store_true",
                        help="Enable smoothing")
    parser.add_argument("--smooth-method", dest="smooth_method",
                        choices=["taubin", "hc"], default="taubin")
    parser.add_argument("--smooth-iter", dest="smooth_iter",
                        type=int, default=60)
    parser.add_argument("--smooth-lambda", dest="smooth_lambda",
                        type=float, default=0.5)
    parser.add_argument("--smooth-mu", dest="smooth_mu",
                        type=float, default=-0.53)
    parser.add_argument("--smooth-beta", dest="smooth_beta",
                        type=float, default=0.5)
    parser.add_argument("--no-lock", dest="no_lock", action="store_true",
                        help="Disable non-design locking (smooth everything)")
    parser.add_argument("--rigid-lock", dest="rigid_lock", action="store_true",
                        help="Hard-pin every locked vertex (v3 behaviour). By "
                             "default flat locked faces may slide in-plane to "
                             "remove the staircased lock boundary.")
    parser.add_argument("--slide-cap", dest="slide_cap", type=float, default=1.0,
                        help="Max in-plane slide (mm) for planar locked vertices "
                             "(default: 1.0)")
    parser.add_argument("--lock-normal-cap", dest="normal_cap", type=float,
                        default=0.0,
                        help="Max normal-direction move (mm) for planar locked "
                             "faces. 0 = keep them exactly flat (default). Set "
                             "~0.5 to flatten leftover voxel bumps on locked "
                             "surfaces. Bore and sharp edges are never affected.")
    parser.add_argument("--plane-angle", dest="plane_angle", type=float,
                        default=PLANAR_ANGLE_DEG,
                        help="Max normal spread (deg) for a locked vertex to "
                             "count as planar/slideable (default: 15)")
    parser.add_argument("--ascii", action="store_true",
                        help="Write ASCII STL (default: binary)")

    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("   BESO STL GENERATOR  v5")
    print("=" * 60)
    t0 = time.time()

    for label, path in [("Nodes", args.nodes),
                        ("Elements", args.elements),
                        ("Solid", args.solid)]:
        if not os.path.exists(path):
            print("\n[ERROR] {} file not found: {}".format(label, path))
            sys.exit(1)

    # --- Load ---
    print("\n[1/5] Loading geometry data...")
    nodes = load_nodes(args.nodes)
    elements = load_elements(args.elements)
    solid = load_id_set(args.solid)
    print("   Solid element count: {:,}".format(len(solid)))

    non_design = set()
    if args.no_lock:
        print("   Non-design locking DISABLED by --no-lock.")
    elif os.path.exists(args.non_design):
        non_design = load_id_set(args.non_design)
        print("   Non-design (locked) element count: {:,}".format(len(non_design)))
    else:
        print("   [WARNING] Non-design file not found: {}".format(args.non_design))
        print("             Proceeding with NOTHING locked; functional features "
              "will be smoothed too.")

    missing = solid - set(elements.keys())
    if missing:
        print("   [WARNING] {:,} solid element IDs not in elements file "
              "(ignored).".format(len(missing)))
    solid = solid & set(elements.keys())
    non_design = non_design & solid

    # Effective lock mode: --no-lock or an empty non-design set means lock nothing.
    if args.no_lock or not non_design:
        lock_mode = "none"
    else:
        lock_mode = args.lock_mode
    print("   Lock mode: {}".format(lock_mode))

    # Integer base for face keys (must exceed every node id).
    base = (max(nodes) + 1) if nodes else 1

    # The cut-face index is only needed for the "original" lock mode.
    void_faces = None
    if lock_mode == "original":
        void_faces = build_void_face_set(elements, solid, base)

    # --- Boundary ---
    print("\n[2/5] Extracting boundary faces...")
    boundary = extract_boundary_faces(elements, solid)
    if not boundary:
        print("\n[ERROR] No boundary faces found. Check that solid element IDs "
              "match the elements file.")
        sys.exit(1)

    # --- Surface mesh ---
    print("\n[3/5] Building surface mesh...")
    if args.plane_angle != PLANAR_ANGLE_DEG:
        print("   Planar angle threshold: {:.1f} deg".format(args.plane_angle))
    positions, faces, locked_mask, _, lock_kind, constraint_normal = \
        build_surface_mesh(
            boundary, elements, nodes, non_design,
            void_faces=void_faces, lock_mode=lock_mode, base=base,
            plane_angle_deg=args.plane_angle)
    original_positions = positions.copy()

    # --- Smoothing ---
    if args.smooth:
        print("\n[4/5] Smoothing...")
        positions = smooth_surface(
            positions, faces, locked_mask,
            method=args.smooth_method,
            iterations=args.smooth_iter,
            lam=args.smooth_lambda,
            mu=args.smooth_mu,
            beta=args.smooth_beta,
            lock=not args.no_lock,
            lock_kind=lock_kind,
            constraint_normal=constraint_normal,
            rigid_lock=args.rigid_lock,
            slide_cap=args.slide_cap,
            normal_cap=args.normal_cap)
        report_fidelity(original_positions, positions, locked_mask,
                        lock_kind=(None if args.rigid_lock else lock_kind),
                        constraint_normal=constraint_normal)
    else:
        print("\n[4/5] Smoothing skipped (use --smooth to enable).")

    # --- Write ---
    print("\n[5/5] Writing STL...")
    if args.ascii:
        write_stl_ascii(positions, faces, args.output)
    else:
        write_stl_binary(positions, faces, args.output)

    print("\n" + "=" * 60)
    print("   DONE in {:.1f} seconds".format(time.time() - t0))
    print("   Output: {}".format(os.path.abspath(args.output)))
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
