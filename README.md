# BESO Topology Optimisation Pipeline

A bi-directional evolutionary structural optimisation (BESO) framework and its
post-processing pipeline, developed for an MSc dissertation in Mechanical
Engineering (Aeronautic and Vehicle Structures) at the Faculty of Engineering
of the University of Porto (FEUP). The framework is a general solid–void and
lattice topology optimiser; in the dissertation it is applied to a suspension
rocker of the Adamastor Furia hypercar, redesigned in AlSi10Mg for selective
laser melting.

> **Thesis:** *Topology Structural Optimisation of Components for the Automotive
> Industry: Focusing on Hypercar Design and Additive Manufacturing*,
> Miguel Dias de Lima, FEUP, 2026.
> <!-- TODO: add a link to the published/deposited dissertation once available -->

## The scripts

The pipeline is made of six scripts, listed in the order in which a run passes
through them. All are in [`src/`](src/).

| Script | Role |
| --- | --- |
| `beso_main.py` | Solid–void optimiser driver: runs inside the Abaqus Python environment and carries the BESO iteration loop (solve, read fields, evaluate specific stiffness, filter sensitivities, update the solid set towards the target volume fraction), with checkpoint/resume and staged continuation. |
| `beso_geometry_dump.py` | Helper called by the driver: exports the node coordinates, element connectivity and solid-element list of the best iteration to CSV. |
| `beso_stl_generator.py` | Solid–void exporter: turns the exported geometry into a watertight STL surface. |
| `beso_lattice_main.py` | Lattice counterpart of the driver: the same loop applied to a graded density field, using homogenised Gibson–Ashby properties of the chosen TPMS morphology. |
| `beso_lattice_stl_generator.py` | Lattice exporter: converts the optimised density field into a graded TPMS lattice STL, applying the resolution law and surface smoothing. |
| `beso_launcher.py` | Local web-based launcher: configures, starts and monitors runs from a browser and compares run histories. See the disclosure below. |

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

- The optimiser drivers (`beso_main.py`, `beso_lattice_main.py`) and
  `beso_geometry_dump.py` run inside the **Abaqus** Python kernel (Python 2.7).
- The launcher and the STL exporters run under **Python 3** and use
  `numpy` and `scipy`.
- A working **Abaqus** installation is required for the analyses.

## Usage (outline)

1. Prepare the Abaqus model (`.inp`/`.cae`) for the component and load cases.
2. Start the launcher with `python beso_launcher.py` and open the address it
   prints in a browser.
3. Configure the run (target volume fraction, filter radius, load cases, and so
   on), start it, and monitor progress from the interface.
4. When the run completes, generate the STL from the same interface.

Details of the method and of every parameter are given in the dissertation.

## Data and intellectual property

This repository contains the **optimisation and post-processing code only**.
Component-specific data (CAD geometry, meshes, load histories) belonging to
Adamastor are **not** included. Before making anything public, confirm with your
supervisor and with Adamastor what may be released.

## Licence

Released under the MIT Licence (see [`LICENSE`](LICENSE)). If a different
arrangement is required by FEUP or Adamastor, replace this file accordingly.

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
- **Co-supervisor:** Dr. Pedro José Silva Campos
- **Supervisor at Adamastor:** Frederico Tomé Ribeiro
