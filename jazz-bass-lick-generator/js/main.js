/**
 * Main — multi-row state with cross-row V/ii linking
 *
 * Chord 4 of row N = V7 of row N+1's ii chord.
 * When row N+1's key changes, row N auto-regenerates chord 4.
 */

document.addEventListener('DOMContentLoaded', () => {
    AudioModule.getCtx();

    const rows          = []; // [{ key, progression, rowEl }]
    const tempoSlider   = document.getElementById('tempo-slider');
    const tempoDisplay  = document.getElementById('tempo-display');
    const playAllBtn    = document.getElementById('play-all-btn');
    const addRowBtn     = document.getElementById('add-row-btn');
    const hideToggle    = document.getElementById('hide-tabs');
    const rowsContainer = document.getElementById('rows-container');

    tempoSlider.addEventListener('input', () => {
        tempoDisplay.textContent = `${tempoSlider.value} BPM`;
    });

    addRowBtn.addEventListener('click', () => addRow(0));
    playAllBtn.addEventListener('click', playAll);
    hideToggle.addEventListener('change', () => UIModule.toggleEarTraining(hideToggle.checked));

    // ── Row management ────────────────────────────────────────────────────────

    function addRow(defaultKey = 0) {
        const idx = rows.length;

        // Connector (between rows)
        if (idx > 0) {
            const conn = document.createElement('div');
            conn.className = 'row-connector';
            conn.id = `connector-${idx}`;
            rowsContainer.appendChild(conn);
        }

        // Row DOM
        const rowEl = document.createElement('div');
        rowEl.className = 'progression-row';
        rowEl.dataset.index = idx;
        rowEl.innerHTML = `
            <div class="row-header">
                <label>Key of I:</label>
                <select class="row-key-select">${keyOptions(defaultKey)}</select>
                <button class="btn primary row-generate-btn">⚡ Generate</button>
                ${idx > 0 ? `<button class="btn danger row-remove-btn">✕ Remove</button>` : ''}
                <span class="row-badge">Row ${idx + 1}</span>
            </div>
            <div class="row-cards"></div>
        `;
        rowsContainer.appendChild(rowEl);

        const state = { key: defaultKey, progression: null, rowEl };
        rows.push(state);

        const keySelect = rowEl.querySelector('.row-key-select');
        keySelect.addEventListener('change', () => {
            state.key = parseInt(keySelect.value);
            // Changing this row's key makes the PREVIOUS row's chord 4 stale → regenerate it
            if (idx > 0 && rows[idx - 1].progression) generateRow(idx - 1);
        });

        rowEl.querySelector('.row-generate-btn').addEventListener('click', () => generateRow(idx));

        const removeBtn = rowEl.querySelector('.row-remove-btn');
        if (removeBtn) removeBtn.addEventListener('click', () => removeRow(idx));

        generateRow(idx);
        updatePlayAllBtn();
    }

    function generateRow(idx) {
        const state    = rows[idx];
        if (!state) return;

        // nextIRoot: if a next row exists, pass its key so chord 4 resolves there
        const nextState  = rows[idx + 1];
        const nextIRoot  = nextState ? nextState.key : null;

        state.progression = LickGenerator.generateProgression(state.key, nextIRoot);
        UIModule.renderRow(state.rowEl, state.progression);

        if (hideToggle.checked) UIModule.toggleEarTraining(true);

        // Update the connector *above* this row (prev row's V/ii label)
        const connAbove = document.getElementById(`connector-${idx}`);
        if (connAbove && rows[idx - 1]) {
            UIModule.updateConnector(connAbove, rows[idx - 1].progression, state.key);
        }

        // Update the connector *below* this row (this row's V/ii label → next row)
        const connBelow = document.getElementById(`connector-${idx + 1}`);
        if (connBelow && rows[idx + 1]) {
            UIModule.updateConnector(connBelow, state.progression, rows[idx + 1].key);
        }

        updatePlayAllBtn();
    }

    function removeRow(idx) {
        if (idx === 0) return;
        const conn = document.getElementById(`connector-${idx}`);
        if (conn) conn.remove();
        rows[idx].rowEl.remove();
        rows.splice(idx, 1);
        // Re-index remaining rows
        rows.forEach((s, i) => { s.rowEl.dataset.index = i; });
        updatePlayAllBtn();
    }

    function playAll() {
        const allNotes = rows.filter(r => r.progression).flatMap(r => r.progression.allNotes);
        if (!allNotes.length) return;

        if (AudioModule.getIsPlaying()) {
            AudioModule.stop();
            playAllBtn.textContent = '▶ Play All';
            return;
        }

        const tempo = parseInt(tempoSlider.value);
        AudioModule.playNotes(allNotes, tempo);
        playAllBtn.textContent = '⏹ Stop';
        const ms = (allNotes.length / (tempo / 60)) * 1000;
        setTimeout(() => { playAllBtn.textContent = '▶ Play All'; }, ms + 300);
    }

    function updatePlayAllBtn() {
        playAllBtn.disabled = !rows.some(r => r.progression);
    }

    function keyOptions(selected) {
        return [
            [0,'C'],[1,'Db'],[2,'D'],[3,'Eb'],[4,'E'],[5,'F'],
            [6,'Gb'],[7,'G'],[8,'Ab'],[9,'A'],[10,'Bb'],[11,'B'],
        ].map(([v, n]) =>
            `<option value="${v}"${v === selected ? ' selected' : ''}>${n}</option>`
        ).join('');
    }

    // Start with one row in C
    addRow(0);
});
