// macOS geometry only. Select a process by PID and an exact, unique window title.
ObjC.import('AppKit');

function fail(code) {
    var error = new Error(code);
    error.demoCode = code;
    throw error;
}

function perform(argv) {
    var screens = $.NSScreen.screens;
    if (Number(screens.count) < 1)
        fail('no-display');
    var se = Application('com.apple.systemevents');
    if (!se.uiElementsEnabled())
        fail('accessibility-required');
    if (argv[0] === 'screen') {
        var mouse = $.NSEvent.mouseLocation;
        var selected = screens.objectAtIndex(0);
        for (var i = 0; i < screens.count; i++) {
            var candidate = screens.objectAtIndex(i);
            var f = candidate.frame;
            if (mouse.x >= f.origin.x && mouse.x < f.origin.x + f.size.width &&
                mouse.y >= f.origin.y && mouse.y < f.origin.y + f.size.height)
                selected = candidate;
        }
        var frame = selected.visibleFrame;
        var primary = screens.objectAtIndex(0).frame;
        // Cocoa has a bottom-left origin; Accessibility has a top-left origin.
        return JSON.stringify({x: frame.origin.x,
            y: primary.origin.y + primary.size.height - frame.origin.y - frame.size.height,
            width: frame.size.width, height: frame.size.height});
    }
    if (argv[0] !== 'place') fail('unknown-operation');
    var processes = se.applicationProcesses.whose({unixId: Number(argv[1])})();
    if (processes.length === 0) fail('process-not-ready');
    if (processes.length !== 1) fail('ambiguous-process');
    var process = processes[0];
    var windows = process.windows.whose({name: argv[2]})();
    if (windows.length === 0) fail('window-not-ready');
    if (windows.length !== 1) fail('ambiguous-window');
    var rect = JSON.parse(argv[3]);
    var window = windows[0];
    window.size = [rect.width, rect.height];
    window.position = [rect.x, rect.y];
    var size = window.size();
    var position = window.position();
    if (Math.abs(size[0] - rect.width) > 24 || Math.abs(size[1] - rect.height) > 24 ||
        Math.abs(position[0] - rect.x) > 24 || Math.abs(position[1] - rect.y) > 24)
        fail('geometry-rejected');
    return JSON.stringify({x: position[0], y: position[1], width: size[0], height: size[1]});
}

function run(argv) {
    try {
        return perform(argv);
    } catch (error) {
        if (error.demoCode) return JSON.stringify({error: {code: error.demoCode}});
        var number = Number(error.errorNumber || error.number);
        if (number === -1743) return JSON.stringify({error: {code: 'automation-required'}});
        throw error;
    }
}
