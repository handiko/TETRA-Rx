/*
 * tetra_codec_wrap.c -- thin frame-level wrapper around the ETSI EN 300 395-2
 * TETRA reference codec, so GNU Radio Embedded Python Blocks can call it via ctypes.
 *
 * The ETSI files are NOT modified. This file only replaces their four main()
 * programs (scoder.c, ccoder.c, cdecoder.c, sdecoder.c) with callable functions
 * that do exactly what those mains do, one frame at a time.
 *
 * Build it TWICE (see build_windows.bat):
 *   -DTETRA_WRAP_ENC  -> tetra_enc.dll : speech encoder + channel encoder
 *   -DTETRA_WRAP_DEC  -> tetra_dec.dll : channel decoder + speech decoder
 *
 * Why two DLLs and not one: the speech encoder and decoder both define the globals
 * last_ener_cod / last_ener_pit, and share Post_Process() filter memory. As separate
 * .exe programs that is harmless; linked into one DLL the decoder would overwrite the
 * encoder's state every frame. One DLL per direction keeps every state private,
 * exactly like the four ETSI executables.
 *
 * Data formats (identical to the ETSI files, 16-bit words):
 *   speech frame : 240 samples, 16-bit PCM, 8 kHz (30 ms)
 *   serial frame : 138 words = BFI + 137 bits, one bit (0/1) per word
 *   channel block: 432 words, one coded bit per word as a soft value,
 *                  +127 = bit 0, -127 = bit 1 (whole block, all classes), 60 ms
 */
#include <string.h>

#ifdef _WIN32
#  define EXPORT __declspec(dllexport)
#else
#  define EXPORT __attribute__((visibility("default")))
#endif

#if defined(TETRA_WRAP_ENC) == defined(TETRA_WRAP_DEC)
#  error "define exactly one of TETRA_WRAP_ENC / TETRA_WRAP_DEC"
#endif

typedef short Word16;
typedef int   Flag;

/* ---- ETSI routines used (prototypes as in source.h / channel.h) ------------------ */
extern Flag Overflow;                      /* tetra_op.c: saturation flag (global)   */

#ifdef TETRA_WRAP_ENC
extern Word16 *new_speech;                 /* scod_tet.c: where the next frame goes  */
void Init_Pre_Process(void);
void Pre_Process(Word16 signal[], Word16 lg);
void Init_Coder_Tetra(void);
void Coder_Tetra(Word16 ana[], Word16 synth[]);
void Post_Process(Word16 signal[], Word16 lg);
void Prm2bits_Tetra(Word16 prm[], Word16 bits[]);
void Channel_Encoding(short first_pass, Word16 Frame_Stealing, Word16 Input_Frame[], Word16 Output_Frame[]);
Word16 Interleaving_Speech(Word16 Input_Frame[], Word16 Output_frame[]);

/* The ETSI channel coder references this global (defined in ccoder.c's main file). */
Word16 FS_Flag = 0;

static short cc_first_pass = 1;
static Flag  ov_speech = 0, ov_chan = 0;   /* keep the two halves' Overflow apart */

EXPORT int tetra_wrap_version(void) { return 1; }

/* scoder: Init_Pre_Process + Init_Coder_Tetra */
EXPORT void tetra_enc_init(void)
{
    Init_Pre_Process();
    Init_Coder_Tetra();
    ov_speech = 0;
}

/* scoder main loop body: 240 PCM in -> 138-word serial frame (+ local synthesis) */
EXPORT void tetra_enc_frame(const Word16 *pcm240, Word16 *serial138, Word16 *lsynth240)
{
    Word16 ana[23];
    Word16 syn[240];
    Overflow = ov_speech;
    memcpy(new_speech, pcm240, 240 * sizeof(Word16));
    Pre_Process(new_speech, (Word16)240);
    Coder_Tetra(ana, syn);
    Post_Process(syn, (Word16)240);
    Prm2bits_Tetra(ana, serial138);         /* serial138[0] = BFI = 0 */
    ov_speech = Overflow;
    if (lsynth240) memcpy(lsynth240, syn, sizeof(syn));
}

/* ccoder: start of a new channel-coded stream */
EXPORT void tetra_cc_init(void) { cc_first_pass = 1; ov_chan = 0; }

/* ccoder main loop body: two serial frames (2 x 138 words, BFIs ignored as in ccoder.c)
   -> one interleaved 432-word TCH/S block (+-127) */
EXPORT void tetra_cc_frame(const Word16 *serial276, Word16 *out432)
{
    Word16 vocod[274], coded[432];
    memcpy(vocod, serial276 + 1, 137 * sizeof(Word16));
    memcpy(vocod + 137, serial276 + 139, 137 * sizeof(Word16));
    Overflow = ov_chan;
    Channel_Encoding(cc_first_pass, (Word16)0, vocod, coded);
    cc_first_pass = 0;
    Interleaving_Speech(coded, out432);
    ov_chan = Overflow;
}
#endif /* TETRA_WRAP_ENC */

#ifdef TETRA_WRAP_DEC
void Init_Decod_Tetra(void);
void Bits2prm_Tetra(Word16 bits[], Word16 prm[]);
void Decod_Tetra(Word16 parm[], Word16 synth[]);
void Post_Process(Word16 signal[], Word16 lg);
Word16 Channel_Decoding(short first_pass, Word16 Frame_Stealing, Word16 Input_Frame[], Word16 Output_Frame[]);
Word16 Desinterleaving_Speech(Word16 Input_Frame[], Word16 Output_frame[]);

/* The ETSI channel decoder references this global (defined in cdecoder.c's main file). */
Word16 Frame_stealing = 0;

static short cd_first_pass = 1;
static Flag  ov_speech = 0, ov_chan = 0;

EXPORT int tetra_wrap_version(void) { return 1; }

EXPORT void tetra_cd_init(void) { cd_first_pass = 1; ov_chan = 0; }

/* cdecoder main loop body: 432 soft values (-127..+127, +127 = bit 0)
   -> two serial frames (2 x 138 words, each BFI + 137 bits). Returns the BFI. */
EXPORT int tetra_cd_frame(const Word16 *in432, Word16 *serial276)
{
    Word16 inter[432], coded[432], reord[286];
    Word16 bfi;
    int i;
    for (i = 0; i < 432; i++) {            /* Read_Tetra_File range check, enforced */
        Word16 v = in432[i];
        inter[i] = v > 127 ? 127 : (v < -127 ? -127 : v);
    }
    Desinterleaving_Speech(inter, coded);
    Overflow = ov_chan;
    bfi = Channel_Decoding(cd_first_pass, (Word16)0, coded, reord);
    cd_first_pass = 0;
    ov_chan = Overflow;
    serial276[0] = bfi;                     /* cdecoder.c: bfi1 = bfi2 when not stealing */
    memcpy(serial276 + 1, reord, 137 * sizeof(Word16));
    serial276[138] = bfi;
    memcpy(serial276 + 139, reord + 137, 137 * sizeof(Word16));
    return bfi;
}

EXPORT void tetra_dec_init(void) { Init_Decod_Tetra(); ov_speech = 0; }

/* sdecoder main loop body: 138-word serial frame (BFI + 137) -> 240 PCM */
EXPORT void tetra_dec_frame(const Word16 *serial138, Word16 *pcm240)
{
    Word16 serial[138], parm[24];
    memcpy(serial, serial138, sizeof(serial));
    Overflow = ov_speech;
    Bits2prm_Tetra(serial, parm);
    Decod_Tetra(parm, pcm240);
    Post_Process(pcm240, (Word16)240);
    ov_speech = Overflow;
}
#endif /* TETRA_WRAP_DEC */
