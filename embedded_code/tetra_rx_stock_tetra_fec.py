"""
tetra_fec - Python Module block (id: tetra_fec). Tables only: no signal processing runs here.
Every number below is used as a parameter of a STOCK block; the processing itself happens in
Multiply Const (descramble), Trellis Permutation (de-interleave), Deinterleave/Interleave +
Null Source (de-puncture) and Viterbi Combo (decode).

Spec: ETSI EN 300 392-2 clause 8 (channel coding), cross-checked against the original EPB,
which decoded live cells (MCC 510 MNC 1945).
"""
import numpy as np

# ---------------------------------------------------------------- frame sync
# Correlate Access Code - Tag puts its tag on the FIRST bit AFTER the code.
# Measured in GNU Radio 3.10.9: tag offset = code start + code length.
Y_CODE = '11000001100111001110100111000001100111'   # 38-bit synchronisation training seq (sync burst)
P_CODE = '0111101001000011011110'                   # 22-bit normal training seq 2 (two half-slot blocks)
# slot positions [bits]: sync burst  BSCH 94..213, y 214..251, SCH/HD 282..497
#                        normal burst blk1 14..229, p 244..265,  blk2 282..497
Y_TAG = 214 + 38      # = 252 : y tag lands here
P_TAG = 244 + 22      # = 266 : p tag lands here
D_BSCH = Y_TAG - 94   # = 158 : delay the DATA by this so the y tag sits on the BSCH start
D_SB2 = 282 - Y_TAG   # =  30 : delay the y TAG by this so it sits on sync-burst block 2
D_NB1 = P_TAG - 14    # = 252 : delay the DATA by this so the p tag sits on normal-burst block 1
D_NB2 = 282 - P_TAG   # =  16 : delay the p TAG by this so it sits on normal-burst block 2

# ---------------------------------------------------------------- scrambler (cl. 8.2.5)
_SCR_TAPS = (32, 26, 23, 22, 16, 12, 11, 10, 8, 7, 5, 4, 2, 1)


def cell_key(mcc, mnc, cc):
    """30-bit extended colour code = MCC(10) | MNC(14) | colour code(6)"""
    return ((mcc & 0x3FF) << 20) | ((mnc & 0x3FFF) << 6) | (cc & 0x3F)


def scr_pm(key, n):
    """n scrambling values as +-1.0 (bit 1 -> -1). Multiply soft bits by this to descramble.
    key = cell_key(mcc, mnc, cc); key 0 is the fixed BSCH scrambler."""
    reg = ((int(key) << 2) | 3) & 0xFFFFFFFF
    out = []
    for _ in range(n):
        b = 0
        for t in _SCR_TAPS:
            b ^= (reg >> (32 - t)) & 1
        reg = (reg >> 1) | (b << 31)
        out.append(1.0 - 2.0 * b)
    return out


BSCH_SCR = scr_pm(0, 120)

# ---------------------------------------------------------------- block (de)interleaver (cl. 8.2.4.1)
# Trellis Permutation computes out[i] = in[TABLE[i]] (verified), which is exactly the de-interleaver:
# type-3 bit i (1..K) was sent as type-4 bit k = 1 + (a*i mod K)


def deint_table(K, a):
    return [(a * i) % K for i in range(1, K + 1)]


DEINT_120 = deint_table(120, 11)    # BSCH
DEINT_216 = deint_table(216, 101)   # SCH/HD, BNCH, STCH

# ---------------------------------------------------------------- RCPC rate 2/3 (cl. 8.2.3.1.3)
# From the 1/4 mother code keep mother bits 1, 2, 5 of every 8 (2 info bits -> 3 sent bits).
# De-puncture = Deinterleave(3 outputs) -> Interleave(8 inputs), received bits on inputs 0, 1, 4,
# a Null Source (0.0 = erasure, no information) on inputs 2, 3, 5, 6, 7.
PUNCT_KEEP = (0, 1, 4)

# ---------------------------------------------------------------- mother code (cl. 8.2.3.1.1)
# K = 5, rate 1/4: G1 = 1+D+D^4, G2 = 1+D^2+D^3+D^4, G3 = 1+D+D^2+D^4, G4 = 1+D+D^3+D^4.
# The FSM is written out explicitly (next-state / output tables) so there is no octal
# generator-order convention to get wrong. Output symbol = g1 g2 g3 g4 (g1 = MSB).
_TAPS = ((0, 3), (1, 2, 3), (0, 1, 3), (0, 2, 3))   # delay cells D1..D4 = state bits 0..3
NS, OS = [], []
for _s in range(16):
    _d = [(_s >> _i) & 1 for _i in range(4)]
    for _u in (0, 1):
        _o = 0
        for _taps in _TAPS:
            _b = _u
            for _t in _taps:
                _b ^= _d[_t]
            _o = (_o << 1) | _b
        NS.append(((_s << 1) | _u) & 15)
        OS.append(_o)
FSM_ARGS = (2, 16, 16, NS, OS)     # trellis.fsm(I, S, O, NS, OS)

# Viterbi Combo, Euclidean metric, dimension 4: symbol o -> 4 coded bits as +-1 (+1 = bit 1),
# the same sign convention as the soft bits after the polarity multiplier.
VIT_TABLE = []
for _o in range(16):
    VIT_TABLE += [2.0 * ((_o >> (3 - _g)) & 1) - 1.0 for _g in range(4)]

# trellis steps per block = type-2 bits = info + 16 CRC + 4 tail
BSCH_STEPS = 60 + 16 + 4      # 80  -> 120 sent bits
HD_STEPS = 124 + 16 + 4       # 144 -> 216 sent bits
