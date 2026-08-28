import AppKit

/// Closing the window quits the app, and quitting gracefully stops a running
/// session first (SIGINT, waits for main.py's own shutdown handler to tear
/// down the tunnel/session) before actually terminating. Replaces needing a
/// separate heartbeat/watchdog: the app process's own lifetime is the signal.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
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
