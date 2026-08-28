import SwiftUI

@main
struct ChurchTranslationApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environment(SessionController.shared)
        }
        .windowResizability(.contentSize)

        Settings {
            SettingsView()
        }
    }
}
