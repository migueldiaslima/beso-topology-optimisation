# =============================================================================
#   BESO GEOMETRY DUMPER  (runs inside Abaqus Python environment)
#   Drop this file next to your optimizer script and import it.
#   It writes three files whenever a new best iteration is found:
#       best_nodes.csv          -> node_id, x, y, z
#       best_elements.csv       -> element_id, element_type, n1, n2, ...
#       best_solid_elements.csv -> element_id  (one per line)
# =============================================================================

import os
import csv
import sys

# Python 2/3 compatibility helper for opening CSV files
def _csv_open(path):
    if sys.version_info[0] >= 3:
        return open(path, 'w', newline='')
    return open(path, 'wb')


# ---------------------------------------------------------------------------
# 1.  CONNECTIVITY EXTRACTOR
#     Call once at pre-flight, alongside node_coords extraction.
#     Returns a dict: { element_id : (type_string, [node1, node2, ...]) }
# ---------------------------------------------------------------------------
def extract_element_connectivity(odb):
    """
    Reads element type and node connectivity for every element in the first
    instance of the ODB root assembly.

    Parameters
    ----------
    odb : Abaqus Odb object  (already opened by the caller)

    Returns
    -------
    connectivity : dict  { int element_id : (str type, list[int] node_ids) }
    """
    connectivity = {}
    # list() ensures indexing works on both Python 2 and 3
    instance = list(odb.rootAssembly.instances.values())[0]

    for elem in instance.elements:
        connectivity[elem.label] = (str(elem.type), list(elem.connectivity))

    print("   [Dump] Connectivity captured: {} elements.".format(len(connectivity)))
    return connectivity


# ---------------------------------------------------------------------------
# 2.  BEST-ITERATION WRITER
#     Call this inside your  if current_specific_stiffness > local_best_stiffness
#     block, passing the current in-memory state.
# ---------------------------------------------------------------------------
def dump_best_geometry(node_coords, connectivity, solid_list,
                       output_dir=".", iteration=None, print_dir=None):
    """
    Writes the three geometry files for the current best iteration.
    Overwrites any previous best automatically.

    Parameters
    ----------
    node_coords  : dict  { int node_id : (x, y, z) }   (from your optimizer)
    connectivity : dict  { int elem_id : (type_str, [node_ids]) }
    solid_list   : list  of int element IDs that are solid this iteration
    output_dir   : str   folder to write files into  (default: current dir)
    iteration    : int   optional, just used in the printed log message
    print_dir    : str   optional, e.g. "+Z", just used in the log message
    """

    label = ""
    if iteration is not None:
        label += "  Iteration {}".format(iteration)
    if print_dir is not None:
        label += "  Direction {}".format(print_dir)

    nodes_path    = os.path.join(output_dir, "best_nodes.csv")
    elems_path    = os.path.join(output_dir, "best_elements.csv")
    solid_path    = os.path.join(output_dir, "best_solid_elements.csv")

    # --- 2a. Write nodes ---
    with _csv_open(nodes_path) as f:
        writer = csv.writer(f)
        writer.writerow(["node_id", "x", "y", "z"])
        for nid, coords in node_coords.items():
            writer.writerow([nid, coords[0], coords[1], coords[2]])

    # --- 2b. Write elements (variable-length connectivity columns) ---
    with _csv_open(elems_path) as f:
        writer = csv.writer(f)
        writer.writerow(["element_id", "element_type", "nodes..."])
        for eid, (etype, nodes) in connectivity.items():
            writer.writerow([eid, etype] + nodes)

    # --- 2c. Write solid element list ---
    with _csv_open(solid_path) as f:
        writer = csv.writer(f)
        writer.writerow(["element_id"])
        for eid in solid_list:
            writer.writerow([eid])

    print("   [Dump] Best geometry saved ({}).".format(label.strip()))
    print("          Nodes:    {}".format(nodes_path))
    print("          Elements: {}".format(elems_path))
    print("          Solid:    {}".format(solid_path))
