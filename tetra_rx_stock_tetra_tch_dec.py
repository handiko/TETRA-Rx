#!/usr/bin/env python3
"""
TETRA TCH/S ACELP decoder -- Embedded Python Block, GNU Radio 3.10

Inputs (message): 'blk1', 'blk2' -- the two 216-float soft blocks that
                   cut_blk1 / cut_blk2 already emit for the existing SCH/HD path
Output (stream):   float32 PCM, 8 kHz mono, one sample per output item

Calls tetra_dec.dll (built from ETSI EN 300 395-2) for the channel decode and
the ACELP speech synthesis. That DLL is bit-exact with the ETSI scoder/ccoder/
cdecoder/sdecoder programs (verify_bitexact.py proves it).
"""
import ctypes, os, threading
import numpy as np
import pmt
from gnuradio import gr

import tetra_fec            # the same tables module your flowgraph already imports


class blk(gr.basic_block):
    def __init__(self, dll_path='', cell_key=0, gain_db=0.0, polarity=-1.0):
        gr.basic_block.__init__(self, name='TETRA TCH/S ACELP decoder',
                                in_sig=None, out_sig=[np.float32])
        self.message_port_register_in(pmt.intern('blk1'))
        self.message_port_register_in(pmt.intern('blk2'))
        self.set_msg_handler(pmt.intern('blk1'), self._on_blk1)
        self.set_msg_handler(pmt.intern('blk2'), self._on_blk2)

        # ---- locate and load the DLL --------------------------------------
        if not dll_path:
            dll_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    'tetra_dec.dll')
            if not os.path.exists(dll_path):
                dll_path = dll_path[:-4] + '.so'
        self.lib = ctypes.CDLL(dll_path)
        i16 = np.ctypeslib.ndpointer(np.int16, flags='C_CONTIGUOUS')
        self.lib.tetra_cd_frame.argtypes  = [i16, i16]
        self.lib.tetra_cd_frame.restype   = ctypes.c_int
        self.lib.tetra_dec_frame.argtypes = [i16, i16]
        self.lib.tetra_cd_init()
        self.lib.tetra_dec_init()

        # ---- state --------------------------------------------------------
        self._ck  = None
        self.scr  = np.ones(432, dtype=np.float32)
        self.mute = True
        self.gain = 10.0 ** (gain_db / 20.0)
        self.polarity = float(polarity)          # -1 = ETSI convention
        self.blk1 = None
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_lock = threading.Lock()

        self.n_calls = self.n_ok = self.n_bfi = 0
        self.cell_key = cell_key                 # triggers the setter below

    # ------------------------------------------------------------------ properties
    @property
    def cell_key(self):
        return self._ck or 0

    @cell_key.setter
    def cell_key(self, value):
        """Called by the flowgraph's set_cell_key() when tetra_dec publishes a new key."""
        v = int(value)
        if v == 0 or v == self._ck:
            return
        self._ck = v
        self.scr  = np.array(tetra_fec.scr_pm(v, 432), dtype=np.float32)
        self.mute = False
        self.lib.tetra_cd_init()                 # fresh state for a new cell
        self.lib.tetra_dec_init()
        self._print('TCH/S decoder armed for cell key 0x%07X' % v)

    # ------------------------------------------------------------------ messages
    @staticmethod
    def _print(s):
        import sys; sys.stdout.write('[TCH/S] ' + s + '\n'); sys.stdout.flush()

    def _on_blk1(self, msg):
        b = np.array(pmt.f32vector_elements(pmt.cdr(msg)), dtype=np.float32)
        if b.size == 216:
            self.blk1 = b

    def _on_blk2(self, msg):
        if self.blk1 is None:
            return
        b = np.array(pmt.f32vector_elements(pmt.cdr(msg)), dtype=np.float32)
        if b.size != 216:
            return
        slot = np.concatenate((self.blk1, b))
        self.blk1 = None                         # consumed

        # ---- descramble + convert to ETSI soft bits -----------------------
        # pol_mul already gives correct-sign soft bits for the Viterbi path
        # (GNU convention: + = bit 1). ETSI wants +127 = bit 0, so negate.
        soft = np.clip(np.round(self.polarity * slot * self.scr * 127.0),
                       -127, 127).astype(np.int16)

        # ---- channel decode + speech synthesis ----------------------------
        serial = np.zeros(276, dtype=np.int16)
        bfi = self.lib.tetra_cd_frame(
            soft.ctypes.data_as(ctypes.POINTER(ctypes.c_short)),
            serial.ctypes.data_as(ctypes.POINTER(ctypes.c_short)))

        pcm = np.zeros(480, dtype=np.int16)
        for k in range(2):
            fr = np.ascontiguousarray(serial[138*k : 138*(k+1)])
            self.lib.tetra_dec_frame(
                fr.ctypes.data_as(ctypes.POINTER(ctypes.c_short)),
                pcm[240*k : 240*(k+1)].ctypes.data_as(ctypes.POINTER(ctypes.c_short)))
        self.n_calls += 1
        if bfi:
            self.n_bfi += 1

        out = pcm.astype(np.float32) / 32768.0 * self.gain
        with self._buf_lock:
            self._buf = np.concatenate((self._buf, out))

    # ------------------------------------------------------------------ work
    def work(self, input_items, output_items):
        out = output_items[0]
        with self._buf_lock:
            n = min(out.size, self._buf.size)
            out[:n] = self._buf[:n]
            self._buf = self._buf[n:]
        if n < out.size:
            out[n:] = 0.0                        # underrun = silence
        return out.size