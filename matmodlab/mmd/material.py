import os
import re
import sys
import numpy as np
from numpy.linalg import cholesky, LinAlgError
import logging

from matmodlab.product import PKG_D, BIN_D, ROOT_D

from ..mml_siteenv import environ
from ..utils.errors import MatmodlabError
from ..utils import mmlabpack
from ..utils.misc import remove
from ..mmd.loader import MaterialLoader

from ..constants import *
from ..materials.completion import *
from ..materials.addon_trs import TRS
from ..materials.addon_expansion import Expansion
from ..materials.addon_viscoelastic import Viscoelastic
from ..materials.product import is_user_model, USER
from ..utils.fortran.product import SDVINI

from ..constants import XX, YY, ZZ, XY, YZ, XZ, DEFAULT_TEMP

class MetaClass(type):
    '''metaclass which overrides the '__call__' function'''
    def __call__(cls, parameters, **kwargs):
        '''Called when you call Class() '''
        obj = type.__call__(cls)
        obj.init(parameters, **kwargs)
        return obj

def Eye(n):
    # Specialized identity for tensors
    if n == 6:
        return np.array([1, 1, 1, 0, 0, 0], dtype=np.float64)
    if n == 9:
        return np.array([1, 0, 0, 0, 1, 0, 0, 0, 1], dtype=np.float64)
    raise MatmodlabError('incorrect n')

class MaterialModel(object):
    '''The base material class

    '''
    __metaclass__ = MetaClass
    name = None
    user = False
    lapack = 'lite'
    completions = None
    elastic_props = None
    lib = None
    libname = None

    @classmethod
    def source_files(cls):
        return []

    @classmethod
    def aux_files(cls):
        return []

    @staticmethod
    def completions_map():
        return None

    def _import_lib(self, libname=None):
        for k in sys.modules.keys():
            if self.lib == sys.modules[k]:
                sys.modules.pop(k)
                break
        self.import_lib(libname=libname)

    def import_lib(self, libname=None):
        pass

    def init(self, parameters, **kwargs):
        '''Parses parameters from user input and allocates parameter array

        '''
        if self.name is None:
            raise MatmodlabError('material did not define name attribute')

        logging.getLogger('matmodlab.mmd.simulator').info(
            'setting up {0} material'.format(self.name))

        # --- parse the input parameters
        if kwargs.get('param_names') is not None:
            param_names = kwargs['param_names']
        else:
            constants = len(parameters)
            param_names = self.param_names(constants)

        param_names = [s.upper() for s in param_names]
        if not isinstance(parameters, (dict,)):
            if len(parameters) != len(param_names):
                raise MatmodlabError('parameters and param_names have '
                                     'inconsistent lengths')
            parameters = dict(zip(param_names, parameters))

        # populate the parameters array
        params = np.zeros(len(param_names))
        errors = 0
        for (key, value) in parameters.items():
            try:
                idx = param_names.index(key.upper())
            except ValueError:
                errors += 1
                logging.error('{0}: unrecognized parameter '
                              'for model {1}'.format(key, self.name))
                continue
            try:
                params[idx] = float(value)
            except ValueError:
                errors += 1
                logging.error('parameter {0} must be a float'.format(key))

        if errors:
            raise MatmodlabError('stopping due to previous errors')

        self.parameter_names = [s.upper() for s in param_names]
        self.iparray = np.array(params)

        # --- set defaults
        self.sqa_stiff = kwargs.get('sqa_stiff', environ.sqa_stiff)
        self.num_stiff = kwargs.get('num_stiff', environ.num_stiff)
        self.iwarn_stiff = 0
        self.visco_model = None
        self.xpan = None
        self.trs_model = None
        self.initial_temp = kwargs.get('initial_temp', DEFAULT_TEMP)

        # parameter arrays
        self.iparams = keyarray(self.parameter_names, self.iparray)
        self.params = keyarray(self.parameter_names, self.iparray)

        # import the material library
        self._import_lib(libname=kwargs.get('libname'))

        # --- setup and initialize the model
        try:
            item = self.setup(**kwargs)
        except Exception as e:
            s = 'failed to setup material model with the following exception:'
            s += '\n' + ' '.join(e.args)
            raise MatmodlabError(s)

        if item is None:
            sdv_keys, sdv_vals = [], []
        else:
            try:
                sdv_keys, sdv_vals = item
            except ValueError:
                raise MatmodlabError('Expected the material setup to return '
                                     'only the sdv keys and values')

        self.num_sdv = len(sdv_keys)
        if len(sdv_vals) != len(sdv_keys):
            raise MatmodlabError('len(sdv_values) != len(sdv_keys)')
        self.sdv_keys = [s for s in sdv_keys]
        self.initial_sdv = np.array(sdv_vals, dtype=np.float64)

        # call model with zero strain rate to get initial jacobian
        time, dtime = 0, 1
        temp, dtemp = self.initial_temp, 0.
        kappa = 0
        F0, F = Eye(9), Eye(9)
        stress, stran, d = np.zeros(6), np.zeros(6), np.zeros(6)
        elec_field = np.zeros(3)
        ddsdde = self.compute_updated_state(time, dtime, temp, dtemp, kappa,
                                       F0, F, stran, d, elec_field,
                                       stress, self.initial_sdv, disp=2)

        # check that stiffness is positive definite
        try:
            cholesky(ddsdde)
        except LinAlgError:
            raise MatmodlabError('initial elastic stiffness not positive definite')

        # property completions
        b = self.completions_map()

        # Check if None or empty dict
        if b is not None and b:
            a = self.params
        else:
            # Bulk modulus
            K = np.sum(ddsdde[:3,:3], axis=None) / 9.

            # Shear modulus
            # pure shear
            G = []
            G.append((2. * ddsdde[0,0] - 3. * ddsdde[0,1] - ddsdde[0,2]
                      + ddsdde[1,1] + ddsdde[1,2]) / 6.)
            G.append((2. * ddsdde[1,1] - 3. * ddsdde[1,2] + ddsdde[2,2]
                      -ddsdde[0,1] + ddsdde[0,2]) / 6.)
            G.append((2 * ddsdde[0,0] - ddsdde[0,1] - 3. * ddsdde[0,2]
                      + ddsdde[1,2] + ddsdde[2,2]) / 6.)
            # simple shear
            G.extend([ddsdde[3,3], ddsdde[4,4], ddsdde[5,5]])
            a = np.array([K, np.average(G)])
            b = {'K': 0, 'G': 1}

        # calculate all elastic constants
        self.completions = complete_properties(a, b)

        self.J0 = np.zeros((6, 6))
        K3 = 3. * self.completions['K']
        G = self.completions['G']
        G2 = 2. * G
        Lam = (K3 - G2) / 3.

        # set diagonal
        self.J0[np.ix_(range(3), range(3))] = Lam
        self.J0[range(3),range(3)] += G2
        self.J0[range(3,6),range(3,6)] = G

    def Viscoelastic(self, type, data):
        if self.visco_model is not None:
            raise MatmodlabError('Material supports only one Viscoelastic model')
        self.visco_model = Viscoelastic(type, data)
        vk, vd = self.visco_model.setup(trs_model=self.trs_model)
        self.visco_slice = self.augment_sdv(vk, vd)

    def TRS(self, definition, data):
        if self.trs_model is not None:
            raise MatmodlabError('Material supports only one TRS model')
        self.trs_model = TRS(definition, data)

    def Expansion(self, type, data):
        if self.xpan is not None:
            raise MatmodlabError('Material supports only one Expansion model')
        self.xpan = Expansion(type, data)
        ek, ed = self.xpan.setup()
        self.xpan_slice = self.augment_sdv(ek, ed)

    @classmethod
    def from_other(cls, other_mat):
        raise NotImplementedError('switching not supported by '
                                  '{0}'.format(cls.name))

    def augment_sdv(self, keys, values):
        '''Increase sdv_keys and initial state -> but not num_sdv. Used by the
        visco model to tack on the extra visco variables to the end of the
        statev array.'''
        M = len(self.sdv_keys)
        self.sdv_keys.extend(keys)
        N = len(self.sdv_keys)
        if len(values) != len(keys):
            raise MatmodlabError('len(values) != len(keys)')
        self.initial_sdv = np.append(self.initial_sdv, np.array(values))
        return slice(M, N)

    def numerical_jacobian(self, time, dtime, temp, dtemp, kappa, F0, F, stran, d,
                           elec_field, stress, statev, v):
        '''Numerically compute material Jacobian by a centered difference scheme.

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

        '''
        # local variables
        nv = len(v)
        deps =  np.sqrt(np.finfo(np.float64).eps)
        Jsub = np.zeros((nv, nv))
        dtime = 1 if dtime < 1.e-12 else dtime

        for i in range(nv):
            # perturb forward
            Dp = d.copy()
            Dp[v[i]] = d[v[i]] + (deps / dtime) / 2.
            Fp, Ep = mmlabpack.update_deformation(dtime, 0., F, Dp)
            sigp = stress.copy()
            xp = statev.copy()
            sigp = self.compute_updated_state(time, dtime, temp, dtemp, kappa,
                      F0, Fp, Ep, Dp, elec_field, sigp, xp, disp=3)

            # perturb backward
            Dm = d.copy()
            Dm[v[i]] = d[v[i]] - (deps / dtime) / 2.
            Fm, Em = mmlabpack.update_deformation(dtime, 0., F, Dm)
            sigm = stress.copy()
            xm = statev.copy()
            sigm = self.compute_updated_state(time, dtime, temp, dtemp, kappa,
                      F0, Fm, Em, Dm, elec_field, sigm, xm, disp=3)

            # compute component of jacobian
            Jsub[i, :] = (sigp[v] - sigm[v]) / deps

            continue

        return Jsub

    @property
    def parameters(self):
        return self.params

    @property
    def initial_parameters(self):
        return self.iparams

    def setup(self, **kwargs):
        pass

    def update_state(self, *args, **kwargs):
        raise NotImplementedError

    def tostr(self, obj='mps'):
        p = {}
        for (i, name) in enumerate(self.parameter_names):
            v = self.initial_parameters[i]
            if abs(v) <= 1.e-12:
                continue
            p[name] = v
        string = 'parameters = {0}\n'
        string += '{1}.Material(\'{2}\', parameters)\n'
        return string.format(p, obj, self.name)

    def dump_aba_kwds(self):
        s  = '** Abaqus keywords material deck for {NAME}\n'
        s += '** This file was automatically generated by Matmodlab\n'
        s += '*Parameter\n{PARAMS}\n'
        s += '*Material, name={NAME}\n'
        s += '*Depvar\n {DEPVAR}\n'
        if 'uhyper' in self.name.lower():
            s += '*Hyperelastic, properties={N}, '
            s += 'moduli=INSTANTANEOUS, type=COMPRESSIBLE\n'
        elif 'uaniso' in self.name.lower():
            s += '*Anisotropic Hyperelastic, formulation=INVARIANT, properties={N}, '
            s += 'moduli=INSTANTANEOUS, type=COMPRESSIBLE\n'
        else:
            s += '*User Material, constants={N}, type=MECHANICAL\n'
        s += ' {NAMES}'

        # create the parameters
        name = self.libname or self.name
        xp = zip(self.params.names, self.params)
        P = '\n'.join(' {0} = {1:.12e}'.format(x[0], float(x[1])) for x in xp)
        names = ['<{0}>'.format(x[0]) for x in xp]
        x = '\n '.join(', '.join(x) for x in grouper(names))
        s = s.format(NAME=name, PARAMS=P, DEPVAR=self.num_sdv,
                     N=self.num_prop, NAMES=x)
        with open('{0}.inc'.format(name), 'w') as fh:
            fh.write(s)

    def compute_updated_state(self, time, dtime, temp, dtemp, kappa, F0, F,
            stran, d, elec_field, stress, statev, disp=0, v=None, last=False,
            sqa_stiff=False):
        '''Update the material state

        '''
        V = v if v is not None else range(6)

        sig = np.array(stress)
        sdv = np.array(statev)

        if environ.sqa:
            ee = mmlabpack.e_from_f(kappa, F)
            if not np.allclose(ee, stran):
                stran = ee
                #raise Exception('not all close')

        # Mechanical deformation
        Fm, Em, dm = F, stran, d

        if self.xpan is not None:
            # thermal expansion: get mechanical deformation
            n = self.xpan_slice
            Fm, Em, dm = self.xpan.update_state(
                kappa, self.initial_temp, temp, dtemp, dtime, F, stran, d)
            sdv[n] = Em

        rho = 1.
        energy = 1.
        N = self.num_sdv
        sig, sdv[:N], ddsdde = self.update_state(time, dtime, temp, dtemp,
            energy, rho, F0, Fm, Em, dm, elec_field, sig,
            sdv[:N], last=last, mode=0)

        if self.visco_model is not None:
            # get visco correction
            n = self.visco_slice
            sig, cfac, sdv[n] = self.visco_model.update_state(
                time, dtime, temp, dtemp, sdv[n], F, sig)

        if disp == 3:
            return sig

        if self.num_stiff or self.visco_model is not None:
            # force the use of a numerical stiffness
            ddsdde = None

        if ddsdde is None:
            # material models without an analytic jacobian send the Jacobian
            # back as None so that it is found numerically here. Likewise, we
            # find the numerical jacobian for visco materials - otherwise we
            # would have to convert the the stiffness to that corresponding to
            # the Truesdell rate, pull it back to the reference frame, apply
            # the visco correction, push it forward, and convert to Jaummann
            # rate. It's not as trivial as it sounds...
            ddsdde = self.numerical_jacobian(time, dtime, temp, dtemp, kappa, F0,
                        Fm, Em, dm, elec_field, stress, sdv, V)

        if v is not None and len(v) != ddsdde.shape[0]:
            # if the numerical Jacobian was called, ddsdde is already the
            # sub-Jacobian
            ddsdde = ddsdde[[[i] for i in v], v]

        sqa_stiff = self.sqa_stiff or sqa_stiff
        if last and sqa_stiff:
            # check how close stiffness returned from material is to the numeric
            c = self.numerical_jacobian(time, dtime, temp, dtemp, kappa, F0,
                        Fm, Em, dm, elec_field, stress, sdv, V)
            err = np.amax(np.abs(ddsdde - c)) / np.amax(ddsdde)
            if err > 5.E-03: # .5 percent error
                msg = 'error in material stiffness: {0:.4E} ({1:.2f})'.format(
                    err, time)
                self.iwarn_stiff += 1
                if self.iwarn_stiff <= 10:
                    if self.iwarn_stiff == 10:
                        msg = msg + ' (future warnings suppressed)'
                    logging.getLogger('matmodlab.mmd.simulator').warn(msg)
                if self.sqa_stiff == 2:
                    ddsdde = c.copy()

        if disp == 2:
            return ddsdde

        elif disp == 1:
            return sig, sdv

        return sig, sdv, ddsdde

    @property
    def num_prop(self):
        return len(self.params)

# ----------------------------------------- Material Model Factory Method --- #
def Material(model, parameters, switch=None, response=None,
             source_files=None, ordering=None, rebuild=False, user_ics=False,
             libname=None, param_names=None, depvar=None, verbosity=None, **kwargs):
    """Factory method for subclasses of MaterialModel

    Parameters
    ----------
    model : str
        Material model name
    parameters : dict or ndarray
        Model parameters. For Abaqus umat models and matmodlab user models,
        parameters is a ndarray of model constants (specified in the order
        expected by the model). For other model types, parameters is a
        dictionary of name:value pairs.
    switch : str
        A name of a different material to substitute for model.
    response : str
        Defines the response of user materials. Specify 'mechanical'
        (default), 'hyperelastic', or 'anisohyperelastic'
    source_files : list of str or None
        List of model source files*. Each file name given in source_files must
        exist and be readable.
    rebuild : bool [False]
        Rebuild the material, or not.
    user_ics : bool [False]
        User defined model defines SDVINI
    libname : str
        Alternative name to give to built libraries.  Defaults to model.
    param_names : list or None
        Parameter names.  If given, then parameters must be a dict, otherwise,
        an array as described above.
    depvar : int, list, or None
        State dependent variables. If depvar is an integer, it represents the
        number of sdvs. If a list, the list contains the names of the sdvs.

    Recognized Keywords
    -------------------
    initial_temp : float or None
        Initial temperature. The initial temperature, if given, must be
        consistent with that of the simulation driver. Defaults to 298K if not
        specified.
    fiber_dirs : ndarray
        Fiber directions, applicable only for model=USER,
        response=ANISOHYPERELASTIC

    Returns
    -------
    material : MaterialModel instance

    """
    # check for any switching requests and handle them
    if switch is not None:
        # input arguments take precedent
        return switch_materials(model, switch, parameters)
    for (old, new) in environ.switch:
        if old == model:
            return switch_materials(model, new, parameters)

    errors = 0
    user_model = is_user_model(model)
    all_mats = MaterialLoader.load_materials()

    if model in environ.interactive_usr_materials:
        user_model = 1
        m = environ.interactive_usr_materials[model]
        source_files = [m['filename']]
        model = m['model']
        response = m['response']
        libname = m['libname']

    if model.lower() in environ.interactive_std_materials:
        # check if the model is in the interactive materials
        mat_info = None
        TheMaterial = environ.interactive_std_materials[model]

    elif model in all_mats.user_libs:
        # requested model has been specified in the user's environment file
        # adjust keywords per the user environment
        mat_info = all_mats.user_libs[model]
        TheMaterial = mat_info.mat_class
        param_names = mat_info.param_names or param_names
        source_files = mat_info.source_files
        user_ics = 0
        libname = mat_info.libname
        ordering = mat_info.ordering
        depvar = depvar or mat_info.depvar

    elif user_model:
        # requested model is a user model
        if not source_files:
            raise MatmodlabError('{0}: requires source_files'.format(model))
        for (i, f) in enumerate(source_files):
            filename = os.path.realpath(f)
            if not os.path.isfile(filename):
                errors += 1
                logging.getLogger('matmodlab.mmd.simulator').error(
                    '{0}: file not found'.format(f))
            source_files[i] = filename

        if not user_ics:
            source_files.append(SDVINI)

        mat_info = all_mats.get(model, response)
        TheMaterial = mat_info.mat_class
        source_files.extend(TheMaterial.aux_files())

    else:
        # check if material has been loaded
        mat_info = all_mats.get(model, response)
        if mat_info is None:
            raise MatmodlabError('model {0} not found'.format(model))
        TheMaterial = mat_info.mat_class
        source_files = TheMaterial.source_files()
        libname = TheMaterial.libname or TheMaterial.name

    if errors:
        raise MatmodlabError('stopping due to previous errors')

    # Check if model is already built (if applicable)
    if source_files:
        if libname is not None:
            # need to build with new libname
            for (i, f) in enumerate(source_files):
                if f.endswith('.pyf'):
                    signature = f
                    break
            else:
                raise MatmodlabError('signature file not found')
            lines = open(signature, 'r').read()
            new_signature = os.path.join(PKG_D, libname + '.pyf')
            libname_ = getattr(TheMaterial, 'libname', TheMaterial.name)
            pat = r'(?is)python\s+module\s+{0}'.format(libname_)
            repl = r'python module {0}'.format(libname)
            lines = re.sub(pat, repl, lines)
            with open(new_signature, 'w') as fh:
                fh.write(lines)
            source_files[i] = new_signature
        else:
            libname = getattr(TheMaterial, 'libname', TheMaterial.name)

        so_lib = os.path.join(PKG_D, libname + '.so')
        rebuild = rebuild or environ.rebuild_mat_lib
        if rebuild and libname not in environ.rebuild_mat_lib:
            remove(so_lib)
            environ.rebuild_mat_lib.append(libname)
        if not os.path.isfile(so_lib):
            logging.getLogger('matmodlab.mmd.simulator').info(
                '{0}: rebuilding material library'.format(libname))
            from ..mmd import builder as bb
            bb.Builder.build_material(libname, source_files,
                                      lapack=TheMaterial.lapack,
                                      verbosity=environ.verbosity)

        if not os.path.isfile(so_lib):
            raise MatmodlabError('model library for {0} '
                                 'not found'.format(libname))

    if user_model:
        ordering = [XX, YY, ZZ, XY, YZ, XZ] if ordering is None else ordering
        if user_model == 2:
            ordering = [XX, YY, ZZ, XY, XZ, YZ]
        # make sure it's a list
        kwargs['ordering'] = [_ for _ in ordering]
        kwargs['param_names'] = param_names

    # instantiate the material
    try:
        filename = mat_info.file
    except (AttributeError, TypeError):
        filename = None
    kwargs.update(file=filename, libname=libname,
                  param_names=param_names, depvar=depvar)
    material = TheMaterial(parameters, **kwargs)

    return material

def switch_materials(model_1, model_2, parameters):

    # determine which model
    all_mats = MaterialLoader.load_materials()

    # retrieve meta info for each material model
    mat_info_1 = all_mats.get(model_1)
    if mat_info_1 is None:
        raise MatmodlabError('model {0} not found'.format(model))

    mat_info_2 = all_mats.get(model_2)
    if mat_info_2 is None:
        raise MatmodlabError('model {0} not found'.format(model_2))

    # get each material's class
    TheMaterial_1 = mat_info_1.mat_class
    TheMaterial_2 = mat_info_2.mat_class

    # instantiate the first material
    try:
        material_1 = TheMaterial_1(parameters)
    except ImportError:
        raise MatmodlabError('failed to import {0}.  Material switching '
                             'requires that both materials be '
                             'built.'.format(material_1.name))

    # instantiate the second from the first
    logging.getLogger('matmodlab.mmd.simulator').warn(
        'switching material {0} for {1}'.format(model_1, model_2))
    try:
        material_2 = TheMaterial_2.from_other(material_1)
    except ImportError:
        raise MatmodlabError('failed to import {0}.  Material switching '
                             'requires that both materials be '
                             'built.'.format(material_1.name))

    return material_2

def build_material(model, verbosity=1):
    import subprocess
    x = os.path.join(BIN_D, 'mml')
    assert os.path.isfile(x)
    command = '{0} build -m {1}'.format(x, model)
    env = dict(os.environ)
    p = ':'.join([os.path.dirname(ROOT_D), env.get('PYTHONPATH', '')])
    env['PYTHONPATH'] = p
    proc = subprocess.Popen(command.split(), env=env)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError('material failed to build')

class keyarray(np.ndarray):
    """Array like object with members accessible by either index or key. i.e.
    param[0] or param[key], assuming 'key' is in the array

    """
    def __new__(cls, names, values):
        if len(names) != len(values):
            raise ValueError('mismatch key,val pairs')
        obj = np.asarray(values).view(cls)
        obj._map = dict(zip([s.upper() for s in names], range(len(names))))
        return obj

    def __str__(self):
        s = ', '.join('{0}={1:.2f}'.format(n, self[i]) for (i, n) in
                           enumerate(self.names))
        return '{{{0}}}'.format(s)

    @property
    def names(self):
        return sorted(self._map.keys(), key=lambda x: self._map[x])

    def index(self, key):
        try:
            # check if key is a string and in the array
            return self._map[key.upper()]
        except AttributeError:
            # key could have been an integer
            return key
        except KeyError:
            # key was a string, but not in the array
            raise KeyError('{0!r} is not in keyarray'.format(key))

    def __getitem__(self, key):
        return super(keyarray, self).__getitem__(self.index(key))

    def __setitem__(self, key, value):
        super(keyarray, self).__setitem__(self.index(key), value)

    def __array_finalize__(self, obj):
        self._map = getattr(obj, '_map', None)

def grouper(seq, n=8):
    """
    >>> list(grouper(3, 'ABCDEFG'))
    [['A', 'B', 'C'], ['D', 'E', 'F'], ['G']]
    """
    import itertools
    seq = iter(seq)
    x = iter(lambda: list(itertools.islice(seq, n)), [])
    return list(x)
