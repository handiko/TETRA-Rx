"""
build_tetra_codec.py -- build tetra_enc.dll and tetra_dec.dll (plus the four ETSI test
programs) from ETSI's own reference code. No Visual Studio, no MSYS2.

Run it in the radioconda prompt, in the folder that holds this script and tetra_codec_wrap.c:

    python build_tetra_codec.py

What it does
  1. Downloads en_30039502v010301p0.zip (EN 300 395-2 V1.3.1, the C code) from etsi.org,
     unless it is already in this folder, and checks its SHA-256.
  2. Unpacks it into etsi_src\\ (the ETSI files are NOT modified).
  3. Installs the zig C compiler from PyPI if needed (pip install ziglang, ~100 MB).
  4. Compiles
       tetra_enc.dll  = speech encoder + channel encoder + tetra_codec_wrap.c
       tetra_dec.dll  = channel decoder + speech decoder + tetra_codec_wrap.c
       scoder.exe ccoder.exe cdecoder.exe sdecoder.exe   (unmodified ETSI programs)
  5. If you give it a raw speech file, runs verify_bitexact.py on it.

Options
  --zip FILE      use this copy of the ETSI zip (no download)
  --cc "CMD"      use another C compiler, e.g. --cc gcc   (MinGW-w64, w64devkit, MSYS2)
  --target T      zig target, default: this machine (e.g. x86_64-windows-gnu)
  --verify FILE   after building, run verify_bitexact.py FILE (8 kHz 16-bit raw PCM)

Why Word32 is left alone: the ETSI code declares Word32 as 'long'. On Windows 'long' is
32 bits, which is what the code expects. On 64-bit Linux/macOS it is 64 bits; there this
script compiles a copy with Word32 = int (the change osmo-tetra's patches make).
"""
import argparse
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ZIP_NAME = 'en_30039502v010301p0.zip'
ZIP_URL = 'https://www.etsi.org/deliver/etsi_en/300300_300399/30039502/01.03.01_60/' + ZIP_NAME
ZIP_SHA256 = '1fe18c4773c8ccb52ef23ca5b4a0b0841b38d54ddd5d9c7d86eb8b060f132f39'

HERE = os.path.dirname(os.path.abspath(__file__))
COMMON = ['sub_sc_d.c', 'sub_dsp.c', 'fbas_tet.c', 'fexp_tet.c', 'fmat_tet.c', 'tetra_op.c']
ENC = ['scod_tet.c'] + COMMON + ['ccod_tet.c', 'sub_cc.c']
DEC = ['sdec_tet.c'] + COMMON + ['cdec_tet.c', 'sub_cd.c']
EXES = {'scoder': ['scoder.c', 'scod_tet.c'] + COMMON,
        'ccoder': ['ccoder.c', 'ccod_tet.c', 'sub_cc.c', 'tetra_op.c'],
        'sdecoder': ['sdecoder.c', 'sdec_tet.c'] + COMMON,
        'cdecoder': ['cdecoder.c', 'cdec_tet.c', 'sub_cd.c', 'tetra_op.c']}


def say(*a):
    print('[build]', *a, flush=True)


def get_zip(path):
    if path:
        return path
    path = os.path.join(HERE, ZIP_NAME)
    if not os.path.exists(path):
        say('downloading', ZIP_URL)
        req = urllib.request.Request(ZIP_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=60) as r, open(path, 'wb') as f:
            shutil.copyfileobj(r, f)
    return path


def unpack(zpath):
    h = hashlib.sha256(open(zpath, 'rb').read()).hexdigest()
    if h == ZIP_SHA256:
        say('ETSI zip SHA-256 OK')
    else:
        say('WARNING: SHA-256 %s does not match the known ETSI zip (%s...). Continuing; '
            'verify_bitexact.py will still tell you if the result is right.' % (h[:16], ZIP_SHA256[:16]))
    src = os.path.join(HERE, 'etsi_src')
    shutil.rmtree(src, ignore_errors=True)
    os.makedirs(src)
    # flatten the C-CODE folder into etsi_src/ with lower-case names (the zip uses upper case;
    # the #includes use lower case, which only matters on Linux/macOS)
    with zipfile.ZipFile(zpath) as z:
        names = [n for n in z.namelist() if re.search(r'(^|/)c-code/[^/]+$', n, re.I)]
        if not names:
            sys.exit('no C-CODE folder in %s' % zpath)
        for n in names:
            data = z.read(n).replace(b'\r\n', b'\n')
            with open(os.path.join(src, os.path.basename(n).lower()), 'wb') as f:
                f.write(data)
    if not os.path.exists(os.path.join(src, 'scod_tet.c')):
        sys.exit('unexpected zip content: scod_tet.c not found')
    return src


def compiler(cc, target):
    if cc:
        return cc.split(), (target or '')
    try:
        import ziglang  # noqa: F401
    except ImportError:
        say('installing the zig C compiler: pip install ziglang')
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'ziglang'])
    return [sys.executable, '-m', 'ziglang', 'cc'], target


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--zip')
    ap.add_argument('--cc')
    ap.add_argument('--target', default='')
    ap.add_argument('--verify')
    a = ap.parse_args()
    a.out = HERE

    src = unpack(get_zip(a.zip))
    cc, target = compiler(a.cc, a.target)
    windows = (target and 'windows' in target) or (not target and os.name == 'nt')
    if not windows and platform.architecture()[0] == '64bit':
        for h in ('source.h', 'channel.h'):              # Word32 must be 32 bits
            p = os.path.join(src, h)
            s = open(p).read()
            s2 = re.sub(r'typedef\s+long\s+Word32;', 'typedef int   Word32;', s)
            open(p, 'w').write(s2)
        say('64-bit non-Windows build: Word32 typed as int (it is long in the ETSI headers)')
    flags = ['-O2', '-w', '-fno-common', '-I', src]
    if target and 'ziglang' in cc:
        flags += ['-target', target]
    lib_ext = '.dll' if windows else ('.dylib' if sys.platform == 'darwin' else '.so')
    lib_pre = '' if windows else 'lib'
    exe_ext = '.exe' if windows else ''
    wrap = os.path.join(HERE, 'tetra_codec_wrap.c')
    os.makedirs(a.out, exist_ok=True)

    def run(cmd):
        say(' '.join(os.path.basename(c) if c.endswith('.c') else c for c in cmd[len(cc):]))
        subprocess.check_call(cmd)

    for name, define, files in (('tetra_enc', 'TETRA_WRAP_ENC', ENC), ('tetra_dec', 'TETRA_WRAP_DEC', DEC)):
        out = os.path.join(a.out, lib_pre + name + lib_ext)
        extra = [] if windows else ['-fPIC', '-fvisibility=hidden']
        run(cc + flags + extra + ['-shared', '-D' + define, '-o', out, wrap] + [os.path.join(src, f) for f in files])
    for name, files in EXES.items():
        run(cc + flags + ['-o', os.path.join(a.out, name + exe_ext)] + [os.path.join(src, f) for f in files])
    for junk in os.listdir(a.out):                       # zig leaves import libs / debug files
        if junk.endswith(('.lib', '.pdb', '.exp')) and junk.split('.')[0] in ('tetra_enc', 'tetra_dec', *EXES):
            os.remove(os.path.join(a.out, junk))
    say('done: %s%s, %s%s and the four ETSI programs are in %s' % (lib_pre, 'tetra_enc' + lib_ext, lib_pre,
                                                                     'tetra_dec' + lib_ext, a.out))
    if a.verify:
        subprocess.check_call([sys.executable, os.path.join(HERE, 'verify_bitexact.py'), a.verify], cwd=a.out)


if __name__ == '__main__':
    main()
