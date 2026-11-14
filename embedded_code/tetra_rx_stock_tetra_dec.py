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

Inputs  (message): 'bsch'     - u8 PDUs of 80 decoded bits  (60 info + 16 CRC + 4 tail)
                   'hd'       - u8 PDUs of 144 decoded bits (124 info + 16 CRC + 4 tail)
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
        self.message_port_register_out(pmt.intern('cell'))
        self.message_port_register_out(pmt.intern('tdma'))
        self.message_port_register_out(pmt.intern('out'))
        self.set_msg_handler(pmt.intern('bsch'), self._on_bsch)
        self.set_msg_handler(pmt.intern('hd'), self._on_hd)
        self.set_msg_handler(pmt.intern('bsch_raw'), self._on_bsch_raw)
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
        self.n = dict(bsch=0, bsch_ok=0, hd=0, hd_ok=0)
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
            L.append(' Half-slot PDUs: ' + ', '.join('%s %d' % kv for kv in sorted(self.mac_cnt.items(), key=lambda kv: -kv[1])))
        L.append(' Decoder      : BSCH cuts %d (CRC ok %d)   half-slot cuts %d (CRC ok %d)' % (
            self.n['bsch'], self.n['bsch_ok'], self.n['hd'], self.n['hd_ok']))
        if self.n['bsch'] == 0:
            L.append(' NOTE         : no sync training sequence seen - not TETRA, not locked, too weak, or wrong polarity')
        elif self.n['bsch_ok'] == 0:
            L.append(' NOTE         : y seen but no BSCH passes CRC - SNR too low, ppm far off, or wrong polarity')
        L.append(' (half-slot cuts include correlator false hits on random data; a low CRC-ok ratio there is normal)')
        L.append(bar)
        return '\n'.join(L)
