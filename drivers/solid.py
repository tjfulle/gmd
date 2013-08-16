import os
import sys
import numpy as np

from __config__ import cfg
import core.kinematics as kin
import utils.tensor as tensor
from utils.tensor import NSYMM, NTENS, NVEC, I9
from utils.errors import Error1
from materials.material import create_material
from drivers.driver import Driver

np.set_printoptions(precision=2)

class SolidDriver(Driver):
    name = "solid"
    def __init__(self):
        pass

    def setup(self, runid, material, mtlprops, *opts):
        """Setup the driver object

        """
        self.runid = runid
        self.mtlmdl = create_material(material)

        # Save the unchecked parameters
        self.mtlmdl.unchecked_params = mtlprops

        # Setup and initialize material model
        self.mtlmdl.setup(mtlprops)
        self.mtlmdl.initialize()

        self.kappa, self.density = opts[:2]

        # register variables
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

        # register material variables
        self.xtra_start = self.ndata
        self.xtra_end = self.xtra_start + self.mtlmdl.nxtra
        for var in self.mtlmdl.variables():
            self.register_variable(var, vtype="SCALAR")
        setattr(self, "xtra_slice", slice(self.xtra_start, self.xtra_end))

        # allocate storage
        self.allocd()

        # initialize nonzero data
        self._data[self.defgrad_slice] = I9
        self._data[self.xtra_slice] = self.mtlmdl.initial_state()
        self._data[self.density_slice] = self.density

        return

    def process_legs(self, legs, iomgr, *args):
        """Process the legs

        Parameters
        ----------

        Returns
        -------

        """
        print "Starting calculations for simulation {0}".format(self.runid)

        kappa = self.kappa

        # initial leg
        rho = self.data("DENSITY")[0]
        xtra = self.data("XTRA")
        tleg = np.zeros(2)
        d = np.zeros(NSYMM)
        dt = 0.
        eps = np.zeros(NSYMM)
        f = np.reshape(np.eye(3), (9, 1))
        depsdt = np.zeros(NSYMM)
        sig = np.zeros(NSYMM)
        sigdum = np.zeros((2, NSYMM))

        nv = 0
        v = np.empty(nv)
        sigspec = np.empty((3, nv))
        vdum = np.zeros(6, dtype=np.int)

        # Process each leg
        nlegs = len(legs)
        lsl = len(str(nlegs))
        for leg_num, leg in enumerate(legs):

            tleg[0] = tleg[1]
            sigdum[0] = sig[:]
            if v:
                sigdum[0, v] = sigspec[1]

            tleg[1], nsteps, ltype, c, ef = leg
            delt = tleg[1] - tleg[0]
            if delt == 0.:
                continue

            nprints = 20
            print_interval = max(1, int(nsteps / nprints))
            lsn = len(str(nsteps))
            consfmt = ("leg {{0:{0}d}}, step {{1:{1}d}}, time {{2:.4E}}, "
                       "dt {{3:.4E}}".format(lsl, lsn))

            nv = 0
            for i, cij in enumerate(c):
                if ltype[i] == 1:                            # -- strain rate
                    depsdt[i] = cij

                elif ltype[i] == 2:                          # -- strain
                    depsdt[i] = (cij - eps[i]) / delt

                elif ltype[i] == 3:                          # -- stress rate
                    sigdum[1, i] = sigdum[0, i] + cij * delt
                    vdum[nv] = i
                    nv += 1

                elif ltype[i] == 4:                          # -- stress
                    sigdum[1, i] = cij
                    vdum[nv] = i
                    nv += 1

                continue

            sigspec = np.empty((3, nv))
            v = vdum[:nv]
            sigspec[:2] = sigdum[:2, v]

            t = tleg[0]
            dt = delt / nsteps

            # process this leg
            if not nv:
                # strain or strain rate prescribed and d is constant over
                # entire leg
                d = kin.deps2d(delt, kappa, eps, depsdt)

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
                    jac = self.mtlmdl.jacobian()
                    d = kin.sig2d(self.mtlmdl, dt, jac,
                                  strain[2], trg_strain,
                                  stress[2], trg_stress, v)

                # compute the current deformation gradient and strain from
                # previous values and the deformation rate
                f, eps = kin.update_deformation(dt, kappa, f, d)

                # update material state
                sigsave = np.array(sig)
                sig, xtra = self.mtlmdl.update_state(dt, d, sig, xtra)

                # -------------------------- quantities derived from final state
                eqeps = np.sqrt(2. / 3. * (np.sum(eps[:3] ** 2)
                                           + 2. * np.sum(eps[3:] ** 2)))
                epsv = np.sum(eps[:3])
                rho = rho * np.exp(-np.sum(d[:3]) * dt)

                pres = -np.sum(sig[:3]) / 3.
                dstress = (sig - sigsave) / dt

                # advance all data after updating state
                self.setvars(stress=sig, strain=eps, defgrad=f,
                             symm_l=d, efield=ef, eqstrain=eqeps,
                             vstrain=epsv, density=rho, pressure=pres,
                             dstress=dstress, xtra=xtra)


                # --- write state to file
                endstep = abs(t - tleg[1]) / tleg[1] < 1.E-12
                if (nsteps - n) % print_interval == 0 or endstep:
                    iomgr(dt, t)

                if cfg.verbosity and (n == 0 or round(nsteps / 2.) == n or endstep):
                    print consfmt.format(leg_num, n + 1, t, dt)

                continue  # continue to next step

            continue # continue to next leg


        return 0