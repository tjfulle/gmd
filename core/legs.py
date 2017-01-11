import numpy as np
from collections import OrderedDict

from utils.errors import MatModLabError
from utils.constants import DEFAULT_TEMP, NTENS, NSYMM
import utils.mmlabpack as mmlabpack
from core.functions import _Function as Function, DEFAULT_FUNCTIONS

CONTROL_FLAGS = {"D": 1,  # strain rate
                 "E": 2,  # strain
                 "R": 3,  # stress rate
                 "S": 4,  # stress
                 "F": 5,  # deformation gradient
                 "P": 6,  # electric field
                 "T": 7,  # temperature
                 "U": 8,  # displacement
                 "X": 9}  # user defined field

class Leg:
    def __init__(self, start_time, termination_time, num_steps, control,
                 Cij, ndumps, efcomp, temp, user_field=None):
        self.start_time = start_time
        self.termination_time = termination_time
        self.dtime = termination_time - start_time

        self.num_steps = num_steps
        self.control = control
        self.components = Cij
        assert len(control) == len(Cij)

        self.num_dumps = ndumps
        self.efield = efcomp

        self.temp = temp
        self.user_field = user_field

        imap = dict([(v,k) for (k,v) in CONTROL_FLAGS.items()])
        imap[1] = 'DE'
        imap[3] = 'DS'
        self.descriptors = [imap[x] for x in self.control]

class LegRepository(OrderedDict):

    @classmethod
    def from_path(cls, driver, path_input, path, num_steps, amplitude,
                  rate_multiplier, step_multiplier, num_io_dumps,
                  termination_time, tfmt, cols, cfmt, skiprows, functions,
                  kappa, estar, tstar, sstar, fstar, efstar, dstar):

        d = driver.lower()
        if d not in ("continuum",):
            raise MatModLabError("{0}: driver not recognized".format(driver))

        p = path_input.lower()
        if p not in ("default", "function", "table"):
            raise MatModLabError("{0}: path_input not "
                                 "recognized".format(path_input))

        if d == "continuum":
            if p == "default":
                path = cls._parse_default_path(path)

            elif p == "function":
                num_steps = num_steps or 1
                if cfmt is None:
                    raise MatModLabError("function path: expected keyword cfmt")
                num_steps = int(num_steps * step_multiplier)
                path = cls._parse_function_path(path, functions, num_steps, cfmt)

            elif p == "table":
                if cfmt is None:
                    raise MatModLabError("table path: expected keyword cfmt")
                if cols is None:
                    raise MatModLabError("table path: expected keyword cols")
                if not isinstance(cols, (list, tuple)):
                    raise MatModLabError("table path: expected cols to be a list")
                path = cls._parse_table_path(path, tfmt, cols, cfmt, skiprows)

            return cls.from_continuum_path(path, kappa, amplitude,
                       rate_multiplier, step_multiplier, num_io_dumps, estar,
                       tstar, sstar, fstar, efstar, dstar, termination_time)


    @classmethod
    def from_continuum_path(cls, path, kappa, amplitude, ratfac, nfac, ndumps,
                            estar, tstar, sstar, fstar, efstar, dstar, tterm):
        """Format the path by applying multipliers

        """
        legs = cls()

        # stress control if any of the control types are 3 or 4
        stress_control = any(c in (3, 4) for leg in path for c in leg[2])
        if stress_control and kappa != 0.:
            raise MatModLabError("kappa must be 0 with stress control option")

        # From these formulas, note that AMPL may be used to increase or
        # decrease the peak strain without changing the strain rate. ratfac is
        # the multiplier on strain rate and stress rate.
        if ndumps == "all":
            ndumps = 100000000
        ndumps= int(ndumps)

        # factors to be applied to deformation types
        efac = amplitude * estar
        tfac = abs(amplitude) * tstar / ratfac
        sfac = amplitude * sstar
        ffac = amplitude * fstar
        effac = amplitude * efstar
        dfac = amplitude * dstar

        # for now unit tensor for rotation
        Rij = np.reshape(np.eye(3), (NTENS,))

        # format each leg
        if not tterm:
            tterm = 1.e80

        for ileg, (termination_time, num_steps, control, Cij) in enumerate(path):

            leg_num = ileg + 1

            num_steps = int(nfac * num_steps)
            termination_time = tfac * termination_time

            if len(control) != len(Cij):
                raise MatModLabError("len(cij) != len(control) in leg "
                                     "{0}".format(leg_num))
                continue

            # pull out electric field from other deformation specifications
            temp = DEFAULT_TEMP
            efcomp = np.zeros(3)
            user_field = []
            trtbl = np.array([True] * len(control))
            j = 0
            for i, c in enumerate(control):
                if c in (6, 7, 9):
                    trtbl[i] = False
                    if c == 6:
                        efcomp[j] = effac * Cij[i]
                        j += 1
                    elif c == 7:
                        temp = Cij[i]
                    else:
                        user_field.append(Cij[i])

            Cij = Cij[trtbl]
            control = control[trtbl]

            if 5 in control:
                # check for valid deformation
                defgrad = np.reshape(ffac * Cij, (3, 3))
                jac = np.linalg.det(defgrad)
                if jac <= 0:
                    raise MatModLabError("Inadmissible deformation gradient in "
                                         "leg {0} gave a Jacobian of "
                                         "{1:f}".format(leg_num, jac))

                # convert defgrad to strain E with associated rotation given by
                # axis of rotation x and angle of rotation theta
                Rij, Vij = np.linalg.qr(defgrad)
                if np.max(np.abs(Rij - np.eye(3))) > np.finfo(np.float).eps:
                    raise MatModLabError("Rotation encountered in leg "
                                         "{0}. Rotations are not "
                                         "supported".format(leg_num))
                Uij = np.dot(Rij.T, np.dot(Vij, Rij))
                Cij = mmlabpack.u2e(Uij, kappa)
                Rij = np.reshape(Rij, (NTENS,))

                # deformation gradient now converted to strains
                control = np.array([2] * NSYMM, dtype=np.int)

            elif 8 in control:
                # displacement control check
                # convert displacments to strains
                Uij = np.zeros((3, 3))
                Uij[DI3] = dfac * Cij[:3] + 1.
                Cij = mmlabpack.u2e(Uij, kappa, 1)

                # displacements now converted to strains
                control = np.array([2] * NSYMM, dtype=np.int)

            elif 2 in control and len(control) == 1:
                # only one strain value given -> volumetric strain
                evol = Cij[0]
                if kappa * evol + 1. < 0.:
                    raise MatModLabError("1 + kappa * ev must be positive in leg "
                                         "{0}".format(leg_num))

                if kappa == 0.:
                    eij = evol / 3.

                else:
                    eij = ((kappa * evol + 1.) ** (1. / 3.) - 1.)
                    eij = eij / kappa

                control = np.array([2] * NSYMM, dtype=np.int)
                Cij = np.array([eij, eij, eij, 0., 0., 0.])

            elif 4 in control and len(control) == 1:
                # only one stress value given -> pressure
                Sij = -Cij[0]
                control = np.array([4, 4, 4, 2, 2, 2], dtype=np.int)
                Cij = np.array([Sij, Sij, Sij, 0., 0., 0.])

            control = np.append(control, [2] * (NSYMM - len(control)))
            Cij = np.append(Cij, [0.] * (NSYMM - len(Cij)))

            # adjust components based on user input
            for idx, ctype in enumerate(control):
                if ctype in (1, 3):
                    # adjust rates
                    Cij[idx] *= ratfac

                elif ctype == 2:
                    # adjust strain
                    Cij[idx] *= efac

                    if kappa * Cij[idx] + 1. < 0.:
                        raise MatModLabError("1 + kappa*E[{0}] must be positive in "
                                             "leg {1}".format(idx, leg_num))

                elif ctype == 4:
                    # adjust stress
                    Cij[idx] *= sfac

                continue

            # initial stress check
            if abs(termination_time) < 1.e-16:
                if 3 in control:
                    raise MatModLabError("initial stress rate ambiguous")

                elif 4 in control and any(x != 0. for x in Cij):
                    raise MatModLabError("nonzero initial stress not yet supported")

            start_time = 0. if not legs else legs[ileg-1].termination_time
            legs[ileg] = Leg(start_time, termination_time, num_steps, control,
                             Cij, ndumps, efcomp, temp, user_field)

            if termination_time > tterm:
                break

            continue

        return legs

    @classmethod
    def _parse_default_path(cls, lines):
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
            termination_time = cls._format_termination_time(
                leg_num, termination_time, final_time)
            if termination_time is None:
                termination_time = 1e99
            final_time = termination_time

            # --- number of steps
            num_steps = cls._format_num_steps(leg_num, num_steps)
            if num_steps is None:
                num_steps = 10000

            # --- control
            control = cls._format_path_control(control_hold, leg_num=leg_num)

            # --- Cij
            Cij = []
            for (i, comp) in enumerate(Cij_hold):
                try:
                    comp = float(comp)
                except ValueError:
                    raise MatModLabError("Path: Component {0} of leg {1} "
                                         "must be a float, got {2}".format(
                                             i+1, leg_num, comp))
                Cij.append(comp)

            Cij = np.array(Cij)

            # --- Check lengths of Cij and control are consistent
            if len(Cij) != len(control):
                raise MatModLabError("Path: len(Cij) != len(control) in leg {0}"
                                     .format(leg_num))
                continue

            path.append([termination_time, num_steps, control, Cij])
            leg_num += 1
            continue

        return path

    @classmethod
    def _parse_function_path(cls, lines, functions, num_steps, cfmt):
        """Parse the path given by functions

        """
        start_time = 0.
        leg_num = 1
        if not lines:
            raise MatModLabError("Empty path encountered")
            return
        elif len(lines) > 1:
            raise MatModLabError("Only one line of table functions allowed, "
                                 "got {0}".format(len(lines)))
            return

        # format functions
        functions = cls._format_functions(functions)

        termination_time = lines[0][0]
        cijfcns = lines[0][1:]

        # check entries
        # --- termination time
        termination_time = cls._format_termination_time(1, termination_time, -1)
        if termination_time is None:
            # place holder, just to check rest of input
            termination_time = 1.e99
        final_time = termination_time

        # --- control
        control = cls._format_path_control(cfmt, leg_num=leg_num)

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
                raise MatModLabError("expected integer function ID, "
                                     "got {0}".format(fid))
                continue
            try:
                scale = float(scale)
            except ValueError:
                raise MatModLabError("expected real function scale for function {0}"
                                     ", got {1}".format(fid, scale))

            fcn = functions.get(fid)
            if fcn is None:
                raise MatModLabError("{0}: function not defined".format(fid))
            Cij.append((scale, fcn))

        # --- Check lengths of Cij and control are consistent
        if len(Cij) != len(control):
            raise MatModLabError("Path: len(Cij) != len(control) in "
                                 "leg {0}".format(leg_num))

        path = []
        vals = np.zeros(len(control))
        if 7 in control:
            # check for nonzero initial values of temperature
            idx = np.where(control == 7)[0][0]
            s, f = Cij[idx]
            vals[idx] = s * f(start_time)
        path.append([start_time, 1, control, vals])
        for time in np.linspace(start_time, final_time, num_steps-1):
            if time == start_time:
                continue
            leg = [time, 1, control]
            leg.append(np.array([s * f(time) for (s, f) in Cij]))
            path.append(leg)
        return path

    @classmethod
    def _parse_table_path(cls, lines, tfmt, cols, cfmt, lineskip):
        """Parse the path table

        """
        path = []
        final_time = 0.
        termination_time = 0.
        leg_num = 1

        # check the control
        control = cls._format_path_control(cfmt)

        if isinstance(lines, np.ndarray):
            tbl = np.array(lines)
        else:
            tbl = []
            for idx, line in enumerate(lines):
                if idx < lineskip or not line:
                    continue
                if line[0].strip().startswith("#"):
                    continue
                try:
                    line = [float(x) for x in line]
                except ValueError:
                    raise MatModLabError("Expected floats in leg {0}, "
                                         "got {1}".format(leg_num, line))
                tbl.append(line)
            tbl = np.array(tbl)

        # if cols was not specified, must want all
        if not cols:
            columns = list(range(tbl.shape[1]))
        else:
            columns = cols

        for line in tbl:
            try:
                line = line[columns]
            except IndexError:
                raise MatModLabError("Requested column not found in leg "
                                     "{0}".format(leg_num))

            if tfmt == "dt":
                termination_time += line[0]
            else:
                termination_time = line[0]

            Cij = line[1:]

            # check entries
            # --- termination time
            termination_time = cls._format_termination_time(
                leg_num, termination_time, final_time)
            if termination_time is None:
                continue
            final_time = termination_time

            # --- number of steps
            num_steps = 1

            # --- Check lengths of Cij and control are consistent
            if len(Cij) != len(control):
                raise MatModLabError("Path: len(Cij) != len(control) "
                                     "in leg {0}".format(leg_num))

            path.append([termination_time, num_steps, control, Cij])
            leg_num += 1
            continue

        return path

    @staticmethod
    def _format_termination_time(leg_num, termination_time, final_time):
        try:
            termination_time = float(termination_time)
        except ValueError:
            raise MatModLabError("Path: expected float for termination time of "
                                 "leg {0} got {1}".format(leg_num,
                                                          termination_time))

        if termination_time < 0.:
            raise MatModLabError("Path: expected positive termination time leg {0} "
                                 "got {1}".format(leg_num, termination_time))

        if termination_time < final_time:
            raise MatModLabError("Path: expected time to increase monotonically in "
                                 "leg {0}".format(leg_num))

        return termination_time

    @staticmethod
    def _format_num_steps(leg_num, num_steps):
        try:
            num_steps = int(num_steps)
        except ValueError:
            raise MatModLabError("Path: expected integer number of steps in "
                                 "leg {0} got {1}".format(leg_num, num_steps))
        if num_steps < 0:
            raise MatModLabError("Path: expected positive integer number of "
                                 "steps in leg {0} got {1}".format(
                                     leg_num, num_steps))
        return num_steps

    @staticmethod
    def _format_path_control(cfmt, leg_num=None):
        leg = "" if leg_num is None else "(leg {0})".format(leg_num)

        _cfmt = [CONTROL_FLAGS.get(s.upper(), s) for s in cfmt]

        control = []
        for (i, flag) in enumerate(_cfmt):
            try:
                flag = int(flag)
            except ValueError:
                raise MatModLabError("Path: unexpected control "
                                     "flag {0}".format(flag))

            if flag not in CONTROL_FLAGS.values():
                valid = ", ".join(xmltools.stringify(x)
                                  for x in CONTROL_FLAGS.values())
                raise MatModLabError("Path: expected control flag to be one "
                                     "of {0}, got {1} {2}".format(valid, flag, leg))

            control.append(flag)

        if control.count(7) > 1:
                raise MatModLabError("Path: multiple temperature fields in "
                                     "leg {0}".format(leg))

        if 5 in control:
            if any(flag != 5 and flag not in (6, 9) for flag in control):
                raise MatModLabError("Path: mixed mode deformation not allowed with "
                                     "deformation gradient control {0}".format(leg))

            # must specify all components
            elif len(control) < 9:
                raise MatModLabError("all 9 components of deformation gradient must "
                                     "be specified {0}".format(leg))

        if 8 in control:
            # like deformation gradient control, if displacement is specified
            # for one, it must be for all
            if any(flag != 8 and flag not in (6, 9) for flag in control):
                raise MatModLabError("Path: mixed mode deformation not allowed with "
                                     "displacement control {0}".format(leg))

            # must specify all components
            elif len(control) < 3:
                raise MatModLabError("all 3 components of displacement must "
                                     "be specified {0}".format(leg))

        return np.array(control, dtype=np.int)

    @staticmethod
    def _format_functions(funcs):
        functions = dict(DEFAULT_FUNCTIONS)
        if isinstance(funcs, Function):
            functions[funcs.func_id] = funcs
        else:
            for func in funcs:
                if not isinstance(func, Function):
                    raise MatModLabError("functions must be instances "
                                         "of utils.functions.Function")
                functions[func.func_id] = func
        return functions
