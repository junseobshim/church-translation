import SwiftUI

@main
struct ChurchTranslationApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup(id: "main") {
            ContentView()
                .environment(SessionController.shared)
        }
        .windowResizability(.contentMinSize)

        MenuBarExtra {
            MenuBarContentView()
                .environment(SessionController.shared)
        } label: {
            MenuBarLabelView()
                .environment(SessionController.shared)
        }
        .menuBarExtraStyle(.window)

        Settings {
            SettingsView()
        }
        .windowResizability(.contentMinSize)
    }
}
