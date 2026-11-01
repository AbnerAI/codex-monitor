// Read or publish a user variable only in the explicitly authorized session.
function run(argv) {
    if (argv.length !== 3) {
        throw new Error('Expected action, authorized session UUID, and payload JSON');
    }
    const action = argv[0];
    const expected = argv[1];
    const payload = JSON.parse(argv[2]);
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
        throw new Error('Expected an object payload');
    }
    if (action !== 'identify' && action !== 'probe' && action !== 'variables') {
        throw new Error('Unknown action: ' + action);
    }
    if (action !== 'identify' && !/^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$/.test(expected)) {
        throw new Error('Expected an exact authorized session UUID');
    }
    if (action === 'variables' && typeof payload.label !== 'string') {
        throw new Error('Expected a string monitor label');
    }

    const app = Application('com.googlecode.iterm2');
    if (!app.running()) {
        throw new Error('iTerm2 is not running; refusing to launch it');
    }
    const win = app.currentWindow();
    const selected = win.currentSession();
    const id = selected.uniqueID();
    if (action === 'identify') {
        return JSON.stringify({session_id: id.toUpperCase(), tty: selected.tty(),
            panes: win.currentTab().sessions().length});
    }
    if (id.toLowerCase() !== expected.toLowerCase()) {
        return JSON.stringify({error: {code: 'iterm_session_mismatch',
            expected_session_id: expected.toUpperCase(), observed_session_id: id.toUpperCase()}});
    }
    const tab = win.currentTab();
    // Pin every later operation to the UUID, even if the user switches tabs.
    const session = tab.sessions.byId(expected);
    const pinnedID = session.uniqueID();
    if (pinnedID.toLowerCase() !== expected.toLowerCase()) {
        return JSON.stringify({error: {code: 'iterm_session_mismatch',
            expected_session_id: expected.toUpperCase(), observed_session_id: pinnedID.toUpperCase()}});
    }
    const panes = tab.sessions().length;
    if (panes !== 1) {
        throw new Error('This adapter currently requires a single-pane tab');
    }
    if (action === 'variables') {
        session.setVariable({named: 'user.codexMonitor', to: payload.label});
    }
    return JSON.stringify({
        session_id: id,
        tty: session.tty(),
        panes: tab.sessions().length,
        tab_title: tab.title(),
        title_format: session.variable({named: 'tab.titleOverrideFormat'}),
        monitor: session.variable({named: 'user.codexMonitor'})
    });
}
