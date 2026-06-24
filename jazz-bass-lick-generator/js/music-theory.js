/**
 * Music Theory Engine
 * buildProgression now accepts nextIRoot so chord 4 (V/ii) resolves
 * to the NEXT row's ii chord, not the current row's.
 */

const MusicTheory = (() => {
    const NOTES = ['C', 'Db', 'D', 'Eb', 'E', 'F', 'Gb', 'G', 'Ab', 'A', 'Bb', 'B'];
    const INTERVAL_NAMES = ['R', 'b2', '2', 'b3', '3', '4', 'b5', '5', 'b6', '6', 'b7', '7'];

    const CHORD_INTERVALS = {
        'm7':   [0, 3, 7, 10],
        '7':    [0, 4, 7, 10],
        'maj7': [0, 4, 7, 11],
    };

    const SCALE_INTERVALS = {
        'Dorian':     [0, 2, 3, 5, 7, 9, 10],
        'Mixolydian': [0, 2, 4, 5, 7, 9, 10],
        'Ionian':     [0, 2, 4, 5, 7, 9, 11],
    };

    function noteName(n) { return NOTES[((n % 12) + 12) % 12]; }

    function chordNotes(root, type) {
        return (CHORD_INTERVALS[type] || [0,4,7]).map(i => (root + i) % 12);
    }

    function scaleNotes(root, type) {
        return (SCALE_INTERVALS[type] || SCALE_INTERVALS['Ionian']).map(i => (root + i) % 12);
    }

    function analyzeDegree(notePitch, root, chordType, scaleType) {
        const interval = ((notePitch - root) % 12 + 12) % 12;
        return {
            name: INTERVAL_NAMES[interval],
            interval,
            isChordTone: (CHORD_INTERVALS[chordType] || []).includes(interval),
            isScaleTone: (SCALE_INTERVALS[scaleType] || []).includes(interval),
            isChromatic: !(SCALE_INTERVALS[scaleType] || []).includes(interval),
        };
    }

    /**
     * Build ii → V → I → V/ii progression.
     *
     * @param {number} iRoot    - Root of the I chord (0-11)
     * @param {number|null} nextIRoot - Root of the NEXT row's I chord.
     *   If provided, chord 4 becomes V7 of (nextIRoot's ii),
     *   so it resolves directly into the next row's ii chord.
     *   If null, defaults to V/ii of the current row (iRoot + 9).
     */
    function buildProgression(iRoot, nextIRoot = null) {
        const iiRoot = (iRoot + 2) % 12;
        const vRoot  = (iRoot + 7) % 12;

        // V/ii root = V of the target ii chord
        // Target ii = (nextIRoot + 2) % 12  (or current ii if no next row)
        const targetIRoot  = nextIRoot !== null ? nextIRoot : iRoot;
        const targetIiRoot = (targetIRoot + 2) % 12;
        const vOfiiRoot    = (targetIiRoot + 7) % 12;

        // Label for chord 4 — clarify what it resolves to
        const nextIiName = noteName(targetIiRoot);
        const sameRow    = nextIRoot === null || nextIRoot === iRoot;
        const chord4Role = sameRow
            ? 'Secondary Dominant'
            : `Secondary Dominant → ${nextIiName}m7`;
        const chord4Note = sameRow
            ? `Its 3rd (${noteName((vOfiiRoot+4)%12)}) leads back to ${noteName(iiRoot)}. Pivots into next ii-V-I.`
            : `Its 3rd (${noteName((vOfiiRoot+4)%12)}) resolves to ${nextIiName}m7 — the ii of the next row's key.`;

        return [
            {
                degree: 'ii', chordType: 'm7', scaleType: 'Dorian', root: iiRoot,
                rootName: noteName(iiRoot), chord: chordNotes(iiRoot, 'm7'), scale: scaleNotes(iiRoot, 'Dorian'),
                role: 'Subdominant', roleNote: 'Pulls away from tonic, sets up the dominant.',
            },
            {
                degree: 'V', chordType: '7', scaleType: 'Mixolydian', root: vRoot,
                rootName: noteName(vRoot), chord: chordNotes(vRoot, '7'), scale: scaleNotes(vRoot, 'Mixolydian'),
                role: 'Dominant', roleNote: 'Tritone (3 ↔ b7) creates tension, resolves to I.',
            },
            {
                degree: 'I', chordType: 'maj7', scaleType: 'Ionian', root: iRoot,
                rootName: noteName(iRoot), chord: chordNotes(iRoot, 'maj7'), scale: scaleNotes(iRoot, 'Ionian'),
                role: 'Tonic', roleNote: 'Resolution and home base — melodic landing point.',
            },
            {
                degree: 'V/ii', chordType: '7', scaleType: 'Mixolydian', root: vOfiiRoot,
                rootName: noteName(vOfiiRoot), chord: chordNotes(vOfiiRoot, '7'), scale: scaleNotes(vOfiiRoot, 'Mixolydian'),
                role: chord4Role, roleNote: chord4Note,
            },
        ];
    }

    return {
        NOTES, INTERVAL_NAMES,
        noteName, chordNotes, scaleNotes,
        buildProgression, analyzeDegree,
    };
})();
