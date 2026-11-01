// Read or publish a user variable only in the explicitly authorized session.
function run(argv) {
    if (argv.length !== 3) {
        throw new Error('Expected action, authorized session UUID, and payload JSON');
    }
    const action = argv[0];
    const expected = argv[1];
    const payload = JSON.parse(argv[2]);
    if (!expected || !payload || typeof payload !== 'object' || Array.isArray(payload)) {
        throw new Error('Expected a session UUID and an object payload');
    }
    if (action !== 'probe' && action !== 'variables') {
        throw new Error('Unknown action: ' + action);
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
    if (id.toLowerCase() !== expected.toLowerCase()) {
        throw new Error('Current session mismatch; nothing changed');
    }
    const tab = win.currentTab();
    // Pin every later operation to the UUID, even if the user switches tabs.
    const session = tab.sessions.byId(expected);
    if (session.uniqueID().toLowerCase() !== expected.toLowerCase()) {
        throw new Error('Authorized session is unavailable; nothing changed');
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
