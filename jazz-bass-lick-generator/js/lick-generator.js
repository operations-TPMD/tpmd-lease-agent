/**
 * Lick Generator — pattern-based, MIDI-aware
 *
 * Instead of random pitch-class selection, all licks come from a library of
 * proven jazz bass patterns (signed semitone offsets from root).
 * MIDI numbers (not pitch classes) are generated first, then converted to
 * string/fret so that ascending/descending motion is accurate on the neck.
 */

const LickGenerator = (() => {

    // ── String/fret helpers ───────────────────────────────────────────────────

    // Open-string MIDI notes: E2=40, A2=45, D3=50, G3=55
    const OPEN = [
        { name: 'E', midi: 40 },
        { name: 'A', midi: 45 },
        { name: 'D', midi: 50 },
        { name: 'G', midi: 55 },
    ];

    /** MIDI → {string, fret} — prefers the lowest fret available (open strings first) */
    function midiToStringFret(midi) {
        let best = null;
        let bestFret = Infinity;
        for (const s of OPEN) {
            const fret = midi - s.midi;
            if (fret >= 0 && fret <= 12 && fret < bestFret) {
                bestFret = fret;
                best = { string: s.name, fret };
            }
        }
        // Fallback — clamp to A string
        if (!best) {
            const fret = Math.max(0, Math.min(12, midi - 45));
            best = { string: 'A', fret };
        }
        return best;
    }

    /**
     * Find MIDI note with pitch class `pc` closest to `center`.
     * Keeps the result within the usable bass range (MIDI 40–67).
     */
    function closestMidi(pc, center) {
        const base = center - ((center - pc + 120) % 12);
        const candidates = [base - 12, base, base + 12];
        let best = candidates[1];
        let bestDist = Infinity;
        for (const m of candidates) {
            if (m < 38 || m > 68) continue; // outside playable range
            const dist = Math.abs(m - center);
            if (dist < bestDist) { bestDist = dist; best = m; }
        }
        return best;
    }

    // ── Pattern library ───────────────────────────────────────────────────────
    //
    // Each pattern is an array of 4 signed semitone offsets from the chord root.
    // Positive = above root, negative = below root.
    // The note is mapped to a real MIDI pitch (with correct octave) before display.

    const PATTERNS = {
        // ── Minor 7 (Dorian / ii chord) ───────────────────────────────────────
        'm7': [
            { steps: [ 0,  2,  3,  7], name: 'Walk-up',         tech: 'walking' },   // R 2 b3 5
            { steps: [ 0, -2, -3, -5], name: 'Walk-down',        tech: 'walking' },   // R b7 6 5 (descend)
            { steps: [ 0,  3,  7, 10], name: 'Arpeggio',         tech: 'walking' },   // R b3 5 b7
            { steps: [ 0,  2,  4,  3], name: 'Enclosure of b3',  tech: 'enclosure' }, // R 2 M3(above) b3
            { steps: [ 0,  2,  6,  7], name: 'Chromatic to 5',   tech: 'approach' },  // R 2 #4 5
            { steps: [ 0,  7,  5,  3], name: 'Descend to b3',    tech: 'walking' },   // R 5 4 b3
        ],

        // ── Dominant 7 (Mixolydian / V chord) ────────────────────────────────
        '7': [
            { steps: [ 0,  2,  4,  7], name: 'Walk-up',          tech: 'walking' },   // R 2 3 5
            { steps: [ 0, -2, -3, -5], name: 'Walk-down',         tech: 'walking' },   // R b7 6 5
            { steps: [ 0,  4,  7, 10], name: 'Arpeggio',          tech: 'walking' },   // R 3 5 b7
            { steps: [ 0,  4,  6,  7], name: 'Tritone approach',  tech: 'approach' },  // R 3 b5 5
            { steps: [ 0,  5,  3,  4], name: 'Enclosure of 3',    tech: 'enclosure' }, // R 4(above) b3(below) 3
            { steps: [ 0,  7,  9,  4], name: 'Descend to 3',      tech: 'walking' },   // R 5 6 3
        ],

        // ── Major 7 (Ionian / I chord) ────────────────────────────────────────
        'maj7': [
            { steps: [ 0,  2,  4,  7], name: 'Walk-up',          tech: 'walking' },   // R 2 3 5
            { steps: [ 0, -1, -3, -5], name: 'Walk-down',         tech: 'walking' },   // R 7 6 5 (descend)
            { steps: [ 0,  4,  7, 11], name: 'Arpeggio',          tech: 'walking' },   // R 3 5 7
            { steps: [ 0,  7,  4,  2], name: 'Descend from 5',    tech: 'walking' },   // R 5 3 2
            { steps: [ 0,  4,  9,  7], name: 'Major 6th lick',    tech: 'walking' },   // R 3 6 5
            { steps: [ 0,  5,  4,  3], name: 'Enclosure of 3',    tech: 'enclosure' }, // R #3(above) b3(below) 3
        ],

        // ── Secondary Dominant (V/ii) — emphasise leading-tone (the 3rd) ─────
        '7_sec': [
            { steps: [ 0,  4, 10,  4], name: 'Tritone → leading',  tech: 'secondary-dom' }, // R 3 b7 3
            { steps: [ 0,  7,  6,  4], name: 'Descend to leading', tech: 'secondary-dom' }, // R 5 #4 3
            { steps: [ 0, 10,  7,  4], name: 'Reverse arpeggio',   tech: 'secondary-dom' }, // R b7 5 3
            { steps: [ 0,  2,  4,  3], name: 'Chrom into leading', tech: 'approach' },      // R 2 3 b3(chrom)
        ],
    };

    function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

    // ── Register variation ────────────────────────────────────────────────────
    //
    // Bass center controls which octave the root lands in.
    // Low=43, Normal=48, High=52 — picked randomly to avoid "always on E string" monotony.

    const CENTERS = [43, 43, 45, 48, 48, 48, 50, 52];

    // ── Lick builder ──────────────────────────────────────────────────────────

    function generateLickForChord(chordData) {
        const { root, chordType, scaleType, degree } = chordData;

        // Choose pattern library
        const isSecondary = degree === 'V/ii';
        const library = isSecondary ? PATTERNS['7_sec'] : (PATTERNS[chordType] || PATTERNS['7']);
        const pattern  = pick(library);

        const center   = pick(CENTERS);
        const rootMidi = closestMidi(root, center);

        // Build notes
        const notes = pattern.steps.map((step, i) => {
            let midi = rootMidi + step;
            // Clamp to playable bass range
            if (midi < 40) midi += 12;
            if (midi > 67) midi -= 12;

            const pc       = ((midi % 12) + 12) % 12;
            const fretInfo = midiToStringFret(midi);
            const degInfo  = MusicTheory.analyzeDegree(pc, root, chordType, scaleType);

            // Role labels for explanation
            const roles = {
                walking:        ['root', 'passing', 'inner-chord', 'landing'],
                approach:       ['root', 'passing', 'approach',    'target'],
                enclosure:      ['root', 'passing', 'above',       'target'],
                'secondary-dom':['root', 'tritone', 'approach',    'leading-tone'],
            };
            const role = (roles[pattern.tech] || roles.walking)[i];

            return { pitch: pc, midi, fret: fretInfo, duration: 0.5, role, degreeInfo: degInfo };
        });

        return { notes, technique: pattern.tech, patternName: pattern.name };
    }

    function generateProgression(iRoot, nextIRoot = null) {
        const chords = MusicTheory.buildProgression(iRoot, nextIRoot);
        const licks  = chords.map(chord => ({ ...chord, ...generateLickForChord(chord) }));
        return {
            iRoot,
            keyName: MusicTheory.noteName(iRoot),
            licks,
            allNotes: licks.flatMap(l => l.notes),
        };
    }

    return { generateProgression };
})();
