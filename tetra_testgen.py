#!/usr/bin/env python3
"""
tetra_testgen.py - synthetic TETRA downlink (continuous BS carrier) test signal.

Builds spec-shaped slots and writes complex64 baseband at 72 kS/s (4 samples/symbol),
channel at 0 Hz, for the self-test flowgraph tetra_rx_stock_selftest.grc.

What is inside each TDMA frame (4 slots, repeated):
  TN1  Synchronisation burst : BSCH (SYNC PDU) + SCH/HD carrying SYSINFO
  TN2  Normal burst, NTS 2   : blk1 ACCESS-DEFINE, blk2 SYSINFO
  TN3  Normal burst, NTS 1   : random full-slot content (decoder must ignore it)
  TN4  Normal burst, NTS 2   : blk1 MAC-RESOURCE (null PDU), blk2 random garbage
Real cells send the sync burst only in frame 18; sending it every frame just makes
the test faster. The decoder does not care.

Coding chain (EN 300 392-2 cl. 8): CRC-16 -> K=5 rate-1/4 mother code + 4 tail bits ->
RCPC rate 2/3 puncturing -> block interleaver -> scrambler -> burst -> pi/4-DQPSK.
Training sequences and bit offsets are the ones the original EPB decoded on air
(cross-checked with osmo-tetra).

Usage:  python tetra_testgen.py [-o tetra_test_72k.cf32] [--seconds 4] [--snr 18]
                                [--foff 400] [--mcc 510] [--mnc 1945] [--cc 1]
        --foff = carrier offset left for the FLL / derotator to remove [Hz]
        --bits FILE also writes the ideal bit stream (float32, +1 = bit 1, -1 = bit 0)
Needs numpy only.
"""
import argparse
import numpy as np

SLOT = 510
RS = 18000.0
SPS = 4
Y_BITS = np.array([1, 1, 0, 0, 0, 0, 0, 1, 1, 0, 0, 1, 1, 1, 0, 0, 1, 1, 1,
                   0, 1, 0, 0, 1, 1, 1, 0, 0, 0, 0, 0, 1, 1, 0, 0, 1, 1, 1], np.uint8)
P_BITS = np.array([0, 1, 1, 1, 1, 0, 1, 0, 0, 1, 0, 0, 0, 0, 1, 1, 0, 1, 1, 1, 1, 0], np.uint8)
N_BITS = np.array([1, 1, 0, 1, 0, 0, 0, 0, 1, 1, 1, 0, 1, 0, 0, 1, 1, 1, 0, 1, 0, 0], np.uint8)
SB_BLK1, SB_Y, SB_BLK2 = 94, 214, 282
NDB_BLK1, NDB_T, NDB_BLK2 = 14, 244, 282

_TAPS = ((0, 3), (1, 2, 3), (0, 1, 3), (0, 2, 3))   # G1..G4 delay cells, state bit i = D(i+1)


def conv_encode(bits):
    s, out = 0, []
    for u in bits:
        d = [(s >> i) & 1 for i in range(4)]
        for taps in _TAPS:
            b = u
            for t in taps:
                b ^= d[t]
            out.append(b)
        s = ((s << 1) | u) & 15
    return np.array(out, np.uint8)


def depunct_index(n):
    P = (1, 2, 5)
    return np.array([8 * ((j - 1) // 3) + P[(j - 1) % 3] - 1 for j in range(1, n + 1)])


def deint_index(K, a):
    return np.array([(a * i) % K for i in range(1, K + 1)])


DEPUNCT = {120: depunct_index(120), 216: depunct_index(216)}
DEINT = {120: deint_index(120, 11), 216: deint_index(216, 101)}


def crc16_append(info):
    crc = 0xFFFF
    for b in info:
        crc ^= int(b) << 15
        crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    crc ^= 0xFFFF
    return np.concatenate([info, [(crc >> (15 - i)) & 1 for i in range(16)]]).astype(np.uint8)


_SCR_TAPS = (32, 26, 23, 22, 16, 12, 11, 10, 8, 7, 5, 4, 2, 1)


def scramb_seq(mcc, mnc, cc, n):
    reg = ((((mcc & 0x3FF) << 20) | ((mnc & 0x3FFF) << 6) | (cc & 0x3F)) << 2 | 3) & 0xFFFFFFFF
    out = np.empty(n, np.uint8)
    for i in range(n):
        b = 0
        for t in _SCR_TAPS:
            b ^= (reg >> (32 - t)) & 1
        reg = (reg >> 1) | (b << 31)
        out[i] = b
    return out


def encode_block(info, scr):
    """info: type-1 bits (60 or 124) -> K type-5 bits (120 or 216)"""
    t2 = np.concatenate([crc16_append(info), np.zeros(4, np.uint8)])
    mother = conv_encode(t2)
    K = len(scr)
    t3 = mother[DEPUNCT[K]]
    t4 = np.zeros(K, np.uint8)
    t4[DEINT[K]] = t3
    return t4 ^ scr


def put(bits, val, n):
    return bits + [(val >> (n - 1 - i)) & 1 for i in range(n)]


def sync_pdu(cc, tn, fn, mn, mcc, mnc):
    b = []
    b = put(b, 1, 4); b = put(b, cc, 6); b = put(b, tn - 1, 2); b = put(b, fn, 5); b = put(b, mn, 6)
    b = put(b, 0, 2); b = put(b, 0, 3); b = put(b, 0, 1); b = put(b, 0, 1); b = put(b, 0, 1)  # dtx, f18ext, 1 reserved bit
    b = put(b, mcc, 10); b = put(b, mnc, 14); b = put(b, 2, 2); b = put(b, 1, 2); b = put(b, 1, 1)
    return np.array(b, np.uint8)        # 60 bits


def sysinfo_pdu(carrier, band, la, hf):
    b = []
    b = put(b, 2, 2); b = put(b, 0, 2)                     # MAC broadcast, SYSINFO
    b = put(b, carrier, 12); b = put(b, band, 4); b = put(b, 0, 2); b = put(b, 0, 3); b = put(b, 0, 1)
    b = put(b, 1, 2); b = put(b, 5, 3); b = put(b, 3, 4); b = put(b, 7, 4); b = put(b, 0, 4)
    b = put(b, 0, 1); b = put(b, hf & 0xFFFF, 16)           # hyperframe number
    b = put(b, 2, 2); b = put(b, 0x12345, 20)               # optional field: default access code A
    b = put(b, la, 14); b = put(b, 0xFFFF, 16)
    b = put(b, (1 << 11) | (1 << 6) | (1 << 5) | (1 << 2), 12)  # reg. mandatory, system-wide, voice, SNDCP
    return np.array(b, np.uint8)        # 124 bits


def access_define_pdu():
    b = []
    b = put(b, 2, 2); b = put(b, 1, 2); b = put(b, 0, 1); b = put(b, 0, 2)   # access code A
    b = put(b, 5, 4); b = put(b, 3, 4); b = put(b, 6, 4); b = put(b, 1, 1); b = put(b, 0, 4); b = put(b, 0, 3)
    return np.array(b + [0] * (124 - len(b)), np.uint8)


def mac_resource_null():
    b = []
    b = put(b, 0, 2); b = put(b, 0, 1); b = put(b, 0, 1); b = put(b, 2, 6)   # MAC-RESOURCE, null PDU
    return np.array(b + [0] * (124 - len(b)), np.uint8)


def build_bits(n_slots, mcc, mnc, cc, rng):
    bsch_scr = scramb_seq(0, 0, 0, 120)
    cell_scr = scramb_seq(mcc, mnc, cc, 216)
    out = []
    for k in range(n_slots):
        tn = k % 4 + 1
        fn = (k // 4) % 18 + 1
        mn = (k // 72) % 60 + 1
        s = rng.integers(0, 2, SLOT).astype(np.uint8)
        if tn == 1:
            s[SB_BLK1:SB_BLK1 + 120] = encode_block(sync_pdu(cc, tn, fn, mn, mcc, mnc), bsch_scr)
            s[SB_Y:SB_Y + 38] = Y_BITS
            s[SB_BLK2:SB_BLK2 + 216] = encode_block(sysinfo_pdu(3723, 3, 105, k // 4320), cell_scr)
        elif tn == 2:
            s[NDB_BLK1:NDB_BLK1 + 216] = encode_block(access_define_pdu(), cell_scr)
            s[NDB_T:NDB_T + 22] = P_BITS
            s[NDB_BLK2:NDB_BLK2 + 216] = encode_block(sysinfo_pdu(3723, 3, 105, k // 4320), cell_scr)
        elif tn == 3:
            s[NDB_T:NDB_T + 22] = N_BITS
        else:
            s[NDB_BLK1:NDB_BLK1 + 216] = encode_block(mac_resource_null(), cell_scr)
            s[NDB_T:NDB_T + 22] = P_BITS
        out.append(s)
    return np.concatenate(out)


DPHI = {(0, 0): np.pi / 4, (0, 1): 3 * np.pi / 4, (1, 0): -np.pi / 4, (1, 1): -3 * np.pi / 4}


def rrc(alpha, sps, span):
    n = np.arange(-span * sps / 2, span * sps / 2 + 1) / sps
    h = np.zeros_like(n)
    for i, t in enumerate(n):
        if abs(t) < 1e-9:
            h[i] = 1 - alpha + 4 * alpha / np.pi
        elif abs(abs(t) - 1 / (4 * alpha)) < 1e-9:
            h[i] = alpha / np.sqrt(2) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha)) +
                                         (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha)))
        else:
            h[i] = (np.sin(np.pi * t * (1 - alpha)) + 4 * alpha * t * np.cos(np.pi * t * (1 + alpha))) / \
                   (np.pi * t * (1 - (4 * alpha * t) ** 2))
    return h / np.sqrt(np.sum(h ** 2))


def modulate(bits, foff, snr_db, rng):
    d = bits.reshape(-1, 2)
    dphi = np.array([DPHI[(int(a), int(b))] for a, b in d])
    sym = np.exp(1j * np.cumsum(dphi))
    up = np.zeros(len(sym) * SPS, complex)
    up[::SPS] = sym
    x = np.convolve(up, rrc(0.35, SPS, 12), mode='same') * np.sqrt(SPS)
    n = np.arange(len(x))
    x *= np.exp(2j * np.pi * foff * n / (RS * SPS))
    sig_p = np.mean(np.abs(x) ** 2)
    # snr_db = Es/N0; noise in the full 72 kHz band
    n0 = sig_p * SPS / (10 ** (snr_db / 10))
    x += np.sqrt(n0 / 2) * (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))
    return (0.1 * x / np.sqrt(sig_p)).astype(np.complex64)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('-o', '--out', default='tetra_test_72k.cf32')
    ap.add_argument('--seconds', type=float, default=4.0)
    ap.add_argument('--snr', type=float, default=18.0, help='Es/N0 [dB]')
    ap.add_argument('--foff', type=float, default=400.0, help='carrier offset [Hz]')
    ap.add_argument('--mcc', type=int, default=510)
    ap.add_argument('--mnc', type=int, default=1945)
    ap.add_argument('--cc', type=int, default=1)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--bits', default=None)
    ap.add_argument('--adj', type=float, default=None,
                    help='add a neighbour carrier 25 kHz above, this many dB relative to the wanted one')
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    n_slots = int(a.seconds * RS * 2 / SLOT) // 4 * 4
    bits = build_bits(n_slots, a.mcc, a.mnc, a.cc, rng)
    if a.bits:
        (2.0 * bits - 1.0).astype(np.float32).tofile(a.bits)
    x = modulate(bits, a.foff, a.snr, rng)
    if a.adj is not None:
        # random pi/4-DQPSK neighbour on the 25 kHz raster, then the flowgraph's channel filter
        # (firdes.low_pass(1, fs, 16 kHz, 8 kHz) equivalent), as it would look after xlate_0
        nb = modulate(rng.integers(0, 2, len(bits)).astype(np.uint8), a.foff + 25e3, 60.0, rng)
        x = x + nb * 10 ** (a.adj / 20)
        fs, fc, tw = RS * SPS, 16e3, 8e3
        nt = int(3.3 * fs / tw) | 1
        k = np.arange(nt) - nt // 2
        h = 2 * fc / fs * np.sinc(2 * fc / fs * k) * np.hamming(nt)
        x = np.convolve(x, h / h.sum(), mode='same').astype(np.complex64)
    x.tofile(a.out)
    if a.adj is not None:
        print('neighbour carrier at +25 kHz, %+.0f dB' % a.adj)
    print('%s: %d slots, %.2f s, %d samples @ 72 kS/s, Es/N0 %.1f dB, offset %+.0f Hz, MCC %d MNC %d CC %d' % (
        a.out, n_slots, len(x) / 72e3, len(x), a.snr, a.foff, a.mcc, a.mnc, a.cc))


if __name__ == '__main__':
    main()
