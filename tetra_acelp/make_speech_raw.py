"""
make_speech_raw.py -- turn any ordinary WAV file into the two inputs the TETRA codec tools need.

Usage (radioconda prompt, in the codec folder):
    python make_speech_raw.py my_recording.wav

Writes, next to this script:
    speech.raw   8 kHz, 16-bit, mono, no header   -> for verify_bitexact.py
    speech.wav   the same audio as an 8 kHz mono WAV -> for tetra_acelp_loopback.grc

Accepts 8/16/24/32-bit PCM WAV, any sample rate, mono or stereo (stereo is mixed to mono).
Needs only numpy (radioconda has it); uses scipy for better resampling if it is installed.
"""
import os
import sys
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
FS = 8000


def read_wav(path):
    try:
        w = wave.open(path, 'rb')
    except wave.Error as e:
        sys.exit('%s is not a plain PCM WAV (%s). Re-export it as "WAV (Microsoft) signed 16-bit PCM",\n'
                 'e.g. in Audacity: File > Export > Export as WAV.' % (path, e))
    ch, sw, fs, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
    raw = w.readframes(n)
    w.close()
    if sw == 1:
        x = (np.frombuffer(raw, np.uint8).astype(np.float64) - 128) / 128
    elif sw == 2:
        x = np.frombuffer(raw, '<i2').astype(np.float64) / 32768
    elif sw == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3)
        v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
        v = np.where(v & 0x800000, v - (1 << 24), v)
        x = v.astype(np.float64) / (1 << 23)
    elif sw == 4:
        x = np.frombuffer(raw, '<i4').astype(np.float64) / (1 << 31)
    else:
        sys.exit('unsupported sample width: %d bytes' % sw)
    x = x.reshape(-1, ch).mean(axis=1)
    return x, fs, ch, sw


def to_8k(x, fs):
    if fs == FS:
        return x
    from math import gcd
    g = gcd(int(fs), FS)
    try:
        from scipy.signal import resample_poly
        return resample_poly(x, FS // g, int(fs) // g)
    except ImportError:
        # no scipy: low-pass at 3.6 kHz with a windowed sinc, then linear interpolation
        nt = 255
        k = np.arange(nt) - nt // 2
        fc = 3600.0 / fs
        h = 2 * fc * np.sinc(2 * fc * k) * np.blackman(nt)
        y = np.convolve(x, h / h.sum(), mode='same')
        t_out = np.arange(int(len(x) * FS / fs)) / FS
        return np.interp(t_out, np.arange(len(x)) / fs, y)


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    src = sys.argv[1]
    x, fs, ch, sw = read_wav(src)
    y = to_8k(x, fs)
    pcm = np.clip(np.round(y * 32768), -32768, 32767).astype('<i2')
    pcm = pcm[:len(pcm) // 240 * 240]                      # whole 30 ms frames only
    raw_path = os.path.join(HERE, 'speech.raw')
    wav_path = os.path.join(HERE, 'speech.wav')
    if os.path.abspath(src) == os.path.abspath(wav_path):
        wav_path = os.path.join(HERE, 'speech_8k.wav')
    pcm.tofile(raw_path)
    with wave.open(wav_path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(FS)
        w.writeframes(pcm.tobytes())
    print('input : %s  (%d Hz, %d channel(s), %d-bit, %.1f s)' % (src, fs, ch, 8 * sw, len(x) / fs))
    print('wrote : %s  (%d frames of 30 ms)' % (raw_path, len(pcm) // 240))
    print('wrote : %s  (8 kHz mono 16-bit)' % wav_path)
    if os.path.basename(wav_path) != 'speech.wav':
        print('note  : your input was already called speech.wav, so the 8 kHz copy is speech_8k.wav;\n'
              '        set wav_in = \'speech_8k.wav\' in the flowgraph, or rename it')
    print('next  : python verify_bitexact.py speech.raw')


if __name__ == '__main__':
    main()
