"""
TETRA cell-info decoder, tail end -- Embedded Python Block, GNU Radio 3.10

Everything before this block is stock GNU Radio: frame sync (Correlate Access Code - Tag),
burst cutting (Delay/Add tag shift + Tags to PDU), soft descrambling (Multiply Const),
de-interleaving (Trellis Permutation), de-puncturing (Deinterleave/Interleave + Null Source)
and Viterbi decoding (Viterbi Combo).

This block does only what no stock block can:
  * CRC-16 check on 76 / 140-bit blocks (digital.crc_check needs whole bytes; these are not)
  * TETRA PDU parsing: SYNC (BSCH), MAC SYSINFO and ACCESS-DEFINE
  * printing (event lines + a status panel every `stats_every` seconds)
  * publishing the cell's scrambling key (message pair 'cell_key') so that a stock
    Message Pair to Var block re-programs the SCH/HD descrambler for this cell
  * publishing a TDMA time reference ('tdma') for the TCH/S decoder's timeslot tracking
  * the MCCH neighbour-cell list: MAC-RESOURCE / MAC-FRAG / MAC-END (with fragment
    reassembly per timeslot), basic-link LLC, and the MLE PDU D-NWRK-BROADCAST.
    Only MLE broadcast information is decoded; PDUs of every other protocol (MM, CMCE,
    SNDCP) are counted and skipped, never parsed.

Inputs  (message): 'bsch'     - u8 PDUs of 80 decoded bits  (60 info + 16 CRC + 4 tail)
                   'hd'       - u8 PDUs of 144 decoded bits (124 info + 16 CRC + 4 tail)
                   'schf'     - u8 PDUs of 288 decoded bits (268 info + 16 CRC + 4 tail),
                                full-slot signalling (SCH/F) from NTS1 bursts
                   'bsch_raw' - the raw 120-soft-bit PDUs straight from cut_bsch. Only their
                                rx_time is used: it gives the absolute bit position of each sync
                                burst. The BSCH chain is strictly 1 PDU in -> 1 PDU out, in order,
                                so the n-th raw PDU belongs to the n-th decoded one.
Outputs (message): 'cell' - pair ('cell_key', MCC<<20 | MNC<<6 | CC) when the cell changes
                   'tdma' - dict {tn, fn, mn, slot_bit, key} for every CRC-good SYNC of the
                            confirmed cell; slot_bit = absolute soft-bit index of that slot's bit 0
                   'out'  - u8 PDU per printed line (optional)
A new cell identity or SYSINFO is only accepted after two identical CRC-good copies,
so a rare false CRC pass (16-bit CRC on correlator false hits) never reaches the display.
"""
import sys
import time
import threading
import numpy as np
import pmt
from gnuradio import gr

SYNC_TAG = 252          # y tag position in the slot (tetra_fec.Y_TAG): slot start = tag bit - 252

SYS_CODE = {0: 'EN 300 392-2 V1', 1: 'EN 300 392-2 V2.3.2+', 2: 'EN 300 392-2 V3.1.1+'}
SHARING = {0: 'continuous transmission', 1: 'carrier sharing', 2: 'MCCH sharing', 3: 'traffic carrier sharing'}
RES_FRAMES = (1, 2, 3, 4, 6, 9, 12, 18)
CELL_LOAD = {0: 'unknown', 1: 'low', 2: 'medium', 3: 'high'}
NEIGH_BCAST = {0: 'not supported', 1: 'on MCCH, not in idle', 2: 'on MCCH, supported in idle', 3: 'reserved'}
OFFSET_HZ = (0, 6250, -6250, 12500)
DUPLEX_KHZ = {
    0: {1: 1600, 2: 10000, 3: 10000, 4: 10000, 5: 10000, 6: 10000},
    1: {1: 4500, 3: 36000, 4: 7000, 8: 45000, 9: 45000},
    2: {b: 0 for b in range(16)},
    3: {3: 8000, 4: 8000, 8: 18000, 9: 18000},
    4: {3: 18000, 4: 5000, 6: 30000, 7: 30000, 9: 39000},
    5: {4: 9500},
}
SERVICES = ((11, 'registration mandatory'), (10, 'de-registration mandatory'), (9, 'priority cell'),
            (8, 'never uses minimum mode'), (7, 'migration'), (6, 'system-wide services'),
            (5, 'voice'), (4, 'circuit data'), (2, 'SNDCP packet data'), (1, 'AIR ENCRYPTION'),
            (0, 'advanced link'))
OPT_FIELD = {0: 'even-multiframe TS_COMMON_FRAMES', 1: 'odd-multiframe TS_COMMON_FRAMES',
             2: 'default access code A', 3: 'extended services broadcast'}
MAC_TYPE = {0: 'MAC-RESOURCE', 1: 'MAC-FRAG/END', 2: 'broadcast', 3: 'MAC-U-SIGNAL/D-BLCK'}

# ---------------------------------------------------------------- MAC / LLC / MLE (EN 300 392-2)
# Field layouts cross-checked against osmo-tetra (MAC-RESOURCE, MAC-FRAG/END, LLC) and the
# sq5bpf fork (D-NWRK-BROADCAST, neighbour cell information).
CC_TAG = 266                                   # NTS1 / NTS2 tag position in the slot
ADDR_LEN = {1: 24, 2: 10, 3: 24, 4: 24, 5: 34, 6: 30, 7: 34}     # MAC address type -> bits
LLC_HDR = {0: 6, 1: 5, 2: 4, 3: 5, 4: 6, 5: 5, 6: 4, 7: 5}       # basic-link PDU types -> header bits
LLC_NAME = {0: 'BL-ADATA', 1: 'BL-DATA', 2: 'BL-UDATA', 3: 'BL-ACK'}
MLE_PD = {1: 'MM', 2: 'CMCE', 4: 'SNDCP', 5: 'MLE', 6: 'management', 7: 'test'}
MLE_PDUT = {0: 'D-NEW-CELL', 1: 'D-PREPARE-FAIL', 2: 'D-NWRK-BROADCAST', 3: 'D-NWRK-BROADCAST-EXT',
            4: 'D-RESTORE-ACK', 5: 'D-RESTORE-FAIL', 6: 'D-CHANNEL-RESPONSE'}
FRAG_MAX_AGE = 2.0                             # s: an unfinished fragment chain is dropped after this
NB_EXPIRE = 600.0                              # s: a neighbour not re-announced for this long is dropped


def mac_len(ind):
    """MAC length indication -> octets, or 'frag' / 'stolen', or None (reserved)"""
    if ind in (0, 0x3b, 0x3c, 0x3d):
        return None
    if ind <= 0x3a:
        return ind                             # pi/4-DQPSK: Y2 = Z2 = 1 octet
    return 'stolen' if ind == 0x3e else 'frag'


def strip_fill(b, start, end):
    """Fill bits are a 1 followed by 0s at the end of a PDU: return the end without them"""
    i = end - 1
    while i > start and b[i] == 0:
        i -= 1
    return i if i > start and b[i] == 1 else end


def chan_alloc_len(b, q):
    """Length of a channel allocation element (only needed to find where the TM-SDU starts)"""
    p = q
    p += 2 + 4                                 # allocation type, timeslot
    ul_dl = bits_uint(b, p, 2); p += 2
    p += 1 + 1 + 12                            # CLCH permission, cell change, carrier number
    if b[p]:
        p += 1 + 10                            # extended carrier numbering
    else:
        p += 1
    mon = bits_uint(b, p, 2); p += 2
    if mon == 0:
        p += 2
    if ul_dl == 0:                             # augmented channel allocation
        p += 2 + 3 + 3 + 3 + 3 + 3 + 4 + 5
        nap = bits_uint(b, p, 2); p += 2
        if nap == 1:
            p += 11
        p += 4
        for _ in range(2):
            p += 1 + (16 if b[p] else 0)
        p += 1
    return p - q


def llc_fcs(bits):
    crc = 0xFFFFFFFF
    if len(bits) < 32:
        crc = (crc << (32 - len(bits))) & 0xFFFFFFFF
    for x in bits:
        bit = (int(x) ^ (crc >> 31)) & 1
        crc = (crc << 1) & 0xFFFFFFFF
        if bit:
            crc ^= 0x04C11DB7
    return (~crc) & 0xFFFFFFFF


def carrier_hz(band, carrier, offset):
    return band * 100e6 + carrier * 25e3 + OFFSET_HZ[offset]


def parse_nwrk(b):
    """D-NWRK-BROADCAST (after the 3-bit PD and 3-bit PDU type). Raises IndexError if short."""
    p = 6
    d = {'resel': bits_uint(b, p, 16), 'load': bits_uint(b, p + 16, 2), 'time': False, 'cells': []}
    p += 18
    if b[p]:                                   # O-bit: optional elements follow
        p += 1
        if b[p]:                               # TETRA network time present
            p += 1 + 48
            d['time'] = True
        else:
            p += 1
        if b[p]:                               # neighbour cells present
            p += 1
            n = bits_uint(b, p, 3); p += 3
            for _ in range(n):
                c, used = parse_nci(b, p)
                p += used
                d['cells'].append(c)
        else:
            p += 1
    if p > len(b):
        raise IndexError('D-NWRK-BROADCAST longer than its TL-SDU')
    return d


def parse_nci(b, q):
    """One 'neighbour cell information for CA' element -> (dict, bits used)"""
    p = q
    c = {'id': bits_uint(b, p, 5), 'resel_types': bits_uint(b, p + 5, 2), 'synced': b[p + 7],
         'load': bits_uint(b, p + 8, 2), 'carrier': bits_uint(b, p + 10, 12)}
    p += 22
    if b[p]:                                   # O-bit
        p += 1
        for name, width in (('ext', 10), ('mcc', 10), ('mnc', 14), ('la', 14), ('txpwr', 3),
                            ('rxlev', 4), ('subscr', 16), ('service', 12), ('timeshare', 5),
                            ('fn_offset', 6)):
            if b[p]:
                v = bits_uint(b, p + 1, width)
                if name == 'ext':
                    c['band'], c['offset'] = v >> 6, (v >> 4) & 3
                    c['duplex'], c['reverse'] = (v >> 1) & 7, v & 1
                else:
                    c[name] = v
                p += 1 + width
            else:
                p += 1
    else:
        p += 1
    return c, p - q


def crc_ok(bits):
    """CRC-ITU-T over info+CRC; a correct block leaves the residue 0x1D0F"""
    crc = 0xFFFF
    for b in bits:
        crc ^= int(b) << 15
        crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc == 0x1D0F


def bits_uint(bits, start, n):
    v = 0
    for b in bits[start:start + n]:
        v = (v << 1) | int(b)
    return v


def parse_sync(b):
    return {
        'syscode': bits_uint(b, 0, 4), 'cc': bits_uint(b, 4, 6),
        'tn': bits_uint(b, 10, 2) + 1, 'fn': bits_uint(b, 12, 5), 'mn': bits_uint(b, 17, 6),
        'sharing': bits_uint(b, 23, 2), 'resv': bits_uint(b, 25, 3), 'dtx': bits_uint(b, 28, 1),
        'f18ext': bits_uint(b, 29, 1),
        'mcc': bits_uint(b, 31, 10), 'mnc': bits_uint(b, 41, 14),
        'nbcast': bits_uint(b, 55, 2), 'load': bits_uint(b, 57, 2), 'late': bits_uint(b, 59, 1)}


def parse_sysinfo(b):
    d = {'carrier': bits_uint(b, 4, 12), 'band': bits_uint(b, 16, 4), 'offset': bits_uint(b, 20, 2),
         'duplex': bits_uint(b, 22, 3), 'reverse': bits_uint(b, 25, 1), 'n_scch': bits_uint(b, 26, 2),
         'txpwr': bits_uint(b, 28, 3), 'rxlev_min': bits_uint(b, 31, 4), 'acc_par': bits_uint(b, 35, 4),
         'dl_timeout': bits_uint(b, 39, 4), 'cck_flag': bits_uint(b, 43, 1), 'hf_or_cck': bits_uint(b, 44, 16),
         'opt': bits_uint(b, 60, 2), 'opt_val': bits_uint(b, 62, 20),
         'la': bits_uint(b, 82, 14), 'subscr': bits_uint(b, 96, 16), 'service': bits_uint(b, 112, 12)}
    d['dl_hz'] = d['band'] * 100e6 + d['carrier'] * 25e3 + OFFSET_HZ[d['offset']]
    dup = DUPLEX_KHZ.get(d['duplex'], {}).get(d['band'])
    d['ul_hz'] = None if dup is None else d['dl_hz'] + (1 if d['reverse'] else -1) * dup * 1e3
    return d


def fmt_sysinfo(d):
    srv = [name for bit, name in SERVICES if d['service'] >> bit & 1]
    ul = ('%.4f MHz' % (d['ul_hz'] / 1e6)) if d['ul_hz'] is not None else 'n/a (reserved duplex code)'
    return [
        'Main carrier : %d  band %d  offset %+.2f kHz  ->  DL %.4f MHz   UL %s' % (
            d['carrier'], d['band'], OFFSET_HZ[d['offset']] / 1e3, d['dl_hz'] / 1e6, ul),
        'Location area: %d   subscriber classes %s   secondary CCHs %d' % (
            d['la'], format(d['subscr'], '016b'), d['n_scch']),
        'Access       : MS max %s   RXLEV_ACCESS_MIN %d dBm   ACCESS_PARAMETER %d dBm   DL timeout %s' % (
            ('%d dBm' % (10 + 5 * d['txpwr'])) if d['txpwr'] else 'reserved',
            -125 + 5 * d['rxlev_min'], -53 + 2 * d['acc_par'],
            'off' if d['dl_timeout'] == 0 else '%d slots' % (144 * d['dl_timeout'])),
        'Services     : %s' % (', '.join(srv) if srv else 'none announced'),
        '%s : %d   option: %s = %05X' % ('CCK id      ' if d['cck_flag'] else 'Hyperframe  ', d['hf_or_cck'],
                                         OPT_FIELD[d['opt']], d['opt_val'])]


_LOCK = threading.Lock()


def _say(line):
    with _LOCK:
        sys.stdout.write(line + '\n')
        sys.stdout.flush()


class blk(gr.basic_block):
    """TETRA cell info: CRC check + SYNC/SYSINFO parsing of stock-decoded blocks"""

    def __init__(self, rf_hz=0.0, stats_every=10.0, show_pdus=False):
        gr.basic_block.__init__(self, name='TETRA CRC + Cell Info', in_sig=None, out_sig=None)
        self.message_port_register_in(pmt.intern('bsch'))
        self.message_port_register_in(pmt.intern('hd'))
        self.message_port_register_in(pmt.intern('bsch_raw'))
        self.message_port_register_in(pmt.intern('schf'))
        self.message_port_register_out(pmt.intern('cell'))
        self.message_port_register_out(pmt.intern('tdma'))
        self.message_port_register_out(pmt.intern('out'))
        self.set_msg_handler(pmt.intern('bsch'), self._on_bsch)
        self.set_msg_handler(pmt.intern('hd'), self._on_hd)
        self.set_msg_handler(pmt.intern('bsch_raw'), self._on_bsch_raw)
        self.set_msg_handler(pmt.intern('schf'), self._on_schf)
        # raw <-> decoded BSCH matching by sequence number (never reset: the chain stays 1:1)
        self._n_raw = 0
        self._n_dec = 0
        self._raw_bit = {}      # seq -> absolute tag bit, raw PDU seen first (the normal case)
        self._pend = {}         # seq -> SYNC dict, decoded PDU seen first (rare thread race)
        self.tdma_ref = None    # (tn, fn, slot_bit, wall time) of the last published reference
        self.rf_hz = float(rf_hz)
        self.stats_every = float(stats_every)
        self.show_pdus = bool(show_pdus)
        self._rf_seen = self.rf_hz
        self._mtx = threading.Lock()
        self._stop_ev = threading.Event()
        self._thr = None
        self._reset()

    def _reset(self):
        self.cell = None            # last good SYNC dict
        self.key = None             # confirmed cell key
        self.key_cand = None        # (key, count) waiting for a second identical copy
        self.si = None
        self.si_lines = None
        self.si_cand = None
        self.acc_def = {}
        self.mac_cnt = {}
        self.n = dict(bsch=0, bsch_ok=0, hd=0, hd_ok=0, f=0, f_ok=0, fcs_bad=0, frag_lost=0, nwrk_bad=0)
        self.frag = {}              # timeslot -> {'bits': [...], 't': wall time}
        self.pd_cnt = {}            # TL-SDU protocol counts (only MLE is parsed)
        self.nwrk = None            # serving-cell part of the last D-NWRK-BROADCAST
        self.nwrk_n = 0
        self.nwrk_t = None
        self.nb = {}                # neighbour cell id -> (entry dict, last seen)
        self.nb_cand = {}           # neighbour cell id -> entry waiting for a second identical copy
        self._mq = []               # MAC blocks waiting to be put back in time order
        self._mq_newest = None
        self.t0 = time.time()

    # ------------------------------------------------------------ helpers
    def _emit(self, text):
        _say(text)
        b = np.frombuffer((text + '\n').encode('ascii', 'replace'), dtype=np.uint8)
        self.message_port_pub(pmt.intern('out'), pmt.cons(pmt.PMT_NIL, pmt.init_u8vector(len(b), b.tolist())))

    @staticmethod
    def _stamp():
        return '[TETRA %s]' % time.strftime('%H:%M:%S')

    @staticmethod
    def _bits(msg):
        return np.array(pmt.u8vector_elements(pmt.cdr(msg)), np.uint8)

    def _check_retune(self):
        if self.rf_hz != self._rf_seen:
            self._rf_seen = self.rf_hz
            self._reset()
            self._emit('%s --- retuned to %.4f MHz: cell picture reset ---' % (self._stamp(), self.rf_hz / 1e6))

    # ------------------------------------------------------------ BSCH (SYNC PDU)
    def _on_bsch(self, msg):
        with self._mtx:
            seq = self._n_dec
            self._n_dec += 1
            self._check_retune()
            b = self._bits(msg)
            self.n['bsch'] += 1
            if len(b) < 76 or not crc_ok(b[:76]):
                self._raw_bit.pop(seq, None)
                return
            self.n['bsch_ok'] += 1
            c = parse_sync(b[:60])
            self.cell = c
            key = (c['mcc'] << 20) | (c['mnc'] << 6) | c['cc']
            if key == self.key:
                self._tdma_from(seq, c)
                return
            # new identity: wait for a second identical copy before switching the descrambler
            if self.key_cand and self.key_cand[0] == key:
                self.key, self.key_cand = key, None
                self.si = self.si_lines = self.si_cand = None
                self.message_port_pub(pmt.intern('cell'), pmt.cons(pmt.intern('cell_key'), pmt.from_long(key)))
                self._emit('%s CELL  MCC %d  MNC %d  colour code %d   (%s, %s, cell load %s)  -> SCH/HD descrambler set' % (
                    self._stamp(), c['mcc'], c['mnc'], c['cc'],
                    SYS_CODE.get(c['syscode'], 'system code %d' % c['syscode']),
                    SHARING[c['sharing']], CELL_LOAD[c['load']]))
                self._tdma_from(seq, c)
            else:
                self.key_cand = (key, 1)
                self._raw_bit.pop(seq, None)

    # ------------------------------------------------------------ TDMA reference for TCH/S
    def _on_bsch_raw(self, msg):
        with self._mtx:
            seq = self._n_raw
            self._n_raw += 1
            bit = self._abs_bit(pmt.car(msg))
            c = self._pend.pop(seq, None)
            if bit is None:
                return
            if c is not None:
                self._pub_tdma(bit, c)
            else:
                self._raw_bit[seq] = bit
            for d in (self._raw_bit, self._pend):          # never let a stray entry pile up
                for s in [s for s in d if s < seq - 64]:
                    del d[s]

    def _tdma_from(self, seq, c):
        bit = self._raw_bit.pop(seq, None)
        if bit is None:
            self._pend[seq] = c
        else:
            self._pub_tdma(bit, c)

    def _pub_tdma(self, tag_bit, c):
        slot_bit = tag_bit - SYNC_TAG
        d = pmt.make_dict()
        for k, v in (('tn', c['tn']), ('fn', c['fn']), ('mn', c['mn']),
                     ('slot_bit', slot_bit), ('key', self.key or 0)):
            d = pmt.dict_add(d, pmt.intern(k), pmt.from_long(int(v)))
        self.message_port_pub(pmt.intern('tdma'), d)
        self.tdma_ref = (c['tn'], c['fn'], slot_bit, time.time())

    @staticmethod
    def _abs_bit(meta):
        """Tags To PDU rx_time (tag offset / rate) -> absolute soft-bit index"""
        try:
            t = pmt.dict_ref(meta, pmt.intern('rx_time'), pmt.PMT_NIL)
            if not pmt.is_tuple(t):
                return None
            r = pmt.dict_ref(meta, pmt.intern('sample_rate'), pmt.PMT_NIL)
            rate = pmt.to_double(r) if pmt.is_number(r) else 36000.0
            return int(round(pmt.to_uint64(pmt.tuple_ref(t, 0)) * rate
                             + pmt.to_double(pmt.tuple_ref(t, 1)) * rate))
        except Exception:
            return None

    # ------------------------------------------------------------ SCH/HD, BNCH (MAC PDUs)
    def _on_hd(self, msg):
        with self._mtx:
            self._check_retune()
            b = self._bits(msg)
            self.n['hd'] += 1
            if self.key is None or len(b) < 140 or not crc_ok(b[:140]):
                return
            self.n['hd_ok'] += 1
            b = b[:124]
            mt = bits_uint(b, 0, 2)
            name = MAC_TYPE[mt]
            if mt in (0, 1):
                self._mac_queue(pmt.car(msg), b, 124)
            if mt == 2:
                bt = bits_uint(b, 2, 2)
                name = {0: 'SYSINFO', 1: 'ACCESS-DEFINE'}.get(bt, 'broadcast type %d' % bt)
                if bt == 0:
                    d = parse_sysinfo(b)
                    lines = fmt_sysinfo(d)
                    stable = lines[:-1]                # hyperframe counter changes every minute
                    if self.si_lines is not None and self.si_lines[:-1] == stable:
                        self.si, self.si_lines = d, lines
                    elif self.si_cand == stable:       # second identical copy: accept and print
                        self.si, self.si_lines, self.si_cand = d, lines, None
                        self._emit('%s SYSINFO\n    ' % self._stamp() + '\n    '.join(lines))
                    else:
                        self.si_cand = stable
                elif bt == 1:
                    ac = bits_uint(b, 5, 2)
                    txt = 'access code %s: IMM %d WT %d Nu %d framelength x%d TS_PTR %d min PDU prio %d' % (
                        'ABCD'[ac], bits_uint(b, 7, 4), bits_uint(b, 11, 4), bits_uint(b, 15, 4),
                        (1, 4)[bits_uint(b, 19, 1)], bits_uint(b, 20, 4), bits_uint(b, 24, 3))
                    if self.acc_def.get(ac) != txt:
                        self._emit('%s ACCESS-DEFINE %s' % (self._stamp(), txt))
                    self.acc_def[ac] = txt
            self.mac_cnt[name] = self.mac_cnt.get(name, 0) + 1
            if self.show_pdus and name not in ('SYSINFO', 'ACCESS-DEFINE'):
                self._emit('%s %s %s' % (self._stamp(), name, ''.join(str(x) for x in b[:40])))

    # ------------------------------------------------------------ SCH/F (full-slot signalling)
    def _on_schf(self, msg):
        with self._mtx:
            self._check_retune()
            b = self._bits(msg)
            self.n['f'] += 1
            if self.key is None or len(b) < 284 or not crc_ok(b[:284]):
                return                         # voice bursts land here too and fail the CRC
            self.n['f_ok'] += 1
            b = b[:268]
            mt = bits_uint(b, 0, 2)
            name = 'SCH/F ' + MAC_TYPE[mt]
            self.mac_cnt[name] = self.mac_cnt.get(name, 0) + 1
            if mt in (0, 1):
                self._mac_queue(pmt.car(msg), b, 268)

    def _mac_queue(self, meta, b, end):
        """Blocks 1 and 2 of a burst come from two cutters 16 bits apart and can arrive swapped.
        Their rx_time says where they really were, so hold each block for one slot and hand
        them to the MAC layer in true time order (a MAC-END must follow its MAC-RESOURCE)."""
        bit = self._abs_bit(meta)
        if bit is None:
            self._mac_block(b, end, 0)
            return
        self._mq.append((bit, end, b, time.time()))
        self._mq_newest = bit if self._mq_newest is None else max(self._mq_newest, bit)
        self._mq_flush(False)

    def _mq_flush(self, by_age):
        self._mq.sort(key=lambda e: e[0])
        now = time.time()
        while self._mq and (self._mq[0][0] <= self._mq_newest - 300 or (by_age and now - self._mq[0][3] > 0.5)):
            bit, end, b, _ = self._mq.pop(0)
            self._mac_block(b, end, self._slot_from_bit(bit))

    def _slot_of(self, meta):
        """Timeslot of a decoded block, from the rx_time its cutter stamped (0 = unknown)"""
        return self._slot_from_bit(self._abs_bit(meta))

    def _slot_from_bit(self, bit):
        ref = self.tdma_ref
        if bit is None or ref is None:
            return 0
        k = int(round((bit - CC_TAG - ref[2]) / 510.0))
        return (ref[0] - 1 + k) % 4 + 1

    # ------------------------------------------------------------ MAC layer
    def _mac_block(self, b, end, slot):
        """Walk the MAC PDUs of one decoded block (124 or 268 bits)"""
        now = time.time()
        for s in [s for s, f in self.frag.items() if now - f['t'] > FRAG_MAX_AGE]:
            del self.frag[s]
            self.n['frag_lost'] += 1
        p = 0
        try:
            while p + 16 <= end:
                mt = bits_uint(b, p, 2)
                if mt == 0:
                    p = self._mac_resource(b, p, end, slot, now)
                elif mt == 1:
                    p = self._mac_frag_end(b, p, end, slot, now)
                else:
                    return                     # broadcast / U-SIGNAL: handled elsewhere or ignored
                if p is None:
                    return
        except IndexError:
            return

    def _mac_resource(self, b, p, end, slot, now):
        q = p + 2
        fill = b[q]; q += 2                    # fill-bit flag, position of grant
        enc = bits_uint(b, q, 2); q += 3       # encryption mode, random-access flag
        ln = mac_len(bits_uint(b, q, 6)); q += 6
        atype = bits_uint(b, q, 3); q += 3
        if atype == 0 or ln is None:
            return None                        # null PDU: nothing more in this block
        q += ADDR_LEN[atype]
        q += 1 + (4 if b[q] else 0)            # power control
        q += 1 + (8 if b[q] else 0)            # slot granting
        if b[q]:                               # channel allocation
            if enc:
                return None
            q += 1 + chan_alloc_len(b, q + 1)
        else:
            q += 1
        stop = end if ln in ('frag', 'stolen') else min(end, p + 8 * ln)
        if fill:
            stop = strip_fill(b, q, stop)
        if not enc and stop > q:
            sdu = list(b[q:stop])
            if ln == 'frag':
                self.frag[slot] = {'bits': sdu, 't': now}
            else:
                self._llc(sdu)
        return None if ln in ('frag', 'stolen') else p + 8 * ln

    def _mac_frag_end(self, b, p, end, slot, now):
        is_end = b[p + 2]
        fill = b[p + 3]
        if not is_end:                         # MAC-FRAG fills the rest of the block
            stop = strip_fill(b, p + 4, end) if fill else end
            f = self.frag.get(slot)
            if f is not None:
                f['bits'] += list(b[p + 4:stop]); f['t'] = now
            return None
        q = p + 5                              # header, fill flag, position of grant
        ln = mac_len(bits_uint(b, q, 6)); q += 6
        if not isinstance(ln, int):
            return None
        q += 1 + (8 if b[q] else 0)            # slot granting
        if b[q]:
            q += 1 + chan_alloc_len(b, q + 1)
        else:
            q += 1
        stop = min(end, p + 8 * ln)
        if fill:
            stop = strip_fill(b, q, stop)
        f = self.frag.pop(slot, None)
        if f is not None:
            self._llc(f['bits'] + list(b[q:stop]))
        return p + 8 * ln

    # ------------------------------------------------------------ LLC and MLE
    def _llc(self, sdu):
        if len(sdu) < 4:
            return
        t = bits_uint(sdu, 0, 4)
        hdr = LLC_HDR.get(t)
        if hdr is None:                        # advanced-link types: not used for broadcasts
            return
        tl = sdu[hdr:]
        if t >= 4:                             # basic link with FCS
            if len(tl) < 32:
                return
            fcs, tl = bits_uint(tl, len(tl) - 32, 32), tl[:-32]
            if fcs != llc_fcs(tl):
                self.n['fcs_bad'] += 1
                return
        if len(tl) < 6:
            return
        pd = bits_uint(tl, 0, 3)
        name = MLE_PD.get(pd, 'PD %d' % pd)
        if pd == 5:
            name = MLE_PDUT.get(bits_uint(tl, 3, 3), 'MLE type %d' % bits_uint(tl, 3, 3))
        self.pd_cnt[name] = self.pd_cnt.get(name, 0) + 1
        if pd == 5 and bits_uint(tl, 3, 3) == 2:
            self._nwrk(tl)
        # every other protocol (MM, CMCE, SNDCP, ...) is only counted, never parsed

    def _nwrk(self, tl):
        try:
            d = parse_nwrk(tl)
        except IndexError:
            self.n['nwrk_bad'] += 1
            return
        now = time.time()
        self.nwrk, self.nwrk_t = d, now
        self.nwrk_n += 1
        for c in d['cells']:
            entry = tuple(sorted(c.items()))
            known = self.nb.get(c['id'])
            if known is not None and known[0] == entry:
                self.nb[c['id']] = (entry, now)
            elif self.nb_cand.get(c['id']) == entry:   # second identical copy: accept
                del self.nb_cand[c['id']]
                self.nb[c['id']] = (entry, now)
                self._emit('%s NEIGHBOUR %s' % (self._stamp(), self._fmt_nb(dict(entry))))
            else:
                self.nb_cand[c['id']] = entry

    def _fmt_nb(self, c):
        if 'band' in c:
            band, off = c['band'], c['offset']
        elif self.si:
            band, off = self.si['band'], self.si['offset']
        else:
            band = off = None
        dl = carrier_hz(band, c['carrier'], off) if band is not None else None
        s = 'cell %2d: carrier %4d' % (c['id'], c['carrier'])
        s += ('  DL %.4f MHz' % (dl / 1e6)) if dl else '  DL ? (no SYSINFO yet)'
        if 'duplex' in c and dl:
            dup = DUPLEX_KHZ.get(c['duplex'], {}).get(band)
            if dup is not None:
                s += '  UL %.4f MHz' % ((dl + (1 if c['reverse'] else -1) * dup * 1e3) / 1e6)
        if 'mcc' in c or 'mnc' in c:
            s += '  MCC %s MNC %s' % (c.get('mcc', '-'), c.get('mnc', '-'))
        if 'la' in c:
            s += '  LA %d' % c['la']
        s += '  load %s  synchronised %s' % (CELL_LOAD[c['load']], 'yes' if c['synced'] else 'no')
        if 'rxlev' in c:
            s += '  RXLEV_ACCESS_MIN %d dBm' % (-125 + 5 * c['rxlev'])
        return s

    # ------------------------------------------------------------ status panel (own thread)
    def start(self):
        self._stop_ev.clear()
        if self.stats_every > 0:
            self._thr = threading.Thread(target=self._panel_loop, daemon=True)
            self._thr.start()
        return True

    def stop(self):
        self._stop_ev.set()
        return True

    def _panel_loop(self):
        while not self._stop_ev.wait(self.stats_every):
            with self._mtx:
                self._check_retune()
                self._mq_flush(True)
                txt = self._status(time.time())
            _say(txt)

    def _status(self, now):
        bar = '=' * 76
        L = [bar, ' TETRA cell status   %s   (running %d s)' % (time.strftime('%H:%M:%S'), now - self.t0), bar]
        if self.rf_hz > 0:
            L.append(' Tuned        : %.4f MHz' % (self.rf_hz / 1e6))
        c = self.cell
        if c is None:
            L.append(' No SYNC decoded yet: check frequency / ppm / gain, the polarity chooser and the plots.')
        else:
            L.append(' Network      : MCC %d  MNC %d (0x%04X)  colour code %d%s' % (
                c['mcc'], c['mnc'], c['mnc'], c['cc'], '   [MCC 510 = Indonesia]' if c['mcc'] == 510 else ''))
            L.append(' SYNC         : %s; %s; %d reserved frame(s); U-plane DTX %s; frame-18 ext %s' % (
                SYS_CODE.get(c['syscode'], 'system code %d' % c['syscode']), SHARING[c['sharing']],
                RES_FRAMES[c['resv']], 'yes' if c['dtx'] else 'no', 'yes' if c['f18ext'] else 'no'))
            L.append(' MLE          : cell load %s; neighbour-cell broadcast %s; late entry %s' % (
                CELL_LOAD[c['load']], NEIGH_BCAST[c['nbcast']], 'yes' if c['late'] else 'no'))
            L.append(' TDMA time    : TN %d  FN %2d  MN %2d (at last SYNC)' % (c['tn'], c['fn'], c['mn']))
            r = self.tdma_ref
            L.append(' TCH/S ref    : %s' % ('none yet (needs a CRC-good SYNC of the confirmed cell)' if r is None else
                                          'TN %d FN %d at bit %d, %.0f s ago' % (r[0], r[1], r[2], now - r[3])))
        if self.si_lines:
            for ln in self.si_lines:
                L.append(' ' + ln)
            if self.rf_hz > 0 and self.si:
                dif = self.si['dl_hz'] - self.rf_hz
                if abs(dif) < 1e3:
                    note = '  (you are on the main carrier, receiver calibrated)'
                elif abs(dif) < 12.5e3:
                    note = ('\n                You ARE on the main carrier; the receiver is %+.1f kHz off.'
                            '\n                Add %+.1f ppm to Freq corr and set tetra_freq to %.4f MHz'
                            ' (if it gets worse, the sign is the other way)' % (
                                -dif / 1e3, dif / self.rf_hz * 1e6, self.si['dl_hz'] / 1e6))
                else:
                    note = '  (secondary carrier: the main carrier is elsewhere)'
                L.append(' Tuning check : announced main carrier minus tuned frequency = %+.1f kHz%s' % (dif / 1e3, note))
        elif c is not None:
            L.append(' SYSINFO      : not decoded yet (needs two identical copies; a few seconds)')
        for v in self.acc_def.values():
            L.append(' Access def.  : ' + v)
        if self.mac_cnt:
            L.append(' MAC PDUs     : ' + ', '.join('%s %d' % kv for kv in sorted(self.mac_cnt.items(), key=lambda kv: -kv[1])))
        if self.pd_cnt:
            L.append(' Signalling   : ' + ', '.join('%s %d' % kv for kv in sorted(self.pd_cnt.items(), key=lambda kv: -kv[1]))
                     + '   (only MLE broadcasts are decoded)')
        for cid in [i for i, (e, t) in self.nb.items() if now - t > NB_EXPIRE]:
            del self.nb[cid]
        if self.nwrk is not None:
            L.append(' Neighbours   : %d cell(s) from %d D-NWRK-BROADCAST, last %.0f s ago; serving cell: '
                     'reselect params 0x%04X, load %s' % (len(self.nb), self.nwrk_n, now - self.nwrk_t,
                                                          self.nwrk['resel'], CELL_LOAD[self.nwrk['load']]))
            for cid in sorted(self.nb):
                L.append('                ' + self._fmt_nb(dict(self.nb[cid][0])))
        elif c is not None:
            L.append(' Neighbours   : no D-NWRK-BROADCAST heard yet (the cell sends it every so often on the MCCH)')
        L.append(' Decoder      : BSCH cuts %d (CRC ok %d)   half-slot cuts %d (CRC ok %d)   full-slot cuts %d (CRC ok %d)' % (
            self.n['bsch'], self.n['bsch_ok'], self.n['hd'], self.n['hd_ok'], self.n['f'], self.n['f_ok']))
        if self.n['bsch'] == 0:
            L.append(' NOTE         : no sync training sequence seen - not TETRA, not locked, too weak, or wrong polarity')
        elif self.n['bsch_ok'] == 0:
            L.append(' NOTE         : y seen but no BSCH passes CRC - SNR too low, ppm far off, or wrong polarity')
        L.append(' (cuts include correlator false hits and, for full slots, every voice burst: a low CRC-ok ratio there is normal)')
        L.append(bar)
        return '\n'.join(L)
