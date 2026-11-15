#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: TETRA RX - stock FEC chain, cell info (live, osmocom RTL-SDR)
# Description: TETRA DL: RTL-SDR, stock front end, stock frame sync, stock descramble/deinterleave/depuncture/Viterbi; EPBs for CRC + PDU parsing and timeslot-aware TCH/S voice
# GNU Radio version: 3.10.10.0

from PyQt5 import Qt
from gnuradio import qtgui
from PyQt5 import QtCore
from PyQt5.QtCore import QObject, pyqtSlot
from gnuradio import analog
from gnuradio import audio
from gnuradio import blocks
from gnuradio import digital
from gnuradio import eng_notation
from gnuradio import filter
from gnuradio.filter import firdes
from gnuradio import gr
from gnuradio.fft import window
import sys
import signal
from PyQt5 import Qt
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import gr, pdu
from gnuradio import pdu
import pmt
import pmt, numpy as np
from gnuradio import trellis
from gnuradio import trellis, digital
import math
import osmosdr
import time
import sip
import tetra_rx_live_osmo_epy_block_0 as epy_block_0  # embedded python block
import tetra_rx_live_osmo_tetra_dec as tetra_dec  # embedded python block
import tetra_rx_live_osmo_tetra_fec as tetra_fec  # embedded python module


def snipfcn_fll_limit(self):
    lim = 2 * 3.141592653589793 * self.fll_range_hz / self.ch_rate
    self.fll_0.set_max_freq(lim)
    self.fll_0.set_min_freq(-lim)

def snipfcn_quiet_cutters(self):
    for b in (self.cut_bsch, self.cut_blk1, self.cut_blk2, self.cut_tch):
        b.set_log_level('critical')


def snippets_main_after_init(tb):
    snipfcn_fll_limit(tb)
    snipfcn_quiet_cutters(tb)

class tetra_rx_live_osmo(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "TETRA RX - stock FEC chain, cell info (live, osmocom RTL-SDR)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("TETRA RX - stock FEC chain, cell info (live, osmocom RTL-SDR)")
        qtgui.util.check_set_qss()
        try:
            self.setWindowIcon(Qt.QIcon.fromTheme('gnuradio-grc'))
        except BaseException as exc:
            print(f"Qt GUI: Could not set Icon: {str(exc)}", file=sys.stderr)
        self.top_scroll_layout = Qt.QVBoxLayout()
        self.setLayout(self.top_scroll_layout)
        self.top_scroll = Qt.QScrollArea()
        self.top_scroll.setFrameStyle(Qt.QFrame.NoFrame)
        self.top_scroll_layout.addWidget(self.top_scroll)
        self.top_scroll.setWidgetResizable(True)
        self.top_widget = Qt.QWidget()
        self.top_scroll.setWidget(self.top_widget)
        self.top_layout = Qt.QVBoxLayout(self.top_widget)
        self.top_grid_layout = Qt.QGridLayout()
        self.top_layout.addLayout(self.top_grid_layout)

        self.settings = Qt.QSettings("GNU Radio", "tetra_rx_live_osmo")

        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except BaseException as exc:
            print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)

        ##################################################
        # Variables
        ##################################################
        self.tetra_freq_entry = tetra_freq_entry = 390.225
        self.tetra_freq = tetra_freq = tetra_freq_entry*1e6
        self.sps = sps = 4
        self.samp_rate = samp_rate = 1.152e6
        self.nfilts = nfilts = 32
        self.lo_offset = lo_offset = 150e3
        self.decim = decim = 16
        self.constellation = constellation = digital.constellation_rect([1+1j, -1+1j, 1-1j, -1-1j], [0, 1, 2, 3],
        4, 2, 2, 1, 1).base()
        self.cell_key = cell_key = 0
        self.vv_len = vv_len = 128
        self.target_tn = target_tn = 0
        self.rrc_taps = rrc_taps = firdes.root_raised_cosine(nfilts, nfilts*sps, 1.0, 0.35, 11*sps*nfilts)
        self.rf_gain = rf_gain = 25.4
        self.rf_fc = rf_fc = tetra_freq - lo_offset
        self.ppm = ppm = 35
        self.pol = pol = 1.0
        self.hd_scr = hd_scr = tetra_fec.scr_pm(cell_key, 216)
        self.fll_range_hz = fll_range_hz = 8e3
        self.fine_hz = fine_hz = 0
        self.f_scr = f_scr = tetra_fec.scr_pm(cell_key, 432)
        self.dqpsk_diff = dqpsk_diff = digital.constellation_calcdist([(0.7071067811865476+0.7071067811865476j), (-0.7071067811865476+0.7071067811865476j), (0.7071067811865476-0.7071067811865476j), (-0.7071067811865476-0.7071067811865476j)], [0, 1, 2, 3], 4, 1, digital.constellation.AMPLITUDE_NORMALIZATION).base()
        self.chan_cut = chan_cut = 13.5e3
        self.ch_rate = ch_rate = samp_rate/decim
        self.algorithm = algorithm = digital.adaptive_algorithm_cma( constellation, .005, 1).base()

        ##################################################
        # Blocks
        ##################################################

        # Create the options list
        self._target_tn_options = [0, 1, 2, 3, 4, -1]
        # Create the labels list
        self._target_tn_labels = ['auto', 'TN1', 'TN2', 'TN3', 'TN4', 'all (mixed)']
        # Create the combo box
        # Create the radio buttons
        self._target_tn_group_box = Qt.QGroupBox("Voice timeslot" + ": ")
        self._target_tn_box = Qt.QHBoxLayout()
        class variable_chooser_button_group(Qt.QButtonGroup):
            def __init__(self, parent=None):
                Qt.QButtonGroup.__init__(self, parent)
            @pyqtSlot(int)
            def updateButtonChecked(self, button_id):
                self.button(button_id).setChecked(True)
        self._target_tn_button_group = variable_chooser_button_group()
        self._target_tn_group_box.setLayout(self._target_tn_box)
        for i, _label in enumerate(self._target_tn_labels):
            radio_button = Qt.QRadioButton(_label)
            self._target_tn_box.addWidget(radio_button)
            self._target_tn_button_group.addButton(radio_button, i)
        self._target_tn_callback = lambda i: Qt.QMetaObject.invokeMethod(self._target_tn_button_group, "updateButtonChecked", Qt.Q_ARG("int", self._target_tn_options.index(i)))
        self._target_tn_callback(self.target_tn)
        self._target_tn_button_group.buttonClicked[int].connect(
            lambda i: self.set_target_tn(self._target_tn_options[i]))
        self.top_grid_layout.addWidget(self._target_tn_group_box, 3, 1, 1, 2)
        for r in range(3, 4):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(1, 3):
            self.top_grid_layout.setColumnStretch(c, 1)
        self._rf_gain_range = qtgui.Range(0, 49.6, 0.1, 25.4, 200)
        self._rf_gain_win = qtgui.RangeWidget(self._rf_gain_range, self.set_rf_gain, "RF gain [dB]", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_grid_layout.addWidget(self._rf_gain_win, 0, 0, 1, 1)
        for r in range(0, 1):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self._ppm_range = qtgui.Range(-60, 60, 0.5, 35, 200)
        self._ppm_win = qtgui.RangeWidget(self._ppm_range, self.set_ppm, "Freq corr [ppm]", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_grid_layout.addWidget(self._ppm_win, 0, 1, 1, 1)
        for r in range(0, 1):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(1, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        # Create the options list
        self._pol_options = [1.0, -1.0]
        # Create the labels list
        self._pol_labels = ['normal', 'inverted']
        # Create the combo box
        # Create the radio buttons
        self._pol_group_box = Qt.QGroupBox("Soft-bit polarity" + ": ")
        self._pol_box = Qt.QHBoxLayout()
        class variable_chooser_button_group(Qt.QButtonGroup):
            def __init__(self, parent=None):
                Qt.QButtonGroup.__init__(self, parent)
            @pyqtSlot(int)
            def updateButtonChecked(self, button_id):
                self.button(button_id).setChecked(True)
        self._pol_button_group = variable_chooser_button_group()
        self._pol_group_box.setLayout(self._pol_box)
        for i, _label in enumerate(self._pol_labels):
            radio_button = Qt.QRadioButton(_label)
            self._pol_box.addWidget(radio_button)
            self._pol_button_group.addButton(radio_button, i)
        self._pol_callback = lambda i: Qt.QMetaObject.invokeMethod(self._pol_button_group, "updateButtonChecked", Qt.Q_ARG("int", self._pol_options.index(i)))
        self._pol_callback(self.pol)
        self._pol_button_group.buttonClicked[int].connect(
            lambda i: self.set_pol(self._pol_options[i]))
        self.top_grid_layout.addWidget(self._pol_group_box, 3, 0, 1, 1)
        for r in range(3, 4):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self._fine_hz_range = qtgui.Range((-samp_rate/2)/1e3, (samp_rate/2)/1e3, 1, 0, 500)
        self._fine_hz_win = qtgui.RangeWidget(self._fine_hz_range, self.set_fine_hz, "Fine tune [kHz]", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_grid_layout.addWidget(self._fine_hz_win, 0, 2, 1, 1)
        for r in range(0, 1):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(2, 3):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.y_zero = blocks.multiply_const_ff(0)
        self.xlate_0 = filter.freq_xlating_fir_filter_ccf(decim, firdes.low_pass(1.0, samp_rate, 16e3, 8e3), (tetra_freq - rf_fc + fine_hz*1e3), samp_rate)
        self.vv_rot = analog.phase_modulator_fc(1.0)
        self.vv_quarter = blocks.multiply_const_ff((-0.25))
        self.vv_pow = blocks.multiply_vcc(1)
        self.vv_mul = blocks.multiply_vcc(1)
        self.vv_dly = blocks.delay(gr.sizeof_gr_complex*1, (vv_len//2))
        self.vv_avg = blocks.moving_average_cc(vv_len, -1.0/vv_len, 4000, 1)
        self.vv_arg = blocks.complex_to_arg(1)
        self._tetra_freq_entry_tool_bar = Qt.QToolBar(self)
        self._tetra_freq_entry_tool_bar.addWidget(Qt.QLabel("TETRA Freq (MHz)" + ": "))
        self._tetra_freq_entry_line_edit = Qt.QLineEdit(str(self.tetra_freq_entry))
        self._tetra_freq_entry_tool_bar.addWidget(self._tetra_freq_entry_line_edit)
        self._tetra_freq_entry_line_edit.editingFinished.connect(
            lambda: self.set_tetra_freq_entry(eng_notation.str_to_num(str(self._tetra_freq_entry_line_edit.text()))))
        self.top_layout.addWidget(self._tetra_freq_entry_tool_bar)
        self.tetra_dec = tetra_dec.blk(rf_hz=tetra_freq + fine_hz*1e3, stats_every=10.0, show_pdus=False)
        self.soft_0 = digital.constellation_soft_decoder_cf(dqpsk_diff, -1)
        self.schf_vit = trellis.viterbi_combined_fb(trellis.fsm(2, 16, 16, tetra_fec.NS, tetra_fec.OS), tetra_fec.F_STEPS, 0, 0, 4, tetra_fec.VIT_TABLE, digital.TRELLIS_EUCLIDEAN)
        self.schf_v2s = blocks.vector_to_stream(gr.sizeof_float*1, 432)
        self.schf_t2p = pdu.tagged_stream_to_pdu(gr.types.byte_t, 'blk')
        self.schf_s2v = blocks.stream_to_vector(gr.sizeof_float*1, 432)
        self.schf_s2t = blocks.stream_to_tagged_stream(gr.sizeof_char, 1, tetra_fec.F_STEPS, "blk")
        self.schf_p2s = pdu.pdu_to_tagged_stream(gr.types.float_t, 'packet_len')
        self.schf_join = pdu.pdu_lambda(lambda x: np.concatenate((x[:216], x[268:484])) if len(x) == 484 else np.zeros(432, np.float32), "UVEC", pmt.intern("key"))
        self.schf_erase = blocks.null_source(gr.sizeof_float*1)
        self.schf_dp8 = blocks.interleave(gr.sizeof_float*1, 1)
        self.schf_dp3 = blocks.deinterleave(gr.sizeof_float*1, 1)
        self.schf_descr = blocks.multiply_const_vff(f_scr)
        self.schf_deint = trellis.permutation(432, tetra_fec.DEINT_432, 1, gr.sizeof_float*1)
        self.qtgui_freq_sink_x_0 = qtgui.freq_sink_c(
            2048, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            rf_fc, #fc
            samp_rate, #bw
            "RTL-SDR capture (channel at +lo_offset)", #name
            1,
            None # parent
        )
        self.qtgui_freq_sink_x_0.set_update_time(0.10)
        self.qtgui_freq_sink_x_0.set_y_axis((-100), (-20))
        self.qtgui_freq_sink_x_0.set_y_label('Relative Gain', 'dB')
        self.qtgui_freq_sink_x_0.set_trigger_mode(qtgui.TRIG_MODE_FREE, 0.0, 0, "")
        self.qtgui_freq_sink_x_0.enable_autoscale(False)
        self.qtgui_freq_sink_x_0.enable_grid(True)
        self.qtgui_freq_sink_x_0.set_fft_average(0.2)
        self.qtgui_freq_sink_x_0.enable_axis_labels(True)
        self.qtgui_freq_sink_x_0.enable_control_panel(False)
        self.qtgui_freq_sink_x_0.set_fft_window_normalized(False)



        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.qtgui_freq_sink_x_0.set_line_label(i, "Data {0}".format(i))
            else:
                self.qtgui_freq_sink_x_0.set_line_label(i, labels[i])
            self.qtgui_freq_sink_x_0.set_line_width(i, widths[i])
            self.qtgui_freq_sink_x_0.set_line_color(i, colors[i])
            self.qtgui_freq_sink_x_0.set_line_alpha(i, alphas[i])

        self._qtgui_freq_sink_x_0_win = sip.wrapinstance(self.qtgui_freq_sink_x_0.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._qtgui_freq_sink_x_0_win)
        self.pol_mul = blocks.multiply_const_ff(pol)
        self.p_zero = blocks.multiply_const_ff(0)
        self.osmo_src = osmosdr.source(
            args="numchan=" + str(1) + " " + "rtl=0"
        )
        self.osmo_src.set_sample_rate(samp_rate)
        self.osmo_src.set_center_freq(rf_fc, 0)
        self.osmo_src.set_freq_corr(ppm, 0)
        self.osmo_src.set_dc_offset_mode(0, 0)
        self.osmo_src.set_iq_balance_mode(0, 0)
        self.osmo_src.set_gain_mode(False, 0)
        self.osmo_src.set_gain(rf_gain, 0)
        self.osmo_src.set_if_gain(20, 0)
        self.osmo_src.set_bb_gain(20, 0)
        self.osmo_src.set_antenna('', 0)
        self.osmo_src.set_bandwidth(0, 0)
        self.num_0 = qtgui.number_sink(
            gr.sizeof_float,
            0.02,
            qtgui.NUM_GRAPH_HORIZ,
            2,
            None # parent
        )
        self.num_0.set_update_time(0.20)
        self.num_0.set_title("FLL carrier offset")

        labels = ["carrier offset", "ppm to add", '', '', '',
            '', '', '', '', '']
        units = ["Hz", "ppm", '', '', '',
            '', '', '', '', '']
        colors = [("black", "black"), ("black", "black"), ("black", "black"), ("black", "black"), ("black", "black"),
            ("black", "black"), ("black", "black"), ("black", "black"), ("black", "black"), ("black", "black")]
        factor = [-ch_rate/6.283185307179586, ch_rate/6.283185307179586/(tetra_freq/1e6), 1, 1, 1,
            1, 1, 1, 1, 1]

        for i in range(2):
            self.num_0.set_min(i, -8000)
            self.num_0.set_max(i, 8000)
            self.num_0.set_color(i, colors[i][0], colors[i][1])
            if len(labels[i]) == 0:
                self.num_0.set_label(i, "Data {0}".format(i))
            else:
                self.num_0.set_label(i, labels[i])
            self.num_0.set_unit(i, units[i])
            self.num_0.set_factor(i, factor[i])

        self.num_0.enable_autoscale(False)
        self._num_0_win = sip.wrapinstance(self.num_0.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._num_0_win, 2, 0, 1, 3)
        for r in range(2, 3):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 3):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.null_ph = blocks.null_sink(gr.sizeof_float*1)
        self.n_zero = blocks.multiply_const_ff(0)
        self.hd_vit = trellis.viterbi_combined_fb(trellis.fsm(2, 16, 16, tetra_fec.NS, tetra_fec.OS), tetra_fec.HD_STEPS, 0, 0, 4, tetra_fec.VIT_TABLE, digital.TRELLIS_EUCLIDEAN)
        self.hd_v2s = blocks.vector_to_stream(gr.sizeof_float*1, 216)
        self.hd_t2p = pdu.tagged_stream_to_pdu(gr.types.byte_t, 'blk')
        self.hd_s2v = blocks.stream_to_vector(gr.sizeof_float*1, 216)
        self.hd_s2t = blocks.stream_to_tagged_stream(gr.sizeof_char, 1, tetra_fec.HD_STEPS, "blk")
        self.hd_p2s = pdu.pdu_to_tagged_stream(gr.types.float_t, 'packet_len')
        self.hd_erase = blocks.null_source(gr.sizeof_float*1)
        self.hd_dp8 = blocks.interleave(gr.sizeof_float*1, 1)
        self.hd_dp3 = blocks.deinterleave(gr.sizeof_float*1, 1)
        self.hd_descr = blocks.multiply_const_vff(hd_scr)
        self.hd_deint = trellis.permutation(216, tetra_fec.DEINT_216, 1, gr.sizeof_float*1)
        self.fll_0 = digital.fll_band_edge_cc(sps, 0.35, 45, 0.0314)
        self.fft_0 = qtgui.freq_sink_c(
            2048, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            (tetra_freq + fine_hz*1e3), #fc
            ch_rate, #bw
            "72 kHz around the channel", #name
            1,
            None # parent
        )
        self.fft_0.set_update_time(0.10)
        self.fft_0.set_y_axis((-100), (-20))
        self.fft_0.set_y_label('Relative Gain', 'dB')
        self.fft_0.set_trigger_mode(qtgui.TRIG_MODE_FREE, 0.0, 0, "")
        self.fft_0.enable_autoscale(False)
        self.fft_0.enable_grid(True)
        self.fft_0.set_fft_average(0.2)
        self.fft_0.enable_axis_labels(True)
        self.fft_0.enable_control_panel(False)
        self.fft_0.set_fft_window_normalized(False)

        self.fft_0.disable_legend()


        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.fft_0.set_line_label(i, "Data {0}".format(i))
            else:
                self.fft_0.set_line_label(i, labels[i])
            self.fft_0.set_line_width(i, widths[i])
            self.fft_0.set_line_color(i, colors[i])
            self.fft_0.set_line_alpha(i, alphas[i])

        self._fft_0_win = sip.wrapinstance(self.fft_0.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._fft_0_win, 1, 0, 1, 2)
        for r in range(1, 2):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.epy_block_0 = epy_block_0.blk(dll_path='', cell_key=cell_key, gain_db=3.0, polarity=-1.0, target_tn=target_tn, skip_fn18=True, prefill_ms=180.0)
        self.digital_linear_equalizer_0 = digital.linear_equalizer(16, 1, algorithm, True, [ ], 'corr_est')
        self.diff_0 = digital.diff_phasor_cc()
        self.d_tch = blocks.delay(gr.sizeof_float*1, tetra_fec.D_NB1)
        self.d_sb2 = blocks.delay(gr.sizeof_float*1, tetra_fec.D_SB2)
        self.d_nb2 = blocks.delay(gr.sizeof_float*1, tetra_fec.D_NB2)
        self.d_nb1 = blocks.delay(gr.sizeof_float*1, tetra_fec.D_NB1)
        self.d_bsch = blocks.delay(gr.sizeof_float*1, tetra_fec.D_BSCH)
        self.cut_tch = pdu.tags_to_pdu_f(pmt.intern('slot'), pmt.intern('EOB'), 484, 2*ch_rate/sps, [], False, 0, 0.0)
        self.cut_tch.set_eob_parameters(1, 0)
        self.cut_tch.enable_time_debug(False)
        self.cut_bsch = pdu.tags_to_pdu_f(pmt.intern('slot'), pmt.intern('EOB'), 120, 2*ch_rate/sps, [], False, 0, 0.0)
        self.cut_bsch.set_eob_parameters(1, 0)
        self.cut_bsch.enable_time_debug(False)
        self.cut_blk2 = pdu.tags_to_pdu_f(pmt.intern('slot'), pmt.intern('EOB'), 216, 2*ch_rate/sps, [], False, 0, 0.0)
        self.cut_blk2.set_eob_parameters(1, 0)
        self.cut_blk2.enable_time_debug(False)
        self.cut_blk1 = pdu.tags_to_pdu_f(pmt.intern('slot'), pmt.intern('EOB'), 216, 2*ch_rate/sps, [], False, 0, 0.0)
        self.cut_blk1.set_eob_parameters(1, 0)
        self.cut_blk1.enable_time_debug(False)
        self.corr_y = digital.correlate_access_code_tag_ff('11000001100111001110100111000001100111', 4, 'slot')
        self.corr_p = digital.correlate_access_code_tag_ff('0111101001000011011110', 1, 'slot')
        self.corr_n = digital.correlate_access_code_tag_ff('1101000011101001110100', 1, 'slot')
        self.const_0 = qtgui.const_sink_c(
            1024, #size
            'Symbol Constellation', #name
            1, #number of inputs
            None # parent
        )
        self.const_0.set_update_time(0.10)
        self.const_0.set_y_axis((-1.5), 1.5)
        self.const_0.set_x_axis((-1.5), 1.5)
        self.const_0.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, "")
        self.const_0.enable_autoscale(False)
        self.const_0.enable_grid(True)
        self.const_0.enable_axis_labels(True)


        labels = ['symbol', 'phase steps -> decoder', 'raw phase steps', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["red", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        styles = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        markers = [1, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        alphas = [0.5, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.const_0.set_line_label(i, "Data {0}".format(i))
            else:
                self.const_0.set_line_label(i, labels[i])
            self.const_0.set_line_width(i, widths[i])
            self.const_0.set_line_color(i, colors[i])
            self.const_0.set_line_style(i, styles[i])
            self.const_0.set_line_marker(i, markers[i])
            self.const_0.set_line_alpha(i, alphas[i])

        self._const_0_win = sip.wrapinstance(self.const_0.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._const_0_win, 1, 2, 1, 1)
        for r in range(1, 2):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(2, 3):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.clock_0 = digital.pfb_clock_sync_ccf(sps, 0.0628, rrc_taps, nfilts, (nfilts/2), 1.5, 1)
        self.chan_lpf = filter.fir_filter_ccf(1, firdes.low_pass(1.0, ch_rate, chan_cut, 3e3))
        self.chan_lpf.declare_sample_delay(0)
        self.cell_to_var = blocks.msg_pair_to_var(self.set_cell_key)
        self.bsch_vit = trellis.viterbi_combined_fb(trellis.fsm(2, 16, 16, tetra_fec.NS, tetra_fec.OS), tetra_fec.BSCH_STEPS, 0, 0, 4, tetra_fec.VIT_TABLE, digital.TRELLIS_EUCLIDEAN)
        self.bsch_v2s = blocks.vector_to_stream(gr.sizeof_float*1, 120)
        self.bsch_t2p = pdu.tagged_stream_to_pdu(gr.types.byte_t, 'blk')
        self.bsch_s2v = blocks.stream_to_vector(gr.sizeof_float*1, 120)
        self.bsch_s2t = blocks.stream_to_tagged_stream(gr.sizeof_char, 1, tetra_fec.BSCH_STEPS, "blk")
        self.bsch_p2s = pdu.pdu_to_tagged_stream(gr.types.float_t, 'packet_len')
        self.bsch_erase = blocks.null_source(gr.sizeof_float*1)
        self.bsch_dp8 = blocks.interleave(gr.sizeof_float*1, 1)
        self.bsch_dp3 = blocks.deinterleave(gr.sizeof_float*1, 1)
        self.bsch_descr = blocks.multiply_const_vff(tetra_fec.BSCH_SCR)
        self.bsch_deint = trellis.permutation(120, tetra_fec.DEINT_120, 1, gr.sizeof_float*1)
        self.audio_sink_0 = audio.sink(8000, '', True)
        self.analog_agc_xx_0 = analog.agc_cc((10e-3), 0.25, 1.0, 65536)
        self.add_tch = blocks.add_vff(1)
        self.add_bsch = blocks.add_vff(1)
        self.add_blk2 = blocks.add_vff(1)
        self.add_blk1 = blocks.add_vff(1)


        ##################################################
        # Connections
        ##################################################
        self.msg_connect((self.bsch_t2p, 'pdus'), (self.tetra_dec, 'bsch'))
        self.msg_connect((self.cut_blk1, 'pdus'), (self.hd_p2s, 'pdus'))
        self.msg_connect((self.cut_blk2, 'pdus'), (self.hd_p2s, 'pdus'))
        self.msg_connect((self.cut_bsch, 'pdus'), (self.bsch_p2s, 'pdus'))
        self.msg_connect((self.cut_bsch, 'pdus'), (self.tetra_dec, 'bsch_raw'))
        self.msg_connect((self.cut_tch, 'pdus'), (self.epy_block_0, 'tch'))
        self.msg_connect((self.cut_tch, 'pdus'), (self.schf_join, 'pdu'))
        self.msg_connect((self.hd_t2p, 'pdus'), (self.tetra_dec, 'hd'))
        self.msg_connect((self.schf_join, 'pdu'), (self.schf_p2s, 'pdus'))
        self.msg_connect((self.schf_t2p, 'pdus'), (self.tetra_dec, 'schf'))
        self.msg_connect((self.tetra_dec, 'cell'), (self.cell_to_var, 'inpair'))
        self.msg_connect((self.tetra_dec, 'tdma'), (self.epy_block_0, 'tdma'))
        self.connect((self.add_blk1, 0), (self.cut_blk1, 0))
        self.connect((self.add_blk2, 0), (self.cut_blk2, 0))
        self.connect((self.add_bsch, 0), (self.cut_bsch, 0))
        self.connect((self.add_tch, 0), (self.cut_tch, 0))
        self.connect((self.analog_agc_xx_0, 0), (self.fll_0, 0))
        self.connect((self.bsch_deint, 0), (self.bsch_dp3, 0))
        self.connect((self.bsch_descr, 0), (self.bsch_v2s, 0))
        self.connect((self.bsch_dp3, 1), (self.bsch_dp8, 1))
        self.connect((self.bsch_dp3, 0), (self.bsch_dp8, 0))
        self.connect((self.bsch_dp3, 2), (self.bsch_dp8, 4))
        self.connect((self.bsch_dp8, 0), (self.bsch_vit, 0))
        self.connect((self.bsch_erase, 0), (self.bsch_dp8, 3))
        self.connect((self.bsch_erase, 0), (self.bsch_dp8, 5))
        self.connect((self.bsch_erase, 0), (self.bsch_dp8, 6))
        self.connect((self.bsch_erase, 0), (self.bsch_dp8, 2))
        self.connect((self.bsch_erase, 0), (self.bsch_dp8, 7))
        self.connect((self.bsch_p2s, 0), (self.bsch_s2v, 0))
        self.connect((self.bsch_s2t, 0), (self.bsch_t2p, 0))
        self.connect((self.bsch_s2v, 0), (self.bsch_descr, 0))
        self.connect((self.bsch_v2s, 0), (self.bsch_deint, 0))
        self.connect((self.bsch_vit, 0), (self.bsch_s2t, 0))
        self.connect((self.chan_lpf, 0), (self.analog_agc_xx_0, 0))
        self.connect((self.clock_0, 0), (self.digital_linear_equalizer_0, 0))
        self.connect((self.corr_n, 0), (self.n_zero, 0))
        self.connect((self.corr_p, 0), (self.p_zero, 0))
        self.connect((self.corr_y, 0), (self.y_zero, 0))
        self.connect((self.d_bsch, 0), (self.add_bsch, 0))
        self.connect((self.d_nb1, 0), (self.add_blk1, 0))
        self.connect((self.d_nb2, 0), (self.add_blk2, 2))
        self.connect((self.d_sb2, 0), (self.add_blk2, 1))
        self.connect((self.d_tch, 0), (self.add_tch, 1))
        self.connect((self.diff_0, 0), (self.vv_dly, 0))
        self.connect((self.diff_0, 0), (self.vv_pow, 1))
        self.connect((self.diff_0, 0), (self.vv_pow, 2))
        self.connect((self.diff_0, 0), (self.vv_pow, 0))
        self.connect((self.diff_0, 0), (self.vv_pow, 3))
        self.connect((self.digital_linear_equalizer_0, 0), (self.diff_0, 0))
        self.connect((self.epy_block_0, 0), (self.audio_sink_0, 1))
        self.connect((self.epy_block_0, 0), (self.audio_sink_0, 0))
        self.connect((self.fll_0, 0), (self.clock_0, 0))
        self.connect((self.fll_0, 2), (self.null_ph, 0))
        self.connect((self.fll_0, 3), (self.null_ph, 1))
        self.connect((self.fll_0, 1), (self.num_0, 0))
        self.connect((self.fll_0, 1), (self.num_0, 1))
        self.connect((self.hd_deint, 0), (self.hd_dp3, 0))
        self.connect((self.hd_descr, 0), (self.hd_v2s, 0))
        self.connect((self.hd_dp3, 1), (self.hd_dp8, 1))
        self.connect((self.hd_dp3, 0), (self.hd_dp8, 0))
        self.connect((self.hd_dp3, 2), (self.hd_dp8, 4))
        self.connect((self.hd_dp8, 0), (self.hd_vit, 0))
        self.connect((self.hd_erase, 0), (self.hd_dp8, 7))
        self.connect((self.hd_erase, 0), (self.hd_dp8, 2))
        self.connect((self.hd_erase, 0), (self.hd_dp8, 5))
        self.connect((self.hd_erase, 0), (self.hd_dp8, 6))
        self.connect((self.hd_erase, 0), (self.hd_dp8, 3))
        self.connect((self.hd_p2s, 0), (self.hd_s2v, 0))
        self.connect((self.hd_s2t, 0), (self.hd_t2p, 0))
        self.connect((self.hd_s2v, 0), (self.hd_descr, 0))
        self.connect((self.hd_v2s, 0), (self.hd_deint, 0))
        self.connect((self.hd_vit, 0), (self.hd_s2t, 0))
        self.connect((self.n_zero, 0), (self.add_tch, 0))
        self.connect((self.osmo_src, 0), (self.qtgui_freq_sink_x_0, 0))
        self.connect((self.osmo_src, 0), (self.xlate_0, 0))
        self.connect((self.p_zero, 0), (self.add_blk1, 1))
        self.connect((self.p_zero, 0), (self.d_nb2, 0))
        self.connect((self.pol_mul, 0), (self.add_blk2, 0))
        self.connect((self.pol_mul, 0), (self.corr_n, 0))
        self.connect((self.pol_mul, 0), (self.corr_p, 0))
        self.connect((self.pol_mul, 0), (self.corr_y, 0))
        self.connect((self.pol_mul, 0), (self.d_bsch, 0))
        self.connect((self.pol_mul, 0), (self.d_nb1, 0))
        self.connect((self.pol_mul, 0), (self.d_tch, 0))
        self.connect((self.schf_deint, 0), (self.schf_dp3, 0))
        self.connect((self.schf_descr, 0), (self.schf_v2s, 0))
        self.connect((self.schf_dp3, 0), (self.schf_dp8, 0))
        self.connect((self.schf_dp3, 1), (self.schf_dp8, 1))
        self.connect((self.schf_dp3, 2), (self.schf_dp8, 4))
        self.connect((self.schf_dp8, 0), (self.schf_vit, 0))
        self.connect((self.schf_erase, 0), (self.schf_dp8, 2))
        self.connect((self.schf_erase, 0), (self.schf_dp8, 6))
        self.connect((self.schf_erase, 0), (self.schf_dp8, 3))
        self.connect((self.schf_erase, 0), (self.schf_dp8, 7))
        self.connect((self.schf_erase, 0), (self.schf_dp8, 5))
        self.connect((self.schf_p2s, 0), (self.schf_s2v, 0))
        self.connect((self.schf_s2t, 0), (self.schf_t2p, 0))
        self.connect((self.schf_s2v, 0), (self.schf_descr, 0))
        self.connect((self.schf_v2s, 0), (self.schf_deint, 0))
        self.connect((self.schf_vit, 0), (self.schf_s2t, 0))
        self.connect((self.soft_0, 0), (self.pol_mul, 0))
        self.connect((self.vv_arg, 0), (self.vv_quarter, 0))
        self.connect((self.vv_avg, 0), (self.vv_arg, 0))
        self.connect((self.vv_dly, 0), (self.vv_mul, 0))
        self.connect((self.vv_mul, 0), (self.const_0, 0))
        self.connect((self.vv_mul, 0), (self.soft_0, 0))
        self.connect((self.vv_pow, 0), (self.vv_avg, 0))
        self.connect((self.vv_quarter, 0), (self.vv_rot, 0))
        self.connect((self.vv_rot, 0), (self.vv_mul, 1))
        self.connect((self.xlate_0, 0), (self.chan_lpf, 0))
        self.connect((self.xlate_0, 0), (self.fft_0, 0))
        self.connect((self.y_zero, 0), (self.add_bsch, 1))
        self.connect((self.y_zero, 0), (self.d_sb2, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("GNU Radio", "tetra_rx_live_osmo")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_tetra_freq_entry(self):
        return self.tetra_freq_entry

    def set_tetra_freq_entry(self, tetra_freq_entry):
        self.tetra_freq_entry = tetra_freq_entry
        self.set_tetra_freq(self.tetra_freq_entry*1e6)
        Qt.QMetaObject.invokeMethod(self._tetra_freq_entry_line_edit, "setText", Qt.Q_ARG("QString", eng_notation.num_to_str(self.tetra_freq_entry)))

    def get_tetra_freq(self):
        return self.tetra_freq

    def set_tetra_freq(self, tetra_freq):
        self.tetra_freq = tetra_freq
        self.set_rf_fc(self.tetra_freq - self.lo_offset)
        self.fft_0.set_frequency_range((self.tetra_freq + self.fine_hz*1e3), self.ch_rate)
        self.tetra_dec.rf_hz = self.tetra_freq + self.fine_hz*1e3
        self.xlate_0.set_center_freq((self.tetra_freq - self.rf_fc + self.fine_hz*1e3))

    def get_sps(self):
        return self.sps

    def set_sps(self, sps):
        self.sps = sps
        self.set_rrc_taps(firdes.root_raised_cosine(self.nfilts, self.nfilts*self.sps, 1.0, 0.35, 11*self.sps*self.nfilts))
        self.cut_blk1.set_rate(2*self.ch_rate/self.sps)
        self.cut_blk2.set_rate(2*self.ch_rate/self.sps)
        self.cut_bsch.set_rate(2*self.ch_rate/self.sps)
        self.cut_tch.set_rate(2*self.ch_rate/self.sps)

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.set_ch_rate(self.samp_rate/self.decim)
        self.osmo_src.set_sample_rate(self.samp_rate)
        self.qtgui_freq_sink_x_0.set_frequency_range(self.rf_fc, self.samp_rate)
        self.xlate_0.set_taps(firdes.low_pass(1.0, self.samp_rate, 16e3, 8e3))

    def get_nfilts(self):
        return self.nfilts

    def set_nfilts(self, nfilts):
        self.nfilts = nfilts
        self.set_rrc_taps(firdes.root_raised_cosine(self.nfilts, self.nfilts*self.sps, 1.0, 0.35, 11*self.sps*self.nfilts))

    def get_lo_offset(self):
        return self.lo_offset

    def set_lo_offset(self, lo_offset):
        self.lo_offset = lo_offset
        self.set_rf_fc(self.tetra_freq - self.lo_offset)

    def get_decim(self):
        return self.decim

    def set_decim(self, decim):
        self.decim = decim
        self.set_ch_rate(self.samp_rate/self.decim)

    def get_constellation(self):
        return self.constellation

    def set_constellation(self, constellation):
        self.constellation = constellation

    def get_cell_key(self):
        return self.cell_key

    def set_cell_key(self, cell_key):
        self.cell_key = cell_key
        self.set_f_scr(tetra_fec.scr_pm(self.cell_key, 432))
        self.set_hd_scr(tetra_fec.scr_pm(self.cell_key, 216))
        self.epy_block_0.cell_key = self.cell_key

    def get_vv_len(self):
        return self.vv_len

    def set_vv_len(self, vv_len):
        self.vv_len = vv_len
        self.vv_avg.set_length_and_scale(self.vv_len, -1.0/self.vv_len)
        self.vv_dly.set_dly(int((self.vv_len//2)))

    def get_target_tn(self):
        return self.target_tn

    def set_target_tn(self, target_tn):
        self.target_tn = target_tn
        self._target_tn_callback(self.target_tn)
        self.epy_block_0.target_tn = self.target_tn

    def get_rrc_taps(self):
        return self.rrc_taps

    def set_rrc_taps(self, rrc_taps):
        self.rrc_taps = rrc_taps
        self.clock_0.update_taps(self.rrc_taps)

    def get_rf_gain(self):
        return self.rf_gain

    def set_rf_gain(self, rf_gain):
        self.rf_gain = rf_gain
        self.osmo_src.set_gain(self.rf_gain, 0)

    def get_rf_fc(self):
        return self.rf_fc

    def set_rf_fc(self, rf_fc):
        self.rf_fc = rf_fc
        self.osmo_src.set_center_freq(self.rf_fc, 0)
        self.qtgui_freq_sink_x_0.set_frequency_range(self.rf_fc, self.samp_rate)
        self.xlate_0.set_center_freq((self.tetra_freq - self.rf_fc + self.fine_hz*1e3))

    def get_ppm(self):
        return self.ppm

    def set_ppm(self, ppm):
        self.ppm = ppm
        self.osmo_src.set_freq_corr(self.ppm, 0)

    def get_pol(self):
        return self.pol

    def set_pol(self, pol):
        self.pol = pol
        self._pol_callback(self.pol)
        self.pol_mul.set_k(self.pol)

    def get_hd_scr(self):
        return self.hd_scr

    def set_hd_scr(self, hd_scr):
        self.hd_scr = hd_scr
        self.hd_descr.set_k(self.hd_scr)

    def get_fll_range_hz(self):
        return self.fll_range_hz

    def set_fll_range_hz(self, fll_range_hz):
        self.fll_range_hz = fll_range_hz

    def get_fine_hz(self):
        return self.fine_hz

    def set_fine_hz(self, fine_hz):
        self.fine_hz = fine_hz
        self.fft_0.set_frequency_range((self.tetra_freq + self.fine_hz*1e3), self.ch_rate)
        self.tetra_dec.rf_hz = self.tetra_freq + self.fine_hz*1e3
        self.xlate_0.set_center_freq((self.tetra_freq - self.rf_fc + self.fine_hz*1e3))

    def get_f_scr(self):
        return self.f_scr

    def set_f_scr(self, f_scr):
        self.f_scr = f_scr
        self.schf_descr.set_k(self.f_scr)

    def get_dqpsk_diff(self):
        return self.dqpsk_diff

    def set_dqpsk_diff(self, dqpsk_diff):
        self.dqpsk_diff = dqpsk_diff
        self.soft_0.set_constellation(self.dqpsk_diff)

    def get_chan_cut(self):
        return self.chan_cut

    def set_chan_cut(self, chan_cut):
        self.chan_cut = chan_cut
        self.chan_lpf.set_taps(firdes.low_pass(1.0, self.ch_rate, self.chan_cut, 3e3))

    def get_ch_rate(self):
        return self.ch_rate

    def set_ch_rate(self, ch_rate):
        self.ch_rate = ch_rate
        self.chan_lpf.set_taps(firdes.low_pass(1.0, self.ch_rate, self.chan_cut, 3e3))
        self.cut_blk1.set_rate(2*self.ch_rate/self.sps)
        self.cut_blk2.set_rate(2*self.ch_rate/self.sps)
        self.cut_bsch.set_rate(2*self.ch_rate/self.sps)
        self.cut_tch.set_rate(2*self.ch_rate/self.sps)
        self.fft_0.set_frequency_range((self.tetra_freq + self.fine_hz*1e3), self.ch_rate)

    def get_algorithm(self):
        return self.algorithm

    def set_algorithm(self, algorithm):
        self.algorithm = algorithm




def main(top_block_cls=tetra_rx_live_osmo, options=None):

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls()
    snippets_main_after_init(tb)
    tb.start()

    tb.show()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()

        Qt.QApplication.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    timer = Qt.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    qapp.exec_()

if __name__ == '__main__':
    main()
