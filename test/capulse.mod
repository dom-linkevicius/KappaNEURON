TITLE L-type calcium channel with low threshold for activation
: used in somatic and proximal dendritic regions 
: it calculates I_Ca using channel permeability instead of conductance

UNITS {
	  (mA) = (milliamp)
	  (mV) = (millivolt)
    (molar) = (1/liter)
    (mM) = (millimolar)
    FARADAY = (faraday) (coulomb)
    R = (k-mole) (joule/degC)
}

PARAMETER {		:parameters that can be entered when function is called in cell-setup 
	  v             (mV)
	  celsius = 34	(degC)
	  gcalbar = 1   (mho/cm2)   : initialized conductance
	  cai = 5.e-5   (mM)        : initial internal Ca++ concentration
	  cao = 2       (mM)        : initial external Ca++ concentration
    eca = 140     (mV)        : Ca++ reversal potential
}

NEURON {
	  SUFFIX capulse
	  USEION ca READ cai,cao,eca WRITE ica
    RANGE gcalbar, gcal
}

ASSIGNED {                       : parameters needed to solve DE
	  ica   (mA/cm2)
    gcal  (mho/cm2)
}

INITIAL {                        : initialize the following parameter using rates()
	  gcal = 0
}

BREAKPOINT {
    gcal = 0
    if ((t > 1) && (t < 2)) {
        gcal = gcalbar
    }
	  ica = gcal*ghk(v,cai,cao): calcium current induced by this channel
    : ica = gcal*(v - eca): calcium current induced by this channel
}

FUNCTION ghk(v(mV), ci(mM), co(mM)) (mV) {
    LOCAL nu,f
    f = KTF(celsius)/2
    nu = v/f
    ghk=-f*(1. - (ci/co)*exp(nu))*efun(nu)
}

FUNCTION KTF(celsius (degC)) (mV) { : temperature-dependent adjustment factor
    KTF = ((25.(mV)/293.15(degC))*(celsius + 273.15(degC)))
}

FUNCTION efun(z) {
	  if (fabs(z) < 1e-4) {
		    efun = 1 - z/2
	  }else{
		    efun = z/(exp(z) - 1)
	  }
}