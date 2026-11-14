"""
verify_bitexact.py -- prove the DLL wrapper is bit-exact with the ETSI reference programs.

Usage (Windows, radioconda prompt, in the folder with the DLLs and ETSI .exe files):
    python verify_bitexact.py speech.raw
speech.raw = 8 kHz, 16-bit little-endian mono PCM, no header
(make one from a WAV with:  sox speech.wav -r 8000 -c 1 -b 16 speech.raw)

It runs the input through
  (a) the four ETSI executables  scoder -> ccoder -> cdecoder -> sdecoder   (files)
  (b) tetra_enc.dll / tetra_dec.dll, frame by frame through ctypes          (memory)
and compares every intermediate file word for word. Any difference = FAIL.
"""
import ctypes
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = '.exe' if os.name == 'nt' else ''


def load(name):
    for f in (name + '.dll', 'lib' + name + '.dll', 'lib' + name + '.so'):
        p = os.path.join(HERE, f)
        if os.path.exists(p):
            return ctypes.CDLL(p)
    sys.exit('cannot find %s.dll next to this script' % name)


def main(raw):
    enc, dec = load('tetra_enc'), load('tetra_dec')
    i16 = np.ctypeslib.ndpointer(np.int16, flags='C_CONTIGUOUS')
    enc.tetra_enc_frame.argtypes = [i16, i16, i16]
    enc.tetra_cc_frame.argtypes = [i16, i16]
    dec.tetra_cd_frame.argtypes = [i16, i16]
    dec.tetra_cd_frame.restype = ctypes.c_int
    dec.tetra_dec_frame.argtypes = [i16, i16]

    # ---- (a) ETSI executables ------------------------------------------------------
    run = lambda *a: subprocess.run([os.path.join(HERE, a[0] + EXE)] + list(a[1:]),
                                    stdout=subprocess.DEVNULL, check=True)
    run('scoder', raw, '_ref.ser', '_ref_lsyn.raw')
    run('ccoder', '_ref.ser', '_ref.tch')
    run('cdecoder', '_ref.tch', '_ref.cdec')
    run('sdecoder', '_ref.cdec', '_ref_out.raw')
    ref_ser = np.fromfile('_ref.ser', '<i2').reshape(-1, 138)
    ref_lsyn = np.fromfile('_ref_lsyn.raw', '<i2').reshape(-1, 240)
    tch = np.fromfile('_ref.tch', '<i2').reshape(-1, 690)   # 6 x (header + 114), padded
    idx = np.r_[1:115, 116:230, 231:345, 346:436]           # the 432 valid words
    ref_tch = tch[:, idx].astype(np.int16)
    ref_tch = np.where(ref_tch & 0x80, ref_tch | ~0xFF, ref_tch).astype(np.int16)
    ref_cdec = np.fromfile('_ref.cdec', '<i2').reshape(-1, 138)
    ref_out = np.fromfile('_ref_out.raw', '<i2').reshape(-1, 240)

    # ---- (b) DLL, frame by frame ---------------------------------------------------
    pcm = np.fromfile(raw, '<i2')
    nfr = len(pcm) // 240
    enc.tetra_enc_init(); enc.tetra_cc_init(); dec.tetra_cd_init(); dec.tetra_dec_init()
    ser = np.zeros((nfr, 138), np.int16)
    lsyn = np.zeros((nfr, 240), np.int16)
    for k in range(nfr):
        enc.tetra_enc_frame(np.ascontiguousarray(pcm[240 * k:240 * k + 240]), ser[k], lsyn[k])
    npair = nfr // 2
    tchb = np.zeros((npair, 432), np.int16)
    cdec = np.zeros((2 * npair, 138), np.int16)
    for k in range(npair):
        enc.tetra_cc_frame(np.ascontiguousarray(ser[2 * k:2 * k + 2].ravel()), tchb[k])
        o = np.zeros(276, np.int16)
        dec.tetra_cd_frame(tchb[k], o)
        cdec[2 * k:2 * k + 2] = o.reshape(2, 138)
    out = np.zeros((2 * npair, 240), np.int16)
    for k in range(2 * npair):
        dec.tetra_dec_frame(np.ascontiguousarray(cdec[k]), out[k])

    ok = True
    for name, a, b in (('scoder serial', ser, ref_ser), ('scoder local synth', lsyn, ref_lsyn),
                       ('ccoder 432-bit blocks', tchb, ref_tch), ('cdecoder serial', cdec, ref_cdec),
                       ('sdecoder speech', out, ref_out)):
        same = a.shape == b.shape and np.array_equal(a, b)
        ok &= same
        print('%-24s %6d frames  %s' % (name, len(a), 'IDENTICAL' if same else 'DIFFERENT  <-- FAIL'))
    print('RESULT:', 'PASS (DLL is bit-exact with the ETSI programs)' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
