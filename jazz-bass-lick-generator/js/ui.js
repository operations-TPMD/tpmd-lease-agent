/**
 * UI Module
 * Renders rows of ii-V-I-V/ii chord cards with inline tablature + explanation
 */

const UIModule = (() => {

    // ── Tab building ──────────────────────────────────────────────────────────

    /** Left-pad degree name to exactly 3 chars for monospace alignment */
    function padDeg(name) {
        return name.length === 1 ? ` ${name} ` : `${name} `;
    }

    /**
     * Build an HTML string for a <pre> element.
     * Includes 4 string lines + a degree-annotation line.
     * Chromatic notes are <span class="deg-chr">; chord tones are <span class="deg-ct">;
     * scale tones are <span class="deg-sc">.
     */
    function buildTabHtml(notes) {
        const order = ['G', 'D', 'A', 'E'];

        // Collect per-column data
        const cols = notes.map(note => {
            const str = note.fret?.string || 'A';
            const fr  = note.fret?.fret ?? 0;
            const deg = note.degreeInfo;

            const cells = {};
            for (const s of order) {
                cells[s] = s === str
                    ? String(fr).padStart(2, '-') + '-'
                    : '---';
            }

            const degLabel = padDeg(deg ? deg.name : '?');
            const cls = deg?.isChordTone ? 'deg-ct'
                      : deg?.isScaleTone  ? 'deg-sc'
                      : 'deg-chr';

            return { cells, degLabel, cls, deg, role: note.role };
        });

        // String lines (plain text — safe to include before degree spans)
        let html = '';
        for (const s of order) {
            html += `${s} |${cols.map(c => esc(c.cells[s])).join('')}|\n`;
        }

        // Degree row (3 leading spaces to align below "G |")
        html += `   `;
        for (const col of cols) {
            html += `<span class="${col.cls}">${esc(col.degLabel)}</span>`;
        }

        return html;
    }

    function esc(str) {
        return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    // ── Explanation text ──────────────────────────────────────────────────────

    const TECH_LABELS = {
        walking:         'Walking Bass',
        approach:        'Chromatic Approach',
        enclosure:       'Enclosure',
        'secondary-dom': 'Secondary Dominant',
    };

    const ROLE_TIPS = {
        root:          { label: 'R',  explain: 'Root — harmonic anchor, beats 1 & 3 are home base.' },
        passing:       { label: '~',  explain: 'Passing/scale tone — bridges between chord tones smoothly.' },
        'inner-chord': { label: 'CT', explain: 'Inner chord tone (3rd/5th/7th) — outlines the harmony.' },
        landing:       { label: 'CT', explain: 'Landing chord tone — strong-beat arrival, closes the phrase.' },
        approach:      { label: '↗',  explain: 'Chromatic approach — half-step below the target. Creates pull.' },
        target:        { label: 'CT', explain: 'Target chord tone — the note the approach resolves into.' },
        above:         { label: '↑',  explain: 'Enclosure from above — half-step above the target note.' },
        below:         { label: '↗',  explain: 'Enclosure from below — half-step below the target (= approach).' },
        tritone:       { label: 'b7', explain: 'Tritone partner (b7) — with the 3rd it defines the dominant tension.' },
        'leading-tone':{ label: '3',  explain: 'Leading tone (3rd of V/ii) — wants to resolve a half-step up into ii.' },
    };

    function buildExplanation(lick) {
        const { degree, chordType, scaleType, rootName, chord, notes, technique, role, roleNote } = lick;
        const chordLabel = `${rootName}${chordType}`;
        const chordToneNames = chord.map(n => MusicTheory.noteName(n)).join(', ');
        const techLabel = TECH_LABELS[technique] || technique;

        // Chromatic note explanations
        const chrNotes = notes.filter(n => n.degreeInfo?.isChromatic);
        const chrLines = chrNotes.map(n => {
            const tip = ROLE_TIPS[n.role] || {};
            const noteName = MusicTheory.noteName(n.pitch);
            return `<li><strong>${noteName} (${n.degreeInfo.name})</strong> — ${tip.explain || 'Chromatic passing tone outside the scale.'}</li>`;
        }).join('');

        return `
            <div class="explain-body">
                <div class="explain-row">
                    <span class="explain-role">${role}</span>
                    <span class="explain-rolenote">${roleNote}</span>
                </div>
                <div class="explain-row">
                    <span class="explain-key">Scale:</span>
                    <span>${scaleType} — chord tones: <strong>${chordToneNames}</strong></span>
                </div>
                <div class="explain-row">
                    <span class="explain-key">Technique:</span>
                    <span>${techLabel}</span>
                </div>
                ${chrLines ? `<ul class="chr-notes"><li class="chr-header">Out-of-scale notes:</li>${chrLines}</ul>` : ''}
            </div>
        `;
    }

    // ── Row rendering ─────────────────────────────────────────────────────────

    function renderRow(rowEl, progression) {
        const cardsEl = rowEl.querySelector('.row-cards');
        cardsEl.innerHTML = '';

        progression.licks.forEach(lick => {
            const card = document.createElement('div');
            card.className = 'chord-card';
            card.dataset.chord = lick.degree;

            const tabHtml = buildTabHtml(lick.notes);
            const explanHtml = buildExplanation(lick);

            card.innerHTML = `
                <div class="card-header">
                    <span class="degree">${lick.degree}</span>
                    <span class="chord-name">${lick.rootName}${lick.chordType}</span>
                </div>
                <div class="tab-wrap">
                    <pre class="tab-display">${tabHtml}</pre>
                </div>
                <div class="legend-row">
                    <span class="leg deg-ct-demo">■ chord tone</span>
                    <span class="leg deg-sc-demo">■ scale tone</span>
                    <span class="leg deg-chr-demo">■ chromatic</span>
                </div>
                ${explanHtml}
            `;

            cardsEl.appendChild(card);
        });
    }

    /**
     * Update the connector label between rows.
     * @param {Element} connectorEl
     * @param {object}  topProgression  - The row above
     * @param {number}  nextIRoot       - The next row's I root (integer 0-11)
     */
    function updateConnector(connectorEl, topProgression, nextIRoot) {
        if (!topProgression || !connectorEl) return;
        const vii        = topProgression.licks[3]; // V/ii is chord 4
        const nextIiName = MusicTheory.noteName((nextIRoot + 2) % 12);
        connectorEl.innerHTML =
            `<span class="connector-arrow">↓</span>` +
            `<span class="connector-text">` +
            `<strong>${vii.rootName}${vii.chordType}</strong> resolves → ` +
            `<strong>${nextIiName}m7</strong> (ii of next row)</span>`;
    }

    function toggleEarTraining(hidden) {
        document.querySelectorAll('.tab-display').forEach(el => {
            el.classList.toggle('blurred', hidden);
        });
    }

    return { renderRow, updateConnector, toggleEarTraining };
})();
