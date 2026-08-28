import SwiftUI

/// API keys move from .env to Keychain (docs/native-macos-app-migration.md
/// §2.3/§2.4). Opened via the app's standard Settings scene (Cmd+,).
struct SettingsView: View {
    @State private var sonioxKey: String = KeychainStore.get("SONIOX_API_KEY") ?? ""
    @State private var anthropicKey: String = KeychainStore.get("ANTHROPIC_API_KEY") ?? ""
    @State private var saved = false

    var body: some View {
        Form {
            SecureField("Soniox API Key", text: $sonioxKey)
            SecureField("Anthropic API Key", text: $anthropicKey)

            HStack {
                Button("Save") {
                    KeychainStore.set(sonioxKey, for: "SONIOX_API_KEY")
                    KeychainStore.set(anthropicKey, for: "ANTHROPIC_API_KEY")
                    saved = true
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { saved = false }
                }
                if saved {
                    Text("Saved").foregroundStyle(.secondary)
                }
            }

            Text("Leave blank to keep using .env (repo checkout dev mode only).")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(20)
        .frame(width: 380)
    }
}
