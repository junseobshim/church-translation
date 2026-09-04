import SwiftUI

/// Menu-bar status icon + small dropdown (doc §2.3, marked optional — built by
/// request). Only offers Stop and window/quit controls, not a full Start —
/// starting needs the source/target/device/outline/tunnel picker state that
/// lives in ContentView, not worth duplicating here for a glance-and-control
/// surface.
struct MenuBarLabelView: View {
    @Environment(SessionController.self) private var session

    var body: some View {
        Image(systemName: iconName)
    }

    private var iconName: String {
        switch session.state {
        case .idle: "mic.slash"
        case .running: "mic.fill"
        case .failed: "exclamationmark.triangle.fill"
        }
    }
}

struct MenuBarContentView: View {
    @Environment(SessionController.self) private var session
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            statusText

            if isRunning {
                Button("Stop") { session.stop() }
            }

            Divider()

            Button("Open Church Translation") {
                NSApp.activate(ignoringOtherApps: true)
                openWindow(id: "main")
            }
            Button("Quit") { NSApp.terminate(nil) }
        }
        .padding(10)
        .frame(width: 220, alignment: .leading)
    }

    private var isRunning: Bool {
        if case .running = session.state { return true }
        return false
    }

    @ViewBuilder
    private var statusText: some View {
        switch session.state {
        case .idle:
            Text("Idle").foregroundStyle(.secondary)
        case .running(let pid):
            Text("Running (pid \(pid))").foregroundStyle(.green)
        case .failed:
            Text("Session failed — see Log tab").foregroundStyle(.red)
        }
    }
}
