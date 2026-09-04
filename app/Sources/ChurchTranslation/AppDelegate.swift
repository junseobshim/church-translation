import AppKit

/// The menu-bar extra is the app's persistent presence: closing the main
/// window stops a running session (see windowWillClose below) but does NOT
/// quit the app — it keeps going, reachable via the menu bar icon. Quitting —
/// from the menu bar, the Dock, or Cmd+Q — gracefully stops a running session
/// first (SIGINT, waits for main.py's own shutdown handler to tear down the
/// tunnel/session) before actually terminating.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // NSWindow.willCloseNotification fires for every window (Settings,
        // the menu-bar extra's own popover, the main window) — filter to the
        // main window by its title (set via .navigationTitle in ContentView)
        // so closing Settings or the menu-bar dropdown doesn't stop a session.
        NotificationCenter.default.addObserver(
            forName: NSWindow.willCloseNotification,
            object: nil,
            queue: .main
        ) { note in
            guard let window = note.object as? NSWindow,
                  window.title == "Church Translation Control Panel" else { return }
            SessionController.shared.stop()
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let session = SessionController.shared
        guard case .running = session.state else {
            return .terminateNow
        }
        session.onStopped = {
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        session.stop()
        return .terminateLater
    }
}
