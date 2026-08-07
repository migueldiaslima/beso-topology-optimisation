# BESO Topology Optimisation Pipeline

A bi-directional evolutionary structural optimisation (BESO) framework and its
post-processing pipeline, developed for an MSc dissertation in Mechanical
Engineering (Aeronautic and Vehicle Structures) at the Faculty of Engineering
of the University of Porto (FEUP). The framework is a general solid-void and
lattice topology optimiser; in the dissertation it is applied to a suspension
rocker of the Adamastor Furia hypercar, redesigned in AlSi10Mg for selective
laser melting.

> **Thesis:** *Topology Structural Optimisation of Components for the Automotive
> Industry: Focusing on Hypercar Design and Additive Manufacturing*,
> Miguel Dias de Lima, FEUP, 2026.

## The scripts

The pipeline is made of six scripts, listed in the order in which a run passes
through them. All are in [`src/`](src/).

| Script | Role |
| --- | --- |
| `beso_main.py` | Solid-void optimiser driver: runs inside the Abaqus Python environment and carries the BESO iteration loop (solve, read fields, evaluate specific stiffness, filter sensitivities, update the solid set towards the target volume fraction), with checkpoint/resume and staged continuation. |
| `beso_geometry_dump.py` | Helper called by the driver: exports the node coordinates, element connectivity and solid-element list of the best iteration to CSV. |
| `beso_stl_generator.py` | Solid-void exporter: turns the exported geometry into a watertight STL surface. |
| `beso_lattice_main.py` | Lattice counterpart of the driver: the same loop applied to a graded density field, using homogenised Gibson-Ashby properties of the chosen TPMS morphology. |
| `beso_lattice_stl_generator.py` | Lattice exporter: converts the optimised density field into a graded TPMS lattice STL, applying the resolution law and surface smoothing. |
| `beso_launcher.py` | Local web-based launcher: configures, starts and monitors runs from a browser and compares run histories. See the disclosure below. |

> **All six scripts must sit in the same folder.** The launcher resolves the
> optimiser drivers and the STL exporters relative to its own location and runs
> them there, so a split installation will stop with an explicit error rather
> than fail obscurely.

## Disclosure on the launcher

The browser interface of `beso_launcher.py` (its HTML, CSS and client-side
JavaScript) was developed with the assistance of large language models. The
optimiser, the exporters and the numerical pipeline they drive are the author's
own work; the launcher places a convenience front-end over that pipeline and
introduces no new method. It communicates with the other scripts through a
shared configuration file and their output logs, launches them as subprocesses,
and reports the state of each run back to the interface, so that the whole
sequence can be driven without editing code.

## Requirements

- A working **Abaqus** installation, with the `abaqus` command available on the
  system `PATH`.
- The optimiser drivers (`beso_main.py`, `beso_lattice_main.py`) and
  `beso_geometry_dump.py` run inside the **Abaqus** Python kernel (Python 2.7)
  and use the `numpy`, `scipy` and `matplotlib` shipped with it.
- The launcher runs under **Python 3** using only the standard library.
- `beso_stl_generator.py` runs under **Python 3** and needs `numpy`, and
  `scipy` when smoothing is enabled.
- `beso_lattice_stl_generator.py` runs under **Python 3** and needs `numpy`,
  `pandas`, `scipy`, `scikit-image`, `fast-simplification` and `psutil`.

```bash
pip install numpy scipy pandas scikit-image fast-simplification psutil
```

## Usage

1. Prepare the Abaqus model (`.inp`) for the component and load cases. Put the
   elements the optimiser must never remove into an element set named
   `NON_DESIGN_SET`.
2. Start the launcher with `python beso_launcher.py`. It serves on
   `http://localhost:8087` and opens a browser automatically.
3. Point it at your `.inp` file. It can live anywhere on disk and be called
   anything; the file is copied into the run folder before solving and is never
   modified.
4. Configure the run (target volume fraction, filter radius, load case weights,
   and so on) and start it. Progress is streamed back to the interface.
5. When the run completes, generate the STL from the **STL** tab.

Details of the method and of every parameter are given in the dissertation, and
every field in the launcher carries a `?` tooltip explaining what it does.

### What a run produces

A run writes everything into a single folder next to the scripts,
`Latest_Classic_Run/` for the solid-void engine or `Latest_Lattice_Run/` for the
lattice engine:

| Folder | Contents |
| --- | --- |
| `INP_Files/` | The archived copy of your base model, plus every iteration's input deck. |
| `ODB_Files/` | Every iteration's Abaqus output database. |
| `Data_Files/` | Convergence history, run log, checkpoints, the geometry CSVs for the exporter, and `base_model.json` recording which model the folder was built from. |
| `Misc_Abaqus_Files/` | Solver by-products (`.dat`, `.msg`, `.sta`, and so on). |
| `Scratch_Temp/` | Abaqus scratch space. |

The geometry CSVs the STL exporter reads are written to `Data_Files/` (the best
stiffness iteration) and to `Data_Files/geom_final/` (the final iteration).
Select either in the STL tab.

### Resuming an interrupted run

The solid-void engine checkpoints every iteration and can pick a run back up, or
continue a finished one down to a lower volume fraction. Neither is exposed in
the launcher yet; both are set by hand in `beso_config.json` next to the
scripts:

```jsonc
{
  "resume": true                              // continue an interrupted run
}
```

```jsonc
{
  "continue": true,                           // extend a finished run
  "continue_target_volume_fraction": 0.30
}
```

`resume` and `continue` are mutually exclusive. Because the run folder keeps an
archived copy of the base model, a resume works even if the original model has
since been moved, renamed or deleted. If the model you launch with does not
match the one the folder was built from, the run stops rather than mixing two
different meshes.

The lattice engine has no resume or continue: an interrupted lattice run must be
restarted.

## Known limitations

These are current behaviours worth knowing before you start a long run.

- **The run folder is reused, not cleared.** Launching a second run writes into
  the same `Latest_Classic_Run/` folder. Iteration files from a previous, longer
  run are not removed, so a folder can end up holding artefacts from two
  different optimisations. Rename or move the folder between runs if you want to
  keep a result.
- **The solid-void STL exporter handles 3D solid elements only:** hexahedra
  (`C3D8*`, `C3D20*`), tetrahedra (`C3D4*`, `C3D10*`) and wedges (`C3D6*`,
  `C3D15*`). A 2D or shell mesh optimises correctly but cannot be exported to
  STL, and the launcher warns about this when the model is inspected.
- **Non-design protection during STL export needs
  `non_design_elements.csv`**, which the optimiser writes into `Data_Files/`.
  Without it the functional features are smoothed like the rest of the part.
- **Two models whose names differ only by punctuation** (for example
  `rocker.v3.inp` and `rocker_v3.inp`) produce the same internal job name, so
  keep them in separate folders.
- Only one run at a time. The launcher detects an optimisation that is already
  in progress and will not start a second.

## Data and intellectual property

This repository contains the **optimisation and post-processing code only**.
Component-specific data (CAD geometry, meshes, load histories) belonging to
Adamastor are **not** included.

## Licence

Released under the MIT Licence (see [`LICENSE`](LICENSE)).

## Citation

If you use this code, please cite the dissertation:

```bibtex
@mastersthesis{lima2026beso,
  author = {Miguel Dias de Lima},
  title  = {Topology Structural Optimisation of Components for the Automotive
            Industry: Focusing on Hypercar Design and Additive Manufacturing},
  school = {Faculdade de Engenharia da Universidade do Porto (FEUP)},
  year   = {2026}
}
```

## Author and supervision

- **Author:** Miguel Dias de Lima
- **Supervisor:** Dr. Ana Isabel Lopes Pais
- **Co-supervisor:** Dr. Pedro Jose Silva Campos
- **Supervisor at Adamastor:** Frederico Tome Ribeiro
