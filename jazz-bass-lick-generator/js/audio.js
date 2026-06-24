/**
 * Audio Module — realistic bass guitar synthesis
 *
 * Signal chain:
 *   Sawtooth OSC ──┐
 *                  ├──► LPF (sweeps bright→warm) ──► Master gain ──► output
 *   Sub Sine OSC ──┘
 *   Click OSC  ─────────────────────────────────► Master gain
 *
 * The sweep LPF mimics a bass pickup/cabinet rolling off highs after the attack.
 * The sub sine reinforces the fundamental (real bass strings are rich in sub).
 * The click burst simulates the finger/pluck transient.
 */

const AudioModule = (() => {
    let ctx = null;
    let scheduled = [];
    let isPlaying = false;
    let stopTimeout = null;

    // Open string MIDI pitches: E2=40, A2=45, D3=50, G3=55
    const OPEN_MIDI = { E: 40, A: 45, D: 50, G: 55 };

    function getCtx() {
        if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
        if (ctx.state === 'suspended') ctx.resume();
        return ctx;
    }

    function midiToHz(midi) { return 440 * Math.pow(2, (midi - 69) / 12); }

    /** Build a soft-knee waveshaper curve for warm overdrive */
    function makeDistortionCurve(amount = 15) {
        const n = 256;
        const curve = new Float32Array(n);
        for (let i = 0; i < n; i++) {
            const x = (i * 2) / n - 1;
            curve[i] = (Math.PI + amount) * x / (Math.PI + amount * Math.abs(x));
        }
        return curve;
    }

    function scheduleNote(fretInfo, startTime, duration) {
        const ac = getCtx();
        const midi = (OPEN_MIDI[fretInfo.string] || 45) + fretInfo.fret;
        const hz   = midiToHz(midi);

        // ── Oscillators ──────────────────────────────────────────────────────
        // Sawtooth gives harmonics similar to a plucked string
        const saw = ac.createOscillator();
        saw.type = 'sawtooth';
        saw.frequency.value = hz;

        // Sub sine: one octave below, for the bass thump
        const sub = ac.createOscillator();
        sub.type = 'sine';
        sub.frequency.value = hz * 0.5;

        // Click: short square burst above the fundamental (finger attack)
        const click = ac.createOscillator();
        click.type = 'square';
        click.frequency.value = hz * 3;

        // ── Low-pass filter (sweep bright → warm) ────────────────────────────
        const lpf = ac.createBiquadFilter();
        lpf.type = 'lowpass';
        lpf.Q.value = 1.2;
        // Start open, quickly close — gives the characteristic bass "woof"
        lpf.frequency.setValueAtTime(4000, startTime);
        lpf.frequency.exponentialRampToValueAtTime(700, startTime + 0.06);

        // ── Warm overdrive ───────────────────────────────────────────────────
        const dist = ac.createWaveShaper();
        dist.curve = makeDistortionCurve(12);
        dist.oversample = '4x';

        // ── Amplitude envelopes ──────────────────────────────────────────────
        const gSaw   = ac.createGain();
        const gSub   = ac.createGain();
        const gClick = ac.createGain();
        const master = ac.createGain();

        const att = 0.004;          // 4 ms attack — punchy
        const dec = duration * 0.82; // decay within the note

        // Saw
        gSaw.gain.setValueAtTime(0, startTime);
        gSaw.gain.linearRampToValueAtTime(0.28, startTime + att);
        gSaw.gain.exponentialRampToValueAtTime(0.001, startTime + dec);

        // Sub (slightly slower attack — real bass sub takes a moment to bloom)
        gSub.gain.setValueAtTime(0, startTime);
        gSub.gain.linearRampToValueAtTime(0.38, startTime + att + 0.006);
        gSub.gain.exponentialRampToValueAtTime(0.001, startTime + dec);

        // Click (very short — just the transient)
        gClick.gain.setValueAtTime(0.12, startTime);
        gClick.gain.linearRampToValueAtTime(0, startTime + 0.018);

        master.gain.value = 0.8;

        // ── Routing ──────────────────────────────────────────────────────────
        saw.connect(gSaw);
        gSaw.connect(lpf);
        lpf.connect(dist);
        dist.connect(master);

        sub.connect(gSub);
        gSub.connect(master);          // sub bypasses LPF (keep low end clean)

        click.connect(gClick);
        gClick.connect(master);        // click bypasses LPF (we want the click bright)

        master.connect(ac.destination);

        // ── Schedule ─────────────────────────────────────────────────────────
        saw.start(startTime);   saw.stop(startTime + duration);
        sub.start(startTime);   sub.stop(startTime + duration);
        click.start(startTime); click.stop(startTime + 0.022);

        scheduled.push(saw, sub, click);
    }

    /** Play a flat array of notes (each with .fret = {string, fret}) */
    function playNotes(notes, tempo) {
        if (isPlaying) { stop(); return; }
        isPlaying = true;

        const ac   = getCtx();
        const beat = 60 / tempo;
        let t      = ac.currentTime + 0.05;

        for (const note of notes) {
            scheduleNote(note.fret || { string: 'A', fret: 0 }, t, beat * 0.88);
            t += beat;
        }

        if (stopTimeout) clearTimeout(stopTimeout);
        stopTimeout = setTimeout(() => { isPlaying = false; }, (t - ac.currentTime) * 1000 + 300);
    }

    function stop() {
        scheduled.forEach(o => { try { o.stop(); } catch (_) {} });
        scheduled = [];
        if (stopTimeout) clearTimeout(stopTimeout);
        isPlaying = false;
    }

    function getIsPlaying() { return isPlaying; }

    return { playNotes, stop, getIsPlaying, getCtx };
})();
