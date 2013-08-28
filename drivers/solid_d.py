import os
import re
import sys
import numpy as np
from numpy.linalg import solve, lstsq

from __config__ import cfg
import utils.tensor as tensor
import utils.xmltools as xmltools
from drivers.driver import Driver
from core.kinematics import deps2d, sig2d, update_deformation
from utils.tensor import NSYMM, NTENS, NVEC, I9
from utils.opthold import OptionHolder
from core.io import Error1, log_message
from materials.material import create_material

np.set_printoptions(precision=4)

class SolidDriver(Driver):
    name = "solid"
    path = []
    proportional = 0
    kappa = 0.
    def __init__(self):
        pass

    def setup(self, runid, material, *opts):
        """Setup the driver object

        """
        self.runid = runid
        self.mtlmdl = create_material(material[0])
        self.density = opts[0]

        # register variables
        self.register_glob_variable("TIME_STEP")
        self.register_glob_variable("STEP_NUM")
        self.register_glob_variable("LEG_NUM")
        self.register_variable("STRESS", vtype="SYMTENS")
        self.register_variable("STRAIN", vtype="SYMTENS")
        self.register_variable("DEFGRAD", vtype="TENS")
        self.register_variable("SYMM_L", vtype="SYMTENS")
        self.register_variable("EFIELD", vtype="VECTOR")
        self.register_variable("EQSTRAIN", vtype="SCALAR")
        self.register_variable("VSTRAIN", vtype="SCALAR")
        self.register_variable("DENSITY", vtype="SCALAR")
        self.register_variable("PRESSURE", vtype="SCALAR")
        self.register_variable("DSTRESS", vtype="SYMTENS")

        # Setup
        self.mtlmdl.setup(material[1])

        # register material variables
        self.xtra_start = self.ndata
        for (var, vtype) in self.mtlmdl.material_variables():
            self.register_variable(var, vtype=vtype)

        nxtra = self.ndata - self.xtra_start
        self.xtra_end = self.xtra_start + nxtra
        setattr(self, "xtra_slice", slice(self.xtra_start, self.xtra_end))

        # allocate storage
        self.allocd()

        # initialize nonzero data
        self._data[self.defgrad_slice] = I9
        self._data[self.density_slice] = self.density

        # initialize material
        sig = np.zeros(6)
        xtra = self.mtlmdl.initial_state()
        args = (I9, np.zeros(3))

        sig, xtra = self.mtlmdl.call_material_zero_state(sig, xtra, *args)

        # -------------------------- quantities derived from final state
        pres = -np.sum(sig[:3]) / 3.

        xtra = self.mtlmdl.adjust_initial_state(xtra)

        self.setvars(stress=sig, pressure=pres, xtra=xtra)

        return

    def process_paths(self, iomgr, *args):
        """Process the deformation path

        Parameters
        ----------

        Returns
        -------

        """
        legs = self.path
        termination_time = args[0]
        if termination_time is None:
            termination_time = legs[-1][0] + 1.e-06

        kappa = self.kappa

        # initial leg
        glob_step_num = 0
        rho = self.elem_var_vals("DENSITY")[0]
        xtra = self.elem_var_vals("XTRA")
        sig = self.elem_var_vals("STRESS")
        tleg = np.zeros(2)
        d = np.zeros(NSYMM)
        dt = 0.
        eps = np.zeros(NSYMM)
        f = np.reshape(np.eye(3), (9, 1))
        depsdt = np.zeros(NSYMM)
        sigdum = np.zeros((2, NSYMM))

        # compute the initial jacobian
        J0 = self.mtlmdl.constant_jacobian()

        # v array is an array of integers that contains the rows and columns of
        # the slice needed in the jacobian subroutine.
        nv = 0
        vdum = np.zeros(6, dtype=np.int)

        # Process each leg
        nlegs = len(legs)
        lsl = len(str(nlegs))
        for leg_num, leg in enumerate(legs):

            tleg[0] = tleg[1]
            sigdum[0] = sig[:]
            if nv:
                sigdum[0, v] = sigspec[1]

            tleg[1], nsteps, control, c, ndumps, ef = leg
            delt = tleg[1] - tleg[0]
            if delt == 0.:
                continue

            # ndumps_per_leg is the number of times to write to the output
            # file in this leg
            dump_interval = max(1, int(float(nsteps / ndumps)))
            lsn = len(str(nsteps))
            consfmt = ("leg {{0:{0}d}}, step {{1:{1}d}}, time {{2:.4E}}, "
                       "dt {{3:.4E}}".format(lsl, lsn))

            nv = 0
            for i, cij in enumerate(c):
                if control[i] == 1:                            # -- strain rate
                    depsdt[i] = cij

                elif control[i] == 2:                          # -- strain
                    depsdt[i] = (cij - eps[i]) / delt

                elif control[i] == 3:                          # -- stress rate
                    sigdum[1, i] = sigdum[0, i] + cij * delt
                    vdum[nv] = i
                    nv += 1

                elif control[i] == 4:                          # -- stress
                    sigdum[1, i] = cij
                    vdum[nv] = i
                    nv += 1

                continue

            sigspec = np.empty((3, nv))
            v = vdum[:nv]
            sigspec[:2] = sigdum[:2, v]
            Jsub = J0[[[x] for x in v], v]

            t = tleg[0]
            dt = delt / nsteps

            if not nv:
                # strain or strain rate prescribed and d is constant over
                # entire leg
                d = deps2d(dt, kappa, eps, depsdt)

                if cfg.sqa and kappa == 0.:
                    if not np.allclose(d, depsdt):
                        log_message("sqa: d != depsdt (k=0, leg"
                                    "={0})".format(leg_num))

            else:
                # Initial guess for d[v]
                try:
                    depsdt[v] = solve(Jsub, (sigspec[1] - sigspec[0]) / delt)
                except:
                    depsdt[v] -= lstsq(Jsub, (sigspec[1] - sigspec[0]) / delt)[0]

            # process this leg
            for n in range(nsteps):

                # increment time
                t += dt

                # interpolate values to the target values for this step
                a1 = float(nsteps - (n + 1)) / nsteps
                a2 = float(n + 1) / nsteps
                sigspec[2] = a1 * sigspec[0] + a2 * sigspec[1]

                # --- find current value of d: sym(velocity gradient)
                if nv:
                    # One or more stresses prescribed
                    # get just the prescribed stress components
                    d = sig2d(self.mtlmdl, dt, depsdt,
                              sig, xtra, v, sigspec[2], self.proportional)

                # compute the current deformation gradient and strain from
                # previous values and the deformation rate
                f, eps = update_deformation(dt, kappa, f, d)

                # update material state
                sigsave = np.array(sig)
                xtrasave = np.array(xtra)
                sig, xtra = self.mtlmdl.update_state(dt, d, sig, xtra, f, ef)

                # -------------------------- quantities derived from final state
                eqeps = np.sqrt(2. / 3. * (np.sum(eps[:3] ** 2)
                                           + 2. * np.sum(eps[3:] ** 2)))
                epsv = np.sum(eps[:3])
                rho = rho * np.exp(-np.sum(d[:3]) * dt)

                pres = -np.sum(sig[:3]) / 3.
                dstress = (sig - sigsave) / dt

                # advance all data after updating state
                glob_step_num += 1
                self.setglobvars(leg_num=leg_num,
                                 step_num=glob_step_num, time_step=dt)

                self.setvars(stress=sig, strain=eps, defgrad=f,
                             symm_l=d, efield=ef, eqstrain=eqeps,
                             vstrain=epsv, density=rho, pressure=pres,
                             dstress=dstress, xtra=xtra)


                # --- write state to file
                endstep = abs(t - tleg[1]) / tleg[1] < 1.E-12
                if (nsteps - n) % dump_interval == 0 or endstep:
                    iomgr(t)

                if n == 0 or round(nsteps / 2.) == n or endstep:
                    log_message(consfmt.format(leg_num, n + 1, t, dt))

                if t > termination_time:
                    return 0

                continue  # continue to next step

            continue # continue to next leg


        return 0

    # --------------------------------------------------------- Parsing methods
    @classmethod
    def parse_and_register_paths(cls, pathlmns, *args):
        """Parse the Path elements of the input file and register the formatted
        paths to the class

        """
        if len(pathlmns) > 1:
            raise Error1("Only 1 Path tag supported for solid driver")
        cls.path, cls.kappa, cls.proportional = cls.pPath(pathlmns[0], *args)
        return 0

    @classmethod
    def pPath(cls, pathlmn, *args):
        """Parse the Path block and set defaults

        """
        functions = args[0]
        ptype = pathlmn.getAttribute("type")
        if not ptype:
            raise Error1("Path 'type' not defined")
        ptype = ptype.strip().lower()
        if ptype not in ("prdef",):
            raise Error1("{0}: unknown Path type")
        pathlmn.removeAttribute("type")

        # Set up options for Path
        options = OptionHolder()
        options.addopt("kappa", 0.)
        options.addopt("amplitude", 1.)
        options.addopt("ratfac", 1.)
        options.addopt("nfac", 1.)
        options.addopt("tstar", 1., test=lambda x: x > 0.)
        options.addopt("estar", 1.)
        options.addopt("sstar", 1.)
        options.addopt("fstar", 1.)
        options.addopt("efstar", 1.)
        options.addopt("dstar", 1.)
        options.addopt("format", "default", dtype=str,
                       choices=("default", "table", "fcnspec"))
        options.addopt("proportional", 0, dtype=mybool)
        options.addopt("ndumps", "20", dtype=str)

        # the following options are for table formatted Path
        options.addopt("tblcols", "1:7", dtype=str)
        options.addopt("tbltfmt", "time", dtype=str, choices=("time", "dt"))
        options.addopt("tblcfmt", "222222", dtype=str)

        # Get control terms
        for i in range(pathlmn.attributes.length):
            options.setopt(*xmltools.get_name_value(pathlmn.attributes.item(i)))

        # Read in the actual Path - splitting them in to lists
        lines = []
        for node in pathlmn.childNodes:
            if node.nodeType == node.COMMENT_NODE:
                continue
            lines.extend([" ".join(xmltools.uni2str(item).split())
                          for item in node.nodeValue.splitlines() if item.split()])
        lines = [xmltools.str2list(line, dtype=str) for line in lines]

        # parse the Path depending on type
        if options.getopt("format") == "default":
            path = cls.parse_path_default(lines)

        elif options.getopt("format") == "table":
            path = cls.parse_path_table(lines, options.getopt("tbltfmt"),
                                        options.getopt("tblcols"),
                                        options.getopt("tblcfmt"))

        elif options.getopt("format") == "fcnspec":
            path = cls.parse_path_cijfcn(lines, functions)

        else:
            raise Error1("Path: {0}: invalid "
                         "format".format(options.getopt("format")))

        # store relevant info to the class
        path = cls.format_path(path, options)
        kappa = options.getopt("kappa")
        proportional = options.getopt("proportional")

        return path, kappa, proportional

    @classmethod
    def parse_path_default(cls, lines):
        """Parse the individual path

        """
        path = []
        final_time = 0.
        leg_num = 1
        for line in lines:
            if not line:
                continue
            termination_time, num_steps, control_hold = line[:3]
            Cij_hold = line[3:]

            # check entries
            # --- termination time
            try:
                termination_time = float(termination_time)
            except ValueError:
                raise Error1("Path: termination time of leg {0} must be a float, "
                             "got {1}".format(leg_num, termination_time))
            if termination_time < 0.:
                raise Error1("Path: termination time {0} of leg {1} must be "
                             "positive".format(termination_time, leg_num))
            elif termination_time < final_time:
                raise Error("Path: time must increase monitonically at leg "
                            "{0}".format(leg_num))
            final_time = termination_time

            # --- number of steps
            try:
                num_steps = int(num_steps)
            except ValueError:
                raise Error1("Path: number of steps of leg {0} must be an integer, "
                             "got {1}".format(leg_num, num_steps))
            if num_steps < 0:
                raise Error1("Path: number of steps {0} of leg {1} must be "
                             "positive".format(num_steps, leg_num))

            # --- control
            control = cls.format_path_control(control_hold, leg_num=leg_num)

            # --- Cij
            Cij = []
            for (i, comp) in enumerate(Cij_hold):
                try:
                    comp = float(comp)
                except ValueError:
                    raise Error1("Path: Component {0} of leg {1} must be a "
                                 "float, got {2}".format(i+1, leg_num, comp))
                Cij.append(comp)

            Cij = np.array(Cij)

            # --- Check lengths of Cij and control are consistent
            if len(Cij) != len(control):
                raise Error1("Path: len(Cij) != len(control) in leg {0}"
                             .format(leg_num))

            path.append([termination_time, num_steps, control, Cij])
            leg_num += 1
            continue

        return path

    @classmethod
    def parse_path_table(cls, lines, tbltfmt, tblcols, tblcfmt):
        """Parse the path table

        """
        path = []
        final_time = 0.
        termination_time = 0.
        leg_num = 1

        # Convert tblcols to a list
        columns = cls.format_tbl_cols(tblcols)

        # check the control
        control = cls.format_path_control(tblcfmt)

        for line in lines:
            if not line:
                continue
            try:
                line = np.array([float(x) for x in line])
            except ValueError:
                raise Error1("Expected floats in leg {0}, got {1}".format(
                    leg_num, line))
            try:
                line = line[columns]
            except IndexError:
                raise Error1("Requested column not found in leg "
                             "{0}".format(leg_num))

            if tbltfmt == "dt":
                termination_time += line[0]
            else:
                termination_time = line[0]

            Cij = line[1:]

            # check entries
            # --- termination time
            if termination_time < 0.:
                raise Error1("Path: termination time {0} of leg {1} must be "
                             "positive".format(termination_time, leg_num))
            elif termination_time < final_time:
                raise Error("Path: time must increase monitonically at leg "
                            "{0}".format(leg_num))
            final_time = termination_time

            # --- number of steps
            num_steps = 1

            # --- Check lengths of Cij and control are consistent
            if len(Cij) != len(control):
                raise Error1("Path: len(Cij) != len(control) in leg {0}"
                             .format(leg_num))

            path.append([termination_time, num_steps, control, Cij])
            leg_num += 1
            continue

        return path

    @classmethod
    def parse_path_cijfcn(cls, lines, functions):
        """Parse the path given by functions

        """
        start_time = 0.
        leg_num = 1

        if not lines:
            raise Error1("No table functions defined")
        elif len(lines) > 1:
            raise Error1("Only one line of table functions allowed, "
                         "got {0}".format(len(lines)))

        termination_time, num_steps, control_hold = lines[0][:3]
        cijfcns = lines[0][3:]

        # check entries
        # --- termination time
        try:
            termination_time = float(termination_time)
        except ValueError:
            raise Error1("Path: termination time must be a float, "
                         "got {0}".format(termination_time))
        if termination_time < 0.:
            raise Error1("Path: termination time {0} must be "
                         "positive".format(termination_time))
        final_time = termination_time

        # --- number of steps
        try:
            num_steps = int(num_steps)
        except ValueError:
            raise Error1("Path: number of steps must be an integer, "
                         "got {0}".format(num_steps))
        if num_steps < 0:
            raise Error1("Path: number of steps {0} must be "
                         "positive".format(num_steps))

        # --- control
        control = cls.format_path_control(control_hold, leg_num=leg_num)

        # --- get the actual functions
        Cij = []
        for icij, cijfcn in enumerate(cijfcns):
            cijfcn = cijfcn.split(":")
            try:
                fid, scale = cijfcn
            except ValueError:
                fid, scale = cijfcn[0], 1
            try:
                fid = int(float(fid))
            except ValueError:
                raise Error1("Function ID must be an integer, got {0}".format(fid))
            try:
                scale = float(scale)
            except ValueError:
                raise Error1("Function scale must be a float, got "
                             "{0}".format(scale))

            fcn = functions.get(fid)
            if fcn is None:
                raise Error1("{0}: function not defined".format(fid))
            Cij.append((scale, fcn))

        # --- Check lengths of Cij and control are consistent
        if len(Cij) != len(control):
            raise Error1("Path: len(Cij) != len(control) in leg {0}"
                         .format(leg_num))

        path = []
        for time in np.linspace(start_time, final_time, num_steps):
            leg = [time, 1, control]
            leg.append(np.array([s * f(time) for (s, f) in Cij]))
            path.append(leg)

        return path

    @staticmethod
    def format_path_control(cfmt, leg_num=None):
        leg = "" if leg_num is None else "(leg {0})".format(leg_num)
        valid_control_flags = [1, 2, 3, 4, 5, 6, 8, 9]
        control = []
        for (i, flag) in enumerate(cfmt):
            try:
                flag = int(flag)
            except ValueError:
                raise Error1("Path: control flag {0} must be an "
                             "integer, got {1} {2}".format(i+1, flag, leg))

            if flag not in valid_control_flags:
                valid = ", ".join(xmltools.stringify(x)
                                  for x in valid_control_flags)
                raise Error1("Path: {0}: invalid control flag choose from "
                             "{1} {2}".format(flag, valid, leg))

            control.append(flag)

        if 5 in control:
            if any(flag != 5 and flag not in (6, 9) for flag in control):
                raise Error1("Path: mixed mode deformation not allowed with "
                             "deformation gradient control {0}".format(leg))

            # must specify all components
            elif len(control) != 9:
                raise Error1("all 9 components of deformation gradient must "
                             "be specified {0}".format(leg))

        if 8 in control:
            # like deformation gradient control, if displacement is specified
            # for one, it must be for all
            if any(flag != 8 and flag not in (6, 9) for flag in control):
                raise Error1("Path: mixed mode deformation not allowed with "
                             "displacement control {0}".format(leg))

            # must specify all components
            elif len(control) != 3:
                raise Error1("all 3 components of displacement must "
                             "be specified {0}".format(leg))

        return np.array(control, dtype=np.int)


    @staticmethod
    def format_tbl_cols(tblcols):
        columns = []
        for item in [x.split(":")
                     for x in xmltools.str2list(
                             re.sub(r"\s*:\s*", ":", tblcols), dtype=str)]:
            try:
                item = [int(x) for x in item]
            except ValueError:
                raise Error1("Path: tblcols items must be int, got "
                             "{0}".format(tblcols))
            item[0] -= 1

            if len(item) == 1:
                columns.append(item[0])
            elif len(item) not in (2, 3):
                raise Error1("Path: tblcfmt range must be specified as "
                             "start:end:[step], got {0}".format(
                                 ":".join(str(x) for x in item)))
            if len(item) == 2:
                columns.extend(range(item[0], item[1]))
            elif len(item) == 3:
                columns.extend(range(item[0], item[1], item[2]))
        return columns

    @staticmethod
    def format_path(path, options):
        """Format the path by applying multipliers

        """
        # stress control if any of the control types are 3 or 4
        stress_control = any(c in (3, 4) for leg in path for c in leg[2])
        kappa = options.getopt("kappa")
        if stress_control and kappa != 0.:
            raise Error1("kappa must be 0 with stress control option")

        # From these formulas, note that AMPL may be used to increase or
        # decrease the peak strain without changing the strain rate. ratfac is
        # the multiplier on strain rate and stress rate.
        amplitude = options.getopt("amplitude")
        ratfac = options.getopt("ratfac")
        nfac = options.getopt("nfac")
        ndumps = options.getopt("ndumps")
        if ndumps == "all":
            ndumps = 100000000
        ndumps= int(ndumps)

        # factors to be applied to deformation types
        efac = amplitude * options.getopt("estar")
        tfac = abs(amplitude) * options.getopt("tstar") / ratfac
        sfac = amplitude * options.getopt("sstar")
        ffac = amplitude * options.getopt("fstar")
        effac = amplitude * options.getopt("efstar")
        dfac = amplitude * options.getopt("dstar")

        # for now unit tensor for rotation
        Rij = np.reshape(np.eye(3), (9,))

        # format each leg
        for ileg, (termination_time, num_steps, control, Cij) in enumerate(path):

            leg_num = ileg + 1

            num_steps = int(nfac * num_steps)
            termination_time = tfac * termination_time

            # pull out electric field from other deformation specifications
            efcomp = np.zeros(3)
            trtbl = np.array([True] * len(control))
            j = 0
            for i, c in enumerate(control):
                if c == 6:
                    efcomp[j] = effac * Cij[i]
                    trtbl[i] = False
                    j += 1
            Cij = Cij[trtbl]
            control = control[trtbl]

            if 5 in control:
                # check for valid deformation
                defgrad = np.reshape(ffac * Cij, (3, 3))
                jac = np.linalg.det(defgrad)
                if jac <= 0:
                    raise Error1("Inadmissible deformation gradient in "
                                 "leg {0} gave a Jacobian of "
                                 "{1:f}".format(leg_num, jac))

                # convert defgrad to strain E with associated rotation given by
                # axis of rotation x and angle of rotation theta
                Rij, Vij = np.linalg.qr(defgrad)
                if np.max(np.abs(Rij - np.eye(3))) > np.finfo(np.float).eps:
                    raise Error1("Rotation encountered in leg {0}. "
                                 "Rotations are not yet supported".format(leg_num))
                Uij = tensor.asarray(np.dot(Rij.T, np.dot(Vij, Rij)))
                Rij = np.reshape(Rij, (9,))
                Cij = tensor.u2e(Uij, kappa)

                # deformation gradient now converted to strains
                control = np.array([2] * 6, dtype=np.int)

            elif 8 in control:
                # displacement control check
                # convert displacments to strains
                Uij = np.zeros(6)
                Uij[:3] = dfac * Cij[:3] + 1.
                Cij = tensor.u2e(Uij, kappa)

                # displacements now converted to strains
                control = np.array([2] * 6, dtype=np.int)

            elif 2 in control and len(control) == 1:
                # only one strain value given -> volumetric strain
                evol = Cij[0]
                if kappa * evol + 1. < 0.:
                    raise Error1("1 + kappa * ev must be positive in leg "
                                 "{0}".format(leg_num))

                if kappa == 0.:
                    eij = evol / 3.

                else:
                    eij = ((kappa * evol + 1.) ** (1. / 3.) - 1.)
                    eij = eij / kappa

                control = np.array([2] * 6, dtype=np.int)
                Cij = np.array([eij, eij, eij, 0., 0., 0.])

            elif 4 in control and len(control) == 1:
                # only one stress value given -> pressure
                Sij = -Cij[0]
                control = np.array([4, 4, 4, 2, 2, 2], dtype=np.int)
                Cij = np.array([Sij, Sij, Sij, 0., 0., 0.])

            if len(control) != len(Cij):
                raise Error1("len(cij) != len(control) in leg {0}".format(leg_num))

            control = np.append(control, [2] * (6 - len(control)))
            Cij = np.append(Cij, [0.] * (6 - len(Cij)))

            # adjust components based on user input
            for idx, ctype in enumerate(control):
                if ctype in (1, 3):
                    # adjust rates
                    Cij[idx] *= ratfac

                elif ctype == 2:
                    # adjust strain
                    Cij[idx] *= efac

                    if kappa * Cij[idx] + 1. < 0.:
                        raise Error("1 + kappa*E[{0}] must be positive in "
                                    "leg {1}".format(idx, leg_num))

                elif ctype == 4:
                    # adjust stress
                    Cij[idx] *= sfac

                continue

            # initial stress check
            if termination_time == 0.:
                if 3 in control:
                    raise Error1("initial stress rate ambiguous")
                elif 4 in control and any(x != 4 for x in control):
                    raise Error1("Mixed initial state not allowed")

            # Replace leg with modfied values
            path[ileg][0] = termination_time
            path[ileg][1] = num_steps
            path[ileg][2] = control
            path[ileg][3] = Cij
            path[ileg].append(ndumps)

            # legs[ileg].append(Rij)
            path[ileg].append(efcomp)

            continue

        return path

def mybool(a):
    if str(a).lower().strip() in ("false", "no", "0"):
        return 0
    else:
        return 1
