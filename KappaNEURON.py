import neuron
import neuron.rxd as nr
import neuron.rxd.rxd as nrr
import neuron.rxd.species
import neuron.rxd.rxdmath
import neuron.rxd.node
from neuron.rxd.generalizedReaction import GeneralizedReaction
import weakref
import random

import SpatialKappa
from py4j.protocol import * 

from scipy.stats import poisson
import numpy
import re
import os, sys
import warnings

verbose = False
def report(mess):
    global verbose
    if (verbose):
        print(mess)
_kappa_schemes = []

def _register_kappa_scheme(r):
    # TODO: should we search to make sure that (a weakref to) r hasn't already been added?
    global _kappa_schemes
    _kappa_schemes.append(weakref.ref(r))

def _unregister_kappa_scheme(weakref_r):
    global _kappa_schemes
    _kappa_schemes.remove(weakref_r)

def _kn_init(): 
    nrr._init()
    global _kappa_schemes
    # update Kappa schemes
    for kptr in _kappa_schemes:
        k = kptr()
        if k is not None: k.re_init()

#
# register the initialization handler and the advance handler
#
nrr._fih = neuron.h.FInitializeHandler(_kn_init)

mode = 'lumped_influx' # 'continuous_influx'

_db = None

def _kn_fixed_step_solve(raw_dt):
    if (mode == 'lumped_influx'):
        _kn_fixed_step_solve_lumped_influx(raw_dt)
    else:
        _kn_fixed_step_solve_continuous_influx(raw_dt)

## Override the NEURON nonvint _fixed_step_solve callback   
def _kn_fixed_step_solve_lumped_influx(raw_dt):
    global _kappa_schemes
    
    report("---------------------------------------------------------------------------")
    report("FIXED STEP SOLVE. NEURON time %f" % nrr.h.t)
    report("states")

    # allow for skipping certain fixed steps
    # warning: this risks numerical errors!
    fixed_step_factor = nrr.options.fixed_step_factor
    nrr._fixed_step_count += 1
    if nrr._fixed_step_count % fixed_step_factor: return
    dt = fixed_step_factor * raw_dt
    
    # TODO: this probably shouldn't be here
    if nrr._diffusion_matrix is None and nrr._euler_matrix is None: nrr._setup_matrices()

    states = nrr._node_get_states()[:]
    report(states)

    report("flux b")
    ## DCS: This gets fluxes (from ica, ik etc) and computes changes
    ## due to reactions

    ## DCS FIXME: This is different from the old rxd.py file - need check what
    ## the difference is
    b = nrr._rxd_reaction(states) - nrr._diffusion_matrix * states
    report(b)
    
    dim = nrr.region._sim_dimension
    if dim is None:
        return
    elif dim == 1:
        states[:] += nrr._reaction_matrix_solve(dt, states, nrr._diffusion_matrix_solve(dt, dt * b))

        ## Go through each kappa scheme. The region belonging to each
        ## kappa scheme should not overlap with any other kappa scheme's
        ## region.
        volumes = nrr.node._get_data()[0]
        for kptr in _kappa_schemes:
            k = kptr()

            ## Now we want add any fluxes to the kappa sims and update the
            ## quantities seen in NEURON.

            ## There is one kappa_sim for each active region in the kappa
            ## scheme.

            report("\nRUN 0.5 KAPPA STEP")
            for kappa_sim in k._kappa_sims:
                kappa_sim.runForTime(dt/2, False)      # Second argument is "time per

            ## This should work for multiple species working, but has only
            ## been tested for ca
            report("\nADDING FLUXES TO KAPPA")
            for  sptr in k._involved_species:
                s = sptr()
                name = s.name
                report("ION: %s" % (name))
                for kappa_sim, i in zip(k._kappa_sims, k._indices_dict[s]):
                    ## Number of ions
                    ## Flux b has units of mM/ms
                    ## Volumes has units of um3
                    ## _conversion factor has units of molecules mM^-1 um^-3
                    mu = dt * b[i] * nrr._conversion_factor * volumes[i]
                    nions = 0.0
                    if mu!=0:
                        nions = numpy.sign(mu)*poisson.rvs(abs(mu))
                    report("index %d; volume: %f ; flux %f ; # of ions: %s" % (i, volumes[i], b[i], nions))
                    kappa_sim.addAgent(name, nions)
                    t_kappa = kappa_sim.getTime()
                    discrepancy = nrr.h.t - t_kappa
                    report('Kappa Time %f; NEURON time %f; Discrepancy %f' % (t_kappa, nrr.h.t, discrepancy))


            report("\nRUN 0.5 KAPPA STEP")  
            for kappa_sim in k._kappa_sims:
                kappa_sim.runForTime(dt/2, False)      # Second argument is "time per
                t_kappa = kappa_sim.getTime()
                discrepancy = nrr.h.t - t_kappa + dt/2
                report('Kappa Time %f; NEURON time %f; Discrepancy %f' % (t_kappa, nrr.h.t, discrepancy))
                ## This code is commented out because it doesn't work
                ## if run_free has been used; this makes NEURON and
                ## SpatialKappa time go out of sync
                ## 
                ## if (abs(discrepancy) > 1e-3):
                ##     raise NameError('NEURON time (%f) does not match Kappa time (%f). Discrepancy = %f ' % (nrr.h.t + dt/2, t_kappa, discrepancy))

            ## Update states
            for  sptr in k._involved_species:
                s = sptr()
                name = s.name
                for kappa_sim, i in zip(k._kappa_sims, k._indices_dict[s]):
                    states[i] = kappa_sim.getObservation(name) \
                        /(nrr._conversion_factor * volumes[i])

        report("Updated states")
        report(states)

        # clear the zero-volume "nodes"
        states[nrr._zero_volume_indices] = 0

        # TODO: refactor so this isn't in section1d... probably belongs in node
        nrr._section1d_transfer_to_legacy()
    elif dim == 3:
        # the actual advance via implicit euler
        n = len(states)
        m = _scipy_sparse_eye(n, n) - dt * _euler_matrix
        # removed diagonal preconditioner since tests showed no improvement in convergence
        result, info = _scipy_sparse_linalg_bicgstab(m, dt * b)
        assert(info == 0)
        states[:] += result

        for sr in nrr._species_get_all_species().values():
            s = sr()
            if s is not None: s._transfer_to_legacy()
    
    t = nrr.h.t + dt
    sys.stdout.write("\rTime = %12.5f/%5.5f [%3.3f%%]" % (t, neuron.h.tstop, t/neuron.h.tstop*100))
    if (abs(t - neuron.h.tstop) < 1E-6):
        sys.stdout.write("\n")
    sys.stdout.flush()

## Override the NEURON nonvint _fixed_step_solve callback   
def _kn_fixed_step_solve_continuous_influx(raw_dt):
    global _kappa_schemes, _db
    print _db
    
    report("---------------------------------------------------------------------------")
    report("FIXED STEP SOLVE. NEURON time %f" % nrr.h.t)
    report("states")

    # allow for skipping certain fixed steps
    # warning: this risks numerical errors!
    fixed_step_factor = nrr.options.fixed_step_factor
    nrr._fixed_step_count += 1
    if nrr._fixed_step_count % fixed_step_factor: return
    dt = fixed_step_factor * raw_dt
    
    # TODO: this probably shouldn't be here
    if nrr._diffusion_matrix is None and nrr._euler_matrix is None: nrr._setup_matrices()

    states = nrr._node_get_states()[:]
    report(states)

    report("flux b")
    ## DCS: This gets fluxes (from ica, ik etc) and computes changes
    ## due to reactions

    ## DCS FIXME: This is different from the old rxd.py file - need check what
    ## the difference is
    b = nrr._rxd_reaction(states) - nrr._diffusion_matrix * states
    report(b)
    
    dim = nrr.region._sim_dimension
    if dim is None:
        return
    elif dim == 1:
        #############################################################################
        ## 1. Pass all relevant continous variables to the rule-based simulator
        ##
        ## Relevant variables might be
        ## * Calcium current (for deterministic channels)
        ## * Membrane potential (for stochastic channels controlled by Kappa model)
        #############################################################################

        ## Go through each kappa scheme. The region belonging to each
        ## kappa scheme should not overlap with any other kappa scheme's
        ## region.
        volumes = nrr.node._get_data()[0]
        for kptr in _kappa_schemes:
            k = kptr()
            report("\nPASSING FLUXES TO KAPPA")
            for  sptr in k._involved_species:
                s = sptr()
                if (s.charge != 0):
                    name = s.name
                    report("ION: %s" % (name))
                    for kappa_sim, i in zip(k._kappa_sims, k._indices_dict[s]):
                        ## Number of ions
                        ## Flux b has units of mM/ms
                        ## Volumes has units of um3
                        ## _conversion factor has units of molecules mM^-1 um^-3
                        flux = b[i] * nrr._conversion_factor * volumes[i]
                        kappa_sim.setTransitionRate('Create %s' % (s.name), flux)
                        ## kappa_sim.setVariable(flux, 'f%s' % (s.name))

            report("\nPASSING MEMBRANE POTENTIAL TO KAPPA")
            ## TODO: pass membrane potential to kappa

        #############################################################################
        ## 2. Run the rule-based simulator from t to t + dt
        #############################################################################
        #############################################################################
        ## 3. Compute the net change Delta Stot in the number of each
        ## bridging species S and convert back into a current.
        #############################################################################
        #############################################################################
        ## 4. Set the corresponding elements of the flux to the
        ## currents computed in step 3
        #############################################################################

        for kptr in _kappa_schemes:
            k = kptr()

            ## Recording total starting value of each species
            Stot0 = {}
            for sptr in k._involved_species:
                s = sptr()
                if (s.charge != 0):
                    Stot0[s.name] = {}
                    for kappa_sim, i in zip(k._kappa_sims, k._indices_dict[s]):
                        Stot0[s.name][i] = kappa_sim.getVariable('Total %s' % (s.name))

            report("\nRUN 1 KAPPA STEP")  
            for kappa_sim in k._kappa_sims:
                kappa_sim.runForTime(dt, False)     
                t_kappa = kappa_sim.getTime()

            ## Recording total ending value of each species
            for sptr in k._involved_species:
                s = sptr()
                for kappa_sim, i in zip(k._kappa_sims, k._indices_dict[s]):
                    ## For ions, compute the current
                    if (s.charge != 0):
                        Stot1 = kappa_sim.getVariable('Total %s' % (s.name))
                        DeltaStot = Stot1 - Stot0[s.name][i]
                        bnew = DeltaStot/(dt*nrr._conversion_factor*volumes[i])
                        _db[i] = bnew - b[i]
                        print "Change in current:", _db[i]
                        b[i] = bnew

        report("Updated states")
        report(states)
        
        #############################################################################
        ## 5. Update the continous variables according to the update step
        #############################################################################
        states[:] += nrr._reaction_matrix_solve(dt, states, nrr._diffusion_matrix_solve(dt, dt * b))

        #############################################################################
        ## 6. Voltage step overrides states, possibly making them negative so put back actual states
        #############################################################################
        for kptr in _kappa_schemes:
            k = kptr()
            ## Recording total ending value of each species
            for sptr in k._involved_species:
                s = sptr()
                for kappa_sim, i in zip(k._kappa_sims, k._indices_dict[s]):
                    ## Update concentration
                    states[i] = kappa_sim.getObservation(s.name) \
                                /(nrr._conversion_factor * volumes[i])

        # clear the zero-volume "nodes"
        states[nrr._zero_volume_indices] = 0

        # TODO: refactor so this isn't in section1d... probably belongs in node
        nrr._section1d_transfer_to_legacy()
    elif dim == 3:
        # the actual advance via implicit euler
        n = len(states)
        m = _scipy_sparse_eye(n, n) - dt * _euler_matrix
        # removed diagonal preconditioner since tests showed no improvement in convergence
        result, info = _scipy_sparse_linalg_bicgstab(m, dt * b)
        assert(info == 0)
        states[:] += result

        for sr in nrr._species_get_all_species().values():
            s = sr()
            if s is not None: s._transfer_to_legacy()
    
    t = nrr.h.t + dt
    sys.stdout.write("\rTime = %12.5f/%5.5f [%3.3f%%]" % (t, neuron.h.tstop, t/neuron.h.tstop*100))
    if (abs(t - neuron.h.tstop) < 1E-6):
        sys.stdout.write("\n")
    sys.stdout.flush()


nrr._callbacks[4] = _kn_fixed_step_solve

def _kn_currents(rhs):
    global _db
    if _db is None:
        _db = nrr._numpy_zeros(len(rhs))
        print "CREATING _db", _db

    nrr._currents(rhs)
    # global nrr._rxd_induced_currents
    print "adding some noise"
    sign = 1
    cur = random.random()/10
    ## This line alters ica, but does not affect the voltage
    ## nrr._curr_ptrs[0][0] += -sign * cur
    ## This line is necessary to change the voltage
    rhs[1] -= _db[1]
    # Is this line needed?
    # nrr._rxd_induced_currents[0] -= _db[1]
    # print nrr._rxd_induced_currents

nrr._callbacks[2] = _kn_currents

gateway = None

class Kappa(GeneralizedReaction):
    def __init__(self, species, kappa_file, regions=None, membrane_flux=False, time_units='ms', verbose=False):
        """create a kappa mechanism linked to a species on a given region or set of regions
        if regions is None, then does it on all regions"""
        global gateway
        self._kappa_sims = []
        self._species = []
        for s in species:
            self._species.append(weakref.ref(s))
            if s.initial is None:
                s.initial = 0
                warnings.warn('Initial concentration of %s not specified; setting to zero' % (s.name), UserWarning)
        ## self._species = weakref.ref(species)
        self._involved_species = self._species
        self._kappa_file = os.path.join(os.getcwd(), kappa_file)
        if not hasattr(regions, '__len__'):
            regions = [regions]
        self._regions = regions
        self._active_regions = []
        self._trans_membrane = False
        self._membrane_flux = membrane_flux
        self._time_units = 'ms'
        self._time_units = time_units
        self._verbose = verbose
        if membrane_flux not in (True, False):
            raise Exception('membrane_flux must be either True or False')
        if membrane_flux and regions is None:
            # TODO: rename regions to region?
            raise Exception('if membrane_flux then must specify the (unique) membrane regions')
        self._update_indices()
        print('Registering kappa scheme')
        _register_kappa_scheme(self)
        print _kappa_schemes
        self._weakref = weakref.ref(self) # Seems to be needed for the destructor
    
    def __repr__(self):
        return 'Kappa(%r, kappa_file=%r, regions=%r, membrane_flux=%r)' % (self._involved_species, self._kappa_file, self._regions, self._membrane_flux)
    
    def __del__(self):
        ## A similar idiom to rxd._register_kappa_scheme() doesn't seem to work
        _unregister_kappa_scheme(self._weakref)
        for kappa_sim in self._kappa_sims:
            del(kappa_sim)

    def _update_indices(self):
        global gateway

        # this is called anytime the geometry changes as well as at init
        
        self._indices_dict = {}
        
        # locate the regions containing all species (including the one
        # that channges)

        active_regions = self._regions
        for sptr in self._involved_species:
            s = sptr()
            if s:
                for r in self._regions:
                    if r in active_regions and not s.indices(r):
                        del active_regions[active_regions.index(r)]
            else:
                active_regions = []
        
        # store the indices
        for sptr in self._involved_species:
            s = sptr()
            self._indices_dict[s] = sum([s.indices(r) for r in active_regions], [])
        ## Check that each species has the same number of elements
        if (len(set([len(self._indices_dict[s()]) for s in self._involved_species])) != 1):
            raise Exception('Different numbers of indices for various species') 
        self._active_regions = active_regions

        ## Create the kappa simulations
        if not gateway:
            gateway = SpatialKappa.SpatialKappa()

        self._kappa_sims = []   # Will this destroy things properly?
        for index in self._indices_dict[self._involved_species[0]()]:
            print "Creating Kappa Simulation in region", r
            kappa_sim = gateway.kappa_sim(self._time_units, verbose)
            try:
                kappa_sim.loadFile(self._kappa_file)
            except Py4JJavaError as e:
                java_err = re.sub(r'java.lang.IllegalStateException: ', r'', str(e.java_exception))
                errstr = 'Error in kappa file %s: %s' % (self._kappa_file, java_err)
                raise RuntimeError(errstr)
            
            if (mode == 'continuous_influx'):
                s = self._involved_species[0]()
                ## Get description of agent
                agent = kappa_sim.getAgentMap(s.name)
                link_names = agent[s.name].keys()
                if (len(link_names) > 1):
                    errstr = 'Error in kappa file %s: Agent %s has more than one site' % (self._kappa_file, s.name)
                    raise RuntimeError()
                
                link_name = link_names[0]

                ## Add transition to create 
                kappa_sim.addTransition('Create %s' % (s.name), {}, agent, 0.0)

                ## Add variable to measure total species
                kappa_sim.addVariableMap('Total %s' % (s.name), {s.name: {link_name: {'l': '?'}}})

            self._kappa_sims.append(kappa_sim)
            ## TODO: Should we check if we are inserting two kappa schemes
            ## in the same place?
        self._mult = [1]

    def _do_memb_scales(self):
        # TODO: does anyone still call this?
        # TODO: update self._memb_scales (this is just a dummy value to make things run)
        self._memb_scales = 1


    
    def _get_memb_flux(self, states):
        if self._membrane_flux:
            raise Exception('membrane flux due to rxd.Rate objects not yet supported')
            # TODO: refactor the inside of _evaluate so can construct args in a separate function and just get self._rate() result
            rates = self._evaluate(states)[2]
            return self._memb_scales * rates
        else:
            return []

    def setVariable(self, variable, value):
        for kappa_sim in self._kappa_sims:
            kappa_sim.setVariable(float(value), variable)

    ## This is perhaps an abuse of this function, but it is called at
    ## init() time
    def re_init(self):
        volumes = nrr.node._get_data()[0]
        states = nrr.node._get_states()[:]
        for sptr in self._involved_species:
            s = sptr()
            if s:
                for kappa_sim, i in zip(self._kappa_sims, self._indices_dict[s]):
                    nions = round(states[i] \
                                  * nrr._conversion_factor * volumes[i])
                    ## print "Species ", s.name, " conc ", states[i], " nions ", nions
                    try:
                        kappa_sim.getObservation(s.name)
                    except:
                        raise NameError('There is no observable in %s called %s; add a line like this:\n%%obs: \'%s\' <complex definition> ' % (self._kappa_file, s.name, s.name))
                    try:
                        kappa_sim.setAgentInitialValue(s.name, nions)
                    except:
                        raise Error('Error setting initial value of agent %s to %d' % (s.name, nions))
                        


    def run_free(self, t_run):
        # Run free of neuron
        for kptr in _kappa_schemes:
            k = kptr()
            for kappa_sim in k._kappa_sims:
                kappa_sim.runForTime(float(t_run), True)
