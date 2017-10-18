#!/usr/bin/env python
#
# Author: Qiming Sun <osirpt.sun@gmail.com>
#

'''
Non-relativistic RHF analytical Hessian
'''

import time
import numpy
from pyscf import lib
from pyscf.lib import logger
from pyscf.scf import _vhf
from pyscf.scf import cphf
from pyscf.scf.newton_ah import _gen_rhf_response
from pyscf.grad import rhf as rhf_grad


def hess_elec(hessobj, mo_energy=None, mo_coeff=None, mo_occ=None,
              mo1=None, mo_e1=None, h1ao=None,
              atmlst=None, max_memory=4000, verbose=None):
    log = logger.new_logger(hessobj, verbose)
    time0 = t1 = (time.clock(), time.time())

    mol = hessobj.mol
    mf = hessobj._scf
    if mo_energy is None: mo_energy = mf.mo_energy
    if mo_occ is None:    mo_occ = mf.mo_occ
    if mo_coeff is None:  mo_coeff = mf.mo_coeff
    if atmlst is None: atmlst = range(mol.natm)

    if h1ao is None:
        h1ao = hessobj.make_h1(mo_coeff, mo_occ, hessobj.chkfile, atmlst, log)
        t1 = log.timer_debug1('making H1', *time0)
    if mo1 is None or mo_e1 is None:
        mo1, mo_e1 = hessobj.solve_mo1(mo_energy, mo_coeff, mo_occ, h1ao,
                                       None, atmlst, max_memory, log)
        t1 = log.timer_debug1('solving MO1', *t1)

    if isinstance(h1ao, str):
        h1ao = lib.chkfile.load(h1ao, 'scf_f1ao')
        h1ao = dict([(int(k), h1ao[k]) for k in h1ao])
    if isinstance(mo1, str):
        mo1 = lib.chkfile.load(mo1, 'scf_mo1')
        mo1 = dict([(int(k), mo1[k]) for k in mo1])

    nao, nmo = mo_coeff.shape
    mocc = mo_coeff[:,mo_occ>0]
    nocc = mocc.shape[1]
    dm0 = numpy.dot(mocc, mocc.T) * 2
    # Energy weighted density matrix
    dme0 = numpy.einsum('pi,qi,i->pq', mocc, mocc, mo_energy[mo_occ>0]) * 2

    h1aa, h1ab = get_hcore(mol)
    s1aa, s1ab, s1a = get_ovlp(mol)

    vj1, vk1 = _get_jk(mol, 'int2e_ipip1', 9, 's2kl',
                       ['lk->s1ij', dm0,   # vj1
                        'jk->s1il', dm0])  # vk1
    vhf_diag = vj1 - vk1*.5
    vhf_diag = vhf_diag.reshape(3,3,nao,nao)
    vj1 = vk1 = None
    t1 = log.timer_debug1('contracting int2e_ipip1', *t1)

    aoslices = mol.aoslice_by_atom()
    de2 = numpy.zeros((mol.natm,mol.natm,3,3))  # (A,B,dR_A,dR_B)
    for i0, ia in enumerate(atmlst):
        shl0, shl1, p0, p1 = aoslices[ia]

        shls_slice = (shl0, shl1) + (0, mol.nbas)*3
        vj1, vk1, vk2 = _get_jk(mol, 'int2e_ip1ip2', 9, 's1',
                                ['ji->s1kl', dm0[:,p0:p1],  # vj1
                                 'li->s1kj', dm0[:,p0:p1],  # vk1
                                 'lj->s1ki', dm0         ], # vk2
                                shls_slice=shls_slice)
        vhf = vj1 * 2 - vk1 * .5
        vhf[:,:,p0:p1] -= vk2 * .5
        t1 = log.timer_debug1('contracting int2e_ip1ip2 for atom %d'%ia, *t1)
        vj1, vk1 = _get_jk(mol, 'int2e_ipvip1', 9, 's2kl',
                           ['lk->s1ij', dm0         ,  # vj1
                            'li->s1kj', dm0[:,p0:p1]], # vk1
                           shls_slice=shls_slice)
        vhf[:,:,p0:p1] += vj1.transpose(0,2,1)
        vhf -= vk1.transpose(0,2,1) * .5
        vj1 = vk1 = vk2 = None
        t1 = log.timer_debug1('contracting int2e_ipvip1 for atom %d'%ia, *t1)
        vhf = vhf.reshape(3,3,nao,nao)

        rinv2aa, rinv2ab = _hess_rinv(mol, ia)
        hcore = rinv2ab + rinv2aa.transpose(0,1,3,2)
        hcore[:,:,p0:p1] += h1ab[:,:,p0:p1]
        s1ao = numpy.zeros((3,nao,nao))
        s1ao[:,p0:p1] += s1a[:,p0:p1]
        s1ao[:,:,p0:p1] += s1a[:,p0:p1].transpose(0,2,1)
        s1oo = numpy.einsum('xpq,pi,qj->xij', s1ao, mocc, mocc)

        for j0 in range(ia+1):
            ja = atmlst[j0]
            q0, q1 = aoslices[ja][2:]
# *2 for double occupancy, *2 for +c.c.
            dm1 = numpy.einsum('ypi,qi->ypq', mo1[ja], mocc)
            de  = numpy.einsum('xpq,ypq->xy', h1ao[ia], dm1) * 4
            dm1 = numpy.einsum('ypi,qi,i->ypq', mo1[ja], mocc, mo_energy[mo_occ>0])
            de -= numpy.einsum('xpq,ypq->xy', s1ao, dm1) * 4
            de -= numpy.einsum('xpq,ypq->xy', s1oo, mo_e1[ja]) * 2

            v2aa, v2ab = _hess_rinv(mol, ja)
            de += numpy.einsum('xypq,pq->xy', v2aa[:,:,p0:p1], dm0[p0:p1])*2
            de += numpy.einsum('xypq,pq->xy', v2ab[:,:,p0:p1], dm0[p0:p1])*2
            de += numpy.einsum('xypq,pq->xy', hcore[:,:,:,q0:q1], dm0[:,q0:q1])*2
            de += numpy.einsum('xypq,pq->xy', vhf[:,:,q0:q1], dm0[q0:q1])*2
            de -= numpy.einsum('xypq,pq->xy', s1ab[:,:,p0:p1,q0:q1], dme0[p0:p1,q0:q1])*2

            if ia == ja:
                de += numpy.einsum('xypq,pq->xy', h1aa[:,:,p0:p1], dm0[p0:p1])*2
                de -= numpy.einsum('xypq,pq->xy', v2aa, dm0)*2
                de -= numpy.einsum('xypq,pq->xy', v2ab, dm0)*2
                de += numpy.einsum('xypq,pq->xy', vhf_diag[:,:,p0:p1], dm0[p0:p1])*2
                de -= numpy.einsum('xypq,pq->xy', s1aa[:,:,p0:p1], dme0[p0:p1])*2

            de2[i0,j0] = de
            de2[j0,i0] = de.T

    log.timer('RHF hessian', *time0)
    return de2

def make_h1(hessobj, mo_coeff, mo_occ, chkfile=None, atmlst=None, verbose=None):
    time0 = t1 = (time.clock(), time.time())
    mol = hessobj.mol
    if atmlst is None:
        atmlst = range(mol.natm)

    nao, nmo = mo_coeff.shape
    mocc = mo_coeff[:,mo_occ>0]
    dm0 = numpy.dot(mocc, mocc.T) * 2
    h1a = rhf_grad.get_hcore(mol)

    aoslices = mol.aoslice_by_atom()
    h1ao = [None] * mol.natm
    for i0, ia in enumerate(atmlst):
        shl0, shl1, p0, p1 = aoslices[ia]

        mol.set_rinv_origin(mol.atom_coord(ia))
        h1 = -mol.atom_charge(ia) * mol.intor('int1e_iprinv', comp=3)
        h1[:,p0:p1] += h1a[:,p0:p1]

        shls_slice = (shl0, shl1) + (0, mol.nbas)*3
        vj1, vj2, vk1, vk2 = _get_jk(mol, 'int2e_ip1', 3, 's2kl',
                                     ['ji->s2kl', -dm0[:,p0:p1],  # vj1
                                      'lk->s1ij', -dm0         ,  # vj2
                                      'li->s1kj', -dm0[:,p0:p1],  # vk1
                                      'jk->s1il', -dm0         ], # vk2
                                     shls_slice=shls_slice)
        h1 += vj1 - vk1*.5
        h1[:,p0:p1] += vj2 - vk2*.5
        h1 = h1 + h1.transpose(0,2,1)

        if chkfile is None:
            h1ao[ia] = h1
        else:
            key = 'scf_f1ao/%d' % ia
            lib.chkfile.save(chkfile, key, h1)
    if chkfile is None:
        return h1ao
    else:
        return chkfile

def get_hcore(mol):
    h1aa = mol.intor('int1e_ipipkin', comp=9)
    h1aa+= mol.intor('int1e_ipipnuc', comp=9)
    h1ab = mol.intor('int1e_ipkinip', comp=9)
    h1ab+= mol.intor('int1e_ipnucip', comp=9)
    nao = h1aa.shape[-1]
    return h1aa.reshape(3,3,nao,nao), h1ab.reshape(3,3,nao,nao)

def _hess_rinv(mol, atom_id):
    mol.set_rinv_origin(mol.atom_coord(atom_id))
    rinv2aa = mol.intor('int1e_ipiprinv', comp=9)
    rinv2ab = mol.intor('int1e_iprinvip', comp=9)
    #mol.set_rinv_origin((0,0,0))
    Z = mol.atom_charge(atom_id)
    rinv2aa *= Z
    rinv2ab *= Z
    nao = rinv2aa.shape[-1]
    return rinv2aa.reshape(3,3,nao,nao), rinv2ab.reshape(3,3,nao,nao)

def get_ovlp(mol):
    s1a =-mol.intor('int1e_ipovlp', comp=3)
    nao = s1a.shape[-1]
    s1aa = mol.intor('int1e_ipipovlp', comp=9).reshape(3,3,nao,nao)
    s1ab = mol.intor('int1e_ipovlpip', comp=9).reshape(3,3,nao,nao)
    return s1aa, s1ab, s1a

def _get_jk(mol, intor, comp, aosym, script_dms,
            shls_slice=None, cintopt=None):
    intor = mol._add_suffix(intor)
    scripts = script_dms[::2]
    dms = script_dms[1::2]
    vs = _vhf.direct_bindm(intor, aosym, scripts, dms, comp,
                           mol._atm, mol._bas, mol._env,
                           cintopt=cintopt, shls_slice=shls_slice)
    for k, script in enumerate(scripts):
        if 's2' in script:
            hermi = 1
        elif 'a2' in script:
            hermi = 2
        else:
            continue

        shape = vs[k].shape
        if shape[-2] == shape[-1]:
            if comp > 1:
                for i in range(comp):
                    lib.hermi_triu(vs[k][i], hermi=hermi, inplace=True)
            else:
                lib.hermi_triu(vs[k], hermi=hermi, inplace=True)
    return vs

def solve_mo1(mf, mo_energy, mo_coeff, mo_occ, h1ao_or_chkfile,
              fx=None, atmlst=None, max_memory=4000, verbose=None):
    mol = mf.mol
    if atmlst is None: atmlst = range(mol.natm)

    nao, nmo = mo_coeff.shape
    mocc = mo_coeff[:,mo_occ>0]
    nocc = mocc.shape[1]

    if fx is None:
        fx = gen_vind(mf, mo_coeff, mo_occ)
    s1a =-mol.intor('int1e_ipovlp', comp=3)

    def _ao2mo(mat):
        return numpy.asarray([reduce(numpy.dot, (mo_coeff.T, x, mocc)) for x in mat])

    mem_now = lib.current_memory()[0]
    max_memory = max(2000, max_memory*.9-mem_now)
    blksize = max(2, int(max_memory*1e6/8 / (nmo*nocc*3*6)))
    mo1s = [None] * mol.natm
    e1s = [None] * mol.natm
    aoslices = mol.aoslice_by_atom()
    for ia0, ia1 in lib.prange(0, len(atmlst), blksize):
        s1vo = []
        h1vo = []
        for i0 in range(ia0, ia1):
            ia = atmlst[i0]
            shl0, shl1, p0, p1 = aoslices[ia]
            s1ao = numpy.zeros((3,nao,nao))
            s1ao[:,p0:p1] += s1a[:,p0:p1]
            s1ao[:,:,p0:p1] += s1a[:,p0:p1].transpose(0,2,1)
            s1vo.append(_ao2mo(s1ao))
            if isinstance(h1ao_or_chkfile, str):
                key = 'scf_f1ao/%d' % ia
                h1ao = lib.chkfile.load(h1ao_or_chkfile, key)
            else:
                h1ao = h1ao_or_chkfile[ia]
            h1vo.append(_ao2mo(h1ao))

        h1vo = numpy.vstack(h1vo)
        s1vo = numpy.vstack(s1vo)
        mo1, e1 = cphf.solve(fx, mo_energy, mo_occ, h1vo, s1vo)
        mo1 = numpy.einsum('pq,xqi->xpi', mo_coeff, mo1).reshape(-1,3,nao,nocc)
        e1 = e1.reshape(-1,3,nocc,nocc)

        for k in range(ia1-ia0):
            ia = atmlst[k+ia0]
            if isinstance(h1ao_or_chkfile, str):
                key = 'scf_mo1/%d' % ia
                lib.chkfile.save(h1ao_or_chkfile, key, mo1[k])
            else:
                mo1s[ia] = mo1[k]
            e1s[ia] = e1[k].reshape(3,nocc,nocc)
        mo1 = e1 = None

    if isinstance(h1ao_or_chkfile, str):
        return h1ao_or_chkfile, e1s
    else:
        return mo1s, e1s

def gen_vind(mf, mo_coeff, mo_occ):
    nao, nmo = mo_coeff.shape
    mocc = mo_coeff[:,mo_occ>0]
    nocc = mocc.shape[1]
    vresp = _gen_rhf_response(mf, mo_coeff, mo_occ, hermi=1)
    def fx(mo1):
        mo1 = mo1.reshape(-1,nmo,nocc)
        nset = len(mo1)
        dm1 = numpy.empty((nset,nao,nao))
        for i, x in enumerate(mo1):
            dm = reduce(numpy.dot, (mo_coeff, x*2, mocc.T)) # *2 for double occupancy
            dm1[i] = dm + dm.T
        v1 = vresp(dm1)
        v1vo = numpy.empty_like(mo1)
        for i, x in enumerate(v1):
            v1vo[i] = reduce(numpy.dot, (mo_coeff.T, x, mocc))
        return v1vo
    return fx

def hess_nuc(mol, atmlst=None):
    gs = numpy.zeros((mol.natm,mol.natm,3,3))
    qs = numpy.asarray([mol.atom_charge(i) for i in range(mol.natm)])
    rs = numpy.asarray([mol.atom_coord(i) for i in range(mol.natm)])
    for i in range(mol.natm):
        r12 = rs[i] - rs
        s12 = numpy.sqrt(numpy.einsum('ki,ki->k', r12, r12))
        s12[i] = 1e60
        tmp1 = qs[i] * qs / s12**3
        tmp2 = numpy.einsum('k, ki,kj->kij',-3*qs[i]*qs/s12**5, r12, r12)

        gs[i,i,0,0] = \
        gs[i,i,1,1] = \
        gs[i,i,2,2] = -tmp1.sum()
        gs[i,i] -= numpy.einsum('kij->ij', tmp2)

        gs[i,:,0,0] += tmp1
        gs[i,:,1,1] += tmp1
        gs[i,:,2,2] += tmp1
        gs[i,:] += tmp2

    if atmlst is not None:
        gs = gs[atmlst][:,atmlst]
    return gs


class Hessian(lib.StreamObject):
    '''Non-relativistic restricted Hartree-Fock hessian'''
    def __init__(self, scf_method):
        self.verbose = scf_method.verbose
        self.stdout = scf_method.stdout
        self.mol = scf_method.mol
        self._scf = scf_method
        self.chkfile = scf_method.chkfile
        self.max_memory = self.mol.max_memory

        self.de = numpy.zeros((0,0,3,3))  # (A,B,dR_A,dR_B)
        self._keys = set(self.__dict__.keys())

    hess_elec = hess_elec
    make_h1 = make_h1

    def solve_mo1(self, mo_energy, mo_coeff, mo_occ, h1ao_or_chkfile,
                  fx=None, atmlst=None, max_memory=4000, verbose=None):
        return solve_mo1(self._scf, mo_energy, mo_coeff, mo_occ, h1ao_or_chkfile,
                         fx, atmlst, max_memory, verbose)

    def hess_nuc(self, mol=None, atmlst=None):
        if mol is None: mol = self.mol
        return hess_nuc(mol, atmlst)

    def kernel(self, mo_energy=None, mo_coeff=None, mo_occ=None, atmlst=None):
        cput0 = (time.clock(), time.time())
        if mo_energy is None: mo_energy = self._scf.mo_energy
        if mo_coeff is None: mo_coeff = self._scf.mo_coeff
        if mo_occ is None: mo_occ = self._scf.mo_occ
        if atmlst is None: atmlst = range(self.mol.natm)

        de = self.hess_elec(mo_energy, mo_coeff, mo_occ, atmlst=atmlst)
        self.de = de + self.hess_nuc(self.mol, atmlst=atmlst)
        return self.de


if __name__ == '__main__':
    from pyscf import gto
    from pyscf import scf
    from pyscf.scf import rhf_grad

    mol = gto.Mole()
    mol.verbose = 0
    mol.output = None
    mol.atom = [
        [1 , (1. ,  0.     , 0.000)],
        [1 , (0. ,  1.     , 0.000)],
        [1 , (0. , -1.517  , 1.177)],
        [1 , (0. ,  1.517  , 1.177)] ]
    mol.basis = '631g'
    mol.unit = 'B'
    mol.build()
    mf = scf.RHF(mol)
    mf.conv_tol = 1e-14
    mf.scf()
    n3 = mol.natm * 3
    hobj = Hessian(mf)
    e2 = hobj.kernel().transpose(0,2,1,3).reshape(n3,n3)
    print(lib.finger(e2) - -0.50693144355876429)
    #from hessian import rhf_o0
    #e2ref = rhf_o0.Hessian(mf).kernel().transpose(0,2,1,3).reshape(n3,n3)
    #print numpy.linalg.norm(e2-e2ref)
    #print numpy.allclose(e2,e2ref)

    def grad_full(ia, inc):
        coord = mol.atom_coord(ia).copy()
        ptr = mol._atm[ia,gto.PTR_COORD]
        de = []
        for i in range(3):
            mol._env[ptr+i] = coord[i] + inc
            mf = scf.RHF(mol).run(conv_tol=1e-14)
            e1a = mf.nuc_grad_method().kernel()
            mol._env[ptr+i] = coord[i] - inc
            mf = scf.RHF(mol).run(conv_tol=1e-14)
            e1b = mf.nuc_grad_method().kernel()
            mol._env[ptr+i] = coord[i]
            de.append((e1a-e1b)/(2*inc))
        return de
    e2ref = [grad_full(ia, .5e-4) for ia in range(mol.natm)]
    e2ref = numpy.asarray(e2ref).reshape(n3,n3)
    print(numpy.linalg.norm(e2-e2ref))
    print(abs(e2-e2ref).max())
    print(numpy.allclose(e2,e2ref,atol=1e-6))

# \partial^2 E / \partial R \partial R'
    h1ao = hobj.make_h1(mf.mo_coeff, mf.mo_occ)
    mo1, mo_e1 = hobj.solve_mo1(mf.mo_energy, mf.mo_coeff, mf.mo_occ, h1ao)
    e2 = hobj.hess_elec(mf.mo_energy, mf.mo_coeff, mf.mo_occ,
                        numpy.zeros_like(mo1), numpy.zeros_like(mo_e1),
                        numpy.zeros_like(h1ao))
    e2 += hobj.hess_nuc(mol)
    e2 = e2.transpose(0,2,1,3).reshape(n3,n3)
    def grad_partial_R(ia, inc):
        coord = mol.atom_coord(ia).copy()
        ptr = mol._atm[ia,gto.PTR_COORD]
        de = []
        for i in range(3):
            mol._env[ptr+i] = coord[i] + inc
            e1a = mf.nuc_grad_method().kernel()
            mol._env[ptr+i] = coord[i] - inc
            e1b = mf.nuc_grad_method().kernel()
            mol._env[ptr+i] = coord[i]
            de.append((e1a-e1b)/(2*inc))
        return de
    e2ref = [grad_partial_R(ia, .5e-4) for ia in range(mol.natm)]
    e2ref = numpy.asarray(e2ref).reshape(n3,n3)
    print(numpy.linalg.norm(e2-e2ref))
    print(abs(e2-e2ref).max())
    print(numpy.allclose(e2,e2ref,atol=1e-8))

## \partial^2 E / \partial R \partial C (dC/dR)
#    e2 = hobj.hess_elec(mf.mo_energy, mf.mo_coeff*0, mf.mo_occ,
#                        numpy.zeros_like(mo1), numpy.zeros_like(mo_e1),
#                        numpy.zeros_like(h1ao))
#    e2 += hobj.hess_nuc(mol)
#    e2 = e2.transpose(0,2,1,3).reshape(n3,n3)
#    def grad_partial_C(ia, inc):
#        coord = mol.atom_coord(ia).copy()
#        ptr = mol._atm[ia,gto.PTR_COORD]
#        de = []
#        for i in range(3):
#            mol._env[ptr+i] = coord[i] + inc
#            mf = scf.RHF(mol).run(conv_tol=1e-14)
#            mol._env[ptr+i] = coord[i]
#            e1a = mf.nuc_grad_method().kernel()
#            mol._env[ptr+i] = coord[i] - inc
#            mf = scf.RHF(mol).run(conv_tol=1e-14)
#            mol._env[ptr+i] = coord[i]
#            e1b = mf.nuc_grad_method().kernel()
#            de.append((e1a-e1b)/(2*inc))
#        return de
#    e2ref = [grad_partial_C(ia, .5e-4) for ia in range(mol.natm)]
#    e2ref = numpy.asarray(e2ref).reshape(n3,n3)
#    print(numpy.linalg.norm(e2-e2ref))
#    print(abs(e2-e2ref).max())
#    print(numpy.allclose(e2,e2ref,atol=1e-6))