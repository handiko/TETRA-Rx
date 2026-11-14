"""
TETRA TCH/S ACELP decoder -- Embedded Python Block, GNU Radio 3.10

Inputs (message)
  'tch'  : f32 PDUs of 484 soft bits = normal-burst bits 14..497 of every burst that carries
           normal training sequence 1 (full slot: TCH, or SCH/F signalling). ONE stock
           Tags To PDU cuts them, so block 1 = [0:216] and block 2 = [268:484] always belong
           to the same burst (no pairing between two cutters).
  'tdma' : dict from 'TETRA CRC + Cell Info' after every CRC-good SYNC of the confirmed cell:
           tn, fn, mn and slot_bit = absolute soft-bit index where that sync burst's slot starts.
Output (stream): float32 PCM, 8 kHz.

How the timeslot is known
  Tags To PDU stamps each PDU with rx_time = tag offset / rate. With rate = 2*ch_rate/sps
  = 36000 bit/s, rx_time*36000 is the absolute soft-bit index of the tag. Every cutter sits on
  the same pol_mul stream, so a TCH burst and a SYNC burst share one bit counter:
      slots = round((tch_slot_start - sync_slot_start) / 510)
      TN = (TN_sync - 1 + slots) mod 4 + 1,  FN advances every 4 slots (1..18).
  A real burst lands on the 510-bit slot grid (|error| <= a few bits); correlator false hits
  land anywhere, so off-grid bursts are dropped.

target_tn
   0  auto   : channel-decode every slot, play the one with the most good (non-BFI) frames
   1..4      : play only that timeslot
  -1  all    : play everything (old behaviour; mixes slots, for debugging only)
Frame 18 is the control frame (no traffic); skip_fn18 drops it.

Needs tetra_dec.dll (Windows) / libtetra_dec.so next to the generated flowgraph .py, exporting
tetra_cd_init(), tetra_dec_init(), int tetra_cd_frame(short soft[432], short serial[276]),
tetra_dec_frame(short serial[138], short pcm[240]).
"""
import os
import sys
import ctypes
import threading
from collections import deque

import numpy as np
import pmt
from gnuradio import gr

# ---- burst geometry (must match tetra_fec / tetra_dec) -------------------------------------
SLOT_BITS = 510
TCH_TAG = 266          # NTS1 tag = first bit after the training sequence (slot bit 266)
PDU_LEN = 484          # slot bits 14..497
BLK2 = 268             # block 2 starts at slot bit 282 = 14 + 268
BLK_LEN = 216
GRID_TOL = 8           # bits: allowed distance from the slot grid
REF_MAX_AGE = 36000 * 30   # bits (30 s): older SYNC reference is not trusted
AUTO_WIN = 36000 * 2   # bits (2 s) window for the auto selector
AUTO_MIN_GOOD = 6      # good frames in the window needed to (keep) select a slot

_SCR_TAPS = (32, 26, 23, 22, 16, 12, 11, 10, 8, 7, 5, 4, 2, 1)


def _scr_pm(key, n):
    """TETRA scrambler (EN 300 392-2 cl. 8.2.5) as +-1.0 values (bit 1 -> -1)."""
    reg = ((int(key) << 2) | 3) & 0xFFFFFFFF
    out = []
    for _ in range(n):
        b = 0
        for t in _SCR_TAPS:
            b ^= (reg >> (32 - t)) & 1
        reg = (reg >> 1) | (b << 31)
        out.append(1.0 - 2.0 * b)
    return out


def abs_bit(meta):
    """Absolute soft-bit index of the PDU's start tag, from Tags To PDU's rx_time."""
    try:
        t = pmt.dict_ref(meta, pmt.intern('rx_time'), pmt.PMT_NIL)
        if not pmt.is_tuple(t):
            return None
        r = pmt.dict_ref(meta, pmt.intern('sample_rate'), pmt.PMT_NIL)
        rate = pmt.to_double(r) if pmt.is_number(r) else 36000.0
        sec = pmt.to_uint64(pmt.tuple_ref(t, 0))
        frac = pmt.to_double(pmt.tuple_ref(t, 1))
        return int(round(sec * rate + frac * rate))
    except Exception:
        return None


def slot_position(ref_tn, ref_fn, ref_slot_bit, slot_bit):
    """TN/FN of the slot starting at slot_bit, given a SYNC burst's TN/FN/slot start.
    Returns (tn, fn, grid_error_bits)."""
    d = slot_bit - ref_slot_bit
    k = int(round(d / float(SLOT_BITS)))
    err = d - k * SLOT_BITS
    q = (ref_tn - 1) + k
    return q % 4 + 1, (ref_fn - 1 + q // 4) % 18 + 1, err


def _candidate_dll_dirs():
    dirs = []
    main = sys.modules.get('__main__')
    f = getattr(main, '__file__', None)
    if f:
        dirs.append(os.path.dirname(os.path.abspath(f)))
    try:
        dirs.append(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass
    dirs.append(os.getcwd())
    if sys.path and sys.path[0]:
        dirs.append(sys.path[0])
    out = []
    for d in dirs:
        d = os.path.normcase(os.path.abspath(d))
        if d not in out:
            out.append(d)
    return out


def _say(s):
    sys.stdout.write('[TCH/S] ' + s + '\n')
    sys.stdout.flush()


class blk(gr.sync_block):
    """TETRA TCH/S decoder: timeslot-aware, ETSI ACELP via tetra_dec.dll"""

    def __init__(self, dll_path='', cell_key=0, gain_db=0.0, polarity=-1.0,
                 target_tn=0, skip_fn18=True, prefill_ms=180.0):
        gr.sync_block.__init__(self, name='TETRA TCH/S ACELP decoder',
                               in_sig=None, out_sig=[np.float32])
        self.message_port_register_in(pmt.intern('tch'))
        self.message_port_register_in(pmt.intern('tdma'))
        self.set_msg_handler(pmt.intern('tch'), self._on_tch)
        self.set_msg_handler(pmt.intern('tdma'), self._on_tdma)

        self._dll_path = dll_path or ''
        self._lib = None
        self._lib_failed = False
        self._dll_lock = threading.Lock()   # the ETSI code keeps global state: one caller at a time

        self.gain = 10.0 ** (float(gain_db) / 20.0)
        self.polarity = float(polarity)
        self.skip_fn18 = bool(skip_fn18)
        self.prefill = int(8.0 * float(prefill_ms))   # samples at 8 kHz

        self._ref = None                    # (tn, fn, slot_bit) of the last good SYNC
        self._sel = None                    # timeslot being played (auto mode)
        self._hist = {t: deque() for t in (1, 2, 3, 4)}   # (bit, good) per TN, auto mode
        self._target = 0

        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_lock = threading.Lock()
        self._playing = False
        self.MAX_BUFFER = 8000              # 1 s

        self.n = dict(pdu=0, short=0, no_time=0, no_ref=0, off_grid=0, fn18=0)
        self.n_tn = {t: [0, 0, 0] for t in (1, 2, 3, 4)}   # seen, good, played
        self.n_unknown = [0, 0, 0]
        self._last_report = 0

        self._ck = None
        self.scr = np.ones(2 * BLK_LEN, dtype=np.float32)
        self.cell_key = cell_key
        self.target_tn = target_tn

    # ------------------------------------------------------------------ settable parameters
    @property
    def cell_key(self):
        return self._ck or 0

    @cell_key.setter
    def cell_key(self, value):
        v = int(value)
        if v == 0 or v == self._ck:
            return
        with self._dll_lock:
            self._ck = v
            self.scr = np.array(_scr_pm(v, 2 * BLK_LEN), dtype=np.float32)
            if self._lib is not None:
                self._lib.tetra_cd_init()
                self._lib.tetra_dec_init()
            self._sel = None
        _say('armed for cell key 0x%07X' % v)

    def set_cell_key(self, value):
        self.cell_key = value

    @property
    def target_tn(self):
        return self._target

    @target_tn.setter
    def target_tn(self, value):
        v = int(value)
        if v not in (-1, 0, 1, 2, 3, 4):
            v = 0
        self._target = v
        self._sel = None
        for h in self._hist.values():
            h.clear()
        _say('timeslot: %s' % {0: 'auto', -1: 'ALL (mixed)'}.get(v, 'TN %d' % v))

    def set_target_tn(self, value):
        self.target_tn = value

    # ------------------------------------------------------------------ DLL
    def _ensure_lib(self):
        if self._lib is not None or self._lib_failed:
            return self._lib
        path = self._dll_path
        if not path:
            for d in _candidate_dll_dirs():
                for name in ('tetra_dec.dll', 'libtetra_dec.so', 'tetra_dec.so'):
                    p = os.path.join(d, name)
                    if os.path.exists(p):
                        path = p
                        break
                if path:
                    break
        if not path:
            self._lib_failed = True
            _say('tetra_dec.dll not found next to the flowgraph: audio stays silent '
                 '(timeslot statistics still print)')
            return None
        try:
            if hasattr(os, 'add_dll_directory'):        # Windows: let MinGW runtime DLLs resolve
                try:
                    os.add_dll_directory(os.path.dirname(os.path.abspath(path)))
                except OSError:
                    pass
            lib = ctypes.CDLL(path)
            i16p = np.ctypeslib.ndpointer(np.int16, ndim=1, flags='C_CONTIGUOUS')
            lib.tetra_cd_frame.argtypes = [i16p, i16p]
            lib.tetra_cd_frame.restype = ctypes.c_int
            lib.tetra_dec_frame.argtypes = [i16p, i16p]
            lib.tetra_dec_frame.restype = None
            lib.tetra_cd_init.restype = None
            lib.tetra_dec_init.restype = None
            lib.tetra_cd_init()
            lib.tetra_dec_init()
            self._lib = lib
            _say('loaded %s' % path)
        except (OSError, AttributeError) as e:
            self._lib_failed = True
            _say('failed to load %s: %s' % (path, e))
        return self._lib

    # ------------------------------------------------------------------ messages
    def _on_tdma(self, msg):
        try:
            tn = pmt.to_long(pmt.dict_ref(msg, pmt.intern('tn'), pmt.PMT_NIL))
            fn = pmt.to_long(pmt.dict_ref(msg, pmt.intern('fn'), pmt.PMT_NIL))
            sb = pmt.to_long(pmt.dict_ref(msg, pmt.intern('slot_bit'), pmt.PMT_NIL))
        except Exception:
            return
        if 1 <= tn <= 4 and 1 <= fn <= 18:
            self._ref = (tn, fn, sb)

    def _locate(self, slot_bit):
        """-> (tn, fn) or None (unknown) or False (off grid = correlator false hit)"""
        ref = self._ref
        if ref is None or abs(slot_bit - ref[2]) > REF_MAX_AGE:
            return None
        tn, fn, err = slot_position(ref[0], ref[1], ref[2], slot_bit)
        if abs(err) > GRID_TOL:
            return False
        return tn, fn

    def _on_tch(self, msg):
        self.n['pdu'] += 1
        try:
            b = np.array(pmt.f32vector_elements(pmt.cdr(msg)), dtype=np.float32)
        except Exception:
            return
        if b.size != PDU_LEN:
            self.n['short'] += 1
            return
        tag_bit = abs_bit(pmt.car(msg))
        if tag_bit is None:
            self.n['no_time'] += 1
            pos = None
        else:
            pos = self._locate(tag_bit - TCH_TAG)
            if pos is False:
                self.n['off_grid'] += 1
                return
            if pos is None:
                self.n['no_ref'] += 1
        if pos is not None and pos[1] == 18 and self.skip_fn18:
            self.n['fn18'] += 1
            return
        tn = pos[0] if pos else None
        cnt = self.n_tn[tn] if tn else self.n_unknown

        # which slot is played?
        target = self._target
        if target == -1:
            play = True
        elif tn is None:
            play = False                      # TN unknown: never mix into the audio
        elif target == 0:
            play = None                       # decided after the channel decode
        else:
            play = (tn == target)
        if play is False and target != 0:
            cnt[0] += 1                       # counted, not decoded
            self._report()
            return

        lib = self._ensure_lib()
        slot = np.concatenate((b[:BLK_LEN], b[BLK2:BLK2 + BLK_LEN]))
        with self._dll_lock:
            soft = np.clip(np.round(self.polarity * slot * self.scr * 127.0),
                           -127, 127).astype(np.int16)
            if lib is None:
                good = False
                serial = None
            else:
                serial = np.zeros(276, dtype=np.int16)
                good = (lib.tetra_cd_frame(soft, serial) == 0)
            cnt[0] += 1
            cnt[1] += int(good)
            if play is None:
                play = self._auto_select(tn, tag_bit, good)
            if play and serial is not None:
                pcm = np.zeros(480, dtype=np.int16)
                for k in range(2):
                    fr = np.ascontiguousarray(serial[138 * k:138 * (k + 1)])
                    out = np.zeros(240, dtype=np.int16)
                    lib.tetra_dec_frame(fr, out)
                    pcm[240 * k:240 * (k + 1)] = out
                cnt[2] += 1
                self._push(pcm.astype(np.float32) / 32768.0 * self.gain)
        self._report()

    def _auto_select(self, tn, bit, good):
        """Called with _dll_lock held. Returns True if this frame should be played."""
        h = self._hist[tn]
        h.append((bit, good))
        for q in self._hist.values():
            while q and bit - q[0][0] > AUTO_WIN:
                q.popleft()
        score = {t: sum(g for _, g in q) for t, q in self._hist.items()}
        sel = self._sel
        if sel is None or score[sel] < AUTO_MIN_GOOD:
            best = max(score, key=score.get)
            new = best if score[best] >= AUTO_MIN_GOOD else None
            if new != sel:
                self._sel = new
                if new is not None and self._lib is not None:
                    self._lib.tetra_dec_init()       # fresh speech decoder for a new call
                _say('auto: %s' % ('playing TN %d' % new if new else 'no call heard'))
        return tn == self._sel

    def _push(self, out):
        with self._buf_lock:
            self._buf = np.concatenate((self._buf, out))
            if self._buf.size > self.MAX_BUFFER:
                self._buf = self._buf[-self.MAX_BUFFER:]

    def _report(self):
        tot = self.n['pdu']
        if tot - self._last_report < 200:
            return
        self._last_report = tot
        per = '  '.join('TN%d %d/%d/%d' % (t, *self.n_tn[t]) for t in (1, 2, 3, 4))
        _say('NTS1 bursts %d: %s  unknownTN %d/%d/%d  (seen/good/played)  '
             'dropped: off-grid %d, FN18 %d, no SYNC ref %d, bad len %d%s' % (
                 tot, per, *self.n_unknown, self.n['off_grid'], self.n['fn18'],
                 self.n['no_ref'], self.n['short'],
                 ', NO rx_time (check Tags To PDU rate)' if self.n['no_time'] else ''))

    # ------------------------------------------------------------------ audio out
    def work(self, input_items, output_items):
        out = output_items[0]
        n = 0
        with self._buf_lock:
            if not self._playing and self._buf.size >= self.prefill:
                self._playing = True              # start only with a cushion queued
            if self._playing:
                n = min(out.size, self._buf.size)
                if n:
                    out[:n] = self._buf[:n]
                    self._buf = self._buf[n:]
                if self._buf.size == 0:
                    self._playing = False         # ran dry: re-buffer before playing again
        if n < out.size:
            out[n:] = 0.0
        return out.size
