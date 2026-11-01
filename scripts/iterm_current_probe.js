function run(argv) {
    const expected = argv[0];
    const app = Application('com.googlecode.iterm2');
    if (!app.running()) throw new Error('iTerm2 is not running; refusing to launch it');
    const win = app.currentWindow();
    const session = win.currentSession();
    const id = session.uniqueID();
    if (id.toLowerCase() !== expected.toLowerCase()) throw new Error('Current session mismatch; nothing changed');
    const tab = win.currentTab();
    const panes = tab.sessions().length;
    if (panes !== 1) throw new Error('This tab has multiple panes; refusing a tab-wide title change');
    return JSON.stringify({session_id: id, tty: session.tty(), panes: panes,
        title_format: session.variable({named: 'tab.titleOverrideFormat'}),
        tab_title: tab.title(), monitor: session.variable({named: 'user.codexMonitor'})});
}
