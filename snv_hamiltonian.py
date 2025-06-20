'''
Copyright (c) <2025> <copyright Gregor Pieplow, Joseph H.D. Munns>

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
'''
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.gridspec as gsp

from scipy.constants import hbar, e, m_e, angstrom, epsilon_0, speed_of_light, elementary_charge
from scipy.constants import Boltzmann as kB
from scipy.constants import c as sol

import copy as cp
import os 

from numba import njit
import time as tm
from scipy.integrate import odeint

pi = np.pi
mu_B = .5 * e * hbar / m_e

# some unit conversion factors
nano = 10 ** (-9)
tera = 10 ** (12)
pico = 10 ** (-12)

# some system properties
life_time = 5.5 * nano
n_refraction = 2.4

# helper functions
def get_eigs(matrix):
    _w, _v = np.linalg.eigh(matrix)
    _i = np.argsort(np.real(_w))
    return _w[_i], _v[:, _i]

def make_B_vector(magnitude, coordinate_vector):
    """
        Note that the coordinate vector is defined in terms of the symmetry axes of the defect *NOT* the host crystal lattice-vectors.
    """
    vec = np.array(coordinate_vector)
    nrm = np.sum(np.abs(vec) ** 2.)
    return magnitude * vec / nrm

def coord_to_lattice(coord_vector):
    _vc = np.array(coord_vector)
    _ex = (1. / np.sqrt(6)) * np.array([1, -2, 1])
    _ey = (1. / np.sqrt(2)) * np.array([1, 0, -1])
    _ez = (1. / np.sqrt(3)) * np.array([1, 1, 1])
    _lattice_basis = np.array([_ex, _ey, _ez])
    return np.dot(_lattice_basis.transpose(), _vc)

def lattice_to_coord(lattice_vector):
    _vc = np.array(lattice_vector).transpose()
    _ex = (1. / np.sqrt(6)) * np.array([1, -2, 1])
    _ey = (1. / np.sqrt(2)) * np.array([1, 0, -1])
    _ez = (1. / np.sqrt(3)) * np.array([1, 1, 1])
    _lattice_basis = np.array([_ex, _ey, _ez])  
    return np.dot(_lattice_basis, _vc)

# definition functions for model
def H_SO(lambda_SO):
    """accepts SO term depending on ground/excited state"""
    _a = 1.j * lambda_SO / 2.
    return np.array([[0, 0, -_a, 0],
                     [0, 0, 0, +_a],
                     [+_a, 0, 0, 0],
                     [0, -_a, 0, 0]])


def H_JT(xi_xy):
    """ accepts 2-vector xi=[xi_x, xi_y]"""
    _x, _y = xi_xy
    return np.array([[+_x, 0, +_y, 0],
                     [0, +_x, 0, +_y],
                     [+_y, 0, -_x, 0],
                     [0, +_y, 0, -_x]])


def H_ZL(B_vec, gammaL, f):
    """
        accepts
            f -
            B - vector for B field B=[Bx, By, Bz]
            gammaL -
    """
    iBx, iBy, iBz = 1.j * B_vec
    return f * gammaL * np.array([[0, 0, +iBz, 0],
                                  [0, 0, 0, +iBz],
                                  [-iBz, 0, 0, 0],
                                  [0, -iBz, 0, 0]])


def H_ZS(B_vec, gammaS):
    """
        accepts
            f -
            B - vector for B field B=[Bx, By, Bz]
            gammaL -
    """
    _Bx, _By, _Bz = B_vec
    _Bp = _Bx + 1.j * _By
    _Bm = _Bx - 1.j * _By
    return gammaS * np.array([[+_Bz, +_Bm, 0, 0],
                              [+_Bp, -_Bz, 0, 0],
                              [0, 0, +_Bz, +_Bm],
                              [0, 0, +_Bp, -_Bz]])


def H_st(alpha, beta, delta):
    """
        accepts scaling factors for SnV strain response
            alpha - E_gx
            beta - E_gy
            delta - A_1g
    """
    _ad0 = alpha - delta  
    _ad1 = -alpha - delta
    _bt = beta
    return np.array([[_ad0, 0, _bt, 0],
                     [0, _ad0, 0, _bt],
                     [_bt, 0, _ad1, 0],
                     [0, _bt, 0, _ad1]])

def dipole_matel():
    _px = np.array([[+1, 0, 0, 0, ],
                    [0, +1, 0, 0, ],
                    [0, 0, -1, 0, ],
                    [0, 0, 0, -1, ],] , dtype='complex')
    _py = np.array([[0, 0, -1, 0, ],
                    [0, 0, 0, -1, ],
                    [-1, 0, 0, 0, ],
                    [0, -1, 0, 0, ], ], dtype='complex')
    _pz = np.array([[+1, 0, 0, 0, ],
                    [0, +1, 0, 0, ],
                    [0, 0, +1, 0, ],
                    [0, 0, 0, +1, ], ], dtype='complex') * 2
    return {_k: np.array(_v) for _k, _v in zip(('x', 'y', 'z'), (_px, _py, _pz))}

def manifold(HT, D):
    _Wu, _Vu = get_eigs(HT['u'])
    _Wg, _Vg = get_eigs(HT['g'])

    _Mvu, _Mvg = (np.array(_m) for _m in (_Vu, _Vg))

    _p = {_axis: np.dot(_Mvg.conjugate().transpose(), np.dot(D[_axis], _Mvu)) for _axis in ('x', 'y', 'z')}
    
    return {'eigs': {'g': _Wg, 'u': _Wu}, 'vecs': {'g': _Mvg, 'u': _Mvu}, 'dips': _p}

def manifold_small(HT, D):
    _Wu, _Vu = get_eigs(HT['u'])
    _Wg, _Vg = get_eigs(HT['g'])

    _Mvu, _Mvg = (np.array(_m) for _m in (_Vu, _Vg))

    _p = {_axis: np.dot(_Mvg.conjugate().transpose(), np.dot(D[_axis], _Mvu))[::2,::2] for _axis in ('x', 'y', 'z')}
    return {'eigs': {'g': _Wg[::2], 'u': _Wu[::2]}, 'vecs': {'g': _Mvg[::2], 'u': _Mvu[::2]}, 'dips': _p}


# SAMPLE PARAMETERS -- from [TrushM20], ...units THz * rad
# Spin orbit

lambda_g = 2 * pi * 0.815
lambda_u = 2 * pi * 2.355

# Jahn-Teller

xi_xg = 2 * pi * 0.065
xi_yg = 2 * pi * 0.
xi_xu = 2 * pi * 0.855
xi_yu = 2 * pi * 0.

# Zee
f_g = 0.15
f_u = 0.15

gamma_L = mu_B / hbar * 1.e-12
# Zeeman - spin
gamma_S =  2 * gamma_L

# Strain
alpha_g = 2 * pi * -0.238
beta_g = 2 * pi * +0.238
delta_g = 2 * pi * 0.
alpha_u = 2 * pi * -0.076
beta_u = 2 * pi * -0.07
delta_u = 2 * pi * 0.

ZPL = 2 * pi * 484.32

# Ground state hamiltonian

_Hg0 = H_SO(lambda_g) + H_JT([xi_xg, xi_yg])
_HgL = lambda _B, _s: (_Hg0
                       + H_ZL(_B, gamma_L, f_g)
                       + H_ZS(_B, gamma_S)
                       + (H_st(alpha_g, beta_g, delta_g) if _s else 0)
                       )

# Excited state hamiltonian

_Hu0 = H_SO(lambda_u) + H_JT([xi_xu, xi_yu])
_HuL = lambda _B, _s: (_Hu0
                       + H_ZL(_B, gamma_L, f_u)
                       + H_ZS(_B, gamma_S)
                       + (H_st(alpha_u, beta_u, delta_u) if _s else 0)
                       )

s = False
e_vu = []
e_vg = []

def snv_hamiltonian(_b, _s, **kwargs):
    b_vec = kwargs.get('b_vec', [0,0,1])
    _H0 = np.zeros((8, 8), dtype=complex)
    B = make_B_vector(_b, lattice_to_coord(b_vec))
    _HgT = np.array(_HgL(B, s))
    _HuT = np.diag([ZPL for i in range(4)]) + np.array(_HuL(B, s))
    _H0[0:4, 0:4] = _HuT
    _H0[4:8, 4:8] = _HgT
    return _H0

def system(_b, _s, **kwargs):

    b_vec = kwargs.get('b_vec', [0,0,1])
    P = dipole_matel()

    Px = np.zeros((8, 8), dtype=complex)
    Py = np.zeros((8, 8), dtype=complex)
    Pz = np.zeros((8, 8), dtype=complex)
    B = make_B_vector(_b, b_vec)

    _HgT = np.array(_HgL(B, s))
    _HuT = np.diag([ZPL for i in range(4)]) + np.array(_HuL(B, s))
    _sys = manifold({'g': _HgT, 'u': _HuT}, P)
    
    Px[0:4, 4:8] = _sys['dips']['x']
    Px[4:8, 0:4] = _sys['dips']['x'].conjugate().transpose()

    Py[0:4, 4:8] = _sys['dips']['y']
    Py[4:8, 0:4] = _sys['dips']['y'].conjugate().transpose()

    Pz[0:4, 4:8] = _sys['dips']['z']
    Pz[4:8, 0:4] = _sys['dips']['z'].conjugate().transpose()

    return {'sys': _sys, 'px' : Px, 'py' : Py, 'pz' : Pz}

def system_small(_b, _s): 

    P = dipole_matel()

    Px = np.zeros((4, 4), dtype=complex)
    Py = np.zeros((4, 4), dtype=complex)
    Pz = np.zeros((4, 4), dtype=complex)

    B = make_B_vector(_b,[0, 0, 1])

    _HgT = np.array(_HgL(B, s))
    _HuT = np.diag([ZPL for i in range(4)]) + np.array(_HuL(B, s))

    _sys = manifold_small({'g': _HgT, 'u': _HuT}, P)

    Px[0:2, 2:4] = _sys['dips']['x']
    Px[2:4, 0:2] = _sys['dips']['x'].conjugate().transpose()

    Py[0:2, 2:4] = _sys['dips']['y']
    Py[2:4, 0:2] = _sys['dips']['y'].conjugate().transpose()

    Pz[0:2, 2:4] = _sys['dips']['z']
    Pz[2:4, 0:2] = _sys['dips']['z'].conjugate().transpose()

    return {'sys': _sys, 'px' : Px, 'py' : Py, 'pz' : Pz}


def vacancy(**kwargs):
    epsilon = kwargs.get('epsilon', 10 ** (-6))
    b_vec  = kwargs.get('b_vec', [0, 0, 1])

    _system = system(epsilon, False, b_vec = b_vec)
    dip_mat = _system['px']
    dim = len(dip_mat)
    H = np.zeros((8, 8), dtype='float')
    for i, k in enumerate(np.concatenate((_system['sys']['eigs']['g'], _system['sys']['eigs']['u']))): H[i, i] = k

    base = [[[[0 if i != n else 1 for i in range(dim)], 0 if l == 0 else 1] for n in range(dim)] for l in range(2)]
    base = [item for sublist in base for item in sublist]
    snv_base = [[0 if i != n else 1 for i in range(dim)] for n in range(dim)]
    hdim = len(base)
    vacancy_ham = np.zeros((hdim, hdim), dtype=float)

    for k, b in enumerate(base):
        index = k
        internal_index = snv_base.index(b[0])
        vacancy_ham[index, k] = vacancy_ham[index, k] + H[internal_index, internal_index]
    return vacancy_ham

def vacancy4lvl():
    epsilon = 10 ** (-6)
    _system = system(epsilon, False)
    dip_mat = _system['px']
    dim = len(dip_mat)
    H = np.zeros((4, 4), dtype='float')
    for i, k in enumerate(np.concatenate((_system['sys']['eigs']['g'][::2], _system['sys']['eigs']['u'][::2]))): H[i, i] = k
    return H

def vacancy_all_lvl(**kwargs):
    b_vec = kwargs.get('b_vec', [0, 0, 1])
    epsilon = kwargs.get('epsilon', 10 ** (-6))
    _system = system(epsilon, False, b_vec = b_vec)
    dip_mat = _system['px']
    dim = len(dip_mat)
    H = np.zeros((dim, dim), dtype='float')
    for i, k in enumerate(np.concatenate((_system['sys']['eigs']['g'], _system['sys']['eigs']['u']))): H[i, i] = k
    return H

def dipole_ham(polarization, **kwargs):
    b_vec = kwargs.get('b_vec', [0,0,1])
    epsilon = kwargs.get('epsilon', 10 ** (-6))
    _system = system(epsilon, False, b_vec = b_vec)
    dip_mat  = _system['px'] * polarization[0] + _system['py'] * polarization[1] + _system['pz'] * polarization[2]
    dim = len(dip_mat)

    base = [[[[0 if i != n else 1 for i in range(dim)], 0 if l == 0 else 1] for n in range(dim)] for l in range(2)]
    base = [item for sublist in base for item in sublist]
    hdim = len(base)

    dip_ham = np.zeros((hdim, hdim), dtype=complex)
    for k, b in enumerate(base):
        state, fac = b, 1
        if fac > 0:
            for l in range(dim):
                for m in range(dim):
                    if state[0][m] == 1:
                        out_state = [[0 if n != l else 1 for n in range(dim)], state[1]]
                        index = base.index(out_state)
                        internal_index = l
                        dip_ham[index, k] = dip_ham[index, k] + dip_mat[internal_index, m]

    return dip_ham

def dipole_ham_bfield(polarization, phi, bm, **kwargs):
    b_vec = np.array([np.cos(phi), 0, np.sin(phi)])
    epsilon = bm
    _system = system(epsilon, False, b_vec = b_vec)
    dip_mat  = _system['px'] * polarization[0] + _system['py'] * polarization[1] + _system['pz'] * polarization[2]
    dim = len(dip_mat)

    base = [[[[0 if i != n else 1 for i in range(dim)], 0 if l == 0 else 1] for n in range(dim)] for l in range(2)]
    base = [item for sublist in base for item in sublist]
    hdim = len(base)

    dip_ham = np.zeros((hdim, hdim), dtype=complex)

    for k, b in enumerate(base):
        state, fac = b, 1
        if fac > 0:
            for l in range(dim):
                for m in range(dim):
                    if state[0][m] == 1:
                        out_state = [[0 if n != l else 1 for n in range(dim)], state[1]]
                        index = base.index(out_state)
                        internal_index = l
                        dip_ham[index, k] = dip_ham[index, k] + dip_mat[internal_index, m]

    return dip_ham

def energy(**kwargs):
    epsilon = kwargs.get('epsilon' , 10 ** (-6))
    b_vec = kwargs.get('b_vec', [0,0,1])
    _system = system(epsilon, False, b_vec = b_vec)
    ens = [i for i in np.concatenate((_system['sys']['eigs']['g'], _system['sys']['eigs']['u']))]
    return ens

def dipole_mat(polarization, **kwargs):
    b_vec = kwargs.get('b_vec', [0,0,1])
    epsilon = kwargs.get('epsilon' , 10 ** (-6))
    _system = system(epsilon, False, b_vec = b_vec)
    dip_mat = _system['px'] * polarization[0] + _system['py'] * polarization[1] + _system['pz'] * polarization[2]
    return dip_mat

def dipole_mat_bfield(polarization, phi, bm, **kwargs):
    b_vec = np.array([np.cos(phi), 0, np.sin(phi)])
    epsilon = bm
    _system = system(epsilon, False, b_vec=b_vec)
    dip_mat = _system['px'] * polarization[0] + _system['py'] * polarization[1] + _system['pz'] * polarization[2]
    return dip_mat

def main():
    return

if __name__ == '__main__':
    main()
