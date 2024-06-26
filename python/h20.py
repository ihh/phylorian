import jax
import jax.numpy as jnp

from jax import grad, value_and_grad
from jax.scipy.special import gammaln, logsumexp
from jax.scipy.linalg import expm

from functools import partial
from jax import jit

from diffrax import diffeqsolve, ODETerm, Dopri5, PIDController, ConstantStepSize, SaveAt

import logging

# We replace zeroes and infinities with small numbers sometimes
# It's sinful but that's life for you
min_float32 = jnp.finfo('float32').min
smallest_float32 = jnp.finfo('float32').smallest_normal

# calculate L, M
def lm (t, rate, prob):
  return jnp.exp (-rate * t / (1. - prob))

def indels (t, rate, prob):
  return 1. / lm(t,rate,prob) - 1.

# calculate derivatives of (a,b,u,q)
def derivs (t, counts, indelParams):
  lam,mu,x,y = indelParams
  a,b,u,q = counts
  L = lm (t, lam, x)
  M = lm (t, mu, y)
  num = mu * (b*M + q*(1.-M))
  unsafe_denom = M*(1.-y) + L*q*y + L*M*(y*(1.+b-q)-1.)
  denom = jnp.where (unsafe_denom > 0., unsafe_denom, 1.)   # avoid NaN gradient at zero
  one_minus_m = jnp.where (M < 1., 1. - M, smallest_float32)   # avoid NaN gradient at zero
  d = jnp.where (unsafe_denom > 0.,
                  jnp.array (((mu*b*u*L*M*(1.-y)/denom - (lam+mu)*a,
                                -b*num*L/denom + lam*(1.-b),
                                -u*num*L/denom + lam*a,
                                ((M*(1.-L)-q*L*(1.-M))*num/denom - q*lam/(1.-y))/one_minus_m))),
                  jnp.array ((-lam-mu,lam,lam,0.)))
#  jax.debug.print("t={t} counts={counts} indelParams={indelParams} L={L} M={M} num={num} denom={denom} one_minus_m={one_minus_m} d={d}", t=t, counts=counts, indelParams=indelParams, L=L, M=M, num=num, denom=denom, one_minus_m=one_minus_m, d=d)
  return d

# calculate counts (a,b,u,q) by numerical integration
def initCounts(indelParams):
    return jnp.array ((1., 0., 0., 0.))
    
# Runge-Kutte (RK4) numerical integration routine
def integrateCounts_RK4 (t, indelParams, /, steps=100, ts=None, **kwargs):
  lam,mu,x,y = indelParams
  debug = kwargs.get('debug',0)
  def RK4body (y, t_dt):
    t, dt = t_dt
    k1 = derivs(t, y, indelParams)
    k2 = derivs(t+dt/2, y + dt*k1/2, indelParams)
    k3 = derivs(t+dt/2, y + dt*k2/2, indelParams)
    k4 = derivs(t+dt, y + dt*k3, indelParams)
    y_next = y + dt*(k1 + 2*k2 + 2*k3 + k4)/6
    if debug:
        if debug > 1:
            print(f"t={t} dt={dt} y_next={y_next.tolist()} y={y.tolist()} k1={k1.tolist()} k2={k2.tolist()} k3={k3.tolist()} k4={k4.tolist()}")
        else:
            print(f"t={t} dt={dt} y_next={y_next}")
    return y_next, y_next
  y0 = initCounts (indelParams)
  gmrate = 1 / (1/lam + 1/mu)
  if ts is None:
    dt0 = jnp.minimum (t/steps, 1/gmrate)
    ts = jnp.geomspace (dt0, t, num=steps)
    ts = jnp.concatenate ([jnp.array([0]), ts])
  assert len(ts) > 0
  dts = jnp.ediff1d (ts)
  y1, ys = jax.lax.scan (RK4body, y0, (ts[:-1],dts))
# jax.lax.scan is equivalent to...
#  y1 = y0
#  ys = []
#  for t,dt in zip(ts[0:-1],dts):
#    y1, y_out = RK4body (y1, (t,dt))
#    ys.append(y_out)
  return y1, jnp.concatenate([jnp.array([y0]),ys],axis=0), ts

# calculate counts (a,b,u,q) by numerical integration using diffrax
def integrateCounts_diffrax (t, indelParams, /, step = None, rtol = 1e-3, atol = 1e-3, ts = None, **kwargs):
  term = ODETerm(derivs)
  solver = Dopri5()
  if step is None and rtol is None and atol is None:
      raise Exception ("please specify step, rtol, or atol")
  if step is not None:
      stepsize_controller = ConstantStepSize()
  else:
      stepsize_controller = PIDController (rtol, atol)
  y0 = initCounts(indelParams)
  sol = diffeqsolve (term, solver, 0., t, step, y0, args=indelParams,
                     stepsize_controller=stepsize_controller,
                     **({'saveat': SaveAt(t0=False,t1=False,ts=ts)} if ts is not None else {}),
                     **kwargs)
#  jax.debug.print('ts={ts} ts.shape={ts.shape} sol.ts={sol.ts} sol.ts.shape={sol.ts.shape} sol.ys={sol.ys}', sol=sol, ts=ts)
  return sol.ys[-1], sol.ys, sol.ts

integrateCounts = integrateCounts_diffrax
#integrateCounts = integrateCounts_RK4

# test whether time is past threshold of alignment signal being undetectable
def alignmentIsProbablyUndetectable (t, indelParams, alphabetSize = 20):
    lam,mu,x,y = indelParams
    expectedMatchRunLength = 1. / (1. - jnp.exp(-mu*t))
    expectedInsertions = indels(t,lam,x)
    expectedDeletions = indels(t,mu,y)
    kappa = 2.
    return jnp.where (t > 0.,
                      ((expectedInsertions + 1) * (expectedDeletions + 1)) > kappa * (alphabetSize ** expectedMatchRunLength),
                      False)

# initial transition matrix
def zeroTimeTransitionMatrix (indelParams):
  lam,mu,x,y = indelParams
  return jnp.array ([[1.,0.,0.],
                     [1.-x,x,0.],
                     [1.-y,0.,y]])

# convert counts (a,b,u,q) to transition matrix ((a,b,c),(f,g,h),(p,q,r))
def smallTimeTransitionMatrix (t, indelParams, /, **kwargs):
    lam,mu,x,y = indelParams
#    jax.debug.print("t={t} lam={lam} mu={mu} x={x} y={y}", t=t, lam=lam, mu=mu, x=x, y=y)
    abuq, _abuq_by_t, _ts = integrateCounts(t,indelParams,**kwargs)
    return transitionMatrixFromCounts (t, indelParams, abuq, **kwargs)

def transitionMatrixFromCounts (t, indelParams, counts, /, **kwargs):
    lam,mu,x,y = indelParams
    a,b,u,q = tuple(jnp.squeeze(x,axis=-1) for x in jnp.split (jnp.array(counts), indices_or_sections=4, axis=-1))
#    jax.debug.print("t={t} lam={lam} mu={mu} x={x} y={y} a={a} b={b} u={u} q={q}", t=t, lam=lam, mu=mu, x=x, y=y, a=a, b=b, u=u, q=q)
    L = lm(t,lam,x)
    M = lm(t,mu,y)
    one_minus_L = jnp.where (L < 1., 1. - L, smallest_float32)   # avoid NaN gradient at zero
    one_minus_M = jnp.where (M < 1., 1. - M, smallest_float32)   # avoid NaN gradient at zero
    mx = jnp.stack ([jnp.stack ([a,b,1-a-b]),
                     jnp.stack ([u*L/one_minus_L,1-(b+q*(1-M)/M)*L/one_minus_L,(b+q*(1-M)/M-u)*L/one_minus_L]),
                     jnp.stack ([(1-a-u)*M/one_minus_M,q,1-q-(1-a-u)*M/one_minus_M])])
    if kwargs.get('norm',True):
        mx = jnp.maximum (0, mx)
        mx = mx / jnp.sum (mx, axis=-1, keepdims=True)
#    jax.debug.print ("mx={mx}", mx=mx)
    return mx

# get limiting transition matrix for large times
def largeTimeTransitionMatrix (t, indelParams):
    lam,mu,x,y = indelParams
    g = 1. - lm(t,lam,x)
    r = 1. - lm(t,mu,y)
    return jnp.array ([[(1-g)*(1-r),g,(1-g)*r],
                       [(1-g)*(1-r),g,(1-g)*r],
                       [(1-r),jnp.zeros_like(t),r]])

# get transition matrix for any given time
tMin = 1e-3
def transitionMatrix (t, indelParams, /, alphabetSize=20, **kwargs):
    lam,mu,x,y = indelParams
    tSafe = jnp.maximum (t, tMin)
    return jnp.where (t > 0.,
                      jnp.where (alignmentIsProbablyUndetectable(tSafe,indelParams,alphabetSize),
                                 largeTimeTransitionMatrix(tSafe,indelParams),
                                 smallTimeTransitionMatrix(tSafe,indelParams,**kwargs)),
                      zeroTimeTransitionMatrix(indelParams))

# get dummy root transition matrix
def dummyRootTransitionMatrix():
  return jnp.array ([[0,1,0],[1,1,0],[1,0,0]])
