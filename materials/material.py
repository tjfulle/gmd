import numpy as np

from core.mmlio import Error1, log_warning
from materials.parameters import Parameters
try:
    from lib.mmlabpack import mmlabpack
except ImportError:
    import utils.mmlabpack as mmlabpack

class Material(object):
    def __init__(self):
        self.mtldb = None
        self.nparam = 0
        self.ndata = 0
        self.nxtra = 0
        self._constant_jacobian = False
        self.bulk_modulus = None
        self.shear_modulus = None
        self._jacobian = None
        self.xinit = np.zeros(self.nxtra)
        self.mtl_variables = []
        self.initialized = True
        self.use_constant_jacobian = False
        if not hasattr(self, "param_names"):
            raise Error1("{0}: param_names not defined".format(self.name))
        self._verify_param_names()

    @classmethod
    def param_parse_table(cls):
        n_no_parse = 0
        parse_table = {}
        for (i, param) in enumerate(cls.param_names):
            for n in param.split(":"):
                n = n.strip().lower()
                if n.startswith("-"):
                    # not to be parsed
                    n = n[1:]
                    i = -i
                parse_table[n] = i

        if hasattr(cls, 'param_defaults'):
            param_defaults = np.array(cls.param_defaults)
            if len(set(parse_table.values())) != len(param_defaults):
                raise Error1("{0}: len(param_defaults) != len(param_names)".
                                                          format(self.name))
        else:
            param_defaults = np.zeros(len(set(parse_table.values())))

        return parse_table, param_defaults, cls.param_names

    @staticmethod
    def _fmt_param_name_aliases(s, mode=0):
        s = [n.upper() for n in s.split(":")]
        if mode == 0:
            return s[0], s[1:]
        if mode == -1:
            return s[0]
        return ":".join(s)

    def _verify_param_names(self):
        registered_params = []
        self.nparam = len(self.param_names)
        for idx, name in enumerate(self.param_names):
            name, aliases = self._fmt_param_name_aliases(name)
            if name in registered_params:
                raise Error1("{0}: param already registered".format(name))
            registered_params.append(name)
            for alias in aliases:
                if alias in registered_params:
                    raise Error1("{0}: non-unique param alias".format(alias))
                registered_params.append(name)

    def set_options(self, **kwargs):
        for (k, v) in kwargs.items():
            setattr(self, "_{0}".format(k), v)

    def set_constant_jacobian(self):
        if not self.bulk_modulus:
            # raise Error1("{0}: bulk modulus not defined".format(self.name))
            log_warning("{0}: bulk modulus not defined".format(self.name))
            return
        if not self.shear_modulus:
            # raise Error1("{0}: shear modulus not defined".format(self.name))
            log_warning("{0}: shear modulus not defined".format(self.name))
            return

        self._jacobian = np.zeros((6, 6))
        threek = 3. * self.bulk_modulus
        twog = 2. * self.shear_modulus
        nu = (threek - twog) / (2. * threek + twog)
        c1 = (1. - nu) / (1. + nu)
        c2 = nu / (1. + nu)

        # set diagonal
        for i in range(3):
            self._jacobian[i, i] = threek * c1
        for i in range(3, 6):
            self._jacobian[i, i] = twog

        # off diagonal
        (self._jacobian[0, 1], self._jacobian[0, 2],
         self._jacobian[1, 0], self._jacobian[1, 2],
         self._jacobian[2, 0], self._jacobian[2, 1]) = [threek * c2] * 6
        return

    def register_mtl_variable(self, var, vtype, units=None):
        self.mtl_variables.append((var, vtype))

    def register_xtra_variables(self, keys, mig=False):
        if self.nxtra:
            raise Error1("Register extra variables at most once")
        if mig:
            keys = [" ".join(x.split())
                    for x in "".join(keys).split("|") if x.split()]
        self.nxtra = len(keys)
        for (i, key) in enumerate(keys):
            self.register_mtl_variable(key, "SCALAR")
            setattr(self, "_x{0}".format(key), i)

    def xidx(self, key):
        return getattr(self, "_x{0}".format(key), None)

    def get_initial_jacobian(self):
        """Get the initial Jacobian numerically

        """
        d = np.zeros(6)
        sig = np.zeros(6)
        t = 0.
        f0 = np.eye(3).reshape(9,)
        f = np.eye(3).reshape(9,)
        eps = np.zeros(6)
        ef = np.zeros(3)
        tmpr = 0.
        dtmpr = 0.
        ufield = 0.
        args = (t, f0, f, eps, ef, tmpr, dtmpr, ufield)
        return self.numerical_jacobian(1., d, sig, self.xinit, range(6), *args)

    def jacobian(self, time, dtime, temp, dtemp, F0, F, stran, d,
                 stress, statev, elec_field, user_field, v):
        if self.use_constant_jacobian:
            return self.constant_jacobian(v)
        return self.numerical_jacobian(time, dtime, temp, dtemp, F0, F, stran, d,
                                       stress, statev, elec_field, user_field, v)

    def numerical_jacobian(self, time, dtime, temp, dtemp, F0, F, stran, d,
                           stress, statev, elec_field, user_field, v):
        """Numerically compute material Jacobian by a centered difference scheme.

        Returns
        -------
        Js : array_like
          Jacobian of the deformation J = dsig / dE

        Notes
        -----
        The submatrix returned is the one formed by the intersections of the
        rows and columns specified in the vector subscript array, v. That is,
        Js = J[v, v]. The physical array containing this submatrix is
        assumed to be dimensioned Js[nv, nv], where nv is the number of
        elements in v. Note that in the special case v = [1,2,3,4,5,6], with
        nv = 6, the matrix that is returned is the full Jacobian matrix, J.

        The components of Js are computed numerically using a centered
        differencing scheme which requires two calls to the material model
        subroutine for each element of v. The centering is about the point eps
        = epsold + d * dt, where d is the rate-of-strain array.

        History
        -------
        This subroutine is a python implementation of a routine by the same
        name in Tom Pucick's MMD driver.

        Authors
        -------
        Tom Pucick, original fortran implementation in the MMD driver
        Tim Fuller, Sandial National Laboratories, tjfulle@sandia.gov

        """
        if self._constant_jacobian:
            return self.constant_jacobian(v)

        # local variables
        nv = len(v)
        deps =  np.sqrt(np.finfo(np.float64).eps)
        Jsub = np.zeros((nv, nv))
        dtime = 1 if dtime < 1.e-12 else dtime

        for i in range(nv):
            # perturb forward
            dp = d.copy()
            dp[v[i]] = d[v[i]] + (deps / dtime) / 2.
            fp, ep = mmlabpack.update_deformation(dtime, 0., f, dp)
            sigp = sig.copy()
            xtrap = xtra.copy()
            sigp, xtrap = self.compute_update_state(time, dtime, temp, dtemp,
                f0, fp, ep, dp, sigp, xtrap, elec_field, user_field)

            # perturb backward
            dm = d.copy()
            dm[v[i]] = d[v[i]] - (deps / dtime) / 2.
            fm, em = mmlabpack.update_deformation(dtime, 0., f, dm)
            sigm = sig.copy()
            xtram = xtra.copy()
            sigp, xtrap = self.compute_update_state(time, dtime, temp, dtemp,
                f0, fm, em, dm, sigm, xtram, elec_field, user_field)

            # compute component of jacobian
            Jsub[i, :] = (sigp[v] - sigm[v]) / deps

            continue

        return Jsub

    def isparam(self, param_name):
        return getattr(self.params, param_name.upper(), False)

    def parameters(self, ival=False, names=False):
        if names:
            return [self._fmt_param_name_aliases(p, mode=1)
                    for p in self.param_names]
        if ival:
            return self.iparams
        return self.params

    def setup_new_material(self, params):
        # For some reason we need to clean the param name aliases.
        self.iparams = np.array(params)
        names = [self._fmt_param_name_aliases(p, mode=-1)
                 for p in params.names]
        self.params = Parameters(names, np.array(params), params.modelname)
        self.setup()

    def setup(self, *args, **kwargs):
        raise Error1("setup must be provided by model")

    def update_state(self, *args, **kwargs):
        raise Error1("update_state must be provided by model")

    def compute_update_state(self, time, dtime, temp, dtemp, F0, F, stran, d,
                             stress, statev, elec_field, user_field, last=False):
        """Update the material state

        """
        args = (time, F0, F, stran, elec_field, temp, dtemp, user_field)
        return self.update_state(dtime, d, stress, statev, *args, last=last)

    def adjust_initial_state(self, *args, **kwargs):
        self.set_initial_state(args[0])

    def initialize(self, temp, user_field):
        """Call the material with initial state

        """
        time = 0.
        dtime = 1.
        dtemp = 0.
        F0 = np.eye(3).reshape(9,)
        F = np.eye(3).reshape(9,)
        stran = np.zeros(6)
        d = np.zeros(6)
        stress = np.zeros(6)
        statev = self.initial_state
        elec_field = np.zeros(3)
        return self.compute_update_state(time, dtime, temp, dtemp, F0, F,
            stran, d, stress, statev, elec_field, user_field)

    def set_initial_state(self, xtra):
        self.xinit = np.array(xtra)

    @property
    def initial_state(self):
        return self.xinit

    @property
    def material_variables(self):
        return self.mtl_variables

    def constant_jacobian(self, v=np.arange(6)):
        return self._jacobian[[[x] for x in v], v]
