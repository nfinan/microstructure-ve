import pathlib
import shutil
import subprocess
from functools import partial, cache
from os import PathLike
from typing import Optional, Sequence, List, Union, TextIO, Iterable

import numpy as np
from dataclasses import dataclass

BASE_PATH = pathlib.Path(__file__).parent


###################
# Keyword Classes #
###################

# Each keyword class represents a specific ABAQUS keyword.
# They know what the structure of the keyword section is and what data
# are needed to fill it out. They should minimize computation outside
# the to_inp method that actually writes directly to the input file.


@dataclass
class Heading:
    text: str = ""

    def to_inp(self, inp_file_obj):
        inp_file_obj.write(
            f"""\
*Heading
{self.text}
"""
        )


# NOTE: every "1 +" you see is correcting the array indexing mismatch between
# python and abaqus (python has zero-indexed, abaqus has one-indexed arrays)
# however "+ 1" actually indicates an extra step


@dataclass
class GridNodes:
    shape: np.ndarray
    scale: float

    @classmethod
    def from_intph_img(cls, intph_img, scale):
        nodes_shape = np.array(intph_img.shape) + 1
        return cls(nodes_shape, scale)

    def __post_init__(self):
        self.node_nums = range(1, 1 + np.prod(self.shape))  # 1-indexing for ABAQUS
        self.virtual_node = self.node_nums[-1] + 1

    def to_inp(self, inp_file_obj):
        y_pos, x_pos = self.scale * np.indices(self.shape)
        inp_file_obj.write("*Node\n")
        for node_num, x, y in zip(self.node_nums, x_pos.ravel(), y_pos.ravel()):
            inp_file_obj.write(f"{node_num:d},\t{x:.6e},\t{y:.6e}\n")
        # noinspection PyUnboundLocalVariable
        # quirk: we abuse the loop variables to put another "virtual" node at the corner
        inp_file_obj.write(f"{self.virtual_node:d},\t{x:.6e},\t{y:.6e}\n")


@dataclass
class RectangularElements:
    nodes: GridNodes
    type: str = "CPE4R"

    def __post_init__(self):
        self.element_nums = range(1, 1 + np.prod(self.nodes.shape - 1))

    def to_inp(self, inp_file_obj):
        # strategy: generate one array representing all nodes, then make slices of it
        # that represent offsets to the right, top, and topright nodes to iterate
        all_nodes = 1 + np.ravel_multi_index(
            np.indices(self.nodes.shape), self.nodes.shape
        )
        # elements are defined counterclockwise
        right_nodes = all_nodes[:-1, 1:].ravel()
        key_nodes = all_nodes[:-1, :-1].ravel()
        top_nodes = all_nodes[1:, :-1].ravel()
        topright_nodes = all_nodes[1:, 1:].ravel()
        inp_file_obj.write(f"*Element, type={self.type}\n")
        for elem_num, tn, kn, rn, trn in zip(
            self.element_nums, top_nodes, key_nodes, right_nodes, topright_nodes
        ):
            inp_file_obj.write(
                f"{elem_num:d},\t{tn:d},\t{kn:d},\t{rn:d},\t{trn:d},\t\n"
            )


# "top" is image rather than matrix convention
sides = {
    "LeftSurface": np.s_[:, 0],
    "RightSurface": np.s_[:, -1],
    "BotmSurface": np.s_[0, 1:-1],
    "TopSurface": np.s_[-1, 1:-1],
    "BotmLeft": np.s_[0, 0],
    "TopLeft": np.s_[-1, 0],
    "BotmRight": np.s_[0, -1],
    "TopRight": np.s_[-1, -1],
}


@dataclass(eq=False)
class NodeSet:
    name: str
    node_inds: Union[np.ndarray, List[int]]

    @classmethod
    def from_side_name(cls, name, nodes):
        sl = sides[name]
        row_ind, col_ind = np.indices(nodes.shape)
        node_inds = 1 + np.ravel_multi_index(
            (row_ind[sl].ravel(), col_ind[sl].ravel()),
            dims=nodes.shape,
        )
        return cls(name, node_inds)

    def __str__(self):
        return self.name

    def to_inp(self, inp_file_obj):
        inp_file_obj.write(f"*Nset, nset={self.name}\n")
        for i in self.node_inds:
            inp_file_obj.write(f"{i:d}\n")


@dataclass
class EqualityEquation:
    nsets: Sequence[Union[NodeSet, int]]
    dof: int

    def to_inp(self, inp_file_obj):
        inp_file_obj.write(
            f"""\
*Equation
2
{self.nsets[0]}, {self.dof}, 1.
{self.nsets[1]}, {self.dof}, -1.
"""
        )


@dataclass
class DriveEquation(EqualityEquation):
    drive_node: Union[NodeSet, int]

    def to_inp(self, inp_file_obj):
        inp_file_obj.write(
            f"""\
*Equation
3
{self.nsets[0]}, {self.dof}, 1.
{self.nsets[1]}, {self.dof}, -1.
{self.drive_node}, {self.dof}, 1.
"""
        )


@dataclass
class ElementSet:
    matl_code: int
    elements: np.ndarray

    @classmethod
    def from_intph_image(cls, intph_img):
        """Produce a list of ElementSets corresponding to unique pixel values.

        Materials are ordered by distance from filler
        i.e. [filler, interphase, matrix]
        """
        intph_img = intph_img.ravel()
        uniq = np.unique(intph_img)  # sorted!
        indices = np.arange(1, 1 + intph_img.size)

        return [cls(matl_code, indices[intph_img == matl_code]) for matl_code in uniq]

    def to_inp(self, inp_file_obj):
        inp_file_obj.write(f"*Elset, elset=SET-{self.matl_code:d}\n")
        for element in self.elements:
            inp_file_obj.write(f"{element:d}\n")


#################
# Combo classes #
#################

# These represent the structure of several keywords that need to be
# ordered or depend on each other's information somehow. They create a graph
# of information for a complete conceptual component of the input file.


class BoundaryConditions:
    def to_inp(self, inp_file_obj):
        pass


@dataclass
class DisplacementBoundaryCondition(BoundaryConditions):
    nset: Union[NodeSet, int]
    first_dof: int
    last_dof: int
    displacement: float

    def to_inp(self, inp_file_obj):
        inp_file_obj.write(
            f"""\
*Boundary, type=displacement
{self.nset}, {self.first_dof}, {self.last_dof}, {self.displacement}
"""
        )


@dataclass
class Material:
    elset: ElementSet
    density: float  # kg/micron^3
    poisson: float
    youngs: float  # MPa, long term, low freq modulus

    def to_inp(self, inp_file_obj):
        self.elset.to_inp(inp_file_obj)
        mc = self.elset.matl_code
        inp_file_obj.write(
            f"""\
*Solid Section, elset=SET-{mc:d}, material=MAT-{mc:d}
1.
*Material, name=MAT-{mc:d}
*Density
{self.density:.6e}
*Elastic
{self.youngs:.6e}, {self.poisson:.6e}
"""
        )


@dataclass
class ViscoelasticMaterial(Material):
    freq: np.ndarray  # excitation freq in Hz
    youngs_cplx: np.ndarray  # complex youngs modulus
    shift: float = 0.0  # frequency shift induced relative to nominal properties
    left_broadening: float = 1.0  # 1 is no broadening
    right_broadening: float = 1.0  # 1 is no broadening

    def apply_shift(self):
        """Apply shift and broadening factors to frequency.

        left and right refer to frequencies below and above tand peak"""
        freq = np.log10(self.freq) - self.shift

        # shift relative to tand peak
        i = np.argmax(self.youngs_cplx.imag / self.youngs_cplx.real)
        f = freq[i]

        freq[:i] = self.left_broadening * (freq[:i] - f) + f
        freq[i:] = self.right_broadening * (freq[i:] - f) + f
        return 10**freq

    def normalize_modulus(self):
        """Convert to abaqus's preferred normalized moduli"""
        # Only works with frequency-dependent poisson's ratio
        shear_cplx = self.youngs_cplx / (2 * (1 + self.poisson))
        bulk_cplx = self.youngs_cplx / (3 * (1 - 2 * self.poisson))

        # special normalized shear modulus used by abaqus
        wgstar = np.empty_like(shear_cplx)
        shear_inf = shear_cplx[0].real
        wgstar.real = shear_cplx.imag / shear_inf
        wgstar.imag = 1 - shear_cplx.real / shear_inf

        # special normalized bulk modulus used by abaqus
        wkstar = np.empty_like(shear_cplx)
        bulk_inf = bulk_cplx[0].real
        wkstar.real = bulk_cplx.imag / bulk_inf
        wkstar.imag = 1 - bulk_cplx.real / bulk_inf

        return wgstar, wkstar

    def to_inp(self, inp_file_obj):
        super().to_inp(inp_file_obj)
        inp_file_obj.write("*Viscoelastic, frequency=TABULAR\n")

        # special normalized bulk modulus used by abaqus
        # if poisson's ratio is frequency-independent, it drops out
        # and youngs=shear=bulk when normalized
        youngs_inf = self.youngs_cplx[0].real
        real = (self.youngs_cplx.imag / youngs_inf).tolist()
        imag = (1 - self.youngs_cplx.real / youngs_inf).tolist()
        freq = self.apply_shift().tolist()

        for wgr, wgi, wkr, wki, f in zip(real, imag, real, imag, freq):
            inp_file_obj.write(f"{wgr:.6e}, {wgi:.6e}, {wkr:.6e}, {wki:.6e}, {f:.6e}\n")


@dataclass
class PeriodicBoundaryCondition(DisplacementBoundaryCondition):
    nodes: GridNodes

    def __post_init__(self):
        make_set = partial(NodeSet.from_side_name, nodes=self.nodes)
        ndim = len(self.nodes.shape)
        self.driven_nset = make_set("RightSurface")
        self.node_pairs: List[List[NodeSet]] = [
            [make_set("LeftSurface"), self.driven_nset],
            [make_set("BotmSurface"), make_set("TopSurface")],
            [make_set("BotmRight"), make_set("TopRight")],
        ]
        # Displacement at any surface node is equal to the opposing surface
        # node in both degrees of freedom unless one of the surfaces is a driver.
        # in that case, add the avg displacement from the drive node
        self.eq_pairs: List[List[EqualityEquation]] = [
            [EqualityEquation(p, x + 1) for x in range(ndim)]
            if (self.driven_nset not in p)
            else [
                DriveEquation(p, x + 1, drive_node=self.nset)
                if x in range(self.first_dof - 1, self.last_dof)
                else EqualityEquation(p, x + 1)
                for x in range(ndim)
            ]
            for p in self.node_pairs
        ]

    def to_inp(self, inp_file_obj):
        for node_pair, eq_pair in zip(self.node_pairs, self.eq_pairs):
            node_pair[0].to_inp(inp_file_obj)
            node_pair[1].to_inp(inp_file_obj)
            eq_pair[0].to_inp(inp_file_obj)
            eq_pair[1].to_inp(inp_file_obj)
        super().to_inp(inp_file_obj)


@dataclass
class Static:
    """Data for an ABAQUS STATIC subsection of STEP"""

    long_term: bool = False

    def to_inp(self, inp_file_obj):
        inp_file_obj.write(
            f"""\
*STATIC{", LONG TERM" if self.long_term else ""}
"""
        )


@dataclass
class Dynamic:
    """Data for an ABAQUS STEADY STATE DYNAMICS subsection of STEP"""

    f_initial: float
    f_final: float
    f_count: int
    bias: int

    def to_inp(self, inp_file_obj):
        inp_file_obj.write(
            f"""\
*STEADY STATE DYNAMICS, DIRECT
{self.f_initial}, {self.f_final}, {self.f_count}, {self.bias}
"""
        )


@dataclass
class Step:
    subsections: Iterable
    perturbation: bool = False

    def to_inp(self, inp_file_obj):
        inp_file_obj.write(
            f"""\
*STEP{",PERTURBATION" if self.perturbation else ""}
"""
        )
        for n in self.subsections:
            n.to_inp(inp_file_obj)
        inp_file_obj.write(
            f"""\
*END STEP
"""
        )


@dataclass
class Model:
    nodes: GridNodes
    elements: RectangularElements
    materials: Iterable[Material]
    bcs: Iterable[BoundaryConditions] = ()
    nsets: Iterable[NodeSet] = ()

    def to_inp(self, inp_file_obj):
        self.nodes.to_inp(inp_file_obj)
        for nset in self.nsets:
            nset.to_inp(inp_file_obj)
        self.elements.to_inp(inp_file_obj)
        for m in self.materials:
            m.to_inp(inp_file_obj)
        for bc in self.bcs:
            bc.to_inp(inp_file_obj)


@dataclass
class Simulation:
    model: Model
    heading: Optional[Heading] = None
    steps: Iterable[Step] = ()

    def to_inp(self, inp_file_obj: TextIO):
        if self.heading is not None:
            self.heading.to_inp(inp_file_obj)
        self.model.to_inp(inp_file_obj)
        for step in self.steps:
            step.to_inp(inp_file_obj)


####################
# Helper functions #
####################

# High level functions representing important transformations or steps.
# Probably the most important part is the name and docstring, to explain
# WHY a certain procedure is being taken/option being input.


def in_sorted(arr, val):
    """Determine if val is contained in arr, assuming arr is sorted"""
    index = np.searchsorted(arr, val)
    if index < len(arr):
        return val == arr[index]
    else:
        return False


def load_matlab_microstructure(matfile, var_name):
    """Load the microstructure in .mat file into a 2D boolean ndarray.
    @para: matfile --> the file name of the microstructure
           var_name --> the name of the variable in the .mat file
                        that contains the 2D microstructure 0-1 matrix.
    @return: 2D ndarray dtype=bool
    """
    from scipy.io import loadmat

    return loadmat(matfile, matlab_compatible=True)[var_name]


def assign_intph(microstructure: np.ndarray, num_layers_list: List[int]) -> np.ndarray:
    """Generate interphase layers around the particles.

    Microstructure must have at least one zero value.

    :rtype: numpy.ndarray
    :param microstructure: The microstructure array. Particles must be zero,
        matrix must be nonzero.
    :type microstructure: numpy.ndarray

    :param num_layers_list: The list of interphase thickness in pixels. The order of
        the layer values is based on the sorted distances in num_layers_list from
        the particles (near particles -> far from particles)
    :type num_layers_list: List(int)
    """
    from scipy.ndimage import distance_transform_edt

    dists = distance_transform_edt(microstructure)
    intph_img = (dists != 0).view("u1")
    for num_layers in sorted(num_layers_list):
        intph_img += dists > num_layers
    return intph_img


def periodic_assign_intph(
    microstructure: np.ndarray, num_layers_list: List[int]
) -> np.ndarray:
    """Generate interphase layers around the particles with periodic BC.

    Microstructure must have at least one zero value.

    :rtype: numpy.ndarray
    :param microstructure: The microstructure array. Particles must be zero,
        matrix must be nonzero.
    :type microstructure: numpy.ndarray

    :param num_layers_list: The list of interphase thickness in pixels. The order of
        the layer values is based on the sorted distances in num_layers_list from
        the particles (near particles -> far from particles)
    :type num_layers_list: List(int)
    """
    tiled = np.tile(microstructure, (3, 3))
    dimx, dimy = microstructure.shape
    intph_tiled = assign_intph(tiled, num_layers_list)
    # trim tiling
    intph = intph_tiled[dimx : dimx + dimx, dimy : dimy + dimy]
    # free intph's view on intph_tiled's memory
    intph = intph.copy()
    return intph


def load_viscoelasticity(matrl_name):
    """load VE data from a text file according to ABAQUS requirements

    mainly the frequency array needs to be strictly increasing, but also having
    the storage/loss data in complex numbers helps our calculations.
    """
    freq, youngs_real, youngs_imag = np.loadtxt(matrl_name, unpack=True)
    youngs = np.empty_like(youngs_real, dtype=complex)
    youngs.real = youngs_real
    youngs.imag = youngs_imag
    sortind = np.argsort(freq)
    return freq[sortind], youngs[sortind]


@cache
def find_command(command: str) -> Optional[PathLike]:
    x = shutil.which(command)
    if x is None:
        # maybe it's a shell alias?
        if shutil.which("bash") is None:
            return None
        p = subprocess.run(
            ["bash", "-i", "-c", f"alias {command}"],
            capture_output=True,
        )
        if p.returncode:
            return None
        x = p.stdout.split(b"'")[1].decode()
    try:
        return pathlib.Path(x).resolve(strict=True)
    except FileNotFoundError:
        return None


def run_job(job_name, cpus):
    """feed .inp file to ABAQUS and wait for the result"""
    subprocess.run(
        [
            find_command("abaqus"),
            "job=" + job_name,
            "cpus=" + str(cpus),
            "interactive",
        ],
        check=True,
    )


def read_odb(job_name, drive_nset):
    """Extract viscoelastic response from abaqus output ODB

    Uses abaqus python api which is stuck in python 2.7 ancient history,
    so we need to farm it out to a subprocess.
    """
    subprocess.run(
        [
            find_command("abaqus"),
            "python",
            BASE_PATH / "readODB.py",
            job_name,
            drive_nset.name,
        ],
        check=True,
    )
